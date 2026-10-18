"""Pydantic Settings stack for type-safe environment-variable configuration.

This module bridges the user-facing ``.env`` / process-env surface to typed
Python objects consumed by :class:`~inqtrix.agent.ResearchAgent` and the HTTP
server. Three concerns are split into dedicated classes (``ModelSettings``,
``AgentSettings``, ``ServerSettings``) and re-aggregated by :class:`Settings`
so providers, agents, and routes can subscribe only to the slice they need.

Precedence (highest wins):

1. Programmatic ``AgentConfig`` overrides (in library mode)
2. Real process environment variables
3. ``.env`` file in the current working directory
4. Built-in defaults defined here
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

from inqtrix.constants import (
    MAX_PLAN_TASKS_DEFAULT,
    ANSWER_PROMPT_CITATIONS_MAX,
    CLAIM_EXTRACT_TIMEOUT,
    CONFIDENCE_STOP,
    EDITOR_ASSISTANT_TIMEOUT,
    FIRST_ROUND_QUERIES,
    HIGH_RISK_SCORE_THRESHOLD,
    MAX_QUESTION_LENGTH,
    MAX_ROUNDS,
    MAX_TOTAL_SECONDS,
    REASONING_TIMEOUT,
    REQUIRED_CONTEXT_WINDOW_TOKENS,
    RUN_INCOGNITO_RETENTION_SECONDS,
    SEARCH_CACHE_MAXSIZE,
    SEARCH_CACHE_TTL,
    SEARCH_TIMEOUT,
)
from inqtrix.core.constants import AGENT_MODE_IDS
from inqtrix.model_routing import resolve_model, validate_model_tier
from inqtrix.report_profiles import (
    ReportProfile,
    ReportProfileTuning,
    settings_overrides_for_report_profile,
    tuning_for_report_profile,
)


_SETTINGS_MODEL_CONFIG = {
    "env_prefix": "",
    "extra": "ignore",
    "populate_by_name": True,
    "env_file": ".env",
    "env_file_encoding": "utf-8",
}


_log = logging.getLogger("inqtrix")

class ModelSettings(BaseSettings):
    """Per-role model identifiers loaded from environment variables.

    Inqtrix dispatches each agent role (reasoning, classify, claim extraction,
    evaluate, search) to a named model. This class collects the env-var
    bindings for those names and provides ``effective_*_model`` properties
    that fall back to ``reasoning_model`` whenever a role-specific name is
    empty. Concrete provider classes such as :class:`~inqtrix.providers.LiteLLM`
    or :class:`~inqtrix.providers.AzureOpenAILLM` translate these names to
    their backend (OpenAI model id, Azure deployment name, Bedrock model id).

    Use this when running in env-driven mode. In Baukasten / explicit-
    provider mode, provider constructors take model names directly and
    ``ModelSettings`` is bypassed.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    reasoning_model: str = Field(
        "claude-opus-4.6-agent",
        alias="REASONING_MODEL",
        description=(
            "Identifier of the primary reasoning model used for plan and "
            "answer synthesis (and as the fallback for unset role-"
            "specific models). Format depends on the provider: an "
            "OpenAI/LiteLLM model id (``gpt-4o``), an Anthropic model "
            "name (``claude-opus-4-20250514``), or an Azure deployment "
            "name when used together with ``AzureOpenAILLM``."
        ),
    )
    """Identifier of the primary reasoning model used for plan and answer synthesis (and as the fallback for unset role-specific models). Format depends on the provider: an OpenAI/LiteLLM model id (``gpt-4o``), an Anthropic model name (``claude-opus-4-20250514``), or an Azure deployment name when used together with ``AzureOpenAILLM``."""
    search_model: str = Field(
        "",
        alias="SEARCH_MODEL",
        description=(
            "Identifier of the search model called by ``PerplexitySearch`` "
            "or any LiteLLM-routed search adapter. Ignored when the "
            "configured search provider is non-LLM (e.g. "
            "``AzureFoundryWebSearch`` that uses an agent reference)."
        ),
    )
    """Identifier of the search model called by ``PerplexitySearch`` or any LiteLLM-routed search adapter. Ignored when the configured search provider is non-LLM (e.g. ``AzureFoundryWebSearch`` that uses an agent reference)."""
    classify_model: str = Field(
        "",
        alias="CLASSIFY_MODEL",
        description=(
            "Optional cheaper model used by the classify node. Empty "
            "string falls back to ``reasoning_model``. Setting a smaller "
            "model here is the standard cost optimisation: classify runs "
            "on every request and rarely needs frontier reasoning."
        ),
    )
    """Optional cheaper model used by the classify node. Empty string falls back to ``reasoning_model``. Setting a smaller model here is the standard cost optimisation: classify runs on every request and rarely needs frontier reasoning."""
    claim_extract_model: str = Field(
        "",
        alias="CLAIM_EXTRACT_MODEL",
        description=(
            "Optional cheaper model used by claim-extraction calls. "
            "Empty string falls back to "
            "``reasoning_model``. This is the highest-volume role in a "
            "research run (one call per search hit), so a smaller model "
            "here typically delivers the largest cost saving."
        ),
    )
    """Optional cheaper model used by claim-extraction calls. Empty string falls back to ``reasoning_model``. This is the highest-volume role in a research run (one call per search hit), so a smaller model here typically delivers the largest cost saving."""
    evaluate_model: str = Field(
        "",
        alias="EVALUATE_MODEL",
        description=(
            "Optional per-node override for the evaluate node. Empty string "
            "falls back to the node's tier model and then to "
            "``reasoning_model``. Be careful: an under-powered evaluate model "
            "is a common cause of premature stops on contested topics."
        ),
    )
    """Optional per-node override for the evaluate node. Empty string falls back to the node's tier model and then to ``reasoning_model``. Be careful: an under-powered evaluate model is a common cause of premature stops on contested topics."""
    plan_model: str = Field(
        "",
        alias="PLAN_MODEL",
        description=(
            "Optional per-node override for the plan node (search-query "
            "generation). Empty string falls back to the node's tier model "
            "and then to ``reasoning_model``."
        ),
    )
    """Optional per-node override for the plan node (search-query generation). Empty string falls back to the node's tier model and then to ``reasoning_model``."""
    answer_model: str = Field(
        "",
        alias="ANSWER_MODEL",
        description=(
            "Optional per-node override for the answer node (final report "
            "synthesis). Empty string falls back to the node's tier model "
            "and then to ``reasoning_model``."
        ),
    )
    """Optional per-node override for the answer node (final report synthesis). Empty string falls back to the node's tier model and then to ``reasoning_model``."""
    direct_chat_model: str = Field(
        "",
        alias="DIRECT_CHAT_MODEL",
        description=(
            "Optional per-node override for the direct-chat node (skip-search "
            "conversational answer). Empty string falls back to the node's "
            "tier model and then to ``reasoning_model``."
        ),
    )
    """Optional per-node override for the direct-chat node (skip-search conversational answer). Empty string falls back to the node's tier model and then to ``reasoning_model``."""

    tier_high_model: str = Field(
        "",
        alias="TIER_HIGH_MODEL",
        description=(
            "Model for the 'high' tier (strongest). Assigned to the answer "
            "node by default. Empty string falls back to ``reasoning_model``."
        ),
    )
    """Model for the 'high' tier (strongest). Assigned to the answer node by default. Empty string falls back to ``reasoning_model``."""
    tier_mid_model: str = Field(
        "",
        alias="TIER_MID_MODEL",
        description=(
            "Model for the 'mid' tier. Assigned to the plan, evaluate, and "
            "direct_chat nodes by default. Empty string falls back to "
            "``reasoning_model``."
        ),
    )
    """Model for the 'mid' tier. Assigned to the plan, evaluate, and direct_chat nodes by default. Empty string falls back to ``reasoning_model``."""
    tier_fast_model: str = Field(
        "",
        alias="TIER_FAST_MODEL",
        description=(
            "Model for the 'fast' tier (cheapest/quickest). Assigned to the "
            "classify and claim_extract nodes by default. Empty string falls "
            "back to ``reasoning_model``."
        ),
    )
    """Model for the 'fast' tier (cheapest/quickest). Assigned to the classify and claim_extract nodes by default. Empty string falls back to ``reasoning_model``."""

    tier_high_effort: str = Field(
        "",
        alias="TIER_HIGH_EFFORT",
        description=(
            "Reasoning effort for the 'high' tier. One of ``none``, "
            "``minimal``, ``low``, ``medium``, ``high``, ``xhigh``. Empty "
            "string inherits the provider's constructor default (no implicit "
            "reasoning). For Anthropic any non-empty/non-none value turns on "
            "adaptive thinking; the provider maps the level to its backend."
        ),
    )
    """Reasoning effort for the 'high' tier. One of ``none``, ``minimal``, ``low``, ``medium``, ``high``, ``xhigh``. Empty string inherits the provider's constructor default (no implicit reasoning). For Anthropic any non-empty/non-none value turns on adaptive thinking; the provider maps the level to its backend."""
    tier_mid_effort: str = Field(
        "",
        alias="TIER_MID_EFFORT",
        description=(
            "Reasoning effort for the 'mid' tier. See ``tier_high_effort`` "
            "for accepted values and semantics."
        ),
    )
    """Reasoning effort for the 'mid' tier. See ``tier_high_effort`` for accepted values and semantics."""
    tier_fast_effort: str = Field(
        "",
        alias="TIER_FAST_EFFORT",
        description=(
            "Reasoning effort for the 'fast' tier. See ``tier_high_effort`` "
            "for accepted values and semantics."
        ),
    )
    """Reasoning effort for the 'fast' tier. See ``tier_high_effort`` for accepted values and semantics."""

    @property
    def effective_classify_model(self) -> str:
        """Resolve the classify model via the central tier router.

        Returns:
            ``classify_model`` if set, else the classify tier model, else
            ``reasoning_model``. Equivalent to ``resolve_model("classify",
            self)``; kept as a property for backward compatibility with
            existing callers and the ``/health`` / ``/v1/stacks`` payloads.
        """
        return resolve_model("classify", self)

    @property
    def effective_claim_extract_model(self) -> str:
        """Resolve the claim-extraction model via the central tier router.

        Returns:
            ``claim_extract_model`` if set, else the claim_extract tier
            model, else ``reasoning_model``.
        """
        return resolve_model("claim_extract", self)

    @property
    def effective_evaluate_model(self) -> str:
        """Resolve the evaluate model via the central tier router.

        Returns:
            ``evaluate_model`` if set, else the evaluate tier model, else
            ``reasoning_model``.
        """
        return resolve_model("evaluate", self)


class AgentSettings(BaseSettings):
    """Tunable parameters that shape a single research run.

    These values control the loop bounds, timeouts, risk-scoring
    behaviour, and search-cache sizing. They are sourced from environment
    variables (matching the upper-cased aliases) so that the same
    container image can be re-tuned per environment without code changes.

    The ``report_profile`` field doubles as a preset trigger: assigning
    ``ReportProfile.COMPACT`` or ``ReportProfile.DEEP`` (via env or
    constructor) auto-applies the profile-specific overrides defined in
    :mod:`inqtrix.report_profiles`, but only for fields the user has not
    explicitly set (see :meth:`with_report_profile_defaults`).
    """

    model_config = _SETTINGS_MODEL_CONFIG

    report_profile: ReportProfile = Field(
        ReportProfile.COMPACT,
        alias="REPORT_PROFILE",
        description=(
            "Selects the answer style and depth preset (``schnell``, "
            "``compact`` or ``deep``). The active profile triggers a bundle "
            "of profile-specific overrides. ``schnell`` runs exactly one "
            "round (``max_rounds=1``, ``min_rounds=1``, "
            "``first_round_queries=6``, ``max_total_seconds=3600``) with a "
            "short two-section answer; ``compact`` sets ``max_rounds=2``, "
            "``min_rounds=1``, ``confidence_stop=7`` and "
            "``first_round_queries=6``; ``deep`` sets ``max_rounds=4``, "
            "``min_rounds=2``, ``confidence_stop=8``, "
            "``first_round_queries=8``, ``answer_prompt_citations_max=500``, "
            "uniform 600-second AI-operation budgets and a 3600-second "
            "active run budget) for any field the user has not "
            "set explicitly."
        ),
    )
    """Selects the answer style and depth preset (``schnell``, ``compact`` or ``deep``). The active profile triggers profile-specific overrides for any field the user has not set explicitly: ``schnell`` runs exactly one round with the broad first-round queries and a short two-section answer (the latency-first profile); ``compact`` uses ``max_rounds=2``, ``min_rounds=1``, ``confidence_stop=7`` and ``first_round_queries=6``; ``deep`` uses ``max_rounds=4``, ``min_rounds=2``, ``confidence_stop=8``, ``first_round_queries=8`` and the larger answer/timeout budgets."""
    depth: Literal["normal", "deep"] = Field(
        "normal",
        alias="DEPTH",
        description=(
            "Agent-mode thoroughness: ``normal`` or ``deep``. "
            "``deep`` is deterministic budget-plus-verification, never an "
            "unbounded loop: the kernel runs with high reasoning effort "
            "(unless the request or a skill pin sets one), a raised "
            "iteration ceiling, child research runs on the DEEP report "
            "profile, and one rubric-checked verification pass before "
            "the final answer. Orthogonal to the permission mode."
        ),
    )
    """Agent-mode thoroughness: ``normal`` (default) or ``deep``. Deep = deterministic budget + verification: high reasoning effort on the kernel node (precedence: explicit request effort > skill pin > deep), ``kernel_max_iterations_deep`` as the loop ceiling, child research runs forced onto the DEEP report profile, and exactly ONE rubric-checked verification/revision round before finalization. Distinct from ``report_profile`` (research-mode answer preset) and orthogonal to autonomy."""

    agent_tier: Literal["", "schnell", "gruendlich", "tief"] = Field(
        "",
        alias="AGENT_TIER",
        description=(
            "User-facing speed/depth tier of the Agent Desk (Stufen): "
            "``schnell`` (one instant search, no clarification, no plan "
            "gate outside strict, chat answer), ``gruendlich`` (default "
            "behavior with bounded research children) or ``tief`` (full "
            "budgets, canvas report, escalating verification). The "
            "empty string means 'not selected' and keeps the legacy "
            "``depth`` semantics untouched. The operational meaning per "
            "tier lives in ONE table, "
            ":data:`inqtrix.agents.tier_policy.TIER_POLICIES`."
        ),
    )
    """User-facing Agent-Desk speed/depth tier (``schnell``/``gruendlich``/``tief``); empty string = not selected (legacy ``depth`` semantics apply unchanged). A selected tier bridges into ``depth`` (``tief`` -> ``deep``, else ``normal``) at the override seam so every existing depth consumer keeps working; the per-tier budgets/gates come from :data:`inqtrix.agents.tier_policy.TIER_POLICIES` (published == enforced). Never entangled with model selection."""

    @field_validator("depth", mode="before")
    @classmethod
    def _normalize_depth(cls, value: object) -> str:
        """Normalize the sole agent-depth contract and reject unknown values.

        Args:
            value: Constructor or environment value supplied for ``depth``.

        Returns:
            The lower-case ``normal`` or ``deep`` token.

        Raises:
            ValueError: If the value is not one of the two public depths.
        """
        normalized = str(value).strip().lower()
        if normalized not in {"normal", "deep"}:
            raise ValueError("depth must be 'normal' or 'deep'")
        return normalized
    max_rounds: int = Field(
        MAX_ROUNDS,
        alias="MAX_ROUNDS",
        description=(
            "Hard upper bound for the research loop. Mirrors "
            "``AgentConfig.max_rounds`` — see that field for tuning "
            "guidance. Default ``2`` matches COMPACT; DEEP raises "
            "to ``4``."
        ),
    )
    """Hard upper bound for the research loop. Mirrors ``AgentConfig.max_rounds`` — see that field for tuning guidance. Default ``2`` matches COMPACT; DEEP raises to ``4``."""
    min_rounds: int = Field(
        1,
        alias="MIN_ROUNDS",
        description=(
            "Lower bound for the research loop. Default ``1`` preserves "
            "the existing behaviour (an early ``confidence_stop`` / "
            "plateau / utility stop after Round 0 is allowed). Raise "
            "this when the model used as evaluator tends to over-"
            "confidently signal ``done`` before the STORM "
            "diversification in Round 1+ has had a chance to broaden "
            "the source pool. Typical effect of ``min_rounds=2``: at "
            "least one additional search round runs even if the "
            "confidence target was already reached in Round 0. Clamped "
            "to ``max_rounds`` at request time so configuration "
            "mistakes never extend the loop beyond the user-specified "
            "hard cap."
        ),
    )
    """Lower bound for the research loop. Default ``1`` preserves the existing behaviour (an early ``confidence_stop`` / plateau / utility stop after Round 0 is allowed). Raise this when the model used as evaluator tends to over-confidently signal ``done`` before the STORM diversification in Round 1+ has had a chance to broaden the source pool. Typical effect of ``min_rounds=2``: at least one additional search round runs even if the confidence target was already reached in Round 0. Clamped to ``max_rounds`` at request time so configuration mistakes never extend the loop beyond the user-specified hard cap."""
    confidence_stop: int = Field(
        CONFIDENCE_STOP,
        alias="CONFIDENCE_STOP",
        description=(
            "Minimum evaluator confidence (1-10) at which the stop "
            "cascade may emit ``done``. Default ``7`` for COMPACT, "
            "``8`` for DEEP. Lower for latency-sensitive deployments."
        ),
    )
    """Minimum evaluator confidence (1-10) at which the stop cascade may emit ``done``. Default ``7`` for COMPACT, ``8`` for DEEP. Lower for latency-sensitive deployments."""
    first_round_queries: int = Field(
        FIRST_ROUND_QUERIES,
        alias="FIRST_ROUND_QUERIES",
        description=(
            "Number of broad queries generated in Round 0 by the plan "
            "node. Default ``6`` for COMPACT, ``8`` for DEEP. Setting "
            "below ``4`` typically starves later rounds of source "
            "diversity."
        ),
    )
    """Number of broad queries generated in Round 0 by the plan node. Default ``6`` for COMPACT, ``8`` for DEEP. Setting below ``4`` typically starves later rounds of source diversity."""
    answer_prompt_citations_max: int = Field(
        ANSWER_PROMPT_CITATIONS_MAX,
        alias="ANSWER_PROMPT_CITATIONS_MAX",
        description=(
            "Hard upper bound on citations passed to the final answer "
            "prompt. Default ``60`` for COMPACT, ``500`` for DEEP."
        ),
    )
    """Hard upper bound on citations passed to the final answer prompt. Default ``60`` for COMPACT, ``500`` for DEEP."""
    required_context_window_tokens: int = Field(
        REQUIRED_CONTEXT_WINDOW_TOKENS,
        alias="REQUIRED_CONTEXT_WINDOW_TOKENS",
        description=(
            "Minimum model context-window size expected for DEEP / forensic "
            "runs. Default ``128_000`` tracks the common 128k model tier; "
            "the answer composer also checks the concrete prompt "
            "estimate plus requested output budget and safety margin. "
            "Unknown provider capacity emits a visible warning; known "
            "capacity below the requirement blocks normal report synthesis."
        ),
    )
    """Minimum model context-window size expected for DEEP / forensic runs. Default ``128_000`` tracks the common 128k model tier. Unknown provider capacity emits a visible warning; known capacity below the requirement blocks normal report synthesis."""

    reasoning_timeout: int = Field(
        REASONING_TIMEOUT,
        alias="REASONING_TIMEOUT",
        description=(
            "Per-call timeout (seconds) for reasoning LLM calls. "
            "Increase for slow extended-thinking deployments; decrease "
            "to fail fast against unhealthy upstreams."
        ),
    )
    """Per-call timeout (seconds) for reasoning LLM calls. Increase for slow extended-thinking deployments; decrease to fail fast against unhealthy upstreams."""
    editor_assistant_timeout: int = Field(
        EDITOR_ASSISTANT_TIMEOUT,
        alias="EDITOR_ASSISTANT_TIMEOUT",
        description=(
            "Per-call timeout (seconds) for editor suggest/instruct calls. "
            "Decoupled from ``reasoning_timeout`` so editor work (a full "
            "generation over large attached context) can get a longer budget "
            "without lengthening every research reasoning call. Defaults to "
            "the reasoning-timeout default."
        ),
    )
    """Per-call timeout (seconds) for editor suggest/instruct calls. Decoupled from ``reasoning_timeout`` so deployments may tune editor work independently while the shared default remains ``600``."""
    search_timeout: int = Field(
        SEARCH_TIMEOUT,
        alias="SEARCH_TIMEOUT",
        description=(
            "Per-call timeout (seconds) for search-provider calls. "
            "The budget covers one logical operation including retries and "
            "is clamped to the active run deadline."
        ),
    )
    """Timeout in seconds for one logical search operation including retries, clamped to the active run deadline."""
    claim_extract_timeout: int = Field(
        CLAIM_EXTRACT_TIMEOUT,
        alias="CLAIM_EXTRACT_TIMEOUT",
        description=(
            "Per-call timeout (seconds) for claim-extraction LLM calls. "
            "The budget covers one logical extraction operation including retries."
        ),
    )
    """Timeout in seconds for one logical claim-extraction operation including retries. Defaults to the shared 600-second AI-operation budget."""
    max_total_seconds: int = Field(
        MAX_TOTAL_SECONDS,
        alias="MAX_TOTAL_SECONDS",
        description=(
            "Wall-clock deadline (seconds) for the entire research run. "
            "Default ``3600`` for every report profile. Checked at "
            "node boundaries; in-flight provider calls may run slightly "
            "past this before the next check."
        ),
    )
    """Wall-clock deadline (seconds) for the active research run. Default ``3600`` for every report profile. Checked at node boundaries and used to clamp provider-operation deadlines."""
    max_question_length: int = Field(
        MAX_QUESTION_LENGTH,
        alias="MAX_QUESTION_LENGTH",
        description=(
            "Maximum input question length in characters. Inputs above "
            "this are rejected before the agent starts to protect "
            "against prompt-flooding accidents. The default is generous "
            "because the chat composer inlines attached file content into "
            "the message; lower for tighter input validation in "
            "public-facing deployments."
        ),
    )
    """Maximum input question length in characters. Inputs above this are rejected before the agent starts to protect against prompt-flooding accidents. The default is generous because the chat composer inlines attached file content into the message; lower for tighter input validation in public-facing deployments."""

    high_risk_score_threshold: int = Field(
        HIGH_RISK_SCORE_THRESHOLD,
        alias="HIGH_RISK_SCORE_THRESHOLD",
        description=(
            "Risk-score threshold (0-10) at and above which a question is "
            "flagged ``high_risk``. The flag is an observability signal only: "
            "it is surfaced in forensic events and ``/health`` and preserved "
            "across follow-up questions. It does not change model selection "
            "(use the model tiers or a per-node model override for that) and "
            "drives no query/answer heuristic. Lower the threshold "
            "to flag more questions as high-risk; raise it to flag fewer."
        ),
    )
    """Risk-score threshold (0-10) at and above which a question is flagged ``high_risk``. The flag is an observability signal only: it is surfaced in forensic events and ``/health`` and preserved across follow-up questions. It does not change model selection (use the model tiers or a per-node model override for that) and drives no query/answer heuristic. Lower the threshold to flag more questions as high-risk; raise it to flag fewer."""
    model_tier: str = Field(
        "",
        alias="MODEL_TIER",
        description=(
            "Optional per-run tier selection ('high', 'mid', or 'fast'). When "
            "set, it replaces mid/high default assignments; fast nodes keep "
            "their tier and effort. An explicit per-node model override "
            "still wins. Empty string uses the default assignment. "
            "Primary use: the chat endpoint's ``model_tier`` override lets a "
            "caller pick the model class for a direct-chat answer."
        ),
    )
    """Optional per-run tier selection ('high', 'mid', or 'fast'). It replaces mid/high default assignments; fast nodes keep their tier and effort. Explicit per-node model overrides still win. Empty uses the default assignment. Direct-chat callers can choose their answer's model class through this field."""
    model: str = Field(
        "",
        alias="MODEL_OVERRIDE",
        description=(
            "Optional explicit model id for the direct-chat answer, set by the "
            "chat/editor model picker selecting a concrete model instead of a "
            "tier. When non-empty it bypasses tier routing for the direct-chat "
            "call only; the research pipeline keeps tier routing. Empty string "
            "uses the tier. Pair with ``effort`` for reasoning depth."
        ),
    )
    """Optional explicit model id for the direct-chat answer, set by the chat/editor model picker selecting a concrete model instead of a tier. When non-empty it bypasses tier routing for the direct-chat call only; the research pipeline keeps tier routing. Empty string uses the tier. Pair with ``effort`` for reasoning depth."""
    effort: str = Field(
        "",
        alias="EFFORT_OVERRIDE",
        description=(
            "Optional reasoning effort for the direct-chat answer, paired with "
            "``model``. One of ``none``, ``minimal``, ``low``, ``medium``, "
            "``high``, ``xhigh``, ``max`` (model-dependent). Empty string "
            "inherits the provider default. Only consulted on the explicit-"
            "model direct-chat path."
        ),
    )
    """Optional reasoning effort for the direct-chat answer, paired with ``model``. One of ``none``, ``minimal``, ``low``, ``medium``, ``high``, ``xhigh``, ``max`` (model-dependent). Empty string inherits the provider default. Only consulted on the explicit-model direct-chat path."""

    @field_validator("model_tier")
    @classmethod
    def _validate_model_tier(cls, value: str) -> str:
        """Reject an unknown ``model_tier`` loudly at construction instead of ignoring it."""
        return validate_model_tier(value)

    search_cache_maxsize: int = Field(
        SEARCH_CACHE_MAXSIZE,
        alias="SEARCH_CACHE_MAXSIZE",
        description=(
            "Maximum number of search results retained in the in-memory "
            "TTL cache. Set to ``0`` to disable the cache for "
            "deterministic test runs."
        ),
    )
    """Maximum number of search results retained in the in-memory TTL cache. Set to ``0`` to disable the cache for deterministic test runs."""
    search_cache_ttl: int = Field(
        SEARCH_CACHE_TTL,
        alias="SEARCH_CACHE_TTL",
        description=(
            "Time-to-live (seconds) for cached search results. Default "
            "``3600`` (1 hour). Lower for fast-moving topics, raise "
            "for stable reference questions."
        ),
    )
    """Time-to-live (seconds) for cached search results. Default ``3600`` (1 hour). Lower for fast-moving topics, raise for stable reference questions."""

    testing_mode: bool = Field(
        False,
        alias="TESTING_MODE",
        description=(
            "When ``True``, the HTTP server exposes the "
            "``/v1/test/run`` endpoint used by ``inqtrix-parity run``. "
            "Never enable in production: the endpoint accepts arbitrary "
            "research questions without rate limiting and returns full "
            "iteration logs."
        ),
    )
    """When ``True``, the HTTP server exposes the ``/v1/test/run`` endpoint used by ``inqtrix-parity run``. Never enable in production: the endpoint accepts arbitrary research questions without rate limiting and returns full iteration logs."""

    observability_profile: str = Field(
        "summary",
        alias="OBSERVABILITY_PROFILE",
        description=(
            "Controls structured runtime event detail. ``summary`` keeps "
            "the compact INFO/DEBUG behavior, ``debug`` is reserved for "
            "future mid-level diagnostics, and ``forensic`` emits "
            "provider-neutral source, citation, claim, stop, and answer "
            "lineage into the protected run audit. Ordinary logs receive "
            "only its content-minimized operational projection; exact "
            "queries, URLs, evidence, prompts, and provider prose stay out "
            "of container/file logs."
        ),
    )
    """Control protected audit detail and its content-minimized log projection."""

    skip_search: bool = Field(
        False,
        alias="SKIP_SEARCH",
        description=(
            "When ``True``, bypasses the plan/search/evaluate loop and "
            "routes the request directly to the LLM provider with the "
            "question plus conversation history. Used by UI clients to "
            "offer a 'chat without web search' mode. The answer is "
            "returned without citations and ``round`` stays at ``0``."
        ),
    )
    """When ``True``, bypasses the plan/search/evaluate loop and routes the request directly to the LLM provider with the question plus conversation history. Used by UI clients to offer a 'chat without web search' mode. The answer is returned without citations and ``round`` stays at ``0``."""

    @property
    def report_tuning(self) -> ReportProfileTuning:
        """Return the runtime tuning bundle for the active report profile.

        Returns:
            The :class:`~inqtrix.report_profiles.ReportProfileTuning` for
            the current ``report_profile``. The tuning bundle holds
            char-/token-/citation-cap values consumed by claim extraction,
            consolidation and answer-composer paths. Cached structurally
            in ``report_profiles.py``; no per-call computation cost.
        """
        return tuning_for_report_profile(self.report_profile)

    def _report_profile_updates(
        self,
        *,
        explicit_fields: set[str] | None = None,
    ) -> dict[str, object]:
        """Compute profile-derived overrides safe to apply.

        Args:
            explicit_fields: Field names the caller has set explicitly
                (typically ``self.model_fields_set``). These are skipped
                so user intent always wins over the profile preset.

        Returns:
            Mapping ``{field_name: new_value}`` with the subset of the
            profile's ``settings_overrides`` that differs from the
            currently held value and is not in ``explicit_fields``.
            Empty dict when the profile has no overrides or no field
            needs updating.
        """
        overrides = settings_overrides_for_report_profile(self.report_profile)
        if not overrides:
            return {}

        explicit = set(explicit_fields or ())
        updates: dict[str, object] = {}
        for field_name, value in overrides.items():
            if field_name in explicit:
                continue
            if getattr(self, field_name) != value:
                updates[field_name] = value
        return updates

    def with_report_profile_defaults(
        self,
        *,
        explicit_fields: set[str] | None = None,
    ) -> "AgentSettings":
        """Return a copy with profile defaults applied.

        Use this when materialising ``AgentSettings`` from a higher-
        level config (e.g. ``AgentConfig``) where the user-specified
        fields must remain authoritative.

        Args:
            explicit_fields: Field names the user has set explicitly;
                these are preserved exactly. Pass the union of fields
                explicitly set on the source object.

        Returns:
            A new ``AgentSettings`` instance with profile-derived
            defaults filled in for non-explicit fields. Returns ``self``
            unchanged when no updates are needed.
        """
        updates = self._report_profile_updates(explicit_fields=explicit_fields)
        if not updates:
            return self
        return self.model_copy(update=updates)

    @model_validator(mode="after")
    def _apply_report_profile(self) -> "AgentSettings":
        """Apply profile defaults at construction time for non-explicit fields.

        Side effects:
            Mutates ``self`` via ``object.__setattr__`` for any field
            present in the profile's ``settings_overrides`` and not in
            ``self.model_fields_set``. Runs once after Pydantic
            validation completes.

        Returns:
            ``self`` (Pydantic ``model_validator(mode="after")``
            contract).
        """
        updates = self._report_profile_updates(explicit_fields=set(self.model_fields_set))
        for field_name, value in updates.items():
            object.__setattr__(self, field_name, value)
        profile = str(self.observability_profile or "summary").strip().lower()
        if profile not in {"summary", "debug", "forensic"}:
            raise ValueError(
                "observability_profile must be one of: summary, debug, forensic"
            )
        object.__setattr__(self, "observability_profile", profile)
        return self


