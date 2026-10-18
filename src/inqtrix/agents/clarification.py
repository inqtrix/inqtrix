"""Clarification records.

Small by design: which questions get asked is decided by intake routing
(``ask_user_first``) or the discovery analyst's ``questions_for_user``;
this module only shapes the control-store records and the deterministic
filters in front of them. A question the run does not ask is never a
silent drop: one its settings already answer is quoted with that
setting, one that repeats an earlier round or an assumption is counted
as skipped, and one a cap keeps out becomes a named assumption the
first plan carries.

Structured option questions: the LLM proposes questions with LIKELY
answer options; :func:`sanitize_questions` turns them into the
deterministic shape the node filters and the record stores. Ids are
assigned positionally here — never trusted from the model — so a
re-executed clarify node (interrupt semantics) regenerates the exact
same payload from the checkpointed phase state.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, NamedTuple, Sequence

from inqtrix.agents.control_ports import ClarificationRecord
from inqtrix.agents.limit_contract import parse_limit_gate
from inqtrix.agents.phase_models import (
    AssignmentProfile,
    ClarificationQuestionModel,
    DiscoveryResult,
    ProposedClarificationQuestion,
)
from inqtrix.constants import CLARIFICATION_OPTION_LABEL_CHARS
from inqtrix.core.results import HandoverClarification
from inqtrix.i18n import t, t_count

log = logging.getLogger("inqtrix")

MAX_QUESTIONS_PER_ROUND = 3
"""Hard cap of questions per gate round (one record, one interrupt)."""

MAX_OPTIONS_PER_QUESTION = 4
"""Hard cap of options per question (AskUserQuestion-informed)."""

ASSUMPTION_GUIDANCE_CHARS = 300
"""Display guidance for a proposed default assumption. The assumption is
what a run works with when its question is not asked, so a longer one is
kept whole and named in a WARNING — never shortened."""

_STORED_QUESTION_KEYS = ("id", "prompt", "options", "multi_select", "skill_key")
"""What one question of a stored round carries. ``skill_key`` binds a
kernel skill-point round back to its point when the row is read after a
resume; the proposal's ``subject`` and ``default_assumption`` are
internal to the clarify node and never reach the store or the wire."""


@dataclass(frozen=True)
class RunSettingsFacts:
    """What the run's immutable settings fix before any question is asked.

    Recomputed from the request and the segment's deps whenever needed
    and never checkpointed, so a resumed segment reads the same facts.

    Attributes:
        knowledge: ``off`` (the source policy removes project knowledge),
            ``pinned`` (collection chips), ``all`` (the session switch) or
            ``inherited`` (no explicit choice).
        collection_count: Collections inside the admitted boundary;
            ``None`` for a deliberately unscoped run.
        collections: ``(name, document_count)`` per admitted collection,
            for display only; ``None`` when the names were not read or
            could not be read — never "no collections".
        web: Whether the source policy allows the public web.
        pinned_form: ``chat``/``canvas`` when the request, a skill, a
            patch target or the tier fixes the output form, else ``""``.
        tier: The run's Stufe (``""`` for a run without one).
        cap: The effective clarification-round cap.
        rounds_used: Clarification rounds this run already held
            (checkpointed, so a resumed segment reads the same number).
        autonomy: The run's permission mode.
    """

    knowledge: Literal["off", "pinned", "all", "inherited"]
    collection_count: int | None
    collections: tuple[tuple[str, int], ...] | None
    web: bool
    pinned_form: Literal["", "chat", "canvas"]
    tier: str
    cap: int
    rounds_used: int
    autonomy: str

    @property
    def knowledge_available(self) -> bool:
        """Whether project knowledge is a source this run can search.

        Off by policy, or a boundary that admits no collection, leaves
        nothing to search; an unscoped run (``collection_count`` None)
        searches what the user can see.
        """
        return self.knowledge != "off" and self.collection_count != 0

    def settles(self, subject: str) -> bool:
        """Whether these settings already answer a question on *subject*.

        THE rule — the clarify node filters with it and the prompts mark
        the same settings as fixed. It reads the typed subject and the
        immutable facts only, never a collection name:

        - ``knowledge_scope`` is settled when project knowledge is off,
          the user pinned collections or switched on all of them, or the
          run's boundary holds at most one collection; an inherited
          boundary of two or more and an unscoped run leave it open;
        - ``sources`` is settled when at most one source is available
          (:attr:`knowledge_available` decides for project knowledge);
        - ``output_form`` is settled only by a pinned form — a form the
          intake merely inferred is the assignment's content;
        - ``assignment`` is never settled.
        """
        if subject == "knowledge_scope":
            return self.knowledge in ("off", "pinned", "all") or (
                self.collection_count is not None
                and self.collection_count <= 1
            )
        if subject == "sources":
            return int(self.web) + int(self.knowledge_available) <= 1
        if subject == "output_form":
            return bool(self.pinned_form)
        return False


class SettledQuestion(NamedTuple):
    """A proposed question the run's settings already answer."""

    question: dict[str, Any]
    subject: str
    """The subject whose setting answers it (renders the setting text)."""


