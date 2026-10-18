"""Tests for editor paragraph-rewrite helpers and the HTTP route."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from inqtrix.exceptions import AgentStructuredOutputError
from inqtrix.providers.base import ProviderContext, StructuredLLMResponse
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.app import create_app
from inqtrix.server.editor_suggestions import (
    EditorSuggestError,
    EditorSuggestRequestData,
    build_editor_suggest_prompt,
    clamp_background,
    parse_editor_suggest_payload,
    parse_editor_suggest_response,
    result_from_parsed,
    validate_editor_suggest_result,
    warnings_for_validation_issues,
)
from inqtrix.server.reference_documents import (
    ReferenceDocument,
    render_reference_documents,
)
from inqtrix.settings import AgentSettings, ModelSettings, ServerSettings, Settings

_VALID_JSON = '{"rewritten_text":"Die Arbeitsgruppe erweitert das Sortiment.","changes":["aktiv"]}'


class _CapturingLLM:
    def __init__(
        self,
        *,
        response: str | list[str] = _VALID_JSON,
        structured: bool = False,
        models: ModelSettings | None = None,
    ) -> None:
        self.prompt: str | None = None
        self.prompts: list[str] = []
        self.kwargs: dict[str, object] = {}
        self.structured_called = False
        self.responses = [response] if isinstance(response, str) else list(response)
        self._structured = structured
        if models is not None:
            self.models = models

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return self._structured

    def complete(self, prompt: str, **kwargs: object) -> str:
        self.prompt = prompt
        self.prompts.append(prompt)
        self.kwargs = kwargs
        return self._next_response()

    def complete_structured(self, prompt: str, **kwargs: object) -> StructuredLLMResponse:
        self.prompt = prompt
        self.prompts.append(prompt)
        self.kwargs = kwargs
        self.structured_called = True
        import json

        return StructuredLLMResponse(parsed=json.loads(self._next_response()))

    def is_available(self) -> bool:
        return True

    def _next_response(self) -> str:
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class _DummySearch:
    def search(self, *args: object, **kwargs: object) -> GroundedSearchResult:
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


def _make_client(
    *,
    llm: _CapturingLLM | None = None,
    server_settings: ServerSettings | None = None,
) -> tuple[TestClient, _CapturingLLM]:
    active_llm = llm or _CapturingLLM()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=server_settings or ServerSettings(),
    )
    app = create_app(
        settings=settings,
        providers=ProviderContext(llm=active_llm, search=_DummySearch()),
    )
    return TestClient(app), active_llm


class _InqtrixWarnings(logging.Handler):
    """Collects the ``inqtrix`` logger's records directly: the app's logging
    setup stops that logger from propagating to pytest's capture."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def __enter__(self) -> "_InqtrixWarnings":
        logging.getLogger("inqtrix").addHandler(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        logging.getLogger("inqtrix").removeHandler(self)


def _direkt_request(**overrides: object) -> EditorSuggestRequestData:
    base = {
        "block_text": "Es wurde beschlossen, das Sortiment zu erweitern.",
        "block_markdown": "",
        "background": "# Bericht\nHintergrundtext.",
        "instruction": "Mach den Satz aktiver.",
        "global_instruction": "",
        "current_suggestion_markdown": "",
        "refinement_instruction": "",
        "snippet": "",
        "locale": "de",
    }
    base.update(overrides)
    return EditorSuggestRequestData(**base)  # type: ignore[arg-type]


# -- prompt building ------------------------------------------------------- #


def test_direkt_prompt_has_english_rules_and_language_preservation() -> None:
    prompt = build_editor_suggest_prompt(_direkt_request())

    assert "rewrite EXACTLY ONE highlighted passage" in prompt
    assert "write \"rewritten_text\" in that same language" in prompt
    assert "interface language: German" in prompt
    assert "<paragraph_instruction>" in prompt
    assert "<rewrite>" in prompt and "Es wurde beschlossen" in prompt
    # No global/template SECTIONS when not provided (the rules text still
    # references the tag names, hence the trailing newline check).
    assert "<global_instruction>\n" not in prompt
    assert "<template>\n" not in prompt


def test_prompt_prioritizes_comment_before_background_for_long_passage() -> None:
    block = (
        "Der Ursprung der Verwechslung liegt vermutlich in der schnellen "
        "Ausdifferenzierung der Google-KI-Architektur, bei der mehrere Marken "
        "und Modellnamen parallel verwendet werden."
    )
    prompt = build_editor_suggest_prompt(
        _direkt_request(
            background="# Report\n" + ("Kontext. " * 400),
            block_text=block,
            instruction="Der Text ist viel zu lang, kuerze den Absatz zu einem Satz runter!",
        )
    )

    assert prompt.index("\n\n<paragraph_instruction>") < prompt.index("\n\n<rewrite>")
    assert prompt.index("\n\n<rewrite>") < prompt.index("\n\n<background>")
    assert "mandatory, not optional commentary" in prompt


_REFERENCE_NOTE = "You are also given the following reference documents"


def test_prompt_places_reference_block_after_rewrite_before_background() -> None:
    block = render_reference_documents(
        [ReferenceDocument(label="alpha", content="Paris.", page_count=None, size_bytes=None)]
    )
    prompt = build_editor_suggest_prompt(_direkt_request(), reference_block=block)

    assert prompt.index("<rewrite>\n") < prompt.index(_REFERENCE_NOTE)
    assert prompt.index(_REFERENCE_NOTE) < prompt.index("<background>\n")
    assert "[1] alpha" in prompt


def test_prompt_without_attachments_omits_reference_block() -> None:
    prompt = build_editor_suggest_prompt(_direkt_request())

    assert _REFERENCE_NOTE not in prompt


def test_system_rules_describe_reference_documents_as_non_authoritative() -> None:
    prompt = build_editor_suggest_prompt(_direkt_request())

    assert "<reference_documents> block, when present, is citable source material only" in prompt


def test_route_includes_attachments_in_prompt() -> None:
    client, llm = _make_client()

    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "Es wurde beschlossen, das Sortiment zu erweitern.",
            "instruction": "Nutze Fakten aus [1].",
            "locale": "de",
            "attachments": [
                {"label": "alpha", "content": "Paris ist die Hauptstadt.", "page_count": 4}
            ],
        },
    )

    assert response.status_code == 200
    assert llm.prompt is not None
    assert "<reference_documents>" in llm.prompt
    assert "[1] alpha (pages: 4)" in llm.prompt


