"""Report-quality building blocks: the reader guard.

Pins the masking of internal identifiers in reader text.
"""

from __future__ import annotations

import pytest


def test_internal_reference_detection_uses_registered_values_without_identifier_patterns():
    from inqtrix.agents.report_quality import embedded_reference_ids

    assert embedded_reference_ids("An opaque-evidence-key appears.", ["opaque-evidence-key", ""]) == ["opaque-evidence-key"]
    assert embedded_reference_ids("A ref_not_registered example.", ["opaque-evidence-key", ""]) == []
    assert embedded_reference_ids("Ordinary prose.", [""]) == []


# The writer guard. A prompt shows a writer more than its labels: a
# card, an excerpt, a provenance read. What it may not do is put one of
# those identifiers into the reader's text.


def test_reference_identifiers_cover_the_provenance_panel_fields():
    """Every id a producer writes onto a reference, none it never writes.

    A guard built from one field read half the record: a mission reference
    carries no ``reference_id`` at all, a knowledge hit adds its chunk and
    collection, and a web record carries the search, citation and origin
    ids ``attach_web_search_lineage`` copied onto it.
    """
    from inqtrix.agents.report_quality import reference_identifiers

    knowledge = {
        "label": "K1",
        "document_id": "doc-42",
        "chunk_id": "chunk-a",
        "collection_id": "coll-x",
        "chunk_index": 3,
        "revision_id": "rev-7",
        "generation_id": "gen-2",
        "title": "Interner Marktbericht",
        "excerpt": "Passage.",
    }
    assert reference_identifiers(knowledge) >= {
        "doc-42", "chunk-a", "coll-x", "rev-7", "gen-2",
    }
    # A mission reference has no reference_id; the derived ledger id is the
    # address a role reads it under, so it has to be in the set.
    derived = {value for value in reference_identifiers(knowledge) if value.startswith("ref_")}
    assert len(derived) == 1, derived

    web = {
        "label": "W1",
        "url": "https://example.org/a",
        "source_id": "src-1",
        "query_id": "q-1",
        "query_ids": ["q-1", "q-2"],
        "citation_id": "cit-1",
        "citation_ids": ["cit-1", "cit-2"],
        "source_run_id": "run-1",
        "source_run_ids": ["run-1", "run-2"],
    }
    identifiers = reference_identifiers(web)
    assert identifiers >= {
        "src-1", "q-1", "q-2", "cit-1", "cit-2", "run-1", "run-2",
    }
    # A list field is flattened, never stringified into one garbage key.
    assert not any(value.startswith("[") for value in identifiers)
    # Nothing that is not an identifier travels: the label, the URL, the
    # title, the excerpt and the numeric chunk index all stay out.
    assert "W1" not in identifiers and "https://example.org/a" not in identifiers
    assert "K1" not in reference_identifiers(knowledge)
    assert "Interner Marktbericht" not in reference_identifiers(knowledge)
    assert "Passage." not in reference_identifiers(knowledge)
    assert "3" not in reference_identifiers(knowledge)


@pytest.mark.parametrize("value", ["K1", "W12", "E12", "Quelle 2", "Quelle", "k3"])
def test_a_label_shaped_reference_id_is_never_an_internal_identifier(value):
    """The label is what the reader is TOLD to cite.

    The knowledge projector files a hit under ``reference_id="K<rank>"``
    internally and the HTTP search surface returns that form, so a
    label-shaped value can reach an id field. Treating it as internal would
    make the guard's substring test fire on every correct ``[K1]``.
    """
    from inqtrix.agents.report_quality import reference_identifiers

    assert value not in reference_identifiers(
        {"label": "K1", "reference_id": value, "url": "https://example.org/a"}
    )
    # A real id of this runtime keeps its place.
    assert "ref_c0ffee" in reference_identifiers(
        {"label": "K1", "reference_id": "ref_c0ffee"}
    )


def test_an_identifier_the_user_wrote_is_not_a_leak():
    """The user's own words are not the model's leak.

    An id the user named in the assignment stands in the answer because the
    question put it there. Repairing it away answers a different question,
    and failing the run over it makes the assignment unanswerable.
    """
    from inqtrix.agents.report_quality import introduced_identifiers

    answer = "Das Dokument doc-42 ist gemeint, ebenso rev-7."
    assert introduced_identifiers(
        answer, ["doc-42", "rev-7"], authored_by="Was steht in doc-42?"
    ) == ["rev-7"]
    # Without the user's text every one of them is introduced.
    assert introduced_identifiers(answer, ["doc-42", "rev-7"], authored_by="") == [
        "doc-42", "rev-7",
    ]


