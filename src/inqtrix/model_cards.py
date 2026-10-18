"""Curated, provider-neutral catalogue of model cards (single source of truth).

A *model card* holds the UI-facing metadata for one logical model (display
name, category, cost, context window, reasoning levels, capabilities) plus a
flat list of every provider-specific identifier that means this model. The
catalogue is hand-curated: ``source_url`` and ``last_verified`` record the
provenance of each card so the data stays auditable (no silent, unsourced
numbers; nothing the sources do not state).

This module deliberately holds **no** routing logic. Resolving which model a
call site uses stays in :mod:`inqtrix.model_routing`; this module only answers
"given an identifier string, which card describes it?" via
:func:`resolve_model_card`. The cross-provider identifier problem (the same
logical model is ``claude-opus-4-8`` on the Anthropic API, ``anthropic.claude-
opus-4-8`` on Bedrock -- optionally region-prefixed ``eu.``/``us.`` and version-
suffixed ``-v1:0`` -- and an operator-named deployment on Azure) is solved by
the per-card :attr:`ModelCard.aliases` list plus a small normalisation step, so
no provider-specific alias table is needed elsewhere.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ModelCategory = Literal["high", "mid", "fast"]
"""Display grouping in the model picker. Independent of latency (see
:attr:`ModelCard.speed`) and of the algorithm's internal node tiers."""

ModelSpeed = Literal["langsam", "mittel", "schnell"]
"""Latency hint shown as the picker's TEMPO tile; intentionally decoupled from
:data:`ModelCategory` (a high-tier model can still be ``mittel``)."""


class Pricing(BaseModel):
    """USD list price per one million tokens for a single model.

    The UI derives a coarse ``$``..``$$$$`` tier from these numbers and shows
    the exact value alongside it. Prices are provider list prices excluding
    batch discounts, prompt caching, or long-context surcharges.
    """

    input_per_mtok: float = Field(
        ...,
        description=(
            "List price in USD for one million input (prompt) tokens, "
            "excluding any cached-input discount or long-context surcharge."
        ),
    )
    """List price in USD for one million input (prompt) tokens, excluding any cached-input discount or long-context surcharge."""
    output_per_mtok: float = Field(
        ...,
        description=(
            "List price in USD for one million output (completion) tokens, "
            "excluding batch discounts."
        ),
    )
    """List price in USD for one million output (completion) tokens, excluding batch discounts."""


