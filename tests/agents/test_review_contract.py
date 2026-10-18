"""Typed review grounds and per-finding local revision without semantic guessing."""

from types import SimpleNamespace

import pytest

from inqtrix.agents.critic_phase import uncovered_criteria
from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.agents.kernel.review_edits import (
    FindingResult,
    apply_review_edits,
    locate_findings,
    review_context_fits,
    split_findings_by_basis,
)
from inqtrix.agents.phase_models import DeepReviewFinding, DeepRevisionBundle
from inqtrix.agents.report_quality import writer_identifiers


def finding(
    quote="Falsch.", *, target="artifact", artifact_id="doc", kind="run_diagnostic",
    basis_ids=("diagnostic:b",), remove_labels=(), criterion_index=None,
):
    """A finding that locates by default: a run diagnostic on its own basis."""
    return DeepReviewFinding(
        target=target, artifact_id=artifact_id, finding="Konkret korrigieren.",
        quote=quote, kind=kind, basis_ids=list(basis_ids),
        remove_labels=list(remove_labels), criterion_index=criterion_index,
    )


def ref(label, identity):
    """A reference of an output under its ledger id *identity*."""
    return {"label": label, "reference_id": identity}


def locate(findings, outputs, *, allowed_references=None, criteria_count=0):
    """Locate model findings; every reference an output holds is a source basis."""
    allowed = allowed_references if allowed_references is not None else {key: [] for key in outputs}
    return locate_findings(
        [("model", item) for item in findings], outputs,
        source_basis_ids={
            key: {"source:a", *(
                f"source:{KernelDeps.ledger_id(item)}" for item in allowed.get(key, [])
            )}
            for key in outputs
        },
        diagnostic_basis_ids={key: {"diagnostic:b"} for key in outputs},
        allowed_references=allowed,
        criteria_count=criteria_count,
    )


def edits(located, *replacements):
    """One replacement per located finding, in order."""
    return DeepRevisionBundle(edits=[
        {"finding_id": item["finding_id"], "replacement": text}
        for item, text in zip(located, replacements)
    ])


def outcome(item, output, replacement, *, references=()):
    """``(state, reason)`` of one finding on ``{"doc": output}``, located then edited."""
    outputs = {"doc": output}
    allowed = {"doc": list(references)}
    located, named = locate([item], outputs, allowed_references=allowed)
    if named:
        return named[0].state, named[0].reason
    _revised, results = apply_review_edits(
        outputs, located, edits(located, replacement),
        allowed_references=allowed, identifiers=[],
    )
    return results[0].state, results[0].reason


@pytest.mark.parametrize("basis_ids, confirmed", [
    # held by the run's ledger AND read before the verdict
    (["source:held_and_read"], True),
    # held, but the role never opened it
    (["source:held_unread"], False),
    # opened by the role, but only its own ledger knows it
    (["source:read_but_foreign"], False),
    # a diagnostic the handover carried for THIS output
    (["diagnostic:for_doc"], True),
    # a diagnostic that belongs to another output
    (["diagnostic:for_chat"], False),
    # an output requirement needs no basis
    ([], True),
    # one unconfirmed basis is enough to open the finding
    (["source:held_and_read", "source:held_unread"], False),
])
def test_a_basis_is_confirmed_by_the_runs_ledger_and_the_roles_reading(basis_ids, confirmed):
    item = finding(kind="output_requirement", basis_ids=basis_ids)
    kept, opened = split_findings_by_basis(
        [item],
        read_sources=frozenset({
            ("retained_evidence", "held_and_read"), ("retained_evidence", "read_but_foreign"),
        }),
        ledger_ids=frozenset({"held_and_read", "held_unread"}),
        diagnostic_ids={"doc": {"diagnostic:for_doc"}, "chat": {"diagnostic:for_chat"}},
    )
    assert (kept, opened) == (([item], []) if confirmed else ([], [item]))


def test_one_unconfirmed_finding_does_not_take_the_confirmed_ones_with_it():
    good = finding("Alpha.", kind="output_requirement", basis_ids=())
    bad = finding("Beta.", basis_ids=["source:nowhere"])
    kept, opened = split_findings_by_basis(
        [good, bad], read_sources=frozenset(), ledger_ids=frozenset(), diagnostic_ids={},
    )
    assert kept == [good] and opened == [bad]


def test_local_revision_preserves_other_bytes_and_does_not_rewrite_chat():
    original = "# Titel\r\n\r\n## Gleich\r\nFalsch.\r\n\r\n## Gleich\r\n  Bleibt.  \r\n"
    outputs = {"doc": original, "chat": "Chat mit  zwei Leerzeichen."}
    located, _ = locate([finding()], outputs)
    revised, _ = apply_review_edits(outputs, located, DeepRevisionBundle(edits=[{
        "finding_id": "finding_1", "replacement": "Korrigiert.",
    }]), allowed_references={"doc": [], "chat": []}, identifiers=[])
    assert revised == {"doc": original.replace("Falsch.", "Korrigiert."), "chat": outputs["chat"]}
    assert outputs["doc"] == original


def test_edits_use_the_original_positions_even_if_replacement_duplicates_an_anchor():
    outputs = {"doc": "Alpha. Beta.", "chat": "Fertig."}
    located, _ = locate([finding("Alpha."), finding("Beta.")], outputs)
    revised, _ = apply_review_edits(outputs, located, DeepRevisionBundle(edits=[
        {"finding_id": "finding_1", "replacement": "Korrigiert."},
        {"finding_id": "finding_2", "replacement": "Alpha."},
    ]), allowed_references={"doc": [], "chat": []}, identifiers=[])
    assert revised["doc"] == "Korrigiert. Alpha."


@pytest.mark.parametrize("kind,basis_id", [
    ("evidence_error", "source:a"), ("run_diagnostic", "diagnostic:a"),
])
def test_ground_from_a_different_output_cannot_justify_a_finding(kind, basis_id):
    outputs = {"doc": "Falsch.", "other": "Falsch."}
    _located, named = locate_findings(
        [("model", finding(kind=kind, basis_ids=[basis_id]))], outputs,
        source_basis_ids={"doc": {"source:b"}, "other": {"source:a"}},
        diagnostic_basis_ids={"doc": {"diagnostic:b"}, "other": {"diagnostic:a"}},
        allowed_references={"doc": [], "other": []}, criteria_count=0,
    )
    assert [result.reason for result in named] == ["basis_invalid"]


def test_one_unlocatable_finding_leaves_the_others_located():
    """An ambiguous anchor names its own finding, never the set."""
    located, named = locate(
        [finding("Alpha."), finding("Beta."), finding("Gamma.")],
        {"doc": "Alpha. Beta. Gamma. Gamma."},
    )
    assert ([item["finding_id"] for item in located], [(r.finding_id, r.reason) for r in named]) == (
        ["finding_1", "finding_2"], [("finding_3", "anchor_ambiguous")],
    )


