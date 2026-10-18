"""Postgres-backed workspace-agent memory candidate store."""

from __future__ import annotations

import uuid
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.agents.memory_ports import (
    AgentMemoryCandidate,
    AgentFeedbackRecord,
    AgentMemoryNotFound,
)
from inqtrix.project.base_session_store import BaseSessionStore
from inqtrix.storage.agent_memory_orm import (
    agent_feedback,
    agent_memory_candidates,
)

_DEFAULT_TENANT = "default"


class PostgresAgentMemoryCandidateStore(BaseSessionStore):
    """Durable candidate store keyed by ``(tenant_id, user_id, candidate_id)``."""

    async def create_candidate(
        self, candidate: AgentMemoryCandidate
    ) -> AgentMemoryCandidate:
        now = time.time()
        values = {
            "tenant_id": candidate.tenant_id or _DEFAULT_TENANT,
            "user_id": candidate.user_id,
            "candidate_id": candidate.candidate_id,
            "scope": candidate.scope,
            "category": candidate.category,
            "content": candidate.content,
            "reason": candidate.reason,
            "confidence": candidate.confidence,
            "source_run_id": candidate.source_run_id,
            "status": candidate.status,
            "memory_id": candidate.memory_id,
            "created_at": candidate.created_at or now,
            "updated_at": candidate.updated_at or now,
        }
        stmt = (
            pg_insert(agent_memory_candidates)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    agent_memory_candidates.c.tenant_id,
                    agent_memory_candidates.c.user_id,
                    agent_memory_candidates.c.candidate_id,
                ],
                set_={
                    "scope": values["scope"],
                    "category": values["category"],
                    "content": values["content"],
                    "reason": values["reason"],
                    "confidence": values["confidence"],
                    "source_run_id": values["source_run_id"],
                    "status": values["status"],
                    "memory_id": values["memory_id"],
                    "updated_at": values["updated_at"],
                },
            )
            .returning(agent_memory_candidates)
        )
        async with self._session() as session:
            row = (await session.execute(stmt)).mappings().one()
            await session.commit()
            return self._from_row(row)

    async def list_candidates(
        self, *, tenant_id: str, user_id: uuid.UUID, status: str | None = None
    ) -> list[AgentMemoryCandidate]:
        stmt = select(agent_memory_candidates).where(
            agent_memory_candidates.c.tenant_id == (tenant_id or _DEFAULT_TENANT),
            agent_memory_candidates.c.user_id == user_id,
        )
        if status is not None:
            stmt = stmt.where(agent_memory_candidates.c.status == status)
        stmt = stmt.order_by(
            agent_memory_candidates.c.created_at.desc(),
            agent_memory_candidates.c.candidate_id.desc(),
        )
        async with self._session() as session:
            rows = (await session.execute(stmt)).mappings().all()
            return [self._from_row(row) for row in rows]

    async def get_candidate(
        self, *, tenant_id: str, user_id: uuid.UUID, candidate_id: str
    ) -> AgentMemoryCandidate:
        stmt = select(agent_memory_candidates).where(
            agent_memory_candidates.c.tenant_id == (tenant_id or _DEFAULT_TENANT),
            agent_memory_candidates.c.user_id == user_id,
            agent_memory_candidates.c.candidate_id == candidate_id,
        )
        async with self._session() as session:
            row = (await session.execute(stmt)).mappings().first()
            if row is None:
                raise AgentMemoryNotFound(candidate_id)
            return self._from_row(row)

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
        row = await self.get_candidate(
            tenant_id=tenant_id, user_id=user_id, candidate_id=candidate_id
        )
        values = {
            "status": status,
            "content": row.content if content is None else content,
            "memory_id": row.memory_id if memory_id is None else memory_id,
            "updated_at": time.time(),
        }
        stmt = (
            agent_memory_candidates.update()
            .where(
                agent_memory_candidates.c.tenant_id == (tenant_id or _DEFAULT_TENANT),
                agent_memory_candidates.c.user_id == user_id,
                agent_memory_candidates.c.candidate_id == candidate_id,
            )
            .values(**values)
            .returning(agent_memory_candidates)
        )
        async with self._session() as session:
            updated = (await session.execute(stmt)).mappings().first()
            if updated is None:
                raise AgentMemoryNotFound(candidate_id)
            await session.commit()
            return self._from_row(updated)

    @staticmethod
    def _from_row(row) -> AgentMemoryCandidate:
        return AgentMemoryCandidate(
            candidate_id=row["candidate_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            scope=row["scope"],
            category=row["category"],
            content=row["content"],
            reason=row["reason"],
            confidence=float(row["confidence"]),
            source_run_id=row["source_run_id"],
            status=row["status"],
            memory_id=row["memory_id"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


class PostgresAgentFeedbackStore(BaseSessionStore):
    """Durable feedback store keyed by ``(tenant_id, user_id, feedback_id)``."""

    async def create_feedback(
        self, feedback: AgentFeedbackRecord
    ) -> AgentFeedbackRecord:
        values = {
            "tenant_id": feedback.tenant_id or _DEFAULT_TENANT,
            "user_id": feedback.user_id,
            "feedback_id": feedback.feedback_id,
            "run_id": feedback.run_id,
            "memory_id": feedback.memory_id,
            "feedback": feedback.feedback,
            "reason": feedback.reason,
            "created_at": feedback.created_at or time.time(),
        }
        stmt = (
            pg_insert(agent_feedback)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    agent_feedback.c.tenant_id,
                    agent_feedback.c.user_id,
                    agent_feedback.c.feedback_id,
                ],
                set_={
                    "run_id": values["run_id"],
                    "memory_id": values["memory_id"],
                    "feedback": values["feedback"],
                    "reason": values["reason"],
                    "created_at": values["created_at"],
                },
            )
            .returning(agent_feedback)
        )
        async with self._session() as session:
            row = (await session.execute(stmt)).mappings().one()
            await session.commit()
            return self._feedback_from_row(row)

    async def list_feedback(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentFeedbackRecord]:
        stmt = select(agent_feedback).where(
            agent_feedback.c.tenant_id == (tenant_id or _DEFAULT_TENANT),
            agent_feedback.c.user_id == user_id,
        )
        if run_id is not None:
            stmt = stmt.where(agent_feedback.c.run_id == run_id)
        stmt = stmt.order_by(
            agent_feedback.c.created_at.desc(),
            agent_feedback.c.feedback_id.desc(),
        ).limit(limit)
        async with self._session() as session:
            rows = (await session.execute(stmt)).mappings().all()
            return [self._feedback_from_row(row) for row in rows]

    @staticmethod
    def _feedback_from_row(row) -> AgentFeedbackRecord:
        return AgentFeedbackRecord(
            feedback_id=row["feedback_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            run_id=row["run_id"],
            memory_id=row["memory_id"],
            feedback=row["feedback"],
            reason=row["reason"],
            created_at=float(row["created_at"]),
        )