class ModelCard(BaseModel):
    """Provider-neutral metadata for one logical model, shown in the UI picker.

    One card per logical model regardless of how many providers serve it. The
    :attr:`aliases` list carries every provider-specific identifier string that
    should resolve to this card (Anthropic id, Bedrock id, operator-named Azure
    deployment, LiteLLM-prefixed id). The UI always renders :attr:`display_name`
    -- never the raw, possibly version-suffixed identifier.
    """

    id: str = Field(
        ...,
        description=(
            "Canonical, stable identifier for the logical model (the first-"
            "party API id where one exists, e.g. ``claude-opus-4-8``). Used as "
            "the card's primary key; also part of :attr:`aliases`."
        ),
    )
    """Canonical, stable identifier for the logical model (the first-party API id where one exists, e.g. ``claude-opus-4-8``). Used as the card's primary key; also part of :attr:`aliases`."""
    display_name: str = Field(
        ...,
        description=(
            "Human-facing name shown in the picker and hover-card, e.g. "
            "``Claude Opus 4.8``. Always preferred over a raw identifier."
        ),
    )
    """Human-facing name shown in the picker and hover-card, e.g. ``Claude Opus 4.8``. Always preferred over a raw identifier."""
    vendor: str = Field(
        ...,
        description=(
            "Provider/lab that publishes the model, e.g. ``Anthropic``, "
            "``OpenAI``, ``Google``. Shown as the secondary label in the picker."
        ),
    )
    """Provider/lab that publishes the model, e.g. ``Anthropic``, ``OpenAI``, ``Google``. Shown as the secondary label in the picker."""
    category: ModelCategory = Field(
        ...,
        description=(
            "Display group in the picker (``high``/``mid``/``fast``). A purely "
            "presentational grouping, decoupled from latency and from the "
            "algorithm's node-tier routing."
        ),
    )
    """Display group in the picker (``high``/``mid``/``fast``). A purely presentational grouping, decoupled from latency and from the algorithm's node-tier routing."""
    speed: ModelSpeed = Field(
        ...,
        description=(
            "Latency hint for the TEMPO tile (``langsam``/``mittel``/"
            "``schnell``). Independent of :attr:`category`."
        ),
    )
    """Latency hint for the TEMPO tile (``langsam``/``mittel``/``schnell``). Independent of :attr:`category`."""
    description: str = Field(
        ...,
        description=(
            "One-line, user-facing summary of what the model is good for, "
            "shown under the name and in the hover-card."
        ),
    )
    """One-line, user-facing summary of what the model is good for, shown under the name and in the hover-card."""
    context_window_tokens: int = Field(
        ...,
        description=(
            "Maximum input context window in tokens. Drives the KONTEXT tile "
            "and is the authoritative denominator for the composer token meter "
            "when this model is selected."
        ),
    )
    """Maximum input context window in tokens. Drives the KONTEXT tile and is the authoritative denominator for the composer token meter when this model is selected."""
    max_output_tokens: int = Field(
        ...,
        description=(
            "Maximum output (completion) tokens the model can emit in one "
            "response. The token meter reserves this from the context window "
            "as output headroom (input + output must both fit)."
        ),
    )
    """Maximum output (completion) tokens the model can emit in one response. The token meter reserves this from the context window as output headroom (input + output must both fit)."""
    reasoning_levels: list[str] = Field(
        default_factory=list,
        description=(
            "Reasoning-effort tokens the model accepts, in increasing depth "
            "(e.g. ``low``, ``medium``, ``high``, ``xhigh``, ``max``). Includes "
            "``none`` only when reasoning can be turned off. An empty list "
            "means the model exposes no effort control (the UI hides the "
            "reasoning selector). The UI maps these to ``No think``/``Think``/"
            "``Think hard`` buckets."
        ),
    )
    """Reasoning-effort tokens the model accepts, in increasing depth (e.g. ``low``, ``medium``, ``high``, ``xhigh``, ``max``). Includes ``none`` only when reasoning can be turned off. An empty list means the model exposes no effort control (the UI hides the reasoning selector). The UI maps these to ``No think``/``Think``/``Think hard`` buckets."""
    capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form capability tags rendered as chips in the hover-card, "
            "e.g. ``reasoning``, ``code``, ``tool_use``, ``vision``."
        ),
    )
    """Free-form capability tags rendered as chips in the hover-card, e.g. ``reasoning``, ``code``, ``tool_use``, ``vision``."""
    input_modalities: list[str] = Field(
        default_factory=lambda: ["text"],
        description=(
            "Accepted input modalities, e.g. ``text``, ``image``. Defaults to "
            "text-only."
        ),
    )
    """Accepted input modalities, e.g. ``text``, ``image``. Defaults to text-only."""
    knowledge_cutoff: str | None = Field(
        None,
        description=(
            "Reliable knowledge cutoff as a coarse ``YYYY-MM`` string, or "
            "``None`` when unknown. Shown for context; never used as a default."
        ),
    )
    """Reliable knowledge cutoff as a coarse ``YYYY-MM`` string, or ``None`` when unknown. Shown for context; never used as a default."""
    pricing: Pricing = Field(
        ...,
        description=(
            "List price per million tokens; the picker derives a ``$`` tier "
            "and shows the exact value."
        ),
    )
    """List price per million tokens; the picker derives a ``$`` tier and shows the exact value."""
    aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Every provider-specific identifier string that resolves to this "
            "card (Anthropic id, Bedrock id incl. region/version variants, "
            "Azure deployment name, LiteLLM-prefixed id). Matched by "
            ":func:`resolve_model_card` after a small region/version "
            "normalisation; should include :attr:`id`."
        ),
    )
    """Every provider-specific identifier string that resolves to this card (Anthropic id, Bedrock id incl. region/version variants, Azure deployment name, LiteLLM-prefixed id). Matched by :func:`resolve_model_card` after a small region/version normalisation; should include :attr:`id`."""
    source_url: str = Field(
        "",
        description=(
            "URL the card's facts were curated from (provider docs/pricing), "
            "for auditability."
        ),
    )
    """URL the card's facts were curated from (provider docs/pricing), for auditability."""
    last_verified: str = Field(
        "",
        description=(
            "ISO date (``YYYY-MM-DD``) the card was last checked against its "
            "source, so stale entries are visible during maintenance."
        ),
    )
    """ISO date (``YYYY-MM-DD``) the card was last checked against its source, so stale entries are visible during maintenance."""


