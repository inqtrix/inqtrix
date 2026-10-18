"""Pluggable strategy ABCs and their default implementations.

Each strategy encapsulates a single algorithmic concern that nodes can
depend on through the ``StrategyContext`` dataclass.  Default
implementations reproduce the exact behaviour of the original monolithic
``_original_agent.py`` so existing node call-sites remain compatible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from inqtrix.constants import CLAIM_EXTRACT_TIMEOUT
from inqtrix.strategies._source_tiering import DefaultSourceTiering, SourceTieringStrategy
from inqtrix.strategies._claim_extraction import (
    ClaimExtractionStrategy,
    LLMClaimExtractor,
    ProviderCitationRef,
)
from inqtrix.strategies._claim_consolidation import ClaimConsolidationStrategy, DefaultClaimConsolidator
from inqtrix.strategies._risk_scoring import KeywordRiskScorer, RiskScoringStrategy
from inqtrix.strategies._stop_criteria import MultiSignalStopCriteria, StopCriteriaStrategy

if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider
    from inqtrix.settings import AgentSettings

_log = logging.getLogger("inqtrix")


def resolve_claim_extract_model(
    llm: "LLMProvider | None",
    fallback: str = "",
) -> str:
    """Resolve the claim-extraction model name from the LLM provider.

    The default claim extractor must use the model name that the LLM
    provider was constructed with, not a globally configured
    ``Settings.models`` value. The two diverge whenever a webserver-stack
    example builds a ``ProviderContext`` explicitly while the ``Settings``
    block is left at its LiteLLM-flavoured defaults.

    Resolution order:

    1. ``llm.models.effective_claim_extract_model`` when the provider exposes
       a ``models`` attribute and that attribute carries a non-empty
       ``effective_claim_extract_model``.
    2. ``llm.models.reasoning_model`` when the provider has ``models``
       but no explicit claim-extraction model.
    3. ``fallback`` otherwise; emits a ``log.warning`` so the operator
       sees the bypassed constructor configuration rather than a
       silent drift.

    Args:
        llm: The LLM provider that the strategy will dispatch through.
            ``None`` short-circuits to the fallback path (and warning if
            ``fallback`` is non-empty).
        fallback: Last-resort model name (typically
            ``settings.models.effective_claim_extract_model``). Used only
            when neither resolution step above succeeds.

    Returns:
        The resolved model name; may be empty when both the provider
        and the fallback have nothing to offer.
    """
    if llm is None:
        if fallback:
            _log.warning(
                "resolve_claim_extract_model: llm=None; falling back to settings.models"
                " for claim_extract_model=%r. Constructor-First prefers a provider with"
                " a models attribute.",
                fallback,
            )
        return fallback
    provider_models = getattr(llm, "models", None)
    if provider_models is not None:
        resolved = (
            getattr(provider_models, "effective_claim_extract_model", "")
            or getattr(provider_models, "reasoning_model", "")
        )
        if resolved:
            return resolved
    if fallback:
        _log.warning(
            "resolve_claim_extract_model: provider %s has no resolvable "
            "models.effective_claim_extract_model; falling back to settings.models for "
            "claim_extract_model=%r. Constructor-First requires provider-owned "
            "configuration; consider adding a models property to the provider.",
            type(llm).__name__,
            fallback,
        )
    return fallback


@dataclass
class StrategyContext:
    """Bundle of all pluggable strategies available to runtime nodes.

    The graph injects a single :class:`StrategyContext` into every node so
    orchestration code can stay agnostic to the concrete implementation of
    source tiering, claim extraction, consolidation, risk scoring, and
    stopping heuristics.

    Attributes:
        source_tiering: URL-to-quality classification and aggregate source
            scoring.
        claim_extraction: Structured claim extraction from raw search text.
        claim_consolidation: Signature grouping, status derivation, and
            answer-facing claim selection.
        risk_scoring: Deterministic risk and aspect heuristics used across
            classify, plan, and search.
        stop_criteria: Multi-signal sufficiency and stop heuristics for the
            evaluate node.
    """

    source_tiering: SourceTieringStrategy
    claim_extraction: ClaimExtractionStrategy
    claim_consolidation: ClaimConsolidationStrategy
    risk_scoring: RiskScoringStrategy
    stop_criteria: StopCriteriaStrategy


def create_default_strategies(
    settings: "AgentSettings",
    *,
    llm: "LLMProvider | None" = None,
    claim_extract_model: str = "",
    claim_extract_timeout: int = CLAIM_EXTRACT_TIMEOUT,
) -> StrategyContext:
    """Create the default strategy bundle used by :class:`ResearchAgent`.

    Args:
        settings: Agent-level thresholds and loop settings used by the stop
            criteria strategy.
        llm: Optional LLM provider used by the default claim extractor.
            Passing ``None`` keeps claim extraction available but inert.
        claim_extract_model: Model identifier used for claim extraction calls.
        claim_extract_timeout: Per-call timeout in seconds for claim extraction.

    Returns:
        A fully wired :class:`StrategyContext` with the default strategy
        implementations.

    Notes:
        A single :class:`DefaultSourceTiering` instance is shared between
        ``source_tiering`` and ``claim_consolidation`` so both layers classify
        source quality with the same domain rules.
    """
    tiering = DefaultSourceTiering()
    return StrategyContext(
        source_tiering=tiering,
        claim_extraction=LLMClaimExtractor(
            llm=llm,
            claim_extract_model=claim_extract_model,
            claim_extract_timeout=claim_extract_timeout,
        ),
        claim_consolidation=DefaultClaimConsolidator(source_tiering=tiering),
        risk_scoring=KeywordRiskScorer(),
        stop_criteria=MultiSignalStopCriteria(settings=settings),
    )


__all__ = [
    # ABCs
    "SourceTieringStrategy",
    "ClaimExtractionStrategy",
    "ProviderCitationRef",
    "ClaimConsolidationStrategy",
    "RiskScoringStrategy",
    "StopCriteriaStrategy",
    # Defaults
    "DefaultSourceTiering",
    "LLMClaimExtractor",
    "DefaultClaimConsolidator",
    "KeywordRiskScorer",
    "MultiSignalStopCriteria",
    # Container
    "StrategyContext",
    "create_default_strategies",
    "resolve_claim_extract_model",
]
