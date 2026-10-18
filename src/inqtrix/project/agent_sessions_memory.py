"""In-memory agent-session store (tier without Postgres + offline test).

Mirrors :class:`~inqtrix.project.agent_sessions_postgres.PostgresAgentSessionStore`:
``list_sessions`` and ``get_session`` return full rows with their small
``items_json`` settings body. Process-local, not durable.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, replace

from inqtrix.project.agent_sessions_ports import (
    AgentSession,
    AgentSessionGroup,
    AgentSessionGroupNotFound,
    AgentSessionNotFound,
)
from inqtrix.project.scoped_upsert import ResourceScope, require_memory_scope
from inqtrix.project.session_titles import TitleSource, title_after_save


class MemoryAgentSessionStore:
    """Process-local
    :class:`~inqtrix.project.agent_sessions_ports.AgentSessionStore`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, AgentSession] = {}
        self._groups: dict[str, AgentSessionGroup] = {}

    async def claim_session(
        self, *, id: str, title: str, created_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
        title_source: TitleSource = "manual",
    ) -> AgentSession:
        with self._lock:
            existing = self._sessions.get(id)
            if existing is not None:
                if existing.lifecycle_status != "active":
                    raise AgentSessionNotFound(id)
                return existing
            session = AgentSession(
                id=id,
                title=title,
                items_json="[]",
                title_source=title_source,
                created_at=created_at,
                updated_at=created_at,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
            self._sessions[id] = session
            return session

    def retains_run_context(self, *, session_id, tenant_id, owner_user_id, workspace_id) -> bool:
        """Saved conversations own their run lifetime, including deletion retries."""
        with self._lock:
            session = self._sessions.get(session_id)
            return bool(
                session is not None and session.tenant_id == tenant_id
                and session.created_by_user_id == owner_user_id
                and session.workspace_id == workspace_id
            )

    async def upsert_session(
        self, *, id: str, title: str, items_json: str, group_id: str | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        title_revision: int | None = None,
        title_source: TitleSource = "manual",
    ) -> AgentSession:
        with self._lock:
            if group_id is not None:
                require_memory_scope(
                    self._groups.get(group_id),
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    resource_id=group_id,
                    not_found=AgentSessionGroupNotFound,
                )
            existing = self._sessions.get(id)
            if existing is not None:
                require_memory_scope(
                    existing,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    resource_id=id,
                    not_found=AgentSessionNotFound,
                )
                if existing.lifecycle_status != "active":
                    raise AgentSessionNotFound(id)
                session = replace(
                    existing, **asdict(title_after_save(existing, title=title, revision=title_revision, source=title_source)),
                    items_json=items_json, group_id=group_id,
                    updated_at=updated_at,
                )
            else:
                session = AgentSession(
                    id=id, title=title, title_source=title_source, items_json=items_json, created_at=created_at,
                    group_id=group_id, updated_at=updated_at, created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                )
            self._sessions[id] = session
            return session

    async def apply_generated_title(
        self, session_id: str, *, title: str, revision: int, scope: ResourceScope
    ) -> bool:
        with self._lock:
            current = self._sessions.get(session_id)
            require_memory_scope(current, created_by_user_id=scope.created_by_user_id,
                                 workspace_id=scope.workspace_id, resource_id=session_id,
                                 not_found=AgentSessionNotFound)
            if current.lifecycle_status != "active":
                raise AgentSessionNotFound(session_id)
            if current.title_revision != revision or current.title_source != "initial":
                return False
            self._sessions[session_id] = replace(current, title=title, title_revision=revision + 1, title_source="generated")
            return True

    async def list_sessions(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSession]:
        with self._lock:
            items = _scoped(self._sessions.values(), created_by_user_id, workspace_id)
        items.sort(key=lambda s: (s.updated_at, s.id), reverse=True)
        return items

    async def get_session(self, session_id: str) -> AgentSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise AgentSessionNotFound(session_id)
        return session

    async def delete_session(
        self, session_id: str, *, scope: ResourceScope
    ) -> None:
        with self._lock:
            require_memory_scope(
                self._sessions.get(session_id),
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                resource_id=session_id,
                not_found=AgentSessionNotFound,
            )
            self._sessions.pop(session_id, None)

    async def set_session_deletion_state(
        self,
        session_id: str,
        *,
        scope: ResourceScope,
        lifecycle_status: str,
        deletion_operation_id: str,
        deletion_stage: str,
        deletion_error: str | None,
    ) -> None:
        with self._lock:
            session = require_memory_scope(
                self._sessions.get(session_id),
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                resource_id=session_id,
                not_found=AgentSessionNotFound,
            )
            self._sessions[session_id] = replace(
                session,
                lifecycle_status=lifecycle_status,
                deletion_operation_id=deletion_operation_id,
                deletion_stage=deletion_stage,
                deletion_error=deletion_error,
            )

    async def count_session_residuals(
        self, session_id: str, *, scope: ResourceScope
    ) -> int:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return 0
            try:
                require_memory_scope(
                    session,
                    created_by_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                    resource_id=session_id,
                    not_found=AgentSessionNotFound,
                )
            except AgentSessionNotFound:
                return 0
            return 1

    async def upsert_group(
        self, *, id: str, title: str, created_at: float, updated_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> AgentSessionGroup:
        with self._lock:
            existing = self._groups.get(id)
            if existing is not None:
                require_memory_scope(
                    existing,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    resource_id=id,
                    not_found=AgentSessionGroupNotFound,
                )
                group = replace(existing, title=title, updated_at=updated_at)
            else:
                group = AgentSessionGroup(
                    id=id, title=title, created_at=created_at,
                    updated_at=updated_at, created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                )
            self._groups[id] = group
            return group

    async def claim_group(
        self, *, id: str, title: str, created_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> AgentSessionGroup:
        with self._lock:
            existing = self._groups.get(id)
            if existing is not None:
                return existing
            group = AgentSessionGroup(
                id=id, title=title, created_at=created_at,
                updated_at=created_at, created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
            self._groups[id] = group
            return group

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSessionGroup]:
        with self._lock:
            items = _scoped(
                self._groups.values(), created_by_user_id, workspace_id
            )
        items.sort(key=lambda group: (group.created_at, group.id), reverse=True)
        return items

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        with self._lock:
            require_memory_scope(
                self._groups.get(group_id),
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                resource_id=group_id,
                not_found=AgentSessionGroupNotFound,
            )
            self._groups.pop(group_id, None)
            for session_id, session in list(self._sessions.items()):
                if session.group_id == group_id:
                    self._sessions[session_id] = replace(
                        session, group_id=None
                    )

    async def aclose(self) -> None:
        return None


def _scoped(
    values,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
):
    items = list(values)
    if created_by_user_id is not None:
        items = [i for i in items if i.created_by_user_id == created_by_user_id]
    if workspace_id is not None:
        items = [i for i in items if i.workspace_id == workspace_id]
    return items
