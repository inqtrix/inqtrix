"""Quote-then-answer grounding tests: parsing, verification, wiring.

Pure unit tests cover the deterministic quote check (whitespace
tolerance, German quotation marks, paraphrase and out-of-range
rejection, fallback shapes); algorithm tests assert the answer prompt
gains the quote instruction, the quote block is stripped from the
user-facing answer, verification lands visibly in ``result_state`` and
the run event stream, and ``grounding=off`` restores the previous
prompt and output byte-for-byte.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any


from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.results import RunRequest
from inqtrix.knowledge.algorithm import (
    KNOWLEDGE_ANSWER_INCOMPLETE,
    KnowledgeAlgorithm,
)
from inqtrix.knowledge.grounding import (
    GROUNDING_MARKER_FORMAT_REPAIRED,
    GROUNDING_MARKER_FALLBACK,
    GROUNDING_MARKER_PARSED,
    GroundingFailureCode,
    GroundingStatus,
    check_grounding,
    dehyphenate,
    quote_is_verbatim,
    quote_key,
    strip_page_break_artifacts,
)
from inqtrix.runtime_logging import sanitize_event_payload
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.base import LLMResponse, ProviderContext
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import AgentSettings, Settings

from tests.contract._app import StubSearch
from tests.test_knowledge_engine import StubEmbeddings

EVIDENCE = [
    "[K1] Rahmenvertrag (Abschnitt 1)\n"
    "Die Haftung ist auf den Auftragswert begrenzt.",
    "[K2] Reiserichtlinie (Abschnitt 1)\n"
    "Die Verpflegungspauschale betraegt 28 Euro pro Tag.",
]


# ------------------------------------------------------------------ #
# check_grounding unit tests
# ------------------------------------------------------------------ #


def test_verified_quote_and_stripped_answer():
    content = (
        "QUOTES:\n"
        '[K1] "Die Haftung ist auf den Auftragswert begrenzt."\n'
        "\n"
        "ANSWER:\n"
        "Die Haftung ist begrenzt [K1]."
    )
    report = check_grounding(content, EVIDENCE)
    assert report.marker == GROUNDING_MARKER_PARSED
    assert report.status is GroundingStatus.VERIFIED
    assert report.publishable is True
    assert report.answer == "Die Haftung ist begrenzt [K1]."
    assert [(q.label, q.verified) for q in report.quotes] == [("K1", True)]


def test_whitespace_differences_do_not_fail_verification():
    content = (
        "QUOTES:\n"
        '[K1] "Die Haftung   ist auf den\nAuftragswert begrenzt."\n'
        "ANSWER:\n"
        "Antwort [K1]."
    )
    # The quote regex is line-based; a wrapped quote is two lines and
    # only the complete single-line form parses — so feed a quote with
    # collapsed-vs-multiple spaces instead.
    content = content.replace("den\nAuftragswert", "den  Auftragswert")
    report = check_grounding(content, EVIDENCE)
    assert report.quotes[0].verified is True


def test_german_quotation_marks_are_accepted():
    content = (
        "QUOTES:\n"
        "[K2] „Die Verpflegungspauschale betraegt 28 Euro pro Tag.“\n"
        "ANSWER:\n"
        "28 Euro [K2]."
    )
    report = check_grounding(content, EVIDENCE)
    assert report.quotes == [
        type(report.quotes[0])(
            label="K2",
            text="Die Verpflegungspauschale betraegt 28 Euro pro Tag.",
            verified=True,
        )
    ]


def test_smart_quotes_and_dashes_verify_against_ascii_source():
    # The PDF text-layer used curly quotes and an em dash; the model quoted the
    # ASCII forms. A genuinely verbatim quote must not fail on typography alone.
    evidence = ["[K1] X\nEr nannte “den Vertrag” — verbindlich."]
    content = (
        "QUOTES:\n"
        '[K1] "Er nannte "den Vertrag" - verbindlich."\n'
        "ANSWER:\nAntwort [K1]."
    )
    assert check_grounding(content, evidence).quotes[0].verified is True


def test_ligature_and_case_differences_verify():
    # NFKC folds the ffi ligature; case folds. Still a verbatim substring.
    evidence = ["[K1] X\nDie EFFIZIENTE Abwicklung."]
    content = (
        "QUOTES:\n"
        '[K1] "die eﬃziente abwicklung."\n'
        "ANSWER:\nAntwort [K1]."
    )
    assert check_grounding(content, evidence).quotes[0].verified is True


def test_paraphrase_is_visibly_unverified():
    content = (
        "QUOTES:\n"
        '[K1] "Die Haftung wurde vertraglich gedeckelt."\n'
        "ANSWER:\n"
        "Antwort [K1]."
    )
    report = check_grounding(content, EVIDENCE)
    assert report.marker == GROUNDING_MARKER_PARSED
    assert report.status is GroundingStatus.REJECTED_QUOTE
    assert report.failure_code is GroundingFailureCode.QUOTE_UNVERIFIED
    assert report.answer == ""
    assert report.publishable is False
    assert report.quotes[0].verified is False


def test_out_of_range_label_is_unverified():
    content = 'QUOTES:\n[K7] "Die Haftung"\nANSWER:\nAntwort.'
    report = check_grounding(content, EVIDENCE)
    assert report.quotes[0].label == "K7"
    assert report.quotes[0].verified is False
    assert report.status is GroundingStatus.REJECTED_QUOTE


def test_missing_answer_section_is_not_publishable():
    content = "Eine Antwort ohne das verlangte Format [K1]."
    report = check_grounding(content, EVIDENCE)
    assert report.marker == GROUNDING_MARKER_FALLBACK
    assert report.status is GroundingStatus.REJECTED_FORMAT
    assert report.failure_code is GroundingFailureCode.FORMAT_INVALID
    assert report.answer == ""
    assert report.quotes == []


def test_answer_section_without_quotes_is_not_publishable():
    content = "QUOTES:\n\nANSWER:\nNur die Antwort [K1]."
    report = check_grounding(content, EVIDENCE)
    assert report.marker == GROUNDING_MARKER_FALLBACK
    assert report.answer == ""
    assert report.status is GroundingStatus.REJECTED_FORMAT


def test_empty_answer_after_separator_is_a_visible_fallback():
    """Truncated completions must not masquerade as clean parses."""
    content = 'QUOTES:\n[K1] "Die Haftung"\nANSWER:\n'
    report = check_grounding(content, EVIDENCE)
    assert report.answer == ""
    assert report.marker == GROUNDING_MARKER_FALLBACK
    assert report.status is GroundingStatus.REJECTED_FORMAT
    assert report.quotes == []


def test_bold_separator_and_crlf_are_tolerated():
    content = (
        'QUOTES:\r\n[K1] "Die Haftung ist auf den Auftragswert begrenzt."\r\n'
        '**ANSWER:**\r\nDie Haftung ist begrenzt [K1].'
    )
    report = check_grounding(content, EVIDENCE)
    assert report.marker == GROUNDING_MARKER_PARSED
    assert report.quotes[0].verified is True
    assert report.answer == 'Die Haftung ist begrenzt [K1].' 


def test_missing_zitate_header_is_rejected_even_with_a_valid_quote_line():
    report = check_grounding(
        '[K1] "Die Haftung ist auf den Auftragswert begrenzt."\n'
        "ANSWER:\nAntwort [K1].",
        EVIDENCE,
    )

    assert report.status is GroundingStatus.REJECTED_FORMAT
    assert report.quotes == []
    assert report.answer == ""


def test_one_bounded_markdown_heading_repair_is_audited():
    report = check_grounding(
        "### QUOTES:\n"
        '[K1] "Die Haftung ist auf den Auftragswert begrenzt."\n'
        "### ANSWER:\nAntwort [K1].",
        EVIDENCE,
    )

    assert report.status is GroundingStatus.VERIFIED
    assert report.marker == GROUNDING_MARKER_FORMAT_REPAIRED
    assert report.format_repaired is True
    assert report.answer == "Antwort [K1]."


# ------------------------------------------------------------------ #
# Algorithm wiring
# ------------------------------------------------------------------ #


class GroundedLLM:
    """Answers with a fixed quote-then-answer shaped completion."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.prompts: list[str] = []

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return "ok"

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(
            content=self._content,
            prompt_tokens=42,
            completion_tokens=11,
            model="stub-answer",
            finish_reason="stop",
        )

    def is_available(self) -> bool:
        return True


