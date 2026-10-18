"""What happens when an internal identifier reaches a reader surface.

The enumeration in ``test_reader_surfaces.py`` says which places hand
text to a person. This module says what each of those places does when
the text carries an identifier the reader cannot resolve and was never
meant to see: a memo heading goes to the bounded repair round, a
transcript line is repaired deterministically because nobody is left to
ask, and an identifier the USER wrote stays wherever it stands.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents import algorithm, synthesis
from inqtrix.agents.phase_models import ReportOutline, ReportSection
from inqtrix.agents.report_quality import (
    introduced_identifiers,
    CitationValidationFailed,
    mask_identifiers,
    reference_identifiers,
    writer_identifiers,
)
from inqtrix.providers.base import StructuredLLMResponse

DOCUMENT_ID = "kd_9f13a4c77e02"
"""One internal identifier, long enough that no prose contains it by chance."""

COLLECTION_ID = "kc_4b8e21d6aa07"
"""The collection that document sits in - an identifier the guard KNOWS.

Used where a test has to show that a MACHINE field is left alone: a
field holding a value the guard does not recognize would survive any
guard at all, so it would prove nothing.
"""

REFERENCES: list[dict[str, Any]] = [
    {
        "label": "K1",
        "document_id": DOCUMENT_ID,
        "collection_id": COLLECTION_ID,
        "title": "Quartalsbericht",
        "url": f"inqtrix://documents/{DOCUMENT_ID}#chunk-3",
    }
]


class _RepairLLM:
    """A provider whose repair answer is fixed, counting what it was asked."""

    def __init__(self, repaired: str) -> None:
        self.repaired = repaired
        self.calls: list[str] = []

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(
        self, prompt: str, *, schema_name: str, **kwargs: Any
    ) -> StructuredLLMResponse:
        del kwargs
        self.calls.append(prompt)
        return StructuredLLMResponse(
            parsed={"markdown": self.repaired},
            content=self.repaired,
            prompt_tokens=5,
            completion_tokens=2,
        )


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "question": "Fasse den Quartalsbericht zusammen.",
        "language": "de",
        "references": [dict(reference) for reference in REFERENCES],
    }
    state.update(overrides)
    return state


def _deps(llm: Any) -> Any:
    return SimpleNamespace(llm=llm, timeout=30.0, emit=lambda *_a, **_k: None)


def _outline(title: str, section_title: str) -> ReportOutline:
    return ReportOutline(
        title=title,
        sections=[
            ReportSection(
                title=section_title,
                focus="Was der Bericht sagt.",
                criterion_ids=["0"],
                evidence_labels=["K1"],
            )
        ],
    )


# -- the memo's own headings ------------------------------------------- #


def test_a_memo_title_carrying_an_identifier_goes_to_the_repair_round():
    """The memo's name is reader text, and it needs a check of its own.

    ``# {title}`` is written by the OUTLINE call, which is handed the
    same evidence cards the section writer is handed — so it can copy the
    same identifiers, and the guard that reads a section's body has never
    seen it. The title is also the artifact's name in the canvas tab and
    in every later listing, so a leak there outlives the run.
    """
    llm = _RepairLLM("# Quartalsbericht")
    state = _state()
    repaired = algorithm._repair_outline_headings(
        _deps(llm),
        state,
        _outline(f"Bericht {DOCUMENT_ID}", "Lage"),
        state["references"],
        model=None,
        effort=None,
    )

    assert repaired.title == "Quartalsbericht"
    assert DOCUMENT_ID not in repaired.title
    assert len(llm.calls) == 1, "a clean section heading must not be repaired"


def test_a_section_heading_carrying_an_identifier_goes_to_the_repair_round():
    """``## {section title}`` comes from the same call and is checked too.

    Repairing only the document title would leave every heading below it
    unguarded, which is the shape the defect had: one surface handled,
    the neighbouring one not.
    """
    llm = _RepairLLM("## Lage")
    state = _state()
    repaired = algorithm._repair_outline_headings(
        _deps(llm),
        state,
        _outline("Bericht", f"Lage laut {DOCUMENT_ID}"),
        state["references"],
        model=None,
        effort=None,
    )

    assert repaired.sections[0].title == "Lage"
    assert repaired.title == "Bericht"
    assert len(llm.calls) == 1


def test_a_clean_outline_travels_on_untouched():
    """The guard is free for the runs that do not need it.

    The outline is checkpointed into the synthesis progress and read back
    after a park, so re-deriving it here would put a second object in
    front of every later reader for nothing. Nothing leaked means nothing
    changed: the same outline, and no model call to find that out.
    """
    llm = _RepairLLM("unused")
    state = _state()
    outline = _outline("Bericht", "Lage")
    assert algorithm._repair_outline_headings(
        _deps(llm), state, outline, state["references"],
        model=None, effort=None,
    ) is outline
    assert llm.calls == []


def test_an_identifier_the_user_wrote_into_a_heading_stays():
    """Their word, and repairing it away answers a different question.

    The same exemption the canvas write makes: an id a person typed is
    theirs to repeat, and a guard that cannot tell a leak from a quote
    sends a correct memo into a repair round for quoting the user back.
    """
    llm = _RepairLLM("# repaired")
    state = _state(question=f"Was steht in {DOCUMENT_ID}?")
    outline = _outline(f"Bericht zu {DOCUMENT_ID}", "Lage")

    assert algorithm._repair_outline_headings(
        _deps(llm), state, outline, state["references"],
        model=None, effort=None,
    ) is outline
    assert llm.calls == []


def test_a_heading_the_repair_empties_fails_instead_of_publishing():
    """No heading is better than a wrong one, and neither is published.

    The repair may return something that is not a heading at all. Taking
    the original back would publish exactly the identifier this round
    exists to remove; taking the empty result would name the document
    with nothing.
    """
    with pytest.raises(CitationValidationFailed):
        synthesis.repair_heading(
            # Accepted by the shared repair round (it is neither empty
            # nor carrying an identifier) and still not a heading.
            _RepairLLM("#"),
            f"Bericht {DOCUMENT_ID}",
            known_labels=["K1"],
            internal_reference_ids=writer_identifiers(REFERENCES),
            authored_by="",
            model=None,
            reasoning_effort=None,
            timeout=30.0,
        )


def test_the_repaired_heading_is_the_line_and_not_the_markdown_around_it():
    """The repair answers in Markdown; a heading is one line of text.

    Keeping the ``#`` would put a literal hash into the artifact's name,
    and keeping a paragraph the model added below it would put a whole
    sentence there.
    """
    cleaned, usage = synthesis.repair_heading(
        _RepairLLM("# Quartalsbericht\n\nDazu ein Satz."),
        f"Bericht {DOCUMENT_ID}",
        known_labels=["K1"],
        internal_reference_ids=writer_identifiers(REFERENCES),
        authored_by="",
        model=None,
        reasoning_effort=None,
        timeout=30.0,
    )
    assert cleaned == "Quartalsbericht"
    assert usage["prompt_tokens"] == 5


# -- the transcript ---------------------------------------------------- #


def test_a_narration_line_carrying_an_identifier_reaches_the_reader_masked():
    """The transcript has no writer left to ask, so the repair is our own.

    A narration line describes a phase that is already over. Re-asking a
    model for one transcript line would pay a call for a side channel;
    dropping the line would take a step of the run away from the reader
    without saying so. The identifier is replaced, visibly, in the run's
    answer language.
    """
    state = _state()
    guarded = algorithm._guard_reader_text(
        state, f"Abschnitt '{DOCUMENT_ID}' geschrieben."
    )

    assert DOCUMENT_ID not in guarded
    assert "[interne Kennung entfernt]" in guarded
    assert guarded.startswith("Abschnitt '")


def test_the_transcript_marker_follows_the_answer_language():
    """It is user-visible text, so it is not written in the runtime's."""
    guarded = algorithm._guard_reader_text(
        _state(language="en"), f"Section '{DOCUMENT_ID}' written."
    )
    assert "[internal identifier removed]" in guarded