class ServerSettings(BaseSettings):
    """HTTP-server-only configuration loaded from environment variables.

    These fields steer the FastAPI surface launched by
    ``python -m inqtrix``: upstream LLM-gateway connection, request
    concurrency, chat-history extraction, native run retention, and
    transport/security settings. Library-mode users (those instantiating
    :class:`~inqtrix.agent.ResearchAgent` directly) can ignore
    ``ServerSettings`` entirely.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    session_titles_enabled: bool = Field(
        True, alias="INQTRIX_SESSION_TITLES_ENABLED",
        description="Generate one optional short conversation title with a configured Fast-tier model.",
    )

    litellm_base_url: str = Field(
        "http://litellm-proxy:4000/v1",
        alias="LITELLM_BASE_URL",
        description=(
            "Base URL of the LiteLLM proxy (or any OpenAI-compatible "
            "gateway) used by the auto-created ``LiteLLM`` provider in "
            "server mode. Must include the ``/v1`` suffix. Default "
            "matches a typical Docker-Compose service name."
        ),
    )
    """Base URL of the LiteLLM proxy (or any OpenAI-compatible gateway) used by the auto-created ``LiteLLM`` provider in server mode. Must include the ``/v1`` suffix. Default matches a typical Docker-Compose service name."""
    litellm_api_key: str = Field(
        "sk-placeholder",
        alias="LITELLM_API_KEY",
        description=(
            "API key forwarded as ``Authorization: Bearer ...`` to the "
            "LiteLLM proxy. The placeholder default is intentionally "
            "obvious so misconfigured deployments fail loudly during "
            "the first upstream call."
        ),
    )
    """API key forwarded as ``Authorization: Bearer ...`` to the LiteLLM proxy. The placeholder default is intentionally obvious so misconfigured deployments fail loudly during the first upstream call."""
    perplexity_api_key: str = Field(
        "",
        alias="PERPLEXITY_API_KEY",
        description=(
            "API key for the native Perplexity Agent API used by the "
            "auto-created ``PerplexitySearch`` provider in server mode. "
            "Perplexity search runs against its own endpoint, not the "
            "LiteLLM proxy, so it needs a dedicated key. Empty default "
            "leaves the auto-created search provider unconfigured until set."
        ),
    )
    """API key for the native Perplexity Agent API used by the auto-created ``PerplexitySearch`` provider in server mode. Perplexity search runs against its own endpoint, not the LiteLLM proxy, so it needs a dedicated key. Empty default leaves the auto-created search provider unconfigured until set."""
    perplexity_base_url: str = Field(
        "",
        alias="PERPLEXITY_BASE_URL",
        description=(
            "Optional base-URL override for the auto-created "
            "``PerplexitySearch`` provider. Empty (default) uses the "
            "Perplexity SDK default (``https://api.perplexity.ai``); set "
            "only to target a Perplexity-compatible proxy."
        ),
    )
    """Optional base-URL override for the auto-created ``PerplexitySearch`` provider. Empty (default) uses the Perplexity SDK default (``https://api.perplexity.ai``); set only to target a Perplexity-compatible proxy."""
    max_concurrent: int = Field(
        100,
        alias="MAX_CONCURRENT",
        ge=1,
        description=(
            "Maximum number of concurrently executing OpenAI-compatible "
            "chat-completion requests in the HTTP server. Native "
            "``/v1/runs`` uses ``run_max_concurrent`` when set, or this "
            "same value otherwise. Each active run holds one thread for "
            "its full duration (the research graph is synchronous), so "
            "this also bounds the in-process thread pool. Sized so a "
            "hundred people work at once without queueing: a thread "
            "that is waiting on a provider costs a few kilobytes and "
            "no interpreter lock, and the lane grows only as load "
            "arrives — so the high default removes a queue rather than "
            "reserving resources. The ceiling in practice is the "
            "upstream provider's rate limit and the host's CPU/RAM, "
            "not this number — raise it when the provider supports "
            "higher parallelism, lower it if you hit provider 429s."
        ),
    )
    """Maximum number of concurrently executing OpenAI-compatible chat-completion requests in the HTTP server. Native ``/v1/runs`` uses ``run_max_concurrent`` when set, or this same value otherwise. Each active call holds one thread for its full duration (the research graph is synchronous), and this value sizes the dedicated AI thread lane — with headroom, since a disconnected stream releases its admission slot while its thread runs on. The lane grows only as load arrives, so the high default removes a queue rather than reserving resources. The real ceiling is the upstream provider's rate limit and the host's CPU/RAM; raise when the provider supports more parallelism, lower on provider 429s."""
    stream_reader_workers: int = Field(
        128,
        alias="INQTRIX_STREAM_READER_WORKERS",
        ge=1,
        description=(
            "Threads reserved for event-stream readers in the HTTP "
            "server. Three kinds of reader draw from it: every open run "
            "event stream, every open indexing event stream, and every "
            "streamed chat completion (which holds one for its whole "
            "turn while it drains progress). The count is not bounded by "
            "the number of runs — several viewers may watch the same "
            "one. Beyond this many readers, they are served in rotation, "
            "which delays events rather than dropping them. A waiting "
            "thread costs a few kilobytes and does not hold the "
            "interpreter lock, so this is sized generously on purpose; "
            "lower it only on a memory-tight host that never has many "
            "readers."
        ),
    )
    """Threads reserved for event-stream readers in the HTTP server (default 128). Drawn on by open run streams, open indexing streams, and streamed chat completions alike; viewer count is not bounded by run count. Past this many readers they are served in rotation — events are delayed, never dropped. Kept separate from the AI lane so a busy chat cannot stall event delivery."""
    run_max_concurrent: int | None = Field(
        100,
        alias="RUN_MAX_CONCURRENT",
        ge=1,
        description=(
            "Active-worker cap for native ``/v1/runs`` jobs, set by "
            "default so runs and chat completions have explicit "
            "per-surface caps. The ``None`` fallback to "
            "``max_concurrent`` survives only for callers that build "
            "settings programmatically — clearing the environment "
            "variable yields this default, not ``None``. This does not "
            "create a global cross-endpoint cap: chat and runs each "
            "admit up to their own value, so the worst case is the sum."
        ),
    )
    """Active-worker cap for native ``/v1/runs`` jobs, set by default so runs and chat completions have explicit per-surface caps. Clearing the environment variable yields this default, not ``None``; the fallback to ``max_concurrent`` survives only for programmatic callers. Chat and runs each admit up to their own value, so the worst case is the sum."""
    run_max_concurrent_per_user: int | None = Field(
        None,
        alias="RUN_MAX_CONCURRENT_PER_USER",
        ge=1,
        description=(
            "Optional per-principal fairness bound UNDER the global run "
            "cap: at most this many slot-occupying native runs per "
            "verified subject (QUEUED+RUNNING standard runs and agent "
            "CHILDREN). Parked/waiting runs hold no slot and are "
            "excluded; agent PARENTS are excluded too (they park "
            "immediately and must not contend against their own "
            "children). ONE agent wave "
            "(INQTRIX_AGENT_MAX_PARALLEL_CHILDREN) is the FLOOR, not the "
            "target: a user may keep several Agent Desks running side by "
            "side, and each of those trees needs its own wave — size it "
            "at that many waves plus headroom (startup warns only when "
            "it falls below a single wave). Approximate on Postgres under "
            "READ COMMITTED (may transiently exceed by a few); exact "
            "in-memory. Unset (default) keeps admission purely global — "
            "single-operator deployments stay byte-identical. Anonymous "
            "submissions (no subject) are never capped."
        ),
    )
    """Optional per-principal fairness bound under the global run cap (default ``None`` = off). Counts a subject's slot-occupying native runs at admission — QUEUED+RUNNING standard runs and agent CHILDREN. Excluded: parked/waiting runs (slot-free) and agent PARENTS (they park immediately and must not contend against their own children). ``INQTRIX_AGENT_MAX_PARALLEL_CHILDREN`` — one wave — is the FLOOR, not the target: a user may keep several Agent Desks running at once, and each tree needs its own wave, so size it at that many waves plus headroom. The composition root warns at startup only when the value falls below a SINGLE wave. APPROXIMATE on the Postgres backend: under READ COMMITTED, concurrent submits by one subject can transiently exceed the cap by a few — acceptable for a fairness bound (the in-memory backend is exact). It protects OTHER users from a noisy neighbour; the monthly quota bounds the neighbour's own spend."""
    run_queue_max_size: int = Field(
        100,
        alias="RUN_QUEUE_MAX_SIZE",
        ge=0,
        description=(
            "Maximum number of native ``/v1/runs`` jobs waiting in the "
            "FIFO queue. Active native jobs are governed by "
            "``run_max_concurrent`` or ``max_concurrent`` and do not "
            "count against this limit. When the queue is full, ``POST "
            "/v1/runs`` returns HTTP 429."
        ),
    )
    """Maximum number of native ``/v1/runs`` jobs waiting in the FIFO queue. Active native jobs are governed by ``run_max_concurrent`` or ``max_concurrent`` and do not count against this limit. When the queue is full, ``POST /v1/runs`` returns HTTP 429."""
    run_completed_ttl_seconds: int = Field(
        300,
        alias="RUN_COMPLETED_TTL_SECONDS",
        ge=0,
        description=(
            "TTL (seconds) for completed, failed, or cancelled native "
            "run records. During this window the UI can still fetch "
            "``/v1/runs/{run_id}``, replay buffered events, or load the "
            "result payload when the in-memory run store is active. The "
            "durable Postgres store uses ``run_durable_retention_seconds`` "
            "instead."
        ),
    )
    """TTL for terminal native run records in the in-memory store; the durable Postgres store uses ``run_durable_retention_seconds``."""
    run_durable_retention_seconds: int = Field(
        7_776_000,
        alias="RUN_DURABLE_RETENTION_SECONDS",
        ge=0,
        description=(
            "Retention (seconds) for terminal native run records in the "
            "DURABLE (Postgres) store; default 90 days. Distinct from "
            "``run_completed_ttl_seconds``, which bounds the in-memory "
            "store's replay buffer to a short window: this governs how long "
            "completed research reports stay fetchable from the database, so "
            "they survive page reloads, re-logins, and other devices instead "
            "of vanishing after the in-memory TTL. The in-memory store "
            "ignores this value. Lazy cleanup deletes terminal rows older "
            "than the window (their events and result payloads cascade); "
            "raise it to keep reports longer at the cost of bounded table "
            "growth, or set 0 to evict on the next cleanup."
        ),
    )
    """Retention (seconds) for terminal native run records in the durable (Postgres) store; default 90 days. Distinct from ``run_completed_ttl_seconds``, which bounds the in-memory store's replay buffer to a short window: this governs how long completed research reports stay fetchable from the database, so they survive page reloads, re-logins, and other devices instead of vanishing after the in-memory TTL. The in-memory store ignores this value. Lazy cleanup deletes terminal rows older than the window (their events and result payloads cascade); raise it to keep reports longer at the cost of bounded table growth, or set 0 to evict on the next cleanup. Rows that never reached a terminal state are force-failed after a hard 7-day cap and then age out through this same window — a stuck payload therefore lives at most 7 days plus this retention, like any ordinarily failed run."""
    run_incognito_retention_seconds: int = Field(
        RUN_INCOGNITO_RETENTION_SECONDS,
        alias="RUN_INCOGNITO_RETENTION_SECONDS",
        ge=60,
        description=(
            "Retention (seconds) for terminal INCOGNITO knowledge runs in "
            "either run store; default 15 minutes. The run, its events and "
            "its result are deleted at the first cleanup after this window, "
            "ahead of ``run_completed_ttl_seconds`` and "
            "``run_durable_retention_seconds``. At least 60 seconds, so a "
            "finished answer stays fetchable for the browser that asked."
        ),
    )
    """Retention (seconds) for terminal incognito knowledge runs in either run store; default 15 minutes. The run, its events and its result are deleted at the first cleanup after this window, ahead of the ordinary retention. At least 60 seconds, so a finished answer stays fetchable for the browser that asked."""
    deletion_max_concurrent: int = Field(
        2,
        alias="INQTRIX_DELETION_MAX_CONCURRENT",
        ge=1,
        description=(
            "Maximum aggregate deletion operations executed in this process "
            "at once. The worker's global concurrency remains an additional "
            "upper bound in Valkey mode."
        ),
    )
    upload_max_concurrent: int = Field(
        6,
        alias="INQTRIX_UPLOAD_MAX_CONCURRENT",
        ge=1,
        description=(
            "Maximum upload operations one WORKER process executes at "
            "once. Unlike a research run, which spends its thread waiting "
            "on a provider, an upload extracts text from the file itself "
            "and spends real CPU — so a worker sized for provider "
            "parallelism would otherwise run that many extractions at "
            "once and saturate a CPU-limited pod. "
            "INQTRIX_WORKER_CONCURRENCY remains an additional upper "
            "bound. Without a worker process this value does nothing: "
            "uploads are then reconciled one at a time in the API "
            "process, which is a serial path and not configurable."
        ),
    )
    """Maximum upload operations one WORKER process executes at once; ``INQTRIX_WORKER_CONCURRENCY`` remains an additional upper bound. Without a worker, uploads are reconciled serially in the API process and this value has no effect."""
    deletion_dispatch_timeout_seconds: int = Field(
        240,
        alias="INQTRIX_DELETION_DISPATCH_TIMEOUT_SECONDS",
        ge=1,
        description=(
            "How long an aggregate deletion operation may sit unclaimed in "
            "``queued`` before it is failed with ``dispatch_timeout``. "
            "Without this bound an operation nobody picked up never becomes "
            "terminal: clients keep polling its receipt and ``retry`` stays "
            "rejected, because retry requires ``delete_failed``. Expiry is "
            "non-destructive — an unclaimed operation has deleted nothing, "
            "and the visible failure offers retry. Keep the default "
            "comfortably above the worker reconciler's re-dispatch window "
            "(queued rows are re-sent from 120 seconds of age, checked every "
            "60) so a Valkey deployment heals itself first; a deployment "
            "without the queue has no reconciler to wait for and can lower "
            "this to about 60 seconds."
        ),
    )
    deletion_receipt_retention_seconds: int = Field(
        2_592_000,
        alias="INQTRIX_DELETION_RECEIPT_RETENTION_SECONDS",
        ge=86_400,
        description=(
            "Retention for terminal deletion-operation metadata. The "
            "default is 30 days; receipts contain identifiers and status, "
            "never document text or blob content."
        ),
    )
    run_event_buffer_size: int = Field(
        200,
        alias="RUN_EVENT_BUFFER_SIZE",
        ge=1,
        description=(
            "Number of recent structured events retained per native run "
            "for late SSE subscribers. The buffer is per-run and bounded "
            "to avoid unbounded memory growth during long research jobs."
        ),
    )
    """Number of recent structured events retained per native run for late SSE subscribers. The buffer is per-run and bounded to avoid unbounded memory growth during long research jobs."""
    max_messages_history: int = Field(
        20,
        alias="MAX_MESSAGES_HISTORY",
        description=(
            "Maximum number of OpenAI-compatible chat messages "
            "extracted from a single ``/v1/chat/completions`` request "
            "for history reconstruction. Older messages are truncated. "
            "Caps prompt cost on long-running conversations."
        ),
    )
    """Maximum number of OpenAI-compatible chat messages extracted from a single ``/v1/chat/completions`` request for history reconstruction. Older messages are truncated. Caps prompt cost on long-running conversations."""
    tls_keyfile: str = Field(
        "",
        alias="INQTRIX_SERVER_TLS_KEYFILE",
        description=(
            "Path to the PEM-encoded TLS private key file. When set "
            "together with ``tls_certfile``, the example webserver "
            "scripts hand both paths to ``uvicorn.run(...)`` so the "
            "server speaks HTTPS instead of HTTP. Setting only one of "
            "the two raises ``RuntimeError`` at startup (no silent "
            "fallback). Empty string (default) keeps the server on "
            "plain HTTP. TLS is opt-in and intended as a minimum "
            "viable hardening layer for the experimental phase; "
            "production deployments should still terminate TLS at a "
            "dedicated reverse proxy (nginx / Traefik / Caddy) for "
            "richer cipher policies."
        ),
    )
    """Path to the PEM-encoded TLS private key file. When set together with ``tls_certfile``, the example webserver scripts hand both paths to ``uvicorn.run(...)`` so the server speaks HTTPS instead of HTTP. Setting only one of the two raises ``RuntimeError`` at startup (no silent fallback). Empty string (default) keeps the server on plain HTTP. TLS is opt-in and intended as a minimum viable hardening layer for the experimental phase; production deployments should still terminate TLS at a dedicated reverse proxy (nginx / Traefik / Caddy) for richer cipher policies."""
    tls_certfile: str = Field(
        "",
        alias="INQTRIX_SERVER_TLS_CERTFILE",
        description=(
            "Path to the PEM-encoded TLS certificate file. Companion "
            "to ``tls_keyfile`` — both must be set together or both "
            "empty. See ``tls_keyfile`` for the broader rationale."
        ),
    )
    """Path to the PEM-encoded TLS certificate file. Companion to ``tls_keyfile`` — both must be set together or both empty. See ``tls_keyfile`` for the broader rationale."""
    api_key: str = Field(
        "",
        alias="INQTRIX_SERVER_API_KEY",
        description=(
            "Static Bearer API key. When set, the server installs a "
            "FastAPI dependency on chat, text-improvement, test-run, "
            "and native run routes that requires "
            "``Authorization: Bearer <api_key>`` and compares with "
            "``hmac.compare_digest`` for constant-time safety. "
            "``/health`` and ``/v1/models`` deliberately stay "
            "unauthenticated so Kubernetes liveness probes and model "
            "discovery clients keep working without credentials. "
            "Empty string (default) disables the gate, matching the "
            "historical behaviour. Rotation requires a server restart "
            "in this iteration; multi-key support is a follow-up task."
        ),
    )
    """Static Bearer API key. When set, the server installs a FastAPI dependency on chat, text-improvement, test-run, and native run routes that requires ``Authorization: Bearer <api_key>`` and compares with ``hmac.compare_digest`` for constant-time safety. ``/health`` and ``/v1/models`` deliberately stay unauthenticated so Kubernetes liveness probes and model discovery clients keep working without credentials. Empty string (default) disables the gate, matching the historical behaviour. Rotation requires a server restart in this iteration; multi-key support is a follow-up task."""
    metrics_enabled: bool = Field(
        False,
        alias="INQTRIX_METRICS_ENABLED",
        description=(
            "Mount a Prometheus ``/metrics`` endpoint. Off by default: "
            "the endpoint requires the optional ``metrics`` extra "
            "(``prometheus-client``); when the flag is on but the extra "
            "is not installed, the server logs a WARNING and leaves "
            "``/metrics`` unmounted rather than failing to start. When "
            "``api_key`` is set, ``/metrics`` is gated behind the same "
            "Bearer token; otherwise it is unauthenticated and must be "
            "protected at the network layer (NetworkPolicy / ingress "
            "allowlist). Metrics carry no run-id/subject/session labels "
            "(bounded cardinality)."
        ),
    )
    """Mount a Prometheus ``/metrics`` endpoint. Off by default: the endpoint requires the optional ``metrics`` extra (``prometheus-client``); when the flag is on but the extra is not installed, the server logs a WARNING and leaves ``/metrics`` unmounted rather than failing to start. When ``api_key`` is set, ``/metrics`` is gated behind the same Bearer token; otherwise it is unauthenticated and must be protected at the network layer (NetworkPolicy / ingress allowlist). Metrics carry no run-id/subject/session labels (bounded cardinality)."""
    cors_origins: str = Field(
        "",
        alias="INQTRIX_SERVER_CORS_ORIGINS",
        description=(
            "Comma-separated list of allowed CORS origins (e.g. "
            "``\"https://app1.example,https://app2.example\"``). When "
            "non-empty, the server installs ``CORSMiddleware`` with "
            "those origins, ``allow_methods=['GET','POST','OPTIONS']``, "
            "``allow_headers=['Authorization','Content-Type']`` and "
            "``allow_credentials=True``. Wildcard (``\"*\"``) is "
            "accepted but logged with a WARNING because browsers "
            "ignore wildcard origins when credentials are sent — use "
            "explicit origins for any browser-based UI. Empty string "
            "(default) installs no middleware (no CORS headers)."
        ),
    )
    """Comma-separated list of allowed CORS origins (e.g. ``"https://app1.example,https://app2.example"``). When non-empty, the server installs ``CORSMiddleware`` with those origins, ``allow_methods=['GET','POST','OPTIONS']``, ``allow_headers=['Authorization','Content-Type']`` and ``allow_credentials=True``. Wildcard (``"*"``) is accepted but logged with a WARNING because browsers ignore wildcard origins when credentials are sent — use explicit origins for any browser-based UI. Empty string (default) installs no middleware (no CORS headers)."""
    enable_openapi: bool = Field(
        False,
        alias="INQTRIX_ENABLE_OPENAPI",
        description=(
            "Serve the FastAPI OpenAPI schema and interactive docs "
            "(``/openapi.json``, ``/docs``, ``/redoc``). Disabled by "
            "default so production deployments keep the historical "
            "no-schema surface (the schema enumerates every route and "
            "request shape, which operators may not want public). "
            "Enable for development, SDK generation, and contract "
            "testing; the toggle only adds documentation routes and "
            "never changes API behaviour."
        ),
    )
    """Serve the FastAPI OpenAPI schema and interactive docs (``/openapi.json``, ``/docs``, ``/redoc``). Disabled by default so production deployments keep the historical no-schema surface (the schema enumerates every route and request shape, which operators may not want public). Enable for development, SDK generation, and contract testing; the toggle only adds documentation routes and never changes API behaviour."""

    max_total_input_tokens: int = Field(
        500_000,
        alias="INQTRIX_MAX_TOTAL_INPUT_TOKENS",
        ge=10_000,
        description=(
            "Approximate-token cap on the combined size of ``question`` + "
            "``messages[]`` in ``/v1/chat/completions``. Token count is "
            "estimated as ``len(text) // 4`` (provider-agnostic heuristic "
            "that overshoots slightly, which is the safe side for a DoS "
            "guard). Default ``500_000`` is generous — real research "
            "requests stay well below this. Raise only when serving "
            "providers with 1M+ effective context windows. Lower bound "
            "``10_000`` prevents accidental misconfiguration that would "
            "block typical conversational use."
        ),
    )
    """Approximate-token cap on the combined size of ``question`` + ``messages[]`` in ``/v1/chat/completions``. Token count is estimated as ``len(text) // 4`` (provider-agnostic heuristic that overshoots slightly, which is the safe side for a DoS guard). Default ``500_000`` is generous — real research requests stay well below this. Raise only when serving providers with 1M+ effective context windows. Lower bound ``10_000`` prevents accidental misconfiguration that would block typical conversational use."""
    max_message_count: int = Field(
        200,
        alias="INQTRIX_MAX_MESSAGE_COUNT",
        ge=1,
        description=(
            "Hard cap on the number of entries in ``messages[]`` for "
            "``/v1/chat/completions``. Defends against array-bomb "
            "payloads without limiting normal multi-turn flows (a typical "
            "long conversation is 20-40 messages). Returns HTTP 413 when "
            "exceeded."
        ),
    )
    """Hard cap on the number of entries in ``messages[]`` for ``/v1/chat/completions``. Defends against array-bomb payloads without limiting normal multi-turn flows (a typical long conversation is 20-40 messages). Returns HTTP 413 when exceeded."""
    public_base_url: str = Field(
        "",
        alias="INQTRIX_PUBLIC_BASE_URL",
        description=(
            "Externally reachable base URL of this server, e.g. "
            "``https://inqtrix.example.com``. It anchors collaboration "
            "WebSocket origin checks behind a trusted TLS proxy, OIDC "
            "callback derivation, and clickable knowledge citations. When "
            "empty (default), citations keep the internal ``inqtrix://`` "
            "URI scheme and forwarded headers cannot authorize a "
            "collaboration origin."
        ),
    )
    """Externally reachable base URL used for trusted collaboration origins, OIDC callback derivation, and clickable citations; empty keeps internal citation URIs and does not trust forwarded origins."""


class StorageSettings(BaseSettings):
    """Persistence-backend configuration for the platform layer.

    ``memory`` (the default) keeps the historical no-infrastructure
    deployment: identity facts live in process memory and vanish on
    restart. ``postgres`` activates the SQLAlchemy/asyncpg-backed
    identity repositories and requires ``database_url``. Run/event
    persistence stays in-memory in either mode until the durable run
    ports land.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    backend: Literal["memory", "postgres"] = Field(
        "memory",
        alias="INQTRIX_STORAGE_BACKEND",
        description=(
            "Persistence backend selector. ``memory`` (default) needs "
            "no external services and is the pytest default; "
            "``postgres`` requires ``INQTRIX_DATABASE_URL`` and a "
            "migrated database (``inqtrix-migrate``). There is "
            "deliberately no SQLite option — the schema relies on "
            "Postgres row-level security."
        ),
    )
    """Persistence backend selector. ``memory`` (default) needs no external services and is the pytest default; ``postgres`` requires ``INQTRIX_DATABASE_URL`` and a migrated database (``inqtrix-migrate``). There is deliberately no SQLite option — the schema relies on Postgres row-level security."""
    database_url: str = Field(
        "",
        alias="INQTRIX_DATABASE_URL",
        description=(
            "SQLAlchemy async database URL, e.g. "
            "``postgresql+asyncpg://inqtrix:...@127.0.0.1:5432/inqtrix``. "
            "Required (and only read) when ``backend`` is "
            "``postgres``; the empty default combined with "
            "``backend=postgres`` fails loudly at startup."
        ),
    )
    """SQLAlchemy async database URL, e.g. ``postgresql+asyncpg://inqtrix:...@127.0.0.1:5432/inqtrix``. Required (and only read) when ``backend`` is ``postgres``; the empty default combined with ``backend=postgres`` fails loudly at startup."""
    migration_database_url: str = Field(
        "",
        alias="INQTRIX_MIGRATION_DATABASE_URL",
        description=(
            "Optional direct PostgreSQL URL used only by ``inqtrix-migrate``. "
            "An empty value preserves the historical fallback to "
            "``INQTRIX_DATABASE_URL``. Production deployments should inject "
            "a dedicated migration credential into the one-shot migration "
            "job and never expose it to API or worker processes."
        ),
    )
    """Optional direct PostgreSQL URL used only by ``inqtrix-migrate``. Empty preserves the historical ``INQTRIX_DATABASE_URL`` fallback; production deployments should inject a dedicated credential only into the one-shot migration job."""
    migration_rls_mode: Literal["auto", "owner", "bypass"] = Field(
        "auto",
        alias="INQTRIX_MIGRATION_RLS_MODE",
        description=(
            "RLS authority contract for schema migrations. ``auto`` accepts "
            "the backwards-compatible superuser/BYPASSRLS path but never "
            "silently enters owner maintenance. ``bypass`` requires a "
            "dedicated BYPASSRLS login. ``owner`` uses the transaction-bound "
            "NO-FORCE maintenance path and requires quiesced services for "
            "an existing schema."
        ),
    )
    """RLS authority contract for schema migrations: ``auto`` for compatible existing deployments, explicit ``bypass`` for a dedicated BYPASSRLS login, or transaction-bound ``owner`` maintenance for managed PostgreSQL."""
    migration_services_quiesced: bool = Field(
        False,
        alias="INQTRIX_MIGRATION_SERVICES_QUIESCED",
        description=(
            "Explicit operator assertion that API, worker, collaboration and "
            "pooler processes are stopped and database sessions drained. "
            "Required for every actual revision change on an installed schema, "
            "independent of RLS mode; a fresh install or no-op at the installed "
            "target does not require it."
        ),
    )
    """Operator assertion that workloads and poolers are stopped and their database sessions drained before an installed schema changes revision."""
    app_role: str = Field(
        "inqtrix_app",
        alias="INQTRIX_DATABASE_APP_ROLE",
        description=(
            "Postgres role every application transaction switches to "
            "via ``SET LOCAL ROLE`` before touching tenant tables. The "
            "role is created NOLOGIN/NOSUPERUSER/NOBYPASSRLS by the "
            "migrations so row-level security applies even when the "
            "connection user is the table owner or a superuser "
            "(dev-compose convenience). Empty disables the switch — "
            "only sensible when the connection user itself is a "
            "restricted role."
        ),
    )
    """Postgres role every application transaction switches to via ``SET LOCAL ROLE`` before touching tenant tables. The role is created NOLOGIN/NOSUPERUSER/NOBYPASSRLS by the migrations so row-level security applies even when the connection user is the table owner or a superuser (dev-compose convenience). Empty disables the switch — only sensible when the connection user itself is a restricted role."""
    runtime_login_policy: Literal["restricted", "bundled_legacy"] = Field(
        "restricted",
        alias="INQTRIX_DATABASE_RUNTIME_LOGIN_POLICY",
        description=(
            "Security contract for the PostgreSQL session login below the "
            "transaction-local application role. ``restricted`` requires a "
            "dedicated unprivileged LOGIN and is the production default. "
            "``bundled_legacy`` accepts only the historical bundled "
            "superuser/table-owner login and is injected explicitly by the "
            "provided bundled Compose/Helm deployments."
        ),
    )
    """Runtime-login security policy: production-safe ``restricted`` by default, or the explicit ``bundled_legacy`` compatibility boundary used only by the provided bundled database stacks."""
    pool_size: int = Field(
        5,
        alias="INQTRIX_DATABASE_POOL_SIZE",
        ge=1,
        description=(
            "Persistent connections per POOLED engine. The engines are "
            "NOT enumerated here -- every process counts the ones it "
            "actually builds and logs the total at startup, because a "
            "list in prose goes stale the day a store is added. "
            "NullPool stores are unaffected by this value. The "
            "worst-case budget of one process is "
            "pooled_engines x (pool_size + max_overflow) "
            "plus the in-flight NullPool connections — size the sum of "
            "all replicas below Postgres max_connections (or front it "
            "with a transaction pooler)."
        ),
    )
    """Persistent connections per POOLED engine (default 5, the SQLAlchemy default — existing deployments see zero change). The number of pooled engines is NOT enumerated here: every process counts the ones it actually builds and logs the worst case at startup, because a list written down in prose goes stale the moment a store is added. NullPool stores open per-operation connections and ignore this setting -- including the API's run-thread lane (``build_run_thread_persistence``), which therefore holds nothing idle; its transient use is bounded by the run-concurrency cap (``RUN_MAX_CONCURRENT`` in the API, ``INQTRIX_WORKER_CONCURRENCY`` in a worker), not by this value. That worst case IS part of the startup comparison against ``max_connections``, because a synchronised burst can ask for all of it at once. Budget arithmetic: pooled_engines x (pool_size + max_overflow) per process, summed over replicas, plus the run lane's transient peak, must stay below Postgres ``max_connections`` unless a transaction pooler fronts the database."""
    pool_max_overflow: int = Field(
        10,
        alias="INQTRIX_DATABASE_POOL_MAX_OVERFLOW",
        ge=0,
        description=(
            "Burst connections a pooled engine may open beyond "
            "pool_size (closed again when idle). Part of the same "
            "budget arithmetic as pool_size."
        ),
    )
    """Burst connections a pooled engine may open beyond ``pool_size`` (default 10, the SQLAlchemy default). Counted in the same worst-case budget as ``pool_size``."""
    pool_timeout_seconds: float = Field(
        30.0,
        alias="INQTRIX_DATABASE_POOL_TIMEOUT_SECONDS",
        gt=0,
        description=(
            "Seconds a request waits for a free pooled connection "
            "before failing loudly. Bounds the queueing latency a "
            "too-small pool converts errors into."
        ),
    )
    """Seconds a request waits for a free pooled connection before failing loudly (default 30, the SQLAlchemy default). A too-small pool trades 'too many connections' errors for waits — this bounds that wait visibly."""
    command_timeout_seconds: float = Field(
        120.0,
        alias="INQTRIX_DATABASE_COMMAND_TIMEOUT_SECONDS",
        ge=0,
        description=(
            "Per-STATEMENT ceiling on pooled engines (asyncpg "
            "command_timeout; 0 disables). A statement on a silently "
            "dead connection otherwise hangs its worker thread "
            "indefinitely — with the ceiling it fails loudly and the "
            "run-store recovery paths turn it into an ordinary, "
            "visible failure. Statements legitimately longer than the "
            "ceiling need a raised value, never a disabled one."
        ),
    )
    """Per-STATEMENT wall-clock ceiling for pooled engines, enforced client-side by asyncpg (default 120 s, ``0`` disables). Bounds the one hang class connection pre-ping cannot catch: an in-flight statement on a dead connection. A fired ceiling surfaces as an ordinary database error wherever such errors already surface; per-statement scope means multi-statement transactions are bounded additively, not in total."""

    def pool_kwargs(self) -> dict[str, Any]:
        """The pooled-engine sizing bundle for :func:`build_engine`.

        Defined ONCE so every pooled call site (container, auth bundle,
        worker) splats the same policy and the per-process budget stays
        reviewable as ``engines x (pool_size + max_overflow)``.
        NullPool engines must NOT receive these (they ignore sizing).
        """
        return {
            "pool_size": self.pool_size,
            "max_overflow": self.pool_max_overflow,
            "pool_timeout": self.pool_timeout_seconds,
            "command_timeout": self.command_timeout_seconds or None,
        }

    replica_count: int = Field(
        1,
        alias="INQTRIX_REPLICA_COUNT",
        ge=1,
        description=(
            "How many replicas of this process the deployment runs — "
            "an operator-injected hint (Helm sets it from "
            "api.replicaCount), not something a single process can "
            "detect itself. Guards refuse per-replica-disk state above "
            "1: the local object-store backend would 404 every blob on "
            "the replica that did not write it."
        ),
    )
    """Deployment-injected replica count of this process (default 1). A single process cannot detect its siblings, so multi-replica safety guards (e.g. rejecting the per-replica-disk ``local`` object store) key on this hint; Helm derives it from ``api.replicaCount``."""
    object_store_backend: Literal["local", "s3"] = Field(
        "local",
        alias="INQTRIX_OBJECT_STORE_BACKEND",
        description=(
            "Binary-blob storage backend for uploaded files. ``local`` "
            "(default) writes content-addressed blobs below "
            "``object_store_path`` — no external services, suitable "
            "for dev and single-node deployments. ``s3`` targets any "
            "S3-compatible endpoint (SeaweedFS is the reference dev "
            "stack) and requires the ``s3_*`` fields. File METADATA "
            "(owner, hash, permissions) always lives in the file "
            "registry, never in the blob store."
        ),
    )
    """Binary-blob storage backend for uploaded files. ``local`` (default) writes content-addressed blobs below ``object_store_path``; ``s3`` targets any S3-compatible endpoint (SeaweedFS is the reference dev stack) and requires the ``s3_*`` fields. File METADATA (owner, hash, permissions) always lives in the file registry, never in the blob store."""
    object_store_path: str = Field(
        "data/object-store",
        alias="INQTRIX_OBJECT_STORE_PATH",
        description=(
            "Root directory of the ``local`` object-store backend, "
            "relative paths resolve against the working directory. "
            "Created on first write."
        ),
    )
    """Root directory of the ``local`` object-store backend, relative paths resolve against the working directory. Created on first write."""
    s3_auth_mode: Literal["static", "default"] = Field(
        "static",
        alias="INQTRIX_S3_AUTH_MODE",
        description=(
            "S3 credential source. ``static`` preserves the existing explicit "
            "access/secret-key contract and optionally accepts an STS session "
            "token. ``default`` passes no Inqtrix credentials to boto3 so its "
            "standard provider chain can use workload, container or instance "
            "identity."
        ),
    )
    """S3 credential source: explicit ``static`` keys or boto3's ``default`` provider chain for workload, container and instance identity."""
    s3_endpoint_url: str = Field(
        "",
        alias="INQTRIX_S3_ENDPOINT_URL",
        description=(
            "Endpoint URL of the S3-compatible service, e.g. "
            "``http://127.0.0.1:8333`` for the SeaweedFS dev stack. Empty "
            "delegates endpoint resolution to boto3, which is the normal "
            "choice for native AWS S3."
        ),
    )
    """Optional endpoint URL of the S3-compatible service. Empty delegates endpoint resolution to boto3 for native AWS S3."""
    s3_bucket: str = Field(
        "inqtrix-files",
        alias="INQTRIX_S3_BUCKET",
        description=(
            "Bucket holding every uploaded blob. Object keys are "
            "namespaced ``tenants/<tenant>/files/<uuid>`` so one "
            "bucket serves all tenants. Creation behavior is controlled by "
            "``INQTRIX_S3_BUCKET_PROVISIONING``."
        ),
    )
    """Bucket holding every uploaded blob. Object keys are namespaced ``tenants/<tenant>/files/<uuid>``; provisioning behavior is configured separately."""
    object_orphan_grace_hours: float = Field(
        24.0,
        alias="INQTRIX_OBJECT_ORPHAN_GRACE_HOURS",
        ge=0.0,
        description=(
            "Minimum object age before the worker's daily consistency "
            "sweep may delete a stored blob that has no ``files`` row "
            "(a crash between blob write and row commit leaves such an "
            "orphan). ``0`` disables the sweep. Like every prune job, "
            "the sweep runs in the ``inqtrix-worker`` process only."
        ),
    )
    """Grace age in hours for the worker's object-orphan sweep; ``0`` disables it."""
    s3_access_key: str = Field(
        "",
        alias="INQTRIX_S3_ACCESS_KEY",
        description=(
            "Access key for the S3-compatible service. Credentials "
            "never leave the backend — clients receive streamed "
            "content, not store URLs."
        ),
    )
    """Access key for the S3-compatible service. Credentials never leave the backend — clients receive streamed content, not store URLs."""
    s3_secret_key: str = Field(
        "",
        alias="INQTRIX_S3_SECRET_KEY",
        description="Secret key for the S3-compatible service.",
    )
    """Secret key for the S3-compatible service."""
    s3_session_token: str = Field(
        "",
        alias="INQTRIX_S3_SESSION_TOKEN",
        description=(
            "Optional short-lived STS session token paired with the static "
            "access and secret keys. It is rejected in default credential "
            "mode so stale explicit credentials cannot shadow workload identity."
        ),
    )
    """Optional STS session token for ``static`` authentication; rejected when boto3's default credential chain is selected."""
    s3_region: str = Field(
        "us-east-1",
        alias="INQTRIX_S3_REGION",
        description=(
            "Region name sent to the S3 client. Self-hosted stores "
            "ignore it but boto3 requires a value; the default is the "
            "conventional placeholder."
        ),
    )
    """Region name sent to the S3 client. Self-hosted stores ignore it but boto3 requires a value; the default is the conventional placeholder."""
    s3_addressing_style: Literal["path", "auto", "virtual"] = Field(
        "path",
        alias="INQTRIX_S3_ADDRESSING_STYLE",
        description=(
            "Bucket addressing mode passed to botocore. ``path`` preserves "
            "SeaweedFS/MinIO compatibility, ``auto`` is recommended for native "
            "AWS S3, and ``virtual`` forces virtual-host bucket names."
        ),
    )
    """Botocore bucket addressing mode. ``path`` is the compatibility default; managed AWS normally uses ``auto``."""
    s3_bucket_provisioning: Literal["create_if_missing", "existing"] = Field(
        "create_if_missing",
        alias="INQTRIX_S3_BUCKET_PROVISIONING",
        description=(
            "Bucket lifecycle contract. ``create_if_missing`` preserves the "
            "bundled object-store behavior and requires CreateBucket when the "
            "bucket is absent. ``existing`` never creates infrastructure and "
            "is the production default recommendation for managed S3."
        ),
    )
    """Bucket lifecycle contract: backwards-compatible ``create_if_missing`` or fail-closed ``existing`` for pre-provisioned managed buckets."""
    s3_ca_bundle: str = Field(
        "",
        alias="INQTRIX_S3_CA_BUNDLE",
        description=(
            "Optional path to a PEM CA bundle mounted in the API and worker "
            "containers. The path must be readable when S3 is enabled. TLS "
            "verification cannot be disabled."
        ),
    )
    """Optional readable PEM CA-bundle path for private S3-compatible endpoints; TLS verification cannot be disabled."""
    s3_server_side_encryption: Literal["none", "AES256", "aws:kms"] = Field(
        "none",
        alias="INQTRIX_S3_SERVER_SIDE_ENCRYPTION",
        description=(
            "Optional server-side encryption header for uploads. ``none`` "
            "relies on the bucket's default policy, ``AES256`` requests SSE-S3, "
            "and ``aws:kms`` requests SSE-KMS."
        ),
    )
    """Upload encryption header: none, SSE-S3 (``AES256``), or SSE-KMS (``aws:kms``). Bucket-default encryption remains valid with ``none``."""
    s3_kms_key_id: str = Field(
        "",
        alias="INQTRIX_S3_KMS_KEY_ID",
        description=(
            "Optional KMS key id or ARN sent only with ``aws:kms`` uploads. "
            "Empty uses the bucket/account KMS default."
        ),
    )
    """Optional KMS key id or ARN, valid only when ``INQTRIX_S3_SERVER_SIDE_ENCRYPTION=aws:kms``."""
    max_file_bytes: int = Field(
        104_857_600,
        alias="INQTRIX_MAX_FILE_BYTES",
        ge=1,
        description=(
            "Upper bound for one uploaded file (default 100 MiB). "
            "Uploads stream through a spool with running hash/size "
            "accounting, so the limit is enforced without buffering "
            "the file in memory; exceeding it aborts with HTTP 413."
        ),
    )
    """Upper bound for one uploaded file (default 100 MiB). Enforced while spooling; exceeding it aborts with HTTP 413."""
    document_parser: Literal["markitdown", "none"] = Field(
        "markitdown",
        alias="INQTRIX_DOCUMENT_PARSER",
        description=(
            "Server-side parser for uploaded files. It prepares the one "
            "canonical text that chat attachments and knowledge ingestion "
            "read, whether or not knowledge is enabled. "
            "``markitdown`` (default) converts PDF/DOCX/PPTX/XLSX/HTML to "
            "Markdown in pure Python — no ML models, no cloud dependency. "
            "``none`` disables server-side preparation; uploads keep only "
            "their original, and switching the parser on later prepares "
            "them from their stored originals."
        ),
    )
    """Server-side parser for uploaded files, independent of knowledge. ``markitdown`` (default) converts PDF/DOCX/PPTX/XLSX/HTML to Markdown in pure Python; ``none`` disables server-side preparation."""

    @model_validator(mode="after")
    def _validate_object_store_contract(self) -> "StorageSettings":
        """Reject incomplete managed-object-store configurations early."""
        if self.object_store_backend != "s3":
            return self
        bucket = self.s3_bucket.strip()
        region = self.s3_region.strip()
        endpoint = self.s3_endpoint_url.strip()
        access_key = self.s3_access_key.strip()
        secret_key = self.s3_secret_key.strip()
        session_token = self.s3_session_token.strip()
        ca_bundle = self.s3_ca_bundle.strip()
        kms_key = self.s3_kms_key_id.strip()
        if not bucket:
            raise ValueError("INQTRIX_S3_BUCKET must not be blank")
        if not region:
            raise ValueError("INQTRIX_S3_REGION must not be blank")
        if self.s3_auth_mode == "static":
            if not access_key or not secret_key:
                raise ValueError(
                    "INQTRIX_S3_AUTH_MODE=static requires "
                    "INQTRIX_S3_ACCESS_KEY and INQTRIX_S3_SECRET_KEY"
                )
        elif access_key or secret_key or session_token:
            raise ValueError(
                "INQTRIX_S3_AUTH_MODE=default must not set Inqtrix static "
                "access, secret, or session-token fields"
            )
        if self.s3_server_side_encryption != "aws:kms" and kms_key:
            raise ValueError(
                "INQTRIX_S3_KMS_KEY_ID is valid only with "
                "INQTRIX_S3_SERVER_SIDE_ENCRYPTION=aws:kms"
            )
        object.__setattr__(self, "s3_bucket", bucket)
        object.__setattr__(self, "s3_region", region)
        object.__setattr__(self, "s3_endpoint_url", endpoint)
        object.__setattr__(self, "s3_access_key", access_key)
        object.__setattr__(self, "s3_secret_key", secret_key)
        object.__setattr__(self, "s3_session_token", session_token)
        object.__setattr__(self, "s3_ca_bundle", ca_bundle)
        object.__setattr__(self, "s3_kms_key_id", kms_key)
        return self


class AuthSettings(BaseSettings):
    """Authentication configuration for the HTTP server.

    The mode selector plus the GENERIC OIDC contract surface
    Inqtrix speaks only standard OIDC — discovery,
    authorization code + PKCE — and hardwires no identity provider.
    The claim-mapping and fallback fields exist because issuer +
    client id + secret alone are not enough to exchange one IdP for
    another: Okta needs an extra groups scope, Entra omits
    ``email_verified``, Keycloak nests roles under a path.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    mode: Literal["infer", "none", "apikey", "oidc", "local"] = Field(
        "infer",
        alias="INQTRIX_AUTH_MODE",
        description=(
            "Authentication-mode selector. ``infer`` (the default) "
            "derives the mode for backwards compatibility: a "
            "non-empty ``INQTRIX_SERVER_API_KEY`` means ``apikey``, "
            "an empty one means ``none`` — existing deployments "
            "behave bit-for-bit without touching their environment. "
            "An explicit value wins over inference and survives "
            "settings serialization round-trips (the sentinel is a "
            "first-class value, not field-set introspection). "
            "``apikey`` without a configured key and ``oidc`` without "
            "its connection settings are rejected loudly at startup; "
            "``none`` "
            "with a configured key disables the gate deliberately and "
            "logs a WARNING. This raw field never reports the ACTIVE "
            "mode — consult "
            ":func:`inqtrix.auth.principal.resolve_auth_mode` or the "
            "auth provider's ``mode`` property for that."
        ),
    )
    """Authentication-mode selector. ``infer`` (the default) derives the mode for backwards compatibility: a non-empty ``INQTRIX_SERVER_API_KEY`` means ``apikey``, an empty one means ``none``. An explicit value wins over inference and survives settings serialization round-trips. ``apikey`` without a configured key and ``oidc`` without its connection settings are rejected loudly at startup; ``none`` with a configured key disables the gate deliberately and logs a WARNING. This raw field never reports the ACTIVE mode — consult :func:`inqtrix.auth.principal.resolve_auth_mode` or the auth provider's ``mode`` property for that."""
    oidc_issuer: str = Field(
        "",
        alias="INQTRIX_OIDC_ISSUER",
        description=(
            "OIDC issuer URL (e.g. ``https://login.example.com/realms/x`` "
            "or the Dex dev issuer ``http://127.0.0.1:5556/dex``). "
            "Discovery is fetched from "
            "``{issuer}/.well-known/openid-configuration`` and the "
            "document's ``issuer`` must match this value exactly. "
            "Required for ``INQTRIX_AUTH_MODE=oidc``."
        ),
    )
    """OIDC issuer URL; discovery is fetched from ``{issuer}/.well-known/openid-configuration`` and must echo this exact value. Required for mode ``oidc``."""
    oidc_client_id: str = Field(
        "",
        alias="INQTRIX_OIDC_CLIENT_ID",
        description=(
            "OAuth client id registered at the identity provider. "
            "Required for ``INQTRIX_AUTH_MODE=oidc``."
        ),
    )
    """OAuth client id registered at the identity provider. Required for mode ``oidc``."""
    oidc_client_secret: str = Field(
        "",
        alias="INQTRIX_OIDC_CLIENT_SECRET",
        description=(
            "Confidential-client secret (the BFF is a confidential "
            "client per the browser-apps BCP; tokens never reach the "
            "browser). Required for ``INQTRIX_AUTH_MODE=oidc``."
        ),
    )
    """Confidential-client secret; the BFF keeps all tokens server-side. Required for mode ``oidc``."""
    oidc_redirect_url: str = Field(
        "",
        alias="INQTRIX_OIDC_REDIRECT_URL",
        description=(
            "Absolute redirect/callback URL registered at the IdP "
            "(must match byte-for-byte — providers compare exact "
            "strings). Empty derives "
            "``{INQTRIX_PUBLIC_BASE_URL}/api/auth/callback``."
        ),
    )
    """Absolute callback URL registered at the IdP; empty derives ``{public_base_url}/api/auth/callback``."""
    oidc_scopes: str = Field(
        "openid profile email",
        alias="INQTRIX_OIDC_SCOPES",
        description=(
            "Space-separated scopes for the authorization request. "
            "Okta requires an additional ``groups`` scope for group "
            "claims; Dex uses ``groups`` too."
        ),
    )
    """Space-separated authorization scopes; add ``groups`` for IdPs that gate group claims behind a scope (Okta, Dex)."""
    oidc_username_claim: str = Field(
        "preferred_username",
        alias="INQTRIX_OIDC_USERNAME_CLAIM",
        description=(
            "Claim used as the display username; falls back to "
            "``email`` then ``sub`` when absent. Dot-separated paths "
            "descend into nested claims (Keycloak's "
            "``realm_access.roles`` style)."
        ),
    )
    """Display-username claim with ``email``/``sub`` fallback; dot paths descend into nested claims."""
    oidc_email_claim: str = Field(
        "email",
        alias="INQTRIX_OIDC_EMAIL_CLAIM",
        description="Claim carrying the user's email address.",
    )
    """Claim carrying the user's email address."""
    oidc_groups_claim: str = Field(
        "groups",
        alias="INQTRIX_OIDC_GROUPS_CLAIM",
        description=(
            "Claim carrying group memberships (list of strings). "
            "Dot-separated paths descend into nested claims."
        ),
    )
    """Group-membership claim (list of strings); dot paths descend into nested claims."""
    oidc_allowed_groups: str = Field(
        "",
        alias="INQTRIX_OIDC_ALLOWED_GROUPS",
        description=(
            "Optional comma-separated allowlist: when non-empty, a "
            "login whose group claim shares no entry with this list "
            "is rejected (visible 403 at the callback, audit-logged). "
            "Empty admits every authenticated user."
        ),
    )
    """Optional comma-separated group allowlist; non-empty rejects logins without a matching group."""
    oidc_roles_claim: str = Field(
        "roles",
        alias="INQTRIX_OIDC_ROLES_CLAIM",
        description=(
            "Claim carrying role assignments used for admin elevation. "
            "Dot-separated paths descend into nested claims (Keycloak "
            "``realm_access.roles`` or ``resource_access.<client>.roles``). "
            "May equal ``INQTRIX_OIDC_GROUPS_CLAIM`` for tenants that emit "
            "groups-as-roles."
        ),
    )
    """Role claim for admin elevation; dot paths descend into nested claims."""
    oidc_admin_roles: str = Field(
        "",
        alias="INQTRIX_OIDC_ADMIN_ROLES",
        description=(
            "Comma-separated role values that grant instance-admin on "
            "login (grant-only: a match promotes, a non-match never "
            "demotes). Matched literally against the roles claim."
        ),
    )
    """Comma-separated roles that grant instance-admin (grant-only)."""
    oidc_admin_groups: str = Field(
        "",
        alias="INQTRIX_OIDC_ADMIN_GROUPS",
        description=(
            "Comma-separated group values that grant instance-admin on "
            "login (grant-only). Matched literally against the groups "
            "claim — use the exact emitted value (Entra group GUIDs, "
            "Keycloak ``/path`` strings)."
        ),
    )
    """Comma-separated groups that grant instance-admin (grant-only)."""
    oidc_allowed_domains: str = Field(
        "",
        alias="INQTRIX_OIDC_ALLOWED_DOMAINS",
        description=(
            "Optional comma-separated email-domain allowlist, orthogonal "
            "to the group allowlist: when non-empty, a login whose email "
            "domain is not listed is rejected (visible 403). A login "
            "without an email is rejected (fail-closed). Matched "
            "case-insensitively."
        ),
    )
    """Optional comma-separated email-domain allowlist (orthogonal to groups)."""
    oidc_claim_separators: str = Field(
        " ,",
        alias="INQTRIX_OIDC_CLAIM_SEPARATORS",
        description=(
            "Characters a STRING-valued group/role claim is split on (a "
            "JSON array is used as-is). Default whitespace and comma, so "
            "``admin, staff`` and ``admin staff`` both parse."
        ),
    )
    """Separators for string-valued group/role claims (default whitespace + comma)."""
    oidc_groups_strip_path_prefix: bool = Field(
        False,
        alias="INQTRIX_OIDC_GROUPS_STRIP_PATH_PREFIX",
        description=(
            "Strip a single leading ``/`` from each group value "
            "(Keycloak full-path groups like ``/Engineering/Backend``). "
            "Explicit rather than a silent normalisation; match the "
            "allowlist to whichever form you keep."
        ),
    )
    """Strip one leading ``/`` from group values (Keycloak full-path groups)."""
    oidc_provider_name: str = Field(
        "",
        alias="INQTRIX_OIDC_PROVIDER_NAME",
        description=(
            "Display name for the SSO login button, surfaced by the "
            "public auth-config endpoint (e.g. ``Okta``, ``Entra ID``). "
            "Empty leaves the frontend's generic SSO label."
        ),
    )
    """SSO login-button display name surfaced by the auth-config endpoint."""
    oidc_skip_email_verified: bool = Field(
        False,
        alias="INQTRIX_OIDC_SKIP_EMAIL_VERIFIED",
        description=(
            "Accept logins whose token lacks ``email_verified=true``. "
            "Required for Entra ID, which omits the claim entirely; "
            "leave off for IdPs that emit it."
        ),
    )
    """Accept logins without ``email_verified=true`` (Entra ID omits the claim)."""
    oidc_discovery_url: str = Field(
        "",
        alias="INQTRIX_OIDC_DISCOVERY_URL",
        description=(
            "Override for the discovery-document URL when the IdP "
            "serves metadata somewhere other than "
            "``{issuer}/.well-known/openid-configuration``. The "
            "document's ``issuer`` must still match "
            "``INQTRIX_OIDC_ISSUER``."
        ),
    )
    """Discovery-document URL override; the document's ``issuer`` must still match the configured issuer."""
    oidc_userinfo_fallback: bool = Field(
        True,
        alias="INQTRIX_OIDC_USERINFO_FALLBACK",
        description=(
            "Fetch the userinfo endpoint when the id_token lacks the "
            "mapped claims (Okta thin tokens, Entra group overflow). "
            "Off saves one IdP round-trip per login."
        ),
    )
    """Fetch userinfo when the id_token lacks mapped claims (Okta thin tokens)."""
    oidc_ca_cert: str = Field(
        "",
        alias="INQTRIX_OIDC_CA_CERT",
        description=(
            "Path to a PEM CA bundle for the IdP's TLS endpoints "
            "(on-prem IdPs behind a private CA). Empty uses the "
            "system trust store."
        ),
    )
    """PEM CA bundle path for IdPs behind a private CA; empty uses the system trust store."""
    oidc_insecure_dev_cookies: bool = Field(
        False,
        alias="INQTRIX_OIDC_INSECURE_DEV_COOKIES",
        description=(
            "Development escape hatch: drop the ``Secure`` flag and "
            "the ``__Host-`` prefix so login works over plain "
            "``http://127.0.0.1`` in every browser (Safari rejects "
            "Secure cookies on loopback HTTP). NEVER in production; "
            "activation logs a WARNING at startup."
        ),
    )
    """Development escape hatch: drop ``Secure``/``__Host-`` cookie hardening for plain-HTTP loopback; logs a WARNING."""
    session_secret: str = Field(
        "",
        alias="INQTRIX_SESSION_SECRET",
        description=(
            "Server-side secret for CSRF-token derivation (OWASP "
            "signed double-submit: HMAC over the session id). "
            "Required for ``INQTRIX_AUTH_MODE=oidc``; rotate to "
            "invalidate outstanding CSRF tokens (sessions survive)."
        ),
    )
    """Server-side secret for CSRF-token derivation (signed double-submit). Required for mode ``oidc``."""
    session_max_age_seconds: int = Field(
        28_800,
        alias="INQTRIX_SESSION_MAX_AGE_SECONDS",
        ge=300,
        description=(
            "Absolute server-side session lifetime (default 8 hours). "
            "Expired sessions resolve to 401 and the SPA re-runs the "
            "login redirect; no silent refresh in this iteration."
        ),
    )
    """Absolute server-side session lifetime in seconds (default 8 hours)."""
    pat_pepper: str = Field(
        "",
        alias="INQTRIX_PAT_PEPPER",
        description=(
            "Server-side secret mixed into the HMAC of every personal "
            "access token. REQUIRED in oidc mode (fail-loud at "
            "startup): without a pepper a database leak alone would "
            "let stolen hashes be brute-forced offline against short "
            "secrets. Rotating the pepper invalidates EVERY issued "
            "token — clients see uniform 401s; plan rotations as "
            "deliberate maintenance."
        ),
    )
    """Server-side secret mixed into the HMAC of every personal access token. Required in oidc mode (fail-loud at startup); a database leak alone must not suffice to verify guesses against stolen hashes. Rotating the pepper invalidates every issued token at once — clients receive uniform 401s, so treat rotation as deliberate maintenance with re-issue."""
    pseudonym_pepper: str = Field(
        "",
        alias="INQTRIX_PSEUDONYM_PEPPER",
        description=(
            "Instance-wide secret behind the pseudonymous subject "
            "references in logs, traces, and audit correlation. Set the "
            "SAME value for the API server and every worker: one person "
            "then carries ONE stable pseudonym across processes and "
            "restarts. Empty keeps the historical per-process references "
            "(a startup WARNING explains the degraded correlation). "
            "REQUIRED in oidc mode (fail-loud at startup). Rotating the "
            "pepper deliberately breaks linkability with all previously "
            "written pseudonyms."
        ),
    )
    """Instance-wide secret behind the stable subject pseudonyms in logs/traces/audit correlation. Shared by API server and workers; empty falls back to per-process references (WARNING at startup). Required in oidc mode; rotation deliberately breaks linkability with previously written pseudonyms."""
    pat_max_per_user: int = Field(
        10,
        alias="INQTRIX_PAT_MAX_PER_USER",
        ge=1,
        description=(
            "Cap on ACTIVE (non-revoked, non-expired) personal access "
            "tokens per user. A guardrail against unbounded token "
            "sprawl, not a security boundary — concurrent creates "
            "across replicas may briefly exceed it."
        ),
    )
    """Cap on active (non-revoked, non-expired) personal access tokens per user. A sprawl guardrail, not a security boundary: the count-then-insert check may briefly overshoot under concurrent creates across replicas."""
    pat_default_ttl_days: int = Field(
        0,
        alias="INQTRIX_PAT_DEFAULT_TTL_DAYS",
        ge=0,
        description=(
            "Default lifetime applied when a token is created WITHOUT "
            "an explicit expiry. 0 (default) means such tokens never "
            "expire; a positive value sets the default in days. An "
            "explicit per-token expiry always wins — this is a "
            "default, not a cap."
        ),
    )
    """Default lifetime in days for personal access tokens created without an explicit expiry; 0 (default) keeps them non-expiring. An explicit per-token expiry always wins — this field is a default, deliberately not a cap."""
    registration: Literal["open", "invite"] = Field(
        "open",
        alias="INQTRIX_REGISTRATION",
        description=(
            "Admission policy for FIRST-time OIDC logins. ``open`` "
            "(default, the historical behaviour) admits every "
            "IdP-authenticated user; ``invite`` admits unknown users "
            "only when an open invitation matches their email and "
            "rejects everyone else with 403 BEFORE any user record is "
            "created. ``invite`` requires oidc mode AND the postgres "
            "storage backend (memory invitations would evaporate on "
            "restart and lock everyone out) — both contradictions "
            "fail loudly at startup."
        ),
    )
    """Admission policy for first-time OIDC logins. ``open`` (default) keeps the historical admit-everyone behaviour; ``invite`` requires a matching open invitation for unknown users (403 otherwise, before any user record exists) and is only valid with oidc mode plus the postgres storage backend — both contradictions are rejected loudly at startup."""
    local_registration: Literal["closed", "open"] = Field(
        "closed",
        alias="INQTRIX_LOCAL_REGISTRATION",
        description=(
            "Self-signup policy for ``INQTRIX_AUTH_MODE=local``. "
            "``closed`` (default, secure): the first-run setup creates "
            "the owner, and every further account is created by an admin "
            "or via an invitation — there is no public registration "
            "route. ``open``: a public self-signup route is mounted "
            "(logged loudly at startup) so anyone can create an account. "
            "Independent of the OIDC ``registration`` field above."
        ),
    )
    """Self-signup policy for ``local`` mode. ``closed`` (default): owner via first-run setup, further accounts admin-created/invited, no public registration. ``open``: a public self-signup route is mounted (logged loudly). Independent of the OIDC ``registration`` field."""

    login_rate_limit_enabled: bool = Field(
        True,
        alias="INQTRIX_LOGIN_RATE_LIMIT_ENABLED",
        description=(
            "Throttle failed password-mode logins (local) per "
            "``(identifier, source_ip)``. On by default (secure default); a "
            "multi-replica deployment should ALSO throttle per-IP at the "
            "reverse proxy, since the counters are process-local."
        ),
    )
    """Throttle failed local logins per (identifier, ip). On by default."""
    login_rate_limit_max_attempts: int = Field(
        10,
        alias="INQTRIX_LOGIN_RATE_LIMIT_MAX_ATTEMPTS",
        ge=1,
        description=(
            "Failed attempts within the window that trip a lockout. The "
            "default 10 tolerates fat-fingering while stopping dictionary runs."
        ),
    )
    """Failed attempts within the window that trip a lockout (default 10)."""
    login_rate_limit_window_seconds: int = Field(
        300,
        alias="INQTRIX_LOGIN_RATE_LIMIT_WINDOW_SECONDS",
        ge=1,
        description="Rolling window (seconds) over which failed attempts accumulate.",
    )
    """Rolling window (seconds) over which failed attempts accumulate (default 300)."""
    login_rate_limit_lockout_seconds: int = Field(
        60,
        alias="INQTRIX_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS",
        ge=1,
        description="How long a tripped ``(identifier, ip)`` stays locked (seconds).",
    )
    """How long a tripped (identifier, ip) stays locked, in seconds (default 60)."""
    trusted_proxy_hops: int = Field(
        1,
        alias="INQTRIX_TRUSTED_PROXY_HOPS",
        ge=0,
        description=(
            "Number of trusted reverse proxies between the client and this "
            "server, used to read the real client IP from the RIGHT of "
            "``X-Forwarded-For`` for the login throttle key. ``1`` (default) "
            "fits the selected bundled single proxy (the packaged Python "
            "web gateway or the explicit nginx alternative) and is not "
            "client-spoofable. "
            "Set ``0`` when exposing the API server directly with no proxy "
            "(only the socket peer is trusted); set the exact chain length "
            "for multiple proxies — too high re-opens spoofing."
        ),
    )
    """Trusted reverse-proxy hop count for resolving the client IP from the right of X-Forwarded-For (login throttle). 1 = one bundled proxy (default); 0 = direct exposure; N = exact chain length."""


class KnowledgeSettings(BaseSettings):
    """Knowledge-engine (internal document retrieval) configuration.

    Disabled by default — the knowledge surface, the embedding
    provider, and the ``mode=knowledge`` algorithm only exist when
    ``enabled`` is true, keeping the historical deployment shape
    untouched. Like every Settings group, this is the only env-coupled
    surface; the providers it configures receive everything via
    constructor arguments.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    enabled: bool = Field(
        False,
        alias="INQTRIX_KNOWLEDGE_ENABLED",
        description=(
            "Master switch for the knowledge engine. When false "
            "(default) no knowledge routes are registered, no "
            "embedding provider is constructed, and ``mode=knowledge`` "
            "is not a registered algorithm — requests naming it get "
            "the standard mode-validation 400."
        ),
    )
    """Master switch for the knowledge engine. When false (default) no knowledge routes are registered, no embedding provider is constructed, and ``mode=knowledge`` is not a registered algorithm — requests naming it get the standard mode-validation 400."""
    vector_backend: Literal["memory", "qdrant"] = Field(
        "memory",
        alias="INQTRIX_VECTOR_BACKEND",
        description=(
            "Vector/document store backend. ``memory`` (in-process, "
            "lost on restart, default) needs no services; ``qdrant`` "
            "persists collections/documents/chunks in a Qdrant "
            "instance and enables hybrid (dense + BM25) retrieval — "
            "requires the ``knowledge-qdrant`` extra and "
            "``qdrant_url``."
        ),
    )
    """Vector/document store backend. ``memory`` (in-process, lost on restart, default) needs no services; ``qdrant`` persists in a Qdrant instance and enables hybrid (dense + BM25) retrieval — requires the ``knowledge-qdrant`` extra and ``qdrant_url``."""
    qdrant_url: str = Field(
        "http://127.0.0.1:6333",
        alias="INQTRIX_QDRANT_URL",
        description=(
            "Qdrant REST endpoint for ``vector_backend=qdrant``. The "
            "default matches the loopback dev compose stack."
        ),
    )
    """Qdrant REST endpoint for ``vector_backend=qdrant``. The default matches the loopback dev compose stack."""
    qdrant_api_key: str = Field(
        "",
        alias="INQTRIX_QDRANT_API_KEY",
        description=(
            "Qdrant API key. Self-hosted Qdrant is UNAUTHENTICATED by "
            "default — set this everywhere except pure loopback dev "
            "(an empty key is accepted but logged loudly)."
        ),
    )
    """Qdrant API key. Self-hosted Qdrant is UNAUTHENTICATED by default — set this everywhere except pure loopback dev (an empty key is accepted but logged loudly)."""
    sparse: Literal["bm25_german", "off"] = Field(
        "bm25_german",
        alias="INQTRIX_KNOWLEDGE_SPARSE",
        description=(
            "Lexical retrieval branch for the qdrant backend. "
            "``bm25_german`` (default) has the Qdrant server compute BM25 "
            "sparse vectors itself (core inference, server >= 1.15.3; a "
            "tokenizer/stemmer ALGORITHM, no model, no download) and fuses "
            "them with the dense branch via RRF. Startup fails fast with an "
            "actionable error when a reachable server is older than 1.15.3. "
            "``off`` runs dense-only. Ignored by the memory backend."
        ),
    )
    """Lexical retrieval branch for the qdrant backend. ``bm25_german`` (default) has the Qdrant server compute BM25 sparse vectors itself (server >= 1.15.3, checked at startup) and fuses via RRF; ``off`` runs dense-only. Ignored by the memory backend."""
    reranker_provider: Literal["none", "cohere", "llm"] = Field(
        "none",
        alias="INQTRIX_RERANKER_PROVIDER",
        description=(
            "Optional rerank stage after retrieval. ``none`` (default) "
            "skips the stage — a visible capability flag, never a "
            "silent downgrade. ``cohere`` calls a Cohere-rerank-schema "
            "endpoint (native or Azure AI Foundry serverless) and "
            "requires the ``reranker_*`` fields. ``llm`` ranks "
            "listwise through the deployment's own LLM — a fallback "
            "for deployments without a rerank API contract: roughly "
            "an order of magnitude more expensive and slower than a "
            "specialized cross-encoder and hard-capped at 20 "
            "candidates per query, so deeper candidate pools are "
            "truncated (visibly logged)."
        ),
    )
    """Optional rerank stage after retrieval. ``none`` (default) skips the stage; ``cohere`` calls a Cohere-rerank-schema endpoint (native or Azure AI Foundry serverless) and requires the ``reranker_*`` fields; ``llm`` ranks listwise through the deployment's own LLM — a fallback without extra infrastructure, roughly an order of magnitude costlier/slower than a cross-encoder and hard-capped at 20 candidates (deeper pools are truncated with a visible log line)."""
    reranker_base_url: str = Field(
        "",
        alias="INQTRIX_RERANKER_BASE_URL",
        description=(
            "Rerank endpoint base, e.g. ``https://api.cohere.com`` or "
            "an Azure serverless deployment URL. Required when "
            "``reranker_provider`` is not ``none``."
        ),
    )
    """Rerank endpoint base, e.g. ``https://api.cohere.com`` or an Azure serverless deployment URL. Required when ``reranker_provider`` is not ``none``."""
    reranker_api_key: str = Field(
        "",
        alias="INQTRIX_RERANKER_API_KEY",
        description="API key for the rerank endpoint.",
    )
    """API key for the rerank endpoint."""
    reranker_model: str = Field(
        "",
        alias="INQTRIX_RERANKER_MODEL",
        description=(
            "Rerank model/deployment id, e.g. ``rerank-v3.5`` or the "
            "Azure deployment name. Required when ``reranker_provider`` "
            "is not ``none``."
        ),
    )
    """Rerank model/deployment id, e.g. ``rerank-v3.5`` or the Azure deployment name. Required when ``reranker_provider`` is not ``none``."""
    contextualize: Literal["on", "off"] = Field(
        "off",
        alias="INQTRIX_KNOWLEDGE_CONTEXTUALIZE",
        description=(
            "Contextual retrieval: at ingestion, one batched fast-tier "
            "LLM call per document generates a short situating context "
            "per chunk, prepended before embedding and BM25 indexing. "
            "Improves retrieval of context-dependent chunks at a "
            "one-time ingestion cost; off by default until the "
            "deployment opts into the extra LLM usage. Existing "
            "documents are unaffected — re-ingest to contextualize."
        ),
    )
    """Contextual retrieval: one batched fast-tier LLM call per document at ingestion generates situating contexts prepended before embedding and BM25 indexing. Off by default; re-ingest existing documents to apply."""
    contextualization_circuit_cooldown_seconds: float = Field(
        60.0,
        alias="INQTRIX_CONTEXTUALIZATION_CIRCUIT_COOLDOWN_SECONDS",
        ge=1.0,
        le=3_600.0,
        description=(
            "Cooldown after a transient contextualization provider failure. "
            "During this interval the tenant/provider/model circuit rejects "
            "new provider calls and indexing pauses visibly; it never "
            "publishes a partial or raw fallback. The next explicit resume "
            "after the cooldown receives one half-open probe."
        ),
    )
    """Provider/model circuit cooldown in seconds; it pauses work and never acts as an indexing deadline."""
    contextualization_circuit_probe_lease_seconds: float = Field(
        900.0,
        alias="INQTRIX_CONTEXTUALIZATION_CIRCUIT_PROBE_LEASE_SECONDS",
        ge=30.0,
        le=7_200.0,
        description=(
            "Lease for the single half-open contextualization probe. The "
            "runtime raises the effective lease to at least the provider call "
            "budget plus a safety margin, so another worker cannot start a "
            "duplicate probe while the first request is legitimately in "
            "flight. An expired lease is recoverable after a worker crash."
        ),
    )
    """Configured minimum lease for the single half-open provider probe."""
    gate: Literal["on", "off"] = Field(
        "on",
        alias="INQTRIX_KNOWLEDGE_GATE",
        description=(
            "Sufficiency gate for mode=knowledge: one fast-tier LLM "
            "call judges the retrieved evidence and may trigger one "
            "second retrieval pass; insufficient evidence yields the "
            "honest no-evidence answer instead of a fabricated one. "
            "``off`` restores the always-answer-from-top-k behaviour "
            "(saves one small LLM call per question)."
        ),
    )
    """Sufficiency gate for mode=knowledge: one fast-tier LLM call judges the retrieved evidence and may trigger one second retrieval pass; insufficient evidence yields the honest no-evidence answer. ``off`` restores always-answer-from-top-k."""
    grounding: Literal["on", "off"] = Field(
        "on",
        alias="INQTRIX_KNOWLEDGE_GROUNDING",
        description=(
            "Quote-then-answer grounding for mode=knowledge: the "
            "answer prompt requires a block of verbatim, labelled "
            "quotes before the answer; the quotes are verified "
            "deterministically against the evidence. One bounded "
            "header-format repair is permitted; malformed grounding or "
            "an unverifiable quote fails the answer visibly and emits a "
            "typed run event instead of publishing unchecked model text. "
            "Verified quotes are stripped from the user-facing answer. "
            "``off`` restores the plain single-section answer prompt."
        ),
    )
    """Quote-then-answer grounding for mode=knowledge. One deterministic header repair is allowed; malformed grounding or any unverifiable quote fails closed with a typed event and never publishes unchecked model text. Verified quotes are stripped from the user-facing answer. ``off`` restores the plain single-section answer prompt."""
    gate_max_rounds: int = Field(
        3,
        alias="INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS",
        ge=1,
        le=5,
        description=(
            "Hard operator cap on gate rewrite-and-retrieve rounds for "
            "EVERY retrieval profile (the deep profile requests up to "
            "this many; standard always uses one). Each round costs "
            "one fast-tier gate call plus one retrieval pass, so the "
            "cap bounds the worst-case cost of an agentic run. "
            "``ge=1`` keeps the standard profile's single rewrite "
            "always possible while the gate itself is enabled."
        ),
    )
    """Hard operator cap on gate rewrite-and-retrieve rounds for every retrieval profile. The deep profile requests up to this many rounds, standard exactly one; each round costs one fast-tier gate call plus one retrieval pass, so this bounds the worst-case cost of an agentic run. Lower bound 1 keeps standard's single rewrite possible while the gate is enabled."""
    rerank_candidate_depth: int = Field(
        40,
        alias="INQTRIX_RERANK_CANDIDATE_DEPTH",
        ge=5,
        le=200,
        description=(
            "Candidate pool retrieved before the rerank stage reduces "
            "it to the requested top_k. Larger pools improve recall at "
            "the cost of rerank latency/cost per query."
        ),
    )
    """Candidate pool retrieved before the rerank stage reduces it to the requested top_k. Larger pools improve recall at the cost of rerank latency/cost per query."""
    embedding_provider: Literal["openai_compatible", "azure"] = Field(
        "openai_compatible",
        alias="INQTRIX_EMBEDDING_PROVIDER",
        description=(
            "How the embedding endpoint authenticates. "
            "``openai_compatible`` (default) posts to "
            "``{base_url}/embeddings`` with a Bearer key (LiteLLM "
            "proxy, OpenAI, vLLM, Azure v1 surface). ``azure`` uses "
            "Azure deployment-based auth (api-key header + "
            "api-version) for resources without the OpenAI-compatible "
            "surface; it reads the ``embedding_azure_*`` fields, which "
            "fall back to the established ``AZURE_AI_PROJECT_*`` / "
            "``AZURE_OPENAI_API_KEY`` variables so an existing Azure "
            "deployment needs exactly this one extra setting."
        ),
    )
    """How the embedding endpoint authenticates. ``openai_compatible`` (default) posts to ``{base_url}/embeddings`` with a Bearer key. ``azure`` uses Azure deployment-based auth (api-key header + api-version) and reads the ``embedding_azure_*`` fields, which fall back to ``AZURE_AI_PROJECT_*`` / ``AZURE_OPENAI_API_KEY``."""
    embedding_azure_endpoint: str = Field(
        "",
        validation_alias=AliasChoices(
            "INQTRIX_EMBEDDING_AZURE_ENDPOINT",
            "AZURE_AI_PROJECT_ENDPOINT",
        ),
        description=(
            "Azure resource endpoint for ``embedding_provider=azure``. "
            "An AI-Foundry PROJECT endpoint is accepted — the provider "
            "reduces it to the resource root where deployments live. "
            "Falls back to ``AZURE_AI_PROJECT_ENDPOINT``."
        ),
    )
    """Azure resource endpoint for ``embedding_provider=azure``. An AI-Foundry PROJECT endpoint is accepted — the provider reduces it to the resource root. Falls back to ``AZURE_AI_PROJECT_ENDPOINT``."""
    embedding_azure_api_key: str = Field(
        "",
        validation_alias=AliasChoices(
            "INQTRIX_EMBEDDING_AZURE_API_KEY",
            "AZURE_AI_PROJECT_API_KEY",
            "AZURE_OPENAI_API_KEY",
        ),
        description=(
            "API key for ``embedding_provider=azure``. Falls back to "
            "``AZURE_AI_PROJECT_API_KEY`` and then "
            "``AZURE_OPENAI_API_KEY``."
        ),
    )
    """API key for ``embedding_provider=azure``. Falls back to ``AZURE_AI_PROJECT_API_KEY`` and then ``AZURE_OPENAI_API_KEY``."""
    embedding_azure_api_version: str = Field(
        "2024-10-21",
        alias="INQTRIX_EMBEDDING_AZURE_API_VERSION",
        description=(
            "Azure OpenAI data-plane API version for the embeddings "
            "deployment. The default is the 2024-10-21 GA version."
        ),
    )
    """Azure OpenAI data-plane API version for the embeddings deployment. The default is the 2024-10-21 GA version."""
    embedding_base_url: str = Field(
        "",
        alias="INQTRIX_EMBEDDING_BASE_URL",
        description=(
            "OpenAI-compatible ``/embeddings`` endpoint base URL. "
            "Empty (default) reuses ``LITELLM_BASE_URL`` so a standard "
            "LiteLLM-proxy deployment needs no extra configuration."
        ),
    )
    """OpenAI-compatible ``/embeddings`` endpoint base URL. Empty (default) reuses ``LITELLM_BASE_URL`` so a standard LiteLLM-proxy deployment needs no extra configuration."""
    embedding_api_key: str = Field(
        "",
        alias="INQTRIX_EMBEDDING_API_KEY",
        description=(
            "API key for the embeddings endpoint. Empty (default) "
            "reuses ``LITELLM_API_KEY``."
        ),
    )
    """API key for the embeddings endpoint. Empty (default) reuses ``LITELLM_API_KEY``."""
    embedding_model: str = Field(
        "text-embedding-3-small",
        alias="INQTRIX_EMBEDDING_MODEL",
        description=(
            "Default embedding model for NEW collections. Each "
            "collection stores its model immutably at creation; "
            "changing this default never affects existing collections."
        ),
    )
    """Default embedding model for NEW collections. Each collection stores its model immutably at creation; changing this default never affects existing collections."""
    selectable_embedding_models: str = Field(
        "",
        alias="INQTRIX_SELECTABLE_EMBEDDING_MODELS",
        description=(
            "Comma-separated embedding-model ids the UI may offer in "
            "the collection-creation picker (annotated via the "
            "embedding catalog). Empty hides the picker — collections "
            "then always use ``embedding_model``."
        ),
    )
    """Comma-separated embedding-model ids the UI may offer in the collection-creation picker (annotated via the embedding catalog). Empty hides the picker — collections then always use ``embedding_model``."""
    default_top_k: int = Field(
        8,
        alias="INQTRIX_KNOWLEDGE_TOP_K",
        ge=1,
        le=50,
        description=(
            "Default number of evidence chunks retrieved per question. "
            "Requests may override per call via "
            "``knowledge_filters.top_k``."
        ),
    )
    """Default number of evidence chunks retrieved per question. Requests may override per call via ``knowledge_filters.top_k``."""
    chunk_max_chars: int = Field(
        2_000,
        alias="INQTRIX_KNOWLEDGE_CHUNK_MAX_CHARS",
        ge=200,
        le=20_000,
        description=(
            "Character budget per document chunk at ingestion. Must "
            "stay below the embedding model's input limit; the "
            "default (~500 tokens) fits every catalogued model."
        ),
    )
    """Character budget per document chunk at ingestion. Must stay below the embedding model's input limit; the default (~500 tokens) fits every catalogued model."""
    max_document_chars: int = Field(
        2_000_000,
        alias="INQTRIX_KNOWLEDGE_MAX_DOCUMENT_CHARS",
        ge=10_000,
        description=(
            "Upper bound on one uploaded document's text size. "
            "Applied when immutable source intent is reserved, before "
            "contextualization or embedding work is queued."
        ),
    )
    """Upper bound on one document revision's canonical source text, enforced before provider work is queued."""
    reindex_max_concurrent: int = Field(
        6,
        alias="INQTRIX_REINDEX_MAX_CONCURRENT",
        ge=1,
        description=(
            "Maximum number of indexing operations admitted for concurrent "
            "execution. Collection-generation builds are serialized per "
            "collection, while immutable document-revision deltas may run "
            "alongside one generation and publish through their own revision "
            "fences. Concurrent operations add up on the embedding endpoint "
            "and compete with live query embedding; raise this when the "
            "provider has headroom, or lower it when background indexing "
            "starves interactive retrieval or reaches provider limits. In "
            "worker mode (INQTRIX_QUEUE_BACKEND=valkey) the indexing "
            "loop runs at the LOWER of this and "
            "INQTRIX_WORKER_CONCURRENCY, so this value bounds "
            "execution as well as admission. Additional jobs wait in "
            "the FIFO queue."
        ),
    )
    """Admission limit for concurrently executing indexing operations; one collection generation may coexist with independently fenced document revisions."""
    reindex_queue_max_size: int = Field(
        50,
        alias="INQTRIX_REINDEX_QUEUE_MAX_SIZE",
        ge=0,
        description=(
            "Maximum number of indexing operations waiting in the FIFO queue. "
            "Active jobs governed by ``reindex_max_concurrent`` do not "
            "count. When the queue is full, submission returns "
            "HTTP 429."
        ),
    )
    """Maximum number of indexing operations waiting in the FIFO queue; active operations do not count against the limit."""
    reindex_completed_ttl_seconds: int = Field(
        3_600,
        alias="INQTRIX_REINDEX_COMPLETED_TTL_SECONDS",
        ge=0,
        description=(
            "TTL (seconds) for terminal (completed/failed/cancelled) "
            "indexing-operation records. During this window the UI can still "
            "fetch the job, replay events, and read its history entry. "
            "Longer than the run TTL because a returning browser shows "
            "recent reindex history. Governs retention in both the "
            "in-memory store and the durable Postgres store "
            "(INQTRIX_STORAGE_BACKEND=postgres); the per-collection "
            "INQTRIX_REINDEX_HISTORY_LIMIT caps history independently. "
            "Queued, running, cancelling, and paused operations are never "
            "expired by this value."
        ),
    )
    """TTL for terminal indexing-operation records only; non-terminal work has no age deadline."""
    reindex_event_buffer_size: int = Field(
        200,
        alias="INQTRIX_REINDEX_EVENT_BUFFER_SIZE",
        ge=1,
        description=(
            "Number of recent structured events retained per indexing "
            "job for late SSE subscribers. Bounded to avoid unbounded "
            "memory growth during long-running provider work."
        ),
    )
    """Number of recent structured events retained per indexing operation for late SSE subscribers."""
    reindex_history_limit: int = Field(
        10,
        alias="INQTRIX_REINDEX_HISTORY_LIMIT",
        ge=0,
        description=(
            "Maximum number of terminal indexing records retained per "
            "collection (the inline 'last N runs' history the UI "
            "shows). Older terminal records for a collection are evicted "
            "beyond this count even before their TTL expires."
        ),
    )
    """Maximum number of terminal indexing records retained per collection before TTL eviction."""
    reindex_auto_resume_attempts: int = Field(
        5,
        alias="INQTRIX_REINDEX_AUTO_RESUME_ATTEMPTS",
        ge=0,
        le=20,
        description=(
            "Automatic resumes of an indexing job a provider, vector-store "
            "or quota outage paused (paused_dependency), in a row without "
            "progress. Waits grow from 60 seconds to 15 minutes; progress "
            "starts a new count; a validation pause is never resumed "
            "automatically. 0 turns automatic resumption off: every pause "
            "then waits for a person."
        ),
    )
    """Bound on automatic resumes of one dependency-paused indexing job (0 = off)."""

    generation_rollback_retention_seconds: int = Field(
        7 * 24 * 60 * 60,
        alias="INQTRIX_GENERATION_ROLLBACK_RETENTION_SECONDS",
        ge=0,
        description=(
            "How long a superseded, fully validated knowledge generation "
            "remains rollback-available. Expiry is serviced by the existing "
            "worker maintenance cadence; interrupted vector cleanup stays "
            "visibly retryable and never re-enters rollback availability."
        ),
    )
    """Rollback window for superseded knowledge generations (default seven days)."""

    def selectable_embedding_model_list(self) -> list[str]:
        """Parse the comma-separated selectable-models field."""
        return [
            item.strip()
            for item in (self.selectable_embedding_models or "").split(",")
            if item.strip()
        ]


class QueueSettings(BaseSettings):
    """Run-queue backend selection and worker tuning.

    Two orthogonal switches keep the zero-infrastructure default
    intact: run *records* become durable via
    ``INQTRIX_STORAGE_BACKEND=postgres`` (execution stays in-process),
    and run *execution* moves to separate worker processes via
    ``INQTRIX_QUEUE_BACKEND=valkey`` (which requires the Postgres run
    store, validated loudly at startup). Both unset = today's
    in-memory, in-process behaviour bit-for-bit.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    backend: Literal["memory", "valkey"] = Field(
        "memory",
        alias="INQTRIX_QUEUE_BACKEND",
        description=(
            "Job-queue backend for native runs. ``memory`` (default) "
            "executes runs in-process exactly as before. ``valkey`` "
            "dispatches accepted runs to a Valkey Stream consumed by "
            "``inqtrix-worker`` processes; requires "
            "``INQTRIX_STORAGE_BACKEND=postgres`` (the run row is the "
            "source of truth, the stream only carries dispatch "
            "messages) and a non-empty ``INQTRIX_VALKEY_URL``."
        ),
    )
    """Job-queue backend for native runs. ``memory`` (default) executes runs in-process exactly as before. ``valkey`` dispatches accepted runs to a Valkey Stream consumed by ``inqtrix-worker`` processes; requires ``INQTRIX_STORAGE_BACKEND=postgres`` and a non-empty ``INQTRIX_VALKEY_URL``."""
    valkey_url: str = Field(
        "",
        alias="INQTRIX_VALKEY_URL",
        description=(
            "Valkey connection URL (``redis://`` scheme, e.g. "
            "``redis://127.0.0.1:6379/0``) for the job queue. Required "
            "when the queue backend is ``valkey``; ignored otherwise."
        ),
    )
    """Valkey connection URL (``redis://`` scheme, e.g. ``redis://127.0.0.1:6379/0``) for the job queue. Required when the queue backend is ``valkey``; ignored otherwise."""
    worker_concurrency: int = Field(
        100,
        alias="INQTRIX_WORKER_CONCURRENCY",
        ge=1,
        description=(
            "Process-wide execution ceiling for one worker replica. It "
            "sizes the research loop directly and caps every other loop "
            "in the process: indexing is additionally bounded by "
            "INQTRIX_REINDEX_MAX_CONCURRENT, upload by "
            "INQTRIX_UPLOAD_MAX_CONCURRENT, deletion by "
            "INQTRIX_DELETION_MAX_CONCURRENT, and each loop runs at the "
            "LOWER of the two. A research run blocks one thread while it "
            "waits on providers, so raising this mostly buys provider "
            "parallelism; the domain ceilings exist because the other "
            "loops spend real CPU instead of waiting."
        ),
    )
    """Process-wide execution ceiling for one worker replica. It sizes the research loop directly and caps the indexing, upload and deletion loops, each of which runs at the lower of this and its own domain ceiling."""
    worker_metrics_port: int = Field(
        0,
        alias="INQTRIX_WORKER_METRICS_PORT",
        ge=0,
        le=65535,
        description=(
            "Port for the worker process's own Prometheus /metrics "
            "endpoint (each worker exposes its own registry; no "
            "pushgateway). 0 (default) disables the endpoint. Also "
            "requires INQTRIX_METRICS_ENABLED=true and the `metrics` "
            "extra."
        ),
    )
    """Worker /metrics port (0 = off; needs INQTRIX_METRICS_ENABLED)."""
    worker_max_attempts: int = Field(
        3,
        alias="INQTRIX_WORKER_MAX_ATTEMPTS",
        ge=1,
        description=(
            "Delivery attempts per run before the worker dead-letters "
            "the job and marks the run failed. At-least-once delivery "
            "means crashes redeliver; the run-row state machine makes "
            "redelivery of finished runs a no-op, so this bound only "
            "fires for runs that repeatedly crash a worker."
        ),
    )
    """Delivery attempts per run before the worker dead-letters the job and marks the run failed. Redelivery of finished runs is a no-op; this bound only fires for runs that repeatedly crash a worker."""
    worker_heartbeat_seconds: float = Field(
        15.0,
        alias="INQTRIX_WORKER_HEARTBEAT_SECONDS",
        gt=0,
        description=(
            "Interval at which a worker re-claims its own in-flight "
            "stream entries (XCLAIM JUSTID) to reset their idle time. "
            "Keeps long research runs from being stolen while still "
            "letting crashed workers be detected quickly."
        ),
    )
    """Interval at which a worker re-claims its own in-flight stream entries (XCLAIM JUSTID) to reset their idle time."""
    worker_claim_idle_seconds: float = Field(
        90.0,
        alias="INQTRIX_WORKER_CLAIM_IDLE_SECONDS",
        gt=0,
        description=(
            "Idle threshold after which another worker may reclaim a "
            "pending stream entry (XAUTOCLAIM). Sized to heartbeat "
            "loss (several missed heartbeats), NOT to the maximum run "
            "duration — the heartbeat keeps legitimate long runs "
            "below this idle time."
        ),
    )
    """Idle threshold after which another worker may reclaim a pending stream entry (XAUTOCLAIM). Sized to heartbeat loss, not to maximum run duration."""


class QuotaSettings(BaseSettings):
    """Per-user usage quotas for multi-user (cookie-session) deployments.

    The operator ceiling layer of the two-level rule (the admin UI sets
    the middle layer — a tenant default and per-user overrides — within
    these bounds; see :mod:`inqtrix.quota`). Each dimension has a
    ``*_default`` (the out-of-box per-user allowance) and a ``*_max``
    (the hard ceiling no admin-set value may exceed). A value of ``0``
    means UNLIMITED for that field — so the all-zero default leaves
    every deployment byte-identical until an operator sets real
    numbers. Quotas apply ONLY when ``enabled`` and the active auth mode
    is one of the cookie-session modes (``oidc``/``local``); the
    single-operator anonymous/static principals (``none``/``apikey``) are
    never metered.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    enabled: bool = Field(
        False,
        alias="INQTRIX_QUOTA_ENABLED",
        description=(
            "Master switch for per-user usage quotas. Off by default. "
            "Even when on, quotas bind only in the cookie-session modes "
            "(``oidc``/``local``) — the single-operator "
            "``none``/``apikey`` modes are never metered. The capability "
            "manifest advertises ``features.quota`` accordingly."
        ),
    )
    """Master switch for per-user usage quotas. Off by default; binds only in the cookie-session modes (oidc/local)."""

    runs_default: int = Field(
        0,
        alias="INQTRIX_QUOTA_RUNS_PER_MONTH",
        ge=0,
        description=(
            "Default native research runs a user may start per calendar "
            "month. ``0`` = unlimited. Checked exactly at submission "
            "(the count is known before the run starts)."
        ),
    )
    """Default native research runs per user per calendar month. ``0`` = unlimited."""
    runs_max: int = Field(
        0,
        alias="INQTRIX_QUOTA_RUNS_PER_MONTH_MAX",
        ge=0,
        description=(
            "Hard ceiling for the per-user monthly run allowance — no "
            "admin-set default or override may exceed it. ``0`` = no "
            "ceiling."
        ),
    )
    """Hard ceiling for the per-user monthly run allowance. ``0`` = no ceiling."""

    llm_tokens_default: int = Field(
        0,
        alias="INQTRIX_QUOTA_LLM_TOKENS_PER_MONTH",
        ge=0,
        description=(
            "Default LLM tokens (prompt + completion, summed across "
            "runs, chat and editor) a user may consume per calendar "
            "month. ``0`` = unlimited. Recorded post-hoc from the "
            "usage each call reports; the current run finishes and the "
            "NEXT submission is blocked once the budget is reached."
        ),
    )
    """Default LLM tokens per user per calendar month (runs + chat + editor). ``0`` = unlimited."""
    llm_tokens_max: int = Field(
        0,
        alias="INQTRIX_QUOTA_LLM_TOKENS_PER_MONTH_MAX",
        ge=0,
        description="Hard ceiling for the per-user monthly LLM-token allowance. ``0`` = no ceiling.",
    )
    """Hard ceiling for the per-user monthly LLM-token allowance. ``0`` = no ceiling."""

    embedding_tokens_default: int = Field(
        0,
        alias="INQTRIX_QUOTA_EMBEDDING_TOKENS_PER_MONTH",
        ge=0,
        description=(
            "Default embedding input tokens a user may consume per "
            "calendar month through document ingestion. ``0`` = "
            "unlimited. Same block-next model as the other flow "
            "dimensions: the current ingestion finishes and the NEXT is "
            "blocked once the budget is reached. A single ingestion "
            "cannot run away because per-document size is already "
            "bounded (INQTRIX_KNOWLEDGE_MAX_DOCUMENT_CHARS / the file "
            "size limit); the exact embedded-text tokens are recorded "
            "after ingestion."
        ),
    )
    """Default embedding input tokens per user per calendar month. ``0`` = unlimited."""
    embedding_tokens_max: int = Field(
        0,
        alias="INQTRIX_QUOTA_EMBEDDING_TOKENS_PER_MONTH_MAX",
        ge=0,
        description="Hard ceiling for the per-user monthly embedding-token allowance. ``0`` = no ceiling.",
    )
    """Hard ceiling for the per-user monthly embedding-token allowance. ``0`` = no ceiling."""

    stored_bytes_default: int = Field(
        0,
        alias="INQTRIX_QUOTA_STORED_BYTES",
        ge=0,
        description=(
            "Default object-store occupancy a user may hold, in bytes. "
            "A STOCK quota (not a per-period flow): it rises on upload "
            "and falls on delete, never resets. ``0`` = unlimited."
        ),
    )
    """Default per-user object-store occupancy in bytes (a stock quota, freed by deletion). ``0`` = unlimited."""
    stored_bytes_max: int = Field(
        0,
        alias="INQTRIX_QUOTA_STORED_BYTES_MAX",
        ge=0,
        description="Hard ceiling for per-user object-store occupancy in bytes. ``0`` = no ceiling.",
    )
    """Hard ceiling for per-user object-store occupancy in bytes. ``0`` = no ceiling."""

    max_tokens_per_run: int = Field(
        0,
        alias="INQTRIX_QUOTA_MAX_TOKENS_PER_RUN",
        ge=0,
        description=(
            "Optional HARD per-run token budget. ``0`` (default) = off "
            "— the per-period quota lets the current bounded run finish "
            "and blocks the next. When set, a single run that crosses "
            "this budget mid-flight is cancelled at the next graph-node "
            "boundary via the existing cancel token (a graceful stop "
            "that returns the partial result, never a mid-call kill). "
            "Independent of the monthly token quota."
        ),
    )
    """Optional hard per-run token budget; ``0`` = off. Cancels a run gracefully at the next node boundary via the existing cancel token."""


class ProviderSettings(BaseSettings):
    """Server-mode provider selection plus the credentials and inference knobs
    that have no other Settings home.

    This is the env-driven, mix-and-match front door for the HTTP server: the
    two independent axes ``llm_provider`` and ``search_provider`` pick which
    concrete providers :func:`~inqtrix.providers.create_providers` builds, and
    the remaining fields supply the per-provider credentials and the
    construction knobs (selectable model catalogue, temperature, token-budget
    parameter, search preset/instructions) that are not already covered by
    :class:`ModelSettings` (model names/tiers) or :class:`ServerSettings`
    (LiteLLM/Perplexity creds).

    Backward compatibility: the defaults ``litellm``/``perplexity`` reproduce
    the historical auto-create stack byte for byte. Library-mode users
    (explicit ``AgentConfig`` providers) bypass this class entirely. Per the
    constructor-first rule, providers never read these env vars themselves —
    only the factory translates them into constructor arguments.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    llm_provider: Literal["litellm", "anthropic", "azure", "bedrock"] = Field(
        "litellm",
        alias="INQTRIX_LLM_PROVIDER",
        description=(
            "Selects the LLM axis of the server-mode provider stack. One of "
            "``litellm`` (OpenAI-compatible gateway, default and historical "
            "behaviour), ``anthropic`` (direct Messages API), ``azure`` "
            "(Azure OpenAI), ``bedrock`` (AWS Bedrock). Independent of "
            "``search_provider`` — any LLM pairs with any search provider. "
            "An unknown value is rejected loudly at ``Settings()`` construction."
        ),
    )
    """Selects the LLM axis of the server-mode provider stack (``litellm`` default). Independent of ``search_provider``; an unknown value fails loudly at construction."""
    search_provider: Literal["perplexity", "azure_foundry"] = Field(
        "perplexity",
        alias="INQTRIX_SEARCH_PROVIDER",
        description=(
            "Selects the search axis of the server-mode provider stack. One "
            "of ``perplexity`` (native Perplexity Agent API, default) or "
            "``azure_foundry`` (Azure AI Foundry Web Search agent). "
            "Independent of ``llm_provider``."
        ),
    )
    """Selects the search axis of the server-mode provider stack (``perplexity`` default). Independent of ``llm_provider``."""

    anthropic_api_key: str = Field(
        "",
        alias="ANTHROPIC_API_KEY",
        description=(
            "API key for the direct Anthropic Messages API. Required when "
            "``llm_provider=anthropic``; the factory fails loudly at startup "
            "if it is empty in that case."
        ),
    )
    """API key for the direct Anthropic Messages API. Required when ``llm_provider=anthropic``."""
    anthropic_base_url: str = Field(
        "",
        alias="ANTHROPIC_BASE_URL",
        description=(
            "Optional Messages-endpoint override for the Anthropic provider. "
            "Empty (default) uses the provider's built-in endpoint."
        ),
    )
    """Optional Messages-endpoint override for the Anthropic provider. Empty uses the provider default."""

    azure_openai_endpoint: str = Field(
        "",
        alias="AZURE_OPENAI_ENDPOINT",
        description=(
            "Azure OpenAI resource endpoint such as "
            "``https://<resource>.openai.azure.com/``. Required when "
            "``llm_provider=azure``. Do not append ``/openai/v1`` — the "
            "provider does that internally."
        ),
    )
    """Azure OpenAI resource endpoint. Required when ``llm_provider=azure``; the provider appends the ``/openai/v1`` path itself."""
    azure_openai_api_key: str = Field(
        "",
        alias="AZURE_OPENAI_API_KEY",
        description=(
            "Azure OpenAI API key (key-auth path). Either this or the full "
            "Service-Principal trio (``AZURE_TENANT_ID``/``AZURE_CLIENT_ID``/"
            "``AZURE_CLIENT_SECRET``) is required when ``llm_provider=azure``."
        ),
    )
    """Azure OpenAI API key (key-auth path). Alternative to the Service-Principal trio for ``llm_provider=azure``."""
    azure_tenant_id: str = Field(
        "",
        alias="AZURE_TENANT_ID",
        description=(
            "Entra tenant id for Service-Principal auth, shared by the Azure "
            "OpenAI LLM and the Azure Foundry search provider. Supply all "
            "three SP vars together or none."
        ),
    )
    """Entra tenant id for Service-Principal auth (shared by Azure LLM + Foundry search)."""
    azure_client_id: str = Field(
        "",
        alias="AZURE_CLIENT_ID",
        description="Entra client id for Service-Principal auth (see ``azure_tenant_id``).",
    )
    """Entra client id for Service-Principal auth (see ``azure_tenant_id``)."""
    azure_client_secret: str = Field(
        "",
        alias="AZURE_CLIENT_SECRET",
        description="Entra client secret for Service-Principal auth (see ``azure_tenant_id``).",
    )
    """Entra client secret for Service-Principal auth (see ``azure_tenant_id``)."""

    aws_profile: str = Field(
        "",
        alias="AWS_PROFILE",
        description=(
            "Optional AWS named profile for the Bedrock provider. Empty "
            "(default) lets boto3 resolve credentials from the standard AWS "
            "chain (env vars, instance role, etc.)."
        ),
    )
    """Optional AWS named profile for Bedrock. Empty lets boto3 use the default credential chain."""
    aws_region: str = Field(
        "eu-central-1",
        alias="AWS_REGION",
        description="AWS region for the Bedrock endpoint when ``llm_provider=bedrock``.",
    )
    """AWS region for the Bedrock endpoint when ``llm_provider=bedrock``."""

    azure_ai_project_endpoint: str = Field(
        "",
        alias="AZURE_AI_PROJECT_ENDPOINT",
        description=(
            "Azure AI Foundry project endpoint such as "
            "``https://<project>.services.ai.azure.com/api/projects/<project>``. "
            "Required when ``search_provider=azure_foundry``."
        ),
    )
    """Azure AI Foundry project endpoint. Required when ``search_provider=azure_foundry``."""
    azure_ai_project_api_key: str = Field(
        "",
        alias="AZURE_AI_PROJECT_API_KEY",
        description=(
            "Optional Foundry project API key for the Azure Foundry search "
            "provider. Either this or the shared Service-Principal trio "
            "authenticates the agent."
        ),
    )
    """Optional Foundry project API key for Azure Foundry search (alternative to the Service-Principal trio)."""
    web_search_agent_name: str = Field(
        "",
        alias="WEB_SEARCH_AGENT_NAME",
        description=(
            "Name of the pre-created Azure Foundry Web Search agent. Required "
            "when ``search_provider=azure_foundry``."
        ),
    )
    """Name of the pre-created Azure Foundry Web Search agent. Required when ``search_provider=azure_foundry``."""
    web_search_agent_version: str = Field(
        "",
        alias="WEB_SEARCH_AGENT_VERSION",
        description="Optional pinned version label for the Azure Foundry Web Search agent.",
    )
    """Optional pinned version label for the Azure Foundry Web Search agent."""
    azure_foundry_max_concurrency: int = Field(
        6,
        ge=1,
        alias="INQTRIX_AZURE_FOUNDRY_MAX_CONCURRENCY",
        description=(
            "Maximum simultaneous Azure Foundry web-search operations in "
            "one process. The provider enforces this on its shared instance, "
            "so Research and Agent Desk waves cannot independently exceed it."
        ),
    )
    """Per-process Azure Foundry web-search concurrency ceiling shared by every caller of the provider instance (default ``6``)."""

    selectable_chat_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="INQTRIX_SELECTABLE_CHAT_MODELS",
        description=(
            "Comma-separated model ids offered in the UI model picker. Passed "
            "to every LLM provider as ``selectable_models``; the model is "
            "resolved against the model-card catalogue and surfaced in "
            "``/health.models_catalog``. Empty (default) leaves the explicit "
            "picker unfed (tier routing still applies)."
        ),
    )
    """Comma-separated model ids for the UI picker; passed as ``selectable_models`` and surfaced in ``/health.models_catalog``."""
    temperature: float | None = Field(
        None,
        alias="INQTRIX_TEMPERATURE",
        description=(
            "Optional sampling temperature for the Anthropic/Azure/Bedrock "
            "LLM providers. ``None`` (default) leaves it unset. LiteLLM is a "
            "generic gateway and ignores this; setting it under "
            "``llm_provider=litellm`` logs a visible warning."
        ),
    )
    """Optional sampling temperature (Anthropic/Azure/Bedrock). Ignored by LiteLLM (logged when set)."""
    token_budget_parameter: str = Field(
        "",
        alias="INQTRIX_TOKEN_BUDGET_PARAMETER",
        description=(
            "Output-budget request field for the LiteLLM/Azure providers: "
            "``max_tokens`` or ``max_completion_tokens`` (the latter is "
            "required by OpenAI o-series). Empty (default) uses each "
            "provider's own default. Anthropic/Bedrock ignore this; setting "
            "it there logs a visible warning."
        ),
    )
    """Output-budget request field (``max_tokens``/``max_completion_tokens``) for LiteLLM/Azure. Empty uses the provider default."""
    search_preset: str = Field(
        "",
        alias="INQTRIX_SEARCH_PRESET",
        description=(
            "Perplexity Agent preset: ``fast-search``, ``pro-search`` or "
            "``deep-research``. Empty (default) uses the provider default "
            "(``fast-search``). Web search has no high/mid/fast tiers like the "
            "LLM — this preset plus ``search_model`` are the search knobs. "
            "Ignored by ``azure_foundry`` (logged when set)."
        ),
    )
    """Perplexity Agent preset (``fast-search``/``pro-search``/``deep-research``). Empty uses the provider default; ignored by ``azure_foundry``."""
    search_instructions: str = Field(
        "",
        alias="INQTRIX_SEARCH_INSTRUCTIONS",
        description=(
            "Optional system instructions for the Perplexity search agent. "
            "Empty (default) leaves the agent's behaviour at its built-in "
            "default. Ignored by ``azure_foundry`` (logged when set)."
        ),
    )
    """Optional system instructions for the Perplexity search agent. Ignored by ``azure_foundry``."""

    @field_validator("llm_provider", "search_provider", mode="before")
    @classmethod
    def _normalize_selector(cls, value: object) -> object:
        """Lower-case and strip the axis selectors so ``Anthropic`` resolves.

        Keeps the env surface forgiving (case/whitespace) while the ``Literal``
        type still rejects genuinely unknown values loudly at construction.
        """
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("selectable_chat_models", mode="before")
    @classmethod
    def _split_selectable(cls, value: object) -> object:
        """Accept a comma-separated env string or an explicit list.

        Splits on commas, strips each entry, and drops empties so a trailing
        comma or stray whitespace never produces a phantom model id. A present
        but empty value collapses to ``[]`` (treated as unset).

        The field is annotated ``NoDecode`` for exactly this reason: without it,
        pydantic-settings JSON-decodes ``list[str]`` env values before any
        validator runs, so a comma string (the documented format) would raise a
        ``SettingsError`` and this splitter would never be reached. Keep both
        together.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("temperature", mode="before")
    @classmethod
    def _blank_temperature_to_none(cls, value: object) -> object:
        """Treat a blank ``INQTRIX_TEMPERATURE`` as unset rather than an error.

        A common ``.env`` pattern is to leave a variable present but empty
        (``INQTRIX_TEMPERATURE=``). Without this, Pydantic would reject the
        empty string as an invalid float and fail ``Settings()`` construction;
        mapping blank to ``None`` keeps "present but empty" equivalent to unset.
        """
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class SharingSettings(BaseSettings):
    """Resource-sharing policy for the cookie-session multi-user modes.

    Sharing is otherwise tenant-wide: any authenticated user may be a share
    target, and the share typeahead searches the whole tenant. This knob lets
    an operator confine collaboration continuously to workspace boundaries.
    Grant, accept, every live access, and membership reconciliation enforce the
    same rule; disabling it does not resurrect shares already revoked.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    enabled: bool = Field(
        True,
        alias="INQTRIX_SHARING_ENABLED",
        description=(
            "Enable direct resource sharing with signed-in people. Disabling "
            "the module suspends the public sharing surface without deleting "
            "stored grants. The default remains true for backwards "
            "compatibility; unsupported authentication modes still keep the "
            "module unavailable."
        ),
    )
    """Master switch for direct person-to-person sharing (default ``True``)."""

    restrict_to_workspace_members: bool = Field(
        False,
        alias="INQTRIX_SHARING_RESTRICT_TO_WORKSPACE_MEMBERS",
        description=(
            "When true, a user may only share a resource with people they "
            "share at least one workspace with, and the share typeahead is "
            "scoped the same way. Accept and every live resource access also "
            "require a common workspace; losing the last common workspace "
            "revokes pending and accepted shares. Startup reconciles existing "
            "shares before readiness. Disabling the setting does not restore "
            "revoked shares. Default false keeps sharing tenant-wide. Only "
            "meaningful in the cookie-session modes (oidc/local) that "
            "mount the sharing surface; the single-operator none/apikey modes "
            "never mount it."
        ),
    )
    """Continuously require a shared workspace for direct resource shares.

    Grant, accept, live authorization, and membership reconciliation enforce
    the boundary. A revoked share is never restored automatically when the
    setting is later disabled. Default ``False`` keeps sharing tenant-wide.
    """


class EditorGuestLinkSettings(BaseSettings):
    """Optional account-less editor link sharing.

    Link sharing is deliberately independent from normal collaboration so an
    enterprise deployment can keep signed-in teamwork while refusing bearer
    links. Enabling the module is validated by the composition root against
    sharing, collaboration, Postgres, and an HTTPS public base URL.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    enabled: bool = Field(
        False,
        alias="INQTRIX_EDITOR_GUEST_LINKS_ENABLED",
        description=(
            "Enable password-protected editor links for visitors without an "
            "Inqtrix account. Requires sharing, collaboration, PostgreSQL, "
            "and an HTTPS INQTRIX_PUBLIC_BASE_URL."
        ),
    )
    """Master switch for account-less editor links (default ``False``)."""

    stats_enabled: bool = Field(
        True,
        alias="INQTRIX_EDITOR_GUEST_LINK_STATS_ENABLED",
        description=(
            "Collect bounded successful-open and guest-session counters for "
            "editor share links. Security throttling remains active even when "
            "product statistics are disabled."
        ),
    )
    """Whether privacy-bounded guest-link product statistics are collected."""

    default_ttl_seconds: int = Field(
        7 * 24 * 60 * 60,
        alias="INQTRIX_EDITOR_GUEST_LINK_DEFAULT_TTL_SECONDS",
        ge=60 * 60,
        le=30 * 24 * 60 * 60,
        description="Default guest-link lifetime in seconds (default 7 days).",
    )
    """Default guest-link lifetime, bounded to one hour through 30 days."""

    max_ttl_seconds: int = Field(
        30 * 24 * 60 * 60,
        alias="INQTRIX_EDITOR_GUEST_LINK_MAX_TTL_SECONDS",
        ge=60 * 60,
        le=30 * 24 * 60 * 60,
        description="Maximum guest-link lifetime in seconds (maximum 30 days).",
    )
    """Operator ceiling for guest-link expiry (maximum 30 days)."""

    token_hmac_secret: str = Field(
        "",
        alias="INQTRIX_EDITOR_GUEST_LINK_TOKEN_SECRET",
        description=(
            "Independent HMAC secret used to store guest-link token digests. "
            "Required at 32+ characters when guest links are enabled and must "
            "not reuse the session or collaboration secret."
        ),
    )
    """Independent secret used only for guest-link token digests."""
    allow_insecure_http: bool = Field(
        False,
        alias="INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP",
        description=(
            "Development escape hatch: allow guest links behind a plain "
            "HTTP INQTRIX_PUBLIC_BASE_URL and drop the Secure flag from "
            "the guest cookies. Guest-link tokens, passwords, and "
            "sessions then cross the wire UNENCRYPTED — never use in "
            "production; activation logs a WARNING at startup."
        ),
    )
    """Explicit dev/trusted-LAN opt-in for HTTP guest links (default off, same pattern as ``INQTRIX_OIDC_INSECURE_DEV_COOKIES``). Without it, an HTTP public base URL keeps failing the boot guard loudly; with it, the guard degrades to a startup WARNING, guest cookies drop ``Secure``, and the capability manifest stops reporting ``https_required``."""

    @model_validator(mode="after")
    def _validate_guest_link_settings(self) -> "EditorGuestLinkSettings":
        if self.default_ttl_seconds > self.max_ttl_seconds:
            raise ValueError(
                "INQTRIX_EDITOR_GUEST_LINK_DEFAULT_TTL_SECONDS must be less "
                "than or equal to INQTRIX_EDITOR_GUEST_LINK_MAX_TTL_SECONDS"
            )
        if self.enabled and len(self.token_hmac_secret) < 32:
            raise ValueError(
                "INQTRIX_EDITOR_GUEST_LINK_TOKEN_SECRET must contain at least "
                "32 characters when guest links are enabled"
            )
        return self


class CollaborationSettings(BaseSettings):
    """Optional editor live-collaboration service configuration.

    The browser always uses the same-origin ``/collaboration`` gateway.
    ``http_url`` and ``ws_url`` are private service-network addresses used
    only by FastAPI. Enabling the feature is fail-loud: this group validates
    its complete transport and cryptographic configuration, while the
    composition root validates the required Postgres and cookie-auth modes.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    enabled: bool = Field(
        False,
        alias="INQTRIX_COLLABORATION_ENABLED",
        description=(
            "Enable the optional editor collaboration module. The default is "
            "false; when true every required URL, service secret, Postgres, "
            "and cookie-auth prerequisite is validated at startup."
        ),
    )
    """Whether the optional editor collaboration module is enabled. Defaults to ``False`` and never degrades silently when explicitly enabled."""
    http_url: str = Field(
        "",
        alias="INQTRIX_COLLABORATION_HTTP_URL",
        description=(
            "Private HTTP base URL of the Node collaboration service, such "
            "as http://collaboration:1234. Required when enabled and never "
            "exposed to browsers."
        ),
    )
    """Private HTTP base URL of the Node collaboration service. Required when enabled; never browser-visible."""
    ws_url: str = Field(
        "",
        alias="INQTRIX_COLLABORATION_WS_URL",
        description=(
            "Private WebSocket URL used by the FastAPI binary gateway to "
            "reach Node, such as ws://collaboration:1234. Required when "
            "enabled; the browser still connects only to /collaboration."
        ),
    )
    """Private Node WebSocket URL reached by the FastAPI gateway. Required when enabled; browsers never receive it."""
    secret: str = Field(
        "",
        alias="INQTRIX_COLLABORATION_SECRET",
        description=(
            "Independent bearer secret for FastAPI-to-Node internal calls. "
            "Must contain at least 32 characters when enabled and must not "
            "reuse the session secret."
        ),
    )
    """Independent internal bearer secret. At least 32 characters when enabled; sensitive and never logged or returned."""
    tenant_id: str = Field(
        "default",
        alias="INQTRIX_COLLABORATION_TENANT_ID",
        min_length=1,
        max_length=160,
        description=(
            "Canonical tenant carried by the single-deployment Node service. "
            "Every private collaboration request must match this value."
        ),
    )
    """Canonical single-deployment collaboration tenant. Default ``default``; private Node requests cannot select another tenant."""
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="INQTRIX_COLLABORATION_ALLOWED_ORIGINS",
        description=(
            "Optional comma-separated additional WebSocket Origin allowlist. "
            "Same-origin is always derived from the request host; entries "
            "must be absolute http(s) origins without paths."
        ),
    )
    """Additional absolute HTTP(S) origins accepted by the public WebSocket gateway. Same-origin remains implicit."""
    lease_ttl_seconds: int = Field(
        60,
        alias="INQTRIX_COLLABORATION_LEASE_TTL_SECONDS",
        ge=15,
        le=300,
        description="Lifetime of one document-scoped collaboration lease.",
    )
    """Document-scoped lease lifetime in seconds. Default ``60`` balances prompt revocation with reconnect churn."""
    token_refresh_seconds: int = Field(
        30,
        alias="INQTRIX_COLLABORATION_TOKEN_REFRESH_SECONDS",
        ge=5,
        le=240,
        description="Client lease-refresh interval; must be below the lease TTL.",
    )
    """Client lease-refresh interval in seconds. Default ``30`` and always lower than ``lease_ttl_seconds``."""
    instance_lease_seconds: int = Field(
        15,
        alias="INQTRIX_COLLABORATION_INSTANCE_LEASE_SECONDS",
        ge=6,
        le=120,
        description="Single-writer Node instance fencing lease lifetime.",
    )
    """Single-writer Node instance fencing lease lifetime in seconds. Default ``15``."""
    instance_renew_seconds: int = Field(
        5,
        alias="INQTRIX_COLLABORATION_INSTANCE_RENEW_SECONDS",
        ge=1,
        le=60,
        description="Instance-fencing renewal interval; must be below its lease.",
    )
    """Instance-fencing renewal interval in seconds. Default ``5`` and always lower than ``instance_lease_seconds``."""
    provider_flush_ms: int = Field(
        50,
        alias="INQTRIX_COLLABORATION_PROVIDER_FLUSH_MS",
        ge=10,
        le=1000,
        description=(
            "Maximum browser-side batching delay for local Yjs updates; "
            "returned by the session API."
        ),
    )
    """Maximum browser-side update batching delay in milliseconds. Default ``50``. Returned by the session API."""
    snapshot_idle_seconds: int = Field(
        5,
        alias="INQTRIX_COLLABORATION_SNAPSHOT_IDLE_SECONDS",
        ge=1,
        le=300,
        description="Idle interval after which Node requests a verified snapshot.",
    )
    """Idle snapshot threshold in seconds. Default ``5``."""
    snapshot_update_count: int = Field(
        256,
        alias="INQTRIX_COLLABORATION_SNAPSHOT_UPDATE_COUNT",
        ge=1,
        le=100_000,
        description="Update-tail count that triggers a verified snapshot.",
    )
    """Number of durable updates that triggers a verified snapshot. Default ``256``."""
    snapshot_tail_bytes: int = Field(
        1_048_576,
        alias="INQTRIX_COLLABORATION_SNAPSHOT_TAIL_BYTES",
        ge=65_536,
        le=100 * 1_048_576,
        description="Durable update-tail bytes that trigger a verified snapshot.",
    )
    """Durable update-tail byte threshold for snapshotting. Default ``1 MiB``."""
    max_frame_bytes: int = Field(
        2 * 1_048_576,
        alias="INQTRIX_COLLABORATION_MAX_FRAME_BYTES",
        ge=65_536,
        le=16 * 1_048_576,
        description=(
            "Maximum public or private WebSocket frame size; returned by the "
            "session API."
        ),
    )
    """Maximum WebSocket frame size in bytes. Default ``2 MiB``; oversize frames close with code 1009. Returned by the session API."""
    max_queued_frames: int = Field(
        32,
        alias="INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES",
        ge=1,
        le=256,
        description=(
            "Maximum inbound WebSocket frames buffered per physical socket "
            "before application processing; returned by the session API."
        ),
    )
    """Maximum inbound WebSocket queue depth per physical socket. Default ``32``; bounded to ``1`` through ``256``. Returned by the session API."""
    max_queued_bytes: int = Field(
        8 * 1_048_576,
        alias="INQTRIX_COLLABORATION_MAX_QUEUED_BYTES",
        ge=65_536,
        le=64 * 1_048_576,
        description=(
            "Maximum inbound bytes the collaboration service reserves per "
            "physical socket before processing; returned by the session API."
        ),
    )
    """Inbound byte budget per physical socket that the collaboration service enforces. Default ``8 MiB``; the API parses it with the same bounds so every session returns the enforced value."""
    max_document_bytes: int = Field(
        10 * 1_048_576,
        alias="INQTRIX_COLLABORATION_MAX_DOCUMENT_BYTES",
        ge=1_048_576,
        le=100 * 1_048_576,
        description="Maximum Markdown or Yjs document accepted for conversion.",
    )
    """Maximum document size in bytes. Default ``10 MiB``; larger activation requests fail with HTTP 413."""
    max_sessions_per_user_document: int = Field(
        5,
        alias="INQTRIX_COLLABORATION_MAX_SESSIONS_PER_USER_DOCUMENT",
        ge=1,
        le=50,
        description="Concurrent session cap per user and document.",
    )
    """Concurrent collaboration-session cap per user and document. Default ``5``."""
    session_rate_per_minute: int = Field(
        30,
        alias="INQTRIX_COLLABORATION_SESSION_RATE_PER_MINUTE",
        ge=1,
        le=1000,
        description="Session-lease issuance limit per user per minute.",
    )
    """Session-lease issuance limit per user per minute. Default ``30``."""
    update_rate_count: int = Field(
        120,
        alias="INQTRIX_COLLABORATION_UPDATE_RATE_COUNT",
        ge=1,
        le=10_000,
        description=(
            "Update-message limit per connection and rate window; returned "
            "by the session API."
        ),
    )
    """Update-message allowance per connection and rate window. Default ``120``. Returned by the session API."""
    update_rate_window_seconds: int = Field(
        10,
        alias="INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS",
        ge=1,
        le=60,
        description=(
            "Window in seconds for the update-message limit; returned by the "
            "session API."
        ),
    )
    """Window for the per-connection update-message limit. Default ``10`` seconds. Returned by the session API."""
    awareness_rate_per_second: int = Field(
        20,
        alias="INQTRIX_COLLABORATION_AWARENESS_RATE_PER_SECOND",
        ge=1,
        le=200,
        description="Awareness-message allowance per connection per second.",
    )
    """Awareness-message allowance per connection per second. Default ``20``."""
    update_payload_retention_seconds: int = Field(
        86_400,
        alias="INQTRIX_COLLABORATION_UPDATE_PAYLOAD_RETENTION_SECONDS",
        ge=3_600,
        description=(
            "Minimum age of snapshot-covered binary updates before their "
            "payload may be pruned. Metadata remains independently retained."
        ),
    )
    """Snapshot-covered binary payload retention in seconds. Default ``24 hours``; metadata is retained longer."""
    activity_retention_seconds: int = Field(
        90 * 86_400,
        alias="INQTRIX_COLLABORATION_ACTIVITY_RETENTION_SECONDS",
        ge=86_400,
        description="Retention of update attribution and decision metadata.",
    )
    """Update attribution and decision metadata retention in seconds. Default ``90 days``."""
    tombstone_retention_seconds: int = Field(
        90 * 86_400,
        alias="INQTRIX_COLLABORATION_TOMBSTONE_RETENTION_SECONDS",
        ge=86_400,
        description=(
            "Delay before physically removing a collaboration document that "
            "has already been tombstoned and revoked."
        ),
    )
    """Tombstone retention before physical cleanup. Default ``90 days``; version 1 exposes no restore UI."""
    protocol_version: int = Field(
        1,
        alias="INQTRIX_COLLABORATION_PROTOCOL_VERSION",
        ge=1,
        description="Binary/stateless collaboration protocol version.",
    )
    """Collaboration protocol version. Version ``1`` is the initial wire contract."""
    schema_version: int = Field(
        2,
        alias="INQTRIX_COLLABORATION_SCHEMA_VERSION",
        ge=1,
        description="Shared Tiptap/Yjs schema version.",
    )
    """Shared editor schema version. Version ``2`` adds reversible structure proposals."""

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _parse_allowed_origins(cls, value: object) -> object:
        """Parse the documented comma-separated origin allowlist."""
        if isinstance(value, str):
            return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_collaboration_contract(self) -> "CollaborationSettings":
        """Reject incomplete or internally contradictory collaboration settings."""
        tenant_id = self.tenant_id.strip()
        if not tenant_id:
            raise ValueError(
                "INQTRIX_COLLABORATION_TENANT_ID must not be blank"
            )
        object.__setattr__(self, "tenant_id", tenant_id)
        if self.token_refresh_seconds >= self.lease_ttl_seconds:
            raise ValueError(
                "INQTRIX_COLLABORATION_TOKEN_REFRESH_SECONDS must be lower than the lease TTL"
            )
        if self.instance_renew_seconds >= self.instance_lease_seconds:
            raise ValueError(
                "INQTRIX_COLLABORATION_INSTANCE_RENEW_SECONDS must be lower than the instance lease"
            )
        for origin in self.allowed_origins:
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.hostname
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ValueError(
                    "INQTRIX_COLLABORATION_ALLOWED_ORIGINS entries must be absolute origins without paths"
                )
        if not self.enabled:
            return self
        parsed_http = urlsplit(self.http_url)
        if (
            parsed_http.scheme not in {"http", "https"}
            or not parsed_http.hostname
            or parsed_http.username is not None
            or parsed_http.password is not None
            or parsed_http.path not in {"", "/"}
            or parsed_http.query
            or parsed_http.fragment
        ):
            raise ValueError(
                "INQTRIX_COLLABORATION_HTTP_URL must be an absolute HTTP(S) origin when enabled"
            )
        parsed_ws = urlsplit(self.ws_url)
        if (
            parsed_ws.scheme not in {"ws", "wss"}
            or not parsed_ws.hostname
            or parsed_ws.username is not None
            or parsed_ws.password is not None
            or parsed_ws.path != "/collaboration"
            or parsed_ws.query
            or parsed_ws.fragment
        ):
            raise ValueError(
                "INQTRIX_COLLABORATION_WS_URL must end in /collaboration when enabled"
            )
        if len(self.secret) < 32:
            raise ValueError(
                "INQTRIX_COLLABORATION_SECRET must contain at least 32 characters when enabled"
            )
        return self


