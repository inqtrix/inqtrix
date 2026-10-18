"""Pure helpers shared by every run-store backend.

The byte-level wire contract (run summary keys, SSE event envelope,
the auto-emitted ``inqtrix.run.snapshot`` companion BEFORE its carrier
event) is pinned by ``tests/contract/``. Both the in-memory store and
the Postgres store build summaries and expand events through these
functions, so the contract has exactly one implementation.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Protocol

from inqtrix.auth.permissions import SharePermission
from inqtrix.runtime_logging import sanitize_event_payload
from inqtrix.text import LOG_CUT_MARKER, shorten_visibly

SNAPSHOT_EVENT = "inqtrix.run.snapshot"
CHILD_PROGRESS_EVENT = "inqtrix.agent.child.progress"
OUTCOME_EVENT = "inqtrix.agent.outcome"
"""An agent run's outcome record: how its review ended and what stayed open;
only a delegated mission's crosses to its parent, as numbers and its bounded
summary sentence."""
ANSWER_DRAFT_EVENT = "inqtrix.answer.draft"
"""The answer a reviewed run shows while its review runs: the draft after its
citation check, whole, once before the review starts. The published answer
replaces it when it is ready; the draft keeps its own text."""

# String form of the terminal lifecycle statuses. Single source of truth:
# ``inqtrix.server.runs.TERMINAL_RUN_STATUSES`` derives its enum set from
# this value set (this module must stay import-free of the server layer).
TERMINAL_RUN_STATUS_VALUES = frozenset({"completed", "failed", "cancelled"})

RETENTION_SWEEP_SECONDS = 60.0
"""How often the API process runs a store's cleanup pass (``sweep``) on its
own, so a retention window holds within about this long while nobody reads
the store."""

_CHILD_SNAPSHOT_KEYS = frozenset(
    {
        "current_node",
        "completed_rounds",
        "active_round",
        "max_rounds",
        "total_queries",
        "executed_queries",
        "total_citations",
        "total_sources",
        "confidence",
        "source_tier_counts",
        "claim_status_counts",
        "evidence_record_count",
        "consolidated_claim_count",
        "done",
        "progress_estimate",
        "last_message",
        "phase",
        "writing_section",
        "sections_total",
    }
)
_CHILD_METRIC_KEYS = frozenset(
    {
        "rounds",
        "queries",
        "sources",
        "citations",
        "claims",
        "result_count",
        "reference_count",
        "claim_count",
    }
)
CHILD_TOOL_PROGRESS_EVERY = 5
"""A kernel role child's tool steps cross to its parent at its first call
and every fifth one after, counted by each call's position in the role's
sequence of calls: a review reads up to its ceiling of calls, and each
crossing locks and appends to the parent's record. The line moves in
steps, never per call."""

_CHILD_TOOL_FINISHED = "inqtrix.agent.tool.finished"
"""A kernel tool call that settled — the one tool event that carries the
server's call counter (its snapshot) and the call's own position."""


def _call_ordinal(payload: Mapping[str, Any]) -> int | None:
    """The settled call's position in the child's sequence of calls, or
    ``None`` when the payload carries none. A batch of parallel calls
    shares one counter; the position tells them apart."""
    ordinal = payload.get("call_ordinal")
    return (
        ordinal
        if isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal >= 1
        else None
    )


def _tool_calls_used(payload: Mapping[str, Any]) -> int | None:
    """The server's counter of the child's tool calls after this one, or
    ``None`` when the payload carries none."""
    snapshot = payload.get("snapshot")
    execution = snapshot.get("execution") if isinstance(snapshot, Mapping) else None
    limits = execution.get("limits") if isinstance(execution, Mapping) else None
    calls = limits.get("tool_calls") if isinstance(limits, Mapping) else None
    used = calls.get("used") if isinstance(calls, Mapping) else None
    return used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else None


_CHILD_PROJECTED_EVENTS = frozenset(
    {
        "inqtrix.run.queued",
        "inqtrix.run.started",
        "inqtrix.run.resumed",
        "inqtrix.run.snapshot",
        "inqtrix.run.cancel_requested",
        "inqtrix.run.waiting",
        "inqtrix.run.completed",
        "inqtrix.run.failed",
        "inqtrix.run.cancelled",
        "inqtrix.node.started",
        "inqtrix.node.finished",
        "inqtrix.node.failed",
        "inqtrix.progress.message",
        "inqtrix.knowledge.retrieval.degraded",
        "inqtrix.knowledge.retrieval.warning",
        "inqtrix.knowledge.answer.retry",
        "inqtrix.knowledge.grounding.checked",
        # A delegated MISSION reports its progress under `agent.*`, not in
        # the research vocabulary above. Without these three a kernel
        # parent saw ONLY lifecycle transitions — eleven events while a
        # whole mission with five research children ran beneath it, and
        # nothing at all about what was being worked on.
        "inqtrix.agent.phase.changed",
        "inqtrix.agent.task.started",
        "inqtrix.agent.task.finished",
        # A failed task settles too. Without it the parent's line kept
        # the failed tasks "open" and read "2 Aufgaben parallel" while
        # the mission was already planning its next round.
        "inqtrix.agent.task.failed",
        # How the mission ended, as numbers and its bounded summary: its
        # reading lines and codes stay in the child's own record.
        OUTCOME_EVENT,
    }
)

#: Child activity operations whose bounded facts cross to the parent: the
#: searches a mission task runs. Provider retries and model-turn brackets
#: carry ``scope="run"`` and stay in the child's own stream.
_CHILD_ACTIVITY_OPERATIONS = frozenset({"web.search.instant", "knowledge.search"})

#: Child narration kinds that cross. Every line is DERIVED by
#: ``narration.py`` from artifacts the mission already produced:
#: ``synthesis`` (outline and section titles), ``task`` (the plan task's
#: title plus the head of its result, <= 240 chars), ``plan`` (the plan's
#: own summary head) and ``discovery`` (fact and gap counts). They are the
#: mission's intermediate results in one line each — what the parent's
#: card shows under the task they belong to. A task's whole result
#: summary never crosses; the narration cap (400) bounds each line.
_CHILD_NARRATION_KINDS = frozenset({"synthesis", "task", "plan", "discovery"})

#: Run-scoped activity kinds that cross: the mission's DECISIONS after a
#: check — why it keeps researching or why it stops. Bounded label and
#: detail only; no evidence text. How the review ended crosses with the
#: outcome record, never as a decision.
_CHILD_DECISION_KINDS = frozenset(
    {
        "critic_unavailable",
        "critic_revise",
        "critic_research",
        "critic_research_exhausted",
        "replan_invalid",
    }
)

#: Visible bound for a projected decision detail (the label is short by
#: construction; the detail names the criteria a check left open).
_CHILD_DECISION_DETAIL_CAP = 240

#: Mission task events whose bounded identifiers cross to the parent.
_CHILD_TASK_EVENTS = frozenset(
    {
        "inqtrix.agent.task.started",
        "inqtrix.agent.task.finished",
        "inqtrix.agent.task.failed",
    }
)


class RunRecordView(Protocol):
    """Attribute surface a record must offer to be summarized.

    Satisfied by the in-memory ``RunRecord`` dataclass and the row
    view the Postgres store builds from a ``runs`` row — ``status``
    may be the ``RunStatus`` enum or its string value.
    """

    run_id: str
    status: Any
    question: str
    stack_name: str
    workspace_id: str | None
    mode: str
    agent_overrides: dict[str, Any]
    created_at: float
    started_at: float | None
    finished_at: float | None
    snapshot: dict[str, Any]
    error: dict[str, Any] | None
    # ``cancel_requested`` is an OPTIONAL additive attribute read via
    # ``getattr`` with a False default, so record views that predate the
    # field keep summarizing unchanged.


def status_value(status: Any) -> str:
    """String form of a status that may be an enum or already a string."""
    return getattr(status, "value", status)


def run_segment_id(run_id: str, ordinal: int) -> str:
    """Stable public identity for one execution segment of a run."""
    suffix = run_id.removeprefix("run_")[-20:]
    return f"seg_{suffix}_{ordinal}"


def answer_artifact_id(run_id: str) -> str:
    """Stable run-local artifact identity for one native answer."""
    return f"art_{run_id[-12:]}_answer"


def answer_publication_id(run_id: str) -> str:
    """Stable event-publication identity for one native answer."""
    return f"pub_{run_id[-12:]}_answer"


def answer_draft_id(run_id: str) -> str:
    """Stable event identity for the one draft of a native answer."""
    return f"drf_{run_id[-12:]}_answer"


def access_annotation(
    shared: Any,
    *,
    owner_user_id: Any,
) -> dict[str, Any]:
    """Canonical public access object for one visible run."""
    if shared is not None:
        return {"mode": "shared", "permission": shared.value}
    if owner_user_id is None:
        return {"mode": "unscoped"}
    return {"mode": "owner"}


def access_permits_edit(access: Mapping[str, Any] | None) -> bool:
    """Whether a run summary's ``access`` annotation permits mutation.

    The consumer twin of :func:`access_annotation` — produce and consume of
    the ``access`` wire shape live in one file so they cannot drift. Missing or
    unknown annotations fail closed. A shared annotation permits mutation only
    with the sole mutable direct-share permission, ``edit``.
    """
    if access is None:
        return False
    if access.get("mode") in {"owner", "unscoped"}:
        return True
    if access.get("mode") != "shared":
        return False
    try:
        return SharePermission(access.get("permission")).at_least(SharePermission.EDIT)
    except ValueError:
        return False


QUESTION_COLUMN_LIMIT = 500


def incognito_request(request_payload: Mapping[str, Any] | None) -> bool:
    """Whether a persisted run request is an incognito knowledge ask.

    The run service marks it in the replay body. Both run stores index the
    mark for their retention sweep, and the execution span withholds the
    run's content by it, in whichever process executes the run.
    """
    body = (request_payload or {}).get("body")
    return isinstance(body, Mapping) and body.get("incognito") is True


def clipped_question(question: str) -> str:
    """Bounded summary/row question with a VISIBLE cut (no silent caps).

    The full text always survives in ``request_payload`` for execution
    and replay; this bound only keeps list/summary rows small. A cut
    that hides itself lets a delegated assignment end mid-word on the
    gate card — the marker tells every consumer that more exists.
    """
    if len(question) <= QUESTION_COLUMN_LIMIT:
        return question
    return question[:QUESTION_COLUMN_LIMIT] + "…"


def question_was_clipped(record: Any) -> bool:
    """Whether this row's stored question is shorter than what was asked.

    Decided by COMPARING the column against the durable payload, never by
    sniffing a trailing ellipsis: a user may legitimately end a question
    with one, and guessing would put a dead "show more" on their text.
    """
    full = getattr(record, "question_full", None)
    return bool(full) and len(full) > len(record.question)


def build_run_summary(
    record: RunRecordView,
    *,
    queue_position: int | None,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Public run summary — the 16-key wire shape of ``/v1/runs``.

    *access* is the required canonical ``unscoped|owner|shared`` annotation.

    Agent-tree keys (``kind``, ``parent_run_id``, ``root_run_id``,
    ``session_id``) follow the same rule: emitted ONLY when non-default,
    so a standard run's summary stays byte-identical to the historical
    shape. Read via ``getattr`` with defaults so record views that
    predate the fields keep summarizing.

    ``cancel_requested`` follows the same additive rule: emitted only as
    ``True`` and only while the run is still non-terminal, letting
    clients render a "cancelling" state (and gate delete flows) without
    a new status enum value.

    ``last_event_sequence`` is the run's event allocator at the moment
    this summary was read, and it exists because a summary and the run's
    event stream are two views of the same run that a client consumes at
    once. A summary is a snapshot of the row; the stream keeps moving.
    Without a position, a client cannot tell a summary that is NEWER than
    what it has streamed from one that is already behind -- and the
    decision endpoints return a summary read inside the transaction that
    re-queues the run, so theirs is behind by construction: the worker
    picks the run up milliseconds later. A client that writes such a
    status over its streamed state shows "queued" for a run that is
    working. It is emitted for the runs that can park and resume, the
    same rule that governs ``timing``, so standard run summaries keep
    their exact historical key set.

    ``question_clipped`` is additive in the same way, and deliberately
    carries only the FLAG: the whole point of ``clipped_question`` is to
    keep list rows small, so the full text stays behind the single-run
    read (``GET /v1/runs/{id}`` adds ``question_full``). The flag is what
    lets a client offer "show more" without guessing from an ellipsis.
    """
    now = time.time()
    elapsed = run_elapsed_seconds(record, now=now)
    cancel_requested = bool(getattr(record, "cancel_requested", False)) and (
        status_value(record.status) not in TERMINAL_RUN_STATUS_VALUES
    )
    agent_tree: dict[str, Any] = {}
    kind = getattr(record, "kind", "standard") or "standard"
    if kind != "standard":
        agent_tree["kind"] = kind
        agent_tree["children_url"] = f"/v1/runs/{record.run_id}/children"
        agent_tree["plan_url"] = f"/v1/runs/{record.run_id}/plan"
        agent_tree["artifacts_url"] = f"/v1/runs/{record.run_id}/artifacts"
    for key in ("parent_run_id", "root_run_id", "session_id", "origin_key", "work_profile"):
        value = getattr(record, key, None)
        if value:
            agent_tree[key] = value
    if question_was_clipped(record):
        agent_tree["question_clipped"] = True
    segment_count = max(0, int(getattr(record, "segment_count", 0) or 0))
    status = status_value(record.status)
    active_seconds = max(0.0, float(getattr(record, "active_seconds", 0.0) or 0.0))
    active_started_at = getattr(record, "active_started_at", None)
    if status == "running" and active_started_at is not None:
        active_seconds += max(0.0, now - float(active_started_at))
    waiting_seconds = max(0.0, float(getattr(record, "waiting_seconds", 0.0) or 0.0))
    waiting_since = getattr(record, "waiting_since", None)
    if (
        status in {"waiting_for_approval", "waiting_for_input", "waiting_for_children"}
        and waiting_since is not None
    ):
        waiting_seconds += max(0.0, now - float(waiting_since))
    queued_seconds = max(0.0, float(getattr(record, "queued_seconds", 0.0) or 0.0))
    queued_since = getattr(record, "queued_since", None)
    if status == "queued" and queued_since is not None:
        queued_seconds += max(0.0, now - float(queued_since))
    # Agent runs can park and resume repeatedly. Expose their timing split so
    # the UI and audit trail can distinguish active work from explicit waits;
    # standard run summaries keep their historical exact key set.
    runtime_timing = (
        {
            "total_seconds": elapsed,
            "active_seconds": round(active_seconds, 2),
            "waiting_seconds": round(waiting_seconds, 2),
            "queued_seconds": round(queued_seconds, 2),
            "segment_count": segment_count,
            "resume_count": max(0, segment_count - 1),
            "current_segment_id": getattr(record, "current_segment_id", None),
        }
        if kind != "standard"
        else None
    )
    return {
        "run_id": record.run_id,
        "status": status_value(record.status),
        "queue_position": queue_position,
        "question": record.question,
        "stack": record.stack_name,
        "workspace_id": record.workspace_id,
        "mode": record.mode,
        "agent_overrides": dict(record.agent_overrides),
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "elapsed_seconds": elapsed,
        "snapshot": dict(record.snapshot),
        "error": dict(record.error) if record.error else None,
        "events_url": f"/v1/runs/{record.run_id}/events",
        "result_url": f"/v1/runs/{record.run_id}/result",
        **agent_tree,
        **({"timing": runtime_timing} if runtime_timing is not None else {}),
        **(
            {"last_event_sequence": int(getattr(record, "event_seq", 0) or 0)}
            if kind != "standard"
            else {}
        ),
        **({"cancel_requested": True} if cancel_requested else {}),
        **({"access": access} if access is not None else {}),
    }