def test_prompt_uses_markdown_rewrite_when_available() -> None:
    prompt = build_editor_suggest_prompt(
        _direkt_request(
            block_text="Google Gemini bleibt aktiv E10.",
            block_markdown="**Google Gemini** bleibt aktiv [E10](https://example.test/e10).",
            instruction="Kuerze den Satz, aber erhalte Link und Hervorhebung.",
        )
    )

    assert "<rewrite>\n**Google Gemini** bleibt aktiv [E10](https://example.test/e10)." in prompt
    assert "<rewrite_plaintext>\nGoogle Gemini bleibt aktiv E10." in prompt
    assert "Preserve meaning, technical terms, proper nouns, numbers, quotes, Markdown formatting, Markdown links" in prompt


def test_sammeln_prompt_includes_global_instruction_and_template() -> None:
    prompt = build_editor_suggest_prompt(
        _direkt_request(
            global_instruction="Formuliere durchgaengig auf Sie um.",
            snippet="Sachlicher Behoerdenstil.",
            locale="en",
        )
    )

    assert "<global_instruction>" in prompt and "auf Sie um" in prompt
    assert "<template>" in prompt and "Behoerdenstil" in prompt
    assert "interface language: English" in prompt


def test_refinement_prompt_uses_current_suggestion_as_revision_target() -> None:
    prompt = build_editor_suggest_prompt(
        _direkt_request(
            block_text="Der urspruengliche Absatz enthaelt drei Details.",
            current_suggestion_markdown="Der Absatz enthaelt drei Details und bleibt zu lang.",
            refinement_instruction="Noch knapper, aber die drei Details behalten.",
        )
    )

    assert "<rewrite>\nDer urspruengliche Absatz enthaelt drei Details." in prompt
    assert "<current_suggestion>\nDer Absatz enthaelt drei Details und bleibt zu lang." in prompt
    assert "<refinement_instruction>\nNoch knapper" in prompt
    assert "revise the current suggestion instead of starting over" in prompt


