"""Retrieval profiles: per-request presets over the knowledge pipeline.

A profile bundles the existing pipeline stages (rerank, sufficiency
gate, vocabulary-bridge rewrite, query decomposition, report-style
synthesis) into one user-facing choice, so a request selects ONE name
instead of five switches. Two levels govern what actually runs:

* The **operator ceiling** (:class:`KnowledgeStageCeiling`) is built
  once from settings at wiring time. A stage the ceiling forbids
  (gate disabled via env, no reranker configured) stays off in EVERY
  profile.
* The **profile** selects within the ceiling. Any clamping is visible
  in :attr:`KnowledgeRunPlan.degraded_stages` — never silent.

There is exactly ONE resolution site (:func:`resolve_run_plan`); no
other code interprets profile names. The module is deliberately free
of settings/provider imports so it can be consumed from the algorithm,
the composition root, and the capabilities manifest without cycles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

RERANK_DEPTH_MAX = 200
"""Upper clamp for the per-profile rerank candidate depth.

Mirrors the ``le=200`` bound of the ``rerank_candidate_depth``
settings field — a profile factor must not exceed what the settings
surface itself allows.
"""

EVIDENCE_K_MAX = 40
"""Upper clamp for the per-profile FINAL evidence count (``final_k``).

A profile's ``final_k_factor`` scales the request ``top_k`` into the number
of chunks actually surfaced to the answer; this caps that product so a deep
profile cannot blow past a sane evidence ceiling. The answer-prompt context
budget (``_render_evidence_block``) remains the real backstop on top of this.
"""


class KnowledgeProfile(StrEnum):
    """User-selectable retrieval profile names.

    ``AUTO`` is a meta profile: a zero-cost heuristic picks one of the
    concrete profiles per question (never :attr:`TIEF` — decomposition
    plus multiple retrievals on a heuristic guess would be the exact
    flat tax the heuristic router exists to avoid).
    """

    SCHNELL = "schnell"
    STANDARD = "standard"
    GRUENDLICH = "gruendlich"
    TIEF = "tief"
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class KnowledgeProfileSpec:
    """What a profile REQUESTS, before the operator ceiling applies.

    Attributes:
        rerank: Whether the cross-encoder rerank stage should run.
        rerank_depth_factor: Multiplier on the configured candidate
            depth (clamped to :data:`RERANK_DEPTH_MAX`); deeper pools
            give the reranker more recall to work with.
        final_k_factor: Multiplier on the request ``top_k`` to get the
            FINAL number of evidence chunks surfaced to the answer
            (clamped to :data:`EVIDENCE_K_MAX`). ``1.0`` keeps the
            shared default; the deep profile raises it so its decompose
            + gate fan-out actually WIDENS the cited evidence instead of
            being re-collapsed back to ``top_k``.
        gate: Whether the sufficiency gate runs at all.
        gate_rewrite_rounds: Maximum rewrite-and-retrieve rounds the
            gate may take. ``None`` means "as many as the operator
            ceiling allows" (the deep profile tracks the env cap
            instead of pinning its own number).
        vocabulary_bridge: Use the technical-vocabulary rewrite prompt
            variant when the gate reformulates (the d20 paraphrase
            class). Meaningless while ``gate`` is off.
        decompose: Split multi-aspect questions into sub-queries
            before retrieval (one fast-tier LLM call).
        report: Render the answer as a structured multi-section
            report instead of the compact answer.
    """

    rerank: bool
    rerank_depth_factor: float
    final_k_factor: float
    gate: bool
    gate_rewrite_rounds: int | None
    vocabulary_bridge: bool
    decompose: bool
    report: bool


@dataclass(frozen=True, slots=True)
class KnowledgeStageCeiling:
    """What the OPERATOR allows, derived once from settings at wiring.

    Attributes:
        gate_available: ``INQTRIX_KNOWLEDGE_GATE`` resolved to on.
        grounding_available: ``INQTRIX_KNOWLEDGE_GROUNDING`` on.
        reranker_available: A reranker provider is actually wired.
        gate_max_rounds: Hard cap on gate rewrite rounds for every
            profile (``INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS``).
        rerank_candidate_depth: The configured standard-profile
            candidate depth profiles scale from.
    """

    gate_available: bool
    grounding_available: bool
    reranker_available: bool
    gate_max_rounds: int
    rerank_candidate_depth: int


@dataclass(frozen=True, slots=True)
class KnowledgeRunPlan:
    """The effective per-request plan AFTER ceiling clamping.

    Frozen and held in a local variable of the algorithm's ``run()``
    — the algorithm instance is a shared singleton, so per-request
    state must never live on ``self``.

    Attributes:
        profile: The concrete profile that runs (never ``AUTO``).
        requested_profile: What the request asked for; ``None`` when
            the request named no profile (legacy behaviour).
        auto_selected: Whether the heuristic router picked
            :attr:`profile` on behalf of an ``auto`` request.
        auto_reason: Short machine-readable reason for the heuristic
            choice (telemetry seam for a later LLM escalation);
            empty when not auto-selected.
        rerank: Effective rerank stage state.
        rerank_candidate_depth: Effective candidate-pool depth.
        final_k_factor: Effective multiplier on ``top_k`` for the final
            surfaced-evidence count (the algorithm computes the concrete
            ``final_k`` since it owns the request ``top_k``).
        gate_enabled: Effective gate state.
        gate_rewrite_rounds: Effective rewrite-round budget (0 when
            the gate is off).
        grounding_enabled: Effective quote-then-answer state.
        vocabulary_bridge: Effective rewrite-prompt variant.
        decompose: Effective decomposition stage state.
        report: Effective report-style synthesis state.
        degraded_stages: Stage names the ceiling removed or clamped
            below the profile's request — the visibility contract.
    """

    profile: KnowledgeProfile
    requested_profile: KnowledgeProfile | None
    auto_selected: bool
    auto_reason: str
    rerank: bool
    rerank_candidate_depth: int
    final_k_factor: float
    gate_enabled: bool
    gate_rewrite_rounds: int
    grounding_enabled: bool
    vocabulary_bridge: bool
    decompose: bool
    report: bool
    degraded_stages: tuple[str, ...]


PROFILE_SPECS: dict[KnowledgeProfile, KnowledgeProfileSpec] = {
    KnowledgeProfile.SCHNELL: KnowledgeProfileSpec(
        rerank=False,
        rerank_depth_factor=1.0,
        final_k_factor=1.0,
        gate=False,
        gate_rewrite_rounds=0,
        vocabulary_bridge=False,
        decompose=False,
        report=False,
    ),
    KnowledgeProfile.STANDARD: KnowledgeProfileSpec(
        rerank=True,
        rerank_depth_factor=1.0,
        final_k_factor=1.0,
        gate=True,
        gate_rewrite_rounds=1,
        vocabulary_bridge=False,
        decompose=False,
        report=False,
    ),
    KnowledgeProfile.GRUENDLICH: KnowledgeProfileSpec(
        rerank=True,
        rerank_depth_factor=1.5,
        final_k_factor=1.0,
        gate=True,
        gate_rewrite_rounds=2,
        vocabulary_bridge=True,
        decompose=False,
        report=False,
    ),
    KnowledgeProfile.TIEF: KnowledgeProfileSpec(
        rerank=True,
        rerank_depth_factor=2.0,
        final_k_factor=2.0,
        gate=True,
        gate_rewrite_rounds=None,
        vocabulary_bridge=True,
        decompose=True,
        report=True,
    ),
}
"""The profile matrix.