CONTRACT_DOCUMENT = (
    "Rahmenvertrag",
    "Die Haftung ist auf den Auftragswert begrenzt.",
)
TRAVEL_DOCUMENT = (
    "Reiserichtlinie",
    "Die Haftung der Reisenden ist im Reisevertrag geregelt.",
)
# The extracted text broke a word at a line end, as PDF text layers do.
HYPHENATED_CONTRACT_DOCUMENT = (
    "Rahmenvertrag",
    "Die Haftung ist auf den Auftrags-\nwert begrenzt.",
)


def make_algorithm(
    llm: GroundedLLM,
    *,
    grounding_enabled: bool,
    seed: bool = True,
    documents: tuple[tuple[str, str], ...] = (CONTRACT_DOCUMENT,),
) -> tuple[KnowledgeAlgorithm, RunContext, RuntimeContext, list]:
    knowledge = KnowledgeProviderContext(
        embeddings=StubEmbeddings(),
        store=MemoryKnowledgeStore(),
        default_top_k=4,
    )
    service = KnowledgeService(
        knowledge=knowledge, chunk_max_chars=2_000, max_document_chars=100_000
    )
    if seed:
        async def _seed() -> None:
            collection = await service.create_collection(name="K")
            for title, text in documents:
                await service.add_document(
                    collection_id=collection.id, title=title, text=text
                )

        asyncio.run(_seed())
    algorithm = KnowledgeAlgorithm(
        knowledge=knowledge,
        gate_enabled=False,
        grounding_enabled=grounding_enabled,
    )
    settings = Settings(agent=AgentSettings())
    runtime = RuntimeContext(
        settings=settings,
        registry=None,
        providers=ProviderContext(llm=llm, search=StubSearch()),
        strategies=None,
    )
    events: list[tuple[str, dict]] = []
    context = RunContext(
        providers=runtime.providers,
        strategies=None,
        agent_settings=settings.agent,
        event_sink=lambda event, payload: events.append((event, payload)),
    )
    return algorithm, context, runtime, events


def run_question(algorithm, runtime, context):
    return algorithm.run(
        RunRequest(mode="knowledge", question="Wie ist die Haftung?"),
        runtime=runtime,
        context=context,
    )


def test_grounded_answer_is_verified_stripped_and_evented():
    llm = GroundedLLM(
        "QUOTES:\n"
        '[K1] "Die Haftung ist auf den Auftragswert begrenzt."\n'
        "\n"
        "ANSWER:\n"
        "Die Haftung ist auf den Auftragswert begrenzt [K1]."
    )
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )

    result = run_question(algorithm, runtime, context)

    assert result.answer == (
        "Die Haftung ist auf den Auftragswert begrenzt [K1]."
    )
    assert "QUOTES:" not in result.answer
    assert "QUOTES:" in llm.prompts[0]
    state = result.raw["result_state"]["knowledge_grounding"]
    assert state["marker"] == GROUNDING_MARKER_PARSED
    assert state["status"] == GroundingStatus.VERIFIED.value
    assert state["failure_code"] is None
    assert state["format_repaired"] is False
    assert state["quotes_total"] == 1
    assert state["quotes_verified"] == 1
    assert state["quotes"][0]["verified"] is True
    assert result.raw["result_state"]["answer"] == result.answer
    grounding_events = [
        payload
        for event, payload in events
        if event == "inqtrix.knowledge.grounding.checked"
    ]
    assert grounding_events == [
        {
            "marker": GROUNDING_MARKER_PARSED,
            "status": GroundingStatus.VERIFIED.value,
            "failure_code": None,
            "format_repaired": False,
            "quotes_total": 1,
            "quotes_verified": 1,
        }
    ]


