"""Build the DORA golden corpus (Regulation (EU) 2022/2554, German).

One document per article — 64 articles of financial-sector ICT
resilience law whose obligations read near-identically across actor
types (Finanzunternehmen vs. IKT-Drittdienstleister vs. Aufseher) and
cross-reference heavily: exactly the multi-hop/aggregation failure
mode the BSI tier measured headroom on (multi_complete 0.75).

Reuses the EUR-Lex fetch/split machinery of the AI-Act tier (the
sibling ``golden_hard/build_corpus.py``); the corpus directory is
gitignored like the other rebuildable tiers (EU legal texts are free
to reuse — we still do not vendor them into the repo). Run once
before the DORA-set evals:

    uv run python tests/eval/golden_dora/build_corpus.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HARD_BUILDER = (
    Path(__file__).resolve().parent.parent / "golden_hard" / "build_corpus.py"
)
_spec = importlib.util.spec_from_file_location("eurlex_builder", _HARD_BUILDER)
assert _spec is not None and _spec.loader is not None
_eurlex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_eurlex)

# The Publications Office Cellar is the canonical machine endpoint;
# the eur-lex.europa.eu HTML views sit behind an AWS WAF JavaScript
# challenge (verified 2026-06) and return a challenge page to scripted
# clients. Cellar negotiates the language via Accept-Language.
SOURCE_URL = "http://publications.europa.eu/resource/celex/32022R2554"
CORPUS_DIR = Path(__file__).parent / "corpus"


def fetch_cellar_html(url: str) -> str:
    """GET the German XHTML rendition from the Cellar."""
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "inqtrix-eval-corpus/1.0",
            "Accept": "application/xhtml+xml",
            "Accept-Language": "de-DE, de;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", errors="replace")


def main() -> None:
    print(f"Fetching {SOURCE_URL} ...")
    lines = _eurlex.html_to_lines(fetch_cellar_html(SOURCE_URL))
    documents = _eurlex.split_documents(lines)
    articles = [key for key in documents if key.startswith("artikel-")]
    if len(articles) != 64:
        sys.exit(
            f"unexpected split: {len(articles)} articles (DORA has "
            "exactly 64) — EUR-Lex layout changed?"
        )
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in CORPUS_DIR.glob("*.md"):
        stale.unlink()
    for key in sorted(articles):
        doc_lines = documents[key]
        title = " — ".join(doc_lines[:2])[:120]
        body = "\n\n".join(doc_lines)
        (CORPUS_DIR / f"{key}.md").write_text(
            f"# {title}\n\n{body}\n", encoding="utf-8"
        )
    total_words = sum(
        len((CORPUS_DIR / f"{key}.md").read_text(encoding="utf-8").split())
        for key in articles
    )
    print(
        f"Wrote {len(articles)} DORA articles ({total_words} words) "
        f"to {CORPUS_DIR}"
    )


if __name__ == "__main__":
    main()