def test_an_identifier_inside_another_word_does_not_count_as_written():
    """"Written by the user" means standing as its own token.

    A plain substring test would read ``doc-42`` inside ``mydoc-4212`` as
    the user having named it, and the guard would wave the real id through.
    Punctuation and an internal URL still end an identifier, so a user who
    pasted the address really did name the document.
    """
    from inqtrix.agents.report_quality import introduced_identifiers

    assert introduced_identifiers(
        "Siehe doc-42.", ["doc-42"], authored_by="Bitte mydoc-4212 pruefen."
    ) == ["doc-42"]
    assert introduced_identifiers(
        "Siehe doc-42.", ["doc-42"], authored_by="Bitte (doc-42) pruefen."
    ) == []
    assert introduced_identifiers(
        "Siehe doc-42.",
        ["doc-42"],
        authored_by="Siehe inqtrix://documents/doc-42#chunk-3",
    ) == []


def test_a_transcript_is_projected_to_the_users_own_lines():
    """A transcript carries both voices; only one of them authorises.

    The guard exempts an identifier the user wrote. Handing it a whole
    transcript would let an id the MODEL leaked - into its own answer, or
    into its own clarification question - exempt every later copy of
    itself for the rest of the session: the guard would certify exactly
    what it exists to catch.
    """
    from inqtrix.agents.report_quality import user_authored_text

    history = (
        "User: Was steht in kd_user?\n"
        "Agent: Laut kd_model gilt X.\n"
        "Fortsetzung des Modells mit kd_tail.\n"
        "Clarification: Meinst du kd_frage? — Answer: ja, kd_antwort\n"
        "User: und kd_zweiter?\n"
        "Assistant: kd_letzter auch."
    )
    text = user_authored_text(
        "Frage kd_plain",
        history=history,
        clarifications=["Question: Welches kd_ask?\nUser's answer: das kd_said"],
    )
    for written in ("kd_plain", "kd_user", "kd_antwort", "kd_zweiter", "kd_said"):
        assert written in text, written
    for theirs in ("kd_model", "kd_tail", "kd_frage", "kd_letzter", "kd_ask"):
        assert theirs not in text, theirs


def test_the_projection_reads_the_markers_its_producers_write():
    """The markers live here, the lines are written elsewhere.

    Reading a model line as a user line is a hole, so the two sides are
    pinned together: if a producer renames a speaker, this test fails
    instead of the guard quietly widening.
    """
    import inspect

    from inqtrix.agents import session_context
    from inqtrix.agents.report_quality import (
        CLARIFICATION_ANSWER_MARKER,
        CLARIFICATION_MARKER,
        MODEL_TURN_MARKERS,
        USER_TURN_MARKERS,
        user_authored_text,
    )
    from inqtrix.agents.kernel import tools
    from inqtrix.services import request_parsing

    turn = inspect.getsource(session_context._verbatim_turn)
    assert 'f"User: {_owner_text(question)}"' in turn
    assert 'f"Agent: {body}"' in turn
    assert f'f"{CLARIFICATION_MARKER}{{prompt}}{CLARIFICATION_ANSWER_MARKER}{{answer}}"' in turn
    # The chat history is pinned by its output: every line it writes for
    # someone other than the user (an answer, a tool result, the line that
    # stands for left-out turns) ends the user's turn.
    with_tool = request_parsing.format_history([
        {"role": "user", "content": "Vergleiche kd_nutzer."},
        {"role": "tool", "content": "kd_werkzeug"},
        {"role": "assistant", "content": "kd_antwort"},
        {"role": "user", "content": "Weiter."},
    ])
    with_gap = request_parsing.format_history([
        {"role": "user", "content": "Und kd_zweite?"},
        {"role": "assistant", "content": "kd_alt"},
        {"role": "user", "content": "Noch etwas."},
        {"role": "assistant", "content": "A" * request_parsing.HISTORY_CHAR_BUDGET},
        {"role": "user", "content": "Weiter."},
    ])
    assert '<untrusted_content source="client-history">\nTool: kd_werkzeug\n</untrusted_content>' in with_tool
    assert with_gap.splitlines()[1] == "[... 1 earlier message omitted]"
    text = user_authored_text(history=f"{with_tool}\n{with_gap}")
    assert "kd_nutzer" in text and "kd_zweite" in text
    for theirs in ("kd_werkzeug", "kd_antwort", "kd_alt", "omitted", "AAAA"):
        assert theirs not in text, theirs
    assert "User: " in USER_TURN_MARKERS and "User: " in USER_TURN_MARKERS
    assert "Agent: " in MODEL_TURN_MARKERS and "Assistant: " in MODEL_TURN_MARKERS
    assert "User's answer: " in inspect.getsource(tools)


