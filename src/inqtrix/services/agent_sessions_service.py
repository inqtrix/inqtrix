"""Agent-session persistence service (Agent-Desk saved sessions).

The Agent-Desk counterpart of the chat-history service: a thin owner-scoped layer
over a :class:`~inqtrix.project.agent_sessions_ports.AgentSessionStore`.
Sessions are PRIVATE per user (no sharing surface, like the asset tier);
every denial is the indistinct not-found.
"""

from __future__ import annotations

import uuid
import time
from typing import TYPE_CHECKING

from inqtrix.auth.permissions import require_owned_access
from inqtrix.project.agent_sessions_ports import (
    AgentSession,
    AgentSessionGroup,
    AgentSessionGroupNotFound,
    AgentSessionNotFound,
    AgentSessionStore,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.session_titles import TitleSource
from inqtrix.services.workspace_guard import deny_cross_workspace

if TYPE_CHECKING:
    from inqtrix.auth.principal import UserContext
    from inqtrix.runs.ports import RunStorePort


class AgentSessionsService:
    """Application service over a :class:`AgentSessionStore`."""

    def __init__(
        self,
        *,
        store: AgentSessionStore,
        run_store: "RunStorePort | None" = None,
        durable: bool = False,
    ) -> None:
        self._store = store
        self._run_store = run_store
        self._durable = durable
        bind_retention = getattr(run_store, "bind_session_retention", None)
        retained = getattr(store, "retains_run_context", None)
        if callable(bind_retention) and callable(retained):
            bind_retention(retained)

    @property
    def durable(self) -> bool:
        return self._durable

    @property
    def store(self) -> AgentSessionStore:
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
        title_source: TitleSource = "manual",
    ) -> AgentSession:
        """Atomically claim or validate an agent execution session.

        A deleted registry row does not erase its historical ownership: when
        runs with the same id remain, only their one unambiguous owner may
        recreate it.  The final store claim is insert-if-absent, and the
        returned row is checked again so concurrent foreign claimants cannot
        win through a check/insert race.
        """
        owners = (
            self._run_store.session_owners(session_id)
            if self._run_store is not None
            else set()
        )
        try:
            existing = await self._store.get_session(session_id)
        except AgentSessionNotFound:
            existing = None
        if existing is not None:
            self._require_owner(existing, visible_to=visible_to)
            if existing.lifecycle_status != "active":
                raise AgentSessionNotFound(session_id)
            if owners and visible_to is not None:
                if len(owners) != 1:
                    raise AgentSessionNotFound(session_id)
                historical_tenant, historical_user_id = next(iter(owners))
                if (
                    existing.created_by_user_id != historical_user_id
                    or historical_tenant
                    not in (None, existing.tenant_id)
                ):
                    raise AgentSessionNotFound(session_id)
            return existing

        if owners:
            if len(owners) != 1:
                raise AgentSessionNotFound(session_id)
            historical_tenant, historical_user_id = next(iter(owners))
            if visible_to is not None:
                principal = visible_to.principal
                if (
                    historical_user_id != principal.user_id
                    or historical_tenant not in (None, principal.tenant_id)
                ):
                    raise AgentSessionNotFound(session_id)

        claimed = await self._store.claim_session(
            id=session_id,
            title=title,
            created_at=created_at if created_at is not None else time.time(),
            title_source=title_source,
            created_by_user_id=caller_user_id,
            workspace_id=workspace_id,
        )
        self._require_owner(claimed, visible_to=visible_to)
        return claimed

    async def list_sessions(
        self, *, caller_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSession]:
        return await self._store.list_sessions(
            created_by_user_id=caller_user_id, workspace_id=workspace_id
        )

    async def get_session(
        self, session_id: str, *, visible_to: "UserContext | None"
    ) -> AgentSession:
        session = await self._store.get_session(session_id)
        require_owned_access(
            owner_user_id=session.created_by_user_id, resource_tenant_id=session.tenant_id,
            resource_id=session.id, visible_to=visible_to,
            not_found=AgentSessionNotFound,
        )
        return session

    async def save_session(
        self, *, id: str, title: str, items_json: str, group_id: str | None,
        created_at: float,
        updated_at: float,
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None, visible_to: "UserContext | None",
        title_revision: int | None = None,
        title_source: TitleSource = "manual",
    ) -> AgentSession:
        claimed = await self.claim_session(
            id,
            title=title,
            caller_user_id=caller_user_id,
            workspace_id=workspace_id,
            visible_to=visible_to,
            created_at=created_at,
            title_source=title_source,
        )
        owner_user_id, owner_ws = claimed.created_by_user_id, claimed.workspace_id
        return await self._store.upsert_session(
            id=id, title=title, items_json=items_json, group_id=group_id,
            created_at=created_at, updated_at=updated_at, created_by_user_id=owner_user_id,
            workspace_id=owner_ws,
            title_revision=title_revision, title_source=title_source,
        )

    async def delete_session(
        self, session_id, *, visible_to: "UserContext | None",
        request_workspace_id=None,
    ) -> None:
        session = await self._store.get_session(session_id)
        require_owned_access(
            owner_user_id=session.created_by_user_id, resource_tenant_id=session.tenant_id,
            resource_id=session.id, visible_to=visible_to,
            not_found=AgentSessionNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=session.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: AgentSessionNotFound(session_id),
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
    ) -> AgentSession:
        session = await self.get_session(session_id, visible_to=visible_to)
        deny_cross_workspace(
            resource_workspace_id=session.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: AgentSessionNotFound(session_id),
        )
        return session

    async def mark_deletion_state(
        self,
        session: AgentSession,
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
        checkpointer: object | None,
    ) -> None:
        preparer = getattr(
            self._run_store, "prepare_agent_session_aggregate_deletion", None
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
                "durable run store cannot fence an active agent session"
            )
        if run_ids and checkpointer is None:
            raise RuntimeError(
                "agent checkpoint store is unavailable for verified deletion"
            )
        if checkpointer is not None:
            delete_thread = getattr(checkpointer, "delete_thread_strict", None)
            if not callable(delete_thread):
                raise RuntimeError("agent checkpointer has no strict deletion surface")
            for run_id in run_ids:
                delete_thread(run_id)
        deleter = getattr(self._run_store, "delete_agent_session_aggregate", None)
        if not callable(deleter):
            if self._durable:
                raise RuntimeError("durable run store cannot delete an agent session")
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
        except AgentSessionNotFound:
            return

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
        # The session tombstone is intentionally retained until the deletion
        # operation and row removal commit atomically.  Only dependent data is
        # expected to be absent at this point.
        del scope
        counter = getattr(self._run_store, "agent_session_residuals", None)
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
    ) -> AgentSessionGroup:
        claimed = await self._store.claim_group(
            id=id,
            title=title,
            created_at=created_at,
            created_by_user_id=caller_user_id,
            workspace_id=workspace_id,
        )
        self._require_group_owner(claimed, visible_to=visible_to)
        owner_user_id, owner_ws = claimed.created_by_user_id, claimed.workspace_id
        return await self._store.upsert_group(
            id=id, title=title, created_at=created_at, updated_at=updated_at,
            created_by_user_id=owner_user_id, workspace_id=owner_ws,
        )

    async def list_groups(
        self, *, caller_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSessionGroup]:
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
            raise AgentSessionGroupNotFound(group_id)
        require_owned_access(
            owner_user_id=existing.created_by_user_id,
            resource_tenant_id=existing.tenant_id,
            resource_id=existing.id,
            visible_to=visible_to,
            not_found=AgentSessionGroupNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=existing.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: AgentSessionGroupNotFound(group_id),
        )
        await self._store.delete_group(
            group_id, scope=ResourceScope.from_record(existing)
        )

    @staticmethod
    def _require_owner(
        session: AgentSession, *, visible_to: "UserContext | None"
    ) -> None:
        require_owned_access(
            owner_user_id=session.created_by_user_id,
            resource_tenant_id=session.tenant_id,
            resource_id=session.id,
            visible_to=visible_to,
            not_found=AgentSessionNotFound,
        )

    @staticmethod
    def _require_group_owner(
        group: AgentSessionGroup, *, visible_to: "UserContext | None"
    ) -> None:
        require_owned_access(
            owner_user_id=group.created_by_user_id,
            resource_tenant_id=group.tenant_id,
            resource_id=group.id,
            visible_to=visible_to,
            not_found=AgentSessionGroupNotFound,
        )
