"""Build the hard golden corpus from the EU AI Act (German, EUR-Lex).

Downloads Regulation (EU) 2024/1689 in German and splits it into one
document per article plus one per annex — ~126 documents of dense,
cross-referencing regulatory German. Retrieval then means finding the
RIGHT article among many near-identical-sounding ones (provider vs
deployer vs importer obligations), which is exactly the failure mode
the saturated base set cannot measure.

The corpus directory is gitignored on purpose: the text is rebuilt
reproducibly from the official source (EU legal texts are free to
reuse; we still do not vendor 1.3 MB of regulation into the repo).
Run once before the hard-set evals:

    uv run python tests/eval/golden_hard/build_corpus.py
"""

from __future__ import annotations

import html
import re
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = (
    "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/"
    "?uri=OJ:L_202401689"
)
CORPUS_DIR = Path(__file__).parent / "corpus"

_TAG = re.compile(r"<[^>]+>")
_ARTICLE_HEAD = re.compile(r"^Artikel (\d{1,3})$")
_ANNEX_HEAD = re.compile(r"^ANHANG ([IVX]+)$")

_ROMAN = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
    "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
}


def fetch_html() -> str:
    request = urllib.request.Request(
        SOURCE_URL, headers={"User-Agent": "inqtrix-eval-corpus/1.0"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", errors="replace")


def html_to_lines(raw: str) -> list[str]:
    """Strip tags and produce normalized, non-empty text lines."""
    # Block-level closes become newlines so headings stay on own lines.
    raw = re.sub(r"</(p|div|td|tr|table|h\d)>", "\n", raw, flags=re.I)
    text = html.unescape(_TAG.sub(" ", raw))
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def split_documents(lines: list[str]) -> dict[str, list[str]]:
    """Group lines into per-article / per-annex documents."""
    documents: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        article = _ARTICLE_HEAD.match(line)
        annex = _ANNEX_HEAD.match(line)
        if article:
            key = f"artikel-{int(article.group(1)):03d}"
            current = documents.setdefault(key, [])
            current.append(line)
            continue
        if annex and annex.group(1) in _ROMAN:
            key = f"anhang-{_ROMAN[annex.group(1)]:02d}"
            current = documents.setdefault(key, [])
            current.append(line)
            continue
        if current is not None:
            current.append(line)
    return documents


def main() -> None:
    print(f"Fetching {SOURCE_URL} ...")
    lines = html_to_lines(fetch_html())
    documents = split_documents(lines)
    articles = [k for k in documents if k.startswith("artikel-")]
    annexes = [k for k in documents if k.startswith("anhang-")]
    if len(articles) < 100 or len(annexes) < 10:
        sys.exit(
            f"unexpected split: {len(articles)} articles, "
            f"{len(annexes)} annexes — EUR-Lex layout changed?"
        )
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for old in CORPUS_DIR.glob("*.md"):
        old.unlink()
    for key, doc_lines in sorted(documents.items()):
        # First line is the heading; second usually the article title.
        title = " — ".join(doc_lines[:2])[:120]
        body = "\n\n".join(doc_lines)
        (CORPUS_DIR / f"{key}.md").write_text(
            f"# {title}\n\n{body}\n", encoding="utf-8"
        )
    total_words = sum(
        len((CORPUS_DIR / f"{k}.md").read_text(encoding="utf-8").split())
        for k in documents
    )
    print(
        f"Wrote {len(articles)} articles + {len(annexes)} annexes "
        f"({total_words} words) to {CORPUS_DIR}"
    )


if __name__ == "__main__":
    main()