# -- request parsing ------------------------------------------------------- #


def test_parse_payload_allows_refinement_without_original_instruction() -> None:
    request = parse_editor_suggest_payload(
        {
            "block_text": "Der markierte Absatz enthaelt die Faktenbasis.",
            "current_suggestion_markdown": "Der aktuelle Vorschlag ist noch zu lang.",
            "refinement_instruction": "Kuerze ihn auf einen Satz.",
            "locale": "de",
        },
        max_background_chars=10_000,
        max_text_chars=2_000,
    )

    assert request.instruction == ""
    assert request.current_suggestion_markdown == "Der aktuelle Vorschlag ist noch zu lang."
    assert request.refinement_instruction == "Kuerze ihn auf einen Satz."


def test_parse_payload_requires_refinement_fields_as_pair() -> None:
    base = {"block_text": "Ein Absatz.", "locale": "de"}

    with pytest.raises(ValueError, match="refinement_instruction must be provided"):
        parse_editor_suggest_payload(
            {**base, "current_suggestion_markdown": "Vorschlag."},
            max_background_chars=10_000,
            max_text_chars=2_000,
        )

    with pytest.raises(ValueError, match="current_suggestion_markdown must be provided"):
        parse_editor_suggest_payload(
            {**base, "refinement_instruction": "Noch knapper."},
            max_background_chars=10_000,
            max_text_chars=2_000,
        )


# -- response parsing ------------------------------------------------------ #


def test_parse_response_accepts_clean_json() -> None:
    result = parse_editor_suggest_response(_VALID_JSON)
    assert result.rewritten_text == "Die Arbeitsgruppe erweitert das Sortiment."
    assert result.changes == ["aktiv"]
    assert result.warnings == []


def test_parse_response_extracts_json_with_prose_and_warns() -> None:
    result = parse_editor_suggest_response(
        'Sure, here is the result:\n' + _VALID_JSON + '\nHope it helps!'
    )
    assert result.rewritten_text == "Die Arbeitsgruppe erweitert das Sortiment."
    assert any("extra text" in w for w in result.warnings)


def test_parse_response_rejects_non_json() -> None:
    with pytest.raises(EditorSuggestError):
        parse_editor_suggest_response("not json at all")


def test_parse_response_rejects_empty_rewritten_text() -> None:
    with pytest.raises(EditorSuggestError):
        parse_editor_suggest_response('{"rewritten_text":"  ","changes":[]}')


def test_result_from_parsed_requires_rewritten_text() -> None:
    with pytest.raises(EditorSuggestError):
        result_from_parsed({"changes": ["x"]})


def test_validation_warnings_are_human_readable_without_raw_codes() -> None:
    request = _direkt_request(
        block_text="Der Originalabsatz war sehr lang und enthaelt mehrere Details.",
        current_suggestion_markdown="Der Vorschlag ist noch immer laenger als gewuenscht.",
        refinement_instruction="Formuliere den Vorschlag neutraler.",
    )
    result = result_from_parsed({
        "rewritten_text": "Der Vorschlag ist noch immer laenger als gewuenscht.",
        "changes": [],
    })

    issues = validate_editor_suggest_result(request, result)
    # The validator still flags it (it drives the retry and is logged) ...
    assert [issue.code for issue in issues] == ["unchanged"]
    # ... but the user never sees the raw code; the warning is plain language.
    assert warnings_for_validation_issues(issues, locale="de") == [
        "Der Vorschlag entspricht unverändert dem markierten Text."
    ]
    assert warnings_for_validation_issues(issues, locale="en") == [
        "The suggestion is unchanged from the selected text."
    ]