@pytest.mark.parametrize("findings,outputs,located_ids,named", [
    pytest.param([finding()], {"doc": "Falsch. Falsch."}, [],
                 [("finding_1", "anchor_ambiguous")], id="ambiguous"),
    pytest.param([finding()], {"doc": "Schon richtig."}, [],
                 [("finding_1", "anchor_missing")], id="missing"),
    pytest.param([finding("Falsch."), finding("Falsch. Ende")], {"doc": "Falsch. Ende"},
                 ["finding_1"], [("finding_2", "anchor_overlap")], id="overlap"),
    pytest.param([finding(artifact_id="unknown")], {"doc": "Falsch."}, [],
                 [("finding_1", "finding_invalid")], id="unknown-artifact"),
    pytest.param([finding(target="chat", artifact_id="doc")], {"chat": "Falsch."}, [],
                 [("finding_1", "finding_invalid")], id="chat-with-artifact"),
    pytest.param([finding(kind="evidence_error")], {"doc": "Falsch."}, [],
                 [("finding_1", "basis_invalid")], id="evidence-without-source"),
    pytest.param([finding(kind="missing_information")], {"doc": "Falsch."}, [],
                 [("finding_1", "basis_invalid")], id="missing-information-without-source"),
    pytest.param([finding(basis_ids=["source:a"])], {"doc": "Falsch."}, [],
                 [("finding_1", "basis_invalid")], id="diagnostic-with-a-source-only"),
    pytest.param([finding(basis_ids=["invented"])], {"doc": "Falsch."}, [],
                 [("finding_1", "basis_invalid")], id="invented-basis"),
    pytest.param([finding(basis_ids=["invented"]), finding()], {"doc": "Falsch."},
                 ["finding_2"], [("finding_1", "basis_invalid")],
                 id="a-finding-named-before-its-anchor-claims-no-span"),
])
def test_every_locate_failure_names_its_finding_with_its_reason(
    findings, outputs, located_ids, named
):
    located, results = locate(findings, outputs)
    assert ([item["finding_id"] for item in located], [(r.finding_id, r.reason) for r in results]) == (
        located_ids, named,
    )


@pytest.mark.parametrize("bundle_edits,reason", [
    pytest.param([], "edit_not_single", id="no-edit"),
    pytest.param([{"finding_id": "unknown", "replacement": "Neu."}], "edit_not_single",
                 id="unknown-finding-only"),
    pytest.param([{"finding_id": "finding_1", "replacement": "Falsch."}], "edit_unchanged",
                 id="unchanged"),
    pytest.param([{"finding_id": "finding_1", "replacement": "Neu [W2]."}], "label_not_citable",
                 id="foreign-label"),
    pytest.param([{"finding_id": "finding_1", "replacement": "Neu."}] * 2, "edit_not_single",
                 id="two-edits"),
])
def test_an_invalid_edit_names_its_finding_and_changes_no_output(bundle_edits, reason):
    outputs = {"doc": "Falsch. Bleibt."}
    located, _ = locate([finding()], outputs)
    revised, results = apply_review_edits(
        outputs, located, DeepRevisionBundle(edits=bundle_edits),
        allowed_references={"doc": [{"label": "W1"}]}, identifiers=[],
    )
    assert (revised, [(r.state, r.reason) for r in results]) == (
        {"doc": "Falsch. Bleibt."}, [("named", reason)],
    )


def test_one_edit_with_a_forbidden_label_names_only_its_finding():
    """The valid edit beside it is applied."""
    outputs = {"doc": "Alpha. Beta."}
    located, _ = locate([finding("Alpha."), finding("Beta.")], outputs)
    revised, results = apply_review_edits(
        outputs, located, edits(located, "A2.", "B2 [K9]."),
        allowed_references={"doc": [{"label": "K1"}]}, identifiers=[],
    )
    assert (revised["doc"], [(r.finding_id, r.state, r.reason) for r in results]) == (
        "A2. Beta.",
        [("finding_1", "applied", ""), ("finding_2", "named", "label_not_citable")],
    )


@pytest.mark.parametrize("original,quotes,replacements,identifiers,named", [
    pytest.param("A.2 Punkte. B.", ("A.", "B."), ("Siehe doc-4", "C."), ["doc-42"],
                 [("finding_1", "identifier_added")], id="identifier-across-a-boundary"),
    pytest.param("## A\n\n## B", ("## A", "## B"), ("", ""), [],
                 [("finding_2", "output_emptied")], id="the-second-heading-removal-empties"),
])
def test_the_first_edit_that_flips_the_whole_text_is_named(
    original, quotes, replacements, identifiers, named
):
    """Each edit is checked on the text composed with the ones accepted
    before it, from the original snapshot."""
    outputs = {"doc": original}
    located, _ = locate([finding(quote) for quote in quotes], outputs)
    _revised, results = apply_review_edits(
        outputs, located, edits(located, *replacements),
        allowed_references={"doc": []}, identifiers=identifiers,
    )
    assert [(r.finding_id, r.reason) for r in results if r.state == "named"] == named
    assert len([r for r in results if r.state == "applied"]) == 1


def test_a_defect_already_standing_does_not_block_an_edit_elsewhere():
    """A user's ``[K3–K5]`` was malformed before the review; an edit of
    another sentence is judged by what IT adds."""
    outputs = {"doc": "Nutzer schrieb [K3–K5]. Falsch."}
    located, _ = locate([finding()], outputs)
    _revised, results = apply_review_edits(
        outputs, located, edits(located, "Richtig."),
        allowed_references={"doc": []}, identifiers=[],
    )
    assert results[0].state == "applied"


@pytest.mark.parametrize("original,quote,replacement", [
    pytest.param("A.9] B.", "A.", "A. [K", id="an-unknown-label-completed"),
    pytest.param("A. K1–K3] B.", "A.", "A. [", id="a-malformed-bracket-completed"),
])
def test_a_splice_that_completes_a_bracket_in_the_text_is_named(original, quote, replacement):
    """Each replacement reads clean on its own; the text it lands in does not."""
    assert outcome(finding(quote), original, replacement, references=[ref("K1", "k1")]) == (
        "named", "labels_whole_text",
    )


@pytest.mark.parametrize("original,replacement", [
    pytest.param("Alpha.\n\nBeta [K3].\n\nGamma [K5].",
                 "Alpha. Die Lesung endete vor Seite 12.\n\n```text", id="an-opened-fence"),
    pytest.param("Alpha. Beta [K3] mit `x` Ende.", "Alpha `neu.", id="a-lone-backtick"),
    pytest.param("Alpha. Beta `x [K3] y` Ende.", "Alpha `.", id="a-code-citation-becomes-one"),
])
def test_an_edit_that_changes_a_citation_it_left_untouched_is_named(original, replacement):
    """The replacement cites nothing, yet the untouched text after it cites
    differently once it is spliced in: into code, or out of it."""
    assert outcome(
        finding("Alpha."), original, replacement, references=[ref("K3", "k3"), ref("K5", "k5")],
    ) == ("named", "labels_whole_text")


@pytest.mark.parametrize("item,replacement,expected", [
    pytest.param(finding("A gilt."), "A gilt nicht.", ("named", "quote_label_dropped", None),
                 id="a-reword-keeps-the-label-run-of-its-passage"),
    pytest.param(finding("A gilt.", kind="evidence_error", basis_ids=["source:k7"],
                         remove_labels=["K12"]),
                 "A gilt nicht [K7].",
                 ("applied", "", "A gilt nicht [K7]. Weiter gilt B [K5]."),
                 id="a-wrong-label-after-the-period-can-be-removed"),
])
def test_a_citation_run_after_the_quote_belongs_to_its_passage(item, replacement, expected):
    """House style cites after the period: ``A gilt. [K12]``. A quote that
    ends at the period still owns that run, so the label neither slides onto
    the next sentence nor escapes a removal."""
    outputs = {"doc": "A gilt. [K12] Weiter gilt B [K5]."}
    allowed = {"doc": SOURCES}
    located, named = locate([item], outputs, allowed_references=allowed)
    assert (named, [entry["quote"] for entry in located]) == ([], ["A gilt. [K12]"]), (
        "precondition"
    )
    revised, results = apply_review_edits(
        outputs, located, edits(located, replacement),
        allowed_references=allowed, identifiers=[],
    )
    applied = revised["doc"] if results[0].state == "applied" else None
    assert (results[0].state, results[0].reason, applied) == expected