class AgentPlatformSettings(BaseSettings):
    """Workspace-agent platform knobs (``INQTRIX_AGENT_*``).

    Server-side limits for the ``mode=workspace_agent`` runtime:
    everything here is enforced
    in code, never prompted — the agent cannot talk itself past a limit.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    enabled: bool = Field(
        True,
        alias="INQTRIX_AGENT_ENABLED",
        description=(
            "Master switch for the workspace-agent mode. Even when True "
            "the algorithm only registers with a durable checkpointer "
            "(Postgres) or the explicit volatile escape below."
        ),
    )
    """Master switch for the workspace-agent mode. Even when True the algorithm only registers with a durable checkpointer (Postgres backend) or the explicit volatile escape below, so degraded durability remains visible."""
    kernel_enabled: bool = Field(
        True,
        alias="INQTRIX_AGENT_KERNEL_ENABLED",
        description=(
            "Switch for the cognitive-kernel mode. Even when True the "
            "kernel only registers with a checkpointer AND an LLM "
            "provider that supports native tool calling; a failed gate "
            "logs a WARNING and names itself in capabilities."
        ),
    )
    """Switch for the ``mode=agent_kernel`` algorithm (default True). Even when True the kernel only registers when the checkpointer gate passes (same rule as ``enabled``) AND the default LLM provider reports ``supports_tool_calls()``; a gated deployment logs a WARNING and publishes the reason under ``feature_status.agent_kernel`` instead of silently missing the mode. Set it to False to run the phase machine (``workspace_agent``) instead."""
    default_agent_mode: str = Field(
        "agent_kernel",
        alias="INQTRIX_AGENT_DEFAULT_MODE",
        description=(
            "Which algorithm the Agent Desk submits by default: "
            "workspace_agent (phase machine) or agent_kernel. Published "
            "via capabilities agent.default_mode; the kernel value only "
            "takes effect when the kernel registration gate passed."
        ),
    )
    """The Agent Desk's configured default ``mode`` (default ``agent_kernel``). Capabilities publish that value only after the kernel registration gate passes; otherwise they visibly fall back to ``workspace_agent`` with the missed gate's reason under ``feature_status.agent_kernel``, so the frontend never submits an unregistered mode and the fallback is never silent. Setting ``INQTRIX_AGENT_DEFAULT_MODE=workspace_agent`` retains the phase machine as an explicit deployment override."""
    skills_max_attached: int = Field(
        3,
        alias="INQTRIX_AGENT_SKILLS_MAX_ATTACHED",
        description=(
            "How many skills one run may attach explicitly (composer "
            "chips). Enforced at run admission, published via "
            "capabilities agent.skills."
        ),
    )
    """Per-run cap on explicitly attached skills (default 3). Enforced server-side at run admission; the composer reads the published value instead of hardcoding it."""
    skills_disclosure_budget_chars: int = Field(
        4000,
        alias="INQTRIX_AGENT_SKILLS_DISCLOSURE_BUDGET_CHARS",
        description=(
            "Character budget of the model-facing skill disclosure "
            "block; overflow is cut with a VISIBLE '... und N weitere' "
            "line, never silently."
        ),
    )
    """Character budget of the model-facing skill disclosure block (default 4000). Deterministic cut with a visible overflow line — a silent truncation would hide activatable skills from the model."""
    kernel_max_iterations: int = Field(
        121,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_ITERATIONS",
        description=(
            "Initial cumulative LangGraph super-step allowance for one "
            "kernel run. deepagents lifts the default bound to 9999; "
            "this restores a durable user-decision point. Sized by "
            "formula: 9 (answer turn) + N tool turns x 8 super-steps; "
            "the default 121 buys 14 sequential tool turns plus the "
            "answer — a research child, a follow-up search, the todo "
            "bookkeeping and a document fit without an extension "
            "decision."
        ),
    )
    """Cumulative checkpointed LangGraph super-step limit per kernel run (default 121 = 9 + 14x8). The limit counts graph super-steps, NOT model turns: the final answer turn costs 9 (``_ANSWER_TURN_SUPERSTEPS``) and every tool-calling turn 8 (``_SUPERSTEPS_PER_TOOL_TURN``) — both measured and pinned by the harness contract test. 121 therefore buys exactly 14 sequential tool turns plus the answer; lower limits may allow barely one ordinary tool turn. ``kernel_max_tool_calls`` (30) stays the limit on PARALLEL batch work; sequentially this recursion ceiling binds first. Reaching it parks the checkpointed run for an explicit user decision; no partial answer is implied."""
    kernel_max_iterations_deep: int = Field(
        121,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_DEEP",
        description=(
            "Initial cumulative LangGraph super-step allowance for one "
            "kernel run in Deep mode. "
            "Same turn allowance as the normal ceiling: Deep buys more "
            "tool calls and the verification pass, not more turns. "
            "Reaching it parks for an explicit, operator-bounded "
            "decision."
        ),
    )
    """LangGraph ``recursion_limit`` per kernel run when ``depth=deep`` (default 121 = 9 + 14x8). Same measured arithmetic as the normal ceiling and the same 14 sequential tool turns plus the answer; Deep buys more tool calls and the verification pass, not more turns and never an unbounded loop. The equality with the normal ceiling is deliberate: a thorough run is bounded by its tool calls, not by its steps, so raising this would buy turns a run does not use; the thoroughness a Stufe promises is bought in tool calls. Reaching the ceiling parks a durable run for an explicit partial/cancel/extension decision; the extension remains bounded by ``kernel_max_iterations_extension_ceiling_deep``. When a deployment lengthens deep runs further, raise the edge route timeout (Helm ``timeout: 3630s``) and ``INQTRIX_MAX_TOTAL_SECONDS`` in the same change, otherwise a K8s route cuts the SSE stream mid-run."""
    kernel_max_iterations_extension_ceiling: int = Field(
        241,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_EXTENSION_CEILING",
        description=(
            "Operator ceiling for an explicitly user-extended normal "
            "kernel step limit. It can never reduce the configured base."
        ),
    )
    """Maximum cumulative checkpointed kernel super-steps after an explicit user extension in normal mode (default 241). This is an operator safety ceiling, not a model-controlled budget."""
    kernel_max_iterations_extension_ceiling_deep: int = Field(
        241,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_EXTENSION_CEILING_DEEP",
        description=(
            "Operator ceiling for an explicitly user-extended deep "
            "kernel step limit. It can never reduce the configured base."
        ),
    )
    """Maximum cumulative checkpointed kernel super-steps after an explicit user extension in Deep mode (default 241)."""
    kernel_max_tool_calls: int = Field(
        30,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS",
        description=(
            "Run-wide tool-call ceiling for normal kernel runs. Every call "
            "in a model batch counts; an overflowing batch is rejected "
            "before any tool executes."
        ),
    )
    """Run-wide tool-call ceiling for normal kernel runs (default 30). The count is derived from checkpointed AI messages, making the limit cumulative and idempotent across park/resume; an overflowing multi-call batch is rejected in full before dispatch."""
    kernel_max_tool_calls_deep: int = Field(
        60,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_DEEP",
        description=(
            "Run-wide tool-call ceiling for deep kernel runs. It has the "
            "same pre-dispatch and resume-safe semantics as the normal "
            "ceiling."
        ),
    )
    """Run-wide tool-call ceiling for Deep kernel runs (default 60). Deep permits more evidence work while retaining the same whole-batch rejection contract."""
    kernel_max_tool_calls_extension_ceiling: int = Field(
        60,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_EXTENSION_CEILING",
        description=(
            "Operator ceiling for an explicitly user-extended normal "
            "kernel tool-call limit. It can never reduce the configured base."
        ),
    )
    """Maximum run-wide tool calls after an explicit user extension in normal mode (default 60). Overflowing batches remain atomic: none of their tools run before the decision."""
    kernel_max_tool_calls_extension_ceiling_deep: int = Field(
        120,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS_EXTENSION_CEILING_DEEP",
        description=(
            "Operator ceiling for an explicitly user-extended deep "
            "kernel tool-call limit. It can never reduce the configured base."
        ),
    )
    """Maximum run-wide tool calls after an explicit user extension in Deep mode (default 120)."""
    kernel_role_tool_calls_base: int = Field(
        6,
        ge=0,
        alias="INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_BASE",
        description=(
            "Tool calls a role may spend independently of the size of its "
            "subject: listing the evidence index and the orientation reads "
            "that belong to no single source."
        ),
    )
    """Subject-independent part of a role run's tool budget (default 6): listing the evidence index and the orientation reads that belong to no single source. How the budget splits between this part and ``kernel_role_tool_calls_per_source`` is an estimate, not a measured separation, so the two are tuned together."""
    kernel_role_tool_calls_per_source: float = Field(
        1.5,
        ge=0.0,
        alias="INQTRIX_AGENT_KERNEL_ROLE_TOOL_CALLS_PER_SOURCE",
        description=(
            "Tool calls a role may spend per thing it has to work through "
            "(one output, one evidence reference, one released source)."
        ),
    )
    """Per-source part of a role run's tool budget (default 1.5): about one read per source plus a continuation window for the long ones. The rate is a starting estimate, not a measured distribution. A role's work is linear in what it re-reads, which is why the budget is priced per source at all; only the rate is uncertain."""
    kernel_sufficiency_gate: bool = Field(
        True,
        alias="INQTRIX_AGENT_KERNEL_SUFFICIENCY_GATE",
        description=(
            "Adaptive evidence-sufficiency judgement for kernel runs: "
            "after enough source-tool calls, one cheap fast-tier verdict "
            "advises the model to answer now or search only the named "
            "gaps. Advisory — tool budget and recursion stay the hard "
            "stops."
        ),
    )
    """Advisory kernel sufficiency judge (default on). The middleware node is always compiled in; this flag short-circuits the judgement, so per-turn super-step costs stay constant either way. schnell-tier runs never judge (their budget is one search)."""
    kernel_sufficiency_min_tool_calls: int = Field(
        3,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_SUFFICIENCY_MIN_TOOL_CALLS",
        description=(
            "Successful web/knowledge tool calls before the first "
            "sufficiency judgement of a normal kernel run. Runs below "
            "the threshold never pay a judge call."
        ),
    )
    """Sufficiency threshold of normal runs (default 3): the count of SUCCESSFUL source-tool calls (web + project knowledge) that arms the first advisory verdict. Guarantees cheapness — simple 0-2-tool runs never spend judge tokens."""
    kernel_sufficiency_min_tool_calls_deep: int = Field(
        5,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_SUFFICIENCY_MIN_TOOL_CALLS_DEEP",
        description=(
            "Successful web/knowledge tool calls before the first "
            "sufficiency judgement of a deep kernel run."
        ),
    )
    """Sufficiency threshold of Deep runs (default 5): Deep buys more evidence work before the first advisory verdict — proportional to its doubled tool budget."""
    kernel_sufficiency_max_judgements: int = Field(
        2,
        ge=1,
        alias="INQTRIX_AGENT_KERNEL_SUFFICIENCY_MAX_JUDGEMENTS",
        description=(
            "Run-wide cap on sufficiency judgements. The count is "
            "derived from flagged nudge messages in the checkpointed "
            "transcript, so it is cumulative across park/resume."
        ),
    )
    """Run-wide judgement cap (default 2). Counted via the flagged nudge messages still present in the checkpointed transcript — idempotent across park/resume, and the same evidence state is never judged twice. Known bound: a context compaction that summarizes away old nudge messages forgets them, so an extremely long run may judge more than the cap in total — acceptable for an ADVISORY fast-tier call (the hard stops stay tool budget and recursion)."""
    kernel_context_trigger_tokens: int = Field(
        0,
        ge=0,
        alias="INQTRIX_AGENT_KERNEL_CONTEXT_TRIGGER_TOKENS",
        description=(
            "Explicit token threshold at which the kernel compacts its "
            "transcript. 0 (default) derives the threshold from the "
            "resolved model's card (context window x trigger fraction, "
            "floor 128k); a nonzero value pins it verbatim."
        ),
    )
    """Explicit kernel compaction threshold in tokens (default 0 = auto). Auto derives ``context_window_tokens x kernel_context_trigger_fraction`` from the resolved model's card with a 128k floor; models without a card use the floor. A nonzero pin wins over the derivation — operators with unusual serving limits set it directly."""
    kernel_context_trigger_fraction: float = Field(
        0.75,
        gt=0.0,
        le=1.0,
        alias="INQTRIX_AGENT_KERNEL_CONTEXT_TRIGGER_FRACTION",
        description=(
            "Fraction of the resolved model's context window at which "
            "the kernel compacts (only used when the trigger-token pin "
            "is 0)."
        ),
    )
    """Fraction of the model context window that arms compaction when no explicit pin is set (default 0.75). Deliberately below deepagents' 0.85 default: the kernel must compact BEFORE a provider over-budget rejection, and the summary call itself still needs headroom."""
    kernel_context_keep_messages: int = Field(
        12,
        ge=2,
        alias="INQTRIX_AGENT_KERNEL_CONTEXT_KEEP_MESSAGES",
        description=(
            "Newest messages kept verbatim when the kernel compacts; "
            "everything older is summarized into the run archive."
        ),
    )
    """Newest transcript messages preserved verbatim per compaction (default 12). Aggressive on purpose: compacting RARELY and LARGELY breaks the provider prompt-cache prefix once per compaction instead of on every turn (halve-not-trim)."""
    kernel_context_offload_chars: int = Field(
        8000,
        ge=0,
        alias="INQTRIX_AGENT_KERNEL_CONTEXT_TOOL_RESULT_OFFLOAD_CHARS",
        description=(
            "Bulk tool results above this many characters are archived "
            "without shortening the model handover. 0 disables eager archival. "
            "Model-input planning owns any task-specific projection; explicit "
            "evidence reads use their own bounded continuation windows."
        ),
    )
    """Character threshold for eager tool-result archival (default 8000, 0 = off). The full text lands in the run's ``context_archive`` artifact (readable via ``read_canvas``) and remains in the handover. Only model-input planning may substitute a task-specific projection with a readable original. Explicit evidence reads use bounded continuation windows."""
    max_parallel_children: int = Field(
        8,
        alias="INQTRIX_AGENT_MAX_PARALLEL_CHILDREN",
        ge=1,
        description=(
            "Child research runs submitted per park round (and the "
            "in-process tool-wave width). Defaults to the validated "
            "plan ceiling (``MAX_PLAN_TASKS_DEFAULT``) so a plan the "
            "validator accepts can also run in ONE wave; below it a "
            "wave serialises. The parent parks slot-free while children "
            "run, so an undersized worker slows a wave but can no "
            "longer deadlock the pool."
        ),
    )
    """Child research runs submitted per park round, and the width of the in-process tool wave (default 8 = the validated plan ceiling). The parent holds NO execution slot while its children run (``waiting_for_children`` park) — sizing worker capacity below one wave serialises the wave's children; it cannot starve or deadlock the pool."""
    max_parallel_queries_per_task: int = Field(
        3,
        alias="INQTRIX_AGENT_MAX_PARALLEL_QUERIES_PER_TASK",
        ge=1,
        description=(
            "Sub-queries of ONE retrieval task run concurrently, up to "
            "this many. They are independent by construction — each "
            "builds its prompt from the task and its own query, and "
            "reads nothing the siblings produce; task DEPENDENCIES live "
            "on ``depends_on`` and stay sequenced by the wave "
            "scheduler. Sizing: the worst case is "
            "INQTRIX_AGENT_MAX_PARALLEL_CHILDREN x this value "
            "concurrent model calls from a single run, which must fit "
            "MAX_CONCURRENT with room for other traffic (startup warns "
            "otherwise). Past 3 the gain flattens and a wider cap only "
            "raises contention. 1 restores the sequential loop."
        ),
    )
    """Concurrent sub-queries per retrieval task (default 3, 1 = sequential). A task's queries are independent by construction; task dependencies (``depends_on``) remain sequenced by the wave scheduler. Worst case is ``max_parallel_children x`` this value concurrent model calls per run — size ``MAX_CONCURRENT`` above that (the composition root warns otherwise). The same width bounds the concurrent window calls of one document analysis."""
    document_analysis_max_documents: int = Field(
        3,
        alias="INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_DOCUMENTS",
        ge=1,
        description=(
            "Documents ONE file_analysis task reads completely, taken in "
            "search order from what its queries found. Further found "
            "documents are named on the task as not analysed."
        ),
    )
    """Documents one file-analysis task reads completely (default 3); the rest are named, never dropped silently."""
    document_analysis_max_windows: int = Field(
        40,
        alias="INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS",
        ge=1,
        description=(
            "Windows of about 19,000 new characters ONE file_analysis task "
            "analyses at most (40 = about 760,000 characters). Each window "
            "is one model call, so "
            "this bounds a task's cost; ranges past it are named as not "
            "analysed."
        ),
    )
    """Windows one file-analysis task analyses at most (default 40 ≈ 760,000 characters); the unanalysed remainder is named."""
    document_analysis_max_windows_per_run: int = Field(
        80,
        alias="INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS_PER_RUN",
        ge=1,
        description=(
            "Windows ALL file_analysis tasks of one mission analyse "
            "together, across waves, replans and parks. Bounds the cost of a "
            "run that plans several analyses; what the budget leaves unread "
            "is named on the task."
        ),
    )
    """Windows all file-analysis tasks of one mission analyse together (default 80)."""
    synthesis_max_parallel_sections: int = Field(
        8,
        alias="INQTRIX_AGENT_SYNTHESIS_MAX_PARALLEL_SECTIONS",
        ge=1,
        description=(
            "Memo sections of ONE mission synthesis written concurrently, "
            "up to this many. Sections are independent by construction: "
            "each is prompted from the outline entry and its own evidence "
            "digest and reads nothing a sibling writes; the memo is still "
            "assembled, flushed and narrated in outline order, so the "
            "document grows in reading order. Synthesis runs after the "
            "task waves, so it never adds to the wave worst case above. "
            "1 restores the sequential loop."
        ),
    )
    """Concurrent memo sections per mission synthesis (default 8 = the plan-task ceiling, 1 = sequential). Sections are independent by construction and are folded, flushed and narrated in outline order; the synthesis runs after the task waves and therefore never adds to the wave worst case."""
    checkpointer_pool_size: int = Field(
        4,
        alias="INQTRIX_AGENT_CHECKPOINTER_POOL_SIZE",
        ge=1,
        description=(
            "Server connections the agent checkpointer's own psycopg "
            "pool may hold, process-wide, shared by every concurrent "
            "agent run. Counted in the startup connection budget and "
            "published via the admin runtime endpoint."
        ),
    )
    """Ceiling for the agent checkpointer's own psycopg pool (default 4), process-wide and shared by every concurrent agent run in that process. One source, three displays: the pool itself, the startup connection-budget line, and the admin runtime endpoint all read this value. Raise it only when a measurement shows pool-wait under agent target load — the checkpointer speaks its own driver, so this adds to (never replaces) the engine budget."""
    discovery_max_tool_calls: int = Field(
        15,
        alias="INQTRIX_AGENT_DISCOVERY_MAX_TOOL_CALLS",
        description=(
            "Hard probe budget for the read-only discovery phase; the "
            "probe plan is truncated server-side, never prompted."
        ),
    )
    """Hard probe budget for the read-only discovery phase (default 15). The probe plan is truncated server-side; the LLM never negotiates it."""
    max_plan_tasks: int = Field(
        MAX_PLAN_TASKS_DEFAULT,
        alias="INQTRIX_AGENT_MAX_PLAN_TASKS",
        description=(
            "Task-count ceiling the plan validator enforces for agent "
            "AND user-edited plans."
        ),
    )
    """Task-count ceiling the deterministic plan validator enforces (default 8) — for planner output and user edits alike (one shared validator)."""
    max_replan_rounds: int = Field(
        2,
        alias="INQTRIX_AGENT_MAX_REPLAN_ROUNDS",
        description="Replan iterations before the run proceeds as-is.",
    )
    """Replan iterations before the run proceeds with what it has (default 2); each replan is an ADDITIVE task delta, results are never discarded. A delegated mission (kernel child) opens at most ONE critic research round regardless — the parent's deep verify is its second check."""
    mission_time_budget_seconds: int = Field(
        720,
        alias="INQTRIX_AGENT_MISSION_TIME_BUDGET_SECONDS",
        description=(
            "Active mission seconds after which the critic opens no "
            "further research round; the memo closes with the named "
            "gaps (0 disables)."
        ),
    )
    """Active (non-waiting) seconds a mission may spend before the critic's research route closes (default 720 = 12 min). The round cap and this budget are two ceilings on the same route: whichever is reached first ends further research visibly (``limit.reached``, kind ``mission_time_budget``) and the memo closes with the critic's remaining findings under "Offene Punkte". Gate waits are excluded — the budget bounds the work the user pays for, not how long a human took to answer. 0 disables the budget."""
    synthesis_evidence_budget: int = Field(
        160,
        alias="INQTRIX_AGENT_SYNTHESIS_EVIDENCE_BUDGET",
        description=(
            "Reference count above which the outline/answer prompt "
            "digest is capped to the top-ranked references "
            "(deterministic rank_evidence; 0 disables the cap)."
        ),
    )
    """Reference count above which the synthesis PROMPT digest is capped by the deterministic ``rank_evidence`` selection (default 160). The default covers a full Tief wave (up to 8 children x about 20 references), so collected evidence reaches the writer. The citation ledger itself is never truncated; 0 disables the cap entirely."""
    synthesis_section_register_budget: int | None = Field(
        None,
        ge=0,
        alias="INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET",
        description=(
            "Operator override for how many references a memo section "
            "lists as a one-line register beyond the ones its outline "
            "entry preselected. Unset: the run's Stufe decides (schnell "
            "20, gruendlich 60, tief 180; 60 for a run without a Stufe). "
            "0 disables the register."
        ),
    )
    """Operator override of the register width a memo section sees beside the references its outline entry preselected; unset by default, so the run's Stufe decides (``tier_policy.resolve_section_register``: schnell 20, gruendlich 60, tief 180, and 60 for a run that names no Stufe). A section may cite anything it can see, so this bounds what it may cite: the outline's selection decides what a section leads WITH, the register decides what it may reach FOR. A number here replaces every Stufe's width and is published as such in ``/v1/capabilities``; ``0`` switches the register off, which brings back the state in which a section whose outline entry preselected nothing believed the run had no evidence. Deliberately not ``synthesis_evidence_budget``: that one bounds the full-text digest of the whole deliverable, a different question and an order of magnitude more expensive per entry."""
    max_clarification_rounds: int = Field(
        2,
        alias="INQTRIX_AGENT_MAX_CLARIFICATION_ROUNDS",
        description=(
            "User-question rounds per run before the agent proceeds on "
            "its best assumption (named in the transcript and the plan's "
            "assumptions)."
        ),
    )
    """User-question rounds per run (default 2) before the agent proceeds on its best assumption — named in the transcript and the plan's assumptions, never silently. A Stufe caps it tighter, never wider (``tier_policy.effective_clarification_rounds``)."""
    default_autonomy: str = Field(
        "balanced",
        alias="INQTRIX_AGENT_DEFAULT_AUTONOMY",
        description=(
            "Permission mode when the request names none: strict "
            "(discovery + every plan delta approved), balanced (initial "
            "plan approved, small read-only replans auto), autonomous "
            "(no plan interrupt; write actions ALWAYS gated)."
        ),
    )
    """Permission mode when the request names none. ``strict`` approves discovery and every plan delta; ``balanced`` (default) approves the initial plan and auto-applies small read-only replans; ``autonomous`` skips plan interrupts entirely. Write-effect actions are gated in EVERY mode."""
    allow_web_discovery_preview: bool = Field(
        True,
        alias="INQTRIX_AGENT_ALLOW_WEB_DISCOVERY_PREVIEW",
        description=(
            "Permit ONE instant web search during discovery so the "
            "planner sees the external source situation. Applies only "
            "in autonomous mode — Standard keeps all web contact "
            "behind the plan gate — and only when the assignment needs "
            "the web and the question is the user's own."
        ),
    )
    """Permit ONE ``web.search.instant`` call during discovery (default on) so the planner sees the external source situation before committing to web tasks; off = discovery stays fully internal. The preview additionally requires ``autonomous`` mode — in Standard the approved plan (whose tasks carry their verbatim queries) is the ONLY web-search consent surface — an assignment that needs the web, and a question the user wrote: a delegated mission's question is a model's text and is never sent."""
    advanced_autonomy: bool = Field(
        False,
        alias="INQTRIX_AGENT_ADVANCED_AUTONOMY",
        description=(
            "Show the legacy three-way autonomy control (incl. strict) "
            "in the UI instead of the two-mode Standard/Auto toggle."
        ),
    )
    """Republish the legacy three-way autonomy control in the UI (default off). The wire vocabulary (strict/balanced/autonomous) is unchanged either way — this only decides whether the composer shows the simplified two-mode Standard/Auto toggle or all three modes; ``strict`` stays fully functional for API callers and enterprise deployments regardless."""
    allow_volatile: bool = Field(
        False,
        alias="INQTRIX_AGENT_ALLOW_VOLATILE",
        description=(
            "Development escape: register the agent WITHOUT Postgres using "
            "an in-memory checkpointer. Interrupted runs then die with "
            "the process — logged loudly, surfaced as "
            "workspace_agent_durable=false."
        ),
    )
    """Development escape: register the workspace agent WITHOUT the Postgres backend, checkpointing in memory. Interrupted runs then cannot survive a restart — the container logs a WARNING and ``/v1/capabilities`` reports ``workspace_agent_durable: false`` so the degradation is visible, never silent."""
    memory_provider: str = Field(
        "none",
        alias="INQTRIX_AGENT_MEMORY_PROVIDER",
        description=(
            "Long-term memory provider for workspace-agent runs: none or "
            "mem0. The provider is optional and never replaces run/session "
            "artifacts; it only backs user-approved long-term memories."
        ),
    )
    """Long-term workspace-agent memory provider (``none`` or ``mem0``). Default ``none`` keeps existing deployments byte-identical and prevents accidental shared memory in no-auth modes."""
    memory_mode: str = Field(
        "candidate_only",
        alias="INQTRIX_AGENT_MEMORY_MODE",
        description=(
            "Long-term memory learning mode: off, candidate_only, or "
            "auto_safe. candidate_only stages memories for user approval; "
            "auto_safe is reserved for harmless preferences only."
        ),
    )
    """Long-term memory learning mode. ``candidate_only`` is the safe default; ``auto_safe`` remains constrained to harmless preferences."""
    mem0_base_url: str = Field(
        "",
        alias="INQTRIX_MEM0_BASE_URL",
        description=(
            "Base URL of the self-hosted Mem0 API. Empty disables the "
            "Mem0 adapter even when memory_provider is mem0."
        ),
    )
    """Self-hosted Mem0 API base URL. Empty means the Mem0 adapter is unavailable."""
    mem0_api_key: str = Field(
        "",
        alias="INQTRIX_MEM0_API_KEY",
        description=(
            "Optional server-side Mem0 API token. It is never exposed to "
            "clients and is only sent as an Authorization header by the "
            "server-side adapter."
        ),
    )
    """Optional server-side Mem0 API token. Sensitive: never log or expose this value."""

    @field_validator("default_agent_mode")
    @classmethod
    def _validate_default_agent_mode(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in AGENT_MODE_IDS:
            raise ValueError(
                "INQTRIX_AGENT_DEFAULT_MODE must be one of: "
                + ", ".join(AGENT_MODE_IDS)
            )
        return normalized

    @field_validator("memory_provider")
    @classmethod
    def _validate_memory_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"none", "mem0"}:
            raise ValueError(
                "INQTRIX_AGENT_MEMORY_PROVIDER must be one of: none, mem0"
            )
        return normalized

    @field_validator("memory_mode")
    @classmethod
    def _validate_memory_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"off", "candidate_only", "auto_safe"}:
            raise ValueError(
                "INQTRIX_AGENT_MEMORY_MODE must be one of: off, "
                "candidate_only, auto_safe"
            )
        return normalized


