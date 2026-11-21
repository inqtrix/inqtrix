"""What the kernel does when an internal identifier reaches a reader surface.

A transcript line or a clarification round is handed to a person after the
turn that wrote it is over, so the kernel repairs it deterministically: the
identifier the reader cannot resolve is replaced, visibly and in the run's
answer language, and an identifier the USER wrote stays wherever it stands.
The masking and detection rules, and the promise the prompts make about
identifiers, are shared with every writer.
"""

from __future__ import annotations

from typing import Any

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.agents.report_quality import (
    introduced_identifiers,
    mask_identifiers,
    reference_identifiers,
)

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


def _kernel_deps(question: str) -> KernelDeps:
    """A kernel run asked *question*, whose ledger holds :data:`REFERENCES`."""
    deps = KernelDeps(
        run_id="run_kernel_reader_guard", control=MemoryAgentControlStore(),
        platform=None, llm=None, model=None, reasoning_effort=None, timeout=1,
    )
    deps.question = question
    deps.register_references([dict(reference) for reference in REFERENCES])
    return deps


# -- the transcript ---------------------------------------------------- #


def test_the_kernel_transcript_passes_the_same_guard():
    """The kernel's clarification question, its review prose and its
    children's reports reach the transcript through the reader guard."""
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


def test_the_kernel_transcript_marker_follows_the_answer_language():
    """It is user-visible text, so it is not written in the runtime's."""
    guarded = _kernel_deps("Summarize the quarterly report.").guard_reader_text(
        f"Section '{DOCUMENT_ID}' written."
    )
    assert DOCUMENT_ID not in guarded
    assert "[internal identifier removed]" in guarded


def test_an_identifier_the_user_wrote_stays_in_the_kernel_transcript():
    """One exemption, one projection: the same question the canvas asks."""
    line = f"Abschnitt '{DOCUMENT_ID}' geschrieben."
    assert _kernel_deps(f"Was steht in {DOCUMENT_ID}?").guard_reader_text(line) == line


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


# -- the clarification round -------------------------------------------- #


def test_a_kernel_clarification_round_is_checked_in_both_of_its_texts():
    """The prompt and the option labels are what the person is asked."""
    from inqtrix.agents.clarification import guarded_questions

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
        _kernel_deps("Fasse den Quartalsbericht zusammen.").guard_reader_text,
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