def test_an_identifier_the_user_wrote_stays_in_the_transcript_too():
    """One exemption, one projection: the same question the memo asks."""
    state = _state(question=f"Was steht in {DOCUMENT_ID}?")
    line = f"Abschnitt '{DOCUMENT_ID}' geschrieben."
    assert algorithm._guard_reader_text(state, line) == line


def test_the_transcript_emitter_publishes_what_the_guard_returned():
    """The guard is wired into the emission, not merely available beside it."""
    emitted: list[tuple[str, dict[str, Any]]] = []
    deps = SimpleNamespace(emit=lambda name, payload: emitted.append((name, payload)))

    algorithm._emit_narration(
        deps,
        _state(),
        narration_id="n-test",
        kind="synthesis",
        text=f"Abschnitt '{DOCUMENT_ID}' geschrieben.",
        phase="synthesis",
    )

    (_name, payload), = emitted
    assert DOCUMENT_ID not in payload["text"]
    assert "[interne Kennung entfernt]" in payload["text"]


def test_the_kernel_transcript_passes_the_same_guard():
    """Both engines write the same transcript, so both answer for it.

    The kernel emitted its clarification question, its review prose and
    its children's reports straight into the transcript. Nothing was
    wrong with the mission's guard; it simply was not the kernel's.
    """
    from inqtrix.agents.kernel.deps import KernelDeps

    deps = KernelDeps.__new__(KernelDeps)
    deps.__dict__.update(
        question="Fasse den Bericht zusammen.",
        request_user_text="",
        session_history="",
        clarified_answers=(),
        evidence_refs={"K1": dict(REFERENCES[0])},
    )
    import threading

    deps.__dict__["_evidence_lock"] = threading.RLock()

    guarded = deps.guard_reader_text(f"Kindlauf zu {DOCUMENT_ID} fertig.")
    assert DOCUMENT_ID not in guarded
    assert "[interne Kennung entfernt]" in guarded