def test_unverified_quote_fails_closed_with_safe_visible_reason(caplog):
    llm = GroundedLLM(
        "QUOTES:\n"
        '[K1] "Die Haftung wurde komplett ausgeschlossen."\n'
        "ANSWER:\n"
        "Antwort [K1]."
    )
    algorithm, context, runtime, _events = make_algorithm(
        llm, grounding_enabled=True
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        result = run_question(algorithm, runtime, context)

    assert "nicht veröffentlicht" in result.answer
    assert "Antwort [K1]." not in result.answer
    assert result.successful is False
    assert result.terminal_failure is not None
    assert (
        result.terminal_failure.type
        == GroundingFailureCode.QUOTE_UNVERIFIED.value
    )
    state = result.raw["result_state"]["knowledge_grounding"]
    assert state["quotes_verified"] == 0
    assert state["status"] == GroundingStatus.REJECTED_QUOTE.value
    assert (
        state["failure_code"]
        == GroundingFailureCode.QUOTE_UNVERIFIED.value
    )
    assert result.raw["result_state"]["answer"] == result.answer
    assert any("Knowledge-Grounding" in m for m in caplog.messages)


UNCLEAR_QUESTION = "Was verlangt Measure 7.6 im Code of Practice, Kapitel Safety and Security?"
"""A German frame around English titles: no clear language."""


def test_an_unclear_question_speaks_the_interface_language_throughout():
    """The interface language decides the directive and every reader text of
    the run, the refusal of an unverified answer included."""
    llm = GroundedLLM(
        "QUOTES:\n"
        '[K1] "Die Haftung wurde komplett ausgeschlossen."\n'
        "ANSWER:\n"
        "Antwort [K1]."
    )
    algorithm, context, runtime, _events = make_algorithm(llm, grounding_enabled=True)
    result = algorithm.run(
        RunRequest(mode="knowledge", question=UNCLEAR_QUESTION, ui_language="de"),
        runtime=runtime,
        context=context,
    )
    assert "Antworte in der Sprache der Frage" in llm.prompts[0]
    assert "ALWAYS respond in English" not in llm.prompts[0]
    assert "nicht veröffentlicht" in result.answer


def test_no_relevant_content_speaks_the_interface_language():
    from inqtrix.i18n import t

    llm = GroundedLLM("unused")
    algorithm, context, runtime, _events = make_algorithm(
        llm, grounding_enabled=True, seed=False,
    )
    result = algorithm.run(
        RunRequest(mode="knowledge", question=UNCLEAR_QUESTION, ui_language="de"),
        runtime=runtime,
        context=context,
    )
    assert result.answer == t({"language": "de"}, "knowledge_no_relevant_content")


def test_a_knowledge_run_decides_its_language_once():
    """Every reader text of a run takes the one resolved language; no message
    guesses it again from the question."""
    import ast
    import inspect

    from inqtrix.knowledge import algorithm as module

    called = [
        node.func.id for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert (called.count("resolve_answer_language"), "detect_ui_language" in called) == (1, False)


def test_unparseable_shape_fails_closed_loudly(caplog):
    llm = GroundedLLM("Antwort ohne Format [K1].")
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        result = run_question(algorithm, runtime, context)

    assert "nicht veröffentlicht" in result.answer
    assert "Antwort ohne Format [K1]." not in result.answer
    assert result.successful is False
    assert result.terminal_failure is not None
    assert (
        result.terminal_failure.type
        == GroundingFailureCode.FORMAT_INVALID.value
    )
    state = result.raw["result_state"]["knowledge_grounding"]
    assert state["marker"] == GROUNDING_MARKER_FALLBACK
    assert state["status"] == GroundingStatus.REJECTED_FORMAT.value
    assert any("knowledge_grounding_format_invalid" in m for m in caplog.messages)
    assert [
        (payload["marker"], payload["status"], payload["failure_code"])
        for event, payload in events
        if event == "inqtrix.knowledge.grounding.checked"
    ] == [
        (
            GROUNDING_MARKER_FALLBACK,
            GroundingStatus.REJECTED_FORMAT.value,
            GroundingFailureCode.FORMAT_INVALID.value,
        )
    ]


def test_grounding_off_restores_plain_prompt_and_passthrough():
    llm = GroundedLLM("Antwort mit Beleg [K1].")
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=False
    )

    result = run_question(algorithm, runtime, context)

    assert result.answer == "Antwort mit Beleg [K1]."
    assert "QUOTES:" not in llm.prompts[0]
    assert "AUSGABEFORMAT" not in llm.prompts[0]
    assert result.raw["result_state"]["knowledge_grounding"] == {
        "enabled": False
    }
    assert [
        reference["quotes"]
        for reference in result.raw["result_state"]["report_references"]
    ] == [[]]
    assert not any(
        event == "inqtrix.knowledge.grounding.checked"
        for event, _payload in events
    )


def test_refusal_path_records_no_grounding_check():
    llm = GroundedLLM("nie aufgerufen")
    algorithm, context, runtime, _events = make_algorithm(
        llm, grounding_enabled=True, seed=False
    )

    result = algorithm.run(
        RunRequest(mode="knowledge", question="?"),
        runtime=runtime,
        context=context,
    )

    assert "No relevant content" in result.answer
    assert llm.prompts == []
    assert result.raw["result_state"]["knowledge_grounding"] == {
        "enabled": True
    }


class LabelAwareLLM(GroundedLLM):
    """Quotes under the label the evidence block gave each document.

    Retrieval decides the ``K#`` order, so the stub reads it from the
    rendered ``[K#] <title> (Abschnitt N)`` lines instead of assuming it.
    """

    def __init__(self, write: Any) -> None:
        super().__init__("")
        self._write = write

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        labels = {
            title: f"K{index}"
            for index, title in re.findall(
                r"^\[K(\d+)\] (\S+) \(Abschnitt", prompt, re.MULTILINE
            )
        }
        self._content = self._write(labels)
        return super().complete_with_metadata(prompt, **kwargs)


def test_published_answer_carries_each_verified_quote_on_its_reference():
    # The verified quote travels with the citation it supports, so the reader
    # can badge the passage and highlight the quoted span instead of the
    # whole chunk. Every quote of a label stays, in the model's order; a
    # reference nobody quoted carries none.
    llm = LabelAwareLLM(
        lambda labels: (
            "QUOTES:\n"
            f'[{labels["Rahmenvertrag"]}] "Die Haftung ist"\n'
            f'[{labels["Rahmenvertrag"]}] "Auftragswert begrenzt"\n'
            "\n"
            "ANSWER:\n"
            f"Die Haftung ist begrenzt [{labels['Rahmenvertrag']}]."
        )
    )
    algorithm, context, runtime, _events = make_algorithm(
        llm,
        grounding_enabled=True,
        documents=(HYPHENATED_CONTRACT_DOCUMENT, TRAVEL_DOCUMENT),
    )

    result = run_question(algorithm, runtime, context)

    assert result.successful is True
    by_title = {
        reference["title"]: reference
        for reference in result.raw["result_state"]["report_references"]
    }
    assert set(by_title) == {"Rahmenvertrag", "Reiserichtlinie"}
    assert by_title["Rahmenvertrag"]["quotes"] == [
        {"text": "Die Haftung ist", "verified": True, "artifact_tolerated": False},
        # Verifies only across the broken word: the audit flag travels too.
        {
            "text": "Auftragswert begrenzt",
            "verified": True,
            "artifact_tolerated": True,
        },
    ]
    assert by_title["Reiserichtlinie"]["quotes"] == []


def test_rejected_answer_attaches_no_quote_to_any_reference():
    # A rejected draft is not published, and no part of it is: even its
    # verified quote stays in the internal audit state only.
    llm = GroundedLLM(
        "QUOTES:\n"
        '[K1] "Die Haftung ist auf den Auftragswert begrenzt."\n'
        '[K1] "Die Haftung wurde komplett ausgeschlossen."\n'
        "ANSWER:\n"
        "Antwort [K1]."
    )
    algorithm, context, runtime, _events = make_algorithm(
        llm, grounding_enabled=True
    )

    result = run_question(algorithm, runtime, context)

    assert result.successful is False
    state = result.raw["result_state"]
    assert [
        quote["verified"] for quote in state["knowledge_grounding"]["quotes"]
    ] == [True, False]
    assert [reference["quotes"] for reference in state["report_references"]] == [
        []
    ]


# ------------------------------------------------------------------ #
# Page-break artifact tolerance (second evidence surface)
# ------------------------------------------------------------------ #

# The evidenced production shape: pdfminer renders the printed page
# number and a form feed INTO a sentence spanning a page break.
PAGE_BREAK_EVIDENCE = [
    "[K1] Commission Guidelines (Abschnitt 21)\n"
    "that element should be understood to\n"
    "emphasise the fact that AI systems are not passive, but actively "
    "impact the environments\n\n11\n\n\x0cin which they are deployed. "
    "Reference to 'physical or virtual environments' indicates that\n"
    "the influence"
]


def test_page_break_artifact_quote_verifies_without_page_number():
    # Regression: the model quotes the sentence as a human reads it —
    # without the printed page number. Must verify.
    content = (
        "QUOTES:\n"
        '[K1] "AI systems are not passive, but actively impact the '
        'environments in which they are deployed."\n'
        "ANSWER:\n"
        "KI-Systeme wirken aktiv auf ihre Umgebung [K1]."
    )
    report = check_grounding(content, PAGE_BREAK_EVIDENCE)
    assert report.status is GroundingStatus.VERIFIED
    assert report.quotes[0].verified is True


def test_page_break_artifact_quote_with_page_number_still_verifies():
    # A model that faithfully includes the artifact stays valid (surface 1).
    content = (
        "QUOTES:\n"
        '[K1] "AI systems are not passive, but actively impact the '
        'environments 11 in which they are deployed."\n'
        "ANSWER:\n"
        "Antwort [K1]."
    )
    assert check_grounding(content, PAGE_BREAK_EVIDENCE).quotes[0].verified


def test_number_without_form_feed_stays_content():
    # "Article 11" protection: a number NOT anchored by a form feed is
    # content — a quote that drops it must keep failing.
    evidence = [
        "[K1] AI Act\nThe requirements of Article 11 in conjunction "
        "with Annex IV apply to providers."
    ]
    content = (
        "QUOTES:\n"
        '[K1] "The requirements of Article in conjunction with Annex IV '
        'apply to providers."\n'
        "ANSWER:\nAntwort [K1]."
    )
    assert check_grounding(content, evidence).quotes[0].verified is False


def test_paraphrase_still_fails_against_artifact_evidence():
    # The tolerance opens page-break artifacts, never paraphrase.
    content = (
        "QUOTES:\n"
        '[K1] "AI systems always change their environment actively."\n'
        "ANSWER:\nAntwort [K1]."
    )
    report = check_grounding(content, PAGE_BREAK_EVIDENCE)
    assert report.status is GroundingStatus.REJECTED_QUOTE
    assert report.failure_code is GroundingFailureCode.QUOTE_UNVERIFIED


def test_strip_page_break_artifacts_shapes_and_idempotency():
    cases = {
        # blank-line-framed page number before the form feed (the
        # corpus-evidenced dominant shape, 347x)
        "a\n\n11\n\n\x0cb": "a\nb",
        # bare form feed (10x)
        "a\n\x0cCONTENTS": "a\nCONTENTS",
        # a number AFTER the form feed is never touched
        "a\x0c12\nb": "a\n12\nb",
        # multiple breaks in one text
        "a\n\n1\n\n\x0cb\n\n2\n\n\x0cc": "a\nb\nc",
        # no form feed: byte-identical, numbers untouched
        "Article 11 applies.\n\n12\n\nNext.": (
            "Article 11 applies.\n\n12\n\nNext."
        ),
        # a digit that merely ends a wrapped text line (single newline,
        # no blank-line frame) is CONTENT and survives the strip
        "price rose from 1\n2\n\x0c3 percent overall": (
            "price rose from 1\n2\n3 percent overall"
        ),
    }
    for source, expected in cases.items():
        stripped = strip_page_break_artifacts(source)
        assert stripped == expected, repr(source)
        assert strip_page_break_artifacts(stripped) == stripped


def test_content_digit_at_page_break_is_never_elidable():
    # Review finding: a content number adjacent to the form feed without
    # the blank-line frame (wrapped amounts, column values) must not be
    # removable — a quote eliding it stays unverified.
    evidence = ["[K1] Report\nprice rose from 1\n2\n\x0c3 percent overall"]
    content = (
        "QUOTES:\n"
        '[K1] "price rose from 1 3 percent overall"\n'
        "ANSWER:\nAntwort [K1]."
    )
    assert check_grounding(content, evidence).quotes[0].verified is False
    evidence2 = [
        "[K1] Report\nThe penalty shall not exceed\n20\n\x0cmillion euros"
    ]
    content2 = (
        "QUOTES:\n"
        '[K1] "The penalty shall not exceed million euros"\n'
        "ANSWER:\nAntwort [K1]."
    )
    assert check_grounding(content2, evidence2).quotes[0].verified is False


def test_zero_width_only_quote_never_verifies():
    # Review finding: a quote that normalizes to nothing ('' is a
    # substring of everything) must fail, exactly as in quote_is_verbatim.
    content = "QUOTES:\n[K1] ​\nANSWER:\nAntwort [K1]."
    report = check_grounding(content, EVIDENCE)
    assert report.quotes[0].verified is False
    assert report.status is GroundingStatus.REJECTED_QUOTE


def test_quote_verifies_only_against_its_assigned_entry():
    # K2's text under a K1 label is false provenance — the check is
    # bound to the ASSIGNED entry, never any-entry search.
    content = (
        "QUOTES:\n"
        '[K1] "Die Verpflegungspauschale betraegt 28 Euro pro Tag."\n'
        "ANSWER:\nAntwort [K1]."
    )
    assert check_grounding(content, EVIDENCE).quotes[0].verified is False


def test_artifact_tolerated_flag_records_the_surface_used():
    clean_quote = (
        "QUOTES:\n"
        '[K1] "AI systems are not passive, but actively impact the '
        'environments in which they are deployed."\n'
        "ANSWER:\nAntwort [K1]."
    )
    tolerated = check_grounding(clean_quote, PAGE_BREAK_EVIDENCE).quotes[0]
    assert tolerated.verified is True
    assert tolerated.artifact_tolerated is True
    raw_quote = (
        "QUOTES:\n"
        '[K1] "Reference to \'physical or virtual environments\'"\n'
        "ANSWER:\nAntwort [K1]."
    )
    raw = check_grounding(raw_quote, PAGE_BREAK_EVIDENCE).quotes[0]
    assert raw.verified is True
    assert raw.artifact_tolerated is False


def test_quote_is_verbatim_covers_both_surfaces():
    # Direct pins for the helper the agent-side check reuses.
    evidence = PAGE_BREAK_EVIDENCE
    assert quote_is_verbatim(
        "actively impact the environments in which they are deployed",
        evidence,
    )
    assert quote_is_verbatim(
        "actively impact the environments 11 in which they are deployed",
        evidence,
    )
    assert not quote_is_verbatim("changes its environment", evidence)
    assert not quote_is_verbatim("", evidence)


# ------------------------------------------------------------------ #
# Visible one-shot answer regeneration
# ------------------------------------------------------------------ #


class QueuedGroundedLLM(GroundedLLM):
    """Pops one scripted completion per call — the regeneration seam."""

    def __init__(self, contents: list[str]) -> None:
        super().__init__(contents[0])
        self._queue = list(contents)

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.prompts.append(prompt)
        return LLMResponse(
            content=self._queue.pop(0),
            prompt_tokens=42,
            completion_tokens=11,
            model="stub-answer",
            finish_reason="stop",
        )


PARAPHRASED_ATTEMPT = (
    "QUOTES:\n"
    '[K1] "Die Haftung wurde komplett ausgeschlossen."\n'
    "ANSWER:\n"
    "Antwort [K1]."
)
VERBATIM_ATTEMPT = (
    "QUOTES:\n"
    '[K1] "Die Haftung ist auf den Auftragswert begrenzt."\n'
    "ANSWER:\n"
    "Die Haftung ist begrenzt [K1]."
)


def test_unverified_quote_triggers_one_visible_regeneration(caplog):
    llm = QueuedGroundedLLM([PARAPHRASED_ATTEMPT, VERBATIM_ATTEMPT])
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )
    # Order probe: snapshot how many retry events exist at the moment of
    # each model call — the retry must be announced BEFORE call 2 runs,
    # not emitted retroactively after it.
    retry_seen_at_call: list[int] = []
    original_complete = llm.complete_with_metadata

    def probed_complete(prompt: str, **kwargs: Any) -> LLMResponse:
        retry_seen_at_call.append(
            sum(
                1
                for event, _payload in events
                if event == "inqtrix.knowledge.answer.retry"
            )
        )
        return original_complete(prompt, **kwargs)

    llm.complete_with_metadata = probed_complete  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        result = run_question(algorithm, runtime, context)

    assert retry_seen_at_call == [0, 1]

    # Second attempt published; the run is a success, not a failure.
    assert result.successful is True
    assert result.terminal_failure is None
    assert result.answer == "Die Haftung ist begrenzt [K1]."
    # Exactly two model calls; the retry prompt names the failed quote.
    assert len(llm.prompts) == 2
    assert "CORRECTION:" in llm.prompts[1]
    assert "Die Haftung wurde komplett ausgeschlossen." in llm.prompts[1]
    assert "CORRECTION:" not in llm.prompts[0]
    # Both calls are real spend: usage is additive across attempts.
    assert result.raw["usage"]["prompt_tokens"] == 84
    assert result.raw["usage"]["completion_tokens"] == 22
    # Both attempts stay visible in the result state.
    state = result.raw["result_state"]["knowledge_grounding"]
    assert state["status"] == GroundingStatus.VERIFIED.value
    assert [a["status"] for a in state["attempts"]] == [
        GroundingStatus.REJECTED_QUOTE.value,
        GroundingStatus.VERIFIED.value,
    ]
    # The regeneration is announced: retry event with counters only,
    # exactly one final grounding.checked, and a log line.
    retry_events = [
        payload
        for event, payload in events
        if event == "inqtrix.knowledge.answer.retry"
    ]
    assert retry_events == [
        {"attempt": 2, "quotes_total": 1, "quotes_unverified": 1}
    ]
    checked = [
        payload
        for event, payload in events
        if event == "inqtrix.knowledge.grounding.checked"
    ]
    assert len(checked) == 1
    assert checked[0]["status"] == GroundingStatus.VERIFIED.value
    assert any("visible answer regeneration" in m for m in caplog.messages)


