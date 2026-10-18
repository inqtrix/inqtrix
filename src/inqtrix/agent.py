"""High-level ``ResearchAgent`` — the main public entry point.

Usage
-----
Minimal (uses environment variables or a local ``.env`` file for configuration)::

    from inqtrix import ResearchAgent

    agent = ResearchAgent()
    result = agent.research("Was ist der aktuelle Stand der GKV-Reform?")
    print(result.answer)
    print(result.metrics.confidence)

Custom configuration (Baukasten)::

    from inqtrix import (
        AgentConfig,
        LiteLLM,
        PerplexitySearch,
        ReportProfile,
        ResearchAgent,
    )

    llm = LiteLLM(
        api_key=os.getenv("LITELLM_API_KEY"),
        base_url="http://localhost:4000/v1",
        default_model="gpt-4o",
    )
    search = PerplexitySearch(
        api_key=os.getenv("PERPLEXITY_API_KEY"),
    )
    agent = ResearchAgent(AgentConfig(
        llm=llm,
        search=search,
        report_profile=ReportProfile.DEEP,
        max_rounds=3,
    ))
"""

from __future__ import annotations

import logging
import time
from queue import Empty, Queue
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, field_validator

from inqtrix.constants import (
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
    SEARCH_CACHE_MAXSIZE,
    SEARCH_CACHE_TTL,
    SEARCH_TIMEOUT,
)
from inqtrix.model_routing import validate_model_tier
from inqtrix.providers.base import LLMProvider, SearchProvider, ProviderContext
from inqtrix.report_profiles import ReportProfile
from inqtrix.result import ResearchResult
from inqtrix.settings import AgentSettings, ModelSettings
from inqtrix.strategies import (
    ClaimConsolidationStrategy,
    ClaimExtractionStrategy,
    RiskScoringStrategy,
    SourceTieringStrategy,
    StopCriteriaStrategy,
    StrategyContext,
)

log = logging.getLogger("inqtrix")


# ------------------------------------------------------------------ #
# AgentConfig — Pydantic model for all configuration
# ------------------------------------------------------------------ #