def test_masking_removes_the_identifier_and_leaves_the_sentence():
    """A masked line is still a line: the reader keeps the step."""
    masked = mask_identifiers(
        f"Abschnitt {DOCUMENT_ID} fertig.", [DOCUMENT_ID], marker="[x]"
    )
    assert masked == "Abschnitt [x] fertig."


def test_detection_and_repair_read_the_word_present_alike():
    """The silent no-op two different readings would produce.

    The guard finds an identifier by plain occurrence; if the repair
    looked for a standalone token instead, the two would disagree exactly
    where an identifier sits inside a longer word - and there the guard
    would report a leak, the repair would change nothing, and the line
    would reach the reader unrepaired with no trace in the log. A stump is
    legible; an
    unrepaired identifier is not.
    """
    for text in (
        f"Siehe {DOCUMENT_ID}9 hier.",
        f"Bericht {DOCUMENT_ID}_v2.pdf gelesen.",
        f"Abschnitt {DOCUMENT_ID} fertig.",
    ):
        leaked = introduced_identifiers(text, [DOCUMENT_ID], authored_by="")
        assert leaked == [DOCUMENT_ID], text
        repaired = mask_identifiers(text, leaked, marker="[x]")
        assert DOCUMENT_ID not in repaired, repaired
        assert "[x]" in repaired


def test_a_longer_mask_that_already_covered_an_identifier_is_no_second_repair():
    """Finding nothing is the truth here, not a failure.

    An internal address contains the document id. Masked longest first,
    the address goes first and the id inside it is gone with it - the
    second pass finds nothing because the work is done, which is the one
    case where a skip is honest.
    """
    address = f"inqtrix://documents/{DOCUMENT_ID}"
    repaired = mask_identifiers(
        f"Quelle {address} gelesen.", [DOCUMENT_ID, address], marker="[x]"
    )
    assert repaired == "Quelle [x] gelesen."


# -- what the prompt promises ------------------------------------------ #


def test_the_prompt_promises_exactly_the_id_families_the_guard_reads():
    """A promise the guard cannot keep is the gap seen from the other side.

    Two hand-written lists had drifted apart: the rule named a "search"
    and a "run" id the fields call ``query`` and ``source_run``, and said
    nothing at all about the ``source`` ids the guard does read — and a
    family a rule leaves out reads as permission.
    """
    from inqtrix.agents.evidence_context import INTERNAL_SOURCE_SCHEME
    from inqtrix.agents.prompts import NO_MODEL_SOURCE_SECTIONS
    from inqtrix.agents.report_quality import (
        IDENTIFIER_FAMILIES,
        REFERENCE_ID_FIELDS,
    )

    # Read the promise back out of the sentence a model is given, so an
    # edit to the wording is judged by what the guard actually reads.
    promised = NO_MODEL_SOURCE_SECTIONS.split("this server (", 1)[1].split(
        " ids)", 1
    )[0]
    named = {
        part.strip() for part in promised.replace(" and ", ", ").split(",")
    }
    assert named == set(IDENTIFIER_FAMILIES), (
        "the prompt and the guard disagree about which id families are "
        f"forbidden; promised only: {sorted(named - set(IDENTIFIER_FAMILIES))}, "
        f"read but never named: {sorted(set(IDENTIFIER_FAMILIES) - named)}"
    )
    assert INTERNAL_SOURCE_SCHEME in NO_MODEL_SOURCE_SECTIONS
    # Every field the guard reads has a family in that promise.
    for field in REFERENCE_ID_FIELDS:
        family = field.removesuffix("s").removesuffix("_id").replace("_", " ")
        assert family in named