LIMIT_CAPACITY = "capacity"
"""A limit that bounds what the DEPLOYMENT carries at once, or how large one
payload may get: concurrency, pools, context windows, prompt bytes. Blind to
the Stufe on purpose — a thorough run does not make a machine bigger or a
context window wider, so reading the tier here would trade a promise of
depth against an outage."""

LIMIT_THOROUGHNESS = "thoroughness"
"""A limit that bounds how much WORK one run does: what it reads, searches,
plans and verifies. It reads the Stufe or the subject, or its docstring says
why it does not — a thoroughness limit that is silently tier-blind is a
promise of depth that a constant quietly revokes."""

AGENT_PLATFORM_LIMIT_PURPOSE: dict[str, str] = {
    # -- capacity ----------------------------------------------------------
    "skills_max_attached": LIMIT_CAPACITY,
    "skills_disclosure_budget_chars": LIMIT_CAPACITY,
    "kernel_context_trigger_tokens": LIMIT_CAPACITY,
    "kernel_context_trigger_fraction": LIMIT_CAPACITY,
    "kernel_context_keep_messages": LIMIT_CAPACITY,
    "kernel_context_offload_chars": LIMIT_CAPACITY,
    "max_parallel_children": LIMIT_CAPACITY,
    "max_parallel_queries_per_task": LIMIT_CAPACITY,
    "synthesis_max_parallel_sections": LIMIT_CAPACITY,
    "checkpointer_pool_size": LIMIT_CAPACITY,
    # -- thoroughness ------------------------------------------------------
    "kernel_max_iterations": LIMIT_THOROUGHNESS,
    "kernel_max_iterations_deep": LIMIT_THOROUGHNESS,
    "kernel_max_iterations_extension_ceiling": LIMIT_THOROUGHNESS,
    "kernel_max_iterations_extension_ceiling_deep": LIMIT_THOROUGHNESS,
    "kernel_max_tool_calls": LIMIT_THOROUGHNESS,
    "kernel_max_tool_calls_deep": LIMIT_THOROUGHNESS,
    "kernel_max_tool_calls_extension_ceiling": LIMIT_THOROUGHNESS,
    "kernel_max_tool_calls_extension_ceiling_deep": LIMIT_THOROUGHNESS,
    "kernel_role_tool_calls_base": LIMIT_THOROUGHNESS,
    "kernel_role_tool_calls_per_source": LIMIT_THOROUGHNESS,
    "kernel_sufficiency_min_tool_calls": LIMIT_THOROUGHNESS,
    "kernel_sufficiency_min_tool_calls_deep": LIMIT_THOROUGHNESS,
    "kernel_sufficiency_max_judgements": LIMIT_THOROUGHNESS,
    "document_analysis_max_documents": LIMIT_THOROUGHNESS,
    "document_analysis_max_windows": LIMIT_THOROUGHNESS,
    "document_analysis_max_windows_per_run": LIMIT_THOROUGHNESS,
    "discovery_max_tool_calls": LIMIT_THOROUGHNESS,
    "max_plan_tasks": LIMIT_THOROUGHNESS,
    "max_replan_rounds": LIMIT_THOROUGHNESS,
    "mission_time_budget_seconds": LIMIT_THOROUGHNESS,
    "synthesis_evidence_budget": LIMIT_THOROUGHNESS,
    "synthesis_section_register_budget": LIMIT_THOROUGHNESS,
    "max_clarification_rounds": LIMIT_THOROUGHNESS,
}
"""What every numeric limit of :class:`AgentPlatformSettings` is FOR.

A tool-call ceiling that binds at 110% while steps and tokens sit near 20%
is not a budget any more, it is a proxy that stopped measuring what it
stands for. Telling the two purposes apart is what makes that visible: a
capacity limit that binds means the deployment is at its edge, a
thoroughness limit that binds means the run was told to stop being thorough
— and only one of those two is ever the right answer to "the Stufe promised
a deeper run".

Every numeric field is listed here, and a new one that is not makes
``test_every_numeric_platform_limit_is_classified`` red. That is the point:
the classification is a question the author of a limit has to answer once,
not documentation somebody maintains afterwards.
"""