def test_both_attempts_unverified_keep_fail_closed_terminal(caplog):
    llm = QueuedGroundedLLM([PARAPHRASED_ATTEMPT, PARAPHRASED_ATTEMPT])
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        result = run_question(algorithm, runtime, context)

    assert len(llm.prompts) == 2
    assert "nicht veröffentlicht" in result.answer
    assert "Antwort [K1]." not in result.answer
    assert result.successful is False
    assert result.terminal_failure is not None
    assert (
        result.terminal_failure.type
        == GroundingFailureCode.QUOTE_UNVERIFIED.value
    )
    state = result.raw["result_state"]["knowledge_grounding"]
    assert state["status"] == GroundingStatus.REJECTED_QUOTE.value
    assert len(state["attempts"]) == 2
    assert (
        len(
            [
                1
                for event, _payload in events
                if event == "inqtrix.knowledge.grounding.checked"
            ]
        )
        == 1
    )
    assert any("Knowledge-Grounding rejected" in m for m in caplog.messages)


def test_verified_first_attempt_never_retries():
    llm = QueuedGroundedLLM([VERBATIM_ATTEMPT, "nie aufgerufen"])
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )

    result = run_question(algorithm, runtime, context)

    assert result.successful is True
    assert len(llm.prompts) == 1
    assert not any(
        event == "inqtrix.knowledge.answer.retry"
        for event, _payload in events
    )
    state = result.raw["result_state"]["knowledge_grounding"]
    assert [a["status"] for a in state["attempts"]] == [
        GroundingStatus.VERIFIED.value
    ]