def test_the_internal_source_link_is_an_identifier_the_guard_reads():
    """The prompt forbids the address too, so the guard must know it.

    An internal address is bookkeeping for tool arguments, exactly like
    the ids inside it. A web URL stays out: there the address IS what a
    reader would look up.
    """
    internal = reference_identifiers(dict(REFERENCES[0]))
    assert f"inqtrix://documents/{DOCUMENT_ID}#chunk-3" in internal

    web = reference_identifiers(
        {"label": "W1", "url": "https://example.org/report", "source_id": "src_1"}
    )
    assert "https://example.org/report" not in web
    assert "src_1" in web


def test_a_line_emitted_before_the_ledger_brings_its_own_evidence():
    """The guard can only find what it was given.

    A run merges its evidence ledger ONCE, in ``_node_evidence``. Every
    transcript line a phase before it emits therefore sees
    ``state["references"]`` empty - and a guard over an empty set finds
    nothing however hard it looks. The subtask line is where that matters:
    its text comes from a model whose search results name documents by
    raw id, and the evidence that model produced is in hand at the very
    call site. Passing it is the difference between a guard and the
    appearance of one.
    """
    import ast
    import inspect

    from inqtrix.agents import algorithm

    source = inspect.getsource(algorithm._emit_task_ended)
    call = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_emit_narration"
    )
    handed = {
        kw.arg: ast.unparse(kw.value) for kw in call.keywords
    }
    assert handed.get("evidence") == "list(outcome.evidence)", handed

    # And the guard really widens its haystack with it.
    from inqtrix.agents.algorithm import _guard_reader_text

    state = {"language": "de", "question": ""}
    line = f"Teilaufgabe gelesen: {DOCUMENT_ID}."
    assert _guard_reader_text(state, line) == line, (
        "without evidence the guard has nothing to find - that is the defect"
    )
    repaired = _guard_reader_text(
        state, line, evidence=[{"document_id": DOCUMENT_ID, "label": "K1"}]
    )
    assert DOCUMENT_ID not in repaired, repaired


# -- the plan, the task result and the clarification round ---------------- #
#
# Three durable records a person reads, and the reason they are checked
# where they become durable rather than where they are rendered: each is
# read from several places that know nothing about each other, and an
# enumeration of rendering components ages with the front end.


def _plan_records(title: str, objective: str = "", summary: str = ""):
    from inqtrix.agents.control_ports import PlanRecord, PlanTaskRecord

    plan = PlanRecord(
        plan_id="pl_1",
        run_id="r_1",
        version=1,
        status="proposed",
        created_by="agent",
        summary_markdown=summary,
    )
    task = PlanTaskRecord(
        task_id="task-1",
        plan_id="pl_1",
        run_id="r_1",
        ordinal=0,
        title=title,
        tool_kind="knowledge_ask",
        objective=objective,
        params={"collection_ids": [COLLECTION_ID]},
    )
    return plan, [task]


def test_a_plan_card_and_the_transcript_read_the_same_task_title():
    """One string, one answer — whichever surface the reader looks at.

    This is the defect that made the plan a declared reader surface. The
    task title was masked in the transcript, because ``task_narration``
    goes through the narration wrapper, and shown verbatim on the plan
    card, because nothing checked the stored row. The reader saw the same
    sentence twice, once repaired and once not, and had no way to know
    which one to believe.
    """
    state = _state()
    title = f"Bericht {DOCUMENT_ID} auswerten"

    _plan, tasks = algorithm._guarded_plan(state, *_plan_records(title))

    assert DOCUMENT_ID not in tasks[0].title
    assert tasks[0].title == algorithm._guard_reader_text(state, title)


def test_the_plans_intent_and_every_objective_are_checked_too():
    """The planner writes three fields a person reads, not one."""
    state = _state()
    plan, tasks = algorithm._guarded_plan(
        state,
        *_plan_records(
            "Bericht auswerten",
            objective=f"Lies {DOCUMENT_ID} vollstaendig.",
            summary=f"Wir werten {DOCUMENT_ID} aus.",
        ),
    )

    assert DOCUMENT_ID not in plan.summary_markdown
    assert DOCUMENT_ID not in tasks[0].objective
    assert "[interne Kennung entfernt]" in tasks[0].objective