def test_validate_refinement_compares_against_current_suggestion() -> None:
    """A refinement revises the current suggestion, so "unchanged" means equal
    to that suggestion, not to the original passage."""
    request = _direkt_request(
        block_text="Der Originalabsatz war sehr lang und enthaelt mehrere Details.",
        current_suggestion_markdown="Der Vorschlag ist noch immer laenger als gewuenscht.",
        refinement_instruction="Formuliere den Vorschlag neutraler.",
    )
    unchanged = result_from_parsed({
        "rewritten_text": "Der Vorschlag ist noch immer laenger als gewuenscht.",
        "changes": [],
    })
    back_to_original = result_from_parsed({
        "rewritten_text": "Der Originalabsatz war sehr lang und enthaelt mehrere Details.",
        "changes": [],
    })

    assert [issue.code for issue in validate_editor_suggest_result(request, unchanged)] == [
        "unchanged",
    ]
    assert validate_editor_suggest_result(request, back_to_original) == []


def test_an_instruction_for_a_short_own_sentence_is_not_read_as_a_shortening_request() -> None:
    """Code cannot read the meaning of a free-text instruction: "kurzen" in
    "als eigenen kurzen Satz" asks for a sentence of its own, and a keyword
    gate rejected the correct answer for not being shorter."""
    request = _direkt_request(
        block_text="das alte Passwort danach nicht mehr gilt",
        instruction="Diesen Teil als eigenen kurzen Satz formulieren.",
    )
    result = result_from_parsed({
        "rewritten_text": "Das alte Passwort gilt danach nicht mehr",
        "changes": ["als eigener Satz formuliert"],
    })

    assert validate_editor_suggest_result(request, result) == []


def test_a_negated_shortening_instruction_raises_no_issue() -> None:
    request = _direkt_request(
        block_text="Die Arbeitsgruppe hat das Sortimet erweitert.",
        instruction="Nicht kürzen, nur Tippfehler korrigieren.",
    )
    result = result_from_parsed({
        "rewritten_text": "Die Arbeitsgruppe hat das Sortiment erweitert.",
        "changes": ["Tippfehler korrigiert"],
    })

    assert validate_editor_suggest_result(request, result) == []


def test_an_instruction_that_adds_one_sentence_sets_no_sentence_limit() -> None:
    """"Ergänze einen Satz" asks for one MORE sentence; a keyword gate read it
    as "at most one sentence" and pushed the retry to condense the paragraph."""
    request = _direkt_request(
        block_text="Die Frist endet am Freitag.",
        instruction="Ergänze einen Satz zur Frist.",
    )
    result = result_from_parsed({
        "rewritten_text": "Die Frist endet am Freitag. Danach ist keine Abgabe mehr möglich.",
        "changes": ["Satz zur Frist ergänzt"],
    })

    assert validate_editor_suggest_result(request, result) == []


# -- the passage in its sentence ------------------------------------------- #


_PROMPT_WITHOUT_CONTEXT_SHA256 = (
    "ef62f530fb63f79b4929e49f50f7a810797a9bf1c1b5b03bb896919be53a13e3"
)
_REFINEMENT_PROMPT_WITHOUT_CONTEXT_SHA256 = (
    "4006556277a7edcf13b509aba61a4b8030e2d30e5bfdd6521d2da90a541c3413"
)


def test_without_context_the_prompt_is_byte_identical_to_the_pinned_prompt() -> None:
    import hashlib

    plain = build_editor_suggest_prompt(_direkt_request())
    refinement = build_editor_suggest_prompt(
        _direkt_request(
            block_markdown="Es wurde **beschlossen**, das Sortiment zu erweitern.",
            current_suggestion_markdown="Wir erweitern das Sortiment.",
            refinement_instruction="Noch aktiver.",
        )
    )

    assert hashlib.sha256(plain.encode()).hexdigest() == _PROMPT_WITHOUT_CONTEXT_SHA256
    assert (
        hashlib.sha256(refinement.encode()).hexdigest()
        == _REFINEMENT_PROMPT_WITHOUT_CONTEXT_SHA256
    )


