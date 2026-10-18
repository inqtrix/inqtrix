"""Native run submission through the algorithm registry.

Owns the run work-callable behind the ``POST /v1/runs`` route: it
dispatches the resolved mode through
the registry, translates the algorithm result into the export payload,
and drives the run-handle lifecycle (events, answer deltas, snapshot,
completion). Read-side operations stay on the
:class:`~inqtrix.server.runs.RunStore` and are called directly by the
runs router — the store already is the in-memory repository.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.results import (
    CanvasContext,
    RunRequest,
    SourcePolicy,
    WebRecency,
    WorkHandover,
)
from inqtrix.execution_authority import AuthorizationRevoked, guard_provider_context
from inqtrix.execution_failures import RunExecutionFailure
from inqtrix.quota.models import QuotaDimension, QuotaSubject, consumed_tokens
from inqtrix.result import ResearchResult, reported_usage
from inqtrix.services.agent_answer_publisher import AgentAnswerPublisher
from inqtrix.services.agent_context import ResolvedAgentContext
from inqtrix.state import build_run_snapshot

if TYPE_CHECKING:
    from inqtrix.agents.control_ports import AgentControlStore
    from inqtrix.auth.principal import Principal
    from inqtrix.core.algorithms import AgentAlgorithm, AlgorithmRegistry
    from inqtrix.server.runs import RunHandle, RunStore
    from inqtrix.services.execution_dependency_authority import (
        ExecutionDependencyAuthorizer,
    )
    from inqtrix.services.quota_service import QuotaService


"""What a reported token count covers: the whole run, or one segment."""


def execute_run_request(
    handle: "RunHandle",
    *,
    algorithm: "AgentAlgorithm",
    run_request: RunRequest,
    resolved: ResolvedAgentContext,
    runtime: RuntimeContext,
    principal: "Principal | None",
    quota_service: "QuotaService | None" = None,
    quota_subject: "QuotaSubject | None" = None,
    token_budget: int | None = None,
    workspace_id: str | None = None,
    authority_check: Callable[[], None] | None = None,
    answer_publisher: AgentAnswerPublisher | None = None,
    trace_root: bool = True,
) -> None:
    """Execute one resolved run request against its algorithm.

    The single execution body shared by the in-process run thread and
    the queue worker: dispatches the algorithm, honours cancellation
    observed at graph node boundaries, shapes the export payload, and
    drives the handle lifecycle (answer deltas, snapshot, completion).
    Exceptions propagate to the caller, which owns the failure path.

    Args:
        handle: Store-backed run handle (events, complete, cancel).
        algorithm: The registry entry for the request's mode.
        run_request: The validated run request to execute.
        resolved: Stack/override/mode resolution for this request.
        runtime: App-level runtime context.
        principal: Submitting identity; ``None`` in worker processes,
            which carry the explicit quota account in *quota_subject*
            instead.
        quota_service: Optional usage meter. When wired (quotas on), the
            run's real LLM-token consumption is booked through its
            synchronous bridge AFTER execution (non-fatal — a recording
            failure never loses the run), and the per-run token budget
            aborts a runaway run at a node boundary. ``None`` for
            unmetered deployments — byte-identical to the historical
            path.
        quota_subject: Explicit quota account for the worker path,
            reconstructed from the run row's persisted effective-actor UUID
            and tenant. When ``None`` (the in-process path), the account is
            derived from *principal*. This makes token accounting fire
            regardless of which process executes the run.
        workspace_id: Persisted project namespace of the native run. Agent
            algorithms pass it unchanged to delegated children; it is not an
            authorization decision.
        answer_publisher: Shared durable publisher for native Agent Desk
            modes.  Those modes fail loudly when it is absent; other modes
            retain the event-only answer lifecycle.
    """
    t0 = time.monotonic()

    # Enrich the run root span (opened by the execution boundary —
    # worker _execute or in-process _run_worker) with the Langfuse trace
    # fields: explicit langfuse.* keys are REQUIRED because this span is
    # a child of the submitter's context, and Langfuse takes trace-level
    # fields only from the root span or these keys.
    from inqtrix.auth.log_redaction import stable_pseudonym
    from inqtrix.observability import semconv
    from inqtrix.observability.otel import (
        current_trace_id_hex,
        enrich_current_span,
        mark_current_span_error,
        operation_span,
    )

    subject_user_id = (
        getattr(principal, "user_id", None)
        or getattr(quota_subject, "user_id", None)
    )
    # getattr throughout: telemetry must never impose fields on the
    # request/handle doubles embedded callers and tests pass in.
    run_mode = str(getattr(run_request, "mode", "") or "")
    trace_metadata: dict[str, Any] = {}
    if trace_root:
        # Filterable trace metadata: Langfuse promotes ONLY the
        # ``langfuse.trace.metadata.*`` keys to top-level trace metadata,
        # so the run shape (mode, depth, tier, profile) is queryable
        # across runs without opening each trace. Child runs share the
        # parent's trace and must not overwrite its shape.
        trace_metadata = {
            semconv.trace_metadata_key("mode"): run_mode,
            semconv.trace_metadata_key("depth"): str(
                getattr(resolved.agent_settings, "depth", "") or ""
            ),
            semconv.trace_metadata_key("tier"): str(
                getattr(resolved.agent_settings, "agent_tier", "") or ""
            ),
            semconv.trace_metadata_key("profile"): str(
                getattr(resolved.agent_settings, "report_profile", "") or ""
            ),
            semconv.trace_metadata_key("stack"): (
                getattr(resolved, "stack_name", "") or ""
            ),
        }
    # Metrics feature label + ledger subject for every provider call
    # inside this segment (token counters per product feature; raw
    # booking identity for llm_usage rows). The executing loop clears
    # both vars in its finally — worker threads are reused.
    from inqtrix.observability.context import (
        bind_feature,
        bind_usage_subject,
    )

    bind_feature(run_mode)
    bind_usage_subject(
        getattr(principal, "tenant_id", None)
        or getattr(quota_subject, "tenant_id", None),
        subject_user_id,
        workspace_id,
    )
    enrich_current_span(
        {
            semconv.LANGFUSE_TRACE_NAME: f"run:{run_mode}",
            semconv.LANGFUSE_USER_ID: (
                stable_pseudonym("usr", subject_user_id)
                if subject_user_id is not None
                else ""
            ),
            semconv.LANGFUSE_SESSION_ID: (
                getattr(run_request, "session_id", "") or ""
            ),
            semconv.INQTRIX_WORKSPACE: workspace_id or "",
            semconv.INQTRIX_MODE: run_mode,
            semconv.INQTRIX_STACK: getattr(resolved, "stack_name", "") or "",
            # A role child shares its parent's trace; its span names the role.
            **(
                {semconv.INQTRIX_WORK_PROFILE: work_profile}
                if (work_profile := str(getattr(run_request, "work_profile", "") or ""))
                else {}
            ),
            **_requirement_span_attributes(run_request),
            **trace_metadata,
        }
    )
    # Persist the trace id as a durable run event so the admin surface
    # and the trace-export API can find the trace WITHOUT a lookup —
    # emitted once per execution segment (retries overwrite by recency).
    trace_id_hex = current_trace_id_hex()
    emit_event = getattr(handle, "emit", None)
    if trace_id_hex is not None and callable(emit_event):
        emit_event("inqtrix.run.trace", {"trace_id": trace_id_hex})

    providers = resolved.providers
    if authority_check is not None:
        authority_check()
        providers = guard_provider_context(providers, authority_check)

    run_context = RunContext(
        providers=providers,
        strategies=resolved.strategies,
        agent_settings=resolved.agent_settings,
        principal=principal,
        run_id=handle.run_id,
        workspace_id=workspace_id,
        cancel_token=handle.cancel_event,
        event_sink=handle.emit,
        # Opt-in hard per-run token cap (0 = off). Independent of the
        # monthly token quota: it bounds a single run, the quota blocks
        # the next one. The optional narrowing comes only from trusted
        # server callers; planner-authored task budgets are never input.
        token_budget=_effective_budget(
            runtime.settings.quota.max_tokens_per_run, token_budget
        ),
        park=handle.wait,
        authority_check=authority_check,
        # Children inherit the parent's provider stack through the
        # resolver — same inheritance rule as workspace_id.
        stack_name=getattr(resolved, "stack_name", "") or "",
    )
    agent_result = algorithm.run(
        run_request,
        runtime=runtime,
        context=run_context,
    )
    result = agent_result.raw
    result_state = result.get("result_state", {}) or {}
    # The LLM tokens (plus folded search-agent tokens) go to trace
    # metadata: Langfuse skips usage on ``invoke_agent`` spans, so the
    # standard usage keys would be dropped silently here. The per-call
    # generations still carry their own usage; the trace total in the UI
    # is their sum. Only the trace root writes it — a child run's total
    # is not the trace total.
    #
    # WHICH number it is, is the point of ``usage_scope``. ``usage`` is
    # this SEGMENT's delta, because the quota books it after every
    # segment and re-booking a cumulative total would double-count. A
    # run that parked — every deep run does, roles park their parent —
    # therefore has several of them, and the last one is not the run.
    # The engines that keep a cumulative count publish it as
    # ``usage_total``; where it exists the metadata names the run, and
    # where it does not the metadata says it names a segment instead of
    # calling a part a whole.
    run_usage, usage_scope = reported_usage(result, result_state)
    enrich_current_span(
        {
            **(
                {
                    semconv.trace_metadata_key("usage_prompt_tokens"): int(
                        run_usage.get("prompt_tokens", 0) or 0
                    ),
                    semconv.trace_metadata_key("usage_completion_tokens"): int(
                        run_usage.get("completion_tokens", 0) or 0
                    ),
                    semconv.trace_metadata_key("usage_scope"): usage_scope,
                }
                if trace_root
                else {}
            ),
            semconv.INQTRIX_OUTCOME: (
                semconv.OUTCOME_CANCELLED
                if (
                    agent_result.cancelled
                    or handle.cancel_event.is_set()
                )
                else (
                    semconv.OUTCOME_PARKED
                    if handle.parked
                    else semconv.OUTCOME_COMPLETED
                )
            ),
        }
    )
    # Book the LLM tokens this run actually consumed before any early
    # return: a budget-cancelled, client-cancelled, or PARKED run still
    # spent what it spent, and that spend must count toward the monthly
    # quota (it is what blocks the NEXT submission).
    if quota_service is not None:
        quota_account = (
            quota_subject
            if quota_subject is not None
            else quota_service.subject_for(principal)
        )
        quota_service.record_blocking(
            quota_account,
            QuotaDimension.LLM_TOKENS,
            consumed_tokens(result.get("usage")),
        )
    if handle.parked:
        # The algorithm parked the run (agent interrupt): the segment is
        # over, the store retained the closure/payload for the resume —
        # completing or cancelling here would destroy the wait.
        return
    if handle.cancel_event.is_set():
        handle.cancel("client_requested_cancel")
        return
    if agent_result.cancelled:
        if agent_result.cancel_reason == "token_budget_exceeded":
            handle.fail(
                "The run reached the server-side token budget.",
                error_type="token_budget_exceeded",
            )
            mark_current_span_error("token_budget_exceeded")
            enrich_current_span({semconv.INQTRIX_OUTCOME: semconv.OUTCOME_FAILED})
        else:
            handle.cancel(
                agent_result.cancel_reason or "client_requested_cancel"
            )
        return

    terminal_failure = result_state.get("_terminal_failure")
    if isinstance(terminal_failure, dict):
        raise RunExecutionFailure(
            str(terminal_failure.get("type") or "server_error"),
            str(
                terminal_failure.get("message")
                or "The algorithm reported a terminal execution failure."
            ),
        )

    research_result = ResearchResult.from_raw(
        result,
        source_tiering=getattr(resolved.strategies, "source_tiering", None),
    )
    segment_elapsed = round(time.monotonic() - t0, 2)
    total_elapsed_reader = getattr(handle, "total_elapsed_seconds", None)
    total_elapsed = (
        round(float(total_elapsed_reader()), 2)
        if callable(total_elapsed_reader)
        else segment_elapsed
    )
    # The durable store starts the timer before dispatch and never resets it
    # on resume. It therefore includes queued-visible execution and explicit
    # waiting segments. The monotonic segment value remains a lower bound for
    # custom non-store handles used by embedded callers and tests.
    research_result.metrics.elapsed_seconds = max(segment_elapsed, total_elapsed)
    answer = research_result.answer
    payload = research_result.to_export_payload()
    # The wire ``usage`` is the run total wherever the engine keeps one;
    # the segment delta stays the quota's own channel (booked exactly
    # once per segment above). Same resolution as the trace metadata, so
    # the two surfaces cannot report different numbers.
    usage, scope = reported_usage(result, result.get("result_state") or {})
    payload["usage"] = {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": (
            usage.get("prompt_tokens", 0)
            + usage.get("completion_tokens", 0)
        ),
        # The scope travels with the number on THIS surface too. The
        # trace carried it and the wire did not, although the wire is the
        # one a person exports into a Markdown file and reads later - the
        # surface where "is this the run or one segment" can no longer be
        # asked of anyone.
        "scope": scope,
    }
    if authority_check is not None:
        authority_check()
    mode = str(getattr(run_request, "mode", "") or "")
    if getattr(run_request, "work_profile", ""):
        # An internal role run: its outcome is the typed work result in the
        # payload. It has no answer to publish and none to stream — a role
        # that reached a user surface would be a second, unreviewed voice.
        _prune_uncollected_agent_metrics(payload)
    elif mode in {"agent_kernel", "workspace_agent"}:
        _prune_uncollected_agent_metrics(payload)
        if answer_publisher is None:
            raise RunExecutionFailure(
                "answer_publication_unavailable",
                "Persistent agent answer publication is unavailable.",
            )
        exported_references = payload.get("references", [])
        if not isinstance(exported_references, list):
            raise RunExecutionFailure(
                "answer_publication_invalid_references",
                "The exported answer references cannot be serialized.",
            )
        # The end of a run gets spans of its own: publishing the answer and
        # storing the result were seconds under the bare run span.
        with operation_span(
            "inqtrix.answer.publish",
            {semconv.INQTRIX_ANSWER_BYTES: len((answer or "").encode("utf-8"))},
        ):
            answer_publisher.publish(
                handle,
                answer,
                references=exported_references,
                question=run_request.question,
            )
    else:
        with operation_span(
            "inqtrix.answer.publish",
            {semconv.INQTRIX_ANSWER_BYTES: len((answer or "").encode("utf-8"))},
        ):
            handle.emit_answer(answer)
    if authority_check is not None:
        authority_check()
    current_node = str(algorithm.capabilities().get("terminal_node", "answer"))
    with operation_span("inqtrix.run.complete"):
        handle.complete(
            payload,
            snapshot=build_run_snapshot(
                result_state,
                current_node=current_node,
                last_message="completed",
            ),
        )
    from inqtrix.observability.trace_scores import record_run_scores

    if getattr(handle, "terminal_landed", True):
        record_run_scores(mode, result_state, trace_root=trace_root)


#: Research metrics an AGENT run genuinely produces. Everything else in
#: ``ResearchMetrics`` is computed by the research-desk graph, which an
#: agent run never executes.
_AGENT_REAL_METRIC_KEYS = frozenset(
    {"elapsed_seconds", "prompt_tokens", "completion_tokens"}
)


def _prune_uncollected_agent_metrics(payload: dict[str, Any]) -> None:
    """Drop research metrics an agent run never measured.

    Agent results ride the shared research serialization, so every
    research counter reached the wire as a hard zero: a mission with
    sixteen knowledge searches and seven citations reported
    ``total_queries: 0`` and ``total_citations: 0``. Zeros are a claim,
    not an absence — omitting the keys says "not measured here", which
    is the truth. What an agent run does measure (wall clock, tokens)
    stays.
    """
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return
    payload["metrics"] = {
        key: value
        for key, value in metrics.items()
        if key in _AGENT_REAL_METRIC_KEYS
    }


def _requirement_span_attributes(run_request: Any) -> dict[str, Any]:
    """The run span's record of its submit-time result requirement.

    Template ids and the revisions the run is frozen under, in attachment
    order, and the composed size — never a label or any text, which are
    user content. Empty when the run carries no requirement; read through
    ``getattr`` like every field here, because telemetry must not impose
    fields on request doubles.
    """
    from inqtrix.observability import semconv

    composed = str(getattr(run_request, "report_requirement", "") or "")
    if not composed:
        return {}
    rules = tuple(getattr(run_request, "report_rules", ()) or ())
    attributes: dict[str, Any] = {
        semconv.INQTRIX_REQUIREMENT_COMPOSED_CHARS: len(composed),
    }
    if rules:
        attributes[semconv.INQTRIX_REQUIREMENT_RULE_TEMPLATE_IDS] = [
            str(part["template_id"]) for part in rules
        ]
        attributes[semconv.INQTRIX_REQUIREMENT_RULE_REVISIONS] = [
            int(part["revision"]) for part in rules
        ]
    return attributes


def _effective_budget(run_cap: int, override: int | None) -> int:
    """The tighter of the deployment cap and a caller override."""
    if not override or override <= 0:
        return run_cap
    if not run_cap or run_cap <= 0:
        return override
    return min(run_cap, override)


class RunService:
    """Submit native runs that execute via the algorithm registry.

    Args:
        registry: The algorithm registry; the resolved request mode
            selects the algorithm executed by the run worker.
        runtime: App-level runtime context handed through to the
            algorithm.
        run_store: The in-memory run registry/queue that owns
            dispatch, events, and retention.
        dependency_authorizer: Safepoint checker for the segment actor
            and every dependency pinned into the request. Required for a
            scoped actor — execution fails closed without it. It owns the
            actor directory, so the run thread never reaches the HTTP
            loop's pooled identity engine.
        answer_artifact_store: Authoritative agent-control store used by the
            shared answer publisher. Native Agent Desk modes fail loudly when
            this dependency is absent.
    """

    def __init__(
        self,
        *,
        registry: "AlgorithmRegistry",
        runtime: RuntimeContext,
        run_store: "RunStore",
        quota_service: "QuotaService | None" = None,
        dependency_authorizer: "ExecutionDependencyAuthorizer | None" = None,
        answer_artifact_store: "AgentControlStore | None" = None,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._run_store = run_store
        self._quota_service = quota_service
        self._dependency_authorizer = dependency_authorizer
        self._answer_publisher = (
            AgentAnswerPublisher(answer_artifact_store)
            if answer_artifact_store is not None
            else None
        )

    @property
    def dependency_authorizer(self) -> "ExecutionDependencyAuthorizer | None":
        """Shared pinned-dependency checker for API and queue workers."""
        return self._dependency_authorizer

    @property
    def run_store(self) -> "RunStore":
        """The underlying run store (read-side surface for the router)."""
        return self._run_store

    @property
    def answer_publisher(self) -> AgentAnswerPublisher | None:
        """Shared native Agent Desk answer publisher, when configured."""
        return self._answer_publisher

    def submit(
        self,
        *,
        question: str,
        history: str,
        messages: list[dict[str, Any]],
        resolved: ResolvedAgentContext,
        workspace_id: str | None,
        principal: "Principal | None" = None,
        kind: str = "standard",
        parent_run_id: str | None = None,
        root_run_id: str | None = None,
        session_id: str | None = None,
        autonomy: str = "",
        document_id: str = "",
        response_form: str = "",
        token_budget: int | None = None,
        origin_key: str = "",
        skill_ids: list[str] | None = None,
        skill_revisions: dict[str, int] | None = None,
        tool_directives: list[str] | None = None,
        source_policy: dict[str, str] | SourcePolicy | None = None,
        web_recency: WebRecency | None = None,
        canvas_context: "CanvasContext | None" = None,
        report_requirement: str = "",
        report_rules: list[dict[str, Any]] | None = None,
        attached_reports: list[dict[str, Any]] | None = None,
        parent_task_id: str = "",
        parent_task_attempt: int = 0,
        work_profile: str = "",
        handover: WorkHandover | None = None,
        incognito: bool = False,
        ui_language: str = "",
    ) -> dict[str, Any]:
        """Queue one native run and return its public summary.

        Args:
            question: Normalized research question.
            history: Pre-formatted conversation history block.
            messages: Raw chat messages (carried on the run request).
            resolved: Stack/override/mode resolution for this request.
            workspace_id: Client-side UI namespace (NOT an
                authorization input).
            principal: Verified request identity, threaded into the
                run context for attribution.
            kind: Role in an agent tree (``standard`` — the default,
                emits no extra summary keys — or ``agent`` /
                ``agent_child``).
            parent_run_id: Direct parent for ``agent_child`` runs; the
                parent's cancel cascades over this link.
            root_run_id: Tree root (equals ``parent_run_id`` for depth-1
                trees); lets deep descendants resolve their root run
                without walking parents.
            session_id: Saved Agent or Knowledge session grouping the run.
            autonomy: Workspace-agent permission mode, carried on
                the run request and the worker replay payload; empty for
                non-agent modes.
            document_id: Target editor document of a workspace-agent
                patch assignment; empty when the run has no edit
                target. Carried like ``autonomy``.
            response_form: Workspace-agent output-form override
                (``chat``/``canvas``); empty delegates to the
                intake profile. Carried like ``autonomy``.
            token_budget: Optional trusted per-run LLM-token ceiling that
                narrows the deployment cap. Planner-authored task budgets
                are compatibility-only data and never flow into this field.
            origin_key: Idempotency key of the submitting kernel tool call or
                mission plan-task attempt. Persisted in the replay body and
                exposed on child summaries so checkpoint re-execution finds
                the already-submitted run. Empty for every other caller.
            skill_ids: Router-admitted skill ids; visibility
                and count cap already enforced; carried on the request
                and the worker replay payload.
            skill_revisions: Admission-time integer revision for each
                attached skill; runtime loading fails closed on drift.
            tool_directives: Whitelisted composer tool hints,
                carried like ``skill_ids``.
            source_policy: Availability of web and project-knowledge tools.
                ``None`` preserves the historical both-available surface.
                The normalized policy is persisted for worker replay and
                inherited by agent children.
            web_recency: Provider-neutral recency filter for delegated web
                research. ``None`` leaves recency inference to the research
                graph. Persisted for worker replay.
            report_requirement: Server-composed result requirement set
                before the run (free text plus attached library rules,
                each with its origin marker). Empty when none was set.
            report_rules: The parts ``report_requirement`` was composed
                from — ``{template_id, label, title, revision}`` per distinct
                attached rule, resolved server-side from the caller's
                catalog. Persisted in the replay body (only when present,
                so a run without rules replays byte-identically); the run
                is frozen under exactly these revisions and its transcript
                is bound to them. Server-set only: the HTTP route refuses
                a client value.
            attached_reports: Research reports the user attached,
                already resolved server-side to
                ``{report_id, title, reference_count}``. Only the names
                travel; the kernel tool fetches the bodies.
            canvas_context: Router-validated canvas attachment of an
                agent-kernel submission (open document + queued selection
                comments). Persisted in the replay body, injected into
                the kernel user message, frozen with the first segment's
                checkpoint. Never inherited by child runs and never part
                of summaries; ``None`` everywhere else.
            parent_task_id: Internal plan-task correlation for an agent
                child. Persisted only in the durable replay payload so the
                run store can project child progress onto the parent task.
            parent_task_attempt: Logical task attempt corresponding to the
                child. Independent of the run-row claim/fencing attempt.
            work_profile: Internal work profile of a role child. Exists on
                this server-side seam only — no HTTP route passes it.
                Persisted in the replay body; such a run stores a typed
                work result and publishes no answer.
            handover: Typed assignment of that role child; persisted with
                the profile and rendered into the child's first model input.
            incognito: An incognito knowledge ask. Marked in the replay
                body, which every execution boundary reads: its spans
                carry no content, and the run store deletes it after the
                short incognito retention.
            ui_language: Interface language of a knowledge ask
                (``de``/``en``), the answer language where the question's
                own language is unclear. Persisted in the replay body only
                when set; empty for every other run.

        Raises:
            inqtrix.server.runs.RunQueueFull: When the waiting queue
                has no free slot (the router maps this to HTTP 429).
        """
        algorithm = self._registry.get(resolved.mode)
        normalized_source_policy = (
            source_policy
            if isinstance(source_policy, SourcePolicy)
            else SourcePolicy.model_validate(source_policy or {})
        )
        effective_agent_overrides = {
            **resolved.agent_overrides,
            **(
                {
                    "depth": resolved.agent_settings.depth,
                    # The selected Stufe travels with the run (worker
                    # replay, summaries, edit-path policy) exactly like
                    # depth. Only echoed when selected: a no-tier run
                    # replays byte-identically to today, and a tier run
                    # replays the CONSISTENT (tier, bridged depth) pair,
                    # which the override validator accepts.
                    **(
                        {"agent_tier": tier_value}
                        if (
                            tier_value := getattr(
                                resolved.agent_settings, "agent_tier", ""
                            )
                        )
                        else {}
                    ),
                }
                if kind in {"agent", "agent_child"}
                else {}
            ),
        }
        run_request = RunRequest(
            mode=resolved.mode,
            question=question,
            history=history,
            messages=messages,
            agent_overrides=effective_agent_overrides,
            knowledge_filters=resolved.knowledge_filters,
            autonomy=autonomy,
            session_id=session_id or "",
            document_id=document_id,
            response_form=response_form,
            skill_ids=tuple(skill_ids or ()),
            skill_revisions=dict(skill_revisions or {}),
            tool_directives=tuple(tool_directives or ()),
            source_policy=normalized_source_policy,
            web_recency=web_recency,
            canvas_context=canvas_context,
            report_requirement=report_requirement,
            report_rules=tuple(dict(part) for part in (report_rules or ())),
            attached_reports=tuple(attached_reports or ()),
            work_profile=work_profile,
            handover=handover,
            ui_language=ui_language,
        )

        def _work(handle: "RunHandle") -> None:
            effective_principal = handle.effective_principal(principal)
            has_scoped_actor = (
                effective_principal is not None
                and effective_principal.user_id is not None
            )

            def _check_authority() -> None:
                self._run_store.check_execution_authority(handle.run_id)
                if self._dependency_authorizer is None:
                    # Only reachable for a scoped actor (see the gate
                    # below): silently skipping the actor and pinned
                    # dependency probes would be a fallback, not a check.
                    raise AuthorizationRevoked(
                        "run execution has no dependency authorizer"
                    )
                self._dependency_authorizer.check(
                    run_request,
                    effective_principal,
                )

            execute_run_request(
                handle,
                algorithm=algorithm,
                run_request=run_request,
                resolved=resolved,
                runtime=self._runtime,
                principal=effective_principal,
                quota_service=self._quota_service,
                token_budget=token_budget,
                workspace_id=workspace_id,
                authority_check=(
                    _check_authority if has_scoped_actor else None
                ),
                answer_publisher=self._answer_publisher,
                trace_root=not (parent_run_id or parent_task_id),
            )

        # Everything a worker process needs to re-resolve and execute
        # this run from the persisted row alone; the in-memory backend
        # ignores it (the work closure stays in-process).
        request_payload: dict[str, Any] = {
            "question": question,
            "history": history,
            "messages": messages,
            "body": {
                "mode": resolved.mode,
                "agent_overrides": effective_agent_overrides,
                "knowledge_filters": resolved.knowledge_filters,
                **({"autonomy": autonomy} if autonomy else {}),
                **({"session_id": session_id} if session_id else {}),
                **({"incognito": True} if incognito else {}),
                **({"ui_language": ui_language} if ui_language else {}),
                **({"document_id": document_id} if document_id else {}),
                **(
                    {"response_form": response_form}
                    if response_form
                    else {}
                ),
                **(
                    {"token_budget": token_budget}
                    if token_budget
                    else {}
                ),
                **({"origin_key": origin_key} if origin_key else {}),
                **(
                    {"parent_task_id": parent_task_id}
                    if parent_task_id
                    else {}
                ),
                **(
                    {"parent_task_attempt": int(parent_task_attempt)}
                    if parent_task_attempt > 0
                    else {}
                ),
                **({"skill_ids": list(skill_ids)} if skill_ids else {}),
                **(
                    {"skill_revisions": dict(skill_revisions)}
                    if skill_revisions
                    else {}
                ),
                **(
                    {"tool_directives": list(tool_directives)}
                    if tool_directives
                    else {}
                ),
                **(
                    {
                        "source_policy": normalized_source_policy.model_dump(
                            mode="json"
                        )
                    }
                    if source_policy is not None
                    else {}
                ),
                **(
                    {"web_recency": web_recency}
                    if web_recency is not None
                    else {}
                ),
                **(
                    {
                        "canvas_context": canvas_context.model_dump(
                            mode="json"
                        )
                    }
                    if canvas_context is not None
                    else {}
                ),
                **(
                    {"report_requirement": report_requirement}
                    if report_requirement
                    else {}
                ),
                **(
                    {"report_rules": [dict(part) for part in report_rules]}
                    if report_rules
                    else {}
                ),
                **(
                    {
                        "attached_reports": [
                            dict(report) for report in attached_reports
                        ]
                    }
                    if attached_reports
                    else {}
                ),
                **(
                    {"stack": resolved.stack_name}
                    if resolved.stack_name
                    else {}
                ),
                **(
                    {
                        "work_profile": work_profile,
                        "handover": run_request.handover.model_dump(mode="json")
                        if run_request.handover is not None
                        else None,
                    }
                    if work_profile
                    else {}
                ),
            },
        }
        return self._run_store.submit(
            question=question,
            stack_name=resolved.stack_name or "default",
            work=_work,
            # One effective override contract feeds in-process execution,
            # worker replay, and summaries. The summary additionally carries
            # UI/session metadata that already travels as dedicated replay
            # fields.
            agent_overrides={
                **effective_agent_overrides,
                **({"autonomy": autonomy} if autonomy else {}),
                **(
                    {"response_form": response_form}
                    if response_form
                    else {}
                ),
                **(
                    {
                        "source_policy": normalized_source_policy.model_dump(
                            mode="json"
                        )
                    }
                    if source_policy is not None
                    else {}
                ),
            },
            mode=resolved.mode,
            workspace_id=workspace_id,
            created_by_user_id=principal.user_id if principal is not None else None,
            created_by_tenant_id=(
                principal.tenant_id if principal is not None else None
            ),
            execution_scopes=(
                principal.scopes if principal is not None else frozenset()
            ),
            request_payload=request_payload,
            kind=kind,
            parent_run_id=parent_run_id,
            root_run_id=root_run_id,
            session_id=session_id,
            origin_key=origin_key or None,
        )
