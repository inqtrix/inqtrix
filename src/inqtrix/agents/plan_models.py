"""Pydantic schema of an executable agent plan.

THE one plan shape: the planner produces it as
structured output, the approval edit endpoint accepts it as the user's
revision, and :mod:`inqtrix.agents.plan_validation` checks both through the
same deterministic rules. Field semantics select the smallest sufficient
tool and a retrieval profile matched to the evidence gap.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from inqtrix.agents.control_ports import TASK_TOOL_KINDS
from inqtrix.core.results import WebRecency

WEB_RESEARCH_PROFILES = ("schnell", "compact", "deep")
"""Report profiles a ``web_research`` task may request — the literal
:class:`~inqtrix.report_profiles.ReportProfile` values. ``schnell``
(1 round / 6 parallel queries) is the DEFAULT agent-child grain of the
``gruendlich`` tier: a plan web task is a bounded STORM round, not a
full Research-Desk run; ``compact``/``deep`` remain per-task upgrades
within the tier's ceiling (tier_policy.TIER_POLICIES)."""

WEB_RESEARCH_PROFILE_ORDER: dict[str, int] = {
    "schnell": 0,
    "compact": 1,
    "deep": 2,
}
"""Monotonic depth order used to enforce a tier's per-task ceiling."""

RAG_PROFILES = ("schnell", "standard", "gruendlich", "tief")
"""Knowledge retrieval profiles a ``rag_query`` task may request."""


class PlanTaskBudget(BaseModel):
    """Deprecated read-compatibility shape for historic plan payloads.

    Resource authority belongs to server/operator quotas. The field is kept
    parseable so old persisted or external payloads fail with one explicit
    validation message instead of an unknown-field error, but it is omitted
    from the planner schema and new plans must leave it empty.
    """

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Deprecated task token ceiling retained only for historic "
            "payload parsing; server/operator quotas are authoritative."
        ),
    )
    """Deprecated task token ceiling; ignored for historic persisted plans."""
    max_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Deprecated task timeout retained only for historic payload "
            "parsing; server/operator timeouts are authoritative."
        ),
    )
    """Deprecated task wall-clock ceiling; operator timeouts are authoritative."""
    max_child_runs: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Deprecated child allowance retained only for historic "
            "payload parsing; approved plan tasks define fan-out."
        ),
    )
    """Deprecated child allowance; the approved plan defines task fan-out."""


class PlanTaskParams(BaseModel):
    """Tool tuning knobs a task may carry.

    Kept as an explicit model (``extra="forbid"``) so a typo'd knob fails
    loudly instead of being silently ignored by the executing tool.
    """

    model_config = ConfigDict(extra="forbid")

    profile: str | None = Field(
        default=None,
        description=(
            "Retrieval profile for web_research or rag_query only. "
            "Set to null for synthesis, web_instant and every other tool kind."
        ),
    )
    """Retrieval size, validated against the task's ``tool_kind``."""
    recency: WebRecency | None = Field(
        default=None,
        description=(
            "Optional provider-neutral web recency filter. Null means no "
            "recency restriction."
        ),
    )
    """Optional provider-neutral freshness hint for web tasks."""
    collection_ids: list[str] | None = None
    """Knowledge collections a ``rag_query`` task is limited to."""

    @field_validator("recency", mode="before")
    @classmethod
    def normalize_empty_optional(cls, value: object) -> object:
        """Treat an empty optional value ('' from a UI field or a strict
        schema that made the key mandatory) as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PlanTaskModel(BaseModel):
    """One task of an execution plan (wire + planner shape)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    """Stable task id, unique within the plan; ``depends_on`` targets."""
    title: str = Field(min_length=1, max_length=300)
    """User-facing label in the answer language."""
    tool_kind: str
    """One of the executable tool kinds; membership is validated in
    :func:`inqtrix.agents.plan_validation.validate_plan` so the error joins
    the collected report instead of a lone Pydantic failure."""
    objective: str = ""
    """What the task must produce."""
    queries: list[str] = Field(
        default_factory=list,
        description=(
            "Self-contained, naturally phrased evidence questions "
            "(web) or guiding questions (rag); max 8, enforced by the "
            "validator — never keyword chains."
        ),
    )
    """Concrete task questions (max 8; enforced by the validator)."""
    gap_ids: list[str] = Field(default_factory=list)
    """Discovery gaps the task covers (synthesis carries none)."""
    depends_on: list[str] = Field(default_factory=list)
    """Task ids that must complete first."""
    budget: SkipJsonSchema[PlanTaskBudget] = Field(
        default_factory=PlanTaskBudget,
        description=(
            "Deprecated read bridge for historic task budgets. It is "
            "omitted from generated schemas and new plans must leave it empty."
        ),
    )
    """Deprecated read bridge; omitted from schemas and rejected if set."""
    params: PlanTaskParams = Field(default_factory=PlanTaskParams)
    expected_output: str = ""
    """Deliverable shape, for the executing child's prompt."""


class ExecutionPlanModel(BaseModel):
    """The complete plan a run proposes for approval."""

    model_config = ConfigDict(extra="forbid")

    summary_markdown: str = ""
    """One-paragraph intent shown above the task list."""
    tasks: list[PlanTaskModel] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    """Non-blocking open points the plan proceeds on (visible to the
    user — the approval IS the chance to correct them)."""
    success_criteria: list[str] = Field(default_factory=list)
    """Measurable criteria in the answer language for the critic to check."""


class ReplanDeltaModel(BaseModel):
    """Server-merged amendment produced after an execution gap.

    The model is deliberately unable to echo or mutate completed work. The
    server owns the immutable prior tasks and reconstructs the single
    synthesis task after merging this delta.
    """

    model_config = ConfigDict(extra="forbid")

    summary_markdown: str = ""
    """Short explanation of why the additional work is needed."""
    new_tasks: list[PlanTaskModel] = Field(default_factory=list)
    """Only new source tasks; synthesis is always server-generated."""
    skip_task_ids: list[str] = Field(default_factory=list)
    """Previously pending task ids the amendment intentionally skips."""
    assumptions: list[str] = Field(default_factory=list)
    """Additional visible assumptions introduced by this amendment."""


__all__ = [
    "ExecutionPlanModel",
    "PlanTaskBudget",
    "PlanTaskModel",
    "PlanTaskParams",
    "ReplanDeltaModel",
    "RAG_PROFILES",
    "TASK_TOOL_KINDS",
    "WEB_RESEARCH_PROFILES",
]