@pytest.mark.parametrize("original,first,second,adder", [
    pytest.param("A laut doc-42. B.", ("A laut doc-42.", "A korrigiert."),
                 ("B.", "B laut doc-42."), "finding_2", id="remover-first"),
    pytest.param("A. B laut doc-42.", ("A.", "A laut doc-42."),
                 ("B laut doc-42.", "B korrigiert."), "finding_1", id="adder-first"),
])
def test_an_identifier_move_is_refused_at_the_adding_edit_in_either_order(
    original, first, second, adder
):
    references = [{"label": "K1", "document_id": "doc-42", "chunk_index": 0, "title": "B"}]
    outputs = {"doc": original}
    located, _ = locate([finding(first[0]), finding(second[0])], outputs)
    _revised, results = apply_review_edits(
        outputs, located, edits(located, first[1], second[1]),
        allowed_references={"doc": references}, identifiers=writer_identifiers(references),
    )
    assert {r.finding_id: (r.state, r.reason) for r in results} == {
        adder: ("named", "identifier_added"),
        ({"finding_1", "finding_2"} - {adder}).pop(): ("applied", ""),
    }


SOURCES = [ref("K5", "k5"), ref("K7", "k7"), ref("K12", "k12"), ref("K13", "k13"), ref("K16", "k16")]


def test_a_new_segment_with_a_foreign_output_label_is_refused():
    """[K5] is a label of the output, yet neither in the passage nor in
    the finding's basis."""
    assert outcome(
        finding("X gilt.", kind="evidence_error", basis_ids=["source:k7"]),
        "X gilt. Rest [K5].", "X gilt nur mit Y [K7] [K5].", references=SOURCES,
    ) == ("named", "label_not_citable")


@pytest.mark.parametrize("item,output,replacement", [
    pytest.param(finding("Text.", kind="output_requirement", basis_ids=()),
                 "Text.", "## Neu\n\nText.", id="heading-without-a-basis"),
    pytest.param(finding("Frist 30 Tage [K1]."), "Frist 30 Tage [K1].",
                 "Frist 30 Tage [K1]. Die Lesung endete vor Seite 12.", id="limit-sentence"),
    pytest.param(finding("Die Übermittlung ist erlaubt. [K13]", kind="evidence_error",
                         basis_ids=["source:k13"]),
                 "Die Übermittlung ist erlaubt. [K13]",
                 "Die Übermittlung ist nur in Ausnahmefällen erlaubt. [K13]", id="qualifier"),
])
def test_structural_limit_and_qualifier_edits_are_valid(item, output, replacement):
    """A heading, a reading-limit sentence and a qualifier are valid edits
    that need no new basis label."""
    assert outcome(item, output, replacement, references=[ref("K1", "k1"), *SOURCES]) == (
        "applied", "",
    )


@pytest.mark.parametrize("item,output,replacement,expected", [
    pytest.param(finding("X gilt [K3].", kind="evidence_error", basis_ids=["source:k7"],
                         remove_labels=["K3"]),
                 "X gilt [K3].", "X gilt [K7].", ("applied", ""), id="removed-and-replaced"),
    pytest.param(finding("X gilt [K3].", kind="evidence_error", basis_ids=["source:k7"],
                         remove_labels=["[K3]"]),
                 "X gilt [K3].", "X gilt [K7].", ("applied", ""), id="removal-in-brackets"),
    pytest.param(finding("Y gilt [K5]."), "Y gilt [K5]. Rest.", "Y gilt.",
                 ("named", "quote_label_dropped"), id="a-label-dropped-without-removal"),
    pytest.param(finding("X gilt [K3].", kind="evidence_error", basis_ids=["source:k7"],
                         remove_labels=["K9"]),
                 "X gilt [K3].", "X gilt [K7].", ("named", "remove_labels_invalid"),
                 id="removal-not-in-the-passage"),
    pytest.param(finding("Z [K3] [K4].", kind="evidence_error", basis_ids=["source:k7"],
                         remove_labels=["K3"]),
                 "Z [K3] [K4].", "Z [K7].", ("named", "quote_label_dropped"),
                 id="a-kept-label-dropped"),
])
def test_label_removal_follows_remove_labels(item, output, replacement, expected):
    """A label leaves the passage only when the finding lists it in
    ``remove_labels``, and only a label the passage carries."""
    references = [ref("K3", "k3"), ref("K4", "k4"), *SOURCES]
    assert outcome(item, output, replacement, references=references) == expected


@pytest.mark.parametrize("quote,replacement,expected", [
    pytest.param("Alpha [K3]. Beta [K5].", "Alpha [K5]. Beta [K3].", ("named", "label_moved"),
                 id="swapped"),
    pytest.param("Alpha [K3]. Beta [K5].", "Alpha [K3]. Beta. Gamma [K5].",
                 ("named", "label_moved"), id="moved-onto-a-new-sentence"),
    pytest.param("Alpha, Beta [K3].", "Alpha. Beta [K3].", ("applied", ""),
                 id="a-split-sentence-keeps-it"),
])
def test_a_label_stays_on_the_sentence_it_stood_on(quote, replacement, expected):
    assert outcome(finding(quote), quote, replacement,
                   references=[ref("K3", "k3"), ref("K5", "k5")]) == expected


def test_an_escaped_label_is_no_citation_and_cannot_become_one():
    """``\\[K2\\]`` is text an editor stored, not a citation."""
    quote = "Siehe \\[K2\\] im Text."
    assert outcome(
        finding(quote, kind="evidence_error", basis_ids=["source:k7"]),
        quote, "Siehe [K2] im Text [K7].", references=[ref("K2", "k2"), *SOURCES],
    ) == ("named", "label_not_citable")


def test_an_unchanged_grouped_quote_is_unchanged():
    assert outcome(
        finding("Satz [K1, K2]."), "Satz [K1, K2]. Rest.", "Satz [K1, K2].",
        references=[ref("K1", "k1"), ref("K2", "k2")],
    ) == ("named", "edit_unchanged")


def test_an_E_label_is_an_unknown_citation_in_a_revision():
    """The desk renders [E7] as an agent citation; no ledger assigns one."""
    assert outcome(finding(), "Falsch.", "Neu [E7].") == ("named", "label_not_citable")


@pytest.mark.parametrize("item", [
    pytest.param(finding("A gilt [K12].", kind="evidence_error", basis_ids=["source:k16"]),
                 id="evidence-error"),
    pytest.param(finding("A gilt [K12].", kind="missing_information", basis_ids=["source:k16"]),
                 id="missing-information"),
    pytest.param(finding("A gilt [K12].", kind="output_requirement", basis_ids=()),
                 id="output-requirement-without-a-basis"),
])
def test_a_new_sentence_without_a_basis_label_is_named(item):
    """A new sentence ("Ergänzend regelt …") that rests on K16 and cites
    nothing is named."""
    replacement = (
        "A gilt [K12]. Neu ergänzt." if item.kind == "output_requirement"
        else "A gilt [K12]. Ergänzend regelt das Kapitel X."
    )
    assert outcome(item, "A gilt [K12].", replacement, references=SOURCES) == (
        "named", "segment_without_basis_label",
    )


def test_joining_two_sentences_of_the_passage_adds_no_new_sentence():
    """A fix adds the basis sentence with [K13] and joins the two old
    sentences with a semicolon; that join carries no new word, so the
    kept labels suffice and the fix is applied."""
    quote = "Die Frist läuft ab Kenntnis. Die Fristen gelten nur ausnahmsweise nicht. [K15]"
    replacement = (
        "Allgemein ist ohne unangemessene Verzögerung zu melden. [K13] Die Frist läuft "
        "ab Kenntnis; die Fristen gelten nur ausnahmsweise nicht. [K15]"
    )
    assert outcome(
        finding(quote, kind="missing_information", basis_ids=["source:k13"]),
        quote, replacement, references=[*SOURCES, ref("K15", "k15")],
    ) == ("applied", "")


