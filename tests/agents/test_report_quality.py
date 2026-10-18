"""Report-quality building blocks: web-excerpt grounding, the
deterministic evidence rank, and the tief-tier escalation trigger.

The moved citation helpers keep their behavior contract in
``test_synthesis_citations.py`` (imports via the synthesis re-exports —
the refactor guard); this module covers the building blocks above.
"""

from __future__ import annotations

import pytest

from inqtrix.agents.report_quality import (
    rank_evidence,
    report_word_count,
    unverified_web_quotes,
    verify_quotes,
)


@pytest.mark.parametrize("opening,closing", [
    ('"', '"'), ('„', '“'), ('“', '”'), ('«', '»'), ('»', '«'),
])
@pytest.mark.parametrize("has_original", [False, True])
def test_paired_quotes_are_checked_against_retained_originals(opening, closing, has_original):
    quote = "Die gespeicherte Aussage gilt nur unter einer Bedingung."
    references = [{"label": "W1", "url": "https://example.org/source",
                   "excerpt": quote if has_original else "", "grounded_support": quote}]
    checks = verify_quotes(f'{opening}{quote}{closing} [W1]', references)
    assert checks == [{"quote": quote, "verified": has_original}]
    assert unverified_web_quotes(f'{opening}{quote}{closing} [W1]', checks) == ([] if has_original else [quote])


def test_mixed_quote_styles_keep_order_and_do_not_cross_unpaired_marks():
    first = "First complete quotation with sufficient length."
    second = "Second complete quotation with sufficient length."
    markdown = f'„{first}“ then «{second}» and „This unclosed text has no matching end.'
    assert verify_quotes(markdown, []) == [
        {"quote": first, "verified": False}, {"quote": second, "verified": False},
    ]


@pytest.mark.parametrize("text,expected", [
    ("", 0),
    ("# Überschrift\n\nDas gilt. [W1]", 4),
    ("l’été don't 20–30 alpha_beta 3.5", 7),
    ("### 你好 Welt\n```python\nx = 2\n```", 5),
])
def test_report_word_count_declares_lexical_markdown_scope(text, expected):
    assert report_word_count(text) == expected


def _web_ref(label: str, url: str, excerpt: str = "", tasks: int = 1) -> dict:
    return {
        "label": label,
        "url": url,
        "excerpt": excerpt,
        "tasks": [f"t{i}" for i in range(tasks)],
    }


def _doc_ref(label: str, excerpt: str = "") -> dict:
    return {
        "label": label,
        "document_id": f"doc-{label}",
        "excerpt": excerpt,
        "tasks": ["t1"],
    }


class TestVerifyQuotesIncludesWebExcerpts:
    def test_quote_matching_a_web_excerpt_verifies(self):
        quote = "Die Compliance-Kosten steigen fuer kleine Anbieter deutlich"
        refs = [
            _web_ref("W1", "https://example.org/a", excerpt=f"Studie: {quote}."),
        ]
        checks = verify_quotes(f'Analyse: "{quote}" [W1].', refs)
        assert checks == [{"quote": quote, "verified": True}]

    def test_quote_without_any_stored_excerpt_stays_unverified(self):
        quote = "Diese Behauptung steht in keinem gespeicherten Auszug drin"
        refs = [
            _web_ref("W1", "https://example.org/a", excerpt="Anderer Inhalt."),
            _doc_ref("K1", excerpt="Interner Text ohne das Zitat."),
        ]
        checks = verify_quotes(f'Er sagte: "{quote}" [W1].', refs)
        assert checks == [{"quote": quote, "verified": False}]


class TestUnverifiedWebQuotes:
    def test_flags_only_unverified_quotes_in_web_cited_paragraphs(self):
        markdown = (
            'Erster Absatz: "Zitat aus dem Web ohne Auszugstreffer xxxx" [W1].'
            "\n\n"
            'Zweiter Absatz: "Internes Zitat ohne jeden Auszugstreffer" [K1].'
        )
        checks = [
            {"quote": "Zitat aus dem Web ohne Auszugstreffer xxxx", "verified": False},
            {"quote": "Internes Zitat ohne jeden Auszugstreffer", "verified": False},
        ]
        assert unverified_web_quotes(markdown, checks) == [
            "Zitat aus dem Web ohne Auszugstreffer xxxx"
        ]

    def test_verified_quotes_never_escalate(self):
        markdown = 'Absatz: "Sauber belegtes Zitat mit Auszugstreffer" [W1].'
        checks = [
            {"quote": "Sauber belegtes Zitat mit Auszugstreffer", "verified": True},
        ]
        assert unverified_web_quotes(markdown, checks) == []


