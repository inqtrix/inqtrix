"""Chat-history persistence service (project tier).

Owns what the chat-history router delegates: payload validation, the
owner-only access rule, and the resolution of "which threads belong to
this caller" before the store's keyset query runs. Distinct from the AI
:class:`~inqtrix.services.chat_service.ChatService` (completions) — this
service never calls a model; it persists and reads the conversation
record.

Ownership model: threads/groups carry ``created_by_user_id``. ``None``
(anonymous/static principals) stays visible to every caller — the
compatibility rule. Messages inherit access from their parent thread.
Every denial is the indistinct :class:`ThreadNotFound`/
:class:`ThreadGroupNotFound` (existence is not disclosed) — the same
hide-on-deny rule as collections.
"""

from __future__ import annotations

import uuid
from typing import Any, TYPE_CHECKING

from inqtrix.auth.permissions import require_owned_access
from inqtrix.services.workspace_guard import deny_cross_workspace
from inqtrix.project.chat_ports import (
    ChatMessage,
    ChatStore,
    ChatThread,
    ChatThreadGroup,
    ThreadGroupNotFound,
    ThreadNotFound,
)
from inqtrix.project.session_titles import TitleSource
from inqtrix.project.scoped_upsert import ResourceScope

if TYPE_CHECKING:
    from inqtrix.auth.principal import UserContext

_VALID_ROLES = frozenset({"user", "assistant"})
_VALID_SOURCES = frozenset({"api", "imported", "mock"})
_MODEL_SELECTION_MAX_CHARS = 2000


class ChatValidationError(ValueError):
    """Raised for client-payload problems (maps to HTTP 400).

    The role/source domain is rejected here, before the database CHECK
    constraint, so a bad value is a clean 400 instead of an opaque 500
    (No Silent Fallbacks: the failure is visible and attributable)."""


