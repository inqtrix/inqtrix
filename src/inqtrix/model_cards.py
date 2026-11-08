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
opus-4-8`` on Bedrock -- optionally prefixed ``eu.``/``us.``/``global.`` and version-
suffixed ``-v1:0`` -- and an operator-named deployment on Azure) is solved by
the per-card :attr:`ModelCard.aliases` list plus a small normalisation step, so
no provider-specific alias table is needed elsewhere.

The rough capacity estimate lives here as well, because the research nodes and
the agents both decide by it and neither of them may import the other for it.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from inqtrix.constants import (
    CONTEXT_WINDOW_SAFETY_TOKENS,
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
)

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


class ClaudeRules(BaseModel, frozen=True):
    """What a Claude model accepts in a request, on every platform that serves it.

    The Anthropic and Bedrock adapters read these rules; the picker never shows
    them. Source: the Claude API documentation (models overview, thinking,
    tool use).
    """

    thinking: Literal["adaptive", "manual"] = "adaptive"
    """``adaptive``: a graded effort turns on ``{"type": "adaptive"}``. ``manual``: the
    model takes only a fixed ``budget_tokens`` budget, so a graded effort cannot turn
    thinking on."""
    thinking_off: bool = True
    """Whether ``thinking: {"type": "disabled"}`` is accepted at the default effort."""
    thinks_by_default: bool = False
    """Whether the model thinks when a request sends no ``thinking`` at all, so turning
    thinking off takes an explicit ``{"type": "disabled"}``."""
    effort: bool = True
    """Whether ``output_config.effort`` is accepted."""
    forced_tool_choice: bool = True
    """Whether ``tool_choice`` ``any`` or ``tool`` is accepted with adaptive thinking."""
    sampling: bool = True
    """Whether ``temperature``, ``top_p`` and ``top_k`` are accepted."""
    prefix_binding: bool = False
    """Whether returned thinking blocks stay valid only while ``system``, ``tools`` and the
    messages before them are unchanged (Claude 5.1 and later)."""


class OpenAIRules(BaseModel, frozen=True):
    """What an OpenAI model accepts in a request, read by the OpenAI-compatible adapters.

    The Azure and LiteLLM adapters read these rules; the picker never shows them.
    Source: the OpenAI model pages and the Azure reasoning guide.
    """

    api: Literal["chat_completions", "responses"] = "chat_completions"
    """The OpenAI API that carries this model's calls. ``responses`` for a model that
    refuses function tools while it reasons over Chat Completions (GPT-6, GPT-5.6)
    or offers no Chat Completions at all (the Pro models); the
    adapters then send every call through the Responses API. ``chat_completions``,
    the proven path, for every other model; it takes no ``max`` effort."""


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
    max_input_tokens: int | None = Field(
        None,
        exclude=True,
        description=(
            "Input limit the provider sets on its own, whatever output a call "
            "asks for (922,000 for the 1,050,000-token GPT-6, GPT-5.6 and GPT-5.5 "
            "windows); None when only the window limits input plus output."
        ),
    )
    """Input limit the provider sets on its own, whatever output a call asks for (922,000 for the 1,050,000-token GPT-6, GPT-5.6 and GPT-5.5 windows); None when only the window limits input plus output. Read by capacity checks, never part of the picker catalogue."""
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
            ":func:`resolve_model_card` exactly and after normalisation (case, "
            "path prefix, region, version, dated snapshot); should include :attr:`id`."
        ),
    )
    """Every provider-specific identifier string that resolves to this card (Anthropic id, Bedrock id incl. region/version variants, Azure deployment name, LiteLLM-prefixed id). Matched by :func:`resolve_model_card` exactly and after normalisation (case, path prefix, region, version, dated snapshot); should include :attr:`id`."""
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
    claude: ClaudeRules | None = Field(
        None,
        exclude=True,
        description="Request rules of a Claude model; None for other vendors.",
    )
    """Request rules of a Claude model, read by the Anthropic and Bedrock adapters; never part of the picker catalogue."""
    openai: OpenAIRules | None = Field(
        None,
        exclude=True,
        description="Request rules of an OpenAI model; None for other vendors.",
    )
    """Request rules of an OpenAI model, read by the Azure and LiteLLM adapters; never part of the picker catalogue."""