def test_a_refused_new_sentence_is_logged_without_its_prose(caplog):
    """The log is English and names labels and counts; the reviser's prose
    stays out of it."""
    with caplog.at_level("WARNING", logger="inqtrix"):
        outcome(
            finding("A gilt [K12].", kind="evidence_error", basis_ids=["source:k16"]),
            "A gilt [K12].", "A gilt [K12]. Ergänzend regelt das Kapitel X.", references=SOURCES,
        )
    assert (
        "segment_without_basis_label (1 new sentence segment(s) without one of: K16)"
        in caplog.text,
        "Ergänzend" in caplog.text,
    ) == (True, False)


def test_a_heading_spliced_in_mid_line_is_no_heading():
    """``## Neu`` after "Alpha. " reads as text of that line, not as a heading."""
    assert outcome(
        finding("Beta.", kind="output_requirement", basis_ids=()),
        "Alpha. Beta.", "## Neu\n\nBeta.",
    ) == ("named", "segment_without_basis_label")


def test_a_heading_spliced_in_before_the_rest_of_a_line_is_no_heading():
    """``## Neu`` before " Beta [K3]." would take that cited sentence into
    the heading; there it reads as text of that line."""
    assert outcome(
        finding("Alpha.", kind="output_requirement", basis_ids=()),
        "Alpha. Beta [K3].", "Alpha.\n\n## Neu", references=[ref("K3", "k3")],
    ) == ("named", "segment_without_basis_label")


@pytest.mark.parametrize("kind,basis_ids,expected", [
    pytest.param("evidence_error", ["source:k7"], ("named", "segment_without_basis_label"),
                 id="a-fix-that-rests-on-sources"),
    pytest.param("run_diagnostic", ["diagnostic:b"], ("applied", ""), id="a-run-diagnostic"),
])
def test_new_code_is_a_new_segment_without_a_label(kind, basis_ids, expected):
    assert outcome(
        finding("X gilt.", kind=kind, basis_ids=basis_ids),
        "X gilt.", "X gilt [K7].\n\n```\nneu = 1\n```" if kind != "run_diagnostic"
        else "X gilt.\n\n```\nneu = 1\n```",
        references=SOURCES,
    ) == expected


@pytest.mark.parametrize("item,references,expected", [
    pytest.param(finding("X gilt.", kind="evidence_error", basis_ids=["source:k7"]),
                 [ref("K7", "k7"), ref("K7", "other")], ("named", "basis_outside_output"),
                 id="a-label-two-references-carry"),
    pytest.param(finding("X gilt.", basis_ids=["diagnostic:b", "source:a"]),
                 [ref("K7", "k7")], ("applied", ""),
                 id="a-run-diagnostic-cites-no-source"),
])
def test_a_basis_maps_only_to_an_unambiguous_label_of_its_target(item, references, expected):
    assert outcome(item, "X gilt.", "X gilt, laut Lesung.", references=references) == expected


@pytest.mark.parametrize("state,reason", [
    ("named", ""), ("named", "some_new_code"), ("applied", "anchor_missing"), ("open", ""),
])
def test_a_result_cannot_carry_a_reason_outside_the_closed_set(state, reason):
    with pytest.raises(ValueError, match="cannot carry the reason"):
        FindingResult(
            finding_id="finding_1", origin="model", output_id="doc", kind="run_diagnostic",
            criterion=None, finding="x", quote="y", state=state, reason=reason,
        )


def test_a_finding_record_carries_its_recheck():
    """Only an applied edit can have held its re-check, and the record says
    which applied edits held."""
    fields = {
        "finding_id": "finding_1", "origin": "model", "output_id": "doc",
        "kind": "run_diagnostic", "criterion": None, "finding": "x", "quote": "y",
    }
    record = FindingResult(**fields, state="applied", recheck="held").record()
    with pytest.raises(ValueError, match="cannot carry the re-check"):
        FindingResult(**fields, state="named", reason="anchor_missing", recheck="held")
    assert record["recheck"] == "held"


def test_a_finding_record_carries_why_its_edit_stayed_unverified():
    """An applied edit the re-check did not hold carries the reason a reader
    is told; a named finding, a held edit and a code outside the closed set
    cannot carry one."""
    fields = {
        "finding_id": "finding_1", "origin": "model", "output_id": "doc",
        "kind": "run_diagnostic", "criterion": None, "finding": "x", "quote": "y",
    }
    record = FindingResult(**fields, state="applied", recheck_reason="no_source_text").record()
    for refused in (
        {"state": "named", "reason": "anchor_missing", "recheck_reason": "too_large"},
        {"state": "applied", "recheck": "held", "recheck_reason": "inconclusive"},
        {"state": "applied", "recheck_reason": "no_basis"},
    ):
        with pytest.raises(ValueError, match="cannot carry the re-check reason"):
            FindingResult(**fields, **refused)
    assert record["recheck_reason"] == "no_source_text"


def test_missing_fact_and_editorial_criterion_take_different_paths():
    from inqtrix.agents.algorithm import _critic_open_points

    # The stored decision of a mission review: unmet criteria with their owner.
    payload = {
        "verdict": "research", "criteria_covered": [],
        "criteria_uncovered": [
            {"text": "Welcher Referenzzeitraum gilt?", "kind": "information"},
            {"text": "At most 350 words.", "kind": "output_requirement"},
        ],
    }
    assert uncovered_criteria(payload, kind="information") == ["Welcher Referenzzeitraum gilt?"]
    assert uncovered_criteria(payload, kind="output_requirement") == ["At most 350 words."]
    block = _critic_open_points(payload)
    assert "Welcher Referenzzeitraum gilt?" in block
    assert "350" not in block
    assert _critic_open_points({"criteria_uncovered": [payload["criteria_uncovered"][1]]}) == ""


def test_unclassified_history_never_becomes_research(caplog):
    old = {"criteria_uncovered": ["A free-text format or factual criterion."]}
    assert uncovered_criteria(old, kind="information") == []
    assert "has no valid classification" in caplog.text
    assert uncovered_criteria(old) == ["Unclassified: A free-text format or factual criterion."]


def test_context_check_uses_the_existing_estimate_without_trimming():
    llm = SimpleNamespace(context_window_tokens=3000, _default_max_tokens=100)
    assert review_context_fits(llm, model=None, prompt="small", schema="{}", fallback_window=10)
    assert not review_context_fits(llm, model=None, prompt="x" * 16000, schema="{}", fallback_window=10)


@pytest.mark.parametrize("criteria", [
    ["Unclassified persisted criterion."],
    [{"text": "At most 350 words.", "kind": "output_requirement"}],
])
def test_resumed_critic_route_cannot_research_without_an_information_gap(monkeypatch, criteria):
    import inqtrix.agents.algorithm as algorithm

    events = []
    # No planner, provider or store is supplied: the route must stop before
    # touching any of them, including when restoring an older checkpoint.
    deps = SimpleNamespace(cancelled=lambda: False, emit=lambda *args: events.append(args))
    monkeypatch.setattr(algorithm, "_deps", lambda: deps)
    state = {"route": "critic_research", "critic": {"criteria_uncovered": criteria}}
    result = algorithm._node_plan(state)
    assert result["route"] == "critic_research_exhausted"
    assert events[0][1]["kind"] == "critic_research_exhausted"


