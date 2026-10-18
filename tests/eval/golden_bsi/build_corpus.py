"""Build the BSI golden corpus: IT-Grundschutz Bausteine + C5:2020.

Downloads two official BSI artifacts and converts them into one
document per evaluation unit:

* the Edition-2023 per-Baustein ZIP — 111 German PDFs of 8-10 pages
  each (one Baustein = one unit, ids like ``gsk-app-1-1``), parsed
  through the SAME MarkItDown pipeline production ingestion uses;
* the C5:2020 catalogue PDF — split per criterion (``c5-ops-01``,
  ...), the compliance-document shape Inqtrix targets.

The corpus directory is gitignored ON PURPOSE and must stay that way:
BSI terms of use (https://www.bsi.bund.de/dok/6627966) allow free
non-commercial use but forbid mirroring without written consent —
every developer rebuilds the corpus locally from the official source;
nothing BSI-authored is committed or redistributed. Run once before
the BSI-set evals:

    uv run python tests/eval/golden_bsi/build_corpus.py

Download quirks verified 2026-06 against the live site: HEAD requests
are rejected by the BSI WAF (use GET only); omitting the
``__blob=publicationFile`` parameter returns an HTML landing page with
HTTP 200 (hence the magic-byte checks); the ZIP stores entry names in
a DOS codepage (umlauts arrive as mojibake) with a doubled
``.pdf.clean.pdf`` extension — unit ids are derived from the
ASCII-safe Baustein prefix instead.
"""

from __future__ import annotations

import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from inqtrix.knowledge.parsing import MarkItDownParser  # noqa: E402

CORPUS_DIR = Path(__file__).parent / "corpus"

BAUSTEIN_ZIP_URL = (
    "https://www.bsi.bund.de/SharedDocs/Downloads/DE/BSI/Grundschutz/"
    "IT-GS-Kompendium_Einzel_PDFs_2023/Zip_Datei_Edition_2023.zip"
    "?__blob=publicationFile"
)
C5_PDF_URL = (
    "https://www.bsi.bund.de/SharedDocs/Downloads/DE/BSI/Publikationen/"
    "Broschueren/C5_2020.pdf?__blob=publicationFile"
)

# Sizes verified 2026-06-11; BSI replaces files silently (the v=
# parameter increments), so drift is a review trigger, not an error.
EXPECTED_SIZES = {
    BAUSTEIN_ZIP_URL: 15_826_108,
    C5_PDF_URL: 1_350_028,
}

_BAUSTEIN_ID = re.compile(r"^([A-Z]{3,4}(?:\.\d+){1,3})\b")
# Criterion headings in the PDF body carry a DOUBLED bullet glyph
# (0x84 0x84) in the extracted text; table-of-contents entries use a
# single one and in-text cross references none — the doubling is what
# separates the 127 real section starts from ~390 mere mentions.
_C5_CRITERION = re.compile(r"^\s*\x84{2}\s*([A-Z]{2,3}-\d{2})\s+(\S.*)$")
_MIN_UNIT_CHARS = 300


