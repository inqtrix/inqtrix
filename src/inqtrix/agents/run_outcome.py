"""The outcome record of a conversational kernel run: what its review did, what stayed open, why.

One record per run segment that reaches a decision of the algorithm
(completed, cancelled or failed), sent as ``inqtrix.agent.outcome`` through
:func:`send_outcome` by ``KernelAgentAlgorithm.run`` (a kernel role run sends
none). ``final`` is the algorithm's own decision, not the run's terminal
status: a cancel that races a normal return, or a publication failure after
the algorithm, can still end the run otherwise. Runs cancelled while parked or
waiting, and runs that fail before the graph starts, have no record.

Built here, pure over one input: the review report in the shape the review
pass writes (:func:`~inqtrix.agents.kernel.review_pass.report_facts`), the
``critic`` dict beside ``last_review``, which the kernel builds from its
segment's review. Every finding the text did not take over is a line with its
reason code. A field the report does not carry is never read as complete or as
revised; a review report written before it carried its round, revision and
accounting, before its lines carried their reasons, before it carried its
reach, or before its findings carried why the re-check left them unverified (a
checkpoint resumed across a deploy), is built with a WARNING, since its record
then reads ``round`` 0 beside a completed review, a finding line without its
reason, or a summary without the review's reach or those reasons.

The record also carries its summary in reader words
(:func:`outcome_words`), built from its counts and states only, never from
a line's text: the final review narration reads it word for word.
A review that counted the delivered statements
(:mod:`~inqtrix.agents.kernel.claim_states`) reads as that count, and what
it did besides stands in the record's details (:func:`outcome_details`).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from inqtrix.agents.kernel.claim_states import STATES
from inqtrix.agents.kernel.review_edits import RECHECK_REASONS
from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded
from inqtrix.i18n import t, t_count
from inqtrix.harness.contracts.events import OUTCOME_EVENT

log = logging.getLogger(__name__)


REPORT_FACTS = ("round", "revision", "accounting")
"""The review report's own facts the record reads; a report without them
predates the record."""


def build_outcome(
    state: Mapping[str, Any],
    *,
    run_id: str,
    final: str,
    guard: Callable[[str], str],
) -> dict[str, Any]:
    """The record of *state* (pure). *guard* is the reader guard every line
    and the summary pass.

    ``review`` is ``completed``, ``unavailable``, ``waived`` (the run's Stufe
    skips the review) or ``not_run``; an ``unavailable`` one carries its
    ``unavailable_reason`` and, when that is ``inconsistent``, the flags the
    verdict gave no reason for in ``unexplained``. ``basis`` is what the
    server decided an assessed verdict could check against (``evidence``,
    ``knowledge`` or ``provider_context``); a record without an assessed
    verdict reads ``evidence``. ``round`` is the review round (0 without
    one); ``revision`` ``none``, ``verified`` (every edit applied and held
    by the re-check), ``unverified`` (every edit applied, at least one not
    held by the re-check), ``partially_discarded`` (some findings applied,
    some named) or ``discarded``; ``accounting`` ``complete`` only for a
    completed review that settled every criterion; ``applied`` the
    findings whose edit was applied, ``verified`` those of them the re-check
    held. ``limits``, ``codes`` and ``open.by_code`` stay empty: a kernel
    run has no research route, so nothing names why research stopped (a
    stored mission run's record carries them). A named finding's line
    carries its reason code. ``open.by_kind`` counts
    an unmet output requirement as ``text_defect``, or as ``revised`` when
    at least one counted finding concerns it and every such finding
    was applied, held by the re-check or not (its entry's ``state``,
    ``revised`` or ``verified``; an entry without one reads open).
    ``reach`` is the review's re-read and cited counts. A ``marked`` line
    is a passage that stays as written with its mark, counted apart from
    the findings no revision applied. ``claims`` counts each delivered output's
    statements by state, ``marks`` names every insufficiently supported one
    with its passage and its note (the note passes *guard*, the passage is
    the delivered text's own); both only when the review counted any.
    ``summary`` is :func:`outcome_words` over the record, in the run's
    language (``state["language"]``), and ``details`` is
    :func:`outcome_details` when it says anything.
    """
    critic: Mapping[str, Any] = state.get("critic") or {}
    language = str(state.get("language") or "")
    last_review: Mapping[str, Any] = state.get("last_review") or {}
    missing = [
        *([] if last_review or not critic else ["last_review"]),
        *(key for key in REPORT_FACTS if critic and key not in critic),
    ]
    if missing:
        log.warning(
            "Outcome record built from a review report without %s (a checkpoint "
            "from before the record); the missing fields read as round 0, "
            "revision none and accounting incomplete.",
            ", ".join(missing),
        )
    review = str(
        last_review.get("status") or ("completed" if state.get("critic") else "not_run")
    )
    uncovered = [
        entry for entry in critic.get("criteria_uncovered") or [] if isinstance(entry, Mapping)
    ]
    lines = [
        entry for entry in critic.get("open_lines") or [] if isinstance(entry, Mapping)
    ]
    if any(entry.get("kind") == "finding" and "reason" not in entry for entry in lines):
        log.warning(
            "Outcome record lines built from a review report without their reasons "
            "(a checkpoint from before the per-finding outcome); those lines carry none."
        )
    requirements = [
        entry for entry in uncovered if entry.get("kind") == "output_requirement"
    ]
    worked_on = ("revised", "verified")
    by_kind = {
        "information": sum(1 for entry in uncovered if entry.get("kind") == "information"),
        "text_defect": sum(1 for entry in requirements if entry.get("state") not in worked_on),
        "revised": sum(1 for entry in requirements if entry.get("state") in worked_on),
        "finding": sum(1 for entry in lines if entry.get("kind") == "finding"),
        "marked": sum(1 for entry in lines if entry.get("kind") == "marked"),
        "not_checked": sum(1 for entry in lines if entry.get("kind") == "not_checked"),
    }
    claims = {
        str(output_id): {key: int(counts.get(key) or 0) for key in ("total", *STATES)}
        for output_id, counts in (critic.get("claims") or {}).items()
        if isinstance(counts, Mapping) and counts.get("total")
    }
    marks = [
        {
            "output_id": str(mark.get("output_id") or ""),
            "quote": str(mark["quote"]),
            "state": str(mark.get("state") or ""),
            "reason": str(mark.get("reason") or ""),
            "note": guard(str(mark.get("note") or "")),
        }
        for mark in critic.get("marks") or []
        if isinstance(mark, Mapping) and mark.get("quote")
    ]
    raw_reach = critic.get("reach")
    reach = (
        {
            "reread": int(raw_reach.get("reread") or 0),
            "cited": int(raw_reach.get("cited") or 0),
            "provider_only": int(raw_reach.get("provider_only") or 0),
        }
        if isinstance(raw_reach, Mapping)
        else None
    )
    if review == "completed" and reach is None:
        log.warning(
            "Outcome record built from a review report without its reach (a "
            "checkpoint from before the report carried it); the summary names "
            "no reach."
        )
    applied = [
        finding
        for finding in critic.get("findings") or []
        if isinstance(finding, Mapping) and finding.get("state") == "applied"
    ]
    if any("recheck_reason" not in finding for finding in applied):
        log.warning(
            "Outcome record built from a review report without re-check reasons (a "
            "checkpoint from before the findings carried them); the summary names none."
        )
    unverified = Counter(
        str(finding.get("recheck_reason") or "")
        for finding in applied
        if finding.get("recheck") != "held"
    )
    record: dict[str, Any] = {
        "outcome_id": f"{run_id}:outcome",
        "final": final,
        "review": review,
        "round": int(last_review.get("round") or 0),
        "revision": str(critic.get("revision", "none")),
        "accounting": (
            "complete"
            if review == "completed" and critic.get("accounting") == "complete"
            else "incomplete"
        ),
        # What the server decided the assessed verdict could check against:
        # a completed review carries it in its report, a contradictory one
        # beside its reason. A record without an assessed verdict (no review
        # owed, none run, none returned) and a report from before the basis
        # was recorded read ``evidence``, worded as before.
        "basis": next(
            (
                str(source["basis"])
                for source in (critic, last_review)
                if source.get("basis") in ("knowledge", "provider_context")
            ),
            "evidence",
        ),
        # Why a review owed no usable verdict, and for an inconsistent one the
        # flags it gave no reason for: the reader text names them.
        **(
            {"unavailable_reason": str(last_review.get("reason") or "")}
            if review == "unavailable" else {}
        ),
        **(
            {"unexplained": [str(flag) for flag in last_review["unexplained"]]}
            if review == "unavailable" and last_review.get("unexplained") else {}
        ),
        "applied": len(applied),
        "verified": sum(1 for finding in applied if finding.get("recheck") == "held"),
        "unverified_reasons": {
            reason: unverified[reason] for reason in RECHECK_REASONS if unverified[reason]
        },
        **({"reach": reach} if reach is not None else {}),
        "codes": [],
        "limits": [],
        "open": {"by_code": {}, "by_kind": by_kind},
        "lines": [
            {
                "kind": str(entry.get("kind") or ""),
                "text": guard(str(entry.get("text") or "")),
                "reasons": [str(entry["reason"])] if entry.get("reason") else [],
            }
            for entry in lines
        ],
        **({"claims": claims} if claims else {}),
        **({"marks": marks} if marks else {}),
    }
    record["summary"] = guard(outcome_words(record, language))
    if details := outcome_details(record, language):
        record["details"] = guard(details)
    return record


def outcome_words(record: Mapping[str, Any], language: str) -> str:
    """The summary of an outcome *record* in reader words (pure).

    Built from the record's states and counts only, never from a line's
    text, so the sentence stays closed vocabulary wherever it is shown. An
    applied edit the re-check held reads "behoben und nachgeprüft" (fixed
    and re-checked); every other applied edit reads "überarbeitet, nicht
    nachgeprüft" (revised, not re-checked): no word says "behoben" about an
    edit the re-check did not hold, and the reasons it left them unverified
    follow in parentheses, counted, in the order of
    :data:`~inqtrix.agents.kernel.review_edits.RECHECK_REASONS` (a finding
    that never reached the re-check has none). A completed review names
    every kind that stayed open, and "ohne Befund" (no findings) only when
    none did. A review from general knowledge (``basis`` ``knowledge``) says
    so once, in place of "Geprüft", and calls its applied edits revised:
    without a source nothing was re-checked, so no word says re-checked or
    not re-checked.

    A review that counted the delivered statements (``claims``) reads as
    that count instead, the plaque: "Geprüft · 24 Aussagen: …", each state
    of :data:`~inqtrix.agents.kernel.claim_states.STATES` that counts, summed
    over the outputs; everything else it names moves to
    :func:`outcome_details`.
    """
    words = {"language": language}
    review = record.get("review")
    if review == "waived":
        return t(words, "outcome_waived")
    if review == "unavailable":
        return _unavailable_words(record, words)
    if review == "not_run":
        return t(
            words,
            "outcome_not_run" if record.get("final") == "completed"
            else "outcome_ended_before_review",
        )
    if review != "completed":
        raise ValueError(f"Outcome record with an unknown review state {review!r}.")
    head = t(words, _REVIEWED_HEAD.get(str(record.get("basis") or ""), "outcome_reviewed"))
    totals = _claim_totals(record)
    if totals["total"]:
        return " · ".join([head, t(
            words,
            "outcome_claims_states",
            claims=t_count(words, "outcome_claims", totals["total"]),
            states=", ".join(
                t_count(words, f"outcome_claim_{state}", totals[state])
                for state in STATES
                if totals[state]
            ),
        )])
    return " · ".join([head, *_review_parts(record, words)])


def outcome_details(record: Mapping[str, Any], language: str) -> str:
    """What a review that counted the delivered statements did besides, in
    reader words: everything :func:`outcome_words` would name without the
    count, from "ohne Befund" to the reach. ``""`` for every other record,
    whose summary names it all."""
    if record.get("review") != "completed" or not _claim_totals(record)["total"]:
        return ""
    return " · ".join(_review_parts(record, {"language": language}))


def _claim_totals(record: Mapping[str, Any]) -> Counter[str]:
    """The record's statement counts summed over its outputs."""
    totals: Counter[str] = Counter()
    for counts in (record.get("claims") or {}).values():
        totals.update({key: int(value or 0) for key, value in counts.items()})
    return totals


_REVIEWED_HEAD = {
    "knowledge": "outcome_reviewed_knowledge",
    "provider_context": "outcome_reviewed_provider",
}
"""The head of a completed review by its basis; the provider's limit is named
here once, so no part repeats it."""

_UNAVAILABLE_WORDS = {
    "failed": "outcome_review_failed",
    "budget_exhausted": "outcome_review_budget",
}


def _unavailable_words(record: Mapping[str, Any], words: dict[str, Any]) -> str:
    """Why a review left no usable verdict, in reader words: an inconsistent
    verdict names the flags it gave no reason for, a failed review and one
    stopped at its budget say so, every other reason reads "not usable"."""
    reason = str(record.get("unavailable_reason") or "")
    flags = [str(flag) for flag in record.get("unexplained") or []]
    if reason == "inconsistent" and flags:
        return t(
            words,
            "outcome_review_inconsistent",
            flags=", ".join(t(words, f"review_flag_{flag}") for flag in flags),
        )
    return t(words, _UNAVAILABLE_WORDS.get(reason, "outcome_unavailable"))


def _review_parts(record: Mapping[str, Any], words: dict[str, Any]) -> list[str]:
    """The parts of a completed review's summary after its head: each kind
    that stayed open with its count, or "ohne Befund", then the reach and
    the references with provider context alone."""
    by_kind: Mapping[str, int] = record["open"]["by_kind"]
    verified = int(record.get("verified") or 0)
    knowledge = record.get("basis") == "knowledge"
    counted = [
        ("outcome_verified", verified),
        (
            "outcome_revised" if knowledge else "outcome_applied",
            int(record.get("applied") or 0) - verified,
        ),
        ("outcome_named", by_kind.get("finding", 0)),
        ("outcome_not_checked", by_kind.get("not_checked", 0)),
        (
            "outcome_criteria_open",
            by_kind.get("information", 0) + by_kind.get("text_defect", 0),
        ),
    ]
    parts: list[str] = []
    for key, count in counted:
        if not count:
            continue
        part = t_count(words, key, count)
        if key == "outcome_applied":
            part = _with_reasons(part, record.get("unverified_reasons") or {}, words)
        parts.append(part)
    if not parts:
        parts.append(t(words, "outcome_no_findings"))
    reach: Mapping[str, int] = record.get("reach") or {}
    if reach.get("cited"):
        parts.append(t(
            words,
            "outcome_reach",
            read=reach.get("reread", 0),
            cited=t_count(words, "review_coverage_cited", reach["cited"]),
        ))
    if reach.get("provider_only") and record.get("basis") != "provider_context":
        # One aggregated line, never one finding per statement: how many cited
        # references rest on the search provider's context alone.
        parts.append(t_count(words, "outcome_provider_only", reach["provider_only"]))
    return parts


def _with_reasons(part: str, reasons: Mapping[str, int], words: dict[str, Any]) -> str:
    """*part*, the count of revised findings not re-checked, with the
    reasons the re-check left them unverified."""
    named = [
        t(words, f"outcome_reason_{reason}", count=reasons[reason])
        for reason in RECHECK_REASONS
        if reasons.get(reason)
    ]
    if not named:
        return part
    return t(words, "outcome_applied_reasons", applied=part, reasons=", ".join(named))


def send_outcome(
    emit: Callable[[str, dict[str, Any]], None],
    read_state: Callable[[], Mapping[str, Any]],
    *,
    run_id: str,
    final: str,
    guard_for: Callable[[Mapping[str, Any]], Callable[[str], str]],
) -> None:
    """Build the record from ``read_state()`` and send it through *emit*.

    ``final`` is the algorithm's decision at its exit; *guard_for* gives
    the run's reader guard for the state read, which every line passes.
    Reading the state, building the record and sending it may each fail;
    the record is then missing, visibly in the log, and never replaces the
    error the run is already leaving with.
    """
    try:
        values = dict(read_state() or {})
        emit(
            OUTCOME_EVENT,
            build_outcome(
                values, run_id=run_id, final=final, guard=guard_for(values)
            ),
        )
    except Exception as exc:  # noqa: BLE001 — the record must not mask the run's own exit
        log.warning(
            "Run outcome record could not be sent (final=%s, error_type=%s).",
            final,
            type(exc).__name__,
        )


def cancel_final(exc: AgentCancelled) -> str:
    """The ``final`` of a run stopped by *exc*: a token budget fails the
    run, every other stop cancels it."""
    return "failed" if isinstance(exc, AgentTokenBudgetExceeded) else "cancelled"