def test_the_prompt_shows_the_passage_in_its_sentence_when_context_is_given() -> None:
    request = _direkt_request(
        block_text="das alte Passwort gilt danach nicht mehr",
        instruction="Diesen Teil als eigenen kurzen Satz formulieren.",
        context_before="Nach der Änderung muss die Sitzung neu gestartet werden, ",
        context_after=". Bei Fragen hilft der Support weiter.",
    )

    prompt = build_editor_suggest_prompt(request)

    assert (
        "<passage_in_sentence>\n"
        "…Nach der Änderung muss die Sitzung neu gestartet werden, "
        "⟦PASSAGE⟧. Bei Fragen hilft der Support weiter.…\n"
        "</passage_in_sentence>"
    ) in prompt
    # The rule travels with the section, so the plain prompt stays unchanged.
    assert "do not repeat punctuation that already follows the passage" in prompt
    assert prompt.index("</rewrite>") < prompt.index("<passage_in_sentence>")


@pytest.mark.parametrize(
    ("rewritten", "context_after", "expected"),
    [
        ("Das gilt nicht mehr.", ". Bei Fragen", "Das gilt nicht mehr"),
        ("erst prüfen,", ", dann senden", "erst prüfen"),
        ("erst prüfen;", "; dann senden", "erst prüfen"),
        ("Liste:", ": eins, zwei", "Liste"),
        ("Das gilt nicht mehr.", "\u2026 und so weiter", "Das gilt nicht mehr"),
        # Not directly after the passage: nothing doubles.
        ("Das gilt nicht mehr.", " . Bei Fragen", "Das gilt nicht mehr."),
        # A different mark is the writer's choice, not a doubling.
        ("Gilt das noch?", ". Bei Fragen", "Gilt das noch?"),
        ("Achtung!", ". Bei Fragen", "Achtung!"),
        ("erst prüfen:", ", dann senden", "erst prüfen:"),
        ("ohne Satzzeichen", ". Bei Fragen", "ohne Satzzeichen"),
        ("Das gilt nicht mehr.", "", "Das gilt nicht mehr."),
    ],
)
def test_fit_rewrite_removes_only_a_doubled_mark_at_the_right_seam(
    rewritten: str, context_after: str, expected: str
) -> None:
    from inqtrix.server.editor_suggestions import fit_rewrite_to_following_text

    request = _direkt_request(context_after=context_after)
    result = result_from_parsed({"rewritten_text": rewritten, "changes": []})

    assert fit_rewrite_to_following_text(request, result).rewritten_text == expected


def test_parse_payload_refuses_an_overlong_context_rather_than_cutting_it() -> None:
    with pytest.raises(ValueError, match="context_after exceeds the maximum length"):
        parse_editor_suggest_payload(
            {
                "block_text": "Ein Absatz.",
                "instruction": "Mach den Satz aktiver.",
                "context_after": "x" * 401,
            },
            max_text_chars=10_000,
            max_background_chars=400_000,
        )
    with pytest.raises(ValueError, match="context_before must be a string"):
        parse_editor_suggest_payload(
            {
                "block_text": "Ein Absatz.",
                "instruction": "Mach den Satz aktiver.",
                "context_before": ["not", "text"],
            },
            max_text_chars=10_000,
            max_background_chars=400_000,
        )


def test_parse_payload_keeps_the_context_exactly_as_sent() -> None:
    """Whether punctuation follows the passage DIRECTLY is the signal the seam
    repair reads, so the context is never trimmed."""
    parsed = parse_editor_suggest_payload(
        {
            "block_text": "Ein Absatz.",
            "instruction": "Mach den Satz aktiver.",
            "context_before": "Davor, ",
            "context_after": " danach.",
        },
        max_text_chars=10_000,
        max_background_chars=400_000,
    )

    assert parsed.context_before == "Davor, "
    assert parsed.context_after == " danach."


# -- background windowing -------------------------------------------------- #


def test_clamp_background_keeps_short_report() -> None:
    report = "# Title\nshort body"
    out, truncated = clamp_background(report, "short body", max_chars=1000)
    assert out == report
    assert truncated is False


def test_clamp_background_windows_around_block_and_keeps_headings() -> None:
    block = "TARGET PARAGRAPH"
    report = "# Heading A\n" + ("filler. " * 2000) + block + (" trailing. " * 2000)
    out, truncated = clamp_background(report, block, max_chars=400)
    assert truncated is True
    assert block in out
    assert "# Heading A" in out
    assert len(out) <= 400 + 64