_REGION_PREFIXES = ("eu.", "us.", "us-gov.", "apac.", "ap.", "ca.", "au.", "jp.", "global.")
"""Bedrock cross-region inference prefixes stripped before alias matching: the
geographic profiles and the global one (``global.anthropic.claude-haiku-5-5``)."""

_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
"""The dated snapshot an OpenAI or Azure answer reports as its model
(``gpt-5.4-2026-03-05`` for a call to ``gpt-5.4``)."""


def _normalize(model_id: str) -> str:
    """Reduce a provider identifier to a region/version-independent form.

    Strips a leading Bedrock cross-region prefix (a geographic profile such as
    ``eu.``/``us.``/``jp.`` or the global ``global.``), a trailing version
    marker (``:0``, ``-v1``, ``-v2``) and a dated snapshot (``-2026-03-05``) so
    that ``eu.anthropic.claude-opus-4-8-v1:0`` and ``anthropic.claude-opus-4-8``
    compare equal, and so do the reported ``gpt-5.4-2026-03-05`` and ``gpt-5.4``.
    Case and path stay, so a metric label of a model without a card keeps them
    (:func:`~inqtrix.observability.metrics_defs.metric_model_label`).

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
    return _SNAPSHOT_SUFFIX.sub("", out.removesuffix("-v1").removesuffix("-v2"))


MODEL_CARDS: tuple[ModelCard, ...] = (
    # -- Anthropic (authoritative: platform.claude.com models overview) -------
    ModelCard(
        id="claude-fable-5-1",
        display_name="Claude Fable 5.1",
        vendor="Anthropic",
        category="high",
        speed="langsam",
        description="Anspruchsvollstes Denken und lange agentische Arbeit.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-06",
        pricing=Pricing(input_per_mtok=10.0, output_per_mtok=50.0),
        aliases=[
            "claude-fable-5-1",
            "anthropic.claude-fable-5-1",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-11-04",
        claude=ClaudeRules(thinking_off=False, thinks_by_default=True, forced_tool_choice=False, sampling=False, prefix_binding=True),
    ),
    ModelCard(
        id="claude-opus-5-5",
        display_name="Claude Opus 5.5",
        vendor="Anthropic",
        category="high",
        speed="mittel",
        description="Lange agentische Code- und Wissensarbeit, Standard für die meisten Aufgaben.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-06",
        pricing=Pricing(input_per_mtok=4.0, output_per_mtok=20.0),
        aliases=[
            "claude-opus-5-5",
            "anthropic.claude-opus-5-5",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-11-04",
        claude=ClaudeRules(thinking_off=False, thinks_by_default=True, forced_tool_choice=False, sampling=False, prefix_binding=True),
    ),
    ModelCard(
        id="claude-sonnet-5-5",
        display_name="Claude Sonnet 5.5",
        vendor="Anthropic",
        category="mid",
        speed="schnell",
        description="Beste Verbindung aus Tempo und Intelligenz.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-06",
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=10.0),
        aliases=[
            "claude-sonnet-5-5",
            "anthropic.claude-sonnet-5-5",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-11-04",
        claude=ClaudeRules(thinking_off=False, thinks_by_default=True, forced_tool_choice=False, sampling=False, prefix_binding=True),
    ),
    ModelCard(
        id="claude-haiku-5-5",
        display_name="Claude Haiku 5.5",
        vendor="Anthropic",
        category="fast",
        speed="schnell",
        description="Hohes Volumen und kurze Antwortzeit: Klassifizieren, Extrahieren, Weiterleiten.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-06",
        pricing=Pricing(input_per_mtok=0.1, output_per_mtok=0.5),
        aliases=[
            "claude-haiku-5-5",
            "anthropic.claude-haiku-5-5",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-11-04",
        claude=ClaudeRules(thinks_by_default=True, sampling=False, prefix_binding=True),
    ),
    ModelCard(
        id="claude-fable-5",
        display_name="Claude Fable 5",
        vendor="Anthropic",
        category="high",
        speed="langsam",
        description="Vorgänger von Fable 5.1, weiter verfügbar.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff=None,
        pricing=Pricing(input_per_mtok=10.0, output_per_mtok=50.0),
        aliases=[
            "claude-fable-5",
            "anthropic.claude-fable-5",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-11-04",
        claude=ClaudeRules(thinking_off=False, thinks_by_default=True, sampling=False),
    ),
    ModelCard(
        id="claude-opus-5",
        display_name="Claude Opus 5",
        vendor="Anthropic",
        category="high",
        speed="mittel",
        description="Vorgänger von Opus 5.5, weiter verfügbar.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff=None,
        pricing=Pricing(input_per_mtok=5.0, output_per_mtok=25.0),
        aliases=[
            "claude-opus-5",
            "anthropic.claude-opus-5",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-11-04",
        claude=ClaudeRules(thinks_by_default=True, sampling=False),
    ),
    ModelCard(
        id="claude-sonnet-5",
        display_name="Claude Sonnet 5",
        vendor="Anthropic",
        category="mid",
        speed="schnell",
        description="Vorgänger von Sonnet 5.5, weiter verfügbar.",
        context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff=None,
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=10.0),
        aliases=[
            "claude-sonnet-5",
            "anthropic.claude-sonnet-5",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-11-04",
        claude=ClaudeRules(thinks_by_default=True, sampling=False),
    ),
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
        claude=ClaudeRules(sampling=False),
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
        claude=ClaudeRules(sampling=False),
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
        claude=ClaudeRules(),
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
        claude=ClaudeRules(),
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
            "anthropic.claude-haiku-4-5",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        ],
        source_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        last_verified="2026-06-08",
        claude=ClaudeRules(thinking="manual", effort=False),
    ),
    # -- OpenAI (authoritative: developers.openai.com model pages) ------------
    ModelCard(
        id="gpt-6-astra",
        display_name="GPT-6 Astra",
        vendor="OpenAI",
        category="high",
        speed="langsam",
        description="Leistungsstärkstes GPT-6-Modell für die anspruchsvollste Arbeit.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        max_input_tokens=922_000,
        reasoning_levels=["low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-04",
        pricing=Pricing(input_per_mtok=10.0, output_per_mtok=50.0),
        aliases=["gpt-6-astra", "openai/gpt-6-astra"],
        source_url="https://developers.openai.com/api/docs/models/gpt-6-astra",
        last_verified="2026-11-06",
        openai=OpenAIRules(api="responses"),
    ),
    ModelCard(
        id="gpt-6.1-sol",
        display_name="GPT-6.1 Sol",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="Fast so stark wie Astra für komplexe Aufgaben, zu geringeren Kosten.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        max_input_tokens=922_000,
        reasoning_levels=["low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-04",
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=10.0),
        aliases=["gpt-6.1-sol", "openai/gpt-6.1-sol"],
        source_url="https://developers.openai.com/api/docs/models/gpt-6.1-sol",
        last_verified="2026-11-06",
        openai=OpenAIRules(api="responses"),
    ),
    ModelCard(
        id="gpt-6.1-terra",
        display_name="GPT-6.1 Terra",
        vendor="OpenAI",
        category="mid",
        speed="schnell",
        description="Schnelles, effizientes Modell für Bildverständnis und klares, natürliches Schreiben.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        max_input_tokens=922_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-06",
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=5.0),
        aliases=["gpt-6.1-terra", "openai/gpt-6.1-terra"],
        source_url="https://developers.openai.com/api/docs/models/gpt-6.1-terra",
        last_verified="2026-11-06",
        openai=OpenAIRules(api="responses"),
    ),
    ModelCard(
        id="gpt-6-luna",
        display_name="GPT-6 Luna",
        vendor="OpenAI",
        category="fast",
        speed="schnell",
        description="Effizientestes GPT-6-Modell für klar umrissene Aufgaben in großer Zahl.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        max_input_tokens=922_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-05",
        pricing=Pricing(input_per_mtok=0.1, output_per_mtok=0.5),
        aliases=["gpt-6-luna", "openai/gpt-6-luna"],
        source_url="https://developers.openai.com/api/docs/models/gpt-6-luna",
        last_verified="2026-11-06",
        openai=OpenAIRules(api="responses"),
    ),
    ModelCard(
        id="gpt-6-sol",
        display_name="GPT-6 Sol",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="Vorgänger von GPT-6.1 Sol für komplexe Aufgaben.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        max_input_tokens=922_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-04",
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=10.0),
        aliases=["gpt-6-sol", "openai/gpt-6-sol"],
        source_url="https://developers.openai.com/api/docs/models/gpt-6-sol",
        last_verified="2026-11-06",
        openai=OpenAIRules(api="responses"),
    ),
    ModelCard(
        id="gpt-5.6-sol",
        display_name="GPT-5.6 Sol",
        vendor="OpenAI",
        category="high",
        speed="mittel",
        description="Stärkstes Modell der 5.6-Reihe für anspruchsvolle Analyse.",
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        max_input_tokens=922_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-02",
        pricing=Pricing(input_per_mtok=4.0, output_per_mtok=20.0),
        aliases=["gpt-5.6-sol", "openai/gpt-5.6-sol"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        last_verified="2026-08-31",
        openai=OpenAIRules(api="responses"),
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
        max_input_tokens=922_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-02",
        pricing=Pricing(input_per_mtok=2.0, output_per_mtok=12.0),
        aliases=["gpt-5.6-terra", "openai/gpt-5.6-terra"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        last_verified="2026-08-31",
        openai=OpenAIRules(api="responses"),
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
        max_input_tokens=922_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh", "max"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2026-02",
        pricing=Pricing(input_per_mtok=0.2, output_per_mtok=1.2),
        aliases=["gpt-5.6-luna", "openai/gpt-5.6-luna"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        last_verified="2026-08-31",
        openai=OpenAIRules(api="responses"),
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
        openai=OpenAIRules(api="responses"),
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
        max_input_tokens=922_000,
        reasoning_levels=["none", "low", "medium", "high", "xhigh"],
        capabilities=["reasoning", "code", "tool_use", "vision"],
        input_modalities=["text", "image"],
        knowledge_cutoff="2025-12",
        pricing=Pricing(input_per_mtok=5.0, output_per_mtok=30.0),
        aliases=["gpt-5.5", "openai/gpt-5.5"],
        source_url="https://developers.openai.com/api/docs/models/gpt-5.5",
        last_verified="2026-06-08",
        openai=OpenAIRules(),
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
        openai=OpenAIRules(api="responses"),
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
        openai=OpenAIRules(),
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
        openai=OpenAIRules(),
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
        openai=OpenAIRules(),
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
        openai=OpenAIRules(),
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
        openai=OpenAIRules(),
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
        openai=OpenAIRules(),
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
        openai=OpenAIRules(),
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


def _match_key(model_id: str) -> str:
    """The form a card match compares: :func:`_normalize` of the lower-cased last path segment.

    Ignores case and a gateway prefix such as ``azure/`` or ``openai/``, and reads
    the profile at the end of a Bedrock inference-profile ARN, so an Azure
    deployment ``GPT-5.4``, a LiteLLM alias ``azure/gpt-5.4`` and the reported
    ``gpt-5.4-2026-03-05`` all find the ``gpt-5.4`` card.
    """
    return _normalize(model_id.strip().lower().rsplit("/", 1)[-1])


def resolve_model_card(model_id: str) -> ModelCard | None:
    """Return the card matching *model_id*, or ``None`` when none is known.

    Matching compares *model_id* against every card's :attr:`ModelCard.aliases`,
    first exactly (ignoring case) across all cards, then after :func:`_match_key`
    (so Bedrock region/version variants, gateway prefixes and dated snapshots of
    the same model resolve to one card). The exact pass first lets a card for a
    snapshot of its own win over the undated card. A ``None`` result is the honest
    "unknown model" signal: callers must surface it visibly (the UI shows a
    "no model card" state and degrades gracefully) and must never substitute a
    default card.

    Args:
        model_id: A provider-specific model identifier (as configured in a
            provider's ``selectable_models`` or sent as a per-run override).

    Returns:
        The matching :class:`ModelCard`, or ``None`` if no alias matches.
    """
    needle = model_id.strip().lower()
    if not needle:
        return None
    for card in MODEL_CARDS:
        if any(alias.lower() == needle for alias in card.aliases):
            return card
    key = _match_key(needle)
    for card in MODEL_CARDS:
        if any(_match_key(alias) == key for alias in card.aliases):
            return card
    return None


def build_models_catalog(
    selectable_models: list[str],
    reasoning_levels: Any = None,
) -> list[dict[str, object]]:
    """Resolve selectable model ids to serialisable catalogue entries for the UI.

    One ``{"model_id", "card"}`` entry per id, in order. ``card`` is the matching
    :class:`ModelCard` as a plain dict, or ``None`` when no card matches -- the
    UI then renders a visible "no model card" state and degrades gracefully
    instead of receiving a fabricated default. The card's ``reasoning_levels``
    are those of the card that the provider can also send for that model, in the
    card's order, so the picker never offers a level the call would not carry
    (``max`` on Chat Completions).

    Args:
        selectable_models: The provider's curated list of selectable model ids
            (``LLMProvider.selectable_models``).
        reasoning_levels: The provider's ``reasoning_levels(model=...)``, or
            ``None``; where it answers ``None`` the card's levels stand.

    Returns:
        A JSON-serialisable list of catalogue entries; empty when the provider
        offers no selectable models (the UI falls back to the tier picker).
    """
    catalog: list[dict[str, object]] = []
    for model_id in selectable_models:
        card = resolve_model_card(model_id)
        entry = card.model_dump() if card else None
        if entry is not None and callable(reasoning_levels):
            levels = reasoning_levels(model=model_id)
            if levels is not None:
                entry["reasoning_levels"] = [level for level in card.reasoning_levels if level in levels]
        catalog.append({"model_id": model_id, "card": entry})
    return catalog


def llm_context_window_tokens(llm: Any) -> int | None:
    """Return a positive configured context window from an LLM provider."""
    value = getattr(llm, "context_window_tokens", None)
    if callable(value):
        value = value()
    if isinstance(value, int) and value > 0:
        return value
    raw = getattr(llm, "_context_window_tokens", None)
    return raw if isinstance(raw, int) and raw > 0 else None


def llm_default_output_tokens(llm: Any) -> int:
    """Return the provider's configured reasoning output budget."""
    raw = getattr(llm, "_default_max_tokens", None)
    return raw if isinstance(raw, int) and raw > 0 else DEFAULT_LLM_MAX_OUTPUT_TOKENS


def card_input_budget(card: ModelCard) -> int:
    """Input tokens the kernel lets a call of *card*'s model use: the window less the output and a margin.

    The margin is a fiftieth of the window, at least 1,024 tokens. The budget
    never exceeds the card's :attr:`ModelCard.max_input_tokens`.
    """
    window = int(card.context_window_tokens)
    budget = max(1, window - int(card.max_output_tokens) - max(1024, window // 50))
    return min(budget, card.max_input_tokens) if card.max_input_tokens else budget


def estimate_required_context_tokens(
    *,
    system: str | None,
    prompt: str,
    requested_output_tokens: int,
) -> tuple[int, int]:
    """Estimate input and total token capacity for one LLM call."""
    char_count = len(system or "") + len(prompt or "")
    estimated_input_tokens = max(1, (char_count + 3) // 4)
    estimated_required_tokens = (
        estimated_input_tokens
        + max(0, int(requested_output_tokens or 0))
        + CONTEXT_WINDOW_SAFETY_TOKENS
    )
    return estimated_input_tokens, estimated_required_tokens
