"""Skill records: server-enforced policy objects of the skill library.

Skills are not a fourth prompt-template category:
templates are client-interpreted text, while a skill's fields are
ENFORCED server-side — ``clarification_points`` drive the intake gate,
``requires_plan`` the plan gate, ``allowed_tools`` the tool dispatch,
``invocation`` the model-disclosure block. The persistence machinery
mirrors prompt templates one-for-one (ownership, optimistic
concurrency, memory + Postgres backends); only the table is new.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.shares import ShareFacts

if TYPE_CHECKING:
    from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator

SKILL_DELIVERABLES = ("", "chat", "canvas", "email", "talking_points")
"""Output form the skill pins ('' = the agent decides).

Two dimensions in one field: ``chat`` and ``canvas`` name the SURFACE the
result lands on and are enforced by routing; ``email`` and
``talking_points`` name the FORM the text takes on the canvas and are
enforced through the prompt
(:data:`~inqtrix.agents.skills_runtime.SKILL_DELIVERABLE_FORMAT_LINES`).
``email`` shapes the text only — there is no sending integration."""

SKILL_REQUIRES_PLAN = ("always", "auto", "never")
"""Plan-gate policy: ``always`` forces the gate even in
Auto, ``auto`` (default) follows the permission mode, ``never`` skips it
(a mission still shows its plan informatively). The STRICTEST value
across activated skills wins; the patch gate is never affected."""

SKILL_INVOCATIONS = ("user_only", "model_allowed")

SKILL_ALLOWED_TOOLS = (
    "search_context",
    "read_context",
    "search_project_knowledge",
    "read_project_document",
    "web_instant",
    "run_web_research",
    "run_deep_mission",
    "write_canvas",
    "read_editor_document",
    "search_editor_document",
    "propose_editor_patch",
)
"""The closed ``allowed_tools`` vocabulary (kernel tool names; the
library editor mirrors this list). A name outside it would be a silent
misconfiguration: the runtime union would restrict the run without any
tool ever matching the typo."""
"""Who may activate the skill: only the user (``/``-mention) or also the
model (disclosure block + ``load_skill``). Shared-in skills are ALWAYS
``user_only`` for the recipient — structural injection defense."""

MAX_CLARIFICATION_POINTS = 5
"""Hard cap on declared clarification points per skill."""

MAX_POINT_OPTIONS = 4
"""Options per clarification point, mirroring
:data:`~inqtrix.agents.clarification.MAX_OPTIONS_PER_QUESTION` — the cap
a clarification gate round applies to one question's options."""


class SkillNotFound(KeyError):
    """Raised when a skill id is unknown (or hidden — same signal)."""


class SkillConflict(RuntimeError):
    """Raised when a mandatory integer revision is stale."""

    def __init__(self, skill_id: str, current_revision: int) -> None:
        self.skill_id = skill_id
        self.current_revision = current_revision
        super().__init__(
            f"skill {skill_id} is at revision {current_revision}"
        )


@dataclass(frozen=True)
class SkillRecord:
    """One stored skill.

    Attributes:
        id: Server-assigned stable identifier (``sk_...``).
        tenant_id: Tenant scope (v1 runs one tenant per deployment).
        owner_user_id: Canonical creating user UUID; ``None`` means an
            ownerless record in an unscoped deployment.
        label: The ``/``-mention token (``[a-z0-9-]``, unique per user
            surface by convention, not enforced).
        title: Display title in the skill library.
        description: One-liner for pickers and the disclosure block.
        when_to_use: Model-facing activation guidance (budgeted into
            the disclosure block).
        instructions_markdown: The skill body, loaded only on
            activation (progressive disclosure). May contain
            ``{{name}}`` placeholders that the clarification points
            fill.
        clarification_points: Declared inputs (max
            :data:`MAX_CLARIFICATION_POINTS`), each
            ``{id, name, question, options, required,
            default_assumption}`` with sanitized deterministic ids —
            the runtime point check asks ONLY what context cannot
            answer, and points that stay unanswered but required are
            raised as questions in the run's clarification gate round.
        deliverable: One of :data:`SKILL_DELIVERABLES`.
        allowed_tools: Tool allowlist enforced at dispatch when
            non-empty (union over activated skills); empty = no
            restriction.
        requires_plan: One of :data:`SKILL_REQUIRES_PLAN`.
        invocation: One of :data:`SKILL_INVOCATIONS`.
        argument_hint: Composer hint after the ``/``-token (free text).
        model_tier: Optional tier pin: '' or high|mid|fast.
        effort: Optional reasoning-effort pin: '' or the effort tokens
            the routing layer accepts.
        include_in_autocomplete: Whether the ``/``-menu offers it.
        created_at: Unix timestamp of creation.
        revision: Monotonic compare-and-swap version, starting at one.
        updated_at: Unix timestamp of the last write, used for display only.
    """

    id: str
    tenant_id: str
    owner_user_id: uuid.UUID | None
    label: str
    title: str
    description: str = ""
    when_to_use: str = ""
    instructions_markdown: str = ""
    clarification_points: tuple[dict[str, Any], ...] = ()
    deliverable: str = ""
    allowed_tools: tuple[str, ...] = ()
    requires_plan: str = "auto"
    invocation: str = "user_only"
    argument_hint: str = ""
    model_tier: str = ""
    effort: str = ""
    include_in_autocomplete: bool = True
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