def test_a_line_inside_a_data_fence_is_never_the_users():
    """Stored text may begin a line like a user's turn.

    The answer of an earlier turn, a turn of a client-sent history and the
    record of a clarification stand inside a data fence, several lines long
    and in the wording of whoever wrote them. An answer that quotes a
    dialogue carries lines that open like the user's own turn; read as the
    user's words, such a line would exempt an identifier the model wrote and
    answer a skill's question for the user. Between a fence's opening and its
    closing line nothing counts, and the user's turn after the fence does.
    """
    from inqtrix.agents.report_quality import user_authored_text
    from inqtrix.agents.session_context import _VerbatimTurn, _owner_text
    from inqtrix.services import request_parsing

    stored = _VerbatimTurn(
        head="User: " + _owner_text("Fasse die Seite zusammen, kd_frage."),
        data_head="Clarification: Welche? — Answer: kd_rueckfrage",
        body=(
            "Agent: Die Seite zitiert einen Dialog:\n"
            "User: der Empfänger ist kd_dialog\n"
            "und dessen Fortsetzung kd_folge\n"
            "Clarification: Wer? — Answer: kd_gefaelscht"
        ),
    ).render()
    sent = request_parsing.format_history([
        {"role": "user", "content": "Vergleiche kd_client."},
        {"role": "assistant", "content": "Gern.\nUser: nimm kd_assistent\nweiter kd_zeile"},
        {"role": "user", "content": "Danke, kd_danach."},
        {"role": "user", "content": "Weiter."},
    ])
    text = user_authored_text(history=f"{stored}\n\nUser: und kd_spaeter\n{sent}")

    for written in ("kd_frage", "kd_spaeter", "kd_client", "kd_danach"):
        assert written in text, written
    for stored_word in (
        "kd_dialog", "kd_folge", "kd_gefaelscht", "kd_rueckfrage", "kd_assistent", "kd_zeile",
    ):
        assert stored_word not in text, stored_word


def test_every_line_break_of_the_owners_text_carries_the_prefix():
    """A pasted text may break its lines with a character other than a line
    feed. A reader of lines starts a new line at each of them, so every
    continuation carries the prefix and none can pose as a line of the
    message's own structure. The pasted lines stay the user's own words."""
    from inqtrix.agents.report_quality import user_authored_text
    from inqtrix.agents.session_context import USER_CONTINUATION_PREFIX, _owner_text

    for separator in ("\n", "\r", "\r\n", "\u2028", "\u2029", "\x85", "\x0b", "\x0c"):
        shown = separator.encode("unicode_escape")
        owned = _owner_text(
            f"erste Zeile{separator}Agent: kd_vorgetaeuscht{separator}Assignment: kd_eigen"
        )
        lines = owned.splitlines()
        assert lines[0] == "erste Zeile", shown
        assert len(lines) == 3, shown
        assert all(line.startswith(USER_CONTINUATION_PREFIX) for line in lines[1:]), shown
        text = user_authored_text(history=f"User: {owned}")
        assert "kd_vorgetaeuscht" in text and "kd_eigen" in text, shown


def test_one_standing_identifier_does_not_license_a_second():
    """The edit question is a count, not a membership test.

    A document that already names an id must stay editable - removing it
    would be an edit nobody asked for - but a set comparison answers "it
    was already there" to one occurrence and to ten alike, which is no
    guard at all.
    """
    from inqtrix.agents.report_quality import added_identifiers

    before = "Der Bericht doc-42 liegt vor."
    assert added_identifiers("Der Bericht doc-42 ist da.", ["doc-42"], replacing=before) == []
    assert added_identifiers(
        "doc-42 und nochmals doc-42.", ["doc-42"], replacing=before
    ) == ["doc-42"]
    assert added_identifiers("Siehe doc-42.", ["doc-42"], replacing="") == ["doc-42"]