@pytest.mark.parametrize("initial_revision,concurrent_edit", [(1, False), (4, False), (4, True)])
def test_deep_revision_uses_the_reviewed_version_and_preserves_concurrent_user_work(initial_revision, concurrent_edit, monkeypatch):
    pytest.importorskip("deepagents")
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel import algorithm as kernel_algorithm
    from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm
    from inqtrix.agents.kernel.review_pass import Review
    from inqtrix.agents.kernel.deps import KernelDeps, run_coro
    from inqtrix.agents.phase_models import DeepReviewVerdict
    from tests.agents.test_kernel_deep_mode import StructuredScriptedLLM
    from tests.agents.test_review_pass import unquoted

    store = MemoryAgentControlStore()

    def write(body, author="agent"):
        return run_coro(store.upsert_artifact(
            run_id="run_review", session_id="session_review", kind="deliverable",
            title="Dokument", status="ready", content_markdown=body,
            payload={"retained": True}, refs=[], updated_by=author, artifact_id="doc",
        ))

    for revision in range(initial_revision):
        record = write(f"# Dokument\n\nStand {revision}.\n\nFehler.")
    assert record.revision == initial_revision
    original = record.content_markdown

    class ReviewingLLM(StructuredScriptedLLM):
        def complete_structured(self, prompt, *, schema_name, **kwargs):
            if schema_name == "DeepRevisionBundle" and concurrent_edit:
                write("# Dokument\n\nNeue Nutzerfassung.", "user")
            return super().complete_structured(prompt, schema_name=schema_name, **kwargs)

    # The verdict arrives from the review role; this test is about what the
    # revision does with it, so the role is stubbed at its seam.
    verdict = DeepReviewVerdict.model_validate(
        {"complete": False, "grounded": True, "contradictions_named": True,
         "findings": [{"target": "artifact", "artifact_id": "doc", "finding": "Gliederung ergänzen.",
                       "kind": "output_requirement", "quote": "Fehler.", "basis_ids": []}]})
    llm = ReviewingLLM([], [
        {"edits": [{"finding_id": "finding_1", "replacement": "## Korrektur\n\nFehler."}]},
        unquoted("finding_1"),
    ])
    events = []
    deps = KernelDeps(run_id="run_review", session_id="session_review", control=store,
                      platform=None, llm=llm, model="high-model", reasoning_effort="high",
                      timeout=1, event_sink=lambda event, payload: events.append((event, payload)))
    monkeypatch.setattr(
        kernel_algorithm, "request_review",
        lambda host, subject, **kwargs: Review(verdict=verdict),
    )
    owner = SimpleNamespace(
        _control=store, _user_message=lambda request, deps: "Dokument verbessern.",
    )
    answer = KernelAgentAlgorithm._deep_verify(owner, deps, None, "Chat bleibt.", [record], messages=[])
    stored, _ = run_coro(store.get_artifact("run_review", "doc"))
    assert answer == "Chat bleibt."
    assert stored.revision == initial_revision + 1
    assert stored.payload == {"retained": True}
    assert stored.content_markdown == (
        "# Dokument\n\nNeue Nutzerfassung." if concurrent_edit
        else original.replace("Fehler.", "## Korrektur\n\nFehler.")
    )
    # The run's language is unresolved here, so the summary reads in English;
    # the run holds no source, so it is reviewed from general knowledge and
    # nothing is re-checked.
    assert [
        payload["text"] for _, payload in events
        if payload.get("narration_id") == "kernel_deep_review" and payload.get("final")
    ] == [
        "Verification pass: Reviewed from general knowledge, without sources · "
        "1 finding not applied" if concurrent_edit
        else "Verification pass: Reviewed from general knowledge, without sources · "
        "1 finding revised"
    ]
    # The applied finding is named after all, with the conflict as its reason.
    assert [
        payload["detail"] for _, payload in events
        if payload.get("kind") == "critic_revision_discarded"
    ] == (
        ["Finding not applied (the document changed meanwhile): Gliederung ergänzen."]
        if concurrent_edit else []
    )


def test_the_revision_evidence_is_narrowed_to_the_bases_the_findings_name():
    from inqtrix.agents.kernel.evidence_read import narrow_review_evidence

    evidence = {
        "sources": [
            {"basis_id": "source:ref_a", "citation_contexts": [{"query_id": "q1"}]},
            {"basis_id": "source:ref_b", "citation_contexts": [{"query_id": "q2"}]},
        ],
        "searches": {"q1": {"provider_answer": "A"}, "q2": {"provider_answer": "B"}},
        "run_diagnostics": [{"basis_id": "diagnostic:run_1"}, {"basis_id": "diagnostic:run_2"}],
        "unavailable_origins": [{"source_run_id": "run_gone"}],
    }
    narrowed = narrow_review_evidence(evidence, ["source:ref_b", "diagnostic:run_1"])
    assert [item["basis_id"] for item in narrowed["sources"]] == ["source:ref_b"]
    assert narrowed["searches"] == {"q2": {"provider_answer": "B"}}
    assert narrowed["run_diagnostics"] == [{"basis_id": "diagnostic:run_1"}]
    # What could not be read stays named; the input is not mutated.
    assert narrowed["unavailable_origins"] == evidence["unavailable_origins"]
    assert len(evidence["sources"]) == 2


def test_review_revision_cannot_introduce_an_internal_reference_id():
    outputs = {"doc": "Old statement [W1]."}
    references = [{"label": "W1", "reference_id": "opaque-evidence-key"}]
    located = [{"finding_id": "finding_1", "origin": "model", "output_id": "doc",
                "kind": "run_diagnostic", "finding": "Fix.", "criterion": None,
                "quote": "Old statement", "start": 0, "end": len("Old statement"),
                "keep": [], "remove": [], "may_add": []}]
    bundle = DeepRevisionBundle(edits=[{
        "finding_id": "finding_1", "replacement": "Revised opaque-evidence-key statement",
    }])
    revised, results = apply_review_edits(
        outputs, located, bundle, allowed_references={"doc": references},
        identifiers=writer_identifiers(references),
    )
    assert (revised, [r.reason for r in results]) == (outputs, ["identifier_added"])


def test_a_revision_may_keep_but_not_introduce_an_identifier():
    """A revision owns its replacement, not the passage it found.

    A guard that read only the references' ``reference_id`` would check
    the mission's revisions against empty strings, because a mission
    reference has none. With the full identifier set the check works,
    and it has to tell an id the edit ADDS from one it inherited with the
    sentence it is rewriting.

    The unit is the whole text a reader sees, counted: an id already
    standing is not this revision's to remove, but one standing occurrence
    may not license a second. A set comparison answers "yes, it was there"
    to both and is therefore no guard at all.
    """
    references = [{
        "label": "K1", "document_id": "doc-42", "chunk_index": 0,
        "title": "Bericht", "revision_id": "rev-7",
    }]
    outputs = {"doc": "Falsch laut doc-42. Steht in doc-42."}
    located, _ = locate([finding(quote="Falsch laut doc-42.")], outputs)

    def revise(replacement, user_text=""):
        return apply_review_edits(
            outputs, located, edits(located, replacement),
            allowed_references={"doc": references},
            identifiers=writer_identifiers(references), user_text=user_text,
        )

    kept, _ = revise("Korrigiert laut doc-42.")
    assert kept["doc"] == "Korrigiert laut doc-42. Steht in doc-42."

    # One standing occurrence, two after the edit: that second one is new.
    assert [r.reason for r in revise("Korrigiert laut doc-42, siehe doc-42.")[1]] == [
        "identifier_added"
    ]
    assert [r.reason for r in revise("Korrigiert laut rev-7.")[1]] == ["identifier_added"]

    # The user's own words count: an id THEY named is theirs.
    assert revise(
        "Korrigiert laut rev-7.", user_text="Pruefe die Fassung rev-7."
    )[0]["doc"].startswith("Korrigiert laut rev-7.")


def test_the_revision_guard_reads_the_users_words_not_the_composed_assignment():
    """The assignment is a message the server composed, not the user's text.

    The kernel's version renders every released collection as
    ``name -> id``. Handing it to the guard as "what the user wrote" would
    exempt every collection id of the run from the very check the revision
    has to pass - and a run with project knowledge always has one.
    """
    from inqtrix.agents.kernel import review_pass

    subject = review_pass.ReviewSubject(
        assignment=(
            "Knowledge collections released for this run (name -> id):\n"
            "- Marktberichte -> kc_2f1a9b3c4d5e (3 documents)"
        ),
        outputs={"doc": "Falsch laut doc-42."},
        canvases=(),
        allowed_references={"doc": []},
        ledger={},
        cited=[],
        evidence={},
        evidence_for=lambda refs: {},
        diagnostic_ids={},
        evidence_run_id="run_x",
    )
    assert "kc_2f1a9b3c4d5e" not in subject.user_text

    references = [{"label": "K1", "collection_id": "kc_2f1a9b3c4d5e", "title": "Bericht"}]
    outputs = {"doc": "Falsch laut doc-42."}
    located, _ = locate([finding(quote="Falsch laut doc-42.")], outputs)
    _revised, results = apply_review_edits(
        outputs, located, edits(located, "Korrigiert, Sammlung kc_2f1a9b3c4d5e."),
        allowed_references={"doc": references},
        identifiers=writer_identifiers(references),
        user_text=subject.user_text,
    )
    assert [r.reason for r in results] == ["identifier_added"]