def _whole_assumption(text: str, number: int) -> str:
    """*text* on one line and whole; a length over
    :data:`ASSUMPTION_GUIDANCE_CHARS` is named, never cut."""
    text = " ".join(text.split())
    if len(text) > ASSUMPTION_GUIDANCE_CHARS:
        log.warning(
            "Clarification %d default assumption has %d characters "
            "(display guidance %d); kept in full.",
            number,
            len(text),
            ASSUMPTION_GUIDANCE_CHARS,
        )
    return text


def sanitize_questions(
    models: Sequence[ClarificationQuestionModel],
    *,
    limit: int | None = MAX_QUESTIONS_PER_ROUND,
) -> list[dict[str, Any]]:
    """The deterministic shape of LLM-proposed questions.

    Guards (all LOUD, nothing silently dropped without a log line):

    - at most *limit* questions survive (``None`` keeps them all: the
      mission's clarify node applies the round cap itself, AFTER its
      filters, and names every question the cap leaves out);
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

    A :class:`ProposedClarificationQuestion` keeps its ``subject`` and
    ``default_assumption`` as internal keys (the assumption whole, one
    line; :data:`ASSUMPTION_GUIDANCE_CHARS` is display guidance);
    :func:`build_clarification` projects them away.

    Ids are positional (``q1``, ``q1_o2`` ...) so the SAME input always
    yields the SAME payload — required by the interrupt re-execution
    contract (pre-interrupt writes must be idempotent).
    """
    sanitized: list[dict[str, Any]] = []
    for model in models:
        if limit is not None and len(sanitized) >= limit:
            log.warning(
                "Clarification round capped at %d questions (%d proposed).",
                limit,
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
        if isinstance(model, ProposedClarificationQuestion):
            question["subject"] = model.subject
            question["default_assumption"] = _whole_assumption(
                model.default_assumption, number
            )
        sanitized.append(question)
    return sanitized


def number_round(questions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """*questions* with positional ids reassigned for the round they form.

    The filters in front of a round drop questions, so the survivors'
    sanitizer ids can have gaps; a round is numbered ``q1..qn`` in its
    own order, options ``q{n}_o{m}``, deterministically.
    """
    numbered: list[dict[str, Any]] = []
    for number, question in enumerate(questions, start=1):
        numbered.append(
            {
                **question,
                "id": f"q{number}",
                "options": [
                    {**option, "id": f"q{number}_o{index}"}
                    for index, option in enumerate(
                        question.get("options", []), start=1
                    )
                ],
            }
        )
    return numbered


def filter_settled_questions(
    questions: Sequence[dict[str, Any]],
    facts: RunSettingsFacts,
) -> tuple[list[dict[str, Any]], list[SettledQuestion]]:
    """Split proposed questions into kept ones and ones the settings answer.

    The decision is :meth:`RunSettingsFacts.settles` over the typed
    subject — never the question text — so an interrupt re-execution
    decides exactly as the first pass did.
    """
    kept: list[dict[str, Any]] = []
    settled: list[SettledQuestion] = []
    for question in questions:
        subject = str(question.get("subject", ""))
        if facts.settles(subject):
            settled.append(SettledQuestion(question, subject))
        else:
            kept.append(question)
    return kept, settled


def settled_setting_text(
    subject: str, facts: RunSettingsFacts, words: dict[str, Any]
) -> str:
    """The reader's wording of the setting that answers *subject*.

    Only a pinned boundary is called "selected"; a boundary the run
    inherited is named as what the run was released, so the transcript
    never claims a choice the user did not make.
    """
    if subject == "output_form":
        return t(words, f"agent_setting_form_{facts.pinned_form}")
    if subject == "sources":
        parts = [] if facts.web else [t(words, "agent_setting_web_off")]
        if facts.knowledge == "off":
            parts.append(t(words, "agent_setting_knowledge_off"))
        elif not facts.knowledge_available:
            parts.append(t(words, "agent_setting_scope_none"))
        return ", ".join(parts)
    if facts.knowledge == "off":
        return t(words, "agent_setting_knowledge_off")
    if facts.knowledge == "all":
        return t(words, "agent_setting_scope_all")
    if facts.collection_count == 0:
        return t(words, "agent_setting_scope_none")
    names = (
        ", ".join(
            t(words, "agent_setting_collection_name", name=name)
            for name, _count in facts.collections
        )
        if facts.collections
        else ""
    )
    if facts.knowledge == "pinned":
        if names:
            return t_count(
                words,
                "agent_setting_scope_pinned",
                len(facts.collections or ()),
                names=names,
            )
        return t_count(
            words, "agent_setting_scope_count", int(facts.collection_count or 0)
        )
    # Inherited and settled: the boundary holds exactly one collection.
    if names:
        return t(words, "agent_setting_scope_only", names=names)
    return t(words, "agent_setting_scope_only_unnamed")


def settled_narration(
    settled: Sequence[SettledQuestion],
    facts: RunSettingsFacts,
    words: dict[str, Any],
) -> str:
    """One transcript line quoting every settled question and its setting."""
    items = "; ".join(
        t(
            words,
            "agent_clarify_settled_item",
            prompt=entry.question.get("prompt", ""),
            setting=settled_setting_text(entry.subject, facts, words),
        )
        for entry in settled
    )
    return t_count(words, "agent_clarify_settled", len(settled), items=items)


NotAskedReason = Literal["limit", "tier", "overflow"]


def not_asked_assumptions(
    questions: Sequence[dict[str, Any]], reason: NotAskedReason
) -> list[dict[str, str]]:
    """The assumption entries for questions a cap keeps the run from asking."""
    return [
        {
            "prompt": str(question.get("prompt", "")),
            "assumption": str(question.get("default_assumption", "")),
            "reason": reason,
        }
        for question in questions
    ]


def not_asked_narration(
    entries: Sequence[dict[str, str]],
    reason: NotAskedReason,
    words: dict[str, Any],
) -> str:
    """One transcript line naming every question not asked and its assumption."""
    items = "; ".join(
        t(
            words,
            "agent_clarify_assumed_item",
            prompt=entry["prompt"],
            assumption=entry["assumption"].rstrip().removesuffix("."),
        )
        if entry["assumption"].strip()
        else t(words, "agent_clarify_no_assumption", prompt=entry["prompt"])
        for entry in entries
    )
    return t_count(
        words,
        f"agent_clarify_not_asked_{reason}",
        len(entries),
        items=items,
        limit=MAX_QUESTIONS_PER_ROUND,
    )


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
    one — required by every interrupt caller (phase-machine clarify
    node, kernel ``ask_user``): the node re-executes on resume, so the
    get-or-create must land on the same row.

    Every question is projected onto :data:`_STORED_QUESTION_KEYS`, so a
    proposal's internal ``subject`` and ``default_assumption`` reach
    neither the store nor ``/clarifications``.
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


def answered_subjects(
    *,
    questions: Sequence[dict[str, Any]],
    answers: dict[str, Any],
    answer: str,
    option_id: str,
) -> set[str]:
    """The subjects of one round the user actually answered.

    A structured answer counts per question (a picked option or free
    text); a whole-round free-text or legacy option answer answers every
    question of the round. Same reading of ``answers`` as
    :func:`round_qa_lines`, so the transcript and the subjects never
    disagree about what was answered.
    """
    if answers and questions:
        subjects: set[str] = set()
        for item in questions:
            entry = answers.get(item.get("id", "")) or {}
            known = {option["id"] for option in item.get("options", [])}
            picked = [oid for oid in entry.get("option_ids", []) if oid in known]
            if picked or str(entry.get("text", "")).strip():
                subjects.add(str(item.get("subject", "")))
        return subjects - {""}
    if answer or option_id:
        return {str(item.get("subject", "")) for item in questions} - {""}
    return set()


_QUESTION_STOPWORDS = frozenset(
    {
        "an", "am", "auch", "auf", "aus", "bei", "beziehen", "bitte",
        "das", "dem", "den", "der", "des", "die", "du", "ein", "eine",
        "einem", "einen", "einer", "es", "fuer", "für", "im", "in",
        "ist", "mit", "nach", "primaer", "primär", "sich", "sie",
        "soll", "sollen", "sollte", "um", "und", "uns", "unsere",
        "von", "was", "welche", "welchem", "welchen", "welcher",
        "welches", "wie", "wir", "zu", "zum", "zur",
    }
)
"""German question scaffolding that carries no topical signal."""

_TOKEN_PREFIX_CHARS = 6
"""Poor-man's stemming: 'Analyse' and 'analysieren' meet at 'analys'.
A full German stemmer would be a new dependency for one comparison."""

_DUPLICATE_JACCARD = 0.6
_DUPLICATE_CONTAINMENT = 0.8


def _question_tokens(prompt: str) -> frozenset[str]:
    """Topical token set of a question prompt (normalized, prefix-cut)."""
    words = re.findall(r"[0-9a-zA-ZÀ-ſ]+", prompt.casefold())
    return frozenset(
        word[:_TOKEN_PREFIX_CHARS]
        for word in words
        if len(word) >= 2 and word not in _QUESTION_STOPWORDS
    )


def filter_repeated_questions(
    questions: Sequence[dict[str, Any]],
    asked_prompts: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop questions that near-duplicate one this run already handled.

    *asked_prompts* are the questions of earlier rounds AND the ones the
    run answered with an assumption instead of asking. Deterministic
    backstop behind the prompt-side never-re-ask rule: the discovery
    analyst is an independent LLM call and can rephrase an intake
    question ("Auf welchen KI-Markt ...?" -> "Welchen KI-Markt sollen
    wir ...?"). Token-set overlap (Jaccard >= :data:`_DUPLICATE_JACCARD`
    or containment >= :data:`_DUPLICATE_CONTAINMENT` of the smaller set)
    marks the rephrase as the same question.

    Returns:
        ``(kept, dropped_prompts)`` — the caller narrates every dropped
        prompt (No Silent Fallbacks) and proceeds without an interrupt
        when nothing is left to ask.
    """
    asked = [
        tokens
        for prompt in asked_prompts
        if (tokens := _question_tokens(prompt))
    ]
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for question in questions:
        prompt = str(question.get("prompt", ""))
        tokens = _question_tokens(prompt)
        duplicate = False
        for prior in asked:
            if not tokens or not prior:
                continue
            overlap = len(tokens & prior)
            jaccard = overlap / len(tokens | prior)
            containment = overlap / min(len(tokens), len(prior))
            if (
                jaccard >= _DUPLICATE_JACCARD
                or containment >= _DUPLICATE_CONTAINMENT
            ):
                duplicate = True
                break
        if duplicate:
            dropped.append(prompt)
        else:
            kept.append(question)
    return kept, dropped


def intake_questions(
    profile: AssignmentProfile | None,
) -> list[dict[str, Any]]:
    """The ask-user-first questions from the intake profile (sanitized).

    Uncapped: the clarify node applies the round cap after its filters.
    """
    if profile is None:
        return []
    return sanitize_questions(profile.clarification_questions, limit=None)


def discovery_questions(
    discovery: DiscoveryResult | None,
) -> list[dict[str, Any]]:
    """Post-discovery questions: the analyst's ``questions_for_user``.

    A blocking GAP is research the plan must close, not a question — the
    router sends a run to the second clarify round only when this list
    (or a pending skill point) is non-empty. Uncapped like
    :func:`intake_questions`.
    """
    if discovery is None:
        return []
    return sanitize_questions(discovery.questions_for_user, limit=None)
