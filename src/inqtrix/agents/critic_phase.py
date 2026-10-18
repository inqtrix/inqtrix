"""The mission's measurements for the shared review pass.

The mission measures, the review judges: citation coverage, quote
verification, contradiction mentions and the stored execution are computed
HERE and handed to the review as run diagnostics with a basis id. Every
diagnostic carries its own method, so the reviewer needs no mission-specific
instructions to read it correctly.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from inqtrix.agents.phase_models import DeepReviewFinding
from inqtrix.agents.report_quality import (
    quoted_passages,
    report_word_count,
    unverified_web_quotes,
)
from inqtrix.i18n import t

log = logging.getLogger("inqtrix")

SOURCE_BASIS_PREFIX = "source:"
"""How a finding names a ledger reference: this prefix plus the reference id."""

DIAGNOSTIC_BASIS_PREFIX = "diagnostic:"
"""How a finding names a run diagnostic: this prefix plus the diagnostic's path.

Both prefixes are the vocabulary of ONE contract — the prompt that asks for
a basis_id, the producers that mint one, the guard that confirms it and the
reader projection that resolves it back into a name. Spelled once, so a
change cannot reach three of those four and leave the fifth reading raw ids.
"""


def uncovered_criteria(
    critic: Mapping[str, Any] | None, *, kind: str | None = None
) -> list[str]:
    """Read typed unmet criteria; unclassified history cannot request research."""
    items: list[str] = []
    for entry in (critic or {}).get("criteria_uncovered") or []:
        if isinstance(entry, dict) and entry.get("kind") in {
            "information", "output_requirement",
        }:
            text = str(entry.get("text") or "").strip()
            if kind is not None and entry["kind"] != kind:
                continue
        else:
            log.warning("Stored remaining criterion has no valid classification.")
            if kind is not None:
                continue
            text = "Unclassified: " + str(entry).strip()
        if text and text not in items:
            items.append(text)
    return items


def quote_check_basis(run_id: str) -> str:
    """Basis id of the quote verification diagnostic of a mission run."""
    return f"{DIAGNOSTIC_BASIS_PREFIX}{run_id}:quote_checks"


def review_diagnostics(
    *,
    run_id: str,
    coverage: dict[str, int],
    quote_checks: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    memo_markdown: str,
    language: str,
    uncited_verified_sources: list[str] | None = None,
    task_evidence_notes: str = "",
    execution: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The mission's deterministic measurements as run diagnostics.

    ``task_evidence_notes`` lists the children whose own evidence audit
    capped their confidence — the review must see them, or a memo built on
    single-source children reads as well-founded. ``uncited_verified_sources``
    are sources a document analysis verified that the output does not cite.
    ``language`` gives every diagnostic a reader title, which is what the
    review names it by once its verdict reaches a person.
    """
    words = {"language": language}

    def diagnostic(name: str, values: dict[str, Any], method: str) -> dict[str, Any]:
        return {
            "basis_id": f"{DIAGNOSTIC_BASIS_PREFIX}{run_id}:{name}",
            "title": t(words, f"review_diagnostic_{name}"),
            "source_run_id": run_id,
            "scope": f"mission_{name}",
            "method": method,
            "values": values,
        }

    unverified = [check["quote"] for check in quote_checks if not check["verified"]]
    diagnostics = [
        diagnostic(
            "text_measurements",
            {
                "word_count": report_word_count(memo_markdown),
                "paragraphs": coverage.get("paragraphs", 0),
                "paragraphs_with_evidence": coverage.get("cited_paragraphs", 0),
                "evidence_labels_used": coverage.get("labels_used", 0),
                "uncited_verified_sources": list(uncited_verified_sources or []),
            },
            "Lexical words in the delivered Markdown, including title, "
            "headings, evidence labels and code; letter/digit groups, "
            "apostrophes and typographic hyphens inside a word join. No token "
            "count and no separate measurement of the running text alone. Use "
            "this count; claim no other word count from an estimate. "
            "uncited_verified_sources: sources a document analysis of this run "
            "verified that the output cites nowhere.",
        ),
        diagnostic(
            "quote_checks",
            {
                "verbatim_quotes_checked": len(quote_checks),
                "not_verified": unverified,
            },
            "Quoted passages compared word for word with the stored original "
            "excerpts. Only verbatim quotes must appear there; a missing "
            "excerpt refutes neither the quote nor the fact.",
        ),
        diagnostic(
            "contradictions",
            {
                "known": [
                    {
                        "internal_position": item.get("internal_position", ""),
                        "external_position": item.get("external_position", ""),
                        "addressed_in_output": any(
                            _mentioned(item.get(side), memo_markdown)
                            for side in ("internal_position", "external_position")
                        ),
                    }
                    for item in contradictions
                ],
            },
            "Contradictions the run detected between its sources. "
            "addressed_in_output is a plain text match of the first 40 "
            "characters of a position; judge the wording yourself.",
        ),
    ]
    if execution is not None:
        diagnostics.append(diagnostic(
            "execution",
            execution,
            "Stored plan and task state. Source operations include the "
            "discovery before the plan; a delegated research assignment "
            "counts as one operation, not as the number of its internal "
            "searches. Check criteria about the execution against this "
            "state, never against the output's description of it. A "
            "completed step need not be repeated in the result, and a task "
            "status is no judgement of content quality.",
        ))
    if task_evidence_notes.strip():
        diagnostics.append(diagnostic(
            "task_evidence_notes",
            {"notes": task_evidence_notes.strip()},
            "Notes of subtasks that report a limit of their own: an "
            "evidence audit that capped their confidence, a reading that "
            "did not cover everything it was asked to, or a search that "
            "reported a limit. They are checked against the run data and "
            "need no external evidence label; on their own they prove no "
            "factual error.",
        ))
    return diagnostics


def _mentioned(position: Any, text: str) -> bool:
    head = str(position or "")[:40]
    return bool(head) and head in text


def unverified_web_quote_findings(
    memo_markdown: str,
    quote_checks: list[dict[str, Any]],
    *,
    target: str,
    artifact_id: str,
    basis_id: str,
    finding_text: str,
) -> tuple[list[DeepReviewFinding], list[str]]:
    """Anchored findings for unverified quotes in web-cited paragraphs.

    Deterministic: a quoted claim that leans on a web citation but matches no
    stored excerpt is a measured defect, whatever the model's verdict says.
    An edit needs a unique anchor: the quote itself, or — when the quote
    occurs more than once — each paragraph that holds it; every uniquely
    anchored holder becomes a finding. Overlaps with the model's findings,
    and between server findings, are decided once in
    :func:`~inqtrix.agents.kernel.review_edits.locate_findings`, where server
    findings stand last. Returns the findings and the quotes that no unique
    anchor reaches; those stay open.
    """
    findings: list[DeepReviewFinding] = []
    unanchored: list[str] = []
    marked = dict(quoted_passages(memo_markdown))
    for quote in unverified_web_quotes(memo_markdown, quote_checks):
        # The passage WITH its quotation marks: turning it into a paraphrase
        # means removing them.
        passage = marked.get(quote, quote)
        holders = (
            [passage]
            if memo_markdown.count(passage) == 1
            else [part for part in memo_markdown.split("\n\n") if passage in part]
        )
        for anchor in dict.fromkeys(holders):
            if memo_markdown.count(anchor) != 1:
                unanchored.append(quote)
                continue
            findings.append(DeepReviewFinding(
                target=target,
                artifact_id=artifact_id,
                finding=finding_text,
                kind="run_diagnostic",
                quote=anchor,
                basis_ids=[basis_id],
            ))
    return findings, list(dict.fromkeys(unanchored))
