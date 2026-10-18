"""The server-built session context pack.

Rows are truth: every follow-up composes durable run/control metadata —
device-independent and reload-safe. Explicit ``request.history`` replaces
only the rendered conversation block; artifact registry, effective output
form, and prior evidence count still come from server rows.

Trim policy is DETERMINISTIC and loud: the newest
:data:`RECENT_TURNS_VERBATIM` turns keep their full Q/A (answer bodies
capped at :data:`TURN_BODY_CHAR_CAP` chars with a visible cut marker),
older turns collapse to one line each, and when the total budget forces
dropping older turns entirely the block STARTS with a visible trim
marker — a shortened context never masquerades as the whole history
(No Silent Fallbacks).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from inqtrix.agents.artifact_names import (
    NAMED_ARTIFACT_KINDS,
    assign_artifact_file_names,
)
from inqtrix.agents.clarification import round_qa_lines
from inqtrix.pagination import decode_cursor

if TYPE_CHECKING:
    from inqtrix.agents.control_ports import AgentControlStore

log = logging.getLogger("inqtrix")

RECENT_TURNS_VERBATIM = 3
"""Newest turns rendered with full question + answer body."""

TURN_BODY_CHAR_CAP = 2000
"""Per-turn answer-body cap (recent turns); cut is visibly marked."""

TURN_BODY_FLOOR_CHARS = 400
"""How far an answer body may be squeezed before a whole turn is dropped.

Bodies are the bulk of the block (three turns at the cap already fill
6000 of 8000 characters) and they are model-written, so they are what
budget pressure spends first."""

TOTAL_HISTORY_CHAR_BUDGET = 8000
"""Hard budget of the whole history block."""

ONE_LINER_QUESTION_CHARS = 100
"""How much of an older question the collapsed reminder line keeps.