def fetch(url: str) -> bytes:
    """GET one artifact (the BSI WAF rejects HEAD requests)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "inqtrix-eval-corpus/1.0"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = response.read()
    if payload[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
        raise RuntimeError(
            f"{url} returned an HTML landing page instead of the file — "
            "the URL layout changed; update build_corpus.py."
        )
    expected = EXPECTED_SIZES.get(url)
    if expected is not None and len(payload) != expected:
        print(
            f"WARNING: {url} is {len(payload)} bytes (expected "
            f"{expected}) — BSI replaced the file; re-review corpus "
            "and golden labels."
        )
    return payload


def slugify(unit_id: str) -> str:
    """Filesystem-safe document id (``APP.1.1`` -> ``app-1-1``)."""
    return unit_id.lower().replace(".", "-")


def join_heading_run(lines: list[str], start: int) -> str:
    """Join a wrapped PDF heading: non-empty lines until the first
    blank line. Single-line takes of multi-line headings drop the
    load-bearing nouns ("OPS.1.1.4 Schutz vor" without
    "Schadprogrammen") and erase exactly the distinctions the eval
    exists to test."""
    parts: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            break
        parts.append(stripped)
    return " ".join(parts)


def write_document(doc_id: str, title: str, text: str) -> None:
    (CORPUS_DIR / f"{doc_id}.md").write_text(
        f"# {title}\n\n{text.strip()}\n", encoding="utf-8"
    )


def build_bausteine(parser: MarkItDownParser, payload: bytes) -> int:
    """One document per Baustein PDF from the Edition-2023 ZIP."""
    count = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for entry in archive.namelist():
            name = entry.rsplit("/", 1)[-1]
            if not name.lower().endswith(".pdf"):
                continue
            match = _BAUSTEIN_ID.match(name)
            if match is None:
                print(f"WARNING: skipping entry without a Baustein id: {name}")
                continue
            name_id = match.group(1)
            text = parser.parse(
                file_name=f"{name_id}.pdf", content=archive.read(entry)
            )
            lines = text.splitlines()
            first = next(
                (i for i, line in enumerate(lines) if line.strip()), 0
            )
            title_line = join_heading_run(lines, first).lstrip("# ")
            # The parsed document is the id source of truth: the ZIP
            # entry names carry quirks (codepage damage, prefix typos
            # like CCON for the CON layer) that would mint unofficial
            # ids and double the title prefix.
            parsed = _BAUSTEIN_ID.match(title_line)
            baustein = parsed.group(1) if parsed else name_id
            if baustein != name_id:
                print(
                    f"WARNING: entry name says {name_id!r}, parsed "
                    f"title says {baustein!r} — trusting the document."
                )
            title = (
                title_line
                if baustein in title_line
                else f"{baustein} {title_line}"
            )
            write_document(f"gsk-{slugify(baustein)}", title, text)
            count += 1
    return count


def build_c5(parser: MarkItDownParser, payload: bytes) -> int:
    """One document per C5:2020 criterion (``OPS-01`` etc.).

    The catalogue lays some criterion PAIRS out in parallel print
    columns; MarkItDown linearizes those pages with the two bodies
    interleaved at paragraph level, which no line heuristic can
    de-interleave reliably. Such pairs are therefore FUSED forward:
    a heading whose own body stays below the stub threshold merges
    into the FOLLOWING criterion's document (which carries both
    bodies), the title names both ids, and every fusion is printed.
    The build fails when any detected heading ends up neither with
    its own document nor inside a declared fusion.
    """
    text = parser.parse(file_name="C5_2020.pdf", content=payload)
    lines = text.splitlines()
    units: list[tuple[str, str, list[str]]] = []
    for number, line in enumerate(lines):
        match = _C5_CRITERION.match(line)
        if match is not None:
            criterion = match.group(1)
            title = match.group(2).strip()
            continuation = join_heading_run(lines, number + 1)
            if continuation and len(title) < 60 and not title.endswith("."):
                # Wrapped heading: the dangling fragment continues on
                # the next non-empty lines ("Vergabe und Aenderung von
                # Zugangs-" + "berechtigungen").
                if title.endswith("-"):
                    title = title[:-1] + continuation
                elif title.endswith(("–", "—", "und", "von", "der")):
                    title = f"{title} {continuation}"
            units.append((criterion, title, [f"{criterion} {title}"]))
            continue
        if units:
            units[-1][2].append(line)

    heading_ids = [criterion for criterion, _, _ in units]
    documents: list[tuple[str, str, str, list[str]]] = []
    pending_stub: tuple[str, str, str] | None = None
    for criterion, title, body_lines in units:
        body = "\n".join(body_lines).strip()
        if len(body) < _MIN_UNIT_CHARS:
            # Heading stub: its body sits interleaved in the NEXT
            # criterion's pages — fuse forward.
            if pending_stub is not None:
                print(
                    "WARNING: consecutive heading stubs "
                    f"{pending_stub[0]} and {criterion} — fusing both "
                    "forward."
                )
                pending_stub = (
                    f"{pending_stub[0]}/{criterion}",
                    f"{pending_stub[1]} / {title}",
                    f"{pending_stub[2]}\n{body}",
                )
            else:
                pending_stub = (criterion, title, body)
            continue
        if pending_stub is not None:
            stub_id, stub_title, stub_body = pending_stub
            pending_stub = None
            print(
                f"FUSED: {stub_id} has a column-interleaved body — "
                f"merged into c5-{slugify(criterion)} (title names "
                "both)."
            )
            documents.append(
                (
                    criterion,
                    f"C5 {stub_id} + {criterion} {stub_title} / {title}",
                    f"{stub_body}\n{body}",
                    [stub_id, criterion],
                )
            )
        else:
            documents.append(
                (criterion, f"C5 {criterion} {title}", body, [criterion])
            )
    if pending_stub is not None:
        print(
            f"ERROR: trailing heading stub {pending_stub[0]} has no "
            "following criterion to fuse into."
        )
        raise SystemExit(1)

    covered: set[str] = set()
    count = 0
    seen_ids: set[str] = set()
    for criterion, title, body, members in documents:
        doc_id = f"c5-{slugify(criterion)}"
        if doc_id in seen_ids:
            print(f"ERROR: duplicate criterion document {doc_id}.")
            raise SystemExit(1)
        seen_ids.add(doc_id)
        write_document(doc_id, title, body)
        for member in members:
            for single in member.split("/"):
                covered.add(single.strip())
        count += 1
    missing = [
        criterion
        for criterion in heading_ids
        if criterion not in covered
    ]
    if missing:
        print(
            "ERROR: criteria without a document or declared fusion: "
            + ", ".join(missing)
        )
        raise SystemExit(1)
    print(
        f"C5: {len(heading_ids)} headings -> {count} documents "
        f"({len(heading_ids) - count} fused)."
    )
    return count


def main() -> None:
    CORPUS_DIR.mkdir(exist_ok=True)
    for stale in CORPUS_DIR.glob("*.md"):
        stale.unlink()
    parser = MarkItDownParser()

    print("Fetching IT-Grundschutz Baustein ZIP (Edition 2023) ...")
    bausteine = build_bausteine(parser, fetch(BAUSTEIN_ZIP_URL))
    print(f"Wrote {bausteine} Baustein documents.")

    print("Fetching the C5:2020 catalogue ...")
    criteria = build_c5(parser, fetch(C5_PDF_URL))
    print(f"Wrote {criteria} C5 criterion documents.")

    total = bausteine + criteria
    print(f"Corpus: {total} documents under {CORPUS_DIR}")
    if bausteine != 111:
        print(
            "ERROR: expected 111 Bausteine — the ZIP layout changed."
        )
        raise SystemExit(1)
    if not 80 <= criteria <= 140:
        print(
            "ERROR: C5 criterion count outside the expected range "
            "(80-140) — re-check the split heuristic."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