def test_a_plan_field_that_binds_rows_together_is_carried_through():
    """Masking a machine field would break the run to repair nothing.

    ``params`` carries the collection the task is scoped to, and that id
    is one the guard KNOWS - it is a reference field, so a guard let
    loose over the whole record would find it and replace it with a
    marker. The task would then be scoped to a sentence. Same for
    ``task_id``, which ``depends_on`` points at and an answer comes back
    keyed by.

    The fixture uses the collection the references name on purpose: a
    machine field holding a value nothing recognizes proves nothing,
    because no guard would have touched it either.
    """
    state = _state()
    _plan, tasks = algorithm._guarded_plan(
        state, *_plan_records(f"Bericht {DOCUMENT_ID}")
    )

    assert tasks[0].task_id == "task-1"
    assert tasks[0].params == {"collection_ids": [COLLECTION_ID]}


def test_an_identifier_the_user_wrote_stays_in_the_plan():
    """The same exemption the memo and the transcript already make."""
    state = _state(question=f"Was steht in {DOCUMENT_ID}?")
    title = f"Bericht {DOCUMENT_ID} auswerten"

    _plan, tasks = algorithm._guarded_plan(state, *_plan_records(title))

    assert tasks[0].title == title


def _task_outcome(summary: str):
    from inqtrix.agents.scheduler import TaskOutcome

    return TaskOutcome(
        status="completed",
        summary=summary,
        evidence=[dict(reference) for reference in REFERENCES],
    )


def test_a_task_result_reaches_the_card_masked():
    """The line most likely to carry an identifier of all of them.

    A subtask's summary comes from a model that was handed raw search
    results and their document ids — unlike the plan, which is written
    from a digest that names sources by title.
    """
    emitted: list[tuple[str, dict[str, Any]]] = []
    deps = SimpleNamespace(
        emit=lambda name, payload: emitted.append((name, payload))
    )

    algorithm._emit_task_ended(
        deps,
        _state(),
        None,
        "task-1",
        _task_outcome(f"Der Bericht {DOCUMENT_ID} nennt drei Zahlen."),
    )

    finished = [
        payload for name, payload in emitted if name.endswith("task.finished")
    ]
    assert finished, [name for name, _ in emitted]
    assert DOCUMENT_ID not in finished[0]["result_summary"]
    assert "[interne Kennung entfernt]" in finished[0]["result_summary"]


def test_a_task_result_is_masked_before_the_clip_so_the_row_still_fits():
    """Order matters here, and getting it wrong turns a leak into a crash.

    The repair replaces one identifier with a several-word marker. The
    store refuses a summary longer than its word bound, so masking an
    already-bounded summary could push it over — and a terminal
    transition would raise instead of leaking. Guarded first, clipped
    second, and the bound holds by construction.
    """
    from inqtrix.agents.control_ports import TASK_RESULT_SUMMARY_MAX_WORDS

    emitted: list[tuple[str, dict[str, Any]]] = []
    deps = SimpleNamespace(
        emit=lambda name, payload: emitted.append((name, payload))
    )
    long_summary = " ".join(
        [DOCUMENT_ID] * 4 + ["Wort"] * TASK_RESULT_SUMMARY_MAX_WORDS
    )

    algorithm._emit_task_ended(
        deps, _state(), None, "task-1", _task_outcome(long_summary)
    )

    summary = [
        payload for name, payload in emitted if name.endswith("task.finished")
    ][0]["result_summary"]
    assert len(summary.split()) <= TASK_RESULT_SUMMARY_MAX_WORDS
    assert DOCUMENT_ID not in summary


def test_a_clarification_round_is_checked_in_both_of_its_texts():
    """The prompt and the option labels are what the person is asked."""
    from inqtrix.agents.clarification import guarded_questions

    state = _state()
    checked = guarded_questions(
        [
            {
                "id": "q1",
                "prompt": f"Meinst du {DOCUMENT_ID}?",
                "options": [
                    {
                        "id": "o1",
                        "label": f"Ja, {DOCUMENT_ID}",
                        "description": f"Das Dokument {DOCUMENT_ID}.",
                    },
                    {"id": "o2", "label": "Nein"},
                ],
                "skill_key": "sk_1",
            }
        ],
        lambda text: algorithm._guard_reader_text(state, text),
    )

    assert DOCUMENT_ID not in checked[0]["prompt"]
    assert DOCUMENT_ID not in checked[0]["options"][0]["label"]
    # The tooltip under the chip is shown too, so it is checked too.
    assert DOCUMENT_ID not in checked[0]["options"][0]["description"]
    assert checked[0]["options"][1]["label"] == "Nein"
    # What binds an answer back to its question is not prose.
    assert checked[0]["id"] == "q1"
    assert checked[0]["options"][0]["id"] == "o1"
    assert checked[0]["skill_key"] == "sk_1"