class ObservabilitySettings(BaseSettings):
    """Trace-sink selection and volume knobs (``INQTRIX_TRACING``).

    One question per switch: ``INQTRIX_TRACING`` decides WHERE spans go
    (nothing / correlation-ids only / spool files / live OTLP);
    ``OBSERVABILITY_PROFILE`` on the agent group keeps deciding HOW DEEP
    capture goes. The OTLP endpoint and headers are read by the exporter
    itself from the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` /
    ``OTEL_EXPORTER_OTLP_HEADERS`` variables — Langfuse example:
    ``…/api/public/otel`` plus ``Authorization=Basic <base64(pk:sk)>,
    x-langfuse-ingestion-version=4``.
    """

    model_config = _SETTINGS_MODEL_CONFIG

    tracing: Literal["off", "local", "file", "otlp"] = Field(
        "off",
        alias="INQTRIX_TRACING",
        description=(
            "Trace sink. off (default) = no tracer. local = spans exist "
            "only to stamp trace_id/span_id into JSON logs, nothing is "
            "persisted. file = spans additionally spool as OTLP-JSON "
            "lines under the spool dir (replayable into Langfuse later). "
            "otlp = live export via OTLP/HTTP to the standard "
            "OTEL_EXPORTER_OTLP_* endpoint. Requires the `observability` "
            "extra for every value except off (missing extra logs a "
            "WARNING and stays off)."
        ),
    )
    """Trace sink: ``off`` (default) | ``local`` (ids only) | ``file`` (OTLP-JSON spool, replayable) | ``otlp`` (live export). Non-off values need the ``observability`` extra; a missing extra degrades loudly to off."""
    trace_sample_rate: float = Field(
        1.0,
        alias="INQTRIX_TRACE_SAMPLE_RATE",
        ge=0.0,
        le=1.0,
        description=(
            "Head sampling ratio (ParentBased+TraceIdRatio, decides per "
            "whole trace). 1.0 keeps everything; lower it in production "
            "when volume grows. Note: the Langfuse SDK variable "
            "LANGFUSE_SAMPLE_RATE does NOT apply to pure OTLP export — "
            "this is the authoritative knob."
        ),
    )
    """Head-sampling ratio for traces (parent-based trace-id ratio; per-trace decision)."""
    trace_spool_dir: str = Field(
        "logs/traces",
        alias="INQTRIX_TRACE_SPOOL_DIR",
        description=(
            "Directory for spooled OTLP-JSON trace files in "
            "INQTRIX_TRACING=file mode. Created automatically."
        ),
    )
    """Spool directory for ``file`` mode (created automatically)."""
    trace_spool_max_mb: int = Field(
        2048,
        alias="INQTRIX_TRACE_SPOOL_MAX_MB",
        ge=16,
        description=(
            "Total size cap for the spool directory. Oldest spool files "
            "are deleted first (size-based retention backstop; the "
            "primary retention lever for live export is time-based on "
            "the backend)."
        ),
    )
    """Total spool-size cap in MiB; oldest files rotate out first."""
    trace_content: Literal["auto", "on", "off"] = Field(
        "auto",
        alias="INQTRIX_TRACE_CONTENT",
        description=(
            "Whether spans carry CONTENT (prompts, messages, redacted "
            "raw responses, search answers, kernel tool inputs and "
            "results) in addition to metadata. "
            "auto (default) follows OBSERVABILITY_PROFILE: content only "
            "in the forensic profile. on/off override explicitly. "
            "Content is always redacted through the existing sanitizers "
            "and hard-capped per attribute (see "
            "INQTRIX_TRACE_MAX_ATTR_BYTES) — Langfuse OSS applies no "
            "server-side truncation or masking."
        ),
    )
    """Span content capture: ``auto`` (follows OBSERVABILITY_PROFILE) | ``on`` | ``off``. Content is always redacted and byte-capped caller-side."""
    trace_max_attr_bytes: int = Field(
        1_048_576,
        alias="INQTRIX_TRACE_MAX_ATTR_BYTES",
        ge=1_024,
        description=(
            "Per-attribute byte cap for span content. Sized as a BACKSTOP "
            "against pathological payloads, not as a routine limit: a cap "
            "that trims normal prompts would defeat the purpose of "
            "forensic capture. 1 MiB keeps real prompts, responses and "
            "tool arguments whole (Langfuse itself only warns past 16 MB "
            "per span, and the OTel SDK sets no attribute-length limit at "
            "all). Every capped value still raises an inqtrix.truncation "
            "span event, so the rare cap is visible."
        ),
    )
    """Per-attribute byte backstop for span content (1 MiB; truncations are reported)."""
    trace_ui_url: str = Field(
        "",
        alias="INQTRIX_TRACE_UI_URL",
        description=(
            'Browser base URL of the Langfuse UI for the "Trace '
            'oeffnen" deep link (joined with the htmlPath the trace API '
            "returns — no projectId configuration needed). Often "
            "differs from the in-cluster OTLP endpoint, e.g. "
            "http://localhost:3300 for the bundled compose profile. "
            "Empty (default) hides the link."
        ),
    )
    """Browser base URL of the Langfuse UI for admin deep links; empty hides them."""
    trace_retention_days: int = Field(
        30,
        alias="INQTRIX_TRACE_RETENTION_DAYS",
        ge=0,
        description=(
            "Time-based retention for traces in Langfuse, enforced by "
            "Inqtrix's own worker cleanup job (the native Langfuse "
            "retention feature is EE-only self-hosted): traces older "
            "than this many days are batch-deleted via the Langfuse "
            "API. 0 disables the job. Only active with "
            "INQTRIX_TRACING=otlp; the file spool has its own "
            "size-based cap instead."
        ),
    )
    """Trace retention in days for the worker's Langfuse cleanup job (0 = off; otlp mode only)."""
    trace_scores: bool = Field(
        True,
        alias="INQTRIX_TRACE_SCORES",
        description=(
            "Export existing run-quality signals as Langfuse scores after successful "
            "publication. Uses the OTLP destination/authentication and background "
            "tracing lifecycle; inactive for file/local/off or non-Langfuse targets. "
            "No additional model calls. Scores are algorithm judgements, not "
            "independent proof of answer quality."
        ),
    )
    """Publish existing algorithm scores to the configured Langfuse OTLP sink."""
    audit_service_starts: bool = Field(
        True,
        alias="INQTRIX_AUDIT_SERVICE_STARTS",
        description=(
            "Enriches the service-start index the admin panel drills "
            "into: gates the chat.completed rows entirely and the "
            "metadata block (mode, duration, token sums) on run "
            "terminal rows. Run and indexing terminal rows themselves "
            "belong to the mandatory resource-effect trail and stay — "
            "audit as a whole has NO off switch. Never content."
        ),
    )
    """Service-start index enrichment (chat rows + run terminal detail; default on)."""
    audit_retention_days: int = Field(
        365,
        alias="INQTRIX_AUDIT_RETENTION_DAYS",
        ge=0,
        description=(
            "Time-based retention for audit_log rows, enforced by the "
            "worker via the SECURITY DEFINER function audit_prune "
            "(the app role itself holds INSERT/SELECT only). 365 days "
            "matches the ISO 27001 / BSI OPS.1.1.5 guidance. 0 disables "
            "pruning (rows are kept indefinitely)."
        ),
    )
    """Audit retention in days for the worker prune job (0 = keep forever)."""

    usage_retention_days: int = Field(
        730,
        alias="INQTRIX_USAGE_RETENTION_DAYS",
        ge=0,
        description=(
            "Time-based retention for llm_usage ledger rows (consumption "
            "history), enforced by the worker via the SECURITY DEFINER "
            "function llm_usage_prune (the app role holds INSERT/SELECT "
            "only). Default 730 days = 24 months of product data. "
            "0 disables pruning (rows are kept indefinitely)."
        ),
    )
    """Usage-ledger retention in days for the worker prune job (0 = keep forever)."""


