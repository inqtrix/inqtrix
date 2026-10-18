"""Structured result types returned by :class:`~inqtrix.agent.ResearchAgent`.

All models are Pydantic v2 and fully serialisable to JSON. They are the
typed public view of the internal mutable agent state and are the
canonical surface for downstream consumers (HTTP responses, parity
tooling, custom integrations).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from inqtrix.core.results import SourcePolicy
from inqtrix.evidence import (
    pinned_tiers_from_records,
    attach_web_search_lineage,
    build_web_search_ledger,
)
from inqtrix.urls import normalize_url
from inqtrix.scoring import claim_assessments


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _empty_tier_counts() -> dict[str, int]:
    return {
        "primary": 0,
        "mainstream": 0,
        "stakeholder": 0,
        "unknown": 0,
        "low": 0,
    }


T = TypeVar("T")


_KNOWLEDGE_MACHINE_TOKEN = r"^[A-Za-z0-9_.:-]+$"
_KNOWLEDGE_RESULT_KEYS = frozenset(
    {
        "knowledge_profile",
        "knowledge_gate",
        "knowledge_grounding",
        "knowledge_retrieval",
        "knowledge_candidates",
        "knowledge_evidence_used",
    }
)
_KNOWLEDGE_PROFILE_KEYS = frozenset(
    {
        "id",
        "requested",
        "auto_selected",
        "auto_reason",
        "degraded_stages",
    }
)
_KNOWLEDGE_GATE_KEYS = frozenset(
    {
        "enabled",
        "marker",
        "sufficient",
        "coverage",
        "rounds_used",
        "max_rounds",
        "second_pass",
        "exhausted",
    }
)
_KNOWLEDGE_GROUNDING_KEYS = frozenset(
    {
        "enabled",
        "marker",
        "status",
        "failure_code",
        "format_repaired",
        "quotes_total",
        "quotes_verified",
    }
)
_KNOWLEDGE_RETRIEVAL_KEYS = frozenset(
    {
        "reason",
        "retrieval_mode",
        "stage",
        "requested_candidate_pool",
        "returned_candidate_pool",
        "final_top_k",
        "final_evidence_complete",
        "requested_top_k",
        "returned_hits",
        "candidate_cap",
    }
)
_KNOWLEDGE_RETRIEVAL_WARNING_KEYS = frozenset(
    {
        "code",
        "reason",
        "stage",
        "count",
        "recommended_action",
    }
)


def _selected_mapping(
    value: object,
    *,
    keys: frozenset[str],
    label: str,
) -> dict[str, Any]:
    """Project one raw state block onto its explicit safe field allow-set.

    Knowledge ``result_state`` also carries model-authored gate reasons and
    verbatim grounding quotes.  They remain in the internal execution state
    and in the already-authorized evidence references, but they are not
    duplicated into the compact lifecycle metadata persisted on a run.
    """

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return {key: value[key] for key in keys if key in value}


def _merge_scalar(
    target: dict[str, Any],
    key: str,
    value: Any,
    *,
    label: str,
) -> None:
    if key not in target:
        target[key] = value
        return
    if target[key] != value:
        raise ValueError(f"conflicting {label}.{key} values")


def _merge_block(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
    *,
    label: str,
    union_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    merged = dict(current or {})
    for key, value in incoming.items():
        if key in union_keys:
            existing = merged.get(key, [])
            if not isinstance(existing, list) or not isinstance(value, list):
                raise ValueError(f"{label}.{key} must be a list")
            combined = list(existing)
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
            continue
        _merge_scalar(merged, key, value, label=label)
    return merged


def _append_retrieval_degradation(
    degradations: list[dict[str, Any]],
    incoming: dict[str, Any],
) -> None:
    """Append one receipt, enriching a legacy subset instead of duplicating it."""

    for index, existing in enumerate(degradations):
        if existing == incoming:
            return
        shared_keys = set(existing).intersection(incoming)
        if not all(existing[key] == incoming[key] for key in shared_keys):
            continue
        existing_keys = set(existing)
        incoming_keys = set(incoming)
        if existing_keys.issubset(incoming_keys):
            degradations[index] = incoming
            return
        if incoming_keys.issubset(existing_keys):
            return
    degradations.append(incoming)


def _append_retrieval_warning(
    warnings: list[dict[str, Any]],
    incoming: dict[str, Any],
) -> None:
    """Merge cumulative warning snapshots without double-counting replays."""

    identity_keys = ("code", "reason", "stage", "recommended_action")
    identity = tuple(incoming.get(key) for key in identity_keys)
    for index, existing in enumerate(warnings):
        if tuple(existing.get(key) for key in identity_keys) != identity:
            continue
        warnings[index] = {
            **existing,
            **incoming,
            "count": max(
                int(existing.get("count", 0) or 0),
                int(incoming.get("count", 0) or 0),
            ),
        }
        return
    warnings.append(incoming)



USAGE_SCOPE_RUN = "run"
USAGE_SCOPE_SEGMENT = "segment"


def reported_usage(
    raw: dict[str, Any], result_state: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """The token count a reader is shown, and what that count covers.

    ``usage`` is one SEGMENT's delta, and deliberately so: the quota
    books it after every segment, and a cumulative value there would be
    charged again for every earlier segment. An engine that also keeps a
    running count publishes it as ``usage_total``; only that one is the
    run.

    The scope travels WITH the number because a run that parks has
    several segments - roles park their parent, so every deep run does -
    and the last segment's delta is not the run's spend. A field named
    for the run that carries a segment is not a rounding error, it is a
    wrong answer to the question a reader asked.

    Three places show this number - the trace, the wire, and the report
    metrics - and it lives HERE, at the lowest of the three, so that they
    cannot answer differently. They did not differ; the expressions were
    simply spelled twice, which is the state a single edit point exists
    to prevent rather than the damage it prevents.
    """
    total = result_state.get("usage_total")
    if total:
        return dict(total), USAGE_SCOPE_RUN
    return dict(raw.get("usage") or {}), USAGE_SCOPE_SEGMENT


class AgentToolUseCounts(BaseModel):
    """Successful source-tool invocations split by source family."""

    model_config = ConfigDict(extra="forbid")

    web: int = Field(
        0,
        ge=0,
        description="Successful web-source tool invocations.",
    )
    """Successful web-source tool invocations; never negative."""
    knowledge: int = Field(
        0,
        ge=0,
        description="Successful project-knowledge tool invocations.",
    )
    """Successful project-knowledge tool invocations; never negative."""


class AgentExecutionLimit(BaseModel):
    """One server-authored, user-visible execution boundary."""

    model_config = ConfigDict(extra="forbid")

    used: int | None = Field(
        None,
        ge=0,
        description="Committed usage when the runtime can measure it exactly.",
    )
    limit: int = Field(ge=0, description="Currently effective allowance.")
    ceiling: int = Field(ge=0, description="Operator-enforced maximum allowance.")
    recoverable: bool = Field(
        description="Whether the run can resume from its authoritative checkpoint."
    )
    extendable: bool = Field(
        description="Whether an explicit extension remains below the ceiling."
    )
    reason: str = Field(
        "",
        description="Stable machine-readable reason for a fixed boundary.",
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> "AgentExecutionLimit":
        if self.ceiling < self.limit:
            raise ValueError("execution limit ceiling must not be below limit")
        if self.extendable and (
            not self.recoverable or self.ceiling <= self.limit
        ):
            raise ValueError(
                "an extendable execution limit needs a recoverable higher ceiling"
            )
        return self


class AgentSourceCoverage(BaseModel):
    """What the run actually read of one source version (coverage contract).

    Ranges are half-open Unicode code-point spans (or catalogue entry
    spans when ``unit`` is ``entries``); ``complete`` states that the
    merged read ranges cover the whole source, and ``continuation_open``
    that a remainder, search page or further page is still pending.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    # Bounds mirror the widest source identity the readers can emit (an
    # encoded conversation source reference is accepted up to 4096 chars);
    # a valid ledger must never make a run snapshot unpublishable.
    source_id: str = Field(min_length=1, max_length=4096)
    version: str = Field(default="", max_length=1024)
    unit: Literal["chars", "entries"] = "chars"
    total: int = Field(ge=0)
    read_units: int = Field(ge=0)
    read_ranges: list[list[int]] = Field(default_factory=list)
    scanned_ranges: list[list[int]] = Field(default_factory=list)
    unread_ranges: list[list[int]] = Field(default_factory=list)
    complete: bool
    continuation_open: bool


