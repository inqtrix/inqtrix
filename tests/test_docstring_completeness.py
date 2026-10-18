"""Reflection tests that lock in the docstring coverage of the public surface.

These tests enforce:

- Every public symbol re-exported via ``inqtrix.__all__`` has a non-trivial
  class- or function-level docstring (no ``None``, no whitespace-only
  body).
- Every public method on the high-level ``ResearchAgent`` and on the
  ``LLMProvider`` / ``SearchProvider`` ABCs and concrete implementations
  has a non-trivial docstring.
- Every Pydantic field on the configuration / result models has a
  non-empty ``Field(description=...)``.

The tests are deliberately structural — they assert that text exists,
not what the text says — so refactoring is unrestricted as long as the
documentation surface stays populated.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import BaseModel

import inqtrix
from inqtrix import (
    AgentConfig,
    AnthropicLLM,
    AzureFoundryWebSearch,
    AzureOpenAILLM,
    BedrockLLM,
    Claim,
    ClaimMetrics,
    LiteLLM,
    PerplexitySearch,
    ReportReference,
    ResearchAgent,
    ResearchMetrics,
    ResearchResult,
    ResearchResultExportOptions,
    Source,
    SourceMetrics,
)
from inqtrix.providers.base import (
    ConfiguredLLMProvider,
    LLMProvider,
    SearchProvider,
)
from inqtrix.settings import (
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
)
from inqtrix.strategies import (
    ClaimConsolidationStrategy,
    ClaimExtractionStrategy,
    RiskScoringStrategy,
    SourceTieringStrategy,
    StopCriteriaStrategy,
)
from inqtrix.strategies._claim_consolidation import DefaultClaimConsolidator
from inqtrix.strategies._claim_extraction import LLMClaimExtractor
from inqtrix.strategies._risk_scoring import KeywordRiskScorer
from inqtrix.strategies._source_tiering import DefaultSourceTiering
from inqtrix.strategies._stop_criteria import MultiSignalStopCriteria


def _has_substantive_docstring(obj: Any) -> bool:
    """Return whether ``obj`` has a non-trivial docstring.

    Args:
        obj: Any object whose ``__doc__`` we want to inspect.

    Returns:
        ``True`` when ``inspect.getdoc(obj)`` yields a non-empty string
        with at least 5 characters of meaningful text. ``False``
        otherwise (missing docstring, whitespace-only, or trivial
        single-character text).
    """
    doc = inspect.getdoc(obj)
    if not doc:
        return False
    return len(doc.strip()) >= 5


# ----------------------------------------------------------------------- #
# 1. Public-API symbols from inqtrix.__all__
# ----------------------------------------------------------------------- #

PUBLIC_API_NAMES = list(getattr(inqtrix, "__all__", ()))


@pytest.mark.parametrize("name", PUBLIC_API_NAMES)
def test_public_api_symbol_has_docstring(name: str) -> None:
    """Every entry in ``inqtrix.__all__`` exposes a substantive docstring."""
    obj = getattr(inqtrix, name)
    assert _has_substantive_docstring(obj), (
        f"Public API symbol 'inqtrix.{name}' is missing a substantive "
        f"docstring (got: {inspect.getdoc(obj)!r})."
    )


# ----------------------------------------------------------------------- #
# 2. Pydantic field descriptions
# ----------------------------------------------------------------------- #

PYDANTIC_MODELS_REQUIRING_FIELD_DESCRIPTIONS: list[type[BaseModel]] = [
    AgentConfig,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    ResearchResult,
    ResearchResultExportOptions,
    ReportReference,
    ResearchMetrics,
    Source,
    Claim,
    SourceMetrics,
    ClaimMetrics,
]


@pytest.mark.parametrize(
    "model",
    PYDANTIC_MODELS_REQUIRING_FIELD_DESCRIPTIONS,
    ids=lambda m: m.__name__,
)
def test_pydantic_model_fields_have_descriptions(model: type[BaseModel]) -> None:
    """Every field on a public Pydantic model has a non-empty ``description``."""
    missing: list[str] = []
    for field_name, field_info in model.model_fields.items():
        description = (field_info.description or "").strip()
        if not description:
            missing.append(field_name)
    assert not missing, (
        f"Pydantic model '{model.__name__}' is missing Field(description=...) "
        f"for the following fields: {missing}. Aufgabe-4-Konvention "
        f"verlangt eine semantische Beschreibung pro Feld."
    )


# ----------------------------------------------------------------------- #
# 3. Methods on ResearchAgent + provider ABCs and implementations
# ----------------------------------------------------------------------- #

def _public_methods(cls: type) -> list[tuple[str, Any]]:
    """Return ``(name, attr)`` pairs for non-dunder public methods on ``cls``.

    Args:
        cls: Class to introspect. Includes ``__init__`` because the
            audit treats it as part of the public surface; skips other
            dunders.

    Returns:
        List of ``(method_name, method_object)`` for the public API
        surface that should carry documentation.
    """
    out: list[tuple[str, Any]] = []
    for name, attr in inspect.getmembers(cls):
        if name == "__init__":
            out.append((name, attr))
            continue
        if name.startswith("_"):
            continue
        if callable(attr) or isinstance(attr, property):
            out.append((name, attr))
    return out


METHOD_AUDIT_TARGETS: list[type] = [
    ResearchAgent,
    LLMProvider,
    SearchProvider,
    ConfiguredLLMProvider,
    LiteLLM,
    AnthropicLLM,
    AzureOpenAILLM,
    AzureFoundryWebSearch,
    BedrockLLM,
    PerplexitySearch,
]


@pytest.mark.parametrize("cls", METHOD_AUDIT_TARGETS, ids=lambda c: c.__name__)
def test_class_public_methods_have_docstrings(cls: type) -> None:
    """Every public method (incl. ``__init__``) on key classes is documented."""
    missing: list[str] = []
    for name, attr in _public_methods(cls):
        target = attr.fget if isinstance(attr, property) else attr
        if not _has_substantive_docstring(target):
            missing.append(name)
    assert not missing, (
        f"Class '{cls.__name__}' is missing substantive docstrings for "
        f"the following public members: {missing}."
    )


# ----------------------------------------------------------------------- #
# 4. Strategy ABCs and default implementations
# ----------------------------------------------------------------------- #

STRATEGY_TARGETS: list[type] = [
    SourceTieringStrategy,
    ClaimExtractionStrategy,
    ClaimConsolidationStrategy,
    RiskScoringStrategy,
    StopCriteriaStrategy,
    DefaultSourceTiering,
    LLMClaimExtractor,
    DefaultClaimConsolidator,
    KeywordRiskScorer,
    MultiSignalStopCriteria,
]


@pytest.mark.parametrize("cls", STRATEGY_TARGETS, ids=lambda c: c.__name__)
def test_strategy_class_has_docstring(cls: type) -> None:
    """Every strategy ABC and default implementation has a class docstring."""
    assert _has_substantive_docstring(cls), (
        f"Strategy class '{cls.__name__}' is missing a substantive class "
        f"docstring."
    )


# ----------------------------------------------------------------------- #
# 5. Spot checks for the ten StopCriteria hooks
# ----------------------------------------------------------------------- #

STOP_CRITERIA_HOOKS = [
    "check_contradictions",
    "extract_competing_events",
    "extract_evidence_scores",
    "check_falsification",
    "check_stagnation",
    "compute_utility",
    "check_plateau",
    "should_stop",
]


@pytest.mark.parametrize("hook", STOP_CRITERIA_HOOKS)
def test_stop_criteria_hook_documented_on_abc_and_default(hook: str) -> None:
    """Each of the 10 stop-cascade hooks is documented on ABC and default impl."""
    abc_method = getattr(StopCriteriaStrategy, hook)
    default_method = getattr(MultiSignalStopCriteria, hook)
    assert _has_substantive_docstring(abc_method), (
        f"StopCriteriaStrategy.{hook} (ABC) is missing a docstring."
    )
    assert _has_substantive_docstring(default_method), (
        f"MultiSignalStopCriteria.{hook} (default impl) is missing a "
        f"docstring."
    )