Older turns are reminders, not transcript, so compressing them is the
design. Saying so is not optional: the cut carries the same marker the
verbatim path uses, because a bare ellipsis reads like something the
user typed."""

_CUT_MARKER = "\n[... shortened]"
_TRIMMED_HEADER = "[... older parts of the conversation shortened]\n\n"
_QUESTION_CUT_MARKER = " [... question shortened]"

_TERMINAL_WORDS = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


@dataclass(frozen=True)
class SessionContextPack:
    """What a follow-up turn knows about its session."""

    history_block: str = ""
    """The composed conversational history ('' for a first turn)."""

    artifact_registry: tuple[dict[str, Any], ...] = field(
        default_factory=tuple
    )
    """Meta of ALL session deliverables ``{artifact_id, kind, title,
    revision, updated_by}`` — the multi-deliverable index."""

    last_response_form: str = ""
    """Output form of the latest completed turn (routing memory);
    '' when unknown (a run whose stored snapshot and overrides carry no
    response form)."""

    prior_evidence_count: int = 0
    """Distinct references retained by visible turns and session artifacts."""

    prior_references: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    """The references behind that count, one per canonical source in
    session order, with the labels the earlier turns cited them by — a
    follow-up turn registers them as known evidence so "[W3]" in its
    answer still resolves instead of being marked unsupported."""


def build_session_context(
    session_id: str,
    *,
    run_store: Any,
    control: "AgentControlStore",
    run_async: Callable[[Awaitable[Any]], Any],
    visible_to: Any,
    current_run_id: str,
) -> SessionContextPack:
    """Compose the pack from durable rows (never raises — a context
    failure must not kill the run; it degrades VISIBLY to empty).

    Args:
        run_store: The run store port (sync surface).
        control: The agent control store (async surface).
        run_async: Bridge that executes a coroutine from this sync worker
            thread (the algorithm passes its ``_run_async``).
        visible_to: The owner's resolved visibility — the context
            can only ever contain what the OWNER may see.
        current_run_id: Excluded (the turn being answered).
    """
    try:
        return _build(
            session_id,
            run_store=run_store,
            control=control,
            run_async=run_async,
            visible_to=visible_to,
            current_run_id=current_run_id,
        )
    except Exception:  # noqa: BLE001 — degrade visibly, never kill intake
        log.warning(
            "Session context for %s could not be built; the turn continues without "
            "history context.",
            session_id,
            exc_info=True,
        )
        return SessionContextPack()


def _build(
    session_id: str,
    *,
    run_store: Any,
    control: "AgentControlStore",
    run_async: Callable[[Awaitable[Any]], Any],
    visible_to: Any,
    current_run_id: str,
) -> SessionContextPack:
    summaries = [
        summary
        for summary in run_store.list_session_runs(
            session_id, visible_to=visible_to
        )
        if summary.get("run_id") != current_run_id
        and summary.get("kind") != "agent_child"
    ]
    # Partial degradation: a control-store outage costs the artifact
    # registry, never the run-based history (each visibly, per part).
    artifacts: list[Any] = []
    try:
        artifacts = run_async(control.list_session_artifacts(session_id))
    except Exception:  # noqa: BLE001 — degrade visibly, keep the history
        log.warning(
            "Artifact registry for %s unavailable; history context is built without the"
            " deliverables index.",
            session_id,
            exc_info=True,
        )
    # File names derive from the titles in created order (the listing is
    # oldest first) — same algorithm as the frontend, pinned by the
    # shared parity fixture.
    file_names = assign_artifact_file_names(
        (artifact.artifact_id, artifact.title)
        for artifact in artifacts
        if artifact.kind in NAMED_ARTIFACT_KINDS
    )
    registry = tuple(
        {
            "artifact_id": artifact.artifact_id,
            "kind": artifact.kind,
            "title": artifact.title,
            "revision": artifact.revision,
            "updated_by": artifact.updated_by,
            **(
                {"name": file_names[artifact.artifact_id]}
                if artifact.artifact_id in file_names
                else {}
            ),
        }
        for artifact in artifacts
    )
    # Chat answers retain their evidence on the run, without creating a
    # session deliverable. Import their metadata from the already authorized
    # session listing; full search answers remain behind the evidence reader's
    # origin check. Internal bundles do not enter the user-facing file index.
    reference_artifacts = list(artifacts)
    for summary in summaries:
        if summary.get("status") != "completed":
            continue
        try:
            after = None
            while True:
                page, cursor = run_async(control.list_artifacts(
                    str(summary["run_id"]), kind="evidence_bundle", after=after,
                ))
                reference_artifacts.extend(page)
                if not cursor:
                    break
                after = decode_cursor(cursor)
        except Exception as exc:  # noqa: BLE001 — keep other origins and history
            log.warning(
                "Stored run evidence could not be read (error_type=%s).",
                type(exc).__name__,
            )

    from inqtrix.agents.evidence import dedup_key, merge_missing_evidence_fields

    prior_references: dict[str, dict[str, Any]] = {}
    for artifact in reference_artifacts:
        for ref in artifact.refs:
            key = dedup_key(dict(ref))
            if key and key not in prior_references:
                prior_references[key] = dict(ref)
            elif key:
                merge_missing_evidence_fields(prior_references[key], dict(ref))
    evidence_count = len(prior_references)
    if not summaries:
        return SessionContextPack(
            artifact_registry=registry,
            prior_evidence_count=evidence_count,
            prior_references=tuple(prior_references.values()),
        )
    verbatim_ids = {
        summary.get("run_id") for summary in summaries[-RECENT_TURNS_VERBATIM:]
    }
    turns: list[str] = []
    last_response_form = ""
    for summary in summaries:
        run_id = str(summary.get("run_id", ""))
        if summary.get("status") == "completed":
            execution = (summary.get("snapshot") or {}).get("execution") or {}
            overrides = summary.get("agent_overrides") or {}
            last_response_form = str(
                execution.get("response_form")
                or overrides.get("response_form", "")
                or ""
            )
        if run_id in verbatim_ids:
            turns.append(
                _verbatim_turn(
                    summary,
                    run_id=run_id,
                    run_store=run_store,
                    control=control,
                    run_async=run_async,
                    visible_to=visible_to,
                )
            )
        else:
            turns.append(_one_liner(summary))
    return SessionContextPack(
        history_block=_within_budget(turns),
        artifact_registry=registry,
        last_response_form=last_response_form,
        prior_evidence_count=evidence_count,
        prior_references=tuple(prior_references.values()),
    )


@dataclass(frozen=True)
class _VerbatimTurn:
    """One rendered turn, kept in parts so the budget can spend the right one.

    ``head`` is the user's question plus the clarifications and notes of that
    turn; ``body`` is the agent's answer line. Only the body is squeezable.
    """

    head: str
    body: str

    def render(self, body_cap: int | None = None) -> str:
        body = self.body
        if body_cap is not None and len(body) > body_cap:
            body = body[:body_cap].rstrip() + _CUT_MARKER
        return f"{self.head}\n{body}" if self.head else body


def _full_question(
    summary: dict[str, Any],
    *,
    run_id: str,
    run_store: Any,
    visible_to: Any,
) -> str:
    """The question as the user asked it, not the clipped list-row copy.

    ``list_session_runs`` hands out the DISPLAY projection: its question is
    bounded by ``runs.shared.QUESTION_COLUMN_LIMIT`` so a session rail stays
    small, and the row says so with ``question_clipped``. That bound is right
    for a row and wrong here. A standing requirement tends to sit at the END
    of a long question ("answer in at most 800 words") — exactly the part a
    row drops — and the user of a follow-up turn rightly assumes it is still
    in force. The single-run read is the documented way back to the full
    text, and the flag means we pay for that read only when a question was
    actually clipped.
    """
    question = str(summary.get("question", "")).strip()
    if not summary.get("question_clipped"):
        return question
    try:
        full = run_store.get(run_id, visible_to=visible_to).get("question_full")
    except Exception:  # noqa: BLE001 — degrade visibly, keep the turn
        log.warning(
            "session context: full question unavailable for run %s; "
            "the history block keeps the clipped row (its own cut marker "
            "stays visible)",
            run_id,
        )
        return question
    return str(full).strip() if full else question


def _verbatim_turn(
    summary: dict[str, Any],
    *,
    run_id: str,
    run_store: Any,
    control: "AgentControlStore",
    run_async: Callable[[Awaitable[Any]], Any],
    visible_to: Any,
) -> _VerbatimTurn:
    # The question is NOT capped here, and it is read from the durable
    # payload rather than the list row, which the display projection cuts
    # at 500 characters. A cut would drop standing requirements that sit at
    # the end of a long question ("at most 800 words") without a trace a
    # reader could recognise — a bare "…" looks like something the user
    # typed. Budget pressure is handled in
    # :func:`_within_budget`, which spends answer bodies first.
    question = _full_question(
        summary, run_id=run_id, run_store=run_store, visible_to=visible_to
    )
    lines = [f"User: {question}"]
    try:
        for record in reversed(
            run_async(control.list_clarifications(run_id))
        ):
            for prompt, answer in round_qa_lines(
                questions=list(record.questions),
                question=record.question,
                options=list(record.options),
                answers=dict(record.answers),
                answer=record.answer,
                option_id=record.option_id,
            ):
                lines.append(f"Clarification: {prompt} — Answer: {answer}")
        for approval in reversed(run_async(control.list_approvals(run_id))):
            if approval.status == "rejected":
                note = (
                    f" (reason: {approval.note})" if approval.note else ""
                )
                lines.append(
                    f"Note: the user declined the {approval.kind} approval"
                    f"{note}."
                )
            elif approval.decision == "edit":
                lines.append(
                    "Note: the user adjusted the plan before approving it."
                )
    except Exception:  # noqa: BLE001 — degrade visibly, keep the turn
        log.warning(
            "Control data (clarifications/approvals) for run %s unavailable; the turn "
            "appears in history context without these rows.",
            run_id,
            exc_info=True,
        )
    status = str(summary.get("status", ""))
    if status == "completed":
        body = _answer_body(run_id, run_store, visible_to)
        if body:
            lines.append(f"Agent: {body}")
        else:
            lines.append("Agent: (result no longer available)")
    else:
        word = _TERMINAL_WORDS.get(status, status)
        lines.append(f"Agent: run {word}.")
    return _VerbatimTurn(head="\n".join(lines[:-1]), body=lines[-1])


def _one_liner(summary: dict[str, Any]) -> _VerbatimTurn:
    question = str(summary.get("question", "")).strip()
    note = ""
    if len(question) > ONE_LINER_QUESTION_CHARS:
        question = question[:ONE_LINER_QUESTION_CHARS].rstrip()
        note = _QUESTION_CUT_MARKER
    status = str(summary.get("status", ""))
    word = _TERMINAL_WORDS.get(status, status)
    return _VerbatimTurn(
        head="", body=f'Earlier: "{question}"{note} — run {word}.'
    )


def _answer_body(run_id: str, run_store: Any, visible_to: Any) -> str:
    try:
        payload = run_store.result(run_id, visible_to=visible_to)
    except Exception:  # noqa: BLE001 — pruned result is a normal state
        return ""
    body = str(payload.get("answer", "") or "").strip()
    if len(body) > TURN_BODY_CHAR_CAP:
        body = body[:TURN_BODY_CHAR_CAP] + _CUT_MARKER
    return body


def _within_budget(turns: list[_VerbatimTurn]) -> str:
    """Fit the block into the budget, spending the cheapest content first.

    The order is by value, not by position, because the two are not the same
    thing. Answer bodies are the bulk (three turns at the body cap already
    fill 6000 of the 8000 characters) and they are model-written, so they go
    first — every body, oldest first, down to :data:`TURN_BODY_FLOOR_CHARS`,
    and the loop stops the moment the block fits. Only when every body sits
    on that floor do whole older turns drop, and only after that does the
    single surviving body shrink below the floor.

    A user's question is the last thing to go, and it never goes silently:
    the block's own header marks dropped turns, a squeezed body carries the
    cut marker, and a question that truly cannot fit ends in
    ``[... question shortened]`` rather than a bare ellipsis that reads like
    something the user typed.

    The budget counts CHARACTERS, not tokens. That is a proxy, deliberately:
    a tokenizer here would make a pure function depend on the provider, and a
    generous character budget with the right priority order buys more than an
    exact count with the wrong one.
    """
    caps: list[int | None] = [None] * len(turns)

    def rendered(kept_from: int) -> str:
        return "\n\n".join(
            turn.render(caps[index])
            for index, turn in enumerate(turns)
            if index >= kept_from
        )

    kept_from = 0
    # 1. squeeze answer bodies, oldest first, down to the floor
    for index in range(len(turns)):
        if len(rendered(kept_from)) <= TOTAL_HISTORY_CHAR_BUDGET:
            break
        if len(turns[index].body) > TURN_BODY_FLOOR_CHARS:
            caps[index] = TURN_BODY_FLOOR_CHARS

    # 2. drop whole older turns
    trimmed = False
    while (
        kept_from < len(turns) - 1
        and len(rendered(kept_from)) > TOTAL_HISTORY_CHAR_BUDGET
    ):
        kept_from += 1
        trimmed = True

    block = rendered(kept_from)
    prefix = _TRIMMED_HEADER if trimmed else ""
    if len(prefix) + len(block) <= TOTAL_HISTORY_CHAR_BUDGET:
        return prefix + block

    # 3. the single surviving turn is still too large: squeeze its body to
    #    whatever is left, so its question survives whole.
    last = turns[-1]
    room = TOTAL_HISTORY_CHAR_BUDGET - len(prefix) - len(last.head) - 1
    if room > len(_CUT_MARKER):
        return prefix + last.render(room - len(_CUT_MARKER))

    # 4. the question alone exceeds the budget. Cut it, and say so.
    available = (
        TOTAL_HISTORY_CHAR_BUDGET - len(prefix) - len(_QUESTION_CUT_MARKER)
    )
    return prefix + last.head[: max(0, available)] + _QUESTION_CUT_MARKER
