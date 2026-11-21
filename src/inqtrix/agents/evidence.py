"""Evidence identity, the evidence digest and the advisory coverage judgement.

Dedup uses EXACTLY the citation identity of the research pipeline
(``doc:{document_id}#{chunk_index}`` for internal, canonical URL for
web) so a source found twice becomes ONE labelled reference.
:func:`evidence_digest` renders references as the one-line register a
prompt reads, and :func:`run_sufficiency_judgement` is the advisory
coverage verdict the kernel's sufficiency judge asks for over that digest
(:mod:`inqtrix.agents.kernel.cognition`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from inqtrix.agents.patterns._structured import StructuredOutcome, structured_call
from inqtrix.agents.phase_models import (
    SufficiencyJudgement,
)
from inqtrix.agents.evidence_context import (
    NO_EVIDENCE,
    internal_reference_name,
    is_internal_reference,
)
from inqtrix.text import (
    LOG_CUT_MARKER,
    PASSAGE_CUT_MARKER,
    shorten_visibly,
)
from inqtrix.urls import normalize_url
from inqtrix.untrusted_content import untrusted_fence

if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider

GROUNDED_SUPPORT_MAX_CHARS = 600
"""Maximum persisted provider-answer support per cited URL.

``grounded_support`` is a bounded passage from the provider's synthesized,
grounded answer. It is explicitly not a verbatim source excerpt; real
``excerpt``/``source_text`` fields always remain the stronger evidence.
"""

_GROUNDED_CUT_MARKER = "…"
"""Marks the end of a persisted support passage that was cut."""

REGISTER_EXCERPT_CHARS = 200 + len(PASSAGE_CUT_MARKER)
"""Longest body of one evidence-register line: up to 200 characters of the
passage, plus the :data:`~inqtrix.text.PASSAGE_CUT_MARKER` a shortened one
closes with."""

def shorten_grounded_support(
    text: object, limit: int = GROUNDED_SUPPORT_MAX_CHARS
) -> str:
    """*text* as a persisted ``grounded_support`` passage.

    The one cut every writer of the field uses (the search ledger for the
    text of a citation, the kernel for a provider answer without sources):
    one line of at most *limit* characters, cut at a word boundary and never
    inside a number by :func:`~inqtrix.text.shorten_visibly`, with the
    grounded cut marker at the cut.
    """
    return shorten_visibly(text, limit, marker=_GROUNDED_CUT_MARKER)


def dedup_key(ref: dict[str, Any]) -> str:
    """Return the citation identity shared with ``result.py``."""
    document_id = ref.get("document_id")
    if document_id is not None and ref.get("chunk_index") is not None:
        return f"doc:{document_id}#{ref['chunk_index']}"
    return normalize_url(str(ref.get("url", "") or ""))


def merge_missing_evidence_fields(
    canonical: dict[str, Any], incoming: dict[str, Any]
) -> bool:
    """Fill absent evidence metadata without replacing stronger truth.

    Returns ``True`` when *canonical* changed. Stable identity fields and
    first-seen labels stay untouched; the function only fills an empty value.
    """
    changed = False
    for field, value in incoming.items():
        if field == "label":
            continue
        if _missing_evidence_value(
            canonical.get(field)
        ) and not _missing_evidence_value(value):
            canonical[field] = value
            changed = True
    return changed


def _missing_evidence_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return value == [] or value == {}


def run_sufficiency_judgement(
    llm: "LLMProvider",
    *,
    success_criteria: list[str],
    evidence_digest: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    clarified_context: str = "",
    source_coverage: str = "",
) -> StructuredOutcome:
    """The fast-tier three-way coverage verdict (gate semantics).

    ``clarified_context`` carries what the user already answered when the
    agent asked back. Without it the judge weighs the evidence against
    the ORIGINAL wording and reports a gap the user has long since
    closed: after the user names both options, the verdict still reads
    "da keine zwei konkreten Optionen genannt sind", and the run keeps
    searching a goal it has been given.
    """
    criteria = "\n".join(f"- {c}" for c in success_criteria) or "- (none)"
    clarified = (
        f"Already specified by the user:\n{clarified_context.strip()}\n\n"
        if clarified_context.strip()
        else ""
    )
    # Sources the run started reading, with their ACTUAL read ranges: a
    # search excerpt from a source is not that source read. A remainder the
    # assignment requires to be read completely is a gap, not coverage.
    coverage = (
        "Sources under reading (actual read ranges; a source with an unread "
        "remainder that the criteria require to be read completely is NOT "
        f"covered — name the remainder as missing):\n{source_coverage.strip()}\n\n"
        if source_coverage.strip()
        else ""
    )
    return structured_call(
        llm,
        prompt=(
            f"Success criteria:\n{criteria}\n\n"
            f"{clarified}"
            f"Evidence at hand:\n{evidence_digest}\n\n"
            f"{coverage}"
            "Judge the coverage: covered, partial or uncovered, plus the "
            "criteria without sufficient evidence. What the user has "
            "already specified counts as given and must NOT be reported "
            "as missing."
        ),
        model_cls=SufficiencyJudgement,
        node="agent_sufficiency",
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


def evidence_digest(references: list[dict[str, Any]]) -> str:
    """Label -> compact prompt digest of *references*.

    Every line is one line: its body is cut visibly within
    :data:`REGISTER_EXCERPT_CHARS` by :func:`~inqtrix.text.shorten_visibly`
    (a word boundary, never inside a number), so a reader never takes a
    shortened passage for the whole one. A passage or grounded support
    closes with the passage marker; a title or address, which carries no
    statement, closes with the plain one.
    """
    lines = []
    for ref in references:
        source_body = ref.get("excerpt") or ref.get("source_text")
        grounded_support = ref.get("grounded_support")
        if source_body:
            body = shorten_visibly(
                source_body, REGISTER_EXCERPT_CHARS, marker=PASSAGE_CUT_MARKER
            )
        elif grounded_support:
            support = shorten_visibly(
                grounded_support, REGISTER_EXCERPT_CHARS, marker=PASSAGE_CUT_MARKER
            )
            body = (
                "Grounded answer context (no source excerpt): "
                f"{support}"
            )
        else:
            # A register line without a passage falls back to the source's
            # name. For an internal document that name is its title, never
            # its id or its inqtrix:// address: the register is prompt
            # material like the full card, and it must not be the place a
            # writer learns an identifier it would then copy.
            body = shorten_visibly(
                internal_reference_name(ref)
                if is_internal_reference(ref)
                else (ref.get("title") or ref.get("url") or ""),
                REGISTER_EXCERPT_CHARS,
                marker=LOG_CUT_MARKER,
            )
        lines.append(f"[{ref['label']}] {body}")
    if not lines:
        return NO_EVIDENCE
    return untrusted_fence("\n".join(lines), "evidence-register")
