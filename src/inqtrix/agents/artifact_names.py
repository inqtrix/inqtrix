"""Derived file names for canvas documents.

The name is DISPLAY AND ADDRESS, never a key: identity stays the
``artifact_id``, the name is ``slug(title) + ".md"`` made unique per
session with a ``-2`` suffix in created order. The slug is a byte-exact
port of the frontend's NFKD slug (``slugLabel``-family), pinned by the
shared parity fixture ``tests/fixtures/artifact_name_parity.json`` that
both languages consume — change one side only via the fixture.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

ARTIFACT_NAME_FALLBACK = "dokument"
_COMBINING_MARKS = re.compile("[\\u0300-\\u036f]")
_TRAILING_MD = re.compile(r"\s*\.md\s*$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Only real canvas documents carry file names; diagnostics kinds keep
# their plain registry rendering.
NAMED_ARTIFACT_KINDS = ("memo", "deliverable")


def artifact_slug(title: str) -> str:
    """NFKD slug, operation-for-operation the frontend's order.

    normalize(NFKD) -> toLowerCase -> strip combining marks ->
    non-alnum runs to ``-`` -> strip edge dashes -> THEN slice to 48
    (a slice landing on a dash keeps it, exactly like the TS side).
    """
    # A title that already carries the extension ("bericht.md") must not
    # become "bericht-md.md": strip ONE trailing ".md" before slugging.
    title = _TRAILING_MD.sub("", title)
    normalized = unicodedata.normalize("NFKD", title).lower()
    normalized = _COMBINING_MARKS.sub("", normalized)
    normalized = _NON_ALNUM.sub("-", normalized)
    normalized = normalized.strip("-")[:48]
    return normalized or ARTIFACT_NAME_FALLBACK


def assign_artifact_file_names(
    items: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Map ``artifact_id -> file name`` for documents in CREATED order.

    Collisions get ``-2``, ``-3``, ... BEFORE the extension
    (``bericht-2.md``, never ``bericht.md-2``); suffix order is the
    deterministic created order the caller supplies (oldest first —
    the session listing's contract). Renaming an OLDER document can
    therefore shift a younger namesake's suffix: accepted, because the
    name is display and the id stays stable.
    """
    taken: set[str] = set()
    names: dict[str, str] = {}
    for artifact_id, title in items:
        base = artifact_slug(title)
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}-{suffix}"
            suffix += 1
        taken.add(candidate)
        names[artifact_id] = f"{candidate}.md"
    return names
