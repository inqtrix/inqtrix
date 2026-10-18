"""Evidence merging, contradiction analysis and conditional coverage checks.

Dedup uses EXACTLY the citation identity of the research pipeline
(``doc:{document_id}#{chunk_index}`` for internal, canonical URL for
web) so a source cited by two tasks becomes ONE labelled reference.
Contradiction analysis is one mid-tier call over LEXICALLY OVERLAPPING
claim pairs only (``claim_signature`` prefilter — never O(n²) LLM).
The coverage judgement supports replanning after failed or insufficient tasks;
successful execution proceeds to the finished output's content review.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from inqtrix.agents.patterns._structured import StructuredOutcome, structured_call
from inqtrix.agents.phase_models import (
    ContradictionReport,
    SufficiencyJudgement,
)
from inqtrix.agents.evidence_context import (
    NO_EVIDENCE,
    internal_reference_name,
    is_internal_reference,
)
from inqtrix.agents.scheduler import TaskOutcome
from inqtrix.text import (
    LOG_CUT_MARKER,
    PASSAGE_CUT_MARKER,
    shorten_visibly,
    start_at_word,
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
"""Marks each end where a persisted support passage was cut from the
provider's answer."""

REGISTER_EXCERPT_CHARS = 200 + len(PASSAGE_CUT_MARKER)
"""Longest body of one evidence-register line: up to 200 characters of the
passage, plus the :data:`~inqtrix.text.PASSAGE_CUT_MARKER` a shortened one
closes with."""

_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_ANSWER_URL = re.compile(r"https?://[^\s\])}>,\"']+")
_SENTENCE_END = re.compile(
    r"(?<!\b[A-Za-zÄÖÜäöü])[.!?](?:[\"'”’)]*)\s+"
    r"(?=[A-ZÄÖÜ0-9#*_(])"
)


def shorten_grounded_support(
    text: object, limit: int = GROUNDED_SUPPORT_MAX_CHARS
) -> str:
    """*text* as a persisted ``grounded_support`` passage.

    The one cut every writer of the field uses (the mission's and the
    kernel's instant search, the research engine's provider annotations):
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


def enrich_instant_evidence(
    grounded_answer: str,
    references: list[dict[str, Any]],
    *,
    support_chars: int = GROUNDED_SUPPORT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Associate cited URLs with provider-grounded supporting prose.

    The instant-search provider returns one grounded answer plus a source
    list. Some providers expose rich per-source snippets; Azure Foundry
    exposes only cited URLs and titles. For URLs that occur in the grounded
    answer, this helper deterministically captures the containing statement
    as ``grounded_support``. That field records what the provider answer said
    near the citation and must never be presented as a verbatim source quote.

    Args:
        grounded_answer: Full provider-grounded answer, potentially with
            inline Markdown links.
        references: Provider-neutral evidence dictionaries containing URLs.
        support_chars: Hard character cap for each persisted support passage.

    Returns:
        Fresh evidence dictionaries. Existing fields are preserved and an
        existing ``grounded_support`` is never overwritten.
    """
    if support_chars <= len(_GROUNDED_CUT_MARKER):
        raise ValueError("support_chars must leave room for the cut marker")
    answer = str(grounded_answer or "").replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    enriched: list[dict[str, Any]] = []
    for raw in references:
        ref = dict(raw)
        if ref.get("grounded_support") or not answer:
            enriched.append(ref)
            continue
        url = str(ref.get("url") or "").strip()
        position = _citation_position(answer, url)
        if position >= 0:
            support = _grounded_statement(
                answer,
                position=position,
                url=url,
                support_chars=support_chars,
            )
            if support:
                ref["grounded_support"] = support
        enriched.append(ref)
    return enriched


def _citation_position(answer: str, url: str) -> int:
    target = normalize_url(url)
    if not target:
        return -1
    for match in _ANSWER_URL.finditer(answer):
        if normalize_url(match.group(0)) == target:
            return match.start()
    return -1