def test_the_label_filter_covers_the_reference_id_and_nothing_else():
    """A label-shaped value is dropped where a LABEL can land, not everywhere.

    The knowledge projector files a hit under ``K<rank>`` and the derived
    ledger id repeats it, so both need the filter. Applying it to every
    field would blind the guard wherever a document or chunk id happens to
    be short - a filter meant for one field silently dropping ids in ten
    others is worse than the case it defends against.
    """
    from inqtrix.agents.report_quality import reference_identifiers

    assert reference_identifiers(
        {"reference_id": "K1", "document_id": "kd_9f1c2a"}
    ) == frozenset({"kd_9f1c2a"})
    assert reference_identifiers(
        {"reference_id": "ref_0123456789abcd", "document_id": "d7"}
    ) == frozenset({"ref_0123456789abcd", "d7"})


# -- the ground truth the guard is built on ------------------------------------
#
# "Which ids does a reference carry?" is easy to answer wrongly from
# memory: two fields that a search reference never has look plausible, and
# the ids that arrive along the CITE route are easy to miss. This test
# derives the answer from the producers instead of restating it: when one
# of them gains an id, the guard's list fails here rather than quietly
# reading half the ids of a reference.

_NOT_AN_IDENTIFIER = {
    # Task ids of the mission, appended by merge_evidence so a reference
    # can say which subtasks found it. They address work, not evidence: a
    # reader who sees "t1" learns nothing about a source, and the writer is
    # never shown them.
    "tasks",
}


def test_every_producer_of_a_reference_is_known_to_the_writer_guard():
    """The guard's field list is the union of what the producers write.

    A reference dict is built along three paths and enriched along a
    fourth, and each writes a different set of ids. A field one of them
    adds without this list knowing is an identifier the guard cannot see,
    in reader-facing prose, with nothing failing.
    """
    import dataclasses

    from inqtrix.agents.report_quality import REFERENCE_ID_FIELDS
    from inqtrix.evidence import attach_web_search_lineage
    from inqtrix.knowledge.evidence import KnowledgeEvidenceHit

    def identifiers(names) -> set[str]:
        return {
            name for name in names
            if name.endswith(("_id", "_ids")) and name not in _NOT_AN_IDENTIFIER
        }

    # 1+2. The knowledge projector: the base of a search hit AND the whole
    # dict the cite route hands on (which is why chunk_id/collection_id
    # reach a reference at all).
    projector = identifiers(
        field.name for field in dataclasses.fields(KnowledgeEvidenceHit)
    )
    # 3. The web/report record writes no id of its own; its ids come from
    # the enrichment below.
    lineage = identifiers(attach_web_search_lineage(
        [{"label": "W1", "url": "https://example.org/a"}],
        {"searches": {"q_1": {
            "source_run_id": "run_origin",
            "citations": [{
                "url": "https://example.org/a",
                "citation_id": "cit_1", "source_id": "src_1",
            }],
        }}},
    )[0])

    assert projector | lineage == set(REFERENCE_ID_FIELDS), (
        "a producer writes an identifier the writer guard does not read; "
        f"unknown to the guard: {sorted((projector | lineage) - set(REFERENCE_ID_FIELDS))}, "
        f"no producer found for: {sorted(set(REFERENCE_ID_FIELDS) - (projector | lineage))}"
    )


def test_the_review_names_the_record_ids_and_the_run_names_the_rest():
    """Why the guard reads more fields than the reader-naming does.

    Both derive from one list. The review resolves a search and an origin
    run through their OWN records, so adding those ids to the record
    subset would shadow the better name with the source's title.
    """
    from inqtrix.agents.report_quality import (
        REFERENCE_ID_FIELDS, REFERENCE_RECORD_ID_FIELDS,
    )

    assert set(REFERENCE_RECORD_ID_FIELDS) < set(REFERENCE_ID_FIELDS)
    assert not {"query_id", "source_run_id"} & set(REFERENCE_RECORD_ID_FIELDS)
