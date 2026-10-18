"""Build the GermanQuAD golden tier (everyday German, non-legal).

The four existing tiers are ALL legal/regulatory German — clean
official prose with distinctive section vocabulary. A retrieval change
that only helps on legalese is over-fitting, not progress; this tier
is the counterweight: real everyday-German questions over Wikipedia
passages (GermanQuAD, deepset, 13.7k extractive QA pairs).

License (CC BY-SA 4.0, share-alike): BOTH the corpus AND queries.json
are GENERATED here and stay gitignored — nothing derived from the
dataset is committed; only this build script (attribution below) and
reviewed baselines enter the repo. Run once before gquad evals:

    uv run python tests/eval/golden_gquad/build_corpus.py

Data access downloads the MTEB mirror's Arrow IPC files over plain
HTTP (the deepset original is a script-based loader whose S3 source
returns 403; the datasets-server rows API serves neither repo, and
the parquet conversion endpoint denies access). Arrow parsing needs
``pyarrow``, which is deliberately NOT a project dependency — run the
script with an ad-hoc environment:

    uv run --with pyarrow python tests/eval/golden_gquad/build_corpus.py

Attribution: GermanQuAD — Moeller et al. (deepset),
https://huggingface.co/datasets/deepset/germanquad, mirrored as
https://huggingface.co/datasets/mteb/germanquad-retrieval (BEIR
format), CC BY-SA 4.0.
"""

from __future__ import annotations

import io
import json
import random
import sys
import urllib.request
from pathlib import Path

ARROW_URLS = {
    "corpus": (
        "https://huggingface.co/datasets/mteb/germanquad-retrieval"
        "/resolve/main/corpus/data-00000-of-00001.arrow"
    ),
    "queries": (
        "https://huggingface.co/datasets/mteb/germanquad-retrieval"
        "/resolve/main/queries/data-00000-of-00001.arrow"
    ),
    "qrels": (
        "https://huggingface.co/datasets/mteb/germanquad-retrieval-qrels"
        "/resolve/main/test/data-00000-of-00001.arrow"
    ),
}
QUERY_COUNT = 30
DISTRACTOR_COUNT = 120
SEED = 20260612
"""Fixed seed: the sample must be reproducible, never re-rolled until
a baseline regression looks better."""

TIER_DIR = Path(__file__).parent
CORPUS_DIR = TIER_DIR / "corpus"


def _read_arrow(url: str) -> list[dict]:
    try:
        import pyarrow as pa
    except ImportError:
        sys.exit(
            "pyarrow fehlt (bewusst keine Projekt-Dependency). "
            "Aufruf: uv run --with pyarrow python "
            "tests/eval/golden_gquad/build_corpus.py"
        )
    request = urllib.request.Request(
        url, headers={"User-Agent": "inqtrix-eval-corpus/1.0"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = response.read()
    # HF `save_to_disk` writes the streaming IPC format; fall back to
    # the file format defensively.
    try:
        table = pa.ipc.open_stream(io.BytesIO(payload)).read_all()
    except pa.ArrowInvalid:
        table = pa.ipc.open_file(io.BytesIO(payload)).read_all()
    return table.to_pylist()


def fetch_rows() -> list[dict]:
    """Join the BEIR triple; one row per ``(question, context)``."""
    corpus = {row["_id"]: row for row in _read_arrow(ARROW_URLS["corpus"])}
    queries = {
        row["_id"]: row for row in _read_arrow(ARROW_URLS["queries"])
    }
    rows: list[dict] = []
    for qrel in _read_arrow(ARROW_URLS["qrels"]):
        query = queries.get(qrel["query-id"])
        document = corpus.get(qrel["corpus-id"])
        if query is None or document is None:
            continue
        title = (document.get("title") or "").strip()
        text = (document.get("text") or "").strip()
        context = f"{title}\n{text}".strip() if title else text
        rows.append({"context": context, "question": query["text"]})
    if len(rows) < 2000:
        sys.exit(
            f"joined only {len(rows)} QA pairs (expected ~2204) — "
            "mirror layout changed?"
        )
    return rows


def passage_title(context: str) -> str:
    first_line = context.strip().splitlines()[0]
    return " ".join(first_line.split()[:10])[:120] or "GermanQuAD-Passage"


def main() -> None:
    rows = fetch_rows()
    # One entry per DISTINCT passage; keep the first question per
    # passage so the gold label is unambiguous (several GermanQuAD
    # questions share a context).
    by_context: dict[str, dict] = {}
    for row in rows:
        context = row["context"].strip()
        if len(context) < 200:
            continue
        by_context.setdefault(context, row)
    passages = list(by_context.items())
    needed = QUERY_COUNT + DISTRACTOR_COUNT
    if len(passages) < needed:
        sys.exit(
            f"only {len(passages)} distinct passages (need {needed}) — "
            "raise FETCH_ROWS"
        )

    rng = random.Random(SEED)
    rng.shuffle(passages)
    query_passages = passages[:QUERY_COUNT]
    distractors = passages[QUERY_COUNT : QUERY_COUNT + DISTRACTOR_COUNT]

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in CORPUS_DIR.glob("*.md"):
        stale.unlink()
    queries = []
    for index, (context, row) in enumerate(
        [*query_passages, *distractors], start=1
    ):
        doc_id = f"gq-{index:04d}"
        title = passage_title(context)
        (CORPUS_DIR / f"{doc_id}.md").write_text(
            f"# {title}\n\n{context}\n", encoding="utf-8"
        )
        if index <= QUERY_COUNT:
            queries.append(
                {
                    "id": f"g{index:02d}",
                    "query": row["question"].strip(),
                    "relevant": [doc_id],
                    # Extractive natural questions over the gold
                    # passage — the fact category by construction.
                    "category": "fact",
                }
            )

    (TIER_DIR / "queries.json").write_text(
        json.dumps(
            {
                "description": (
                    "GermanQuAD tier (GENERATED — this file and the "
                    "corpus are gitignored, CC BY-SA 4.0 share-alike; "
                    "rebuild via build_corpus.py, fixed seed "
                    f"{SEED}): {QUERY_COUNT} everyday-German questions "
                    f"over their gold Wikipedia passages plus "
                    f"{DISTRACTOR_COUNT} distractor passages. The "
                    "non-legal counterweight tier: a retrieval change "
                    "must hold here AND on the legal tiers, otherwise "
                    "it is vocabulary over-fitting. All queries are "
                    "single-passage fact questions; the tier has no "
                    "multi/no_evidence categories by construction "
                    "(multi_complete reports 0.0 — see the baseline "
                    "note)."
                ),
                "queries": queries,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    total_words = sum(
        len(path.read_text(encoding="utf-8").split())
        for path in CORPUS_DIR.glob("*.md")
    )
    print(
        f"Wrote {QUERY_COUNT + DISTRACTOR_COUNT} passages "
        f"({total_words} words) and {QUERY_COUNT} queries to {TIER_DIR}"
    )


if __name__ == "__main__":
    main()