class Settings(BaseSettings):
    """Root container that aggregates the Settings groups.

    This is the convenience entry point when a single object needs to
    expose all configuration at once (HTTP-server bootstrap, parity
    tooling, integration tests). Library-mode users typically work
    directly with :class:`AgentConfig` and never instantiate this
    class explicitly.
    """

    models: ModelSettings = Field(
        default_factory=ModelSettings,
        description=(
            "Per-role model identifiers. Initialised from environment "
            "variables on instantiation. Replace with a custom "
            "instance to inject test fixtures."
        ),
    )
    """Per-role model identifiers. Initialised from environment variables on instantiation. Replace with a custom instance to inject test fixtures."""
    agent: AgentSettings = Field(
        default_factory=AgentSettings,
        description=(
            "Behavioural tuning (loop bounds, timeouts, risk scoring, "
            "search cache). Initialised from environment variables and "
            "auto-applies the configured ``report_profile`` overrides."
        ),
    )
    """Behavioural tuning (loop bounds, timeouts, risk scoring, search cache). Initialised from environment variables and auto-applies the configured ``report_profile`` overrides."""
    server: ServerSettings = Field(
        default_factory=ServerSettings,
        description=(
            "HTTP-server-only settings. Ignored in pure library mode."
        ),
    )
    """HTTP-server-only settings. Ignored in pure library mode."""
    providers: ProviderSettings = Field(
        default_factory=ProviderSettings,
        description=(
            "Server-mode provider selection (LLM and search axes) plus the "
            "credentials and inference knobs not covered by ``models``/"
            "``server``. Defaults reproduce the historical LiteLLM+Perplexity "
            "stack. Ignored in pure library mode."
        ),
    )
    """Server-mode provider selection + credentials/inference knobs. Defaults to the historical LiteLLM+Perplexity stack."""
    auth: AuthSettings = Field(
        default_factory=AuthSettings,
        description=(
            "Authentication-mode settings for the HTTP server. "
            "Ignored in pure library mode."
        ),
    )
    """Authentication-mode settings for the HTTP server. Ignored in pure library mode."""
    knowledge: KnowledgeSettings = Field(
        default_factory=KnowledgeSettings,
        description=(
            "Knowledge-engine settings (internal document retrieval). "
            "Disabled by default."
        ),
    )
    """Knowledge-engine settings (internal document retrieval). Disabled by default."""
    storage: StorageSettings = Field(
        default_factory=StorageSettings,
        description=(
            "Persistence-backend settings for the platform layer "
            "(identity schema). Memory-backed by default."
        ),
    )
    """Persistence-backend settings for the platform layer (identity schema). Memory-backed by default."""
    queue: QueueSettings = Field(
        default_factory=QueueSettings,
        description=(
            "Run-queue backend and worker tuning. Memory-backed "
            "(in-process execution) by default."
        ),
    )
    """Run-queue backend and worker tuning. Memory-backed (in-process execution) by default."""
    quota: QuotaSettings = Field(
        default_factory=QuotaSettings,
        description=(
            "Per-user usage quotas (oidc multi-user). Disabled by "
            "default; the operator-ceiling layer of the two-level rule."
        ),
    )
    """Per-user usage quotas (oidc multi-user). Disabled by default."""
    sharing: SharingSettings = Field(
        default_factory=SharingSettings,
        description=(
            "Resource-sharing policy. Tenant-wide by default; can confine "
            "sharing to workspace co-members (cookie-session modes)."
        ),
    )
    """Resource-sharing policy (tenant-wide by default)."""
    editor_guest_links: EditorGuestLinkSettings = Field(
        default_factory=EditorGuestLinkSettings,
        description=(
            "Optional password-protected editor links for guests without an "
            "account. Disabled by default and dependent on sharing, live "
            "collaboration, Postgres, and HTTPS."
        ),
    )
    """Optional account-less editor-link settings."""
    collaboration: CollaborationSettings = Field(
        default_factory=CollaborationSettings,
        description=(
            "Optional live-collaboration settings for editor documents. "
            "Disabled by default and fail-loud when explicitly enabled."
        ),
    )
    """Optional editor live-collaboration settings. Disabled by default; enabled configurations are validated as one strict contract."""
    agent_platform: AgentPlatformSettings = Field(
        default_factory=AgentPlatformSettings,
        description=(
            "Workspace-agent platform limits (INQTRIX_AGENT_*): autonomy "
            "default, wave/probe/replan ceilings, the volatile dev escape."
        ),
    )
    """Workspace-agent platform limits (``INQTRIX_AGENT_*``)."""
    observability: ObservabilitySettings = Field(
        default_factory=ObservabilitySettings,
        description=(
            "Trace-sink selection (INQTRIX_TRACING: off|local|file|otlp) "
            "and trace volume knobs. Off by default; requires the "
            "`observability` extra for any non-off value."
        ),
    )
    """Trace-sink selection and volume knobs (``INQTRIX_TRACING``)."""

    @model_validator(mode="after")
    def _warn_forensic_without_recording_sink(self) -> "Settings":
        """Forensic depth without a recording sink records NOTHING.

        The two knobs are orthogonal by design (depth vs. destination),
        and that is exactly how an operator ends up asking for full
        lineage while every span is dropped: ``off`` installs no
        provider at all, ``local`` installs an ALWAYS_OFF sampler, and a
        sample rate below 1.0 drops whole traces head-first — forensic
        is the one profile whose promise is PER-RUN completeness.
        Silence here would be the worst kind of fallback: the operator
        believes they have a forensic record and has none.
        """
        profile = str(
            getattr(self.agent, "observability_profile", "") or ""
        ).strip().lower()
        if profile != "forensic":
            return self
        mode = str(self.observability.tracing or "off")
        rate = float(self.observability.trace_sample_rate or 0.0)
        if mode in ("off", "local"):
            _log.warning(
                "OBSERVABILITY_PROFILE=forensic with INQTRIX_TRACING=%s: no spans are "
                "recorded (off installs no provider; local deliberately samples "
                "everything out), so forensic detail has no destination. Set "
                "INQTRIX_TRACING=file (file spool) or otlp.",
                mode,
            )
        elif rate < 1.0:
            _log.warning(
                "OBSERVABILITY_PROFILE=forensic with INQTRIX_TRACE_SAMPLE_RATE=%s: head"
                " sampling decides per run, not per event; approximately %.0f%% of runs"
                " have no forensic record. Set 1.0 for complete lineage.",
                rate,
                max(0.0, (1.0 - rate) * 100),
            )
        return self