def _grounded_statement(
    answer: str,
    *,
    position: int,
    url: str,
    support_chars: int,
) -> str:
    preceding_break = answer.rfind("\n\n", 0, position)
    paragraph_start = 0 if preceding_break < 0 else preceding_break + 2
    paragraph_end = answer.find("\n\n", position)
    if paragraph_end < 0:
        paragraph_end = len(answer)
    paragraph = answer[paragraph_start:paragraph_end]
    relative_position = position - paragraph_start

    sentence_start = 0
    sentence_end = len(paragraph)
    for boundary in _SENTENCE_END.finditer(paragraph):
        boundary_end = boundary.end()
        if boundary_end <= relative_position:
            sentence_start = boundary_end
            continue
        sentence_end = boundary.end() - len(boundary.group(0)) + 1
        break

    statement = paragraph[sentence_start:sentence_end].strip()
    relative_in_statement = max(0, relative_position - sentence_start)
    if len(statement) > support_chars:
        # Citations usually follow the claim. Preserve more context before
        # the URL while retaining a small suffix for qualifiers. The window
        # opens on a whole word, never inside a number; its end is the
        # final cut below, which follows the same rule.
        window_start = min(
            max(0, relative_in_statement - (support_chars * 4 // 5)),
            len(statement) - support_chars,
        )
        if window_start:
            statement = _GROUNDED_CUT_MARKER + statement[
                start_at_word(statement, window_start, reach=support_chars // 2):
            ]

    statement = _MARKDOWN_LINK.sub(r"\1", statement)
    target_url = normalize_url(url)
    statement = _ANSWER_URL.sub(
        lambda match: (
            ""
            if normalize_url(match.group(0)) == target_url
            else match.group(0)
        ),
        statement,
    )
    statement = re.sub(r"\[([^\]]+)\]\(\s*\)", r"\1", statement)
    statement = re.sub(r"^[ \t]*(?:[-*+] |#+ |>|\d+[.)] )", "", statement)
    statement = " ".join(statement.split()).strip(" -–—")
    return shorten_grounded_support(statement, support_chars)


def merge_evidence(
    outcomes: dict[str, TaskOutcome],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge task evidence into labelled references + collected claims.

    Returns ``(references, claims)``; each reference gains a stable
    ``label`` (``K#`` internal, ``W#`` web) in first-seen order — the
    labels the synthesis cites.
    """
    references: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    counters = {"K": 0, "W": 0}
    claims: list[dict[str, Any]] = []
    for task_id in sorted(outcomes):
        outcome = outcomes[task_id]
        for ref in outcome.evidence:
            key = dedup_key(ref)
            if not key:
                continue
            if key in seen:
                canonical = seen[key]
                if task_id not in canonical["tasks"]:
                    canonical["tasks"].append(task_id)
                # First-seen order owns the label, not evidence quality. A
                # later task may carry the real excerpt that an earlier
                # URL-only provider record lacked. Fill only missing fields;
                # an existing real source excerpt is never replaced by
                # provider-synthesized support or another duplicate.
                merge_missing_evidence_fields(canonical, ref)
                continue
            prefix = "K" if ref.get("document_id") is not None else "W"
            counters[prefix] += 1
            labelled = {
                **ref,
                "label": f"{prefix}{counters[prefix]}",
                "tasks": [task_id],
            }
            seen[key] = labelled
            references.append(labelled)
        for claim in outcome.claims:
            claims.append({**claim, "task_id": task_id})
    return references, claims


def merge_missing_evidence_fields(
    canonical: dict[str, Any], incoming: dict[str, Any]
) -> bool:
    """Fill absent evidence metadata without replacing stronger truth.

    Returns ``True`` when *canonical* changed. Stable identity fields and
    first-seen labels stay untouched; the function only fills an empty value.
    """
    changed = False
    for field, value in incoming.items():
        if field in {"label", "tasks"}:
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


def overlapping_claim_pairs(
    claims: list[dict[str, Any]],
    *,
    signature: Any,
    limit: int = 12,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Lexically overlapping claim pairs (the LLM prefilter).

    *signature* is ``claim_signature(text) -> str`` from the claim
    consolidation strategy. Whitespace-only text differences cannot form a
    contradiction pair; other differences remain for the model to judge.
    """
    by_signature: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        text = str(claim.get("text", ""))
        if not text.strip():
            continue
        by_signature.setdefault(signature(text), []).append(claim)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for bucket in by_signature.values():
        for index in range(len(bucket) - 1):
            first, second = bucket[index], bucket[index + 1]
            if " ".join(str(first.get("text", "")).split()) == " ".join(
                str(second.get("text", "")).split()
            ):
                continue
            pairs.append((bucket[index], bucket[index + 1]))
            if len(pairs) >= limit:
                return pairs
    return pairs


def run_contradiction_analysis(
    llm: "LLMProvider",
    *,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
) -> StructuredOutcome | None:
    """One mid-tier call over the overlapping pairs; ``None`` when there
    is nothing to compare (no call, no cost)."""
    if not pairs:
        return None
    lines = [
        f"Pair {index + 1}:\n  A: {a.get('text', '')}\n  B: {b.get('text', '')}"
        for index, (a, b) in enumerate(pairs)
    ]
    return structured_call(
        llm,
        prompt=(
            "Check the following pairs of claims for contradictions. Report "
            "only genuine substantive conflicts (hard = irreconcilable, "
            "soft = tension) with the likely cause.\n\n"
            + untrusted_fence("\n\n".join(lines), "claims")
        ),
        model_cls=ContradictionReport,
        node="agent_contradiction",
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )


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


def evidence_digest(
    references: list[dict[str, Any]],
    *,
    excerpt_chars: int = REGISTER_EXCERPT_CHARS,
    labels: Iterable[str] | None = None,
    with_reference_ids: bool = False,
) -> str:
    """Label -> compact prompt digest, optionally scoped to one section.

    Every line is one line: its body is cut visibly within
    ``excerpt_chars`` by :func:`~inqtrix.text.shorten_visibly` (a word
    boundary, never inside a number), so a reader never takes a shortened
    passage for the whole one. A passage or grounded support closes with
    the passage marker; a title or address, which carries no statement,
    closes with the plain one.

    ``with_reference_ids`` appends the id a reader re-opens the full entry
    with: the address form a section role reads its preselection from,
    built from the same line as the register so the two cannot disagree
    about what a reference says.
    """
    allowed = set(labels) if labels is not None else None
    lines = []
    for ref in references:
        if allowed is not None and ref.get("label") not in allowed:
            continue
        source_body = ref.get("excerpt") or ref.get("source_text")
        grounded_support = ref.get("grounded_support")
        if source_body:
            body = shorten_visibly(source_body, excerpt_chars, marker=PASSAGE_CUT_MARKER)
        elif grounded_support:
            support = shorten_visibly(
                grounded_support, excerpt_chars, marker=PASSAGE_CUT_MARKER
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
                excerpt_chars,
                marker=LOG_CUT_MARKER,
            )
        address = (
            f" (reference_id: {ref['reference_id']})"
            if with_reference_ids and ref.get("reference_id")
            else ""
        )
        lines.append(f"[{ref['label']}] {body}{address}")
    if not lines:
        return NO_EVIDENCE
    return untrusted_fence("\n".join(lines), "evidence-register")