``STANDARD`` must encode exactly the pre-profile behaviour: rerank as
configured, one gate rewrite, plain answer prompt — a request without
a profile resolves to it and stays byte-stable.
"""


def parse_knowledge_profile(raw: object) -> KnowledgeProfile:
    """Parse a request-supplied profile value.

    Raises:
        ValueError: With the full list of valid identifiers — the
            resolver turns this into the HTTP 400 message, so a typo
            never silently runs a different profile.
    """
    if isinstance(raw, str):
        try:
            return KnowledgeProfile(raw.strip().lower())
        except ValueError:
            pass
    valid = ", ".join(f"'{profile.value}'" for profile in KnowledgeProfile)
    raise ValueError(
        f'knowledge_filters.profile must be one of {valid}'
    )


# Auto-routing heuristics. Deliberately crude and zero-cost (v1): the
# chosen profile and reason are emitted as telemetry, and only if that
# telemetry shows systematic misroutes does an LLM escalation replace
# the unclear branch. Thresholds:
_SHORT_QUESTION_CHARS = 80
"""Below this, a single-clause question is a lookup — `schnell`."""
_LONG_QUESTION_CHARS = 240
"""Above this, the question almost surely carries several aspects."""

_STRONG_ENUMERATION = re.compile(
    r"\b(sowie|jeweils|bzw\.?|au(?:ss|ß)erdem|sowohl"
    r"|vergleich\w*|unterschied\w*)\b",
    re.IGNORECASE,
)
"""Markers that practically always join distinct aspects.