def run_elapsed_seconds(
    record: RunRecordView,
    *,
    now: float | None = None,
) -> float | None:
    """Project durable wall time without applying public visibility.

    Run workers need this value while the owned row is still executing. The
    calculation is shared with the public summary, but callers must obtain the
    record through their store's internal lifecycle read rather than routing a
    worker through the user-facing authorization surface.
    """

    if record.started_at is None:
        return None
    current = time.time() if now is None else float(now)
    end = record.finished_at if record.finished_at is not None else current
    return round(max(0.0, float(end) - float(record.started_at)), 2)


def replay_after(
    replay: list[dict[str, Any]], after: int | None
) -> list[dict[str, Any]]:
    """Filter a subscription's replay to events with ``sequence > after``.

    The ONE implementation of the ``?after=<seq>`` reconnect semantics,
    shared by the SSE route over both store backends: the filter
    applies to the REPLAY set only, the live tail is untouched. ``None``
    replays everything (historical behaviour); an ``after`` at or past
    the newest sequence yields an empty replay. Events without a
    ``sequence`` (never produced by the stores) are kept — dropping
    unmarked events silently would hide data.
    """
    if after is None:
        return replay
    return [
        event
        for event in replay
        if not isinstance(event.get("sequence"), int) or event["sequence"] > after
    ]


