"""Clarification records.

Small by design: which questions get asked is decided by the kernel's
``ask_user`` call and by the skill-point check; this module only shapes the
control-store records in front of them.

Structured option questions: the model proposes questions with LIKELY
answer options; :func:`sanitize_questions` turns them into the
deterministic shape the record stores. Ids are assigned positionally here —
never trusted from the model — so a re-executed node (interrupt semantics)
regenerates the exact same payload from the checkpointed state.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Sequence

from inqtrix.agents.control_ports import ClarificationRecord
from inqtrix.agents.limit_contract import parse_limit_gate
from inqtrix.agents.phase_models import (
    ClarificationQuestionModel,
)
from inqtrix.constants import CLARIFICATION_OPTION_LABEL_CHARS
from inqtrix.core.results import HandoverClarification

log = logging.getLogger(__name__)

MAX_QUESTIONS_PER_ROUND = 3
"""Hard cap of questions per gate round (one record, one interrupt)."""

MAX_OPTIONS_PER_QUESTION = 4
"""Hard cap of options per question (AskUserQuestion-informed)."""

_STORED_QUESTION_KEYS = ("id", "prompt", "options", "multi_select", "skill_key")
"""What one question of a stored round carries. ``skill_key`` binds a
kernel skill-point round back to its point when the row is read after a
resume."""


def sanitize_questions(
    models: Sequence[ClarificationQuestionModel],
) -> list[dict[str, Any]]:
    """The deterministic shape of LLM-proposed questions.

    Guards (all LOUD, nothing silently dropped without a log line):

    - at most :data:`MAX_QUESTIONS_PER_ROUND` questions survive;
    - a question without a usable prompt is dropped with a warning (an
      empty question cannot be asked);
    - a question with fewer than 2 usable options keeps the question but
      loses the chips (free text always works);
    - options beyond :data:`MAX_OPTIONS_PER_QUESTION` are cut and
      duplicate labels collapse;
    - labels and descriptions are stored whole: the stored label is what
      the user's answer reads, so a shortened one would change the
      answer. A label longer than
      :data:`~inqtrix.constants.CLARIFICATION_OPTION_LABEL_CHARS` (what
      the option schema asks for) is logged and kept.

    Ids are positional (``q1``, ``q1_o2`` ...) so the SAME input always
    yields the SAME payload — required by the interrupt re-execution
    contract (pre-interrupt writes must be idempotent).
    """
    sanitized: list[dict[str, Any]] = []
    for model in models:
        if len(sanitized) >= MAX_QUESTIONS_PER_ROUND:
            log.warning(
                "Clarification round capped at %d questions (%d proposed).",
                MAX_QUESTIONS_PER_ROUND,
                len(models),
            )
            break
        prompt = model.prompt.strip()
        if not prompt:
            log.warning(
                "Clarification without question text discarded (%d options).",
                len(model.options),
            )
            continue
        number = len(sanitized) + 1
        options: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        for option in model.options:
            label = option.label.strip()
            if not label or label.casefold() in seen_labels:
                continue
            if len(options) >= MAX_OPTIONS_PER_QUESTION:
                log.warning(
                    "Clarification %d: options capped at %d.",
                    number,
                    MAX_OPTIONS_PER_QUESTION,
                )
                break
            if len(label) > CLARIFICATION_OPTION_LABEL_CHARS:
                log.warning(
                    "Clarification %d option %d label has %d characters "
                    "(display guidance %d); kept in full.",
                    number,
                    len(options) + 1,
                    len(label),
                    CLARIFICATION_OPTION_LABEL_CHARS,
                )
            seen_labels.add(label.casefold())
            options.append(
                {
                    "id": f"q{number}_o{len(options) + 1}",
                    "label": label,
                    "description": option.description.strip(),
                }
            )
        if len(options) < 2:
            if options:
                log.warning(
                    "Clarification %d has only %d usable option(s); asking it as a "
                    "free-text question.",
                    number,
                    len(options),
                )
            options = []
        question: dict[str, Any] = {
            "id": f"q{number}",
            "prompt": prompt,
            "options": options,
            "multi_select": bool(model.multi_select) and bool(options),
        }
        sanitized.append(question)
    return sanitized


def guarded_questions(
    questions: Sequence[dict[str, Any]],
    guard: Callable[[str], str],
) -> list[dict[str, Any]]:
    """One round's reader text, checked before the round becomes durable.

    A clarification round is a reader surface of both engines: the
    prompts, the option labels and their tooltips are what the person is
    asked, and all of them are written by a model that was handed the
    run's material. The chips
    are rendered straight from the stored row, so a checked row is the
    only thing that makes every later reading of it clean - the card, the
    transcript's own copy, and the row a resumed run finds again.

    The shape lives HERE, with the rest of what a question is, and the
    three callers pass only their own bound guard. Spelling the shape out
    per engine would be three places to keep in agreement about which key
    is reader text, and the third one would be added without the other
    two.

    Everything that is not reader text is carried through untouched -
    ``id`` binds an answer back to its question, ``skill_key`` binds a
    round back to its skill point, and masking either would break the
    answer path to repair a surface nobody reads.
    """
    checked: list[dict[str, Any]] = []
    for question in questions:
        entry = dict(question)
        if entry.get("prompt"):
            entry["prompt"] = guard(str(entry["prompt"]))
        options = entry.get("options")
        if options:
            entry["options"] = [
                {
                    **option,
                    **{
                        # The label is the chip; the description is its
                        # tooltip. Both are shown, so both are checked -
                        # the tooltip is the easier one to forget, which
                        # is the only reason it is named here.
                        key: guard(str(option[key]))
                        for key in ("label", "description")
                        if option.get(key)
                    },
                }
                for option in options
            ]
        checked.append(entry)
    return checked


def build_clarification(
    run_id: str,
    *,
    questions: Sequence[dict[str, Any]],
    default_assumption: str = "",
    clarification_id: str = "",
) -> ClarificationRecord:
    """One pending clarification row for a whole gate round.

    ``question`` (the legacy single-text column) carries the joined
    prompts so older readers and the composer free-text path keep one
    human-readable string; the structured ``questions`` payload is the
    chips source. Legacy single-option rounds keep working: with exactly
    one question its options mirror into the legacy ``options`` column.

    ``clarification_id`` overrides the random id with a DETERMINISTIC
    one — required by every interrupt caller (the kernel's ``ask_user``
    and its skill-point round): the node re-executes on resume, so the
    get-or-create must land on the same row.

    Every question is projected onto :data:`_STORED_QUESTION_KEYS`, so no
    other key a caller put on a question reaches the store or
    ``/clarifications``.
    """
    stored = tuple(
        {key: question[key] for key in _STORED_QUESTION_KEYS if key in question}
        for question in questions
    )
    question_text = " ".join(q["prompt"] for q in stored)
    legacy_options: tuple[dict[str, Any], ...] = ()
    if len(stored) == 1:
        legacy_options = tuple(stored[0]["options"])
    return ClarificationRecord(
        clarification_id=clarification_id or f"clr_{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        question=question_text,
        options=legacy_options,
        questions=stored,
        default_assumption=default_assumption,
    )


def _structured_answers(
    questions: Sequence[dict[str, Any]], answers: dict[str, Any]
) -> list[tuple[str, list[str], str]]:
    """``(prompt, picked labels, free text)`` per question of a round with
    structured *answers*: the one reading of ``answers`` that
    :func:`round_qa_lines` composes and :func:`run_clarifications` keeps
    apart."""
    rows: list[tuple[str, list[str], str]] = []
    for item in questions:
        entry = answers.get(item.get("id", "")) or {}
        labels = {
            option["id"]: option["label"]
            for option in item.get("options", [])
        }
        picked = [
            labels[oid]
            for oid in entry.get("option_ids", [])
            if oid in labels
        ]
        rows.append(
            (str(item.get("prompt", "")), picked, str(entry.get("text", "")).strip())
        )
    return rows


def _whole_round_answer(
    *, options: Sequence[dict[str, Any]], answer: str, option_id: str
) -> tuple[str, str]:
    """``(free text, picked option wording)`` of a round answered as a
    whole: the whole-round free text when there is one, otherwise the
    legacy option's label, or its id when no option of the round names
    one."""
    if answer:
        return answer, ""
    label = next(
        (
            str(option.get("label", ""))
            for option in options
            if str(option.get("id", "")) == option_id and option_id
        ),
        "",
    )
    return "", label or option_id


def round_qa_lines(
    *,
    questions: Sequence[dict[str, Any]],
    question: str,
    options: Sequence[dict[str, Any]],
    answers: dict[str, Any],
    answer: str,
    option_id: str,
) -> list[tuple[str, str]]:
    """Human-readable (prompt, answer) pairs of one ANSWERED round.

    Structured answers compose per question (picked labels, then free
    text); a whole-round free-text or legacy option answer yields the
    single legacy pair. Empty while unanswered. Shared by the clarify
    node's history composition AND the session-context builder so
    transcript and follow-up context can never phrase a round
    differently.
    """
    if answers and questions:
        lines: list[tuple[str, str]] = []
        for prompt, picked, free_text in _structured_answers(questions, answers):
            parts = [part for part in ("; ".join(picked), free_text) if part]
            if parts:
                lines.append((prompt, " — ".join(parts)))
        return lines
    free_text, picked = _whole_round_answer(
        options=options, answer=answer, option_id=option_id
    )
    legacy = free_text or picked
    return [(question, legacy)] if legacy else []


def _unanswered(
    number: int, prompt: str, record: ClarificationRecord
) -> HandoverClarification:
    """A question that got no answer: the run continued with the round's
    assumption, which stays an assumption."""
    return HandoverClarification(
        round=number,
        question=prompt,
        status="unanswered",
        default_assumption=record.default_assumption,
    )


def run_clarifications(
    records: Sequence[ClarificationRecord],
) -> tuple[HandoverClarification, ...]:
    """The run's clarifying questions and what came of each, oldest first.

    *records* come in the control store's order, newest first
    (``list_clarifications``); the record reads them in the order the run
    asked them, and a round's questions keep theirs. A limit gate is the
    server's own decision row, not a question the run asked, and stays out
    (:func:`~inqtrix.agents.limit_contract.parse_limit_gate`); a skill-point
    round is a clarification like any other.

    The chosen options and the free text stay apart, read from the stored
    fields with the same reading :func:`round_qa_lines` composes, never
    from a composed transcript. A whole-round free text or legacy option
    answers the round's joined question. A round that is not answered,
    and a question an answered round left blank, read as unanswered with
    the round's default assumption: an assumption, never an answer.
    """
    entries: list[HandoverClarification] = []
    number = 0
    for record in reversed(records):
        if parse_limit_gate(record) is not None:
            continue
        number += 1
        questions = list(record.questions)
        answered = record.status == "answered"
        if answered and record.answers and questions:
            for prompt, picked, free_text in _structured_answers(
                questions, dict(record.answers)
            ):
                entries.append(
                    HandoverClarification(
                        round=number,
                        question=prompt,
                        chosen_options=tuple(picked),
                        free_text=free_text,
                        status="answered",
                    )
                    if picked or free_text
                    else _unanswered(number, prompt, record)
                )
            continue
        free_text, picked = (
            _whole_round_answer(
                options=list(record.options),
                answer=record.answer,
                option_id=record.option_id,
            )
            if answered
            else ("", "")
        )
        if free_text or picked:
            entries.append(
                HandoverClarification(
                    round=number,
                    question=record.question,
                    chosen_options=(picked,) if picked else (),
                    free_text=free_text,
                    status="answered",
                )
            )
            continue
        prompts = [str(item.get("prompt", "")) for item in questions]
        entries.extend(
            _unanswered(number, prompt, record)
            for prompt in prompts or [record.question]
        )
    return tuple(entries)