# -- HTTP route ------------------------------------------------------------ #


def test_route_prompt_json_path_returns_rewrite() -> None:
    client, llm = _make_client()
    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "Es wurde beschlossen, das Sortiment zu erweitern.",
            "background": "# Bericht\nKontext.",
            "instruction": "Mach den Satz aktiver.",
            "locale": "de",
        },
    )
    assert response.status_code == 200
    assert response.json()["improved_text"] == "Die Arbeitsgruppe erweitert das Sortiment."
    assert llm.prompt is not None and "<rewrite>" in llm.prompt
    assert llm.kwargs["max_output_tokens"] == 4000


def test_route_retries_when_the_model_returns_the_text_unchanged() -> None:
    block = "Es wurde beschlossen, das Sortiment zu erweitern."
    unchanged = '{"rewritten_text":"' + block + '","changes":[]}'
    valid = '{"rewritten_text":"Die Arbeitsgruppe erweitert das Sortiment.","changes":["aktiv"]}'
    client, llm = _make_client(llm=_CapturingLLM(response=[unchanged, valid]))

    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": block,
            "instruction": "Mach den Satz aktiver.",
            "locale": "de",
        },
    )

    assert response.status_code == 200
    assert response.json()["improved_text"] == "Die Arbeitsgruppe erweitert das Sortiment."
    assert len(llm.prompts) == 2
    assert "<validation_feedback>" in llm.prompts[1]
    assert "- unchanged:" in llm.prompts[1]


def test_route_returns_the_first_correct_answer_without_a_retry() -> None:
    """The live failure: the correct first answer was rejected as "not
    shortened", and the retry asked for something shorter, which produced a
    fragment that landed in the document."""
    sentence = '{"rewritten_text":"Das alte Passwort gilt danach nicht mehr","changes":["eigener Satz"]}'
    fragment = '{"rewritten_text":"altes Passwort ungültig","changes":["gekürzt"]}'
    client, llm = _make_client(llm=_CapturingLLM(response=[sentence, fragment]))

    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "das alte Passwort danach nicht mehr gilt",
            "instruction": "Diesen Teil als eigenen kurzen Satz formulieren.",
            "locale": "de",
        },
    )

    assert response.status_code == 200
    assert response.json()["improved_text"] == "Das alte Passwort gilt danach nicht mehr"
    assert len(llm.prompts) == 1


def test_route_drops_punctuation_the_passage_is_already_followed_by() -> None:
    """The selection ends before the sentence's full stop; a rewrite that
    ends with its own full stop would put ".." into the document."""
    rewrite = '{"rewritten_text":"Das alte Passwort gilt danach nicht mehr.","changes":["eigener Satz"]}'
    client, llm = _make_client(llm=_CapturingLLM(response=rewrite))

    with _InqtrixWarnings() as warnings:
        response = client.post(
            "/v1/editor/suggest",
            json={
                "block_text": "das alte Passwort danach nicht mehr gilt",
                "instruction": "Diesen Teil als eigenen kurzen Satz formulieren.",
                "context_before": "Nach der Änderung muss die Sitzung neu gestartet werden, ",
                "context_after": ". Bei Fragen hilft der Support weiter.",
                "locale": "de",
            },
        )

    assert response.status_code == 200
    assert response.json()["improved_text"] == "Das alte Passwort gilt danach nicht mehr"
    assert len(llm.prompts) == 1
    assert "<passage_in_sentence>" in llm.prompts[0]
    assert any(
        "repeated the punctuation that follows the passage" in message
        for message in warnings.messages
    )


def test_route_keeps_punctuation_when_none_follows_the_passage() -> None:
    rewrite = '{"rewritten_text":"Das alte Passwort gilt danach nicht mehr.","changes":["eigener Satz"]}'
    client, _llm = _make_client(llm=_CapturingLLM(response=rewrite))

    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "das alte Passwort danach nicht mehr gilt",
            "instruction": "Diesen Teil als eigenen kurzen Satz formulieren.",
            "context_after": " und der Support hilft weiter.",
            "locale": "de",
        },
    )

    assert response.status_code == 200
    assert response.json()["improved_text"] == "Das alte Passwort gilt danach nicht mehr."


