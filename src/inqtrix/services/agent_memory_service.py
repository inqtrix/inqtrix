"""Governed workspace-agent long-term memory service.

This is the enterprise boundary around provider-backed memory. Routers and
the agent pass a verified :class:`~inqtrix.auth.principal.Principal`; client
payloads never carry owner identifiers. The service derives a provider
namespace from ``(tenant_id, user_id)`` and rejects anonymous/static principals
for long-term memory.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
import re
from typing import TYPE_CHECKING, Any

from inqtrix.agents.memory_ports import (
    MEMORY_CANDIDATE_STATUSES,
    MEMORY_CATEGORIES,
    MEMORY_FEEDBACK_VALUES,
    MEMORY_SCOPES,
    AgentMemoryCandidate,
    AgentMemoryCandidateStore,
    AgentFeedbackRecord,
    AgentFeedbackStore,
    AgentMemoryProvider,
    AgentMemoryRecord,
    AgentMemoryUnavailable,
    AgentMemoryValidationError,
)
from inqtrix.auth.principal import Principal

if TYPE_CHECKING:
    from inqtrix.project.account_preferences_ports import (
        AccountPreferencesStore,
    )

log = logging.getLogger("inqtrix")

_LONG_TERM_KINDS = frozenset({"oidc_session", "pat"})
_INELIGIBLE_SUBS = frozenset({"__anonymous__", "__static__", ""})
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(api[_ -]?key|password|secret)\s*[:=]", re.I),
)


class AgentMemoryService:
    """Application service for accepted memories and review candidates."""

    def __init__(
        self,
        *,
        candidate_store: AgentMemoryCandidateStore,
        feedback_store: AgentFeedbackStore,
        provider: AgentMemoryProvider | None,
        provider_name: str,
        mode: str,
        durable: bool = False,
        account_preferences: "AccountPreferencesStore | None" = None,
    ) -> None:
        self._candidate_store = candidate_store
        self._feedback_store = feedback_store
        self._provider = provider
        self._provider_name = provider_name
        self._mode = mode
        self._durable = durable
        self._account_preferences = account_preferences

    @property
    def durable(self) -> bool:
        """Whether candidate review state survives process restarts."""
        return self._durable

    @property
    def mode(self) -> str:
        """Configured learning mode."""
        return self._mode

    @property
    def provider_name(self) -> str:
        """Configured provider name."""
        return self._provider_name

    def status(self, principal: Principal | None = None) -> dict[str, Any]:
        """Reader-facing availability summary for settings and activity."""
        eligible = True
        if principal is not None:
            eligible = self._is_principal_eligible(principal)
        effective_mode = (
            "candidate_only" if self._mode == "auto_safe" else self._mode
        )
        degraded_reason = (
            "auto_safe_not_implemented" if self._mode == "auto_safe" else ""
        )
        enabled = (
            effective_mode != "off"
            and self._provider_name != "none"
            and self._provider is not None
            and eligible
        )
        return {
            "provider": self._provider_name,
            "mode": self._mode,
            "effective_mode": effective_mode,
            "degraded_reason": degraded_reason,
            "available": enabled,
            "durable": self._durable,
            "principal_eligible": eligible,
        }

    async def opt_in_enabled(self, principal: Principal | None) -> bool:
        """Whether *principal* opted long-term memory IN (privacy default OFF).

        Long-term memory is off unless the user turns it on in Settings. An
        anonymous/ineligible principal, an unwired preferences reader, an
        absent preferences row, or a failed read all resolve to ``False`` —
        the agent then reads and writes no long-term memory. A read failure
        degrades to OFF VISIBLY (``log.warning``), never silently enabling
        memory (the fallback is visible and fails safe).

        This gates BOTH the read path (:func:`_load_memory_briefing`) and the
        write path (:func:`_stage_memory_candidates`) so an opt-out neither
        recalls nor stages memory.
        """
        # When memory infra is globally off there is nothing to opt into —
        # short-circuit BEFORE the preferences read so a memory-less
        # deployment never pays a per-segment DB round-trip.
        if (
            self._provider is None
            or self._provider_name == "none"
            or self._mode == "off"
        ):
            return False
        if principal is None or not self._is_principal_eligible(principal):
            return False
        if self._account_preferences is None:
            return False
        try:
            prefs = await self._account_preferences.get_preferences(
                user_id=principal.user_id
            )
        except Exception:  # noqa: BLE001 — memory is non-essential; degrade OFF
            log.warning(
                "Agent memory opt-in could not be read; memory remains disabled for "
                "this run (privacy default)."
            )
            return False
        return bool(prefs is not None and prefs.enable_agent_memory)

    async def list_memories(
        self,
        *,
        principal: Principal,
        scope: str | None = None,
        query: str = "",
        limit: int = 100,
    ) -> list[AgentMemoryRecord]:
        normalized_scope = self._validate_scope(scope, optional=True)
        if query.strip():
            rows = await self.recall(
                principal=principal,
                query=query,
                limit=_clamp_limit(limit),
            )
            if normalized_scope:
                rows = [row for row in rows if row.scope == normalized_scope]
            return rows
        namespace = self._namespace_for(principal)
        return await self._require_provider().list_memories(
            namespace=namespace,
            scope=normalized_scope or None,
            limit=_clamp_limit(limit),
        )

    async def recall(
        self,
        *,
        principal: Principal,
        query: str,
        limit: int = 5,
    ) -> list[AgentMemoryRecord]:
        namespace = self._namespace_for(principal)
        try:
            return await self._require_provider().recall(
                namespace=namespace,
                query=query,
                limit=_clamp_limit(limit, default=5, maximum=20),
            )
        except AgentMemoryUnavailable:
            log.warning("Agent memory recall unavailable for this run.")
            raise

    async def recall_briefing(
        self,
        *,
        principal: Principal,
        query: str,
        limit: int = 5,
    ) -> tuple[str, str]:
        """Return non-evidentiary memory context for the agent prompt.

        Returns:
            ``(briefing, status)`` where status is ``used`` or
            ``unavailable``. The briefing intentionally has no citations.
        """
        try:
            memories = await self.recall(
                principal=principal, query=query, limit=limit
            )
        except AgentMemoryUnavailable:
            return "", "unavailable"
        if not memories:
            return "", "empty"
        lines = [
            f"- ({memory.scope}/{memory.category}) {memory.content}"
            for memory in memories
            if memory.content.strip()
        ]
        return "\n".join(lines), "used" if lines else "empty"

    async def create_candidate(
        self,
        *,
        principal: Principal,
        scope: str,
        category: str,
        content: str,
        reason: str,
        confidence: float,
        source_run_id: str,
    ) -> AgentMemoryCandidate:
        tenant_id, user_id = self._principal_key(principal)
        scope = self._validate_scope(scope)
        category = self._validate_category(category)
        content = self._validate_content(content)
        reason = _trim(reason, 1200)
        return await self._candidate_store.create_candidate(
            AgentMemoryCandidate(
                candidate_id=f"memcand_{uuid.uuid4().hex}",
                tenant_id=tenant_id,
                user_id=user_id,
                scope=scope,
                category=category,
                content=content,
                reason=reason,
                confidence=_clamp_confidence(confidence),
                source_run_id=_trim(source_run_id, 160),
            )
        )

    async def stage_candidates(
        self,
        *,
        principal: Principal,
        candidates: list[dict[str, Any]],
        source_run_id: str,
    ) -> list[AgentMemoryCandidate]:
        if self._mode == "off":
            return []
        # Idempotent per source run: a terminal segment can be redelivered
        # after a crash (both engines stage from the terminal write), which
        # would otherwise DUPLICATE this run's candidates. If any already
        # exist for this run, return them instead of staging again. Shared
        # sink for the mission engine and the kernel — one root, no per-
        # engine guard. (The reflection LLM call upstream still re-runs on
        # redelivery; that cost residual is bounded to the rare crash path.)
        if source_run_id:
            tenant_id, user_id = self._principal_key(principal)
            existing = await self._candidate_store.list_candidates(
                tenant_id=tenant_id, user_id=user_id, status=None
            )
            prior = [
                candidate
                for candidate in existing
                if candidate.source_run_id == source_run_id
            ]
            if prior:
                log.info(
                    "Memory candidates for run %s already staged (%d); repeated staging"
                    " skipped (idempotent on redelivery).",
                    source_run_id,
                    len(prior),
                )
                return prior
        staged: list[AgentMemoryCandidate] = []
        for item in candidates[:5]:
            try:
                staged.append(
                    await self.create_candidate(
                        principal=principal,
                        scope=str(item.get("scope") or "user"),
                        category=str(item.get("category") or "project_fact"),
                        content=str(item.get("content") or ""),
                        reason=str(item.get("reason") or ""),
                        confidence=float(item.get("confidence") or 0.0),
                        source_run_id=source_run_id,
                    )
                )
            except AgentMemoryValidationError as exc:
                log.warning(
                    "Agent memory candidate skipped (error_type=%s)",
                    type(exc).__name__,
                )
        return staged

    async def list_candidates(
        self,
        *,
        principal: Principal,
        status: str | None = None,
    ) -> list[AgentMemoryCandidate]:
        tenant_id, user_id = self._principal_key(principal)
        if status is not None and status not in MEMORY_CANDIDATE_STATUSES:
            raise AgentMemoryValidationError(f"unknown status: {status}")
        return await self._candidate_store.list_candidates(
            tenant_id=tenant_id, user_id=user_id, status=status
        )

    async def accept_candidate(
        self,
        *,
        principal: Principal,
        candidate_id: str,
        content: str | None = None,
    ) -> AgentMemoryCandidate:
        tenant_id, user_id = self._principal_key(principal)
        candidate = await self._candidate_store.get_candidate(
            tenant_id=tenant_id,
            user_id=user_id,
            candidate_id=candidate_id,
        )
        if candidate.status == "accepted":
            return candidate
        if candidate.status != "pending":
            raise AgentMemoryValidationError("candidate is not pending")
        retained = await self._require_provider().retain(
            namespace=self._namespace_for(principal),
            content=self._validate_content(content or candidate.content),
            scope=candidate.scope,
            category=candidate.category,
            confidence=candidate.confidence,
            source_run_id=candidate.source_run_id,
        )
        return await self._candidate_store.update_candidate(
            tenant_id=tenant_id,
            user_id=user_id,
            candidate_id=candidate_id,
            status="accepted",
            content=content,
            memory_id=retained.memory_id,
        )

    async def reject_candidate(
        self, *, principal: Principal, candidate_id: str
    ) -> AgentMemoryCandidate:
        tenant_id, user_id = self._principal_key(principal)
        candidate = await self._candidate_store.get_candidate(
            tenant_id=tenant_id, user_id=user_id, candidate_id=candidate_id
        )
        if candidate.status == "rejected":
            return candidate
        if candidate.status != "pending":
            raise AgentMemoryValidationError("candidate is not pending")
        return await self._candidate_store.update_candidate(
            tenant_id=tenant_id,
            user_id=user_id,
            candidate_id=candidate_id,
            status="rejected",
        )

    async def update_memory(
        self,
        *,
        principal: Principal,
        memory_id: str,
        content: str,
        scope: str,
        category: str,
    ) -> AgentMemoryRecord:
        return await self._require_provider().update(
            namespace=self._namespace_for(principal),
            memory_id=memory_id,
            content=self._validate_content(content),
            scope=self._validate_scope(scope),
            category=self._validate_category(category),
        )

    async def delete_memory(
        self, *, principal: Principal, memory_id: str
    ) -> None:
        await self._require_provider().delete(
            namespace=self._namespace_for(principal), memory_id=memory_id
        )

    async def clear_memories(
        self, *, principal: Principal, scope: str | None = None
    ) -> int:
        self._validate_scope(scope, optional=True)
        return await self._require_provider().clear(
            namespace=self._namespace_for(principal), scope=scope
        )

    async def feedback(
        self,
        *,
        principal: Principal,
        run_id: str,
        memory_id: str,
        feedback: str,
        reason: str,
    ) -> AgentFeedbackRecord:
        tenant_id, user_id = self._principal_key(principal)
        normalized = self._validate_feedback(feedback)
        trimmed_memory_id = _trim(memory_id, 240)
        trimmed_reason = _trim(reason, 1200)
        if trimmed_memory_id:
            await self._require_provider().feedback(
                namespace=self._namespace_for(principal),
                memory_id=trimmed_memory_id,
                feedback=normalized,
                reason=trimmed_reason,
            )
        return await self._feedback_store.create_feedback(
            AgentFeedbackRecord(
                feedback_id=f"agfb_{uuid.uuid4().hex}",
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=_trim(run_id, 160),
                memory_id=trimmed_memory_id,
                feedback=normalized,
                reason=trimmed_reason,
            )
        )

    async def list_feedback(
        self,
        *,
        principal: Principal,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentFeedbackRecord]:
        tenant_id, user_id = self._principal_key(principal)
        return await self._feedback_store.list_feedback(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=_trim(run_id or "", 160) or None,
            limit=_clamp_limit(limit),
        )

    def _principal_key(self, principal: Principal) -> tuple[str, uuid.UUID]:
        if not self._is_principal_eligible(principal):
            raise AgentMemoryUnavailable("Memory requires an authenticated user")
        return principal.tenant_id or "default", principal.user_id

    def _namespace_for(self, principal: Principal) -> str:
        tenant_id, user_id = self._principal_key(principal)
        digest = hashlib.sha256(f"{tenant_id}:{user_id}".encode("utf-8")).hexdigest()
        return f"inqtrix_{tenant_id}_{digest[:48]}"

    def _require_provider(self) -> AgentMemoryProvider:
        if self._mode == "off" or self._provider_name == "none":
            raise AgentMemoryUnavailable("Memory is disabled")
        if self._provider is None:
            raise AgentMemoryUnavailable("Memory provider unavailable")
        return self._provider

    @staticmethod
    def _is_principal_eligible(principal: Principal) -> bool:
        return (
            principal.kind in _LONG_TERM_KINDS
            and principal.user_id is not None
        )

    @staticmethod
    def _validate_scope(scope: str | None, *, optional: bool = False) -> str:
        if scope is None and optional:
            return ""
        normalized = str(scope or "").strip().lower()
        if normalized not in MEMORY_SCOPES:
            raise AgentMemoryValidationError(f"unknown memory scope: {scope}")
        return normalized

    @staticmethod
    def _validate_category(category: str) -> str:
        normalized = str(category or "").strip().lower()
        if normalized not in MEMORY_CATEGORIES:
            raise AgentMemoryValidationError(
                f"unknown memory category: {category}"
            )
        return normalized

    @staticmethod
    def _validate_feedback(feedback: str) -> str:
        normalized = str(feedback or "").strip().lower()
        if normalized not in MEMORY_FEEDBACK_VALUES:
            raise AgentMemoryValidationError(
                "feedback must be positive, negative, or neutral"
            )
        return normalized

    @staticmethod
    def _validate_content(content: str) -> str:
        normalized = _trim(content, 4000)
        if not normalized:
            raise AgentMemoryValidationError("memory content is required")
        if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
            raise AgentMemoryValidationError("memory content appears sensitive")
        return normalized


def _trim(value: str, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _clamp_limit(
    value: int | None,
    *,
    default: int = 100,
    maximum: int = 200,
) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))
