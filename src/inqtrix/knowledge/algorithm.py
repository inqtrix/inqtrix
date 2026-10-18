"""The knowledge algorithm: retrieve from internal documents, answer cited.

The pipeline runs synchronously on a run-worker thread. The request's
retrieval profile selects its stages within the operator ceiling:

1. Resolve the retrieval query: a conversational turn may be rewritten
   into a standalone query, and a decomposing profile splits it into
   sub-queries.
2. Retrieve from the knowledge store for the query and every sub-query,
   reranked when the profile asks for it and a reranker is wired.
3. When the profile enables the sufficiency gate, rewrite and retrieve
   again while the evidence is judged insufficient, up to the operator's
   round cap.
4. Render the hits as a ``[K#]``-labelled evidence block (budgeted to
   the LLM's context window) and answer through the ``knowledge_answer``
   routing node, quote-then-answer when grounding is enabled.

The raw result dict mirrors the web-research shape (``answer``,
``usage``, ``result_state``) so the existing run serialization
(``ResearchResult.from_raw``, snapshots, SSE events) consumes knowledge
runs without a parallel code path.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from inqtrix.i18n import t
from inqtrix.core.results import AgentResult, RunRequest
from inqtrix.exceptions import AgentCancelled
from inqtrix.execution_failures import RunExecutionFailure
from inqtrix.providers.base import INCOMPLETE_FINISH_REASONS, provider_cancel_scope
from inqtrix.i18n import detect_ui_language_confident
from inqtrix.knowledge.contextualize import contextualize_followup_question
from inqtrix.knowledge.evidence import KnowledgeEvidenceProjector
from inqtrix.untrusted_content import untrusted_fence
from inqtrix.knowledge.gate import GateDecision, evaluate_evidence
from inqtrix.knowledge.grounding import (
    GroundingFailureCode,
    GroundingReport,
    QuoteCheck,
    check_grounding,
    grounding_failure_message,
)
from inqtrix.knowledge.decompose import decompose_question
from inqtrix.observability.otel import operation_span
from inqtrix.knowledge.profiles import (
    EVIDENCE_K_MAX,
    KnowledgeStageCeiling,
    parse_knowledge_profile,
    resolve_run_plan,
)
from inqtrix.knowledge.retrieval import (
    interleave_candidates,
    merge_candidates,
    retrieve,
)
from inqtrix.knowledge.retrieval_warnings import (
    project_retrieval_exclusion_warnings,
)
from inqtrix.knowledge.stores.ports import (
    KnowledgeProviderContext,
    RetrievalCandidate,
    RetrievalCandidateBatch,
)
from inqtrix.model_routing import resolve_effort, resolve_model
from inqtrix.agents.markdown import normalize_agent_markdown
from inqtrix.observability.context import bind_purpose
from inqtrix.prompt_language import resolve_answer_language
from inqtrix.prompts import build_knowledge_answer_prompt
from inqtrix.sync_bridge import run_coro_sync

if TYPE_CHECKING:
    from inqtrix.core.context import RunContext, RuntimeContext

log = logging.getLogger("inqtrix")

_EVIDENCE_BUDGET_FLOOR_CHARS = 8_000
_PROMPT_RESERVED_TOKENS = 4_000
# Bounded answer regeneration: at most ONE visible retry after a
# quote-unverified grounding rejection (attempt 2 of 2). Never silent —
# the retry is announced via progress event, log line, and span attempt.
_ANSWER_GROUNDING_ATTEMPTS = 2

KNOWLEDGE_ANSWER_INCOMPLETE = "knowledge_answer_incomplete"
"""Terminal failure type when the answer provider delivered no complete
completion: an incomplete stop signal (``INCOMPLETE_FINISH_REASONS``) or no
text at all.  A provider outcome, deliberately outside the grounding
vocabulary — the grounding gate judges what the model wrote, and here the
model wrote nothing usable."""


def _answer_incomplete_message(*, language: str, reason: str) -> str:
    """Return the safe visible explanation for an incomplete answer completion."""

    if str(language or "").lower().startswith("de"):
        return (
            "Die Antwort wurde nicht veröffentlicht, weil das Modell keine "
            f"vollständige Antwort geliefert hat (Abbruchgrund: {reason})."
        )
    return (
        "The answer was not published because the model did not deliver a "
        f"complete answer (stop reason: {reason})."
    )

# Visible-not-silent marker: the BM25 tokenizer language does
# not match the confidently-detected query language, so the lexical branch's
# language-specific normalization (tokenizing/stemming/stopwords) does not apply
# cleanly and keyword retrieval is less reliable. NOT "contributes nothing" —
# query and documents pass through the same encoder, so exact terms can still
# match. This signal is query-vs-TOKENIZER only; query-vs-DOCUMENT visibility
# needs per-collection language, which collections do not record.
SPARSE_MARKER_TOKENIZER_MISMATCH = "_knowledge_sparse_tokenizer_mismatch"


def _sparse_tokenizer_state(
    knowledge: KnowledgeProviderContext,
    question: str,
    emit: "Any",
) -> dict[str, Any] | None:
    """Flag a confident query-vs-BM25-tokenizer language mismatch, or ``None``.

    Returns a small visibility record (marker + the two language codes) only
    when the lexical branch is active (``sparse_language`` is set) AND the query
    language is *confidently* detected AND it differs from the tokenizer
    language. Otherwise ``None`` — so the same-language default path adds no new
    field, log, or event (byte-/field-identical). The log carries only the two
    language codes plus the marker, never the query text (sensitivity discipline,
    cf. :func:`inqtrix.urls.sanitize_log_message`).
    """
    sparse_language = getattr(knowledge.store, "sparse_language", None)
    if sparse_language is None:
        return None
    query_language = detect_ui_language_confident(question)
    if query_language is None or query_language == sparse_language:
        return None
    log.warning(
        "Knowledge-Sparse: query language (%s) differs from BM25 tokenizer language "
        "(%s); lexical normalization is unreliable, so sparse quality is less reliable "
        "(exact terms can still match). Optionally enable a multilingual cross-encoder "
        "reranker (for example, Cohere rerank-v3.5). (%s)",
        query_language,
        sparse_language,
        SPARSE_MARKER_TOKENIZER_MISMATCH,
    )
    emit(
        "inqtrix.knowledge.sparse.tokenizer_mismatch",
        {
            "query_language": query_language,
            "sparse_language": sparse_language,
            "marker": SPARSE_MARKER_TOKENIZER_MISMATCH,
        },
    )
    return {
        "marker": SPARSE_MARKER_TOKENIZER_MISMATCH,
        "query_language": query_language,
        "sparse_language": sparse_language,
    }


def _resolve_collection_ids(request: RunRequest) -> list[str] | None:
    """Read the optional collection scope from the request filters."""
    raw = request.knowledge_filters.get("collection_ids")
    if raw is None:
        return None
    return [str(item) for item in raw]


def _scoped_document_count(
    knowledge: KnowledgeProviderContext, collection_ids: list[str] | None
) -> int | None:
    """Total documents in the queried scope — ALL eligible for retrieval (the
    filter is collection-level, so every document is searched). Surfaces the
    "N documents searched" coverage signal. Returns ``None`` if the count can't
    be resolved (logged, never silent) so the UI omits it rather than guess."""

    # An explicitly empty, already-authorized scope is semantically different
    # from an absent scope.  It must neither touch the store nor expose the
    # aggregate size of collections that are outside the run's pinned scope.
    if collection_ids == []:
        return 0

    async def _count() -> int:
        if collection_ids:
            total = 0
            for collection_id in collection_ids:
                total += (
                    await knowledge.store.get_collection(collection_id)
                ).document_count
            return total
        collections = await knowledge.store.list_collections()
        return sum(collection.document_count for collection in collections)

    try:
        return run_coro_sync(_count())
    except Exception as exc:  # noqa: BLE001 - a count must never fail the answer
        log.warning(
            "Knowledge: could not determine the collection document count "
            "(error_type=%s).",
            type(exc).__name__,
        )
        return None


def _render_evidence_entry(index: int, candidate: RetrievalCandidate) -> str:
    """One ``[K#]``-labelled evidence entry (title line + source text).

    Carries the chunk's SOURCE text, never the contextualization prefix
    that was prepended for indexing. That prefix is model-composed prose
    written to make the chunk findable; it is not part of the document
    and can state facts the chunk itself does not contain. Passing it
    here would let the answering model treat generated text as source
    content — a paraphrase built on it would read as grounded while
    citing nothing, and quote verification (which runs against the
    source text) could not catch it.
    """
    evidence = KnowledgeEvidenceProjector.project(
        candidate, reference_id=f"K{index}"
    )
    return (
        f"[K{index}] {candidate.document_title} "
        f"(Abschnitt {candidate.chunk.chunk_index + 1})\n"
        f"{evidence.excerpt}"
    )


def _render_evidence_block(
    candidates: list[RetrievalCandidate],
    *,
    max_chars: int,
) -> tuple[str, list[RetrievalCandidate]]:
    """Render ``[K#]`` evidence entries up to the character budget.

    Returns the rendered block and the candidates that actually fit —
    the reference list and the answer prompt must describe the same
    set, so truncation happens once, here, and visibly via the
    returned subset.
    """
    entries: list[str] = []
    used: list[RetrievalCandidate] = []
    total = 0
    for index, candidate in enumerate(candidates, start=1):
        entry = _render_evidence_entry(index, candidate)
        if used and total + len(entry) > max_chars:
            break
        entries.append(entry)
        used.append(candidate)
        total += len(entry)
    if not entries:
        return "", used
    # Document text is material for the answer, never an instruction to it.
    return untrusted_fence("\n\n".join(entries), "knowledge-evidence"), used


class KnowledgeAlgorithm:
    """Retrieve-and-answer over the deployment's knowledge collections.

    Args:
        knowledge: The wired knowledge capabilities (embeddings, store,
            retrieval defaults). Constructor-First: the composition
            root builds and injects this bundle; the algorithm never
            reads settings or the environment.
        citation_base_url: Public base URL of the serving deployment.
            When non-empty, references become clickable HTTP links
            into ``/v1/sources/...``; empty keeps the internal
            ``inqtrix://`` URI scheme (deliberate, visible default —
            never a guessed hostname).
        gate_enabled: Operator ceiling for the sufficiency gate;
            ``False`` removes the gate from EVERY retrieval profile
            (the always-answer-from-top-k path).
        grounding_enabled: Operator ceiling for quote-then-answer:
            the answer prompt requires a verbatim-quote block which is
            verified deterministically against the evidence. Only a fully
            parsed response whose every quote verifies is publishable; a
            malformed block or one unverifiable quote returns a typed terminal
            failure with a safe explanation. Verified quote blocks are
            stripped from the user-facing answer. ``False`` keeps the plain
            single-section answer prompt in every profile.
        gate_max_rounds: Hard cap on gate rewrite-and-retrieve rounds
            for every profile; the deep profile requests up to this
            many, standard exactly one. Bounds the worst-case cost of
            an agentic run.

    The constructor flags form the operator CEILING; the per-request
    retrieval profile selects within it (resolved to a frozen
    :class:`~inqtrix.knowledge.profiles.KnowledgeRunPlan` inside
    ``run()``). The instance is a shared singleton across server
    threads and workers — per-request state never lives on ``self``.
    """

    id = "knowledge"
    display_name = "Knowledge Retrieval"

    def __init__(
        self,
        *,
        knowledge: KnowledgeProviderContext,
        citation_base_url: str = "",
        gate_enabled: bool = True,
        grounding_enabled: bool = True,
        gate_max_rounds: int = 3,
    ) -> None:
        self._knowledge = knowledge
        self._citation_base_url = citation_base_url.rstrip("/")
        self._ceiling = KnowledgeStageCeiling(
            gate_available=gate_enabled,
            grounding_available=grounding_enabled,
            reranker_available=knowledge.reranker is not None,
            gate_max_rounds=gate_max_rounds,
            rerank_candidate_depth=knowledge.rerank_candidate_depth,
        )

    def _reference_url(self, candidate: RetrievalCandidate) -> str:
        """Citation target for one evidence chunk.

        HTTP links into the sources view when the deployment knows its
        public base URL; the chunk index travels as a query parameter
        because the export pipeline strips URL fragments.
        """
        document_id = candidate.chunk.document_id
        chunk_index = candidate.chunk.chunk_index
        if self._citation_base_url:
            return (
                f"{self._citation_base_url}/v1/sources/{document_id}"
                f"?chunk={chunk_index}"
            )
        return f"inqtrix://documents/{document_id}#chunk-{chunk_index}"

    def capabilities(self) -> dict:
        """Manifest entry for the capability endpoint and clients.

        ``supports_chat_completions`` makes this algorithm streamable on
        ``/v1/chat/completions`` (streaming dispatches through the registry).
        It emits structured events for the native ``/v1/runs`` surface; the
        chat SSE surface streams the answer without granular progress lines
        (no ``event_sink`` there).
        """
        return {
            "requires": ["llm", "embeddings", "knowledge_store"],
            "streams_events": True,
            "supports_chat_completions": True,
            "terminal_node": "knowledge",
            "produces": ["answer", "references"],
            "supports": ["collection_filters"],
        }

    def _run_gate(
        self,
        llm: Any,
        context: "RunContext",
        question: str,
        evidence_block: str,
        emit,
        usage_accumulator: dict[str, int],
        *,
        round_index: int = 0,
        vocabulary_bridge: bool = False,
    ) -> GateDecision:
        """One sufficiency evaluation on the fast tier (mini model)."""
        provider_models = getattr(llm, "models", None)
        requested_tier = (
            context.agent_settings.model_tier or ""
        ).strip() or None
        gate_model = (
            resolve_model("knowledge_gate", provider_models, requested_tier)
            or None
            if provider_models is not None
            else None
        )
        decision, gate_usage = evaluate_evidence(
            llm,
            question=question,
            evidence_block=evidence_block,
            model=gate_model,
            timeout=context.agent_settings.reasoning_timeout,
            vocabulary_bridge=vocabulary_bridge,
        )
        usage_accumulator["prompt_tokens"] += gate_usage["prompt_tokens"]
        usage_accumulator["completion_tokens"] += gate_usage[
            "completion_tokens"
        ]
        emit(
            "inqtrix.knowledge.gate.evaluated",
            {
                "sufficient": decision.sufficient,
                "coverage": decision.coverage,
                "marker": decision.marker,
                "rewritten": decision.rewritten_query is not None,
                "round": round_index,
            },
        )
        return decision

    def _run_decomposition(
        self,
        llm: Any,
        context: "RunContext",
        question: str,
        emit,
        usage_accumulator: dict[str, int],
    ) -> tuple[str, ...]:
        """One decomposition call on the fast tier (deep profile only)."""
        provider_models = getattr(llm, "models", None)
        requested_tier = (
            context.agent_settings.model_tier or ""
        ).strip() or None
        decompose_model = (
            resolve_model(
                "knowledge_decompose", provider_models, requested_tier
            )
            or None
            if provider_models is not None
            else None
        )
        decomposition, usage = decompose_question(
            llm,
            question=question,
            model=decompose_model,
            timeout=context.agent_settings.reasoning_timeout,
        )
        usage_accumulator["prompt_tokens"] += usage["prompt_tokens"]
        usage_accumulator["completion_tokens"] += usage["completion_tokens"]
        emit(
            "inqtrix.knowledge.decomposition.completed",
            {
                "sub_query_count": len(decomposition.sub_queries),
                "marker": decomposition.marker,
            },
        )
        return decomposition.sub_queries

    def _run_query_contextualization(
        self,
        llm: Any,
        context: "RunContext",
        request: RunRequest,
        emit,
        usage_accumulator: dict[str, int],
    ) -> tuple[str, dict[str, Any]]:
        """Resolve the retrieval query for a conversational Knowledge turn.

        Args:
            llm: Provider used for the optional standalone-query rewrite.
            context: Per-run container with model routing and timeout settings.
            request: Incoming Knowledge run request. ``history`` controls
                whether contextualization is attempted.
            emit: Event sink used to expose the contextualization decision.
            usage_accumulator: Mutable prompt/completion-token totals for this
                pre-retrieval step.

        Returns:
            A tuple of the query text used by retrieval and a compact state
            dictionary for the final ``result_state``. No-history turns return
            the original question and are omitted from ``result_state`` to keep
            the historical no-history payload stable.
        """
        if not request.history.strip():
            return request.question, {
                "used_history": False,
                "rewritten": False,
                "marker": None,
            }
        provider_models = getattr(llm, "models", None)
        requested_tier = (
            context.agent_settings.model_tier or ""
        ).strip() or None
        contextualize_model = (
            resolve_model(
                "knowledge_contextualize", provider_models, requested_tier
            )
            or None
            if provider_models is not None
            else None
        )
        result, usage = contextualize_followup_question(
            llm,
            question=request.question,
            history=request.history,
            model=contextualize_model,
            timeout=context.agent_settings.reasoning_timeout,
        )
        usage_accumulator["prompt_tokens"] += usage["prompt_tokens"]
        usage_accumulator["completion_tokens"] += usage["completion_tokens"]
        emit(
            "inqtrix.knowledge.contextualized",
            {
                "used_history": True,
                "rewritten": result.rewritten,
                "marker": result.marker,
            },
        )
        return result.question, {
            "used_history": True,
            "rewritten": result.rewritten,
            "marker": result.marker,
        }

    def run(
        self,
        request: RunRequest,
        *,
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> AgentResult:
        """Execute retrieval + cited answer synthesis for one question.

        Binds the run's cancel probe for every provider call on this
        thread — providers consult it at retry-attempt boundaries and
        during backoff sleeps; an in-flight HTTP attempt still runs to
        its transport timeout — then delegates to the pipeline, whose
        stage checkpoints raise :class:`AgentCancelled` between stages.
        """
        probe = (
            context.cancel_token.is_set
            if context.cancel_token is not None
            else None
        )
        with provider_cancel_scope(probe):
            return self._run_pipeline(request, runtime=runtime, context=context)

    def _run_pipeline(
        self,
        request: RunRequest,
        *,
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> AgentResult:
        knowledge = self._knowledge
        emit = context.event_sink or (lambda _event, _payload: None)
        started = time.monotonic()
        llm = context.providers.llm

        def _check_cancel() -> None:
            token = context.cancel_token
            if token is not None and token.is_set():
                raise AgentCancelled(
                    "Knowledge query stopped after cancellation was requested."
                )

        cancel_probe = (
            context.cancel_token.is_set
            if context.cancel_token is not None
            else None
        )
        # Checkpoint BEFORE the first stage: a pre-set token must not pay
        # for the contextualization call.
        _check_cancel()
        collection_ids = _resolve_collection_ids(request)
        # Defense in depth: an authenticated ask MUST arrive pre-scoped —
        # the admission gate pins an omitted filter to the caller-visible set
        # before the run is stored.  Resolve this boundary before any model
        # call so an invalid or deliberately empty scope cannot consume model
        # work, let alone reach retrieval.
        if (
            context.principal is not None
            and context.principal.user_id is not None
            and collection_ids is None
        ):
            log.warning(
                "_knowledge_unscoped_principal: authenticated ask reached "
                "retrieval without a pinned collection scope; execution was "
                "blocked before retrieval."
            )
            emit(
                "inqtrix.knowledge.scope.unscoped_principal",
                {
                    "marker": "_knowledge_unscoped_principal",
                    "scope": "blocked",
                },
            )
            raise RunExecutionFailure(
                "knowledge_scope_missing",
                ("The knowledge query could not be safely restricted to the authorized "
                "collections and was stopped before searching."),
            )
        contextualize_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        if collection_ids == []:
            query_question = request.question
            contextualization_state = {
                "used_history": False,
                "rewritten": False,
                "marker": "empty_scope",
            }
            emit(
                "inqtrix.knowledge.scope.empty",
                {"scope": "empty", "collection_count": 0},
            )
        else:
            with operation_span("knowledge.contextualize"):
                query_question, contextualization_state = (
                    self._run_query_contextualization(
                        llm, context, request, emit, contextualize_usage
                    )
                )

        _check_cancel()
        raw_profile = request.knowledge_filters.get("profile")
        requested_profile = (
            parse_knowledge_profile(raw_profile)
            if raw_profile is not None
            else None
        )
        # The plan is a frozen LOCAL — this instance is shared across
        # threads/workers; per-request flags on `self` would race.
        plan = resolve_run_plan(
            requested_profile,
            question=query_question,
            ceiling=self._ceiling,
        )
        emit(
            "inqtrix.knowledge.profile.resolved",
            {
                "profile": plan.profile.value,
                "requested_profile": (
                    plan.requested_profile.value
                    if plan.requested_profile is not None
                    else None
                ),
                "auto_selected": plan.auto_selected,
                "auto_reason": plan.auto_reason,
                "rerank": plan.rerank,
                "gate_rounds": plan.gate_rewrite_rounds,
                "grounding": plan.grounding_enabled,
                "vocabulary_bridge": plan.vocabulary_bridge,
                "decompose": plan.decompose,
                "report": plan.report,
                "degraded_stages": list(plan.degraded_stages),
            },
        )

        # Make the BM25 tokenizer-language limitation visible (No Silent
        # Fallbacks). Returns None on the same-language default path, so
        # result_state stays field-identical there.
        sparse_state = _sparse_tokenizer_state(knowledge, query_question, emit)
        top_k = int(
            request.knowledge_filters.get("top_k", knowledge.default_top_k)
        )
        # `top_k` is the per-query retrieval width; `final_k` is how many chunks
        # actually reach the answer. The profile widens the latter (deep > 1.0)
        # so its decompose + gate fan-out surfaces a broader, multi-document
        # evidence set instead of being re-collapsed back to `top_k`. An explicit
        # request `top_k` stays the base the factor scales.
        # A request may pin `final_k` directly (validated to 1..EVIDENCE_K_MAX at
        # the resolver); the explicit value then beats the profile factor, and
        # the override is surfaced so the live ledger never hides it.
        explicit_final_k = request.knowledge_filters.get("final_k")
        final_k_overridden = explicit_final_k is not None
        final_k = (
            min(int(explicit_final_k), EVIDENCE_K_MAX)
            if final_k_overridden
            else min(int(top_k * plan.final_k_factor), EVIDENCE_K_MAX)
        )

        decompose_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        sub_queries: tuple[str, ...] = ()
        if plan.decompose and collection_ids != []:
            with operation_span("knowledge.decompose") as decompose_span:
                sub_queries = self._run_decomposition(
                    llm, context, query_question, emit, decompose_usage
                )
                if decompose_span is not None:
                    decompose_span.set_attribute(
                        "inqtrix.subquery_count", len(sub_queries)
                    )
        _check_cancel()

        def _on_rerank_retry(notice: dict[str, Any]) -> None:
            # Rerank retries stay visible on the run's event surface (no
            # silent fallbacks); the notice dict is the shared
            # _RetryNoticeMixin shape. Fires on the rerank worker thread
            # while this run-worker thread is parked in run_coro_sync, so
            # the emit is temporally exclusive per run.
            emit(
                "inqtrix.knowledge.rerank.retry",
                {
                    "provider": str(notice.get("provider", "")),
                    "model": str(notice.get("model", "")),
                    "attempt": int(notice.get("attempt", 0) or 0),
                    "max_attempts": int(notice.get("max_attempts", 0) or 0),
                    "delay_seconds": float(
                        notice.get("delay_seconds", 0.0) or 0.0
                    ),
                    "error_code": str(notice.get("error_code", "")),
                    "status_code": notice.get("status_code"),
                },
            )

        retrieval_degradations: list[dict[str, Any]] = []
        seen_retrieval_degradations: set[
            tuple[
                str,
                str,
                str,
                int | None,
                int | None,
                int | None,
                int,
                int | None,
            ]
        ] = set()

        def _retrieve(
            query: str, k: int = top_k
        ) -> RetrievalCandidateBatch:
            # Covers the initial, every sub-query, and every gate-rewrite
            # retrieval with one checkpoint.
            _check_cancel()
            # The research graph is synchronous (a node on a run-worker
            # thread); the knowledge store is async. Bridge per call.
            with operation_span(
                "knowledge.retrieve",
                {
                    "inqtrix.top_k": int(k),
                    "inqtrix.collection_count": len(collection_ids or []),
                    "inqtrix.rerank": bool(plan.rerank),
                },
            ) as retrieve_span:
                batch = run_coro_sync(
                    retrieve(
                        knowledge,
                        query=query,
                        collection_ids=collection_ids,
                        top_k=k,
                        use_reranker=plan.rerank,
                        rerank_candidate_depth=plan.rerank_candidate_depth,
                        on_provider_retry=_on_rerank_retry,
                        cancel_probe=cancel_probe,
                    )
                )
                if retrieve_span is not None:
                    retrieve_span.set_attribute(
                        "inqtrix.candidate_count",
                        len(getattr(batch, "candidates", []) or []),
                    )
                    retrieve_span.set_attribute(
                        "inqtrix.degradation_count",
                        len(getattr(batch, "degradations", []) or []),
                    )
            for degradation in batch.degradations:
                key = (
                    degradation.reason,
                    degradation.retrieval_mode,
                    degradation.stage,
                    degradation.requested_candidate_pool,
                    degradation.returned_candidate_pool,
                    degradation.final_top_k,
                    degradation.returned_hits,
                    degradation.candidate_cap,
                )
                if key in seen_retrieval_degradations:
                    continue
                seen_retrieval_degradations.add(key)
                payload = degradation.as_dict()
                retrieval_degradations.append(payload)
                emit("inqtrix.knowledge.retrieval.degraded", payload)
            return batch

        if collection_ids == []:
            candidates = RetrievalCandidateBatch()
        elif sub_queries:
            # Round-robin across the original question and every sub-query so
            # each aspect contributes; each list is per-query `top_k`, the union
            # is capped at the profile's wider `final_k`.
            result_lists = [_retrieve(query_question)] + [
                _retrieve(sub_query) for sub_query in sub_queries
            ]
            candidates = interleave_candidates(result_lists, limit=final_k)
        else:
            # No decomposition: a single query must fill the evidence budget
            # itself, so retrieve `final_k` directly.
            candidates = _retrieve(query_question, final_k)
        initial_retrieval_warnings = [
            warning.as_dict(include_message=False)
            for warning in project_retrieval_exclusion_warnings(
                candidates.exclusions
            )
        ]
        emit(
            "inqtrix.knowledge.retrieval.completed",
            {
                "candidate_count": len(candidates),
                "top_k": top_k,
                "final_k": final_k,
                "final_k_overridden": final_k_overridden,
                # Coverage: how many documents are in scope — ALL are searched
                # (the filter is collection-level), so this confirms the answer
                # considered every indexed document.
                "collection_document_count": _scoped_document_count(
                    knowledge, collection_ids
                ),
                "embedding_model": knowledge.embeddings.default_model,
                "degradations": retrieval_degradations,
                "warnings": initial_retrieval_warnings,
            },
        )
        context_window = getattr(llm, "context_window_tokens", None)
        budget_chars = max(
            _EVIDENCE_BUDGET_FLOOR_CHARS,
            ((context_window or 16_000) - _PROMPT_RESERVED_TOKENS) * 3,
        )
        evidence_block, used_candidates = _render_evidence_block(
            candidates, max_chars=budget_chars
        )
        if len(used_candidates) < len(candidates):
            log.warning(
                "Knowledge evidence reduced to %d of %d hits (context budget).",
                len(used_candidates),
                len(candidates),
            )
            emit(
                "inqtrix.knowledge.evidence.truncated",
                {
                    "kept": len(used_candidates),
                    "dropped": len(candidates) - len(used_candidates),
                },
            )

        queries_run = [query_question, *sub_queries]
        gate_state: dict[str, Any] = {"enabled": plan.gate_enabled}
        gate_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        if plan.gate_enabled and used_candidates:
            with operation_span(
                "knowledge.gate", {"inqtrix.gate.round": 0}
            ) as gate_span:
                decision = self._run_gate(
                    llm, context, query_question, evidence_block, emit,
                    gate_usage,
                    round_index=0,
                    vocabulary_bridge=plan.vocabulary_bridge,
                )
                if gate_span is not None:
                    gate_span.set_attribute(
                        "inqtrix.gate.sufficient", bool(decision.sufficient)
                    )
                    gate_span.set_attribute(
                        "inqtrix.gate.marker", str(decision.marker or "")
                    )
            gate_state.update(
                marker=decision.marker,
                sufficient=decision.sufficient,
                coverage=decision.coverage,
                reason=decision.reason,
            )
            rounds_used = 0
            # The agentic loop: rewrite-and-retrieve until the gate is
            # satisfied or the profile's round budget is spent. One
            # round reproduces the pre-profile flow call for call.
            while (
                not decision.sufficient
                and decision.rewritten_query
                and rounds_used < plan.gate_rewrite_rounds
            ):
                _check_cancel()
                rounds_used += 1
                queries_run.append(decision.rewritten_query)
                before = len(candidates)
                second = _retrieve(decision.rewritten_query, final_k)
                candidates = merge_candidates(
                    candidates, second, limit=final_k
                )
                if len(candidates) == before:
                    # The rewrite surfaced NO new evidence (merge dedupes on
                    # chunk id) — the corpus is exhausted for this question, so
                    # re-gating identical evidence would only repeat the verdict.
                    # Stop early: saves this round's gate LLM call AND every
                    # remaining round, and make the reason visible (no silent
                    # spin). Productive rounds (that DO add evidence) are
                    # unaffected.
                    gate_state["exhausted"] = True
                    emit(
                        "inqtrix.knowledge.gate.exhausted",
                        {"round": rounds_used, "reason": "no_new_evidence"},
                    )
                    break
                evidence_block, used_candidates = _render_evidence_block(
                    candidates, max_chars=budget_chars
                )
                with operation_span(
                    "knowledge.gate",
                    {"inqtrix.gate.round": rounds_used},
                ) as rewrite_gate_span:
                    decision = self._run_gate(
                        llm, context, query_question, evidence_block, emit,
                        gate_usage,
                        round_index=rounds_used,
                        vocabulary_bridge=plan.vocabulary_bridge,
                    )
                    if rewrite_gate_span is not None:
                        rewrite_gate_span.set_attribute(
                            "inqtrix.gate.sufficient",
                            bool(decision.sufficient),
                        )
                        rewrite_gate_span.set_attribute(
                            "inqtrix.gate.marker",
                            str(decision.marker or ""),
                        )
                gate_state.update(
                    marker=decision.marker,
                    sufficient=decision.sufficient,
                    coverage=decision.coverage,
                    reason=decision.reason,
                )
            gate_state.update(
                rounds_used=rounds_used,
                max_rounds=plan.gate_rewrite_rounds,
            )
            if rounds_used >= 1:
                # Back-compat key consumed by the answer-eval harness.
                gate_state["second_pass"] = True
            if not decision.sufficient and decision.coverage == "none":
                # Honest refusal ONLY for IRRELEVANT evidence: the
                # gate found nothing on-topic after the rewrite
                # budget — answering would fabricate confidence.
                # PARTIAL coverage answers instead: the answer prompt
                # names the gaps explicitly, which serves the user
                # strictly better than a blanket refusal (live-eval
                # finding: the binary verdict refused 10/18 answerable
                # DORA questions that had partial evidence).
                log.warning(
                    "Knowledge-Gate: evidence is irrelevant; returning an explicit "
                    "no-evidence answer.",
                )
                used_candidates = []
            elif not decision.sufficient:
                log.info(
                    "Knowledge-Gate: evidence is only partly sufficient; the answer "
                    "names the gaps.",
                )

        # ``candidates`` is a RetrievalCandidateBatch throughout single-query,
        # decomposition/interleave and gate-rewrite/merge paths.  Project its
        # complete typed exclusion ledger only after the final retrieval so a
        # late rewrite cannot disappear from the durable result.  The event is
        # an idempotent cumulative snapshot; the client merges it with the
        # earlier retrieval.completed snapshot and the final run receipt. Counts
        # denote observations across retrieval calls, not unique chunks: this
        # text-free aggregate intentionally carries no ids that could support
        # cross-query identity deduplication.
        retrieval_warnings = [
            warning.as_dict(include_message=False)
            for warning in project_retrieval_exclusion_warnings(
                candidates.exclusions
            )
        ]
        for warning in retrieval_warnings:
            emit("inqtrix.knowledge.retrieval.warning", warning)

        grounding_state: dict[str, Any] = {
            "enabled": plan.grounding_enabled
        }
        # The quotes of a PUBLISHED answer by citation label: they travel with
        # the reference they support. A rejected draft exposes none of them.
        published_quotes: dict[str, list[dict[str, Any]]] = {}
        terminal_failure: dict[str, str] | None = None
        # One language decision for the run: the answer and every reader
        # text of it, the refusals included, speak it. The interface
        # language decides only where the question leaves it open.
        answer_language = resolve_answer_language(
            request.question, ui_language=request.ui_language or None,
        )
        log.info(
            "answer language resolved: %s (source=%s)",
            answer_language.code or "unresolved",
            answer_language.source,
        )
        if not used_candidates:
            answer = t(
                {"language": answer_language.code},
                "knowledge_no_relevant_content",
            )
            usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0}
        else:
            provider_models = getattr(llm, "models", None)
            requested_tier = (
                context.agent_settings.model_tier or ""
            ).strip() or None
            requested_model = (context.agent_settings.model or "").strip()
            requested_effort = (context.agent_settings.effort or "").strip()
            if requested_model:
                model: str | None = requested_model
                effort: str | None = requested_effort or None
            elif provider_models is not None:
                model = (
                    resolve_model(
                        "knowledge_answer", provider_models, requested_tier
                    )
                    or None
                )
                effort = (
                    resolve_effort(
                        "knowledge_answer", provider_models, requested_tier
                    )
                    or None
                )
            else:
                model = None
                effort = None
            # Evidence excerpts are projected ONCE: prompt labels K1..Kn and
            # every grounding attempt must verify against the same surfaces.
            evidence_excerpts = (
                [
                    KnowledgeEvidenceProjector.project(
                        candidate, reference_id=f"K{index}"
                    ).excerpt
                    for index, candidate in enumerate(
                        used_candidates, start=1
                    )
                ]
                if plan.grounding_enabled
                else []
            )
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            attempts_state: list[dict[str, Any]] = []
            unverified: list[QuoteCheck] = []
            failed_quote_texts: tuple[str, ...] = ()
            report: GroundingReport | None = None
            for attempt in range(1, _ANSWER_GROUNDING_ATTEMPTS + 1):
                _check_cancel()
                prompt = build_knowledge_answer_prompt(
                    request.question,
                    evidence_block,
                    language=answer_language.code,
                    directive=answer_language.directive,
                    history=request.history,
                    grounding=plan.grounding_enabled,
                    report=plan.report,
                    unverified_quotes=failed_quote_texts,
                )
                try:
                    with bind_purpose("knowledge_answer"):
                        response = llm.complete_with_metadata(
                            prompt,
                            model=model,
                            reasoning_effort=effort,
                            timeout=context.agent_settings.reasoning_timeout,
                        )
                except AgentCancelled:
                    # A run cancellation is not a provider failure — the
                    # graceful-terminal fallback below must never turn a
                    # cancelled run into a grounding verdict.
                    raise
                except Exception as exc:
                    if attempt == 1 or report is None:
                        raise
                    # The regeneration call itself failed (throttle,
                    # timeout, overflow). Attempt 1's typed grounding
                    # rejection stays the graceful terminal outcome —
                    # raising here would discard its booked usage and
                    # turn a fail-closed verdict into a hard crash.
                    log.warning(
                        "Answer regeneration failed (error_type=%s); the initial "
                        "grounding failure remains the terminal outcome.",
                        type(exc).__name__,
                    )
                    failure_code = report.failure_code
                    if failure_code is None:
                        raise
                    message = grounding_failure_message(
                        failure_code,
                        language=answer_language.code or "",
                    )
                    answer = message
                    terminal_failure = {
                        "type": failure_code.value,
                        "message": message,
                    }
                    break
                answer = normalize_agent_markdown(response.content)
                # Additive across attempts: both calls are real spend and
                # the quota booking reads this one sum per run.
                usage["prompt_tokens"] += (
                    getattr(response, "prompt_tokens", 0) or 0
                )
                usage["completion_tokens"] += (
                    getattr(response, "completion_tokens", 0) or 0
                )
                finish_reason = (
                    str(getattr(response, "finish_reason", "") or "")
                    .strip()
                    .lower()
                )
                incomplete = finish_reason in INCOMPLETE_FINISH_REASONS
                if incomplete or not answer.strip():
                    # The provider stopped before the completion was finished
                    # (output budget, content filter) or returned no text at
                    # all: a provider outcome, never a grounding verdict — the
                    # grounding gate judges what the model wrote, and nothing
                    # usable was written.  Consumed usage stays booked; no
                    # retry (a second call against a backend that just failed
                    # is a decision, not a repair).
                    reason = finish_reason if incomplete else "empty"
                    log.warning(
                        "knowledge answer incomplete: the provider delivered "
                        "no publishable completion (finish_reason=%s, "
                        "content_chars=%d, completion_tokens=%s, "
                        "reasoning_tokens=%s, request_max_tokens=%s, "
                        "attempt=%d)",
                        finish_reason or "none",
                        len(response.content or ""),
                        getattr(response, "completion_tokens", None),
                        getattr(response, "reasoning_tokens", None),
                        getattr(response, "request_max_tokens", None),
                        attempt,
                    )
                    if report is not None and report.failure_code is not None:
                        # The regeneration delivered nothing: attempt 1's
                        # typed grounding rejection stays the terminal
                        # outcome, exactly as when the regeneration call
                        # itself fails.
                        message = grounding_failure_message(
                            report.failure_code,
                            language=answer_language.code or "",
                        )
                        terminal_failure = {
                            "type": report.failure_code.value,
                            "message": message,
                        }
                    else:
                        message = _answer_incomplete_message(
                            language=answer_language.code or "",
                            reason=reason,
                        )
                        terminal_failure = {
                            "type": KNOWLEDGE_ANSWER_INCOMPLETE,
                            "message": message,
                        }
                    answer = message
                    break
                if not plan.grounding_enabled:
                    break
                # Verification runs against the chunks' SOURCE text:
                # a "verbatim, verified" quote must exist in the cited
                # document, not in the contextualization prefix or the
                # rendering scaffolding the prompt carries.
                with operation_span("knowledge.grounding") as ground_span:
                    report = check_grounding(answer, evidence_excerpts)
                    if ground_span is not None:
                        verified = sum(
                            1 for quote in report.quotes if quote.verified
                        )
                        ground_span.set_attribute(
                            "inqtrix.grounding.attempt", attempt
                        )
                        ground_span.set_attribute(
                            "inqtrix.grounding.quotes_artifact_tolerated",
                            sum(
                                1
                                for quote in report.quotes
                                if quote.artifact_tolerated
                            ),
                        )
                        ground_span.set_attribute(
                            "inqtrix.grounding.status", report.status.value
                        )
                        ground_span.set_attribute(
                            "inqtrix.grounding.quotes_total",
                            len(report.quotes),
                        )
                        ground_span.set_attribute(
                            "inqtrix.grounding.quotes_verified", verified
                        )
                        ground_span.set_attribute(
                            "inqtrix.grounding.format_repaired",
                            bool(report.format_repaired),
                        )
                        if report.failure_code is not None:
                            ground_span.set_attribute(
                                "inqtrix.grounding.failure_code",
                                report.failure_code.value,
                            )
                answer = normalize_agent_markdown(report.answer)
                unverified = [
                    quote for quote in report.quotes if not quote.verified
                ]
                attempts_state.append(
                    {
                        "status": report.status.value,
                        "failure_code": (
                            report.failure_code.value
                            if report.failure_code is not None
                            else None
                        ),
                        "format_repaired": report.format_repaired,
                        "quotes_total": len(report.quotes),
                        "quotes_verified": len(report.quotes)
                        - len(unverified),
                        "quotes": [
                            {
                                "label": quote.label,
                                "text": quote.text,
                                "verified": quote.verified,
                                "artifact_tolerated": quote.artifact_tolerated,
                            }
                            for quote in report.quotes
                        ],
                    }
                )
                if report.publishable:
                    terminal_failure = None
                    break
                if (
                    attempt < _ANSWER_GROUNDING_ATTEMPTS
                    and report.failure_code
                    is GroundingFailureCode.QUOTE_UNVERIFIED
                ):
                    # One visible regeneration, never a silent retry: the
                    # progress step, this log line, and the event announce
                    # it before the second model call runs.
                    failed_quote_texts = tuple(
                        quote.text for quote in unverified
                    )
                    log.warning(
                        "Knowledge-Grounding: %d of %d quotes are unsupported; visible "
                        "answer regeneration (attempt %d/%d).",
                        len(unverified),
                        len(report.quotes),
                        attempt + 1,
                        _ANSWER_GROUNDING_ATTEMPTS,
                    )
                    emit(
                        "inqtrix.knowledge.answer.retry",
                        {
                            "attempt": attempt + 1,
                            "quotes_total": len(report.quotes),
                            "quotes_unverified": len(unverified),
                        },
                    )
                    continue
                failure_code = report.failure_code
                if failure_code is None:  # defensive: rejected => code
                    raise RuntimeError(
                        "Knowledge grounding rejected without a failure code"
                    )
                message = grounding_failure_message(
                    failure_code,
                    language=answer_language.code or "",
                )
                # Never expose the rejected model completion.  Returning a
                # safe diagnostic together with the established terminal
                # failure marker preserves usage/audit data while every
                # native/chat/Agent adapter can reject the result through
                # the ONE AgentResult terminal-failure contract.
                answer = message
                terminal_failure = {
                    "type": failure_code.value,
                    "message": message,
                }
                log.warning(
                    "Knowledge-Grounding rejected (failure_code=%s, quotes_total=%d, "
                    "quotes_unverified=%d).",
                    failure_code.value,
                    len(report.quotes),
                    len(unverified),
                )
                break
            if plan.grounding_enabled and report is not None:
                grounding_state.update(
                    marker=report.marker,
                    status=report.status.value,
                    failure_code=(
                        report.failure_code.value
                        if report.failure_code is not None
                        else None
                    ),
                    format_repaired=report.format_repaired,
                    quotes_total=len(report.quotes),
                    quotes_verified=len(report.quotes) - len(unverified),
                    quotes=[
                        {
                            "label": quote.label,
                            "text": quote.text,
                            "verified": quote.verified,
                            "artifact_tolerated": quote.artifact_tolerated,
                        }
                        for quote in report.quotes
                    ],
                    attempts=attempts_state,
                )
                emit(
                    "inqtrix.knowledge.grounding.checked",
                    {
                        "marker": report.marker,
                        "status": report.status.value,
                        "failure_code": (
                            report.failure_code.value
                            if report.failure_code is not None
                            else None
                        ),
                        "format_repaired": report.format_repaired,
                        "quotes_total": len(report.quotes),
                        "quotes_verified": len(report.quotes)
                        - len(unverified),
                    },
                )
                if report.publishable:
                    for quote in report.quotes:
                        published_quotes.setdefault(quote.label, []).append(
                            {
                                "text": quote.text,
                                "verified": quote.verified,
                                "artifact_tolerated": quote.artifact_tolerated,
                            }
                        )

        usage["prompt_tokens"] += (
            contextualize_usage["prompt_tokens"]
            + gate_usage["prompt_tokens"]
            + decompose_usage["prompt_tokens"]
        )
        usage["completion_tokens"] += (
            contextualize_usage["completion_tokens"]
            + gate_usage["completion_tokens"]
            + decompose_usage["completion_tokens"]
        )

        references = []
        for index, candidate in enumerate(used_candidates, start=1):
            evidence = KnowledgeEvidenceProjector.project(
                candidate, reference_id=f"K{index}"
            )
            references.append(
                {
                    "label": f"K{index}",
                    "url": self._reference_url(candidate),
                    "tier": "primary",
                    "title": evidence.title,
                # The exact retrieved passage travels WITH the citation so the
                # client can show "where this came from" (the cited chunk, with
                # the quoted span highlighted) without a second fetch, and open
                # the source reliably via the explicit document id. Both fields
                # carry the DOCUMENT's own text: a reader checking a citation
                # must see what the source says, not the indexing prefix a
                # model wrote about it.
                    "document_id": evidence.document_id,
                    "chunk_index": evidence.chunk_index,
                    "excerpt": evidence.excerpt,
                    "source_text": evidence.excerpt,
                # The verified quotes this citation supports, so the reader
                # badges the passage and highlights the quoted span.
                    "quotes": published_quotes.get(f"K{index}", []),
                    "source_span": evidence.as_dict()["source_span"],
                    "revision_id": evidence.revision_id,
                    "generation_id": evidence.generation_id,
                # Best-effort source page (PDFs only) for a page-level "open at
                # page N" jump; None when unmapped.
                    "page_number": evidence.page_number,
                }
            )
        raw: dict[str, Any] = {
            "answer": answer,
            "usage": usage,
            "result_state": {
                "answer": answer,
                "round": 1,
                "queries": queries_run,
                "knowledge_gate": gate_state,
                "knowledge_grounding": grounding_state,
                "all_citations": [reference["url"] for reference in references],
                "report_references": references,
                "knowledge_candidates": len(candidates),
                "knowledge_evidence_used": len(used_candidates),
                "knowledge_collections": collection_ids or [],
                "knowledge_profile": {
                    "id": plan.profile.value,
                    "requested": (
                        plan.requested_profile.value
                        if plan.requested_profile is not None
                        else None
                    ),
                    "auto_selected": plan.auto_selected,
                    "auto_reason": plan.auto_reason,
                    "degraded_stages": list(plan.degraded_stages),
                },
                "knowledge_retrieval": {
                    "degradations": retrieval_degradations,
                    **(
                        {"warnings": retrieval_warnings}
                        if retrieval_warnings
                        else {}
                    ),
                },
                "elapsed_seconds": round(time.monotonic() - started, 2),
            },
        }
        # Only present on a real tokenizer-language mismatch — keeps the
        # same-language default result_state field-identical (No Silent
        # Fallbacks without new noise).
        if sparse_state is not None:
            raw["result_state"]["knowledge_sparse"] = sparse_state
        if contextualization_state["used_history"]:
            raw["result_state"]["knowledge_contextualization"] = {
                **contextualization_state,
            }
        if terminal_failure is not None:
            raw["result_state"]["_terminal_failure"] = terminal_failure
        return AgentResult(
            answer=answer,
            result_type="knowledge_result",
            raw=raw,
        )