def test_route_keeps_a_question_mark_before_the_following_full_stop() -> None:
    """Only a doubled mark is a seam defect. A question the instruction asked
    for must not silently lose its "?"; the mismatch is logged and visible in
    the diff instead."""
    rewrite = '{"rewritten_text":"Gilt das alte Passwort danach noch?","changes":["als Frage"]}'
    client, _llm = _make_client(llm=_CapturingLLM(response=rewrite))

    with _InqtrixWarnings() as warnings:
        response = client.post(
            "/v1/editor/suggest",
            json={
                "block_text": "das alte Passwort danach nicht mehr gilt",
                "instruction": "Formuliere als Frage.",
                "context_after": ". Bei Fragen hilft der Support weiter.",
                "locale": "de",
            },
        )

    assert response.status_code == 200
    assert response.json()["improved_text"] == "Gilt das alte Passwort danach noch?"
    assert any(
        "ends with other punctuation than the text that follows" in message
        for message in warnings.messages
    )


def test_route_repairs_the_seam_before_checking_for_an_unchanged_result() -> None:
    """A rewrite that only appends the full stop the sentence already has is
    no change at all; repairing after the check would hand the user a
    suggestion that changes nothing."""
    block = "das alte Passwort danach nicht mehr gilt"
    only_a_stop = '{"rewritten_text":"' + block + '.","changes":["Punkt ergänzt"]}'
    real = '{"rewritten_text":"Das alte Passwort gilt danach nicht mehr","changes":["eigener Satz"]}'
    client, llm = _make_client(llm=_CapturingLLM(response=[only_a_stop, real]))

    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": block,
            "instruction": "Formuliere diesen Teil als eigenen Satz.",
            "context_after": ". Bei Fragen hilft der Support weiter.",
            "locale": "de",
        },
    )

    assert response.status_code == 200
    assert response.json()["improved_text"] == "Das alte Passwort gilt danach nicht mehr"
    assert len(llm.prompts) == 2


def test_route_does_not_refuse_secret_looking_text_in_the_context() -> None:
    """The context is an excerpt of the background, which is sent to the
    model without the secret check; checking only the excerpt would refuse
    text the request sends anyway."""
    client, llm = _make_client()

    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "Es wurde beschlossen, das Sortiment zu erweitern.",
            "instruction": "Mach den Satz aktiver.",
            "context_after": " token=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "locale": "de",
        },
    )

    assert response.status_code == 200
    assert llm.prompt is not None


def test_route_traces_the_suggestion_as_one_request_trace_with_its_user(
    monkeypatch,
) -> None:
    """The access log's trace id must lead to the model call: the editor span
    runs in an executor thread and opened a trace of its own, and no trace of
    the request carried a user."""
    import uuid

    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from inqtrix.auth.log_redaction import stable_pseudonym
    from inqtrix.auth.principal import NoneAuthProvider, Principal
    from inqtrix.observability import semconv
    from inqtrix.observability.otel import current_trace_id_hex

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        otel_trace,
        "get_tracer",
        lambda *args, **kwargs: provider.get_tracer("test"),
    )
    user_id = uuid.UUID("12345678-1234-4234-8234-123456789abc")

    class _UserAuthProvider(NoneAuthProvider):
        def resolve_principal(self, request):
            return Principal(user_id=user_id, kind="oidc_session")

    class _TraceRecordingLLM(_CapturingLLM):
        def complete(self, prompt: str, **kwargs: object) -> str:
            self.trace_id_in_call = current_trace_id_hex()
            return super().complete(prompt, **kwargs)

    llm = _TraceRecordingLLM()
    app = create_app(
        settings=Settings(models=ModelSettings(), agent=AgentSettings()),
        providers=ProviderContext(llm=llm, search=_DummySearch()),
        auth_provider=_UserAuthProvider(),
    )
    response = TestClient(app).post(
        "/v1/editor/suggest",
        json={"block_text": "Ein Absatz.", "instruction": "Mach den Satz aktiver."},
    )
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    (request_span,) = [
        span for span in spans if span.kind == otel_trace.SpanKind.SERVER
    ]
    (editor_span,) = [span for span in spans if span.name == "editor:suggest"]
    request_trace = request_span.get_span_context().trace_id
    assert editor_span.get_span_context().trace_id == request_trace
    assert editor_span.parent is not None
    assert editor_span.parent.span_id == request_span.get_span_context().span_id
    # A log line written during the model call carries this trace id.
    assert llm.trace_id_in_call == format(request_trace, "032x")
    assert (
        request_span.attributes[semconv.INQTRIX_REQUEST_ID]
        == response.headers["X-Request-ID"]
    )
    assert request_span.attributes[semconv.LANGFUSE_USER_ID] == stable_pseudonym(
        "usr", user_id
    )
    # The operation keeps its own trace name inside the request's trace.
    assert editor_span.attributes[semconv.LANGFUSE_TRACE_NAME] == "editor:suggest"


