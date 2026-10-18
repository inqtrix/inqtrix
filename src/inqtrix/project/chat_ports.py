"""Contracts of the chat-history store (Baukasten port + dataclasses).

The store owns persistence only: scoping decisions (which threads a
caller may see, whether a write needs an edit grant) live in
:class:`~inqtrix.services.chat_history_service.ChatHistoryService`, and
the wire shape lives in the router. Two implementations behind the same
port: :class:`~inqtrix.project.chat_memory.MemoryChatStore` (the tier
without Postgres, also the offline test backend) and
:class:`~inqtrix.project.chat_postgres.PostgresChatStore`. All methods
are async — the platform persistence layer is async end-to-end
(asyncpg), and a uniform async port lets the HTTP routes ``await``
directly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.session_titles import TitleSource


class ThreadNotFound(KeyError):
    """Raised when a thread id is unknown to the store (maps to HTTP 404)."""


class ThreadGroupNotFound(KeyError):
    """Raised when a thread-group id is unknown to the store (HTTP 404)."""


@dataclass(frozen=True)
class ChatThreadGroup:
    """One grouping of a user's chat threads.

    Attributes:
        id: Client-supplied stable id (``ctg_...``), the primary key.
        title: User-facing group label.
        created_at: Unix timestamp of creation.
        updated_at: Unix timestamp of the last metadata change.
        tenant_id: Tenant scope (v1 runs one tenant per deployment).
        created_by_user_id: Ownership anchor. ``None`` = unscoped/anonymous
            deployments (the single implicit owner) — the established
            compatibility rule.
        workspace_id: Workspace the group's project lives in (``None``
            for no-workspace/anonymous deployments).
    """

    id: str
    title: str
    created_at: float
    updated_at: float
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None


@dataclass(frozen=True)
class ChatThread:
    """One chat conversation (metadata only — messages are paginated).

    Attributes:
        id: Client-supplied stable id (``ct_...``), the primary key.
        title: Conversation title.
        preview: Denormalized last-line preview shown in the thread list.
        source: Origin marker round-tripped from the client
            (``api``/``imported``/``mock``).
        group_id: Owning :class:`ChatThreadGroup` id, or ``None`` when
            ungrouped.
        created_at: Unix timestamp of creation (the stable list-sort key).
        updated_at: Unix timestamp of the last activity (display sort on
            the client; never the keyset key because it mutates).
        tenant_id: Tenant scope.
        created_by_user_id: Ownership anchor (see :class:`ChatThreadGroup`).
        workspace_id: Workspace the thread's project lives in.
        model_selection: The model picked inside this thread, as
            client-owned JSON (the ``items_json`` pattern). ``''`` means
            nothing was picked here.
    """

    id: str
    title: str
    preview: str
    source: str
    group_id: str | None
    created_at: float
    updated_at: float
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None
    model_selection: str = ""
    title_revision: int = 0
    title_source: TitleSource = "manual"


@dataclass(frozen=True)
class ChatMessage:
    """One message in a thread.

    Attributes:
        id: Client-supplied stable id (``cm_...``), the primary key.
        thread_id: Owning thread.
        role: ``user`` or ``assistant``.
        content_markdown: The rendered message body.
        metadata: Verbatim optional client fields (``attachments``,
            ``chainTrace``, ``modelResolution``) — stored as-is so a
            round-trip reconstructs the exact ``ChatMessageRecord``,
            never reinterpreted.
        created_at: Unix timestamp (stable; the keyset key).
    """

    id: str
    thread_id: str
    role: str
    content_markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


@runtime_checkable
class ChatStore(Protocol):
    """Persistence port for chat threads, their groups, and messages.

    Scoping note: ``list_threads_page`` / ``list_groups`` take the
    resolved ``created_by_user_id`` and ``workspace_id`` and filter in the
    query so the DB ``LIMIT`` is never under-filled (the keyset-page
    correctness rule). ``get_thread`` returns the row unscoped — the
    service applies the owner/share access check on top (the
    knowledge-store pattern), so denial and absence stay byte-identical.
    """

    async def upsert_thread(
        self,
        *,
        id: str,
        title: str,
        preview: str,
        source: str,
        group_id: str | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        model_selection: str = "",
        title_revision: int | None = None,
        title_source: TitleSource = "manual",
    ) -> ChatThread:
        """Create or idempotently update a thread by id (autosave upsert).

        On an existing id only the mutable metadata (preview,
        source, group_id, model_selection, updated_at) is overwritten;
        title intent is reconciled against its own revision and source.
        ``created_at`` and the ownership columns are never reassigned by a
        later write.
        """
        ...

    async def apply_generated_title(
        self, thread_id: str, *, title: str, revision: int, scope: ResourceScope
    ) -> bool:
        """Replace an initial title at the given revision; never create a row."""
        ...

    async def list_threads_page(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[ChatThread], str | None]:
        """One keyset page of the caller's threads (newest first).

        ``created_by_user_id`` ``None`` (unscoped caller) lists every thread
        in the tenant; a set value scopes to that owner. ``workspace_id``
        further partitions to one project when provided. Ordering is
        ``(created_at, id)`` descending; returns the page and the
        ``next_cursor`` (``None`` on the last page)."""
        ...

    async def get_thread(self, thread_id: str) -> ChatThread:
        """One thread (unscoped fetch) or :class:`ThreadNotFound`."""
        ...

    async def delete_thread(
        self, thread_id: str, *, scope: ResourceScope
    ) -> None:
        """Delete a thread and its messages (cascade)."""
        ...

    async def append_messages(
        self,
        messages: list[ChatMessage],
        *,
        expected_created_by_user_id: uuid.UUID | None,
        expected_workspace_id: str | None,
    ) -> list[ChatMessage]:
        """Idempotently upsert messages by id (append/regenerate-safe).

        An existing message id overwrites only role/content/metadata,
        never ``created_at`` (the conversation order stays stable).
        The expected parent scope is revalidated and locked in the same
        transaction as the child write, preventing a deleted thread id from
        being recreated under another owner between service authorization and
        persistence. Returns the stored messages.
        """
        ...

    async def delete_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        expected_created_by_user_id: uuid.UUID | None,
        expected_workspace_id: str | None,
    ) -> None:
        """Delete one message from a thread (idempotent).

        Scoped on the composite ``(thread_id, id)`` so a re-used id in
        another thread is never touched. A no-op when the row is absent:
        the autosave diff may re-issue a delete after a coalesced burst or
        a multi-device race, and a missing-row error would wedge the
        retry loop (the same idempotency rule the upsert append honours).
        The parent thread scope is locked and checked transactionally before
        the delete.
        """
        ...

    async def list_messages_page(
        self,
        thread_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[ChatMessage], str | None]:
        """One keyset page of a thread's messages (newest first).

        Newest-first like the documents page (the natural chat-history
        load: latest page first, page back for older); the client
        renders each page in chronological order. ``(created_at, id)``
        descending."""
        ...

    async def upsert_group(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> ChatThreadGroup:
        """Create or idempotently update a thread group by id."""
        ...

    async def list_groups(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[ChatThreadGroup]:
        """All of the caller's thread groups, newest first (groups are
        few — no keyset page)."""
        ...

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        """Delete a group; its threads orphan to ungrouped (SET NULL)."""
        ...

    async def aclose(self) -> None:
        """Release backing resources at application shutdown."""
        ...