class AgentConfig(BaseModel):
    """Fully declarative agent configuration.

    Every field has a sensible default.  Pass only what you want to
    override.  Provider and strategy objects are accepted directly,
    enabling the *Baukasten* (building-block) pattern::

        AgentConfig(
            llm=LiteLLM(api_key="...", default_model="gpt-4o"),
            search=PerplexitySearch(api_key="..."),
            report_profile=ReportProfile.DEEP,
            max_rounds=2,
        )

    If ``llm`` or ``search`` are left as ``None`` (the default),
    :class:`ResearchAgent` will auto-create them from environment
    variables or a local ``.env`` file on first use (same behaviour as
    the FastAPI server).
    """

    # extra="forbid" makes the Python building-block layer as strict as the
    # HTTP layer (AgentOverridesRequest also forbids extras): an unknown or
    # since-removed field (a typo, or a knob deleted in a refactor) fails loudly
    # at construction with a ValidationError instead of being silently dropped.
    # This upholds "No Silent Fallbacks" for the constructor surface.
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    # -- Providers (None = auto-create from env vars) --
    llm: LLMProvider | None = Field(
        default=None,
        description=(
            "Concrete LLM provider used for classify, plan, claim extraction, "
            "evaluate, and answer calls. When ``None`` (the default), "
            "``ResearchAgent`` lazy-creates a ``LiteLLM`` provider from "
            "``Settings`` (env vars / .env) on first ``research()`` call. "
            "Pass an explicit instance (``LiteLLM``, ``AnthropicLLM``, "
            "``BedrockLLM``, ``AzureOpenAILLM``, or any ``LLMProvider`` "
            "subclass) when you want full Baukasten control over auth, "
            "deployment names, and request shaping."
        ),
    )
    """Concrete LLM provider used for classify, plan, claim extraction, evaluate, and answer calls. When ``None`` (the default), ``ResearchAgent`` lazy-creates a ``LiteLLM`` provider from ``Settings`` (env vars / .env) on first ``research()`` call. Pass an explicit instance (``LiteLLM``, ``AnthropicLLM``, ``BedrockLLM``, ``AzureOpenAILLM``, or any ``LLMProvider`` subclass) when you want full Baukasten control over auth, deployment names, and request shaping."""
    search: SearchProvider | None = Field(
        default=None,
        description=(
            "Concrete search provider used by the search node. When "
            "``None`` (the default), ``ResearchAgent`` lazy-creates a "
            "``PerplexitySearch`` provider from ``Settings``. Pass an "
            "explicit instance (``PerplexitySearch``, "
            "``AzureFoundryWebSearch``, or any ``SearchProvider`` "
            "subclass) to use a different backend or auth path."
        ),
    )
    """Concrete search provider used by the search node. When ``None`` (the default), ``ResearchAgent`` lazy-creates a ``PerplexitySearch`` provider from ``Settings``. Pass an explicit instance (``PerplexitySearch``, ``AzureFoundryWebSearch``, or any ``SearchProvider`` subclass) to use a different backend or auth path."""

    # -- Strategies (None = use defaults) --
    source_tiering: SourceTieringStrategy | None = Field(
        default=None,
        description=(
            "Strategy that maps URLs onto quality tiers (``primary``, "
            "``mainstream``, ``stakeholder``, ``unknown``, ``low``) and "
            "computes a per-batch quality score. ``None`` selects "
            "``DefaultSourceTiering`` (the structural floor of registry-class "
            "suffixes; every other source takes the role the claim-extraction "
            "call judges). "
            "Override to inject organisation-specific trust lists or "
            "external taxonomy services."
        ),
    )
    """Strategy that maps URLs onto quality tiers (``primary``, ``mainstream``, ``stakeholder``, ``unknown``, ``low``) and computes a per-batch quality score. ``None`` selects ``DefaultSourceTiering`` (the structural floor of registry-class suffixes; every other source takes the role the claim-extraction call judges). Override to inject organisation-specific trust lists or external taxonomy services."""
    claim_extraction: ClaimExtractionStrategy | None = Field(
        default=None,
        description=(
            "Strategy that extracts structured claims from each search "
            "result during the search node. ``None`` selects "
            "``LLMClaimExtractor`` (uses the configured LLM provider). "
            "Override for non-LLM extraction (regex/pipeline) or to "
            "force a different model than the claim-extraction role."
        ),
    )
    """Strategy that extracts structured claims from each search result during the search node. ``None`` selects ``LLMClaimExtractor`` (uses the configured LLM provider). Override for non-LLM extraction (regex/pipeline) or to force a different model than the claim-extraction role."""
    claim_consolidation: ClaimConsolidationStrategy | None = Field(
        default=None,
        description=(
            "Strategy that deduplicates extracted claims, computes "
            "support/contradict counts and assigns ``verified`` / "
            "``contested`` / ``unverified`` status. ``None`` selects "
            "``DefaultClaimConsolidator``. Override to plug in a custom "
            "verification pipeline or external knowledge base."
        ),
    )
    """Strategy that deduplicates extracted claims, computes support/contradict counts and assigns ``verified`` / ``contested`` / ``unverified`` status. ``None`` selects ``DefaultClaimConsolidator``. Override to plug in a custom verification pipeline or external knowledge base."""
    risk_scoring: RiskScoringStrategy | None = Field(
        default=None,
        description=(
            "Strategy that scores question risk (0-10) and derives "
            "required aspects. ``None`` selects ``KeywordRiskScorer``. "
            "Override to use external classifiers or domain-specific risk "
            "signals."
        ),
    )
    """Strategy that scores question risk (0-10) and derives required aspects. ``None`` selects ``KeywordRiskScorer``. Override to use external classifiers or domain-specific risk signals."""
    stop_criteria: StopCriteriaStrategy | None = Field(
        default=None,
        description=(
            "Strategy that runs the 9-signal stop cascade in the "
            "evaluate node (confidence, plateau, utility, falsification, "
            "stagnation, etc.). ``None`` selects ``MultiSignalStopCriteria``. "
            "Override to replace the stop heuristic without touching the "
            "graph wiring."
        ),
    )
    """Strategy that runs the 9-signal stop cascade in the evaluate node (confidence, plateau, utility, falsification, stagnation, etc.). ``None`` selects ``MultiSignalStopCriteria``. Override to replace the stop heuristic without touching the graph wiring."""

    # -- Behaviour --
    report_profile: ReportProfile = Field(
        default=ReportProfile.COMPACT,
        description=(
            "Controls the report style and research-depth preset. "
            "Use ``ReportProfile.COMPACT`` for the current concise "
            "answer style with lower latency. Use ``ReportProfile.DEEP`` "
            "to keep more evidence in the pipeline and produce a denser, "
            "review-style report with broader citation coverage and "
            "higher runtime/token cost. Explicit settings such as "
            "``max_rounds`` or ``confidence_stop`` still override the preset."
        ),
    )
    """Controls the report style and research-depth preset. Use ``ReportProfile.COMPACT`` for the current concise answer style with lower latency. Use ``ReportProfile.DEEP`` to keep more evidence in the pipeline and produce a denser, review-style report with broader citation coverage and higher runtime/token cost. Explicit settings such as ``max_rounds`` or ``confidence_stop`` still override the preset."""
    max_rounds: int = Field(
        default=MAX_ROUNDS,
        description=(
            "Hard upper bound for the research loop. The loop never runs "
            "more search rounds than this, regardless of confidence / "
            "plateau / utility signals. COMPACT defaults to 2; increase "
            "to 4 for DEEP-style coverage on complex topics."
        ),
    )
    """Hard upper bound for the research loop. The loop never runs more search rounds than this, regardless of confidence / plateau / utility signals. COMPACT defaults to 2; increase to 4 for DEEP-style coverage on complex topics."""
    min_rounds: int = Field(
        default=1,
        description=(
            "Lower bound for the research loop. Default ``1`` preserves "
            "existing behaviour (an early stop after Round 0 is allowed). "
            "Raise to ``2+`` when the evaluator model tends to over-"
            "confidently signal ``done`` before the STORM diversification "
            "in Round 1+ has had a chance to broaden the source pool. "
            "Typical effect of ``min_rounds=2``: at least one additional "
            "search round runs even if ``confidence_stop`` was already "
            "reached in Round 0. Clamped to ``max_rounds`` at request "
            "time so a misconfiguration never extends the loop beyond "
            "the user-specified hard cap."
        ),
    )
    """Lower bound for the research loop. Default ``1`` preserves existing behaviour (an early stop after Round 0 is allowed). Raise to ``2+`` when the evaluator model tends to over-confidently signal ``done`` before the STORM diversification in Round 1+ has had a chance to broaden the source pool. Typical effect of ``min_rounds=2``: at least one additional search round runs even if ``confidence_stop`` was already reached in Round 0. Clamped to ``max_rounds`` at request time so a misconfiguration never extends the loop beyond the user-specified hard cap."""
    confidence_stop: int = Field(
        default=CONFIDENCE_STOP,
        description=(
            "Minimum confidence (1-10) at which the loop is allowed to "
            "stop early. The evaluator model assigns the value; once it "
            "reaches this threshold, the stop cascade may emit ``done``. "
            "Default ``7`` matches the COMPACT profile; DEEP raises it "
            "to ``8`` for stricter evidence demands. Lower when latency "
            "matters more than evidence breadth."
        ),
    )
    """Minimum confidence (1-10) at which the loop is allowed to stop early. The evaluator model assigns the value; once it reaches this threshold, the stop cascade may emit ``done``. Default ``7`` matches the COMPACT profile; DEEP raises it to ``8`` for stricter evidence demands. Lower when latency matters more than evidence breadth."""
    first_round_queries: int = Field(
        default=FIRST_ROUND_QUERIES,
        description=(
            "Number of broad search queries the plan node generates in "
            "Round 0. Subsequent rounds generate ``max(6, "
            "first_round_queries - 2)`` targeted slot queries, and "
            "search executes the same width. DEEP profile uses ``8``."
        ),
    )
    """Number of broad search queries the plan node generates in Round 0. Subsequent rounds generate ``max(6, first_round_queries - 2)`` targeted slot queries, and search executes the same width. DEEP profile uses ``8``."""
    answer_prompt_citations_max: int = Field(
        default=ANSWER_PROMPT_CITATIONS_MAX,
        description=(
            "Hard upper bound on the number of citations the final "
            "answer prompt may reference. Caps prompt size for large-"
            "context models. This is the only internal citation cap for "
            "the answer body. DEEP profile raises it to ``500``."
        ),
    )
    """Hard upper bound on the number of citations the final answer prompt may reference. This is the only internal citation cap for the answer body. DEEP profile raises this to ``500``."""
    required_context_window_tokens: int = Field(
        default=REQUIRED_CONTEXT_WINDOW_TOKENS,
        description=(
            "Minimum model context-window size (in tokens) required for "
            "DEEP / forensic runs. Default ``128_000`` tracks the common "
            "128k model tier. Known smaller windows block normal report "
            "synthesis; unknown windows produce a visible capacity warning."
        ),
    )
    """Minimum model context-window size (in tokens) required for DEEP / forensic runs. Default ``128_000`` tracks the common 128k model tier. Known smaller windows block normal report synthesis; unknown windows produce a visible capacity warning."""
    max_total_seconds: int = Field(
        default=MAX_TOTAL_SECONDS,
        description=(
            "Wall-clock deadline for the entire research run, in "
            "seconds. The graph honours this as a soft deadline checked "
            "at node boundaries; in-flight provider calls may run "
            "slightly past it before the next check. Default ``3600`` "
            "is shared by all report profiles. Set to a "
            "higher value for slow models or unreliable upstream "
            "search APIs."
        ),
    )
    """Wall-clock deadline for the active research run, in seconds. The graph checks it at node boundaries and providers clamp every logical operation to the remaining budget. Default ``3600`` for every report profile."""
    max_question_length: int = Field(
        default=MAX_QUESTION_LENGTH,
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

    # -- Timeouts --
    reasoning_timeout: int = Field(
        default=REASONING_TIMEOUT,
        description=(
            "Per-call timeout (seconds) for reasoning LLM calls "
            "(classify, plan, evaluate, answer). The provider raises "
            "``AgentTimeout`` if a single call exceeds this. Increase "
            "for slow extended-thinking deployments; decrease to fail "
            "fast against unhealthy upstreams."
        ),
    )
    """Per-call timeout (seconds) for reasoning LLM calls (classify, plan, evaluate, answer). The provider raises ``AgentTimeout`` if a single call exceeds this. Increase for slow extended-thinking deployments; decrease to fail fast against unhealthy upstreams."""
    editor_assistant_timeout: int = Field(
        default=EDITOR_ASSISTANT_TIMEOUT,
        description=(
            "Per-call timeout (seconds) for editor suggest/instruct calls. "
            "Decoupled from ``reasoning_timeout`` so editor work (a full "
            "generation over large attached context) can be given a longer "
            "budget without lengthening every research reasoning call. "
            "Defaults to the shared 600-second AI-operation budget."
        ),
    )
    """Timeout in seconds for one logical editor suggest/instruct operation including retries. Defaults to the shared 600-second AI-operation budget."""
    search_timeout: int = Field(
        default=SEARCH_TIMEOUT,
        description=(
            "Timeout in seconds for one logical search-provider operation "
            "including retries, clamped to the active run deadline."
        ),
    )
    """Timeout in seconds for one logical search-provider operation including retries, clamped to the active run deadline."""
    claim_extract_timeout: int = Field(
        default=CLAIM_EXTRACT_TIMEOUT,
        description=(
            "Per-call timeout (seconds) for claim-extraction LLM calls. "
            "The shared 600-second budget covers the complete logical "
            "extraction operation including retries."
        ),
    )
    """Timeout in seconds for one logical claim-extraction operation including retries. Defaults to 600 seconds."""

    # -- Risk --
    high_risk_score_threshold: int = Field(
        default=HIGH_RISK_SCORE_THRESHOLD,
        description=(
            "Risk-score threshold (0-10) at and above which a question is "
            "flagged ``high_risk``. The flag is an observability signal only "
            "(forensic events, ``/health``, follow-up preservation); it does "
            "not change model selection -- use the model tiers or a per-node "
            "model override for that -- and drives no query/answer heuristic."
        ),
    )
    """Risk-score threshold (0-10) at and above which a question is flagged ``high_risk``. The flag is an observability signal only (forensic events, ``/health``, follow-up preservation); it does not change model selection -- use the model tiers or a per-node model override for that -- and drives no query/answer heuristic."""
    model_tier: str = Field(
        default="",
        description=(
            "Optional per-run tier selection ('high', 'mid', or 'fast'). "
            "When set, replaces mid/high default assignments; fast nodes "
            "keep their tier and effort. Explicit per-node model overrides "
            "still win. Empty string uses the default assignment."
        ),
    )
    """Optional per-run tier selection ('high', 'mid', or 'fast'). It replaces mid/high defaults; fast nodes keep their tier and effort. Explicit per-node model overrides still win. Empty uses the default assignment."""
    model: str = Field(
        default="",
        description=(
            "Optional explicit model id for the direct-chat answer (the UI "
            "model picker selecting a concrete model instead of a tier). When "
            "non-empty it bypasses tier routing for the direct-chat call only; "
            "the research pipeline keeps tier routing. Empty string uses the "
            "tier. Pair with ``effort``."
        ),
    )
    """Optional explicit model id for the direct-chat answer (the UI model picker selecting a concrete model instead of a tier). When non-empty it bypasses tier routing for the direct-chat call only; the research pipeline keeps tier routing. Empty string uses the tier. Pair with ``effort``."""
    effort: str = Field(
        default="",
        description=(
            "Optional reasoning effort for the direct-chat answer, paired with "
            "``model`` (``none``/``low``/``medium``/``high``/``xhigh``/``max``, "
            "model-dependent). Empty string inherits the provider default."
        ),
    )
    """Optional reasoning effort for the direct-chat answer, paired with ``model`` (``none``/``low``/``medium``/``high``/``xhigh``/``max``, model-dependent). Empty string inherits the provider default."""

    @field_validator("model_tier")
    @classmethod
    def _validate_model_tier(cls, value: str) -> str:
        """Reject an unknown ``model_tier`` loudly at construction instead of ignoring it."""
        return validate_model_tier(value)

    # -- Search cache --
    search_cache_maxsize: int = Field(
        default=SEARCH_CACHE_MAXSIZE,
        description=(
            "Maximum number of search results retained in the in-memory "
            "TTL cache. Cache hits skip the provider call, which "
            "matters when later runs issue overlapping queries. Set "
            "to ``0`` to disable the cache for testing."
        ),
    )
    """Maximum number of search results retained in the in-memory TTL cache. Cache hits skip the provider call when later runs issue overlapping queries. Set to ``0`` to disable the cache for testing."""
    search_cache_ttl: int = Field(
        default=SEARCH_CACHE_TTL,
        description=(
            "Time-to-live (seconds) for cached search results. Default "
            "``3600`` (1 hour) balances staleness against re-query "
            "cost. Lower for fast-moving topics (news), raise for "
            "stable reference questions."
        ),
    )
    """Time-to-live (seconds) for cached search results. Default ``3600`` (1 hour) balances staleness against re-query cost. Lower for fast-moving topics (news), raise for stable reference questions."""

    # -- Testing --
    testing_mode: bool = Field(
        default=False,
        description=(
            "When ``True``, exposes the ``/v1/test/run`` endpoint on "
            "the HTTP server (used by ``inqtrix-parity run``). Has no "
            "effect in library mode. Never enable in production: the "
            "endpoint accepts arbitrary research questions without rate "
            "limiting and returns full iteration logs."
        ),
    )
    """When ``True``, exposes the ``/v1/test/run`` endpoint on the HTTP server (used by ``inqtrix-parity run``). Has no effect in library mode. Never enable in production: the endpoint accepts arbitrary research questions without rate limiting and returns full iteration logs."""

    observability_profile: str = Field(
        default="summary",
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


# ------------------------------------------------------------------ #
# ResearchAgent — the Baukasten entry point
# ------------------------------------------------------------------ #


class ResearchAgent:
    """Iterative research agent with pluggable providers and strategies.

    The agent compiles a LangGraph state machine
    (classify → plan → search ↔ evaluate → answer) and orchestrates one
    research run per :meth:`research` or :meth:`stream` call. Providers
    and strategies are pulled from the bound :class:`AgentConfig`; any
    fields left as ``None`` are auto-created from environment variables
    on first use and then cached for the lifetime of the instance.

    The instance is **safe to reuse** across many sequential runs (the
    compiled graph and lazy-created providers are cached). It is **not
    thread-safe** for concurrent runs against the same instance —
    create one instance per worker, or wrap calls in a lock if you must
    share. Streaming via :meth:`stream` uses an internal background
    thread to drain progress; that does not make the rest of the API
    concurrent-safe.

    Attributes:
        config: The bound :class:`AgentConfig`. Read-only after
            construction; mutate by building a new agent instead.
        providers: The lazily-created :class:`ProviderContext`. Auto-
            created on first :meth:`research` / :meth:`stream` call;
            access this property to force eager creation.
        strategies: The lazily-created :class:`StrategyContext`. Same
            lifecycle as ``providers``.

    Example:
        >>> from inqtrix import ResearchAgent
        >>> agent = ResearchAgent()
        >>> result = agent.research("Was ist der Stand der GKV-Reform?")
        >>> print(result.answer)
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        """Bind a configuration to the agent without performing any I/O.

        Provider and strategy auto-creation is deferred until the first
        :meth:`research` / :meth:`stream` call so that constructing the
        agent never touches the network or environment.

        Args:
            config: Optional :class:`AgentConfig`. When ``None`` (the
                default), an empty ``AgentConfig`` is used and every
                provider/strategy is auto-created from environment
                variables on first call. Pass an explicit config for
                Baukasten-style provider injection or to override
                behaviour fields (timeouts, loop bounds, report
                profile).

        Example:
            >>> from inqtrix import AgentConfig, ResearchAgent
            >>> agent = ResearchAgent(AgentConfig(max_rounds=2))
        """
        self._config = config or AgentConfig()
        self._providers: ProviderContext | None = None
        self._strategies: StrategyContext | None = None
        self._settings: AgentSettings | None = None

    # -- Public API ------------------------------------------------ #

    def research(
        self,
        question: str,
        *,
        history: str = "",
    ) -> ResearchResult:
        """Run the full iterative research loop and return a typed result.

        Blocks until the loop terminates (either via the stop cascade
        or the wall-clock deadline). All progress events are discarded;
        use :meth:`stream` if you need live updates.

        Args:
            question: The user question. Must be non-empty and below
                ``AgentConfig.max_question_length`` characters,
                otherwise the agent rejects the input early.
            history: Optional pre-formatted conversation history string
                concatenated into the classify prompt. Empty string
                (default) starts without conversation context.

        Returns:
            A populated :class:`~inqtrix.result.ResearchResult` with
            answer, metrics, top sources, and top claims.
            ``metrics.elapsed_seconds`` is set from a monotonic clock
            measured around the graph execution.

        Raises:
            inqtrix.exceptions.AgentTimeout: When the run exceeds
                ``AgentConfig.max_total_seconds`` (checked at node
                boundaries).
            inqtrix.exceptions.AgentRateLimited: When a provider
                surfaces a 429 / daily-limit error that the SDK
                retry could not absorb.
            ValueError: For malformed input (empty question, oversize
                question).

        Example:
            >>> agent = ResearchAgent()
            >>> result = agent.research("Was ist Quantencomputing?")
            >>> result.metrics.confidence
            8
        """
        from inqtrix.graph import run

        providers, strategies, settings = self._ensure_initialised()

        t0 = time.monotonic()
        raw = run(
            question,
            history=history,
            providers=providers,
            strategies=strategies,
            settings=settings,
        )
        elapsed = time.monotonic() - t0

        result = ResearchResult.from_raw(raw, source_tiering=strategies.source_tiering)
        result.metrics.elapsed_seconds = round(elapsed, 2)
        return result

    def stream(
        self,
        question: str,
        *,
        history: str = "",
        include_progress: bool = True,
    ) -> Iterator[str]:
        """Run research and stream progress + answer chunks incrementally.

        Internally runs the same graph as :meth:`research` in a
        background thread and drains progress events from a queue.
        When the run completes, the final answer is yielded as
        word-aligned chunks for smooth UI rendering.

        Args:
            question: See :meth:`research`.
            history: See :meth:`research`.
            include_progress: When ``True`` (default), yield progress
                lines (each prefixed with ``"> "`` and terminated with
                ``"\\n"``), then a single ``"---\\n"`` separator, then
                the answer chunks. When ``False``, yield only the
                answer chunks. Use ``False`` when the consumer is
                another program that should only see answer text.

        Yields:
            UTF-8 string chunks. Progress lines are full lines
            (``"> message\\n"``); answer chunks are word-aligned and
            may not end on a newline.

        Raises:
            inqtrix.exceptions.AgentTimeout: Same conditions as
                :meth:`research` — propagated from the background
                thread when ``future.result()`` is awaited.
            inqtrix.exceptions.AgentRateLimited: Same conditions as
                :meth:`research`.

        Example:
            >>> agent = ResearchAgent()
            >>> for chunk in agent.stream("Meine Frage"):
            ...     print(chunk, end="", flush=True)
        """
        from inqtrix.graph import run
        from inqtrix.text import iter_word_chunks

        providers, strategies, settings = self._ensure_initialised()
        progress_queue: Queue | None = Queue() if include_progress else None

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                run,
                question,
                history=history,
                progress_queue=progress_queue,
                providers=providers,
                strategies=strategies,
                settings=settings,
            )

            # Yield progress updates while agent runs
            while include_progress and progress_queue is not None and not future.done():
                try:
                    kind, msg = progress_queue.get(timeout=0.3)
                    if kind == "progress" and msg != "done":
                        yield f"> {msg}\n"
                except Empty:
                    continue
                except Exception as exc:
                    log.warning(
                        "Progress queue disabled after an unexpected error "
                        "(error_code=%s)",
                        type(exc).__name__,
                    )
                    break

            # Drain remaining progress
            while include_progress and progress_queue is not None and not progress_queue.empty():
                try:
                    kind, msg = progress_queue.get_nowait()
                    if kind == "progress" and msg != "done":
                        yield f"> {msg}\n"
                except Empty:
                    break
                except Exception as exc:
                    log.warning(
                        "Remaining progress messages could not be read (error_code=%s)",
                        type(exc).__name__,
                    )
                    break

            raw = future.result()
            if include_progress:
                yield "---\n"
            for chunk in iter_word_chunks(raw.get("answer", "")):
                yield chunk

    # -- Properties ------------------------------------------------ #

    @property
    def config(self) -> AgentConfig:
        """Return the immutable :class:`AgentConfig` bound to this agent.

        Returns:
            The exact instance passed to the constructor (or an empty
            ``AgentConfig`` if none was supplied). Mutating this object
            after construction has no effect on already-cached
            providers / strategies; build a new ``ResearchAgent``
            instead.
        """
        return self._config

    @property
    def providers(self) -> ProviderContext:
        """Return the active providers, creating them if necessary.

        Touching this property forces lazy provider auto-creation,
        which may read environment variables and instantiate SDK
        clients. Use this for early-validation of the configuration
        before the first :meth:`research` call.

        Returns:
            The cached :class:`~inqtrix.providers.ProviderContext`. The
            same instance is returned on subsequent calls.
        """
        self._ensure_initialised()
        assert self._providers is not None
        return self._providers

    @property
    def strategies(self) -> StrategyContext:
        """Return the active strategies, creating them if necessary.

        Same eager-init behaviour as :attr:`providers`.

        Returns:
            The cached :class:`~inqtrix.strategies.StrategyContext`.
            The same instance is returned on subsequent calls.
        """
        self._ensure_initialised()
        assert self._strategies is not None
        return self._strategies

    # -- Internals ------------------------------------------------- #

    def _ensure_initialised(
        self,
    ) -> tuple[ProviderContext, StrategyContext, AgentSettings]:
        """Lazily create providers, strategies, and settings from config."""
        if self._providers is not None:
            assert self._strategies is not None
            assert self._settings is not None
            return self._providers, self._strategies, self._settings

        cfg = self._config
        settings = self._build_settings(cfg)
        self._settings = settings

        # -- Providers --
        llm = cfg.llm
        search = cfg.search
        if llm is None or search is None:
            from inqtrix.providers import create_providers
            from inqtrix.settings import Settings

            env_settings = Settings()
            full_settings = Settings(
                models=env_settings.models,
                server=env_settings.server,
                agent=settings,
            )
            auto = create_providers(full_settings)
            llm = llm or auto.llm
            search = search or auto.search

        # Attach model metadata for providers that don't expose it
        # (e.g. AnthropicLLM, custom LLMProvider implementations).
        if llm is not None and not hasattr(llm, "models"):
            from inqtrix.providers.base import ConfiguredLLMProvider

            llm = ConfiguredLLMProvider(
                llm,
                ModelSettings(
                    reasoning_model="",
                    classify_model="",
                    claim_extract_model="",
                    evaluate_model="",
                ),
            )

        # Library callers inject providers directly; instrument them
        # exactly like the factory path so spans, call metrics and
        # ledger rows do not depend on the construction route. The
        # wrappers are idempotent.
        from inqtrix.providers import instrument_providers
        from inqtrix.settings import Settings as _Settings

        self._providers = instrument_providers(
            ProviderContext(llm=llm, search=search),
            _Settings(agent=settings),
        )

        # -- Strategies --
        # Derived from the instrumented provider, never the raw one: the
        # claim extractor keeps the provider it is given and reads raw web
        # text with it, so a raw provider here would call the model without
        # the untrusted-content rule and without a trace.
        from inqtrix.strategies import create_default_strategies, resolve_claim_extract_model

        served_llm = self._providers.llm
        defaults = create_default_strategies(
            settings,
            llm=served_llm,
            claim_extract_model=resolve_claim_extract_model(served_llm),
            claim_extract_timeout=cfg.claim_extract_timeout,
        )
        self._strategies = StrategyContext(
            source_tiering=cfg.source_tiering or defaults.source_tiering,
            claim_extraction=cfg.claim_extraction or defaults.claim_extraction,
            claim_consolidation=cfg.claim_consolidation or defaults.claim_consolidation,
            risk_scoring=cfg.risk_scoring or defaults.risk_scoring,
            stop_criteria=cfg.stop_criteria or defaults.stop_criteria,
        )

        return self._providers, self._strategies, self._settings

    @staticmethod
    def _build_settings(cfg: AgentConfig) -> AgentSettings:
        """Build an AgentSettings instance from the flat AgentConfig."""
        env_defaults = AgentSettings()
        data = env_defaults.model_dump()
        explicit_fields = set(env_defaults.model_fields_set) | set(cfg.model_fields_set)
        for field_name in (
            "report_profile",
            "max_rounds",
            "min_rounds",
            "confidence_stop",
            "first_round_queries",
            "answer_prompt_citations_max",
            "required_context_window_tokens",
            "max_total_seconds",
            "max_question_length",
            "reasoning_timeout",
            "editor_assistant_timeout",
            "search_timeout",
            "claim_extract_timeout",
            "high_risk_score_threshold",
            "model_tier",
            "model",
            "effort",
            "search_cache_maxsize",
            "search_cache_ttl",
            "testing_mode",
            "observability_profile",
        ):
            if field_name in cfg.model_fields_set:
                data[field_name] = getattr(cfg, field_name)
        return AgentSettings(**data).with_report_profile_defaults(
            explicit_fields=explicit_fields,
        )