def test_route_structured_path_uses_complete_structured() -> None:
    client, llm = _make_client(llm=_CapturingLLM(structured=True))
    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "Ein langer Absatz.",
            "instruction": "Mach den Satz aktiver.",
            "locale": "de",
        },
    )
    assert response.status_code == 200
    assert llm.structured_called is True
    assert response.json()["improved_text"] == "Die Arbeitsgruppe erweitert das Sortiment."


def test_route_requires_an_instruction() -> None:
    client, _llm = _make_client()
    response = client.post(
        "/v1/editor/suggest",
        json={"block_text": "Ein Absatz ohne Anweisung."},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_route_rejects_sensitive_text_before_model_call() -> None:
    client, llm = _make_client()
    response = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "token=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
                "instruction": "Mach den Satz aktiver.",
        },
    )
    assert response.status_code == 400
    assert "secret material" in response.json()["error"]["message"]
    assert llm.prompt is None


def test_route_surfaces_model_json_failure() -> None:
    client, _llm = _make_client(llm=_CapturingLLM(response="not json"))
    response = client.post(
        "/v1/editor/suggest",
            json={"block_text": "Ein Absatz.", "instruction": "Mach den Satz aktiver."},
    )
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "server_error"


def test_route_routes_by_selected_tier() -> None:
    models = ModelSettings(
        reasoning_model="default-model",
        tier_high_model="big-model",
        tier_mid_model="mid-model",
    )
    client, llm = _make_client(llm=_CapturingLLM(models=models))

    high = client.post(
        "/v1/editor/suggest",
        json={
            "block_text": "Ein Absatz.",
            "instruction": "Mach den Satz aktiver.",
            "agent_overrides": {"model_tier": "high"},
        },
    )
    assert high.status_code == 200
    assert llm.kwargs["model"] == "big-model"

    default = client.post(
        "/v1/editor/suggest",
        json={"block_text": "Ein Absatz.", "instruction": "Mach den Satz aktiver."},
    )
    assert default.status_code == 200
    # direct_chat defaults to the mid tier.
    assert llm.kwargs["model"] == "mid-model"


def test_route_uses_bearer_auth_when_configured() -> None:
    client, _llm = _make_client(server_settings=ServerSettings(api_key="secret-token-123"))
    missing = client.post(
        "/v1/editor/suggest",
        json={"block_text": "Ein Absatz.", "instruction": "Mach den Satz aktiver."},
    )
    allowed = client.post(
        "/v1/editor/suggest",
        json={"block_text": "Ein Absatz.", "instruction": "Mach den Satz aktiver."},
        headers={"Authorization": "Bearer secret-token-123"},
    )
    assert missing.status_code == 401
    assert allowed.status_code == 200


def test_structured_output_error_surfaces_as_502() -> None:
    class _FailingStructuredLLM(_CapturingLLM):
        def complete_structured(self, prompt: str, **kwargs: object) -> StructuredLLMResponse:
            raise AgentStructuredOutputError("model", "schema", "boom")

    client, _llm = _make_client(llm=_FailingStructuredLLM(structured=True))
    response = client.post(
        "/v1/editor/suggest",
        json={"block_text": "Ein Absatz.", "instruction": "Kuerzen."},
    )
    assert response.status_code == 502