def test_format_invalid_first_attempt_never_retries():
    # Only QUOTE_UNVERIFIED regenerates; format failures keep today's
    # immediate terminal behaviour.
    llm = QueuedGroundedLLM(["Antwort ohne Format [K1].", "nie aufgerufen"])
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )

    result = run_question(algorithm, runtime, context)

    assert len(llm.prompts) == 1
    assert result.successful is False
    assert (
        result.terminal_failure.type
        == GroundingFailureCode.FORMAT_INVALID.value
    )
    assert not any(
        event == "inqtrix.knowledge.answer.retry"
        for event, _payload in events
    )


def test_answer_retry_is_projected_and_forwarded_on_the_agent_path():
    # Review finding: deleting either allowlist entry kept the suite
    # green while silently hiding the retry from parent/mission streams.
    from inqtrix.runs.shared import _CHILD_PROJECTED_EVENTS

    assert "inqtrix.knowledge.answer.retry" in _CHILD_PROJECTED_EVENTS


def test_answer_retry_event_sanitizer_drops_quote_texts():
    # The event boundary carries counters only — a quote text smuggled
    # into the payload must not survive sanitization.
    sanitized = sanitize_event_payload(
        "inqtrix.knowledge.answer.retry",
        {
            "attempt": 2,
            "quotes_total": 3,
            "quotes_unverified": 1,
            "quote_text": "geheim",
            "quotes": [{"text": "geheim"}],
        },
    )
    assert sanitized == {
        "attempt": 2,
        "quotes_total": 3,
        "quotes_unverified": 1,
    }