def build_child_progress_payload(
    *,
    child_run_id: str,
    parent_task_id: str,
    run_status: str,
    event_type: str,
    payload: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Project one child event into a bounded parent-task progress signal.

    The child stream remains the detailed technical record. This projection
    deliberately carries only stable UI facts, preventing provider payloads
    or a child's full state from being copied into the parent event log.
    """
    clean = sanitize_event_payload(event_type, dict(payload))
    carried_snapshot = clean.get("snapshot")
    source_snapshot = (
        carried_snapshot if isinstance(carried_snapshot, dict) else dict(snapshot or {})
    )
    compact_snapshot = {
        key: value
        for key, value in source_snapshot.items()
        if key in _CHILD_SNAPSHOT_KEYS
    }
    raw_metrics = clean.get("metrics")
    metrics = (
        {key: value for key, value in raw_metrics.items() if key in _CHILD_METRIC_KEYS}
        if isinstance(raw_metrics, dict)
        else {}
    )
    message = next(
        (
            str(value).strip()
            for value in (
                clean.get("message"),
                clean.get("label"),
                clean.get("last_message"),
                compact_snapshot.get("last_message"),
            )
            if value and str(value).strip()
        ),
        "",
    )
    error = clean.get("error")
    projected: dict[str, Any] = {
        "task_id": parent_task_id,
        "child_run_id": child_run_id,
        "run_status": run_status,
        "event_type": event_type,
        "updated_at": time.time(),
    }
    if compact_snapshot:
        projected["snapshot"] = compact_snapshot
    current_node = compact_snapshot.get("current_node")
    if current_node:
        projected["current_node"] = current_node
    if message:
        projected["message"] = message
    if metrics:
        projected["metrics"] = metrics
    if event_type in _CHILD_TASK_EVENTS:
        # Which unit of work the mission is on — nothing more. The
        # payload also carries `result_summary`, the child's whole
        # executive summary with source URLs: that belongs to the child's
        # own record, never to the parent's live feed. Its `child_run_id`
        # names a GRANDCHILD and must not clobber the projected one, so
        # both stay off this list by construction.
        # `title` is the plan task's own title (capped by the sanitizer's
        # text cap), so the parent can name the unit of work. The child's
        # own plan-task id crosses under its own name (as a search's does)
        # so the parent can join a task's searches and its result line to
        # the row; `task_id` stays the PARENT's tool-call id.
        for key in ("ordinal", "tool_kind", "status", "title"):
            if clean.get(key) is not None:
                projected[key] = clean[key]
        if clean.get("task_id"):
            projected["child_task_id"] = str(clean["task_id"])
    if event_type == _CHILD_TOOL_FINISHED:
        # A kernel role child's step: the tool's name and the server's
        # counter, nothing else — its arguments and its result name the
        # material the role reads, and stay with the child.
        projected["tool"] = str(clean.get("tool") or "")
        projected["tool_calls"] = _tool_calls_used(clean)
    if event_type == "inqtrix.agent.activity":
        # The search a mission task runs right now: operation, bracket
        # status, the query (text-capped under its own key), and the
        # counter. `purpose`/`detail` duplicate the query or the task
        # objective and stay behind; the child's task id crosses under
        # its own name so it can never clobber the PARENT task id.
        for key in ("operation", "status", "query", "current", "total"):
            if clean.get(key) is not None:
                projected[key] = clean[key]
        if clean.get("task_id"):
            projected["child_task_id"] = str(clean["task_id"])
        if clean.get("kind") in _CHILD_DECISION_KINDS:
            # A decision line: its kind, label and a bounded detail.
            projected["kind"] = str(clean["kind"])
            for key in ("label", "detail"):
                value = clean.get(key)
                if value:
                    projected[key] = shorten_visibly(
                        value, _CHILD_DECISION_DETAIL_CAP, marker=LOG_CUT_MARKER
                    )
    if event_type == OUTCOME_EVENT:
        # The parent learns how the mission ended and how much stayed open,
        # counted per code and per kind, and the record's summary: a closed
        # sentence built from the record's states and counts (the reading
        # limit and the review's reach among them), cut visibly at the
        # decision bound. Never a line, a code list or evidence text.
        for key in ("final", "review", "round", "revision", "accounting"):
            if clean.get(key) is not None:
                projected[key] = clean[key]
        if clean.get("applied") is not None:
            projected["applied"] = int(clean["applied"])
        if clean.get("summary"):
            projected["summary"] = shorten_visibly(
                clean["summary"], _CHILD_DECISION_DETAIL_CAP, marker=LOG_CUT_MARKER
            )
        raw_open = clean.get("open")
        if isinstance(raw_open, dict):
            projected["open"] = {
                group: {
                    str(name): int(count)
                    for name, count in dict(raw_open.get(group) or {}).items()
                }
                for group in ("by_code", "by_kind")
            }
    if event_type == "inqtrix.agent.narration":
        # Already excerpt-capped by the narration schema on entry.
        for key in ("narration_id", "kind", "text"):
            if clean.get(key) is not None:
                projected[key] = clean[key]
    if event_type == "inqtrix.knowledge.grounding.checked":
        # A delegated mission's child stream owns the full event.  The parent
        # receives only the bounded verdict/count projection needed to explain
        # why a Knowledge task did not complete; quote/source text never
        # crosses the run boundary.
        for key in ("status", "failure_code", "marker", "format_repaired"):
            if clean.get(key) is not None:
                projected[key] = clean[key]
        grounding_metrics = {
            key: clean[key]
            for key in ("quotes_total", "quotes_verified")
            if clean.get(key) is not None
        }
        if grounding_metrics:
            projected["metrics"] = {
                **dict(projected.get("metrics") or {}),
                **grounding_metrics,
            }
    if event_type == "inqtrix.knowledge.answer.retry":
        # The visible regeneration must stay visible ACROSS the run
        # boundary: the parent receives the counters (never quote text),
        # and the regeneration attempt travels under its own key so the
        # task-attempt field below cannot clobber it.
        retry_metrics = {
            key: clean[key]
            for key in ("quotes_total", "quotes_unverified")
            if clean.get(key) is not None
        }
        if clean.get("attempt") is not None:
            retry_metrics["answer_attempt"] = clean["attempt"]
        if retry_metrics:
            projected["metrics"] = {
                **dict(projected.get("metrics") or {}),
                **retry_metrics,
            }
    if event_type == "inqtrix.knowledge.retrieval.degraded":
        # Only the reason and bounded counters cross into the parent.  The
        # child retains query/source detail in its own protected run record.
        for key in (
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
        ):
            if clean.get(key) is not None:
                projected[key] = clean[key]
    if event_type == "inqtrix.knowledge.retrieval.warning":
        # Source-integrity exclusions cross run boundaries only as bounded
        # aggregate metadata. Queries, source ids and evidence text remain in
        # the authorized child run.
        for key in (
            "code",
            "reason",
            "stage",
            "count",
            "recommended_action",
        ):
            if clean.get(key) is not None:
                projected[key] = clean[key]
    if attempt is not None:
        projected["attempt"] = max(1, int(attempt))
    if isinstance(error, dict):
        projected["error"] = {
            key: error[key] for key in ("type", "message") if error.get(key)
        }
    elif error:
        projected["error"] = {"message": str(error)}
    return sanitize_event_payload(CHILD_PROGRESS_EVENT, projected)


def should_project_child_event(
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    work_profile: str | None = None,
) -> bool:
    """Whether a child event belongs in the parent's bounded live feed.

    A kernel ROLE child (*work_profile* set: a review, a section writer)
    crosses its settled tool calls by name and counter, at the first call
    and every :data:`CHILD_TOOL_PROGRESS_EVERY`-th by the call's position
    (``call_ordinal``), so a batch of parallel calls that settles with one
    shared counter still crosses; a step without its position stays (fail
    closed). Its typed result call (``result_call``) is no step. A mission
    child's own tool rows never cross. Two more families are payload-gated,
    and fail CLOSED without a payload:
    ``agent.activity`` crosses only for a task's searches (``scope ==
    "task"`` and a search operation) — a provider retry or a model-turn
    bracket carries ``scope="run"`` and stays; ``agent.narration``
    crosses only for the derived synthesis lines, never for model prose.
    """
    if event_type == _CHILD_TOOL_FINISHED:
        if not work_profile or payload is None or payload.get("result_call"):
            return False
        ordinal = _call_ordinal(payload)
        return ordinal is not None and (
            ordinal == 1 or ordinal % CHILD_TOOL_PROGRESS_EVERY == 0
        )
    if event_type == "inqtrix.agent.activity":
        if payload is None:
            return False
        if payload.get("kind") in _CHILD_DECISION_KINDS:
            return True
        return (
            payload.get("scope") == "task"
            and payload.get("operation") in _CHILD_ACTIVITY_OPERATIONS
        )
    if event_type == "inqtrix.agent.narration":
        return payload is not None and payload.get("kind") in _CHILD_NARRATION_KINDS
    return event_type in _CHILD_PROJECTED_EVENTS


def expand_run_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    status: str,
) -> tuple[dict[str, Any] | None, list[tuple[str, dict[str, Any]]]]:
    """Sanitize one event and expand its snapshot companion.

    Any payload whose ``snapshot`` value is a dict updates the run's
    stored snapshot AND auto-emits an ``inqtrix.run.snapshot``
    companion event BEFORE the carrier event (unless the carrier IS
    the snapshot event) — the order the SSE contract pins.

    Args:
        event_type: Carrier event type.
        payload: Raw event payload (sanitized here, never trusted).
        status: Current run status string for the companion payload.

    Returns:
        ``(new_snapshot, events)`` where ``new_snapshot`` is the dict
        to store on the run (``None`` when the payload carried no
        snapshot) and ``events`` is the ordered list of
        ``(type, clean_payload)`` pairs to append.
    """
    clean_payload = sanitize_event_payload(event_type, dict(payload))
    events: list[tuple[str, dict[str, Any]]] = []
    new_snapshot: dict[str, Any] | None = None
    snapshot = clean_payload.get("snapshot")
    if isinstance(snapshot, dict) and event_type != CHILD_PROGRESS_EVENT:
        new_snapshot = dict(snapshot)
        if event_type != SNAPSHOT_EVENT:
            events.append(
                (
                    SNAPSHOT_EVENT,
                    sanitize_event_payload(
                        SNAPSHOT_EVENT,
                        {"status": status, "snapshot": new_snapshot},
                    ),
                )
            )
    events.append((event_type, clean_payload))
    return new_snapshot, events