class AgentExecution(BaseModel):
    """Canonical effective execution state shown by the Agent Desk.

    Persisted snapshots written before the one-shot execution routes were
    withdrawn still carry an ``execution_directive`` key; reading ignores
    unknown keys so those runs stay loadable.
    """

    model_config = ConfigDict(extra="ignore")

    effective_mode: Literal["agent_kernel", "workspace_agent"] = Field(
        description="Algorithm mode that actually executed the run."
    )
    """Algorithm mode that actually executed the run."""
    response_form: Literal["auto", "chat", "canvas"] = Field(
        description="Effective response form: auto, chat, or canvas."
    )
    """Effective response form: auto, chat, or canvas."""
    depth: Literal["normal", "deep"] = Field(
        description="Effective run depth: normal or deep."
    )
    """Effective run depth: normal or deep."""
    model: str = Field(
        description="Resolved model id, or an empty string when unresolved."
    )
    """Resolved model id, or an empty string when unresolved."""
    reasoning_effort: str = Field(
        description=(
            "Resolved reasoning-effort token, or an empty string when the "
            "provider default applies."
        )
    )
    """Resolved reasoning-effort token, or an empty string when the provider default applies."""
    source_policy: SourcePolicy = Field(
        description="Effective web and project-knowledge availability."
    )
    """Effective source availability keyed by web and knowledge."""
    knowledge_scope: Literal["all", "pinned", "inherited"] | None = Field(
        default=None,
        description=(
            "Whose knowledge boundary the run searched: the session switch "
            "(all), attached chips (pinned), the session's inherited scope, "
            "or null when the payload did not record it."
        ),
    )
    """Whose knowledge boundary the run searched: the session switch (all), attached chips (pinned), the session's inherited scope, or null when the payload did not record it."""
    knowledge_collection_count: int = Field(
        default=0,
        ge=0,
        description="Collections inside that boundary; 0 when not recorded.",
    )
    """Collections inside that boundary; 0 when not recorded."""
    consent_reason: Literal[
        "explicit_directive",
        "strict_approval_required",
        "strict_approval",
        "strict_rejected",
        "autonomous_policy",
        "permission_policy",
    ] = Field(
        description="Stable reason why source-tool execution was permitted."
    )
    """Stable reason why source-tool execution was permitted or still requires approval."""
    tool_use_counts: AgentToolUseCounts = Field(
        description="Actual source-tool invocation counts keyed by source."
    )
    """Actual source-tool invocation counts keyed by web and knowledge."""
    web_searches: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Web searches the run's own provider calls executed; a delegated "
            "research counts its own in its snapshot (executed_queries). Null "
            "when the payload did not record it."
        ),
    )
    """Web searches the run's own provider calls executed; null when the payload did not record it."""
    limits: dict[str, AgentExecutionLimit] = Field(
        default_factory=dict,
        description=(
            "Server-authored execution boundaries keyed by stable limit id."
        ),
    )
    """Visible limits; an empty mapping preserves older result payloads."""
    tool_grants: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Tools the user approved run-wide (approval_scope=run); "
            "empty when nothing is granted."
        ),
    )
    """Run-wide tool grants; the default preserves older payloads."""
    source_coverage: list[AgentSourceCoverage] = Field(
        default_factory=list,
        description=(
            "Per source version: the ranges the run actually read or "
            "searched, its size, and whether a continuation is still open. "
            "Empty for algorithms without windowed readers."
        ),
    )
    """Source coverage ledger; the default preserves older payloads."""
    work_profile: str = Field(
        "",
        description=(
            "Internal work profile the run executed under. Empty for every "
            "ordinary run."
        ),
    )
    """Work profile of an internal role run; the default preserves older payloads."""


