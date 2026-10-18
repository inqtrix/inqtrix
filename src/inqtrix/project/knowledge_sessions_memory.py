"""In-memory knowledge-session store (tier without Postgres + offline test).

Mirrors :class:`~inqtrix.project.knowledge_sessions_postgres.PostgresKnowledgeSessionStore`:
``list_sessions`` returns metadata only (empty ``items_json``); ``get_session``
returns the full row. Process-local, not durable.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace

from inqtrix.project.knowledge_sessions_ports import (
    KnowledgeSession,
    KnowledgeSessionGroup,
    KnowledgeSessionGroupNotFound,
    KnowledgeSessionNotFound,
)
from inqtrix.project.scoped_upsert import ResourceScope, require_memory_scope


class MemoryKnowledgeSessionStore:
    """Process-local
    :class:`~inqtrix.project.knowledge_sessions_ports.KnowledgeSessionStore`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, KnowledgeSession] = {}
        self._groups: dict[str, KnowledgeSessionGroup] = {}

    async def claim_session(
        self, *, id: str, title: str, created_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> KnowledgeSession:
        with self._lock:
            existing = self._sessions.get(id)
            if existing is not None:
                if existing.lifecycle_status != "active":
                    raise KnowledgeSessionNotFound(id)
                return existing
            session = KnowledgeSession(
                id=id,
                title=title,
                items_json="[]",
                created_at=created_at,
                updated_at=created_at,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
            self._sessions[id] = session
            return session

    async def upsert_session(
        self, *, id: str, title: str, items_json: str, group_id: str | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> KnowledgeSession:
        with self._lock:
            if group_id is not None:
                require_memory_scope(
                    self._groups.get(group_id),
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    resource_id=group_id,
                    not_found=KnowledgeSessionGroupNotFound,
                )
            existing = self._sessions.get(id)
            if existing is not None:
                require_memory_scope(
                    existing,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    resource_id=id,
                    not_found=KnowledgeSessionNotFound,
                )
                if existing.lifecycle_status != "active":
                    raise KnowledgeSessionNotFound(id)
                session = replace(
                    existing, title=title, items_json=items_json, group_id=group_id,
                    updated_at=updated_at,
                )
            else:
                session = KnowledgeSession(
                    id=id, title=title, items_json=items_json, created_at=created_at,
                    group_id=group_id, updated_at=updated_at,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                )
            self._sessions[id] = session
            return session

    async def list_sessions(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[KnowledgeSession]:
        items = _scoped(self._sessions.values(), created_by_user_id, workspace_id)
        items.sort(key=lambda s: (s.updated_at, s.id), reverse=True)
        # Metadata only — the items body loads via get_session.
        return [replace(session, items_json="[]") for session in items]

    async def get_session(self, session_id: str) -> KnowledgeSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KnowledgeSessionNotFound(session_id)
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
                not_found=KnowledgeSessionNotFound,
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
                not_found=KnowledgeSessionNotFound,
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
        session = self._sessions.get(session_id)
        if session is None:
            return 0
        try:
            require_memory_scope(
                session,
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                resource_id=session_id,
                not_found=KnowledgeSessionNotFound,
            )
        except KnowledgeSessionNotFound:
            return 0
        return 1

    async def upsert_group(
        self, *, id: str, title: str, created_at: float, updated_at: float,
        created_by_user_id: uuid.UUID | None, workspace_id: str | None,
    ) -> KnowledgeSessionGroup:
        existing = self._groups.get(id)
        if existing is not None:
            require_memory_scope(
                existing,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=id,
                not_found=KnowledgeSessionGroupNotFound,
            )
            group = replace(existing, title=title, updated_at=updated_at)
        else:
            group = KnowledgeSessionGroup(
                id=id, title=title, created_at=created_at, updated_at=updated_at,
                created_by_user_id=created_by_user_id, workspace_id=workspace_id,
            )
        self._groups[id] = group
        return group

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[KnowledgeSessionGroup]:
        items = _scoped(self._groups.values(), created_by_user_id, workspace_id)
        items.sort(key=lambda group: (group.created_at, group.id), reverse=True)
        return items

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        require_memory_scope(
            self._groups.get(group_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=group_id,
            not_found=KnowledgeSessionGroupNotFound,
        )
        self._groups.pop(group_id, None)
        for session_id, session in list(self._sessions.items()):
            if session.group_id == group_id:
                self._sessions[session_id] = replace(session, group_id=None)

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