# -- content stays; a removed heading takes its line end -------------------- #

TABLE = "| Punkt | Frist |\n|---|---|\n| r1 | eins |\n| r2 | zwei |\n| r3 | drei |\n| r4 | vier |\n"


def apply_empty(output: str, *quotes: str) -> tuple[str, list[FindingResult]]:
    """*output* and the results after an empty replacement of each quote
    (one finding each)."""
    outputs = {"doc": output}
    located, named = locate([finding(quote) for quote in quotes], outputs)
    assert named == []
    revised, results = apply_review_edits(
        outputs, located, edits(located, *([""] * len(quotes))),
        allowed_references={"doc": []}, identifiers=[],
    )
    return revised["doc"], results


@pytest.mark.parametrize(("output", "quotes"), [
    (TABLE, ("| r2 | zwei |",)),
    (TABLE, ("| r2 | zwei |", "| r3 | drei |")),
    ("- a\n- b\n- c", ("- b",)),
    ("Eins.\nZwei.", ("Zwei.",)),
    ("| r1 |\r\n| r2 |\r\n| r3 |", ("| r2 |",)),
    ("Eins.\nZwei.\nDrei.", ("Zwei.", "Drei.")),
    ("Vorher.\n\nMitte.\n\nNachher.", ("Mitte.",)),
    ("Satz eins. Satz zwei.\nNeue Zeile.", ("Satz zwei.",)),
], ids=["inner-row", "two-rows", "list-item", "last-line", "crlf-row", "last-two-lines",
        "a-paragraph", "a-sentence-inside-a-line"])
def test_an_empty_replacement_of_content_is_named_and_the_output_keeps_it(output, quotes):
    """A table row, a list item, a line and a sentence are content: the
    review corrects or qualifies them, it never removes them, so an empty
    replacement of one is named and the output stays as it was."""
    revised, results = apply_empty(output, *quotes)
    assert (revised, {(r.state, r.reason) for r in results}) == (
        output, {("named", "edit_deletes")},
    )


@pytest.mark.parametrize(("output", "heading", "expected"), [
    ("## Alt\nText.", "## Alt", "Text."),
    ("Text.\n## Ende", "## Ende", "Text."),
    ("Text.\r\n## Alt\r\nMehr.", "## Alt", "Text.\r\nMehr."),
], ids=["first-line", "last-line", "crlf"])
def test_a_removed_heading_takes_its_line_end_with_it(output, heading, expected):
    """A heading is structure, not content: it may go without a replacement,
    and its line end goes with it, so no blank line is left behind."""
    revised, results = apply_empty(output, heading)
    assert (revised, [r.state for r in results]) == (expected, ["applied"])


def test_removing_a_heading_between_paragraphs_never_merges_them():
    revised, _ = apply_empty("Vorher.\n\n## Mitte\n\nNachher.", "## Mitte")
    assert revised == "Vorher.\n\n\nNachher."


def test_the_recheck_finds_a_later_replacement_where_the_revised_text_holds_it():
    """The re-check places each replacement in the delivered text with the
    same spans the revision spliced: a removed heading line before it
    shifts it by its line end too, down to the text beside it on its own
    line."""
    from inqtrix.agents.kernel.review_edits import placement
    from inqtrix.agents.kernel.review_pass import _placements

    outputs = {"doc": "Erste Zeile.\n## Weg damit\nSatz A und Satz B."}
    located, _ = locate([finding("## Weg damit"), finding("Satz B.")], outputs)
    revised, _ = apply_review_edits(
        outputs, located, edits(located, "", "Satz C."),
        allowed_references={"doc": []}, identifiers=[],
    )
    assert revised["doc"] == "Erste Zeile.\nSatz A und Satz C."
    replacements = {"finding_1": "", "finding_2": "Satz C."}
    placed = _placements(located, [located[1]], revised, replacements, originals=outputs)
    start = revised["doc"].index("Satz C.")
    assert placed["finding_2"] == placement(revised["doc"], start, start + len("Satz C."))
    assert placed["finding_2"].text_before == "Satz A und"


def test_two_removed_lines_never_share_a_line_end():
    """The spans of an output's edits never overlap: when the line before the
    text's last line is removed with its line end, the last line keeps no
    line end of its own to take."""
    from inqtrix.agents.kernel.review_edits import edit_span

    text = "Eins.\nZwei.\nDrei."
    first = edit_span(text, 6, 11, "")
    assert first == (6, 12)
    assert edit_span(text, 12, 17, "", after=first[1]) == (12, 17)
    assert edit_span(text, 12, 17, "") == (11, 17)


# -- a revision never removes content -------------------------------------- #

ADVICE = (
    "Ein Router, der nach Stunden die Verbindung verliert, ist meist kein Totalausfall.\n\n"
    "## Ursachen\n\n"
    "Oft überhitzt das Gerät in einem engen Regal. Auch eine veraltete Firmware "
    "trennt nach langer Laufzeit die Verbindung.\n\n"
    "## Einschätzung\n\n"
    "Am wahrscheinlichsten ist Wärme, verstärkt durch eine alte Firmware.\n\n"
    "## Vorgehen\n\n"
    "Stell den Router frei auf. Spiel danach die aktuelle Firmware ein.\n"
)
ADVICE_BODIES = (
    "Oft überhitzt das Gerät in einem engen Regal. Auch eine veraltete Firmware "
    "trennt nach langer Laufzeit die Verbindung.",
    "Am wahrscheinlichsten ist Wärme, verstärkt durch eine alte Firmware.",
    "Stell den Router frei auf. Spiel danach die aktuelle Firmware ein.",
)
LONG = (
    "Die Firmware sollte regelmäßig aktualisiert werden, weil ältere Versionen nach "
    "langer Laufzeit Speicher verlieren und dann die Funkverbindung ohne Warnung "
    "trennen, besonders wenn viele Geräte gleichzeitig verbunden sind."
)
"""Twenty-eight words: a passage long enough that a remnant of it is a cut."""


def test_a_revision_never_empties_an_answer_section_by_section():
    """A review without any source raised one finding per section, each
    asking to delete it; the guard against an empty output never fired
    because the opening paragraph stayed. No section loses its text: every
    empty replacement is named and the answer stays as it was."""
    outputs = {"doc": ADVICE}
    located, named = locate(
        [finding(body, kind="output_requirement", basis_ids=()) for body in ADVICE_BODIES],
        outputs,
    )
    assert named == []
    revised, results = apply_review_edits(
        outputs, located, edits(located, "", "", ""),
        allowed_references={"doc": []}, identifiers=[],
    )
    assert (revised, [(r.state, r.reason) for r in results]) == (
        outputs, [("named", "edit_deletes")] * 3,
    )