def test_quotes_verify_across_line_break_hyphenation_of_the_source():
    """A PDF source reads "high- risk" and "decision- making" where the line
    broke; a model quotes the joined words, and a strict match fails the
    run with quote_unverified. Both readings of the broken word verify; a
    reworded quote still fails."""
    from inqtrix.knowledge.grounding import dehyphenate, quote_is_verbatim

    source = (
        "If Article 6(1) AI Act is not applicable, AI systems within the scope of the use "
        "cases in point 5(d) are classified as high- risk pursuant to Article 6(2). An AI "
        "system that does not materially influence the outcome of decision- making could be "
        "clas- sified differently."
    )
    assert quote_is_verbatim("are classified as high-risk pursuant to Article 6(2)", [source])
    assert quote_is_verbatim("the outcome of decision-making could be classified differently", [source])
    # The raw reading with the artifact still verifies too.
    assert quote_is_verbatim("classified as high- risk pursuant", [source])
    assert not quote_is_verbatim("are classified as low-risk pursuant to Article 6(2)", [source])
    assert dehyphenate("no artifact here-") == "no artifact here-"
    assert dehyphenate("high- risk and re-sign") == "highrisk and resign"
    # The answer-block contract tolerates the same artifact, marked as tolerated.
    from inqtrix.knowledge.grounding import GroundingStatus, check_grounding

    report = check_grounding(
        'QUOTES:\n[K1] "classified as high-risk pursuant to Article 6(2)"\nANSWER:\nHigh-risk [K1].',
        [source],
    )
    assert report.status is GroundingStatus.VERIFIED
    assert report.quotes[0].verified and report.quotes[0].artifact_tolerated
    rejected = check_grounding(
        'QUOTES:\n[K1] "classified as low-risk pursuant to Article 6(2)"\nANSWER:\nLow-risk [K1].',
        [source],
    )
    assert rejected.status is not GroundingStatus.VERIFIED


# ------------------------------------------------------------------ #
# Numbers are compared as written
# ------------------------------------------------------------------ #


