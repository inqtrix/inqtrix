"""Workspace-agent long-term memory contracts.

Run artifacts and session memos remain the canonical short-lived memory.
This module defines the optional long-term memory seam: provider-backed
accepted memories and Inqtrix-owned approval candidates. Client-supplied
owner fields are deliberately absent; callers pass the verified principal
through the service, and the service derives the provider namespace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

MEMORY_SCOPES = ("user", "workspace", "project", "agent")
MEMORY_CATEGORIES = ("preference", "project_fact", "strategy", "correction")
MEMORY_CANDIDATE_STATUSES = ("pending", "accepted", "rejected")
MEMORY_FEEDBACK_VALUES = ("positive", "negative", "neutral")


class AgentMemoryUnavailable(RuntimeError):
    """Raised when the long-term memory surface is unavailable."""


class AgentMemoryNotFound(KeyError):
    """Raised when a memory or candidate is absent for the caller scope."""


class AgentMemoryValidationError(ValueError):
    """Raised for out-of-domain memory requests."""


@dataclass(frozen=True)
class AgentMemoryRecord:
    """One accepted long-term memory as shown through Inqtrix.

    Attributes:
        memory_id: Provider memory id.
        scope: Inqtrix memory scope, never an owner identifier.
        category: Memory class used for UI filtering and prompt framing.
        content: Human-editable memory text.
        confidence: Best-effort confidence in the memory, 0.0 to 1.0.
        source_run_id: Run that produced or last confirmed the memory.
        metadata: Provider metadata preserved for audit-friendly display.
        created_at: Provider creation timestamp, if available.
        updated_at: Provider update timestamp, if available.
    """

    memory_id: str
    scope: str
    category: str
    content: str
    confidence: float = 0.0
    source_run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AgentMemoryCandidate:
    """One proposed memory awaiting user decision."""

    candidate_id: str
    tenant_id: str
    user_id: uuid.UUID
    scope: str
    category: str
    content: str
    reason: str
    confidence: float
    source_run_id: str
    status: str = "pending"
    memory_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class AgentFeedbackRecord:
    """One personal feedback entry for a workspace-agent run."""

    feedback_id: str
    tenant_id: str
    user_id: uuid.UUID
    run_id: str
    feedback: str
    reason: str = ""
    memory_id: str = ""
    created_at: float = 0.0


@runtime_checkable
class AgentMemoryProvider(Protocol):
    """Provider seam for accepted long-term memories."""

    async def list_memories(
        self, *, namespace: str, scope: str | None, limit: int
    ) -> list[AgentMemoryRecord]:
        """List accepted memories for a provider namespace."""

    async def recall(
        self, *, namespace: str, query: str, limit: int
    ) -> list[AgentMemoryRecord]:
        """Retrieve relevant memories for a prompt."""

    async def retain(
        self,
        *,
        namespace: str,
        content: str,
        scope: str,
        category: str,
        confidence: float,
        source_run_id: str,
    ) -> AgentMemoryRecord:
        """Store one accepted memory."""

    async def update(
        self,
        *,
        namespace: str,
        memory_id: str,
        content: str,
        scope: str,
        category: str,
    ) -> AgentMemoryRecord:
        """Edit one memory after ownership validation."""

    async def delete(self, *, namespace: str, memory_id: str) -> None:
        """Delete one memory after ownership validation."""

    async def clear(self, *, namespace: str, scope: str | None) -> int:
        """Delete all memories in the namespace, optionally one scope."""

    async def feedback(
        self,
        *,
        namespace: str,
        memory_id: str,
        feedback: str,
        reason: str,
    ) -> None:
        """Submit provider feedback after ownership validation."""


@runtime_checkable
class AgentMemoryCandidateStore(Protocol):
    """Persistence seam for user-reviewable memory candidates."""

    async def create_candidate(
        self, candidate: AgentMemoryCandidate
    ) -> AgentMemoryCandidate:
        """Persist a proposed memory candidate."""

    async def list_candidates(
        self, *, tenant_id: str, user_id: uuid.UUID, status: str | None = None
    ) -> list[AgentMemoryCandidate]:
        """List candidates for the caller subject."""

    async def get_candidate(
        self, *, tenant_id: str, user_id: uuid.UUID, candidate_id: str
    ) -> AgentMemoryCandidate:
        """Get one candidate for the caller subject."""

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
        """Change candidate content and/or lifecycle state."""


@runtime_checkable
class AgentFeedbackStore(Protocol):
    """Persistence seam for personal agent-feedback history."""

    async def create_feedback(
        self, feedback: AgentFeedbackRecord
    ) -> AgentFeedbackRecord:
        """Persist one feedback entry for the caller subject."""

    async def list_feedback(
        self,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentFeedbackRecord]:
        """List feedback entries for the caller subject."""