``und`` is deliberately NOT in this set — ordinary German compounds
("Sicherheits- und Risikomanagement") would route nearly every
question to `gruendlich`. Repeated ``und`` is counted separately.
"""

_UND = re.compile(r"\bund\b", re.IGNORECASE)


def choose_auto_profile(question: str) -> tuple[KnowledgeProfile, str]:
    """Pick a concrete profile for an ``auto`` request, with a reason.

    Returns:
        The chosen profile (never ``TIEF``) and a short
        machine-readable reason string that travels into the
        ``profile.resolved`` event — the telemetry on which a later
        LLM-escalation decision will be grounded.
    """
    text = question.strip()
    question_marks = text.count("?")
    if _STRONG_ENUMERATION.search(text):
        return KnowledgeProfile.GRUENDLICH, "strong_enumeration_marker"
    if question_marks >= 2:
        return KnowledgeProfile.GRUENDLICH, "multiple_questions"
    if len(text) > _LONG_QUESTION_CHARS:
        return KnowledgeProfile.GRUENDLICH, "long_question"
    if len(_UND.findall(text)) >= 2:
        return KnowledgeProfile.GRUENDLICH, "repeated_und"
    if len(text) < _SHORT_QUESTION_CHARS and question_marks <= 1:
        return KnowledgeProfile.SCHNELL, "short_simple"
    return KnowledgeProfile.STANDARD, "default"


def resolve_run_plan(
    requested: KnowledgeProfile | None,
    *,
    question: str,
    ceiling: KnowledgeStageCeiling,
) -> KnowledgeRunPlan:
    """Resolve the effective plan: requested profile ∩ operator ceiling.

    The ONE place profile names are interpreted. ``None`` (request
    named no profile) resolves to ``STANDARD``, which encodes the
    pre-profile pipeline exactly.
    """
    auto_selected = False
    auto_reason = ""
    profile = requested or KnowledgeProfile.STANDARD
    if profile is KnowledgeProfile.AUTO:
        profile, auto_reason = choose_auto_profile(question)
        auto_selected = True
    spec = PROFILE_SPECS[profile]

    degraded: list[str] = []

    rerank = spec.rerank and ceiling.reranker_available
    if spec.rerank and not ceiling.reranker_available:
        degraded.append("rerank")
    rerank_depth = min(
        int(ceiling.rerank_candidate_depth * spec.rerank_depth_factor),
        RERANK_DEPTH_MAX,
    )

    gate_enabled = spec.gate and ceiling.gate_available
    if spec.gate and not ceiling.gate_available:
        degraded.append("gate")
    requested_rounds = (
        spec.gate_rewrite_rounds
        if spec.gate_rewrite_rounds is not None
        else ceiling.gate_max_rounds
    )
    gate_rounds = min(requested_rounds, ceiling.gate_max_rounds)
    if gate_enabled and gate_rounds < requested_rounds:
        degraded.append("gate_rounds")
    if not gate_enabled:
        gate_rounds = 0

    grounding = ceiling.grounding_available
    if not grounding:
        # Every profile wants grounding; only the operator can remove
        # it — still listed so the run plan never hides the downgrade.
        degraded.append("grounding")

    vocabulary_bridge = spec.vocabulary_bridge and gate_enabled

    return KnowledgeRunPlan(
        profile=profile,
        requested_profile=requested,
        auto_selected=auto_selected,
        auto_reason=auto_reason,
        rerank=rerank,
        rerank_candidate_depth=rerank_depth,
        final_k_factor=spec.final_k_factor,
        gate_enabled=gate_enabled,
        gate_rewrite_rounds=gate_rounds,
        grounding_enabled=grounding,
        vocabulary_bridge=vocabulary_bridge,
        decompose=spec.decompose,
        report=spec.report,
        degraded_stages=tuple(degraded),
    )


def build_profile_manifest(
    ceiling: KnowledgeStageCeiling,
) -> list[dict[str, object]]:
    """Capability-manifest entries for every selectable profile.

    All concrete profiles are ALWAYS listed; ceiling degradation is
    shown per profile, never hidden — the UI renders what would
    actually run. ``auto`` appears as a delegating entry so pickers
    can offer it without hardcoding its existence.
    """
    entries: list[dict[str, object]] = []
    for profile in (
        KnowledgeProfile.SCHNELL,
        KnowledgeProfile.STANDARD,
        KnowledgeProfile.GRUENDLICH,
        KnowledgeProfile.TIEF,
    ):
        plan = resolve_run_plan(profile, question="", ceiling=ceiling)
        entries.append(
            {
                "id": profile.value,
                # The multiplier on the request ``top_k`` that yields the FINAL
                # surfaced-evidence count, so a client can render the effective
                # ``final_k`` per profile without re-deriving the matrix.
                "final_k_factor": plan.final_k_factor,
                "stages": {
                    "rerank": plan.rerank,
                    "gate_rounds": plan.gate_rewrite_rounds,
                    "grounding": plan.grounding_enabled,
                    "vocabulary_bridge": plan.vocabulary_bridge,
                    "decompose": plan.decompose,
                    "report": plan.report,
                },
                "degraded": list(plan.degraded_stages),
            }
        )
    entries.append(
        {
            "id": KnowledgeProfile.AUTO.value,
            "delegates_to": [
                KnowledgeProfile.SCHNELL.value,
                KnowledgeProfile.STANDARD.value,
                KnowledgeProfile.GRUENDLICH.value,
            ],
        }
    )
    return entries
