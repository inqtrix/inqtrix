"""Knowledge-session persistence service (Wissensmodus saved sessions).

The Ask-view counterpart of the chat-history service: a thin owner-scoped layer
over a :class:`~inqtrix.project.knowledge_sessions_ports.KnowledgeSessionStore`.
Sessions are PRIVATE per user (no sharing surface, like the asset tier);
every denial is the indistinct not-found.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from inqtrix.auth.permissions import require_owned_access
from inqtrix.project.knowledge_sessions_ports import (
    KnowledgeSession,
    KnowledgeSessionGroup,
    KnowledgeSessionGroupNotFound,
    KnowledgeSessionNotFound,
    KnowledgeSessionStore,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.services.workspace_guard import deny_cross_workspace

if TYPE_CHECKING:
    from inqtrix.auth.principal import UserContext
    from inqtrix.runs.ports import RunStorePort


class KnowledgeSessionsService:
    """Application service over a :class:`KnowledgeSessionStore`."""

    def __init__(
        self,
        *,
        store: KnowledgeSessionStore,
        run_store: "RunStorePort | None" = None,
        durable: bool = False,
    ) -> None:
        self._store = store
        self._run_store = run_store
        self._durable = durable

    @property
    def durable(self) -> bool:
        return self._durable

    @property
    def store(self) -> KnowledgeSessionStore:
        return self._store

    async def claim_session(
        self,
        session_id: str,
        *,
        title: str,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        created_at: float | None = None,
    ) -> KnowledgeSession:
        """Atomically create or validate the saved session for one run."""

        claimed = await self._store.claim_session(
            id=session_id,
            title=title,
            created_at=created_at if created_at is not None else time.time(),
            created_by_user_id=caller_user_id,
            workspace_id=workspace_id,
        )
        self._require_owner(claimed, visible_to=visible_to)
        return claimed

    async def list_sessions(
        self, *, caller_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[KnowledgeSession]:
        return await self._store.list_sessions(
            created_by_user_id=caller_user_id, workspace_id=workspace_id
        )

    async def get_session(
        self, session_id: str, *, visible_to: "UserContext | None"
    ) -> KnowledgeSession:
        session = await self._store.get_session(session_id)
        require_owned_access(
            owner_user_id=session.created_by_user_id, resource_tenant_id=session.tenant_id,
            resource_id=session.id, visible_to=visible_to,
            not_found=KnowledgeSessionNotFound,
        )
        return session

    async def save_session(
        self, *, id: str, title: str, items_json: str, group_id: str | None,
        created_at: float,
        updated_at: float,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None, visible_to: "UserContext | None",
    ) -> KnowledgeSession:
        try:
            existing = await self._store.get_session(id)
        except KnowledgeSessionNotFound:
            existing = None
        if existing is not None:
            self._require_owner(existing, visible_to=visible_to)
            if existing.lifecycle_status != "active":
                raise KnowledgeSessionNotFound(id)
            owner_user_id, owner_ws = existing.created_by_user_id, existing.workspace_id
        else:
            owner_user_id, owner_ws = caller_user_id, workspace_id
        return await self._store.upsert_session(
            id=id, title=title, items_json=items_json, group_id=group_id,
            created_at=created_at, updated_at=updated_at, created_by_user_id=owner_user_id,
            workspace_id=owner_ws,
        )

    async def delete_session(
        self, session_id, *, visible_to: "UserContext | None",
        request_workspace_id=None,
    ) -> None:
        session = await self._store.get_session(session_id)
        require_owned_access(
            owner_user_id=session.created_by_user_id, resource_tenant_id=session.tenant_id,
            resource_id=session.id, visible_to=visible_to,
            not_found=KnowledgeSessionNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=session.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: KnowledgeSessionNotFound(session_id),
        )
        await self._store.delete_session(
            session_id, scope=ResourceScope.from_record(session)
        )

    async def prepare_deletion(
        self,
        session_id: str,
        *,
        visible_to: "UserContext | None",
        request_workspace_id: str | None,
    ) -> KnowledgeSession:
        session = await self.get_session(session_id, visible_to=visible_to)
        deny_cross_workspace(
            resource_workspace_id=session.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: KnowledgeSessionNotFound(session_id),
        )
        return session

    async def mark_deletion_state(
        self,
        session: KnowledgeSession,
        *,
        lifecycle_status: str,
        operation_id: str,
        stage: str,
        error: str | None,
    ) -> None:
        await self._store.set_session_deletion_state(
            session.id,
            scope=ResourceScope.from_record(session),
            lifecycle_status=lifecycle_status,
            deletion_operation_id=operation_id,
            deletion_stage=stage,
            deletion_error=error,
        )

    def delete_run_aggregate(
        self,
        session_id: str,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        preparer = getattr(
            self._run_store, "prepare_knowledge_session_aggregate_deletion", None
        )
        if callable(preparer):
            preparer(
                session_id,
                tenant_id=tenant_id,
                requester_user_id=owner_user_id,
                workspace_id=workspace_id,
                run_ids=run_ids,
            )
        elif self._durable:
            raise RuntimeError(
                "durable run store cannot fence an active knowledge session"
            )
        deleter = getattr(
            self._run_store, "delete_knowledge_session_aggregate", None
        )
        if not callable(deleter):
            if self._durable:
                raise RuntimeError(
                    "durable run store cannot delete a knowledge session"
                )
            return
        deleter(
            session_id,
            tenant_id=tenant_id,
            requester_user_id=owner_user_id,
            workspace_id=workspace_id,
            run_ids=run_ids,
        )

    async def delete_registry_for_operation(
        self, session_id: str, *, scope: ResourceScope
    ) -> None:
        try:
            await self._store.delete_session(
                session_id, scope=scope
            )
        except KnowledgeSessionNotFound:
            return

    async def deletion_residual_count(
        self, session_id: str, *, scope: ResourceScope
    ) -> int:
        return await self._store.count_session_residuals(
            session_id, scope=scope
        )

    async def deletion_residuals(
        self,
        session_id: str,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
        scope: ResourceScope,
    ) -> dict[str, int]:
        # The registry tombstone remains until the operation receipt commits.
        del scope
        counter = getattr(self._run_store, "knowledge_session_residuals", None)
        run_residuals = (
            counter(
                session_id,
                tenant_id=tenant_id,
                requester_user_id=owner_user_id,
                workspace_id=workspace_id,
                run_ids=run_ids,
            )
            if callable(counter)
            else {}
        )
        return {
            str(key): int(value) for key, value in dict(run_residuals).items()
        }

    async def save_group(
        self, *, id: str, title: str, created_at: float, updated_at: float,
        caller_user_id: uuid.UUID | None, workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> KnowledgeSessionGroup:
        existing = None
        for group in await self._store.list_groups(created_by_user_id=None, workspace_id=None):
            if group.id == id:
                existing = group
                break
        if existing is not None:
            require_owned_access(
                owner_user_id=existing.created_by_user_id,
                resource_tenant_id=existing.tenant_id,
                resource_id=existing.id,
                visible_to=visible_to,
                not_found=KnowledgeSessionGroupNotFound,
            )
            owner_user_id, owner_ws = existing.created_by_user_id, existing.workspace_id
        else:
            owner_user_id, owner_ws = caller_user_id, workspace_id
        return await self._store.upsert_group(
            id=id, title=title, created_at=created_at, updated_at=updated_at,
            created_by_user_id=owner_user_id, workspace_id=owner_ws,
        )

    async def list_groups(
        self, *, caller_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[KnowledgeSessionGroup]:
        return await self._store.list_groups(
            created_by_user_id=caller_user_id, workspace_id=workspace_id
        )

    async def delete_group(
        self, group_id: str, *, visible_to: "UserContext | None",
        request_workspace_id=None,
    ) -> None:
        existing = None
        for group in await self._store.list_groups(created_by_user_id=None, workspace_id=None):
            if group.id == group_id:
                existing = group
                break
        if existing is None:
            raise KnowledgeSessionGroupNotFound(group_id)
        require_owned_access(
            owner_user_id=existing.created_by_user_id,
            resource_tenant_id=existing.tenant_id,
            resource_id=existing.id,
            visible_to=visible_to,
            not_found=KnowledgeSessionGroupNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=existing.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: KnowledgeSessionGroupNotFound(group_id),
        )
        await self._store.delete_group(
            group_id, scope=ResourceScope.from_record(existing)
        )

    @staticmethod
    def _require_owner(
        session: KnowledgeSession, *, visible_to: "UserContext | None"
    ) -> None:
        require_owned_access(
            owner_user_id=session.created_by_user_id,
            resource_tenant_id=session.tenant_id,
            resource_id=session.id,
            visible_to=visible_to,
            not_found=KnowledgeSessionNotFound,
        )