class TestRankEvidence:
    def test_passthrough_at_or_below_budget(self):
        refs = [_web_ref("W1", "https://a.example"), _doc_ref("K1")]
        assert rank_evidence(refs, budget=2) is refs
        assert rank_evidence(refs, budget=0) is refs

    def test_caps_by_tier_corroboration_and_excerpt(self):
        refs = [
            _web_ref("W1", "https://low.example"),
            _doc_ref("K1", excerpt="Interner Auszug."),
            _web_ref(
                "W2",
                "https://corroborated.example",
                excerpt="Belegter Auszug.",
                tasks=3,
            ),
            _web_ref("W3", "https://plain.example"),
        ]
        tiers = {
            "https://low.example": "low",
            "https://corroborated.example": "mainstream",
            "https://plain.example": "unknown",
        }
        kept = rank_evidence(
            refs, budget=2, tier_for_url=lambda url: tiers[url]
        )
        # Internal doc (primary weight) and the corroborated excerpt win;
        # ledger order is preserved among the kept rows.
        assert [ref["label"] for ref in kept] == ["K1", "W2"]

    def test_never_mutates_or_reorders_the_ledger(self):
        refs = [
            _web_ref("W1", "https://a.example", excerpt="x", tasks=2),
            _web_ref("W2", "https://b.example"),
            _web_ref("W3", "https://c.example", excerpt="y"),
        ]
        before = [dict(ref) for ref in refs]
        kept = rank_evidence(refs, budget=2, tier_for_url=lambda url: "unknown")
        assert refs == before
        assert [ref["label"] for ref in kept] == ["W1", "W3"]


class TestRankEvidenceVisibility:
    def test_a_capped_digest_raises_the_truncation_event(self, monkeypatch, caplog):
        from inqtrix.agents import report_quality
        from inqtrix.observability import semconv

        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            report_quality, "add_span_event", lambda name, payload: events.append((name, dict(payload)))
        )
        refs = [_web_ref(f"W{i}", f"https://s{i}.example") for i in range(1, 5)]
        with caplog.at_level("WARNING", logger="inqtrix"):
            kept = rank_evidence(refs, budget=2, tier_for_url=lambda url: "unknown")
        assert len(kept) == 2
        assert events == [
            (
                semconv.TRUNCATION_EVENT,
                {
                    semconv.TRUNCATION_LIMIT_NAME: "synthesis_evidence_budget",
                    semconv.TRUNCATION_ORIGINAL_SIZE: 4,
                    semconv.TRUNCATION_CAPPED_SIZE: 2,
                },
            )
        ]
        assert any(
            "capped synthesis_evidence_budget to 2 of 4" in r.getMessage() for r in caplog.records
        )
        # Below the budget nothing is capped and nothing is reported.
        events.clear()
        assert rank_evidence(refs, budget=4) is refs
        assert events == []


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
        "Nutzer: und kd_zweiter?\n"
        "Assistent: kd_letzter auch."
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
    )
    from inqtrix.agents.kernel import tools
    from inqtrix.services import request_parsing

    turn = inspect.getsource(session_context._verbatim_turn)
    assert 'f"User: {question}"' in turn
    assert 'f"Agent: {body}"' in turn
    assert f'f"{CLARIFICATION_MARKER}{{prompt}}{CLARIFICATION_ANSWER_MARKER}{{answer}}"' in turn
    roles = inspect.getsource(request_parsing.format_history)
    assert '"user": "Nutzer"' in roles and '"assistant": "Assistent"' in roles
    assert "Nutzer: " in USER_TURN_MARKERS and "User: " in USER_TURN_MARKERS
    assert "Agent: " in MODEL_TURN_MARKERS and "Assistent: " in MODEL_TURN_MARKERS
    assert "User's answer: " in inspect.getsource(tools)


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