def test_a_number_range_never_verifies_as_another_number():
    """A hyphen or dash between digits is content: "3-5" is a range, "35" a
    number. Neither reading verifies as the other, in either direction and
    whichever dash the source used; the range itself verifies with either
    dash."""
    hyphen_range = ["Die Meldung erfolgt innerhalb von 3-5 Tagen nach Eingang."]
    dash_range = ["Die Meldung erfolgt innerhalb von 3–5 Tagen nach Eingang."]
    assert not quote_is_verbatim("innerhalb von 35 Tagen", hyphen_range)
    assert not quote_is_verbatim("innerhalb von 35 Tagen", dash_range)
    assert not quote_is_verbatim(
        "innerhalb von 3-5 Tagen", ["Die Meldung erfolgt innerhalb von 35 Tagen."]
    )
    assert not quote_is_verbatim("Artikel 12 regeln", ["Artikel 1-2 regeln"])
    assert not quote_is_verbatim("Frist von 20242026", ["Frist von 2024-2026 gilt"])
    assert not quote_is_verbatim(
        "tritt am 20240512 in Kraft", ["tritt am 2024-05-12 in Kraft"]
    )
    assert not quote_is_verbatim("Folgen von COVID19", ["Folgen von COVID-19 für"])
    assert quote_is_verbatim("innerhalb von 3-5 Tagen", hyphen_range)
    assert quote_is_verbatim("innerhalb von 3-5 Tagen", dash_range)
    assert quote_is_verbatim("innerhalb von 3–5 Tagen", hyphen_range)
    assert quote_is_verbatim(
        "tritt am 2024-05-12 in Kraft", ["tritt am 2024-05-12 in Kraft"]
    )
    # A reworded range fails as before.
    assert not quote_is_verbatim(
        "Frist von 2024 bis 2026", ["Frist von 2024-2026 gilt"]
    )


def test_a_line_break_after_a_hyphen_next_to_a_digit_keeps_the_hyphen():
    """Hyphenation never splits a number, so a hyphen that ends a line next
    to a digit is a real one: the line break closes, the hyphen stays.
    "COVID-19" and "2024-2026" verify across the break, the joined digits
    never do."""
    assert dehyphenate("covid- 19, 2024- 2026, 3-5, high- risk") == (
        "covid-19, 2024-2026, 3-5, highrisk"
    )
    covid = ["Die Folgen von COVID-\n19 für den Arbeitsmarkt"]
    years = ["Die Frist von 2024-\n2026 gilt fort."]
    articles = ["Die Pflichten gemäß Art. 5-\n7 DSGVO gelten."]
    assert quote_is_verbatim("Folgen von COVID-19 für", covid)
    assert quote_is_verbatim("Frist von 2024-2026 gilt", years)
    assert quote_is_verbatim("gemäß Art. 5-7 DSGVO", articles)
    assert not quote_is_verbatim("Folgen von COVID19 für", covid)
    assert not quote_is_verbatim("Frist von 20242026 gilt", years)
    assert not quote_is_verbatim("gemäß Art. 57 DSGVO", articles)


def test_number_forms_are_compared_as_written():
    """Compatibility folding would read the exponent in "10²" as "102" and
    the footnote marker in "Artikel 5³" as "53". Superscript, subscript and
    fraction digits stay as written: a quote that keeps them verifies, a
    baseline or transliterated rewrite does not, and dropping a footnote
    marker is an elision like any other."""
    fee = ["Die Gebühr beträgt 10² Euro."]
    assert quote_is_verbatim("Die Gebühr beträgt 10² Euro", fee)
    assert not quote_is_verbatim("Die Gebühr beträgt 102 Euro", fee)
    assert not quote_is_verbatim("Die Gebühr beträgt 10^2 Euro", fee)
    footnoted = ["Nach Artikel 5³ gilt die Pflicht des Anbieters¹ fort."]
    assert quote_is_verbatim(
        "Nach Artikel 5³ gilt die Pflicht des Anbieters¹ fort", footnoted
    )
    assert not quote_is_verbatim("Nach Artikel 53 gilt", footnoted)
    assert not quote_is_verbatim("die Pflicht des Anbieters1 fort", footnoted)
    assert not quote_is_verbatim("die Pflicht des Anbieters fort", footnoted)
    measures = ["Der CO₂-Ausstoß von 1½ Tonnen je 80 m² Fläche."]
    assert quote_is_verbatim(
        "Der CO₂-Ausstoß von 1½ Tonnen je 80 m² Fläche", measures
    )
    assert not quote_is_verbatim("Der CO2-Ausstoß", measures)
    assert not quote_is_verbatim("von 11⁄2 Tonnen", measures)
    assert not quote_is_verbatim("je 80 m2 Fläche", measures)


def test_formatting_folds_outside_number_forms_still_verify():
    """Everything that is not a number form folds exactly as NFKC does:
    ligatures, no-break spaces (inside a number too), full-width letters
    and decomposed accents verify against their plain spelling, in both
    directions."""
    assert quote_is_verbatim("die effiziente Abwicklung", ["Die eﬃziente Abwicklung"])
    assert quote_is_verbatim("Die eﬃziente Abwicklung", ["die effiziente Abwicklung"])
    assert quote_is_verbatim("beträgt 10 000 Euro", ["beträgt 10 000 Euro"])
    assert quote_is_verbatim("das Modell ABC", ["das Modell ＡＢＣ"])
    assert quote_is_verbatim("Café Größe", ["Café Größe"])
    assert quote_is_verbatim("Café Größe", ["Café Größe"])