@pytest.mark.parametrize("item,output,replacement,expected", [
    pytest.param(finding("Eins ist so. Zwei ist so. Drei ist so."),
                 "Eins ist so. Zwei ist so. Drei ist so.", "Eins ist so.",
                 ("named", "edit_deletes"), id="a-kept-sentence-beside-dropped-ones"),
    pytest.param(finding("Der Satz bleibt wichtig."), "Der Satz bleibt wichtig. Rest.", "—",
                 ("named", "edit_deletes"), id="a-mark-for-a-sentence"),
    pytest.param(finding("Der Satz bleibt wichtig."),
                 "## Teil\n\nDer Satz bleibt wichtig.\n\n## Weiter\n\nRest.", "### Neu",
                 ("named", "edit_deletes"), id="a-heading-for-the-only-sentence-of-a-section"),
    pytest.param(finding("X gilt [K3].", kind="evidence_error", basis_ids=["source:k7"],
                         remove_labels=["K3"]),
                 "X gilt [K3]. Rest.", "", ("named", "edit_deletes"),
                 id="a-passage-whose-labels-all-go"),
    pytest.param(finding(LONG), LONG, "Die Firmware sollte aktuell sein.",
                 ("named", "edit_shrinks"), id="a-long-passage-cut-to-a-remnant"),
])
def test_a_replacement_that_drops_content_is_named(item, output, replacement, expected):
    """A revision corrects, qualifies or attributes: it never drops a
    sentence, never turns one into a heading or a mark, and never cuts a
    long passage to a remnant of it."""
    assert outcome(item, output, replacement, references=[ref("K3", "k3"), *SOURCES]) == expected


@pytest.mark.parametrize("item,output,replacement", [
    pytest.param(finding(LONG), LONG,
                 "Die Firmware sollte regelmäßig aktualisiert werden, weil ältere Versionen "
                 "nach langer Laufzeit Speicher verlieren und die Verbindung dann ohne Warnung "
                 "trennen.", id="a-long-passage-tightened"),
    pytest.param(finding("Das gilt immer."), "Das gilt immer. Rest.", "Das gilt meist.",
                 id="a-short-sentence-reworded"),
    pytest.param(finding("Das gilt."), "Das gilt. Rest.", "Das gilt nur unter einer Bedingung.",
                 id="a-sentence-qualified"),
])
def test_a_revision_may_tighten_and_reword(item, output, replacement):
    """Seventy-five words of a hundred are a tightening, not a cut; a short
    sentence may change freely as long as it stays a sentence."""
    assert outcome(item, output, replacement) == ("applied", "")


def paragraphs(*names: str) -> list[str]:
    """One paragraph of twenty-two words per name, each unique."""
    return [
        f"Absatz {name} sagt etwas über das Gerät, das im Alltag genutzt wird und "
        "deshalb zuverlässig laufen sollte, ohne dass jemand eingreifen muss."
        for name in names
    ]


@pytest.mark.parametrize("names,states", [
    pytest.param(("eins", "zwei", "drei", "vier", "fünf", "sechs"),
                 ["applied", "applied", "output_shrunk", "output_shrunk"],
                 id="a-long-output-keeps-three-quarters"),
    pytest.param(("eins", "zwei", "drei", "vier"),
                 ["applied", "applied", "applied", "applied"],
                 id="a-short-output-has-no-bound"),
])
def test_many_small_cuts_stop_at_the_bound_of_the_output(names, states):
    """No cut is a remnant on its own (each passage is short), yet together
    they would take more than a quarter of a long output: the first edit
    that would cross that bound is named, and every one after it."""
    texts = paragraphs(*names)
    outputs = {"doc": "\n\n".join(texts)}
    located, _ = locate([finding(text) for text in texts[:4]], outputs)
    _revised, results = apply_review_edits(
        outputs, located,
        edits(located, *(f"Absatz {name} sagt etwas über das Gerät." for name in names[:4])),
        allowed_references={"doc": []}, identifiers=[],
    )
    assert [r.reason or r.state for r in results] == states


# -- knowledge mode: no source to cite -------------------------------------- #


@pytest.mark.parametrize("knowledge_mode,replacement,expected", [
    pytest.param(True, "Das gilt nur für ältere Geräte.", ("applied", ""),
                 id="a-reword-from-general-knowledge"),
    pytest.param(False, "Das gilt nur für ältere Geräte.",
                 ("named", "segment_without_basis_label"), id="a-reword-in-a-run-with-sources"),
    pytest.param(True, "", ("named", "edit_deletes"), id="knowledge-deletes-nothing-either"),
])
def test_knowledge_mode_lifts_the_label_rule_and_nothing_else(knowledge_mode, replacement, expected):
    """A run without any stored evidence has no label to cite, so a fix from
    general knowledge may reword a sentence without one; it still never
    removes content. Only the explicit mode lifts the rule: an empty basis
    in a run with sources does not."""
    outputs = {"chat": "Das gilt immer. Rest."}
    item = finding(
        "Das gilt immer.", target="chat", artifact_id="", kind="output_requirement", basis_ids=(),
    )
    located, named = locate_findings(
        [("model", item)], outputs,
        source_basis_ids={"chat": set()}, diagnostic_basis_ids={"chat": set()},
        allowed_references={"chat": []}, criteria_count=0, knowledge_mode=knowledge_mode,
    )
    assert named == []
    _revised, results = apply_review_edits(
        outputs, located, edits(located, replacement),
        allowed_references={"chat": []}, identifiers=[],
    )
    assert (results[0].state, results[0].reason) == expected


# -- sources without stored text: a finding marks, an edit keeps sentences and numbers -- #

WEB = [ref("W7", "w7"), ref("W8", "w8"), ref("W11", "w11")]
UNSTORED = frozenset({"source:w7", "source:w8", "source:w11"})
"""Every source of these outputs is the search provider's context alone."""


def locate_web(findings, outputs, *, unstored=UNSTORED, knowledge_mode=False):
    """Locate model findings on outputs citing WEB, *unstored* holding no stored text."""
    return locate_findings(
        [("model", item) for item in findings], outputs,
        source_basis_ids={key: {f"source:{KernelDeps.ledger_id(r)}" for r in WEB} for key in outputs},
        diagnostic_basis_ids={key: {"diagnostic:b"} for key in outputs},
        allowed_references={key: list(WEB) for key in outputs},
        criteria_count=0,
        unstored_basis_ids=unstored,
        knowledge_mode=knowledge_mode,
    )


def web_outcome(item, output, replacement, *, unstored=UNSTORED, knowledge_mode=False):
    """``(state, reason)`` of one finding on ``{"doc": output}`` citing WEB."""
    outputs = {"doc": output}
    located, named = locate_web([item], outputs, unstored=unstored, knowledge_mode=knowledge_mode)
    if named:
        return named[0].state, named[0].reason
    _revised, results = apply_review_edits(
        outputs, located, edits(located, replacement),
        allowed_references={"doc": list(WEB)}, identifiers=[],
    )
    return results[0].state, results[0].reason


BONUS = "Der Bonus beträgt 16 Prozent. [W7]\n\nDer Antrag läuft über die KfW. [W8]\n"


def test_a_finding_resting_only_on_its_passages_provider_context_marks_the_passage():
    """The review cannot read what the search provider's summary left out:
    a statement that summary does not carry is marked for the reader, never
    reworded, and no revision is asked for it."""
    item = finding("Der Bonus beträgt 16 Prozent.", kind="evidence_error", basis_ids=["source:w7"])
    located, named = locate_web([item], {"doc": BONUS})
    assert located == []
    assert [(r.state, r.reason) for r in named] == [("named", "provider_context_only")]


def test_a_marked_passage_claims_its_span_against_a_later_finding():
    marked = finding("Der Bonus beträgt 16 Prozent.", kind="evidence_error", basis_ids=["source:w7"])
    later = finding("beträgt 16 Prozent.", kind="output_requirement", basis_ids=["source:w7"])
    located, named = locate_web([marked, later], {"doc": BONUS})
    assert located == []
    assert [r.reason for r in named] == ["provider_context_only", "anchor_overlap"]


