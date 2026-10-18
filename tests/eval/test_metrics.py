"""Offline unit tests for the eval metric math and golden-set hygiene."""

from __future__ import annotations

import pytest

from tests.eval.harness import (
    GoldenQuery,
    QueryResult,
    load_corpus,
    load_queries,
    mean_reciprocal_rank,
    ndcg_at_k,
    recall_at_k,
    retrieval_queries,
)


def make_result(
    relevant: tuple[str, ...], ranked: tuple[str, ...], *, qid: str = "q"
) -> QueryResult:
    return QueryResult(
        query=GoldenQuery(id=qid, query="?", relevant=relevant, category="fact"),
        ranked_doc_ids=ranked,
    )


# ------------------------------------------------------------------ #
# Metric math against hand-computed values
# ------------------------------------------------------------------ #


def test_recall_at_k_counts_any_relevant_hit():
    results = [
        make_result(("a",), ("a", "b", "c")),     # hit at 1
        make_result(("a",), ("b", "a", "c")),     # hit at 2
        make_result(("a",), ("b", "c", "d")),     # miss
    ]
    assert recall_at_k(results, 1) == pytest.approx(1 / 3)
    assert recall_at_k(results, 2) == pytest.approx(2 / 3)
    assert recall_at_k(results, 5) == pytest.approx(2 / 3)


def test_mrr_uses_first_relevant_rank():
    results = [
        make_result(("a",), ("a", "b")),          # 1/1
        make_result(("a",), ("b", "c", "a")),     # 1/3
        make_result(("a",), ("b", "c")),          # 0
    ]
    assert mean_reciprocal_rank(results) == pytest.approx((1 + 1 / 3 + 0) / 3)


def test_ndcg_at_k_hand_computed():
    # Single relevant doc at rank 2: DCG = 1/log2(3), IDCG = 1/log2(2).
    single = [make_result(("a",), ("b", "a"))]
    expected = (1 / 1.5849625007211562) / 1.0
    assert ndcg_at_k(single, 5) == pytest.approx(expected, abs=1e-9)

    # Two relevant docs in ideal order: nDCG == 1.
    perfect = [make_result(("a", "b"), ("a", "b", "c"))]
    assert ndcg_at_k(perfect, 5) == pytest.approx(1.0)


def test_metrics_on_empty_results_are_zero():
    assert recall_at_k([], 5) == 0.0
    assert mean_reciprocal_rank([]) == 0.0
    assert ndcg_at_k([], 5) == 0.0


# ------------------------------------------------------------------ #
# Golden-set hygiene (loads validate label integrity)
# ------------------------------------------------------------------ #


def test_golden_corpus_and_labels_are_consistent():
    corpus = load_corpus()
    queries = load_queries()

    assert len(corpus) == 10
    assert len(queries) == 50
    # Every corpus document is the labeled target of at least one query.
    labeled = {doc_id for query in queries for doc_id in query.relevant}
    assert labeled == {doc_id for doc_id, _, _ in corpus}


def test_retrieval_queries_exclude_no_evidence():
    queries = retrieval_queries()
    assert len(queries) == 44
    assert all(query.relevant for query in queries)
    categories = {query.category for query in queries}
    assert categories == {"fact", "paraphrase", "exact", "multi"}

def test_hard_golden_labels_stay_consistent_when_corpus_is_built():
    """Label-integrity guard for the hard tier (EU AI Act). Skips when
    the gitignored corpus has not been built locally."""
    from tests.eval.harness import GOLDEN_HARD_DIR

    if not (GOLDEN_HARD_DIR / "corpus").is_dir():
        pytest.skip("hard corpus not built (golden_hard/build_corpus.py)")
    queries = load_queries(GOLDEN_HARD_DIR)
    assert len(queries) == 38
    assert sum(q.category == "no_evidence" for q in queries) == 6



# ------------------------------------------------------------------ #
# Abstention ruler: declared-absent detection (offline)
# ------------------------------------------------------------------ #


def test_declared_absent_matches_the_measured_phrasings():
    """Pinned against the REAL live answers the coverage verdict
    produces for unanswerable questions (artifacts 2026-06)."""
    from tests.eval.answer_harness import _declares_absent

    measured = [
        "Der monatliche Preis ist aus den vorliegenden Auszügen "
        "**nicht ersichtlich**.",
        "Die vorliegenden Auszüge nennen **keine maximale Geldbuße "
        "in Euro**.",
        "Die Auszüge nennen **keine konkrete Anzahl von Stunden**.",
        "Auf Basis der Auszüge lässt sich **kein benannter "
        "Zertifizierungsstandard** (etwa ISO 27001) nennen.",
        "Die genaue Gebühr geht aus den Auszügen **nicht hervor**.",
        "Aus den Auszügen geht **kein konkreter Marktanteil in "
        "Prozent** hervor.",
        "Dazu gibt es keine Angabe in den Dokumenten.",
    ]
    for answer in measured:
        assert _declares_absent(answer), answer


def test_declared_absent_ignores_substantive_answers():
    from tests.eval.answer_harness import _declares_absent

    substantive = [
        "Die Haftung ist auf den Auftragswert begrenzt [K1].",
        "TLPT muessen mindestens alle drei Jahre durchgefuehrt "
        "werden [K2].",
        "Der Vertrag kann mit einer Frist von drei Monaten zum "
        "Quartalsende gekuendigt werden [K1].",
    ]
    for answer in substantive:
        assert not _declares_absent(answer), answer