class KnowledgeProfileResult(BaseModel):
    """Effective Knowledge profile without question or evidence content."""

    model_config = ConfigDict(extra="forbid")

    id: Literal["schnell", "standard", "gruendlich", "tief"]
    requested: Literal[
        "schnell", "standard", "gruendlich", "tief", "auto"
    ] | None = None
    auto_selected: bool = False
    auto_reason: str | None = Field(
        None,
        max_length=64,
        pattern=_KNOWLEDGE_MACHINE_TOKEN,
    )
    degraded_stages: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("requested", "auto_reason", mode="before")
    @classmethod
    def _blank_optional_values(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _validate_profile_contract(self) -> "KnowledgeProfileResult":
        if self.auto_selected and self.requested not in {None, "auto"}:
            raise ValueError(
                "an auto-selected profile cannot name a non-auto request"
            )
        if not self.auto_selected and self.auto_reason:
            raise ValueError(
                "auto_reason is valid only for an auto-selected profile"
            )
        for stage in self.degraded_stages:
            if not isinstance(stage, str) or not re.fullmatch(
                _KNOWLEDGE_MACHINE_TOKEN, stage
            ):
                raise ValueError("degraded_stages must contain machine tokens")
            if len(stage) > 64:
                raise ValueError("degraded stage exceeds 64 characters")
        if len(set(self.degraded_stages)) != len(self.degraded_stages):
            raise ValueError("degraded_stages must be deduplicated")
        return self


class KnowledgeGateResult(BaseModel):
    """Bounded sufficiency-gate receipt safe for run lifecycle surfaces."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    marker: str | None = Field(
        None,
        max_length=64,
        pattern=_KNOWLEDGE_MACHINE_TOKEN,
    )
    sufficient: bool | None = None
    coverage: Literal["full", "partial", "none"] | None = None
    rounds_used: int | None = Field(None, ge=0, le=32)
    max_rounds: int | None = Field(None, ge=0, le=32)
    second_pass: bool | None = None
    exhausted: bool | None = None

    @field_validator("marker", mode="before")
    @classmethod
    def _blank_marker(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _validate_gate_contract(self) -> "KnowledgeGateResult":
        if not self.enabled and (
            self.marker is not None
            or self.sufficient is not None
            or self.coverage is not None
            or self.rounds_used not in {None, 0}
            or self.max_rounds not in {None, 0}
            or self.second_pass not in {None, False}
            or self.exhausted not in {None, False}
        ):
            raise ValueError("a disabled Knowledge gate cannot report a verdict")
        if (
            self.rounds_used is not None
            and self.max_rounds is not None
            and self.rounds_used > self.max_rounds
        ):
            raise ValueError("gate rounds_used cannot exceed max_rounds")
        if self.second_pass is True and not (self.rounds_used or 0) >= 1:
            raise ValueError("second_pass requires at least one rewrite round")
        return self


class KnowledgeGroundingResult(BaseModel):
    """Grounding verdict and counts; verbatim source quotes are excluded."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    marker: str | None = Field(
        None,
        max_length=64,
        pattern=_KNOWLEDGE_MACHINE_TOKEN,
    )
    status: Literal["verified", "rejected_format", "rejected_quote"] | None = None
    failure_code: Literal[
        "knowledge_grounding_format_invalid",
        "knowledge_grounding_quote_unverified",
    ] | None = None
    format_repaired: bool | None = None
    quotes_total: int | None = Field(None, ge=0, le=1_000)
    quotes_verified: int | None = Field(None, ge=0, le=1_000)

    @field_validator("marker", "status", "failure_code", mode="before")
    @classmethod
    def _blank_optional_values(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _validate_grounding_contract(self) -> "KnowledgeGroundingResult":
        if not self.enabled and (
            self.marker is not None
            or self.status is not None
            or self.failure_code is not None
            or self.format_repaired not in {None, False}
            or self.quotes_total not in {None, 0}
            or self.quotes_verified not in {None, 0}
        ):
            raise ValueError(
                "disabled Knowledge grounding cannot report a verdict"
            )
        if (
            self.quotes_verified is not None
            and self.quotes_total is not None
            and self.quotes_verified > self.quotes_total
        ):
            raise ValueError("quotes_verified cannot exceed quotes_total")
        if self.status == "verified" and self.failure_code is not None:
            raise ValueError("verified grounding cannot carry a failure code")
        if self.status in {"rejected_format", "rejected_quote"} and (
            self.failure_code is None
        ):
            raise ValueError("rejected grounding requires a failure code")
        return self


class KnowledgeRetrievalDegradationResult(BaseModel):
    """One technical retrieval shortfall, separate from corpus exhaustion."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        max_length=64,
        pattern=_KNOWLEDGE_MACHINE_TOKEN,
    )
    retrieval_mode: str = Field(
        max_length=64,
        pattern=_KNOWLEDGE_MACHINE_TOKEN,
    )
    stage: str = Field(
        "vector_candidate_pool",
        max_length=64,
        pattern=_KNOWLEDGE_MACHINE_TOKEN,
    )
    requested_candidate_pool: int | None = Field(None, ge=0, le=1_000_000)
    returned_candidate_pool: int | None = Field(None, ge=0, le=1_000_000)
    final_top_k: int | None = Field(None, ge=0, le=1_000_000)
    final_evidence_complete: bool | None = None
    requested_top_k: int = Field(ge=0, le=1_000_000)
    returned_hits: int = Field(ge=0, le=1_000_000)
    candidate_cap: int | None = Field(None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _validate_retrieval_contract(
        self,
    ) -> "KnowledgeRetrievalDegradationResult":
        final_top_k = (
            self.requested_top_k
            if self.final_top_k is None
            else self.final_top_k
        )
        if self.final_top_k is not None and self.requested_top_k != final_top_k:
            raise ValueError("requested_top_k must describe the final_top_k")
        if self.returned_hits > final_top_k:
            raise ValueError("returned_hits cannot exceed final_top_k")
        if (
            self.requested_candidate_pool is not None
            and self.returned_candidate_pool is not None
            and self.returned_candidate_pool > self.requested_candidate_pool
        ):
            raise ValueError(
                "returned_candidate_pool cannot exceed requested_candidate_pool"
            )
        if (
            self.returned_candidate_pool is not None
            and self.returned_hits > self.returned_candidate_pool
        ):
            raise ValueError(
                "final returned_hits cannot exceed the returned candidate pool"
            )
        if (
            self.candidate_cap is not None
            and self.returned_candidate_pool is not None
            and self.returned_candidate_pool > self.candidate_cap
        ):
            raise ValueError("returned candidate pool cannot exceed candidate_cap")
        complete = self.returned_hits >= final_top_k
        if (
            self.final_evidence_complete is not None
            and self.final_evidence_complete is not complete
        ):
            raise ValueError("final_evidence_complete conflicts with hit counts")
        self.final_top_k = final_top_k
        self.final_evidence_complete = complete
        return self


class KnowledgeRetrievalWarningResult(BaseModel):
    """One source-integrity warning emitted by canonical retrieval."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=64, pattern=_KNOWLEDGE_MACHINE_TOKEN)
    reason: str = Field(max_length=64, pattern=_KNOWLEDGE_MACHINE_TOKEN)
    stage: str = Field(max_length=64, pattern=_KNOWLEDGE_MACHINE_TOKEN)
    count: int = Field(ge=1, le=1_000_000)
    recommended_action: str | None = Field(
        None,
        max_length=64,
        pattern=_KNOWLEDGE_MACHINE_TOKEN,
    )

    @field_validator("recommended_action", mode="before")
    @classmethod
    def _blank_recommended_action(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class KnowledgeRetrievalResult(BaseModel):
    """Deduplicated bounded warning ledger for one Knowledge answer."""

    model_config = ConfigDict(extra="forbid")

    degradations: list[KnowledgeRetrievalDegradationResult] = Field(
        default_factory=list,
        max_length=64,
    )
    warnings: list[KnowledgeRetrievalWarningResult] | None = Field(
        None,
        max_length=64,
    )

    @model_validator(mode="after")
    def _validate_deduplicated(self) -> "KnowledgeRetrievalResult":
        identities = [
            item.model_dump_json(exclude_none=True)
            for item in self.degradations
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("retrieval degradations must be deduplicated")
        if self.warnings is not None:
            warning_identities = [
                (
                    item.code,
                    item.reason,
                    item.stage,
                    item.recommended_action,
                )
                for item in self.warnings
            ]
            if len(warning_identities) != len(set(warning_identities)):
                raise ValueError("retrieval warnings must be deduplicated")
        return self


class KnowledgeResultState(BaseModel):
    """Compact, text-free Knowledge execution receipt persisted with a run.

    The model deliberately excludes user queries, gate prose, verbatim quotes,
    document bodies and provider payloads.  Evidence text continues to travel
    only through the separately authorized reference contract.
    """

    model_config = ConfigDict(extra="forbid")

    profile: KnowledgeProfileResult | None = None
    gate: KnowledgeGateResult | None = None
    grounding: KnowledgeGroundingResult | None = None
    retrieval: KnowledgeRetrievalResult | None = None
    candidate_count: int | None = Field(None, ge=0, le=1_000_000)
    evidence_used: int | None = Field(None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _validate_counts(self) -> "KnowledgeResultState":
        if (
            self.candidate_count is not None
            and self.evidence_used is not None
            and self.evidence_used > self.candidate_count
        ):
            raise ValueError("knowledge evidence_used cannot exceed candidates")
        return self

    @classmethod
    def from_sources(
        cls,
        *sources: Mapping[str, Any] | "KnowledgeResultState" | None,
    ) -> "KnowledgeResultState | None":
        """Conflict-check and merge raw result/snapshot projections.

        Multiple sources are expected during reload: the final result is
        authoritative presentation, while the compact snapshot is its durable
        lifecycle companion.  Equal/subset blocks merge; conflicting known
        fields fail closed. Retrieval degradations and source-integrity
        warnings accumulate in first-seen order; repeated cumulative warning
        snapshots retain the highest observed count.
        """

        mappings: list[Mapping[str, Any]] = []
        for source in sources:
            if source is None:
                continue
            if isinstance(source, cls):
                mappings.append(source.to_export_fields())
                continue
            if not isinstance(source, Mapping):
                raise ValueError("Knowledge result source must be an object")
            nested = source.get("result_state")
            if nested is not None:
                if not isinstance(nested, Mapping):
                    raise ValueError("result_state must be an object")
                mappings.append(nested)
            knowledge = source.get("knowledge")
            if isinstance(knowledge, cls):
                mappings.append(knowledge.to_export_fields())
            elif knowledge is not None:
                if not isinstance(knowledge, Mapping):
                    raise ValueError("knowledge must be an object")
                nested_model = cls.model_validate(knowledge)
                mappings.append(nested_model.to_export_fields())
            mappings.append(source)

        profile: dict[str, Any] | None = None
        gate: dict[str, Any] | None = None
        grounding: dict[str, Any] | None = None
        degradations: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        warnings_present = False
        candidate_count: int | None = None
        evidence_used: int | None = None
        found = False
        for mapping in mappings:
            if not any(key in mapping for key in _KNOWLEDGE_RESULT_KEYS):
                continue
            found = True
            if "knowledge_profile" in mapping:
                profile = _merge_block(
                    profile,
                    _selected_mapping(
                        mapping["knowledge_profile"],
                        keys=_KNOWLEDGE_PROFILE_KEYS,
                        label="knowledge_profile",
                    ),
                    label="knowledge_profile",
                    union_keys=frozenset({"degraded_stages"}),
                )
            if "knowledge_gate" in mapping:
                gate = _merge_block(
                    gate,
                    _selected_mapping(
                        mapping["knowledge_gate"],
                        keys=_KNOWLEDGE_GATE_KEYS,
                        label="knowledge_gate",
                    ),
                    label="knowledge_gate",
                )
            if "knowledge_grounding" in mapping:
                grounding = _merge_block(
                    grounding,
                    _selected_mapping(
                        mapping["knowledge_grounding"],
                        keys=_KNOWLEDGE_GROUNDING_KEYS,
                        label="knowledge_grounding",
                    ),
                    label="knowledge_grounding",
                )
            if "knowledge_retrieval" in mapping:
                retrieval = _selected_mapping(
                    mapping["knowledge_retrieval"],
                    keys=frozenset({"degradations", "warnings"}),
                    label="knowledge_retrieval",
                )
                raw_degradations = retrieval.get("degradations", [])
                if not isinstance(raw_degradations, list):
                    raise ValueError(
                        "knowledge_retrieval.degradations must be a list"
                    )
                for raw_degradation in raw_degradations:
                    projected = _selected_mapping(
                        raw_degradation,
                        keys=_KNOWLEDGE_RETRIEVAL_KEYS,
                        label="knowledge_retrieval.degradation",
                    )
                    parsed = KnowledgeRetrievalDegradationResult.model_validate(
                        projected
                    ).model_dump(exclude_none=True)
                    _append_retrieval_degradation(degradations, parsed)
                if len(degradations) > 64:
                    raise ValueError(
                        "knowledge retrieval degradations exceed the safe limit"
                    )
                if "warnings" in retrieval:
                    warnings_present = True
                    raw_warnings = retrieval["warnings"]
                    if not isinstance(raw_warnings, list):
                        raise ValueError(
                            "knowledge_retrieval.warnings must be a list"
                        )
                    for raw_warning in raw_warnings:
                        projected = _selected_mapping(
                            raw_warning,
                            keys=_KNOWLEDGE_RETRIEVAL_WARNING_KEYS,
                            label="knowledge_retrieval.warning",
                        )
                        parsed = (
                            KnowledgeRetrievalWarningResult.model_validate(
                                projected
                            ).model_dump(exclude_none=True)
                        )
                        _append_retrieval_warning(warnings, parsed)
                    if len(warnings) > 64:
                        raise ValueError(
                            "knowledge retrieval warnings exceed the safe limit"
                        )
            if "knowledge_candidates" in mapping:
                value = mapping["knowledge_candidates"]
                if candidate_count is None:
                    candidate_count = value
                elif candidate_count != value:
                    raise ValueError("conflicting knowledge_candidates values")
            if "knowledge_evidence_used" in mapping:
                value = mapping["knowledge_evidence_used"]
                if evidence_used is None:
                    evidence_used = value
                elif evidence_used != value:
                    raise ValueError(
                        "conflicting knowledge_evidence_used values"
                    )

        if not found:
            return None
        return cls.model_validate(
            {
                "profile": profile,
                "gate": gate,
                "grounding": grounding,
                "retrieval": (
                    {
                        "degradations": degradations,
                        **(
                            {"warnings": warnings}
                            if warnings_present or warnings
                            else {}
                        ),
                    }
                    if degradations
                    or warnings
                    or any(
                        "knowledge_retrieval" in mapping
                        for mapping in mappings
                    )
                    else None
                ),
                "candidate_count": candidate_count,
                "evidence_used": evidence_used,
            }
        )

    def to_export_fields(self) -> dict[str, Any]:
        """Return the established flat Knowledge result wire projection."""

        payload: dict[str, Any] = {}
        if self.profile is not None:
            payload["knowledge_profile"] = self.profile.model_dump(
                exclude_none=True
            )
        if self.gate is not None:
            payload["knowledge_gate"] = self.gate.model_dump(exclude_none=True)
        if self.grounding is not None:
            payload["knowledge_grounding"] = self.grounding.model_dump(
                exclude_none=True
            )
        if self.retrieval is not None:
            payload["knowledge_retrieval"] = self.retrieval.model_dump(
                exclude_none=True
            )
        if self.candidate_count is not None:
            payload["knowledge_candidates"] = self.candidate_count
        if self.evidence_used is not None:
            payload["knowledge_evidence_used"] = self.evidence_used
        return payload


def _limit_items(items: list[T], limit: int | None) -> list[T]:
    if limit is None:
        return list(items)
    return list(items[:limit])


def _append_unique_url(urls: list[str], value: Any) -> None:
    url = normalize_url(str(value or ""))
    if url and url not in urls:
        urls.append(url)


def _extract_used_answer_urls(answer: str) -> list[str]:
    """Return Markdown-linked URLs used in the generated answer body."""
    urls: list[str] = []
    for match in _MARKDOWN_LINK_RE.finditer(answer or ""):
        _append_unique_url(urls, match.group(2))
    return urls


def _report_references_from_state(
    references: Any,
    *,
    tier_by_url: dict[str, str],
    tiering: Any,
) -> list[ReportReference]:
    if not isinstance(references, list):
        return []

    report_references: list[ReportReference] = []
    seen: set[str] = set()
    for index, reference in enumerate(references, 1):
        if not isinstance(reference, dict):
            continue
        document_id_raw = reference.get("document_id")
        document_id = str(document_id_raw) if document_id_raw else None
        chunk_index_raw = reference.get("chunk_index")
        chunk_index = (
            int(chunk_index_raw)
            if isinstance(chunk_index_raw, (int, float))
            else None
        )
        url = normalize_url(str(reference.get("url", "") or ""))
        if not url and document_id:
            url = f"inqtrix://documents/{document_id}"
            if chunk_index is not None:
                url += f"#chunk-{chunk_index}"
        if not url:
            continue
        # Knowledge citations are identified by (document_id, chunk_index):
        # distinct chunks of the SAME document must NOT collapse, but the
        # default `inqtrix://documents/{id}#chunk-{n}` URL loses its fragment to
        # ``normalize_url`` — so de-duping by URL would silently drop every
        # cited chunk after the first. Web references (no document id) keep the
        # URL-dedup that conflates the same source cited twice.
        dedup_key = f"doc:{document_id}#{chunk_index}" if document_id else url
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        label = str(reference.get("label", "") or f"Quelle {index}")
        tier = str(
            reference.get("tier", "")
            or tier_by_url.get(url)
            or tiering.tier_for_url(url)
        )
        title_raw = reference.get("title")
        title = str(title_raw) if title_raw else None
        excerpt_raw = reference.get("excerpt")
        source_text_raw = reference.get("source_text")
        grounded_support_raw = reference.get("grounded_support")
        provider_snippet_raw = reference.get("provider_snippet")
        page_number_raw = reference.get("page_number")
        source_span_raw = reference.get("source_span")
        report_references.append(
            ReportReference(
                label=label,
                url=url,
                tier=tier,
                tier_reason=str(reference.get("tier_reason", "") or ""),
                role_reason=str(reference.get("role_reason", "") or ""),
                cited=bool(reference.get("cited", True)),
                title=title,
                document_id=document_id,
                chunk_index=chunk_index,
                excerpt=str(excerpt_raw) if excerpt_raw else None,
                source_text=str(source_text_raw) if source_text_raw else None,
                quotes=[
                    ReportReferenceQuote(
                        text=quote["text"],
                        verified=bool(quote.get("verified")),
                        artifact_tolerated=bool(quote.get("artifact_tolerated")),
                    )
                    for quote in reference.get("quotes") or []
                    if isinstance(quote, dict)
                    and isinstance(quote.get("text"), str)
                    and quote["text"]
                ],
                grounded_support=(
                    str(grounded_support_raw)
                    if grounded_support_raw
                    else None
                ),
                provider_snippet=(
                    str(provider_snippet_raw)
                    if provider_snippet_raw
                    else None
                ),
                page_number=(
                    int(page_number_raw)
                    if isinstance(page_number_raw, (int, float))
                    and not isinstance(page_number_raw, bool)
                    else None
                ),
                reference_id=(
                    str(reference.get("reference_id"))
                    if reference.get("reference_id")
                    else None
                ),
                source_id=(
                    str(reference.get("source_id"))
                    if reference.get("source_id")
                    else None
                ),
                query_id=(
                    str(reference.get("query_id"))
                    if reference.get("query_id")
                    else None
                ),
                query_ids=[
                    str(value)
                    for value in reference.get("query_ids", [])
                    if str(value)
                ],
                citation_id=(
                    str(reference.get("citation_id"))
                    if reference.get("citation_id")
                    else None
                ),
                citation_ids=[
                    str(value)
                    for value in reference.get("citation_ids", [])
                    if str(value)
                ],
                source_run_id=(
                    str(reference.get("source_run_id"))
                    if reference.get("source_run_id")
                    else None
                ),
                source_run_ids=[
                    str(value)
                    for value in reference.get("source_run_ids", [])
                    if str(value)
                ],
                source_span=(
                    dict(source_span_raw)
                    if isinstance(source_span_raw, dict)
                    else None
                ),
                revision_id=(
                    str(reference.get("revision_id"))
                    if reference.get("revision_id")
                    else None
                ),
                generation_id=(
                    str(reference.get("generation_id"))
                    if reference.get("generation_id")
                    else None
                ),
                provenance_status=(
                    str(reference.get("provenance_status"))
                    if reference.get("provenance_status")
                    else None
                ),
                statements=[
                    ReportReferenceStatement(
                        text=str(item["text"]),
                        status=str(item.get("status") or "unverified"),
                        checked=bool(item.get("checked")),
                    )
                    for item in reference.get("statements") or []
                    if isinstance(item, dict) and item.get("text")
                ],
            )
        )
    return report_references


class Source(BaseModel):
    """A single cited source with its quality tier classification.

    Sources are the URLs that contributed to the final answer. Each
    source carries a ``tier`` label assigned by the active
    ``SourceTieringStrategy``; downstream consumers can use the tier to
    order or visualise sources without re-running the tiering logic. The tier
    is not a claim-verification result and never filters the provider result.
    """

    url: str = Field(
        ...,
        description=(
            "Absolute URL of the source as captured from the search "
            "provider's citations list. Already normalised (scheme + "
            "host lower-cased, fragment stripped) by the agent."
        ),
    )
    """Absolute URL of the source as captured from the search provider's citations list. Already normalised (scheme + host lower-cased, fragment stripped) by the agent."""
    tier: str = Field(
        "unknown",
        description=(
            "Quality tier assigned by the source-tiering strategy. One "
            "of ``primary`` (peer-reviewed / official), ``mainstream`` "
            "(established media), ``stakeholder`` (industry / NGO / "
            "association), ``unknown`` (no classification), or ``low`` "
            "(known low-quality domains)."
        ),
    )
    """Quality tier assigned by the source-tiering strategy. One of ``primary`` (peer-reviewed / official), ``mainstream`` (established media), ``stakeholder`` (industry / NGO / association), ``unknown`` (no classification), or ``low`` (known low-quality domains)."""


class ReportReferenceQuote(BaseModel):
    """One verbatim quote the published answer grounded on a reference."""

    text: str = Field(
        ...,
        description=(
            "The quote as the answer stated it, without surrounding "
            "quotation marks. It occurs in the reference's ``source_text``, "
            "tolerant only to formatting (whitespace, typography, case)."
        ),
    )
    """The quote as the answer stated it, without surrounding quotation marks; it occurs in the reference's ``source_text``, tolerant only to formatting."""
    verified: bool = Field(
        ...,
        description=(
            "Whether the deterministic grounding check found the quote in "
            "this reference's source text."
        ),
    )
    """Whether the deterministic grounding check found the quote in this reference's source text."""
    artifact_tolerated: bool = Field(
        False,
        description=(
            "Whether the quote verified only across a tolerated extraction "
            "artifact (a page break inside the sentence, a word broken at a "
            "line end), never through the raw text."
        ),
    )
    """Whether the quote verified only across a tolerated extraction artifact, never through the raw text."""


class ReportReferenceStatement(BaseModel):
    """One statement the research ties to a reference."""

    text: str
    """The statement as the claim ledger holds it."""
    status: str = "unverified"
    """The claim's status: ``verified``, ``attributed``, ``contested`` or
    ``unverified``."""
    checked: bool = False
    """Whether THIS source's own text was checked and states the statement;
    a statement verified through another page is only attributed here."""


class ReportReference(BaseModel):
    """One reference entry rendered in the final report appendix.

    Report references are the structured counterpart of the markdown
    ``## Referenzen`` section. They preserve the human-visible label,
    canonical URL, and source tier so UI clients can show the exact
    report reference list without parsing markdown.
    """

    label: str = Field(
        ...,
        description=(
            "Human-readable reference label exactly as selected for the "
            "report appendix, such as an evidence label (``E3``), numeric "
            "citation label, or fallback ``Quelle N`` label."
        ),
    )
    """Human-readable reference label exactly as selected for the report appendix, such as an evidence label (``E3``), numeric citation label, or fallback ``Quelle N`` label."""
    url: str = Field(
        ...,
        description=(
            "Canonical absolute URL shown in the report's ``Referenzen`` "
            "section. Already normalized by the agent before export."
        ),
    )
    """Canonical absolute URL shown in the report's ``Referenzen`` section. Already normalized by the agent before export."""
    tier: str = Field(
        "unknown",
        description=(
            "Quality tier assigned to this reference URL by the active "
            "source-tiering strategy. Uses the same labels as "
            ":class:`Source.tier`."
        ),
    )
    """Quality tier assigned to this reference URL by the active source-tiering strategy. Uses the same labels as :class:`Source.tier`."""
    tier_reason: str = Field(
        "",
        description=(
            "How the tier came about: ``structural:<role>`` (a registry-class "
            "suffix proved it), ``model:<role>`` (the claim-extraction call "
            "judged the source), ``unjudged`` (neither) or empty on payloads "
            "without provenance."
        ),
    )
    """How the tier came about: ``structural:<role>`` (a registry-class suffix proved it), ``model:<role>`` (the claim-extraction call judged the source), ``unjudged`` (neither) or empty on payloads without provenance."""
    role_reason: str = Field(
        "",
        description=(
            "The extractor's evidence for a judged role in a few words "
            "(``Foerderbank des Bundes``); empty for structural and unjudged "
            "sources."
        ),
    )
    """The extractor's evidence for a judged role in a few words (``Foerderbank des Bundes``); empty for structural and unjudged sources."""
    cited: bool = Field(
        True,
        description=(
            "Whether the answer body cites this source (listed under the "
            "references heading) or the writer only saw it in the evidence "
            "overview without citing it (listed under the further-sources "
            "heading). Both headings follow the answer language through "
            "``prompt_language.heading_for``. Additive field — older payloads "
            "without it validate as cited."
        ),
    )
    """Whether the answer body cites this source (listed under the references heading) or the writer only saw it in the evidence overview without citing it (listed under the further-sources heading). Both headings follow the answer language through ``prompt_language.heading_for``. Additive field — older payloads without it validate as cited."""
    title: str | None = Field(
        None,
        description=(
            "Human-readable title of the cited source, e.g. the original "
            "document filename for a knowledge citation. ``None`` when the "
            "producer supplied no title; clients then fall back to the URL. "
            "Additive field — older payloads without it still validate."
        ),
    )
    """Human-readable title of the cited source, e.g. the original document filename for a knowledge citation. ``None`` when the producer supplied no title; clients then fall back to the URL. Additive field — older payloads without it still validate."""
    document_id: str | None = Field(
        None,
        description=(
            "Knowledge-document id this citation points into. Lets clients "
            "open the exact source reliably (not only by parsing the URL). "
            "``None`` for web references."
        ),
    )
    """Knowledge-document id this citation points into; lets clients open the exact source reliably. ``None`` for web references."""
    chunk_index: int | None = Field(
        None,
        description=(
            "0-based index of the cited chunk within its document, so the UI "
            "can label/locate the passage. ``None`` for web references."
        ),
    )
    """0-based index of the cited chunk within its document. ``None`` for web references."""
    excerpt: str | None = Field(
        None,
        description=(
            "The exact retrieved chunk text the answer was grounded in — the "
            "passage shown (with the cited span highlighted) when the user "
            "verifies a knowledge citation. ``None`` for web references."
        ),
    )
    """The exact retrieved chunk text the answer was grounded in — shown as the verifiable source passage. ``None`` for web references."""
    source_text: str | None = Field(
        None,
        description=(
            "The chunk's ORIGINAL source text (without any contextualization "
            "prefix the retrieval added), used to verify a quoted span against "
            "the real document. Falls back to ``excerpt`` when absent."
        ),
    )
    """The chunk's original source text (sans contextualization prefix), for verifying a quoted span. ``None`` for web references."""
    quotes: list[ReportReferenceQuote] = Field(
        default_factory=list,
        description=(
            "The verbatim quotes a published Knowledge answer grounded on "
            "this reference, in the answer's order: each one verified "
            "against ``source_text``. Empty for web references, for an "
            "answer without grounding and for a rejected answer, whose "
            "draft is never exposed."
        ),
    )
    """The verbatim quotes a published Knowledge answer grounded on this reference, in the answer's order; empty for web references, ungrounded and rejected answers."""
    grounded_support: str | None = Field(
        None,
        description=(
            "Bounded prose from a provider-grounded answer surrounding this "
            "web citation. It records the provider answer's support context "
            "and is not a verbatim source excerpt. ``None`` when an exact "
            "excerpt exists or the provider supplied no grounded context."
        ),
    )
    """Bounded provider-answer context for a web citation; never presented as a verbatim source excerpt."""
    provider_snippet: str | None = Field(
        None,
        description=(
            "Snippet supplied by the web-search provider for this citation. "
            "It is provider metadata, not a verbatim excerpt independently "
            "read from the linked page."
        ),
    )
    page_number: int | None = Field(
        None,
        description=(
            "Best-effort 1-based source page of the cited chunk (PDF knowledge "
            "sources only), for a page-level 'open PDF at page N' jump. ``None`` "
            "when the source has no pages, the mapping was inconclusive, or for "
            "web references."
        ),
    )
    """Best-effort 1-based source page of the cited chunk (PDF knowledge sources only). ``None`` when unmapped or for web references."""
    reference_id: str | None = Field(
        None,
        description=(
            "Opaque run-local reference identity used to resolve the durable "
            "evidence artifact."
        ),
    )
    """Opaque run-local reference identity used to resolve the evidence artifact."""
    source_id: str | None = Field(
        None,
        description="Stable source identity inside the web-search ledger.",
    )
    """Stable source identity inside the web-search ledger."""
    query_id: str | None = Field(
        None,
        description="Primary provider-search invocation that returned the source.",
    )
    query_ids: list[str] = Field(
        default_factory=list,
        description="All provider-search invocations that returned the source.",
    )
    citation_id: str | None = Field(
        None,
        description="Primary provider citation record for this reference.",
    )
    citation_ids: list[str] = Field(
        default_factory=list,
        description="All provider citation records linked to this reference.",
    )
    source_run_id: str | None = Field(
        None,
        description="Run that executed the primary provider search.",
    )
    source_run_ids: list[str] = Field(
        default_factory=list,
        description="Runs whose provider searches returned this reference.",
    )
    source_span: dict[str, Any] | None = Field(
        None,
        description=(
            "Exact Knowledge source span, offset unit, and content-hash "
            "contract where available."
        ),
    )
    """Exact Knowledge source span and offset contract where available."""
    revision_id: str | None = Field(
        None,
        description="Immutable Knowledge document revision that supplied the hit.",
    )
    generation_id: str | None = Field(
        None,
        description="Index generation from which the Knowledge hit was resolved.",
    )
    provenance_status: str | None = Field(
        None,
        description=(
            "Machine-readable source-span verification status for the evidence."
        ),
    )
    statements: list[ReportReferenceStatement] = Field(
        default_factory=list,
        description=(
            "Every statement the claim ledger ties to this source, from "
            "every search that cited it, once each, with its status and "
            "whether this source's own text was checked. Empty without "
            "claims and on older payloads."
        ),
    )


class Claim(BaseModel):
    """A consolidated claim extracted during research.

    This is the public, typed view of one entry in the internal
    consolidated claim ledger. It preserves the verification metadata
    (status, support/contradict counts, source-tier breakdown) needed
    for downstream inspection without leaking the full mutable agent
    state.
    """

    text: str = Field(
        ...,
        description=(
            "Canonical claim text after consolidation. May be a "
            "rewritten merge of several similar claims that the "
            "consolidation strategy considered semantically equivalent."
        ),
    )
    """Canonical claim text after consolidation. May be a rewritten merge of several similar claims that the consolidation strategy considered semantically equivalent."""
    status: str = Field(
        "unverified",
        description=(
            "Verification status assigned by the consolidation "
            "strategy. One of ``verified`` (supported, and a source's own "
            "text was checked and states the claim), ``attributed`` "
            "(supported, but only the search provider's answer ties the "
            "claim to its sources; no source text was checked), "
            "``contested`` (supporting and contradicting sources, or "
            "conflicting values, both present), or ``unverified`` "
            "(insufficient evidence). Before 0.2.0.10 ``verified`` also "
            "covered provider attribution."
        ),
    )
    """Verification status assigned by the consolidation strategy."""
    claim_type: str = Field(
        "fact",
        description=(
            "Claim taxonomy assigned by the extractor. One of "
            "``fact`` (verifiable factual statement), ``actor_claim`` "
            "(a position attributed to a named actor), or ``forecast`` "
            "(a forward-looking projection). Used by the answer "
            "composer to phrase claims appropriately."
        ),
    )
    """Claim taxonomy assigned by the extractor. One of ``fact`` (verifiable factual statement), ``actor_claim`` (a position attributed to a named actor), or ``forecast`` (a forward-looking projection). Used by the answer composer to phrase claims appropriately."""
    needs_primary: bool = Field(
        False,
        description=(
            "True when the claim needs primary-tier or independently "
            "corroborated provider-grounded support. Surfaced in DEEP-profile "
            "reports as an evidence-depth transparency hint."
        ),
    )
    """Whether the claim needs stronger primary or corroborated web evidence."""
    status_reason: str = Field(
        "",
        description=(
            "Free-text justification produced by the consolidation "
            "strategy explaining why the claim received its ``status``. "
            "Empty string when no reason is available."
        ),
    )
    """Free-text justification produced by the consolidation strategy explaining why the claim received its ``status``. Empty string when no reason is available."""
    support_count: int = Field(
        0,
        description=(
            "Number of distinct sources that explicitly support this "
            "claim, after deduplication of near-identical phrasings."
        ),
    )
    """Number of distinct sources that explicitly support this claim, after deduplication of near-identical phrasings."""
    contradict_count: int = Field(
        0,
        description=(
            "Number of distinct sources that explicitly contradict "
            "this claim. Even one contradiction shifts the status from "
            "``verified`` to ``contested`` under the default policy."
        ),
    )
    """Number of distinct sources that explicitly contradict this claim. Even one contradiction shifts the status from ``verified`` to ``contested`` under the default policy."""
    source_tier_counts: dict[str, int] = Field(
        default_factory=_empty_tier_counts,
        description=(
            "Per-tier breakdown of the supporting sources for this "
            "claim. Keys are the five tier labels; values are integer "
            "counts. Always populated for all five tiers (zero for "
            "absent tiers) for stable downstream consumption."
        ),
    )
    """Per-tier breakdown of the supporting sources for this claim. Keys are the five tier labels; values are integer counts. Always populated for all five tiers (zero for absent tiers) for stable downstream consumption."""
    sources: list[str] = Field(
        default_factory=list,
        description=(
            "Source URLs that back this claim, in the order produced "
            "by the consolidation strategy (typically primary tiers "
            "first). Capped per ``ReportProfileTuning.claim_source_url_cap`` "
            "so the list stays bounded for large research runs."
        ),
    )
    """Source URLs that back this claim, in the order produced by the consolidation strategy (typically primary tiers first). Capped per ``ReportProfileTuning.claim_source_url_cap`` so the list stays bounded for large research runs."""

    @classmethod
    def from_consolidated(cls, claim_data: dict) -> Claim:
        """Build a public claim model from one internal consolidated claim.

        Args:
            claim_data: Raw consolidated-claim dict as produced by
                :meth:`~inqtrix.strategies.ClaimConsolidationStrategy.materialize`.
                Missing keys are tolerated and substituted with their
                public defaults so that older state snapshots remain
                consumable.

        Returns:
            A new :class:`Claim` instance with all numeric counts coerced
            to ``int`` and ``source_tier_counts`` normalised to contain
            all five tier keys.
        """
        return cls(
            text=claim_data.get("claim_text", ""),
            status=claim_data.get("status", "unverified"),
            claim_type=claim_data.get("claim_type", "fact"),
            needs_primary=bool(claim_data.get("needs_primary", False)),
            status_reason=str(claim_data.get("status_reason", "") or ""),
            support_count=int(claim_data.get("support_count", 0) or 0),
            contradict_count=int(claim_data.get("contradict_count", 0) or 0),
            source_tier_counts={
                **_empty_tier_counts(),
                **(claim_data.get("source_tier_counts", {}) or {}),
            },
            sources=list(claim_data.get("source_urls", []) or []),
        )


class SourceMetrics(BaseModel):
    """Aggregate quality breakdown of all sources used in the run.

    Computed by the active ``SourceTieringStrategy`` over the union of
    all citations contributed by the search node. Surfaced as the
    ``sources`` slice of :class:`ResearchMetrics`.
    """

    tier_counts: dict[str, int] = Field(
        default_factory=_empty_tier_counts,
        description=(
            "Per-tier count of distinct source URLs used in the run. "
            "Always contains all five tier keys for stable downstream "
            "consumption (zero for absent tiers)."
        ),
    )
    """Per-tier count of distinct source URLs used in the run. Always contains all five tier keys for stable downstream consumption (zero for absent tiers)."""
    quality_score: float = Field(
        0.0,
        description=(
            "Weighted source-quality score in ``[0.0, 1.0]``. Computed "
            "as the tier-weighted mean over all sources, where "
            "``primary=1.0``, ``mainstream=0.8``, ``stakeholder=0.45``, "
            "``unknown=0.35``, ``low=0.1`` (default weights). Higher is "
            "better. ``0.0`` means all sources are low-tier or unknown; "
            "``1.0`` means all primary."
        ),
    )
    """Weighted source-quality score in ``[0.0, 1.0]``. Computed as the tier-weighted mean over all sources, where ``primary=1.0``, ``mainstream=0.8``, ``stakeholder=0.45``, ``unknown=0.35``, ``low=0.1`` (default weights). Higher is better. ``0.0`` means all sources are low-tier or unknown; ``1.0`` means all primary."""


class ClaimMetrics(BaseModel):
    """Aggregate quality breakdown of all consolidated claims.

    Computed by the active ``ClaimConsolidationStrategy`` after the
    final round. Surfaced as the ``claims`` slice of
    :class:`ResearchMetrics`.
    """

    status_counts: dict[str, int] = Field(
        default_factory=lambda: {"verified": 0, "attributed": 0, "contested": 0, "unverified": 0},
        description=(
            "Per-status count of consolidated claims. Always contains "
            "the four status keys (``verified``, ``attributed``, "
            "``contested``, ``unverified``) for stable downstream "
            "consumption; results stored before 0.2.0.10 carry three."
        ),
    )
    """Per-status count of consolidated claims. Always contains the four status keys (``verified``, ``attributed``, ``contested``, ``unverified``) for stable downstream consumption; results stored before 0.2.0.10 carry three."""
    quality_score: float = Field(
        0.0,
        description=(
            "Weighted claim-quality score in ``[0.0, 1.0]``: the mean over "
            "all consolidated claims of 1.0 per supported (verified or "
            "attributed) claim with a cross-checked structure, 0.9 with a "
            "primary source, 0.7 with one quality source, 0.5 per "
            "contested and 0.0 per unverified claim. It measures the "
            "support structure; the status counts say what was checked."
        ),
    )
    """Weighted claim-quality score in ``[0.0, 1.0]``: the mean over all consolidated claims of 1.0 per supported (verified or attributed) claim with a cross-checked structure, 0.9 with a primary source, 0.7 with one quality source, 0.5 per contested and 0.0 per unverified claim. It measures the support structure; the status counts say what was checked."""


class ResearchMetrics(BaseModel):
    """Aggregate metrics for a single research run.

    All numeric fields default to ``0`` so that early-failed or
    minimal runs still deserialize cleanly.
    """

    rounds: int = Field(
        0,
        description=(
            "Total number of completed search rounds. ``0`` means the "
            "run never reached the search node (e.g. classify failure). "
            "Bounded above by ``AgentConfig.max_rounds``."
        ),
    )
    """Total number of completed search rounds. ``0`` means the run never reached the search node (e.g. classify failure). Bounded above by ``AgentConfig.max_rounds``."""
    elapsed_seconds: float = Field(
        0.0,
        description=(
            "Wall-clock duration of the run, in seconds, with two-"
            "decimal precision. Filled in by ``ResearchAgent.research`` "
            "after ``ResearchResult.from_raw`` returns; pure ``from_raw`` "
            "calls leave this at ``0.0``."
        ),
    )
    """Wall-clock duration of the run, in seconds, with two-decimal precision. Filled in by ``ResearchAgent.research`` after ``ResearchResult.from_raw`` returns; pure ``from_raw`` calls leave this at ``0.0``."""
    total_queries: int = Field(
        0,
        description=(
            "Total number of search queries dispatched across all "
            "rounds. Includes deduplicated queries; cache hits do not "
            "count as separate dispatches."
        ),
    )
    """Total number of search queries dispatched across all rounds. Includes deduplicated queries; cache hits do not count as separate dispatches."""
    total_citations: int = Field(
        0,
        description=(
            "Total number of distinct cited URLs across all rounds. "
            "Counts unique URLs only; duplicate citations from "
            "different searches collapse to one."
        ),
    )
    """Total number of distinct cited URLs across all rounds. Counts unique URLs only; duplicate citations from different searches collapse to one."""
    confidence: int = Field(
        0,
        description=(
            "Final evaluator confidence in ``[0, 10]``. ``0`` means no "
            "evaluator pass ran (early failure). Compared against "
            "``AgentConfig.confidence_stop`` to decide whether the loop "
            "stopped on confidence."
        ),
    )
    """Final evaluator confidence in ``[0, 10]``. ``0`` means no evaluator pass ran (early failure). Compared against ``AgentConfig.confidence_stop`` to decide whether the loop stopped on confidence."""
    aspect_coverage: float = Field(
        0.0,
        description=(
            "Fraction of required aspects covered by the final answer, "
            "in ``[0.0, 1.0]``: the share of the classify sub-questions the "
            "evaluator marked as answered in its last round; ``1.0`` when "
            "the run had no sub-questions, ``0.0`` when none was covered "
            "or no evaluation ran."
        ),
    )
    """Fraction of the classify sub-questions the evaluator marked as answered in its last round, in ``[0.0, 1.0]``; ``1.0`` when the run had no sub-questions, ``0.0`` when none was covered or no evaluation ran."""
    evidence_consistency: int = Field(
        0,
        description=(
            "Evaluator-assigned consistency score in ``[0, 10]`` for "
            "the evidence pool. Higher means fewer cross-source "
            "contradictions. ``0`` indicates the score was not parsed "
            "from the evaluator response (logged as "
            "``_evidence_consistency_parsed`` fallback)."
        ),
    )
    """Evaluator-assigned consistency score in ``[0, 10]`` for the evidence pool. Higher means fewer cross-source contradictions. ``0`` indicates the score was not parsed from the evaluator response (logged as ``_evidence_consistency_parsed`` fallback)."""
    evidence_sufficiency: int = Field(
        0,
        description=(
            "Evaluator-assigned sufficiency score in ``[0, 10]`` for "
            "answering the question with the available evidence. ``0`` "
            "indicates the score was not parsed from the evaluator "
            "response."
        ),
    )
    """Evaluator-assigned sufficiency score in ``[0, 10]`` for answering the question with the available evidence. ``0`` indicates the score was not parsed from the evaluator response."""
    sources: SourceMetrics = Field(
        default_factory=SourceMetrics,
        description=(
            "Aggregate source-tier breakdown and quality score for the "
            "run. See :class:`SourceMetrics`."
        ),
    )
    """Aggregate source-tier breakdown and quality score for the run. See :class:`SourceMetrics`."""
    claims: ClaimMetrics = Field(
        default_factory=ClaimMetrics,
        description=(
            "Aggregate claim-status breakdown and quality score for the "
            "run. See :class:`ClaimMetrics`."
        ),
    )
    """Aggregate claim-status breakdown and quality score for the run. See :class:`ClaimMetrics`."""
    answer_bound_claims_count: int = Field(
        0,
        description=(
            "Number of final-answer claim-level bindings that matched a "
            "consolidated claim -- a cited answer sentence that plausibly "
            "carries the claim. This is the same claim-grounded signal the "
            "evidence contract uses; coarser URL-level matches are tracked "
            "separately (matched_evidence_binding_count) and do not inflate "
            "this count. Higher means more answer text is claim-traceable."
        ),
    )
    """Number of final-answer claim-level bindings that matched a consolidated claim -- a cited answer sentence that plausibly carries the claim. This is the same claim-grounded signal the evidence contract uses; coarser URL-level matches are tracked separately (matched_evidence_binding_count) and do not inflate this count. Higher means more answer text is claim-traceable."""
    unbound_answer_citations_count: int = Field(
        0,
        description=(
            "Number of answer-side citations that resolved to no "
            "EvidenceRecord (``unknown_citation`` bindings). Non-zero "
            "values mean the answer cited a URL the evidence base cannot "
            "substantiate."
        ),
    )
    """Number of answer-side citations that resolved to no EvidenceRecord (``unknown_citation`` bindings). Non-zero values mean the answer cited a URL the evidence base cannot substantiate."""
    verified_claims_used_count: int = Field(
        0,
        description=(
            "Number of verified consolidated claims (a source's own text "
            "was checked) marked as used in the final answer by the "
            "deterministic binding audit. Claims the search provider only "
            "attributes are not counted, so a run on a provider that "
            "returns no source text reports 0."
        ),
    )
    """Number of verified consolidated claims (a source's own text was checked) marked as used in the final answer by the deterministic binding audit. Claims the search provider only attributes are not counted, so a run on a provider that returns no source text reports 0."""
    evidence_contract_status: str = Field(
        "unknown",
        description=(
            "Compact status of the evidence-to-answer contract, decided by "
            "the claim-level binding of cited answer sentences. ``clean``: "
            "at least one cited sentence carries a consolidated claim and "
            "nothing cited is unsubstantiated. ``needs_review``: a claim is "
            "carried, but some citation is unsubstantiated (a URL with no "
            "record, or a record with no claim). ``source_context_only``: "
            "sources were cited but no sentence carries a consolidated "
            "claim. ``algorithm_failed``: report synthesis was blocked. "
            "``unknown``: no audit ran. ``algorithm_failed`` (cap 3), "
            "``source_context_only`` (cap 4) and ``needs_review`` (cap 6) "
            "each cap final_confidence; ``clean`` and ``unknown`` do not."
        ),
    )
    """Compact status of the evidence-to-answer contract, decided by the claim-level binding of cited answer sentences. ``clean``: at least one cited sentence carries a consolidated claim and nothing cited is unsubstantiated. ``needs_review``: a claim is carried, but some citation is unsubstantiated (a URL with no record, or a record with no claim). ``source_context_only``: sources were cited but no sentence carries a consolidated claim. ``algorithm_failed``: report synthesis was blocked. ``unknown``: no audit ran. ``algorithm_failed`` (cap 3), ``source_context_only`` (cap 4) and ``needs_review`` (cap 6) each cap final_confidence; ``clean`` and ``unknown`` do not."""
    single_source_ratio: float = Field(
        0.0,
        description=(
            "Share of supported claims (verified or attributed) resting on "
            "a single source while the evidence-depth gap was active; 0.0 "
            "otherwise."
        ),
    )
    """Share of supported claims (verified or attributed) resting on a single source while the evidence-depth gap was active; 0.0 otherwise."""
    rendered_evidence_record_count: int = Field(
        0,
        description=(
            "Report-eligible evidence records the writer actually saw in "
            "the answer prompt's evidence overview."
        ),
    )
    """Report-eligible evidence records the writer actually saw in the answer prompt's evidence overview."""
    omitted_evidence_record_count: int = Field(
        0,
        description=(
            "Report-eligible evidence records that did not fit the "
            "profile's evidence budget and were not evaluated in the "
            "report. Non-zero means the report is a partial reading of "
            "the evidence; a parent run carries it as an open point."
        ),
    )
    """Report-eligible evidence records that did not fit the profile's evidence budget and were not evaluated in the report. Non-zero means the report is a partial reading of the evidence; a parent run carries it as an open point."""
    omitted_evidence_query_count: int = Field(
        0,
        description=(
            "Number of searches that lost at least one record to the "
            "evidence budget."
        ),
    )
    """Number of searches that lost at least one record to the evidence budget."""
    prompt_tokens: int = Field(
        0,
        description=(
            "Total prompt-token usage across all LLM calls in the run, "
            "summed from per-call ``usage.prompt_tokens`` returned by "
            "the providers. ``0`` when no provider returned token counts."
        ),
    )
    """Total prompt-token usage across all LLM calls in the run, summed from per-call ``usage.prompt_tokens`` returned by the providers. ``0`` when no provider returned token counts."""
    completion_tokens: int = Field(
        0,
        description=(
            "Total completion-token usage across all LLM calls in the "
            "run, summed from per-call ``usage.completion_tokens``. "
            "``0`` when no provider returned token counts."
        ),
    )
    """Total completion-token usage across all LLM calls in the run, summed from per-call ``usage.completion_tokens``. ``0`` when no provider returned token counts."""


class ResearchResultExportOptions(BaseModel):
    """Optional projection settings for serialised result payloads.

    All fields are optional switches or limits so downstream surfaces
    (parity, HTTP responses, custom tooling) can select the public
    result view they need without introducing parallel result models.
    The defaults emit the full payload — set fields to opt out.
    """

    include_answer: bool = Field(
        True,
        description=(
            "When ``True``, include the markdown answer text in the "
            "exported payload. Set ``False`` for metrics-only views "
            "where the answer is shipped through a separate channel."
        ),
    )
    """When ``True``, include the markdown answer text in the exported payload. Set ``False`` for metrics-only views where the answer is shipped through a separate channel."""
    include_metrics: bool = Field(
        True,
        description=(
            "When ``True``, include the full :class:`ResearchMetrics` "
            "object as a nested dict. Set ``False`` for minimal "
            "answer-only payloads."
        ),
    )
    """When ``True``, include the full :class:`ResearchMetrics` object as a nested dict. Set ``False`` for minimal answer-only payloads."""
    include_sources: bool = Field(
        True,
        description=(
            "When ``True``, include the ``top_sources`` list. Combine "
            "with ``max_sources`` to cap the list length."
        ),
    )
    """When ``True``, include the ``top_sources`` list. Combine with ``max_sources`` to cap the list length."""
    include_references: bool = Field(
        True,
        description=(
            "When ``True``, include the exact report-reference list from "
            "the markdown ``Referenzen`` appendix. Combine with "
            "``max_references`` to cap only this exported projection."
        ),
    )
    """When ``True``, include the exact report-reference list from the markdown ``Referenzen`` appendix. Combine with ``max_references`` to cap only this exported projection."""
    include_claims: bool = Field(
        True,
        description=(
            "When ``True``, include the ``top_claims`` list. Combine "
            "with ``max_claims`` to cap the list length."
        ),
    )
    """When ``True``, include the ``top_claims`` list. Combine with ``max_claims`` to cap the list length."""
    include_evidence_bundle: bool = Field(
        True,
        description=(
            "Include the versioned, lossless evidence manifest and its "
            "structured child-to-parent outcome when available."
        ),
    )
    max_sources: int | None = Field(
        None,
        description=(
            "Optional hard cap on the exported ``top_sources`` list. "
            "``None`` (default) keeps the full list as produced by the "
            "agent. Use a positive integer to truncate for compact "
            "downstream payloads."
        ),
    )
    """Optional hard cap on the exported ``top_sources`` list. ``None`` (default) keeps the full list as produced by the agent. Use a positive integer to truncate for compact downstream payloads."""
    max_references: int | None = Field(
        None,
        description=(
            "Optional hard cap on the exported ``references`` list. "
            "``None`` (default) keeps every report reference produced by "
            "the answer node, even when ``top_sources`` is capped."
        ),
    )
    """Optional hard cap on the exported ``references`` list. ``None`` (default) keeps every report reference produced by the answer node, even when ``top_sources`` is capped."""
    max_claims: int | None = Field(
        None,
        description=(
            "Optional hard cap on the exported ``top_claims`` list. "
            "``None`` (default) keeps the full list. Use a positive "
            "integer to truncate."
        ),
    )
    """Optional hard cap on the exported ``top_claims`` list. ``None`` (default) keeps the full list. Use a positive integer to truncate."""


class ResearchResult(BaseModel):
    """Complete result of a :meth:`ResearchAgent.research` call.

    The result is fully self-contained: serialise it via
    :meth:`pydantic.BaseModel.model_dump_json` for storage, or via
    :meth:`to_export_payload` for a configurable public view.
    """

    answer: str = Field(
        ...,
        description=(
            "Final markdown-formatted answer text including inline "
            "citation markers. Empty string when the run failed before "
            "the answer node ran."
        ),
    )
    """Final markdown-formatted answer text including inline citation markers. Empty string when the run failed before the answer node ran."""
    metrics: ResearchMetrics = Field(
        default_factory=ResearchMetrics,
        description=(
            "Aggregated quality and performance metrics for the run. "
            "See :class:`ResearchMetrics` for field-level semantics."
        ),
    )
    """Aggregated quality and performance metrics for the run. See :class:`ResearchMetrics` for field-level semantics."""
    top_sources: list[Source] = Field(
        default_factory=list,
        description=(
            "Most relevant sources, ordered by answer-linked URLs first, "
            "then prompt-selected evidence URLs, then discovered citations. "
            "Capped at 60 items by :meth:`from_raw` to bound payload "
            "size; tighten further via "
            ":class:`ResearchResultExportOptions.max_sources`."
        ),
    )
    """Most relevant sources, ordered by answer-linked URLs first, then prompt-selected evidence URLs, then discovered citations. Capped at 60 items by :meth:`from_raw` to bound payload size; tighten further via :class:`ResearchResultExportOptions.max_sources`."""
    references: list[ReportReference] = Field(
        default_factory=list,
        description=(
            "Exact source list rendered in the final report's ``Referenzen`` "
            "appendix, in display order and not capped by "
            ":meth:`from_raw`. UI clients should use this for evidence tabs "
            "that need to match the report text."
        ),
    )
    """Exact source list rendered in the final report's ``Referenzen`` appendix, in display order and not capped by :meth:`from_raw`. UI clients should use this for evidence tabs that need to match the report text."""
    top_claims: list[Claim] = Field(
        default_factory=list,
        description=(
            "Key consolidated claims with their verification metadata. "
            "Capped at 30 items by :meth:`from_raw`; tighten further "
            "via :class:`ResearchResultExportOptions.max_claims`."
        ),
    )
    """Key consolidated claims with their verification metadata. Capped at 30 items by :meth:`from_raw`; tighten further via :class:`ResearchResultExportOptions.max_claims`."""
    execution: AgentExecution | None = Field(
        None,
        description=(
            "Effective Agent Desk execution state. Present only for agent "
            "algorithms; legacy research/direct results omit it."
        ),
    )
    """Effective Agent Desk execution state; ``None`` for non-agent and legacy results."""
    knowledge: KnowledgeResultState | None = Field(
        None,
        description=(
            "Text-free Knowledge profile, gate, grounding and retrieval "
            "receipt. Exported through the established flat Knowledge keys."
        ),
    )
    """Typed Knowledge execution receipt; ``None`` for non-Knowledge and legacy results."""
    work_result: dict[str, Any] | None = Field(
        None,
        description=(
            "Typed outcome of an internal role run: profile, status, the "
            "validated value and the sources read before it was written. "
            "Present only for runs with a work profile, which publish no "
            "answer."
        ),
    )
    """Typed outcome of an internal role run; ``None`` for every ordinary run."""
    web_search_ledger: dict[str, Any] | None = Field(
        None,
        description=(
            "Read-only audit projection of provider search requests, provider "
            "answers and citations. Linked pages are not fetched by Inqtrix."
        ),
    )

    def to_export_payload(
        self,
        options: ResearchResultExportOptions | None = None,
    ) -> dict[str, Any]:
        """Build a configurable public payload from the typed result model.

        Use this to serialise the result to JSON-friendly dicts while
        opting selected sections in or out (e.g. metrics-only view for
        dashboards, or answer + sources without claim ledger).

        Args:
            options: Projection settings controlling which top-level
                keys are emitted and how the lists are capped. ``None``
                (default) uses :class:`ResearchResultExportOptions` with
                its defaults, which emits the full payload.

        Returns:
            A new ``dict`` with the selected top-level keys
            (``answer``, ``metrics``, ``top_sources``, ``references``,
            ``top_claims`` and, where applicable, ``execution`` plus the
            flat, text-free ``knowledge_*`` receipt), each value already
            converted from the Pydantic model via
            :meth:`pydantic.BaseModel.model_dump`.
            Caller may mutate freely — the dict is independent of the
            source model.

        Example:
            >>> result.to_export_payload(
            ...     ResearchResultExportOptions(
            ...         include_sources=False,
            ...         max_claims=5,
            ...     )
            ... )
            {'answer': '...', 'metrics': {...}, 'top_claims': [...]}
        """
        export_options = options or ResearchResultExportOptions()
        payload: dict[str, Any] = {}

        if export_options.include_answer:
            payload["answer"] = self.answer

        if export_options.include_metrics:
            payload["metrics"] = self.metrics.model_dump()

        if export_options.include_sources:
            payload["top_sources"] = [
                source.model_dump()
                for source in _limit_items(self.top_sources, export_options.max_sources)
            ]

        if export_options.include_references:
            payload["references"] = [
                reference.model_dump()
                for reference in _limit_items(self.references, export_options.max_references)
            ]

        if export_options.include_claims:
            payload["top_claims"] = [
                claim.model_dump()
                for claim in _limit_items(self.top_claims, export_options.max_claims)
            ]

        if (
            export_options.include_evidence_bundle
            and self.web_search_ledger is not None
        ):
            payload["web_search_ledger"] = dict(self.web_search_ledger)

        if self.execution is not None:
            payload["execution"] = self.execution.model_dump()

        if self.work_result is not None:
            payload["work_result"] = dict(self.work_result)

        if self.knowledge is not None:
            payload.update(self.knowledge.to_export_fields())

        return payload

    @classmethod
    def from_raw(
        cls, raw: dict, *, source_tiering: Any = None
    ) -> ResearchResult:
        """Build a :class:`ResearchResult` from the raw ``graph.run()`` dict.

        This is the bridge between the internal state-dict world and the
        typed public API. Callers other than ``ResearchAgent`` rarely
        need this directly — it is exposed for parity tooling and tests
        that consume the raw graph output.

        Args:
            raw: Result dict produced by :func:`inqtrix.graph.run`. Must
                contain at least ``result_state`` and ``answer`` keys;
                other fields are tolerated as missing and filled with
                neutral defaults. ``elapsed_seconds`` is always set to
                ``0.0`` here and must be filled by the caller after
                measuring the wall-clock duration.
            source_tiering: The run's configured tiering strategy, used
                only for a citation the run's source registry does not
                cover (legacy states). ``None`` falls back to the default
                structural floor.

        Returns:
            A populated :class:`ResearchResult`. ``top_sources`` is
            capped at 60 entries and prioritizes URLs actually linked in
            the final answer, then prompt-selected evidence URLs, then
            the remaining discovered citations. Source tiers come from
            the run's normalized source records when available and only
            fall back to default tiering for legacy result states.
        """
        result_state: dict = raw.get("result_state", {})
        usage, _usage_scope = reported_usage(raw, result_state)

        # -- sources --
        if source_tiering is None:
            from inqtrix.strategies import DefaultSourceTiering

            source_tiering = DefaultSourceTiering()
        tiering = source_tiering
        all_urls: list[str] = result_state.get("all_citations", [])
        # Order: URLs actually linked in the answer first, then the visible
        # EvidenceOverview allowlist, then remaining report-eligible
        # EvidenceLedger URLs and discovered citations.
        ordered_urls: list[str] = []
        for url in _extract_used_answer_urls(raw.get("answer", "")):
            _append_unique_url(ordered_urls, url)
        for url in result_state.get("allowed_citations", []) or []:
            _append_unique_url(ordered_urls, url)
        for record in result_state.get("evidence_ledger", []) or []:
            if not record.get("report_eligible"):
                continue
            _append_unique_url(ordered_urls, record.get("canonical_url", ""))
            for citation in record.get("citation_set", []) or []:
                if isinstance(citation, dict):
                    _append_unique_url(ordered_urls, citation.get("url", ""))
        for url in all_urls:
            _append_unique_url(ordered_urls, url)
        tier_by_url = pinned_tiers_from_records(result_state.get("source_records", {}) or {})
        top_sources = [
            Source(url=u, tier=tier_by_url.get(u) or tiering.tier_for_url(u))
            for u in ordered_urls[:60]
        ]
        web_search_ledger = build_web_search_ledger(
            run_id=str(result_state.get("_run_id", "") or "legacy_research"),
            query_records=[
                dict(record)
                for record in result_state.get("query_records", []) or []
                if isinstance(record, dict)
            ],
            query_synthesis={
                str(key): dict(value)
                for key, value in (
                    (result_state.get("query_synthesis") or {}).items()
                    if isinstance(
                        result_state.get("query_synthesis"), Mapping
                    )
                    else ()
                )
                if isinstance(value, dict)
            },
            citation_records=[
                dict(record)
                for record in (
                    result_state.get("provider_citation_records", []) or []
                )
                if isinstance(record, dict)
            ],
        )
        raw_references = [
            dict(reference)
            for reference in (
                result_state.get("report_references")
                or result_state.get("references", [])
                or []
            )
            if isinstance(reference, dict)
        ]
        references = _report_references_from_state(
            attach_web_search_lineage(raw_references, web_search_ledger),
            tier_by_url=tier_by_url,
            tiering=tiering,
        )

        # -- claims --
        consolidated: list[dict] = result_state.get("consolidated_claims", [])
        top_claims = [
            Claim.from_consolidated(c)
            for c in consolidated[:30]
        ]

        # -- metrics --
        tier_counts = result_state.get("source_tier_counts", {})
        claim_counts = result_state.get("claim_status_counts", {})
        answer_claim_bindings = result_state.get("answer_claim_bindings", []) or []
        answer_evidence_bindings = result_state.get("answer_evidence_bindings", []) or []
        # Count only claim-level matches -- the same claim-grounded signal that
        # decides the evidence contract (a cited sentence that plausibly carries
        # a consolidated claim). The coarser URL-level answer_evidence_bindings
        # feed the separate diagnostics below (unbound_answer_citations_count);
        # mixing their "matched" rows in let a source_context_only report still
        # report bound_claims > 0, overstating the same way the contract does not.
        bound_claims = [
            binding
            for binding in answer_claim_bindings
            if binding.get("binding_status") == "matched"
        ]
        # "unknown_citation" = a cited answer URL with no backing
        # EvidenceRecord, the only unbound binding status there is.
        unbound_bindings = [
            binding
            for binding in answer_evidence_bindings
            if binding.get("binding_status") == "unknown_citation"
        ]
        verified_claims_used_count = sum(
            1
            for claim in claim_assessments(result_state)
            if claim.get("status") == "verified" and claim.get("used_in_answer")
        )
        # Read the canonical contract the answer node already computed and stored;
        # do not recompute it here (single source of truth).
        evidence_contract_status = str(
            result_state.get("evidence_contract_status", "unknown")
        )
        depth_gap = result_state.get("evidence_depth_gap")
        depth_gap = depth_gap if isinstance(depth_gap, dict) else {}
        single_source_ratio = (
            float(depth_gap.get("single_source_ratio") or 0.0)
            if depth_gap.get("active")
            else 0.0
        )
        metrics = ResearchMetrics(
            rounds=result_state.get("round", 0),
            elapsed_seconds=0.0,  # filled by caller
            total_queries=len(result_state.get("queries", [])),
            total_citations=len(all_urls),
            confidence=result_state.get("final_confidence", 0),
            aspect_coverage=result_state.get("aspect_coverage", 0.0),
            evidence_consistency=result_state.get("evidence_consistency", 0),
            evidence_sufficiency=result_state.get("evidence_sufficiency", 0),
            sources=SourceMetrics(
                tier_counts=tier_counts,
                quality_score=result_state.get("source_quality_score", 0.0),
            ),
            claims=ClaimMetrics(
                status_counts=claim_counts,
                quality_score=result_state.get("claim_quality_score", 0.0),
            ),
            answer_bound_claims_count=len(bound_claims),
            unbound_answer_citations_count=len(unbound_bindings),
            verified_claims_used_count=verified_claims_used_count,
            evidence_contract_status=evidence_contract_status,
            single_source_ratio=single_source_ratio,
            rendered_evidence_record_count=int(
                result_state.get("rendered_evidence_record_count", 0) or 0
            ),
            omitted_evidence_record_count=int(
                result_state.get("omitted_evidence_record_count", 0) or 0
            ),
            omitted_evidence_query_count=len(
                result_state.get("omitted_evidence_by_query", []) or []
            ),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        if not web_search_ledger.get("searches"):
            web_search_ledger = None

        return cls(
            answer=raw.get("answer", ""),
            metrics=metrics,
            top_sources=top_sources,
            references=references,
            top_claims=top_claims,
            execution=(
                AgentExecution.model_validate(result_state["execution"])
                if isinstance(result_state.get("execution"), dict)
                else None
            ),
            knowledge=KnowledgeResultState.from_sources(result_state, raw),
            work_result=(
                dict(result_state["work_result"])
                if isinstance(result_state.get("work_result"), dict)
                else None
            ),
            web_search_ledger=web_search_ledger,
        )


def merge_knowledge_result_payload(
    result: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the safe Knowledge receipt into one completed result response.

    Stored results created by the current run service already carry these
    fields.  The snapshot merge is an additive recovery seam for imported or
    rolling-upgrade rows whose presentation result predates the projection.
    Conflicting known metadata raises instead of choosing one source silently;
    old rows without Knowledge metadata remain byte-compatible.
    """

    payload = dict(result)
    knowledge = KnowledgeResultState.from_sources(snapshot, result)
    if knowledge is not None:
        payload.update(knowledge.to_export_fields())
    return payload