@pytest.mark.parametrize("item,unstored,knowledge_mode", [
    pytest.param(finding("Der Bonus beträgt 16 Prozent.", kind="evidence_error",
                         basis_ids=["source:w7", "source:w11"]), UNSTORED, False,
                 id="a-source-the-passage-does-not-cite"),
    pytest.param(finding("Der Bonus beträgt 16 Prozent.", kind="evidence_error",
                         basis_ids=["source:w7"]), frozenset({"source:w8"}), False,
                 id="a-source-with-stored-text"),
    pytest.param(finding("Der Bonus beträgt 16 Prozent."), UNSTORED, False,
                 id="a-run-diagnostic"),
    pytest.param(finding("Der Bonus beträgt 16 Prozent.", kind="output_requirement",
                         basis_ids=["source:w7"]), UNSTORED, False,
                 id="an-output-requirement"),
])
def test_only_an_evidence_finding_on_its_passages_own_provider_context_is_marked(
    item, unstored, knowledge_mode,
):
    located, named = locate_web([item], {"doc": BONUS}, unstored=unstored, knowledge_mode=knowledge_mode)
    assert named == []
    assert len(located) == 1


def test_a_marked_finding_is_no_revision_decision():
    """A marked passage was never handed to the revision: it counts neither
    as applied nor as discarded."""
    from inqtrix.agents.kernel.review_edits import is_counted
    from inqtrix.agents.kernel.review_pass import revision_state

    item = finding("Der Bonus beträgt 16 Prozent.", kind="evidence_error", basis_ids=["source:w7"])
    _located, named = locate_web([item], {"doc": BONUS})
    assert not is_counted("model", named[0].reason)
    assert revision_state(named) == "none"


@pytest.mark.parametrize("replacement,expected", [
    pytest.param("Der Antrag läuft über die KfW. [W11]", ("applied", ""),
                 id="the-label-moves-to-the-carrying-source"),
    pytest.param("Der Antrag läuft über die KfW. [W8] Online geht er über das Portal. [W11]",
                 ("applied", ""), id="a-sentence-with-its-own-label-is-added"),
    pytest.param("Den Antrag nimmt die KfW entgegen. [W8] [W11]",
                 ("named", "edit_rewrites_unchecked"), id="the-sentence-is-reworded"),
])
def test_a_fix_resting_on_sources_without_stored_text_keeps_every_sentence(replacement, expected):
    """Without stored text nothing can confirm a reworded statement: the fix
    may move the label to the source that carries it and add a sentence of
    its own, it never rewords the passage."""
    item = finding(
        "Der Antrag läuft über die KfW.", kind="evidence_error",
        basis_ids=["source:w8", "source:w11"],
        remove_labels=["W8"] if "[W8]" not in replacement else [],
    )
    assert web_outcome(item, BONUS, replacement) == expected


def test_a_fix_citing_stored_text_may_reword():
    item = finding(
        "Der Antrag läuft über die KfW.", kind="evidence_error",
        basis_ids=["source:w8", "source:w11"],
    )
    replacement = "Den Antrag nimmt die KfW entgegen. [W8] [W11]"
    assert web_outcome(item, BONUS, replacement, unstored=frozenset({"source:w8"})) == ("applied", "")


def test_a_fix_keeping_a_stored_source_of_its_passage_goes_to_the_recheck_instead():
    """The passage also cites a source with stored text, and the fix keeps
    citing it: the re-check can read what stands behind the new wording, so
    the sentence rule does not hold it back."""
    output = "Der Antrag läuft über die KfW. [W7] [W8]\n"
    item = finding(
        "Der Antrag läuft über die KfW.", kind="evidence_error",
        basis_ids=["source:w7", "source:w11"],
    )
    replacement = "Den Antrag nimmt die KfW entgegen. [W7] [W8] [W11]"
    unstored = frozenset({"source:w7", "source:w11"})
    assert web_outcome(item, output, replacement, unstored=unstored) == ("applied", "")


def test_a_fix_citing_stored_text_is_checked_against_it_not_by_its_numbers():
    """A replacement citing a source with stored text goes to the re-check,
    which reads that text; the number rule holds only where nothing could."""
    output = "Der Bonus beträgt 16 Prozent. [W7] [W8]\n"
    item = finding("Der Bonus beträgt 16 Prozent.", kind="output_requirement", basis_ids=["source:w8"])
    replacement = "Der Bonus ist neu geregelt. [W7] [W8]"
    assert web_outcome(item, output, replacement, unstored=frozenset({"source:w7"})) == ("applied", "")


@pytest.mark.parametrize("output,quote,replacement,expected", [
    pytest.param("Die Förderung beträgt USD 8.400; gemeint sind 8.400 Euro. [W8]\n",
                 "Die Förderung beträgt USD 8.400; gemeint sind 8.400 Euro.",
                 "Die Förderung beträgt 8.400 Euro. [W8]", ("applied", ""),
                 id="a-currency-slip-is-corrected"),
    pytest.param("Rechnerisch $0{,}30 \\times 28{.}000 = 8{.}400$ Euro. [W8]\n",
                 "Rechnerisch $0{,}30 \\times 28{.}000 = 8{.}400$ Euro.",
                 "Rechnerisch 0,30 × 28.000 Euro = 8.400 Euro. [W8]", ("applied", ""),
                 id="a-formula-becomes-plain-text"),
    pytest.param("Der Bonus beträgt 16 Prozent ab dem 21.07.2026. [W8]\n",
                 "Der Bonus beträgt 16 Prozent ab dem 21.07.2026.",
                 "Der Bonus beträgt 16 Prozent ab dem 21. Juli 2026. [W8]", ("applied", ""),
                 id="a-date-is-written-out"),
    pytest.param("Der Bonus beträgt 16 Prozent. [W8]\n", "Der Bonus beträgt 16 Prozent.",
                 "Der Bonus ist für 2026 neu geregelt. [W8]", ("named", "edit_drops_figure"),
                 id="a-number-is-dropped"),
])
def test_a_fix_resting_on_sources_without_stored_text_keeps_every_number(
    output, quote, replacement, expected,
):
    """A number the provider's summary may not repeat is no wrong number: a
    fix without stored text keeps every number of its passage, however it
    writes it."""
    item = finding(quote, kind="output_requirement", basis_ids=["source:w8"])
    assert web_outcome(item, output, replacement) == expected


@pytest.mark.parametrize("replacement,expected", [
    pytest.param("Der Druck beträgt 2,8 bar.", ("applied", ""), id="a-number-is-corrected"),
    pytest.param("Der Druck ist nach Angabe einzustellen.", ("named", "edit_drops_figure"),
                 id="a-number-is-dropped"),
])
def test_a_correction_from_general_knowledge_may_change_a_number_never_drop_one(replacement, expected):
    outputs = {"chat": "Der Druck beträgt 2,5 bar. Rest."}
    item = finding(
        "Der Druck beträgt 2,5 bar.", target="chat", artifact_id="",
        kind="output_requirement", basis_ids=(),
    )
    located, named = locate_findings(
        [("model", item)], outputs,
        source_basis_ids={"chat": set()}, diagnostic_basis_ids={"chat": set()},
        allowed_references={"chat": []}, criteria_count=0, knowledge_mode=True,
    )
    assert named == []
    _revised, results = apply_review_edits(
        outputs, located, edits(located, replacement),
        allowed_references={"chat": []}, identifiers=[],
    )
    assert (results[0].state, results[0].reason) == expected


def test_the_reviser_is_told_which_findings_keep_their_sentences():
    from inqtrix.agents.prompts import revision_frame_line

    item = finding(
        "Der Antrag läuft über die KfW.", kind="evidence_error", basis_ids=["source:w8", "source:w11"],
    )
    located, _ = locate_web([item], {"doc": BONUS})
    assert "sentences word for word" in revision_frame_line(located[0])
    stored, _ = locate_web([item], {"doc": BONUS}, unstored=frozenset())
    assert "sentences word for word" not in revision_frame_line(stored[0])