@runtime_checkable
class SkillRepository(Protocol):
    """Persistence port for skills (memory + Postgres)."""

    async def create(self, record: SkillRecord) -> SkillRecord: ...

    async def get(self, skill_id: str, *, tenant_id: str) -> SkillRecord: ...

    async def list_for_tenant(
        self, *, tenant_id: str
    ) -> list[SkillRecord]: ...

    async def share_facts(
        self, skill_ids: Sequence[str], *, tenant_id: str
    ) -> dict[str, ShareFacts]:
        """Owner and title of each listed skill of the tenant, in one read
        and regardless of visibility (share layer); others are absent."""
        ...

    async def update(
        self,
        record: SkillRecord,
        *,
        expected_revision: int,
        actor_user_id: uuid.UUID | None,
    ) -> SkillRecord: ...

    async def delete(
        self,
        skill_id: str,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None: ...


def new_skill_id() -> str:
    """Mint one ``sk_``-prefixed identifier."""
    return f"sk_{uuid.uuid4().hex[:20]}"


class MemorySkillRepository:
    """Thread-safe in-process implementation (zero-infrastructure default)."""

    def __init__(self) -> None:
        self._records: dict[str, SkillRecord] = {}
        self._lock = threading.RLock()
        self._authority: MemoryAuthorityCoordinator | None = None

    @property
    def atomic_resource_effects(self) -> bool:
        """Whether writes include audit and invalidations under one lock."""
        return self._authority is not None

    def bind_authority_coordinator(
        self, coordinator: "MemoryAuthorityCoordinator"
    ) -> None:
        """Join the process-wide memory authority and register ownership."""
        self._authority = coordinator
        self._lock = coordinator.lock
        coordinator.register_resource("skill_template", self._resource_snapshot)

    def _resource_snapshot(self, tenant_id: str, skill_id: str):
        """Return existence and owner while the shared lock is held."""
        from inqtrix.auth.memory_authority import MemoryResourceSnapshot

        record = self._records.get(skill_id)
        return MemoryResourceSnapshot(
            exists=record is not None and record.tenant_id == tenant_id,
            owner_user_id=(
                record.owner_user_id
                if record is not None and record.tenant_id == tenant_id
                else None
            ),
        )

    @contextmanager
    def _mutation_guard(
        self,
        record: SkillRecord,
        *,
        actor_user_id: uuid.UUID | None,
        owner_only: bool = False,
    ) -> Iterator[None]:
        """Hold final live authority across one repository mutation."""
        if self._authority is None:
            yield
            return
        from inqtrix.execution_authority import AuthorizationRevoked

        try:
            with self._authority.resource_access_guard(
                tenant_id=record.tenant_id,
                owner_user_id=record.owner_user_id,
                actor_user_id=actor_user_id,
                resource_type="skill_template",
                resource_id=record.id,
                minimum=SharePermission.EDIT,
                owner_only=owner_only,
            ):
                yield
        except AuthorizationRevoked as exc:
            raise SkillNotFound(record.id) from exc

    async def create(self, record: SkillRecord) -> SkillRecord:
        with self._lock:
            guard = (
                self._authority.creation_guard(
                    tenant_id=record.tenant_id,
                    actor_user_id=record.owner_user_id,
                )
                if self._authority is not None
                else nullcontext()
            )
            with guard:
                self._records[record.id] = record
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=record.tenant_id,
                        actor_user_id=record.owner_user_id,
                        owner_user_id=record.owner_user_id,
                        action="skill_template.created",
                        resource_type="skill_template",
                        resource_id=record.id,
                        scope="skills",
                    )
                return record

    async def get(self, skill_id: str, *, tenant_id: str) -> SkillRecord:
        with self._lock:
            record = self._records.get(skill_id)
        if record is None or record.tenant_id != tenant_id:
            raise SkillNotFound(skill_id)
        return record

    async def list_for_tenant(self, *, tenant_id: str) -> list[SkillRecord]:
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
            ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def share_facts(
        self, skill_ids: Sequence[str], *, tenant_id: str
    ) -> dict[str, ShareFacts]:
        with self._lock:
            return {
                skill_id: ShareFacts(
                    owner_user_id=record.owner_user_id, title=record.title
                )
                for skill_id in skill_ids
                if (record := self._records.get(skill_id)) is not None
                and record.tenant_id == tenant_id
            }

    async def update(
        self,
        record: SkillRecord,
        *,
        expected_revision: int,
        actor_user_id: uuid.UUID | None,
    ) -> SkillRecord:
        with self._lock:
            current = self._records.get(record.id)
            if current is None or current.tenant_id != record.tenant_id:
                raise SkillNotFound(record.id)
            with self._mutation_guard(
                current, actor_user_id=actor_user_id
            ):
                if current.revision != expected_revision:
                    raise SkillConflict(record.id, current.revision)
                stored = replace(
                    record,
                    tenant_id=current.tenant_id,
                    owner_user_id=current.owner_user_id,
                    revision=current.revision + 1,
                    updated_at=time.time(),
                )
                self._records[record.id] = stored
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=stored.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=stored.owner_user_id,
                        action="skill_template.updated",
                        resource_type="skill_template",
                        resource_id=stored.id,
                        scope="skills",
                    )
                return stored

    async def delete(
        self,
        skill_id: str,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        with self._lock:
            record = self._records.get(skill_id)
            if record is None or record.tenant_id != tenant_id:
                raise SkillNotFound(skill_id)
            with self._mutation_guard(
                record, actor_user_id=actor_user_id, owner_only=True
            ):
                if record.owner_user_id != actor_user_id:
                    raise SkillNotFound(skill_id)
                if self._authority is not None:
                    self._authority.revoke_deleted_resource(
                        tenant_id=record.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=record.owner_user_id,
                        action="skill_template.deleted",
                        resource_type="skill_template",
                        resource_id=record.id,
                        scope="skills",
                    )
                del self._records[skill_id]
