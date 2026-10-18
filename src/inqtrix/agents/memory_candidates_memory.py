"""In-memory store for workspace-agent memory candidates."""

from __future__ import annotations

import uuid
import threading
import time
from dataclasses import replace

from inqtrix.agents.memory_ports import (
    AgentMemoryCandidate,
    AgentFeedbackRecord,
    AgentMemoryNotFound,
)


class MemoryAgentMemoryCandidateStore:
    """Process-local candidate store for offline and test deployments."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, uuid.UUID, str], AgentMemoryCandidate] = {}

    async def create_candidate(
        self, candidate: AgentMemoryCandidate
    ) -> AgentMemoryCandidate:
        with self._lock:
            now = time.time()
            stored = replace(
                candidate,
                created_at=candidate.created_at or now,
                updated_at=candidate.updated_at or now,
            )
            self._rows[
                (stored.tenant_id, stored.user_id, stored.candidate_id)
            ] = stored
            return stored

    async def list_candidates(
        self, *, tenant_id: str, user_id: uuid.UUID, status: str | None = None
    ) -> list[AgentMemoryCandidate]:
        with self._lock:
            rows = [
                row
                for (row_tenant, row_user_id, _), row in self._rows.items()
                if row_tenant == tenant_id
                and row_user_id == user_id
                and (status is None or row.status == status)
            ]
            return sorted(
                rows, key=lambda row: (row.created_at, row.candidate_id), reverse=True
            )

    async def get_candidate(
        self, *, tenant_id: str, user_id: uuid.UUID, candidate_id: str
    ) -> AgentMemoryCandidate:
        with self._lock:
            row = self._rows.get((tenant_id, user_id, candidate_id))
            if row is None:
                raise AgentMemoryNotFound(candidate_id)
            return row

    async def update_candidate(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        candidate_id: str,
        status: str,
        content: str | None = None,
        memory_id: str | None = None,
    ) -> AgentMemoryCandidate:
        with self._lock:
            row = self._rows.get((tenant_id, user_id, candidate_id))
            if row is None:
                raise AgentMemoryNotFound(candidate_id)
            stored = replace(
                row,
                status=status,
                content=row.content if content is None else content,
                memory_id=row.memory_id if memory_id is None else memory_id,
                updated_at=time.time(),
            )
            self._rows[(tenant_id, user_id, candidate_id)] = stored
            return stored


class MemoryAgentFeedbackStore:
    """Process-local feedback store for offline and test deployments."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, uuid.UUID, str], AgentFeedbackRecord] = {}

    async def create_feedback(
        self, feedback: AgentFeedbackRecord
    ) -> AgentFeedbackRecord:
        with self._lock:
            stored = replace(
                feedback,
                created_at=feedback.created_at or time.time(),
            )
            self._rows[
                (stored.tenant_id, stored.user_id, stored.feedback_id)
            ] = stored
            return stored

    async def list_feedback(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentFeedbackRecord]:
        with self._lock:
            rows = [
                row
                for (row_tenant, row_user_id, _), row in self._rows.items()
                if row_tenant == tenant_id
                and row_user_id == user_id
                and (run_id is None or row.run_id == run_id)
            ]
            return sorted(
                rows,
                key=lambda row: (row.created_at, row.feedback_id),
                reverse=True,
            )[:limit]