_REGION_PREFIXES = ("eu.", "us.", "apac.", "ap.")
"""Bedrock cross-region inference prefixes stripped before alias matching."""


def _normalize(model_id: str) -> str:
    """Reduce a provider identifier to a region/version-independent form.

    Strips a leading Bedrock cross-region prefix (``eu.``/``us.``/``apac.``/
    ``ap.``) and a trailing version marker (``:0``, ``-v1``, ``-v2``) so that
    ``eu.anthropic.claude-opus-4-8-v1:0`` and ``anthropic.claude-opus-4-8``
    compare equal.

    Args:
        model_id: A provider-specific model identifier.

    Returns:
        The normalised identifier used for alias comparison.
    """
    out = model_id.strip()
    for prefix in _REGION_PREFIXES:
        if out.startswith(prefix):
            out = out[len(prefix):]
            break
    out = out.split(":", 1)[0]
    return out.removesuffix("-v1").removesuffix("-v2")


MODEL_CARDS: tuple[ModelCard, ...] = (
    # -- Anthropic (authoritative: platform.claude.com models overview) -------
    ModelCard(
        id="claude-opus-4-8",
        display_name="Claude Opus 4.8",
        vendor="Anthropic",
        category="high",
        speed="langsam",
        description="Tiefste Analyse, lange Kontexte, stärkste Synthese.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-01",
        pricing=Pricing(input_per_mtok=5.0, output_per_mtok=25.0),
        aliases=["claude-opus-4-8", "anthropic.claude-opus-4-8"],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="claude-opus-4-7",
        display_name="Claude Opus 4.7",
        vendor="Anthropic",
        category="high",
        speed="langsam",
        description="Vorherige Opus-Generation; hochautonom, lange Kontexte.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-01",
        pricing=Pricing(input_per_mtok=5.0, output_per_mtok=25.0),
        aliases=["claude-opus-4-7", "anthropic.claude-opus-4-7"],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="claude-opus-4-6",
        display_name="Claude Opus 4.6",
        vendor="Anthropic",
        category="high",
        speed="mittel",
        description="Tiefe Analyse, lange Kontexte; adaptive Thinking.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-05",
        pricing=Pricing(input_per_mtok=5.0, output_per_mtok=25.0),
        aliases=["claude-opus-4-6", "anthropic.claude-opus-4-6-v1"],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="claude-sonnet-4-6",
        display_name="Claude Sonnet 4.6",
        vendor="Anthropic",
        category="mid",
        speed="schnell",
        description="Bestes Verhältnis aus Tempo und Intelligenz.",
        context_window_tokens=1_000_000,
        max_output_tokens=64_000,
        reasoning_levels=["none", "low", "medium", "high"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-08",
        pricing=Pricing(input_per_mtok=3.0, output_per_mtok=15.0),
        aliases=["claude-sonnet-4-6", "anthropic.claude-sonnet-4-6"],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="claude-haiku-4-5",
        display_name="Claude Haiku 4.5",
        vendor="Anthropic",
        category="fast",
        speed="schnell",
        description="Schnellstes Modell mit nahezu Frontier-Intelligenz.",
        context_window_tokens=200_000,
        max_output_tokens=64_000,
        reasoning_levels=[],  # effort wird von Haiku abgelehnt (kein Selektor)
        capabilities=["code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-02",
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=5.0),
        aliases=[
            "claude-haiku-4-5",
            "claude-haiku-4-5-20251001",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-06-08",
    ),
    # -- OpenAI (authoritative: developers.openai.com model pages) ------------
    ModelCard(
        id="gpt-5.6-sol",
        display_name="GPT-5.6 Sol",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="Stärkstes Modell der 5.6-Reihe für anspruchsvolle Analyse.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-02",
        pricing=Pricing(input_per_mtok=4.0, output_per_mtok=20.0),
        aliases=["gpt-5.6-sol", "openai/gpt-5.6-sol"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        last_verified="2026-08-31",
    ),
    ModelCard(
        id="gpt-5.6-terra",
        display_name="GPT-5.6 Terra",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="Ausgewogenes 5.6-Modell für den Alltag bei großem Kontext.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-02",
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=12.0),
        aliases=["gpt-5.6-terra", "openai/gpt-5.6-terra"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        last_verified="2026-08-31",
    ),
    ModelCard(
        id="gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        vendor="OpenAI",
        category="fast",
        speed="schnell",
        description="Sehr günstiges 5.6-Modell für hohe Stückzahlen und Unteraufträge.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-02",
        pricing=Pricing(input_per_mtok=0.2, output_per_mtok=1.2),
        aliases=["gpt-5.6-luna", "openai/gpt-5.6-luna"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        last_verified="2026-08-31",
    ),
    ModelCard(
        id="gpt-5.5-pro",
        display_name="GPT-5.5 Pro",
        vendor="OpenAI",
        category="high",
        speed="langsam",
        description="Stärkstes Pro-Modell für anspruchsvollste Aufgaben.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_levels=["medium", "high", "xhigh"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-12",
        pricing=Pricing(input_per_mtok=30.0, output_per_mtok=180.0),
        aliases=["gpt-5.5-pro", "openai/gpt-5.5-pro"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.5-pro",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-5.5",
        display_name="GPT-5.5",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="Starkes Allzweckmodell mit großem Kontext.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-12",
        pricing=Pricing(input_per_mtok=5.0, output_per_mtok=30.0),
        aliases=["gpt-5.5", "openai/gpt-5.5"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.5",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-5.4-pro",
        display_name="GPT-5.4 Pro",
        vendor="OpenAI",
        category="high",
        speed="langsam",
        description="Pro-Modell für tiefe, korrektheitskritische Aufgaben.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_levels=["medium", "high", "xhigh"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-08",
        pricing=Pricing(input_per_mtok=30.0, output_per_mtok=180.0),
        aliases=["gpt-5.4-pro", "openai/gpt-5.4-pro"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.4-pro",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-5.4",
        display_name="GPT-5.4",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="Stärkste Allzweck-Qualität für Recherche und Synthese.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-08",
        pricing=Pricing(input_per_mtok=2.5, output_per_mtok=15.0),
        aliases=["gpt-5.4", "openai/gpt-5.4"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.4",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-5.4-mini",
        display_name="GPT-5.4 Mini",
        vendor="OpenAI",
        category="mid",
        speed="schnell",
        description="Gutes Preis-Leistungs-Verhältnis für Code und Subagenten.",
        context_window_tokens=400_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-08",
        pricing=Pricing(input_per_mtok=0.75, output_per_mtok=4.5),
        aliases=["gpt-5.4-mini", "openai/gpt-5.4-mini"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.4-mini",
        last_verified="2026-08-31",
    ),
    ModelCard(
        id="gpt-5.4-nano",
        display_name="GPT-5.4 Nano",
        vendor="OpenAI",
        category="fast",
        speed="schnell",
        description="Günstigstes GPT-5.4-Modell für einfache Massenaufgaben.",
        context_window_tokens=400_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-08",
        pricing=Pricing(input_per_mtok=0.2, output_per_mtok=1.25),
        aliases=["gpt-5.4-nano", "openai/gpt-5.4-nano"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.4-nano",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-5.1",
        display_name="GPT-5.1",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="GPT-5-Reasoning-Generation mit konfigurierbarem Aufwand.",
        context_window_tokens=400_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2024-09",
        pricing=Pricing(input_per_mtok=1.25, output_per_mtok=10.0),
        aliases=["gpt-5.1", "openai/gpt-5.1"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.1",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-5-mini",
        display_name="GPT-5 Mini",
        vendor="OpenAI",
        category="mid",
        speed="schnell",
        description="Günstiges GPT-5-Modell für latenzkritische Massenlasten.",
        context_window_tokens=400_000,
        max_output_tokens=128_000,
        reasoning_levels=["minimal", "low", "medium", "high"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2024-05",
        pricing=Pricing(input_per_mtok=0.25, output_per_mtok=2.0),
        aliases=["gpt-5-mini", "openai/gpt-5-mini"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5-mini",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-5-nano",
        display_name="GPT-5 Nano",
        vendor="OpenAI",
        category="fast",
        speed="schnell",
        description="Günstigstes GPT-5-Modell für einfachste Massenaufgaben.",
        context_window_tokens=400_000,
        max_output_tokens=128_000,
        reasoning_levels=["minimal", "low", "medium", "high"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2024-05",
        pricing=Pricing(input_per_mtok=0.05, output_per_mtok=0.4),
        aliases=["gpt-5-nano", "openai/gpt-5-nano"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5-nano",
        last_verified="2026-06-08",
    ),
    ModelCard(
        id="gpt-4.1",
        display_name="GPT-4.1",
        vendor="OpenAI",
        category="mid",
        speed="schnell",
        description="Schnelles Allzweckmodell ohne Reasoning; sehr großer Kontext.",
        context_window_tokens=1_047_576,
        max_output_tokens=32_768,
        reasoning_levels=[],  # Non-Reasoning-Modell: kein reasoning_effort
        capabilities=["code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2024-06",
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=8.0),
        aliases=["gpt-4.1", "openai/gpt-4.1"],
        source_url="https://developers.openai.com/api/docs/models/gpt-4.1",
        last_verified="2026-06-08",
    ),
    # -- Google (display-ready; reasoning uses thinking_level, not effort) ----
    ModelCard(
        id="gemini-3.1-pro",
        display_name="Gemini 3.1 Pro",
        vendor="Google",
        category="mid",
        speed="mittel",
        description="Ausgewogen, schnell genug; großer Kontext.",
        context_window_tokens=1_000_000,
        max_output_tokens=64_000,
        reasoning_levels=[],  # Gemini nutzt thinking_level (nicht effort); nicht verdrahtet
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-01",
        pricing=Pricing(input_per_mtok=2.5, output_per_mtok=15.0),
        aliases=["gemini-3.1-pro", "google/gemini-3.1-pro", "gemini-3.1-pro-preview"],
        source_url="https://ai.google.dev/gemini-api/docs/models",
        last_verified="2026-06-08",
    ),
)
"""The curated catalogue. Add a model by appending one :class:`ModelCard`; no
code change is needed elsewhere. Claude facts are authoritative (Anthropic
docs); OpenAI/Google facts are curated from the linked provider pages."""


def resolve_model_card(model_id: str) -> ModelCard | None:
    """Return the card matching *model_id*, or ``None`` when none is known.

    Matching compares *model_id* against every card's :attr:`ModelCard.aliases`,
    both exactly and after :func:`_normalize` (so Bedrock region/version variants
    of the same model resolve to one card). A ``None`` result is the honest
    "unknown model" signal: callers must surface it visibly (the UI shows a
    "no model card" state and degrades gracefully) and must never substitute a
    default card.

    Args:
        model_id: A provider-specific model identifier (as configured in a
            provider's ``selectable_models`` or sent as a per-run override).

    Returns:
        The matching :class:`ModelCard`, or ``None`` if no alias matches.
    """
    needle = model_id.strip()
    if not needle:
        return None
    normalized = _normalize(needle)
    for card in MODEL_CARDS:
        for alias in card.aliases:
            if alias == needle or _normalize(alias) == normalized:
                return card
    return None


def build_models_catalog(selectable_models: list[str]) -> list[dict[str, object]]:
    """Resolve selectable model ids to serialisable catalogue entries for the UI.

    One ``{"model_id", "card"}`` entry per id, in order. ``card`` is the matching
    :class:`ModelCard` as a plain dict, or ``None`` when no card matches -- the
    UI then renders a visible "no model card" state and degrades gracefully
    instead of receiving a fabricated default.

    Args:
        selectable_models: The provider's curated list of selectable model ids
            (``LLMProvider.selectable_models``).

    Returns:
        A JSON-serialisable list of catalogue entries; empty when the provider
        offers no selectable models (the UI falls back to the tier picker).
    """
    catalog: list[dict[str, object]] = []
    for model_id in selectable_models:
        card = resolve_model_card(model_id)
        catalog.append(
            {"model_id": model_id, "card": card.model_dump() if card else None}
        )
    return catalog