class ChatHistoryService:
    """Application service over a :class:`ChatStore`.

    Args:
        store: The wired chat store (memory or Postgres).
        durable: Whether the store survives a restart (Postgres). The
            capability manifest advertises project persistence only when
            this is ``True`` — a volatile store must not invite the
            frontend to abandon its durable local markdown project for an
            ephemeral server tier (the prompt-template ``durable`` rule).
    """

    def __init__(self, *, store: ChatStore, durable: bool = False) -> None:
        self._store = store
        self._durable = durable

    @property
    def store(self) -> ChatStore:
        """The wired chat store (shutdown disposes its engine)."""
        return self._store

    @property
    def durable(self) -> bool:
        """Whether the backing store survives a restart (capability gate)."""
        return self._durable

    # -- threads ---------------------------------------------------------- #

    async def save_thread(
        self,
        *,
        id: str,
        title: str,
        preview: str,
        source: str,
        group_id: str | None,
        created_at: float,
        updated_at: float,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        model_selection: str = "",
        title_revision: int | None = None,
        title_source: TitleSource = "manual",
    ) -> ChatThread:
        """Create or update a thread (idempotent autosave).

        A new id is owned by the caller; an existing id must belong to the
        caller and keeps its original owner/workspace.
        """
        if source not in _VALID_SOURCES:
            raise ChatValidationError(f"unknown thread source: {source!r}")
        # Client-owned JSON, the items_json trust model: content is the
        # client's business, an unbounded blob is not.
        if len(model_selection) > _MODEL_SELECTION_MAX_CHARS:
            raise ChatValidationError(
                f"model_selection exceeds {_MODEL_SELECTION_MAX_CHARS} characters"
            )
        try:
            existing = await self._store.get_thread(id)
        except ThreadNotFound:
            existing = None
        if existing is not None:
            require_owned_access(
                owner_user_id=existing.created_by_user_id,
                resource_tenant_id=existing.tenant_id,
                resource_id=existing.id,
                visible_to=visible_to,
                not_found=ThreadNotFound,
            )
            owner_user_id = existing.created_by_user_id
            owner_workspace = existing.workspace_id
        else:
            owner_user_id = caller_user_id
            owner_workspace = workspace_id
        return await self._store.upsert_thread(
            id=id,
            title=title,
            preview=preview,
            source=source,
            group_id=group_id,
            created_at=created_at,
            updated_at=updated_at,
            created_by_user_id=owner_user_id,
            workspace_id=owner_workspace,
            model_selection=model_selection,
            title_revision=title_revision,
            title_source=title_source,
        )

    async def list_threads(
        self,
        *,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[ChatThread], str | None]:
        """One keyset page of the caller's own threads (newest first)."""
        return await self._store.list_threads_page(
            created_by_user_id=caller_user_id,
            workspace_id=workspace_id,
            limit=limit,
            after=after,
        )

    async def get_thread(
        self,
        thread_id: str,
        *,
        visible_to: "UserContext | None",
    ) -> ChatThread:
        """One thread the caller may see, or :class:`ThreadNotFound`."""
        thread = await self._store.get_thread(thread_id)
        require_owned_access(
            owner_user_id=thread.created_by_user_id,
            resource_tenant_id=thread.tenant_id,
            resource_id=thread.id,
            visible_to=visible_to,
            not_found=ThreadNotFound,
        )
        return thread

    async def delete_thread(
        self,
        thread_id: str,
        *,
        visible_to: "UserContext | None",
        request_workspace_id: str | None = None,
    ) -> None:
        """Delete a thread (owner-only) with its messages (cascade)."""
        thread = await self._store.get_thread(thread_id)
        require_owned_access(
            owner_user_id=thread.created_by_user_id,
            resource_tenant_id=thread.tenant_id,
            resource_id=thread.id,
            visible_to=visible_to,
            not_found=ThreadNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=thread.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: ThreadNotFound(thread_id),
        )
        await self._store.delete_thread(
            thread_id, scope=ResourceScope.from_record(thread)
        )

    # -- messages --------------------------------------------------------- #

    async def append_messages(
        self,
        thread_id: str,
        *,
        messages: list[dict[str, Any]],
        visible_to: "UserContext | None",
    ) -> list[ChatMessage]:
        """Append/upsert messages into a thread the caller may edit."""
        thread = await self._store.get_thread(thread_id)
        require_owned_access(
            owner_user_id=thread.created_by_user_id,
            resource_tenant_id=thread.tenant_id,
            resource_id=thread.id,
            visible_to=visible_to,
            not_found=ThreadNotFound,
        )
        parsed = [self._parse_message(thread_id, raw) for raw in messages]
        return await self._store.append_messages(
            parsed,
            expected_created_by_user_id=thread.created_by_user_id,
            expected_workspace_id=thread.workspace_id,
        )

    async def delete_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        visible_to: "UserContext | None",
        request_workspace_id: str | None = None,
    ) -> None:
        """Delete one message from a thread owned by the caller.

        Only an inaccessible/unknown thread raises the indistinct
        :class:`ThreadNotFound`; a missing message is the store's no-op
        (the idempotency rule the port documents).
        """
        thread = await self._store.get_thread(thread_id)
        require_owned_access(
            owner_user_id=thread.created_by_user_id,
            resource_tenant_id=thread.tenant_id,
            resource_id=thread.id,
            visible_to=visible_to,
            not_found=ThreadNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=thread.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: ThreadNotFound(thread_id),
        )
        await self._store.delete_message(
            thread_id,
            message_id,
            expected_created_by_user_id=thread.created_by_user_id,
            expected_workspace_id=thread.workspace_id,
        )

    async def list_messages(
        self,
        thread_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
        visible_to: "UserContext | None",
    ) -> tuple[list[ChatMessage], str | None]:
        """One keyset page of a readable thread's messages (newest first)."""
        await self.get_thread(thread_id, visible_to=visible_to)
        return await self._store.list_messages_page(
            thread_id, limit=limit, after=after
        )

    # -- groups ----------------------------------------------------------- #

    async def save_group(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        updated_at: float,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> ChatThreadGroup:
        """Create or update a thread group (idempotent)."""
        existing = None
        for group in await self._store.list_groups(
            created_by_user_id=None, workspace_id=None
        ):
            if group.id == id:
                existing = group
                break
        if existing is not None:
            require_owned_access(
                owner_user_id=existing.created_by_user_id,
                resource_tenant_id=existing.tenant_id,
                resource_id=existing.id,
                visible_to=visible_to,
                not_found=ThreadGroupNotFound,
            )
            owner_user_id = existing.created_by_user_id
            owner_workspace = existing.workspace_id
        else:
            owner_user_id = caller_user_id
            owner_workspace = workspace_id
        return await self._store.upsert_group(
            id=id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            created_by_user_id=owner_user_id,
            workspace_id=owner_workspace,
        )

    async def list_groups(
        self,
        *,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[ChatThreadGroup]:
        """All of the caller's thread groups (newest first)."""
        return await self._store.list_groups(
            created_by_user_id=caller_user_id, workspace_id=workspace_id
        )

    async def delete_group(
        self,
        group_id: str,
        *,
        visible_to: "UserContext | None",
        request_workspace_id: str | None = None,
    ) -> None:
        """Delete a group (its threads orphan to ungrouped)."""
        existing = None
        for group in await self._store.list_groups(
            created_by_user_id=None, workspace_id=None
        ):
            if group.id == group_id:
                existing = group
                break
        if existing is None:
            raise ThreadGroupNotFound(group_id)
        require_owned_access(
            owner_user_id=existing.created_by_user_id,
            resource_tenant_id=existing.tenant_id,
            resource_id=existing.id,
            visible_to=visible_to,
            not_found=ThreadGroupNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=existing.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: ThreadGroupNotFound(group_id),
        )
        await self._store.delete_group(
            group_id, scope=ResourceScope.from_record(existing)
        )

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _parse_message(thread_id: str, raw: dict[str, Any]) -> ChatMessage:
        message_id = raw.get("id")
        role = raw.get("role")
        if not isinstance(message_id, str) or not message_id:
            raise ChatValidationError("message id is required")
        if role not in _VALID_ROLES:
            raise ChatValidationError(f"unknown message role: {role!r}")
        content = raw.get("content_markdown", "")
        if not isinstance(content, str):
            raise ChatValidationError("content_markdown must be a string")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ChatValidationError("message metadata must be an object")
        created_at = raw.get("created_at")
        if not isinstance(created_at, (int, float)):
            raise ChatValidationError("message created_at must be a number")
        return ChatMessage(
            id=message_id,
            thread_id=thread_id,
            role=role,
            content_markdown=content,
            metadata=dict(metadata),
            created_at=float(created_at),
        )