def test_dash_variants_and_line_breaks_are_one_passage_everywhere():
    """Quote, evidence and dedup key meet in one comparison form: the same
    passage typed with another dash or broken at a line end is one passage
    for quote_key, quote_is_verbatim and the grounding gate alike, while
    two different numbers never share a key (a distinct second finding
    would otherwise be dropped as a repeat)."""
    assert quote_key("high–risk systems") == quote_key("high-risk systems")
    assert quote_key("Frist von 3–5 Tagen") == quote_key("Frist von 3-5 Tagen")
    assert quote_key("COVID-\n19 Folgen") == quote_key("COVID-19 Folgen")
    assert quote_key("clas- sified as high- risk") == quote_key(
        "classified as high-risk"
    )
    assert quote_key("Frist von 3-5 Tagen") != quote_key("Frist von 35 Tagen")
    assert quote_key("Gebühr von 10² Euro") != quote_key("Gebühr von 102 Euro")
    broken = ["Only high- risk systems need a conformity assessment."]
    assert quote_is_verbatim("Only high–risk systems need", broken)
    report = check_grounding(
        'QUOTES:\n[K1] "Only high–risk systems need"\nANSWER:\nHigh-risk only [K1].',
        broken,
    )
    assert report.status is GroundingStatus.VERIFIED
    assert report.quotes[0].artifact_tolerated is True


def test_grounding_gate_rejects_a_quote_that_joins_a_number_range():
    """The answer-block gate shares the number rules: a range broken at a
    line end verifies as the range, never as the number its digits form."""
    evidence = [
        "[K1] Meldepflicht (Abschnitt 2)\n"
        "Die Meldung erfolgt innerhalb von 3-\n5 Tagen nach Eingang."
    ]
    joined = check_grounding(
        'QUOTES:\n[K1] "innerhalb von 35 Tagen"\nANSWER:\nFünfunddreißig Tage [K1].',
        evidence,
    )
    assert joined.status is GroundingStatus.REJECTED_QUOTE
    assert joined.failure_code is GroundingFailureCode.QUOTE_UNVERIFIED
    faithful = check_grounding(
        'QUOTES:\n[K1] "innerhalb von 3-5 Tagen"\nANSWER:\nDrei bis fünf Tage [K1].',
        evidence,
    )
    assert faithful.status is GroundingStatus.VERIFIED
    assert faithful.quotes[0].artifact_tolerated is True


# ------------------------------------------------------------------ #
# Incomplete completions are provider outcomes, not grounding verdicts
# ------------------------------------------------------------------ #


class ScriptedResponseLLM(GroundedLLM):
    """Pops one scripted ``LLMResponse`` per call — stop signal and all."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(responses[0].content)
        self._responses = list(responses)

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.prompts.append(prompt)
        return self._responses.pop(0)


def test_empty_completion_with_a_length_stop_is_incomplete_not_a_format_failure(
    caplog,
):
    # Live case: the answer model returned content "" with finish_reason
    # "length" after 306 reasoning tokens (the backend's content filter
    # errored); the grounding gate then blamed the output contract.
    llm = ScriptedResponseLLM(
        [
            LLMResponse(
                content="",
                prompt_tokens=6768,
                completion_tokens=306,
                reasoning_tokens=306,
                model="stub-answer",
                finish_reason="length",
                request_max_tokens=64000,
            )
        ]
    )
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        result = run_question(algorithm, runtime, context)

    assert len(llm.prompts) == 1
    assert result.successful is False
    assert result.terminal_failure.type == KNOWLEDGE_ANSWER_INCOMPLETE
    assert result.terminal_failure.type != (
        GroundingFailureCode.FORMAT_INVALID.value
    )
    assert "length" in result.terminal_failure.message
    assert result.answer == result.terminal_failure.message
    # The failed call is real spend: its usage stays booked.
    assert result.raw["usage"]["prompt_tokens"] == 6768
    assert result.raw["usage"]["completion_tokens"] == 306
    assert not any(
        event
        in ("inqtrix.knowledge.answer.retry", "inqtrix.knowledge.grounding.checked")
        for event, _payload in events
    )
    assert any(
        "knowledge answer incomplete" in m and "finish_reason=length" in m
        for m in caplog.messages
    )


def test_blank_completion_never_publishes_an_empty_answer_without_grounding():
    llm = ScriptedResponseLLM(
        [
            LLMResponse(
                content="  \n",
                prompt_tokens=10,
                completion_tokens=1,
                model="stub-answer",
                finish_reason="stop",
            )
        ]
    )
    algorithm, context, runtime, _events = make_algorithm(
        llm, grounding_enabled=False
    )

    result = run_question(algorithm, runtime, context)

    assert result.successful is False
    assert result.terminal_failure.type == KNOWLEDGE_ANSWER_INCOMPLETE
    assert "empty" in result.terminal_failure.message
    assert result.answer.strip()


def test_an_incomplete_regeneration_keeps_the_first_attempts_grounding_verdict():
    # Same rule as a regeneration call that fails: attempt 1's typed
    # grounding rejection is the terminal outcome, both calls are booked.
    llm = ScriptedResponseLLM(
        [
            LLMResponse(
                content=PARAPHRASED_ATTEMPT,
                prompt_tokens=42,
                completion_tokens=11,
                model="stub-answer",
                finish_reason="stop",
            ),
            LLMResponse(
                content="",
                prompt_tokens=40,
                completion_tokens=5,
                model="stub-answer",
                finish_reason="length",
            ),
        ]
    )
    algorithm, context, runtime, events = make_algorithm(
        llm, grounding_enabled=True
    )

    result = run_question(algorithm, runtime, context)

    assert len(llm.prompts) == 2
    assert result.successful is False
    assert result.terminal_failure.type == (
        GroundingFailureCode.QUOTE_UNVERIFIED.value
    )
    assert result.raw["usage"]["prompt_tokens"] == 82
    assert result.raw["usage"]["completion_tokens"] == 16
    assert [
        event for event, _payload in events
        if event == "inqtrix.knowledge.answer.retry"
    ] == ["inqtrix.knowledge.answer.retry"]


def test_incomplete_finish_reasons_have_one_definition():
    from inqtrix.knowledge import contextualize
    from inqtrix.providers.base import INCOMPLETE_FINISH_REASONS
    from inqtrix.strategies import _claim_extraction

    assert {"length", "content_filter"} <= INCOMPLETE_FINISH_REASONS
    assert contextualize.INCOMPLETE_FINISH_REASONS is INCOMPLETE_FINISH_REASONS
    assert (
        _claim_extraction.INCOMPLETE_FINISH_REASONS is INCOMPLETE_FINISH_REASONS
    )
