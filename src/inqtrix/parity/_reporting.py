"""Report formatting functions -- markdown tables, CSV export, claim snapshots."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from inqtrix.parity._types import CLAIM_REPORT_LIMIT, CLAIM_SNAPSHOT_LIMIT


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------

def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n... (gekuerzt)"


def _numeric_delta(baseline: Any, current: Any) -> int | float | None:
    if isinstance(baseline, bool) or isinstance(current, bool):
        return None
    if isinstance(baseline, (int, float)) and isinstance(current, (int, float)):
        return current - baseline
    return None


def _format_scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _format_delta(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "-"
    if isinstance(value, int):
        return f"{value:+d}"
    if isinstance(value, float):
        text = f"{value:+.3f}".rstrip("0").rstrip(".")
        return text or "+0"
    return str(value)


def _pair_summary(current: Any, baseline: Any, delta: Any) -> str:
    current_text = _format_scalar(current)
    baseline_text = _format_scalar(baseline)
    delta_text = _format_delta(delta)
    if delta_text == "-":
        return f"{current_text} vs {baseline_text}"
    return f"{current_text} vs {baseline_text} ({delta_text})"


def _escape_markdown_cell(value: Any) -> str:
    return _format_scalar(value).replace("|", "\\|").replace("\n", "<br>")


def _format_markdown_table(
    rows: list[dict[str, Any]],
    columns: list[tuple[str, str]],
) -> str:
    header = "| " + " | ".join(label for _, label in columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = [header, separator]
    for row in rows:
        body.append(
            "| "
            + " | ".join(_escape_markdown_cell(row.get(key)) for key, _ in columns)
            + " |"
        )
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Claim formatting helpers
# ---------------------------------------------------------------------------

def _claim_tier_summary(claim: dict[str, Any]) -> str:
    counts = claim.get("source_tier_counts", {}) or {}
    parts: list[str] = []
    for key in ("primary", "mainstream", "stakeholder", "unknown", "low"):
        value = int(counts.get(key, 0) or 0)
        if value:
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else "-"


def _claim_table_rows(
    claims: list[dict[str, Any]],
    *,
    max_items: int = CLAIM_REPORT_LIMIT,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, claim in enumerate(claims[:max_items], start=1):
        rows.append(
            {
                "rank": index,
                "status": claim.get("status", ""),
                "claim_type": claim.get("claim_type", ""),
                "needs_primary": "yes" if claim.get("needs_primary", False) else "no",
                "support_count": int(claim.get("support_count", 0) or 0),
                "contradict_count": int(claim.get("contradict_count", 0) or 0),
                "source_tiers": _claim_tier_summary(claim),
                "status_reason": _truncate_text(
                    str(claim.get("status_reason", "") or ""),
                    120,
                ),
                "text": _truncate_text(str(claim.get("text", "") or ""), 180),
            }
        )
    return rows


def _serialize_claim_snapshot(
    claims: list[dict[str, Any]],
    *,
    max_items: int = CLAIM_SNAPSHOT_LIMIT,
    max_chars: int = 600,
) -> str:
    if not claims:
        return ""

    compact: list[dict[str, Any]] = []
    for claim in claims[:max_items]:
        compact.append(
            {
                "text": str(claim.get("text", "") or ""),
                "status": claim.get("status", ""),
                "claim_type": claim.get("claim_type", ""),
                "needs_primary": bool(claim.get("needs_primary", False)),
                "status_reason": str(claim.get("status_reason", "") or ""),
                "support_count": int(claim.get("support_count", 0) or 0),
                "contradict_count": int(claim.get("contradict_count", 0) or 0),
                "source_tier_counts": claim.get("source_tier_counts", {}) or {},
            }
        )

    return _truncate_text(
        json.dumps(compact, ensure_ascii=False),
        max_chars,
    ).replace("\n", " ")


def _format_claim_sections(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for question in report.get("questions", []):
        current_claims = question.get("current_top_claims", []) or []
        baseline_claims = question.get("baseline_top_claims", []) or []
        if not current_claims and not baseline_claims:
            continue

        lines.extend(
            [
                "",
                f"### {question.get('question_id', '')}",
                "",
                "Current top claims:",
                "",
            ]
        )
        if current_claims:
            lines.append(
                _format_markdown_table(
                    _claim_table_rows(current_claims),
                    [
                        ("rank", "#"),
                        ("status", "Status"),
                        ("claim_type", "Type"),
                        ("needs_primary", "Primary?"),
                        ("support_count", "Support"),
                        ("contradict_count", "Contradict"),
                        ("source_tiers", "Source tiers"),
                        ("status_reason", "Reason"),
                        ("text", "Text"),
                    ],
                )
            )
        else:
            lines.append("No exported claims.")

        lines.extend(["", "Baseline top claims:", ""])
        if baseline_claims:
            lines.append(
                _format_markdown_table(
                    _claim_table_rows(baseline_claims),
                    [
                        ("rank", "#"),
                        ("status", "Status"),
                        ("claim_type", "Type"),
                        ("needs_primary", "Primary?"),
                        ("support_count", "Support"),
                        ("contradict_count", "Contradict"),
                        ("source_tiers", "Source tiers"),
                        ("status_reason", "Reason"),
                        ("text", "Text"),
                    ],
                )
            )
        else:
            lines.append("No exported claims.")
    return lines


# ---------------------------------------------------------------------------
# Overview / check row builders
# ---------------------------------------------------------------------------

def build_compare_overview_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build per-question overview rows for human and file-based reporting."""
    rows: list[dict[str, Any]] = []
    for question in report.get("questions", []):
        baseline_metrics = question.get("baseline_metrics", {}) or {}
        current_metrics = question.get("current_metrics", {}) or {}
        issue_failures = sum(
            1 for issue in question.get("issues", []) if issue.get("level") == "error"
        )
        issue_warnings = sum(
            1 for issue in question.get("issues", []) if issue.get("level") == "warning"
        )
        check_summary = question.get("check_summary", {}) or {}
        rows.append(
            {
                "question_id": question.get("question_id", ""),
                "category": question.get("category", ""),
                "status": question.get("status", ""),
                "failed_checks": max(int(check_summary.get("failed", 0) or 0), issue_failures),
                "warning_checks": max(
                    int(check_summary.get("warnings", 0) or 0),
                    issue_warnings,
                ),
                "baseline_confidence": baseline_metrics.get("final_confidence"),
                "current_confidence": current_metrics.get("final_confidence"),
                "confidence_delta": _numeric_delta(
                    baseline_metrics.get("final_confidence"),
                    current_metrics.get("final_confidence"),
                ),
                "baseline_rounds": baseline_metrics.get("total_rounds"),
                "current_rounds": current_metrics.get("total_rounds"),
                "rounds_delta": _numeric_delta(
                    baseline_metrics.get("total_rounds"),
                    current_metrics.get("total_rounds"),
                ),
                "baseline_queries": baseline_metrics.get("total_queries"),
                "current_queries": current_metrics.get("total_queries"),
                "queries_delta": _numeric_delta(
                    baseline_metrics.get("total_queries"),
                    current_metrics.get("total_queries"),
                ),
                "baseline_citations": baseline_metrics.get("total_citations"),
                "current_citations": current_metrics.get("total_citations"),
                "citations_delta": _numeric_delta(
                    baseline_metrics.get("total_citations"),
                    current_metrics.get("total_citations"),
                ),
                "baseline_aspect_coverage": baseline_metrics.get("aspect_coverage_final"),
                "current_aspect_coverage": current_metrics.get("aspect_coverage_final"),
                "aspect_coverage_delta": _numeric_delta(
                    baseline_metrics.get("aspect_coverage_final"),
                    current_metrics.get("aspect_coverage_final"),
                ),
                "baseline_source_quality": baseline_metrics.get("source_quality_score_final"),
                "current_source_quality": current_metrics.get("source_quality_score_final"),
                "source_quality_delta": _numeric_delta(
                    baseline_metrics.get("source_quality_score_final"),
                    current_metrics.get("source_quality_score_final"),
                ),
                "baseline_claim_quality": baseline_metrics.get("claim_quality_score_final"),
                "current_claim_quality": current_metrics.get("claim_quality_score_final"),
                "claim_quality_delta": _numeric_delta(
                    baseline_metrics.get("claim_quality_score_final"),
                    current_metrics.get("claim_quality_score_final"),
                ),
            }
        )
    return rows


def build_compare_check_rows(
    report: dict[str, Any],
    *,
    only_flagged: bool = False,
) -> list[dict[str, Any]]:
    """Flatten question x metric checks into export rows."""
    rows: list[dict[str, Any]] = []
    for question in report.get("questions", []):
        baseline_top_claims = _serialize_claim_snapshot(
            question.get("baseline_top_claims", []) or []
        )
        current_top_claims = _serialize_claim_snapshot(
            question.get("current_top_claims", []) or []
        )
        for check in question.get("checks", []):
            if only_flagged and check.get("status") == "pass":
                continue
            rows.append(
                {
                    "question_id": question.get("question_id", ""),
                    "question": question.get("question", ""),
                    "category": question.get("category", ""),
                    "question_status": question.get("status", ""),
                    "metric_key": check.get("key", ""),
                    "metric_label": check.get("label", ""),
                    "level": check.get("level", ""),
                    "status": check.get("status", ""),
                    "baseline": check.get("baseline"),
                    "current": check.get("current"),
                    "delta": check.get("delta"),
                    "allowed_delta": check.get("allowed_delta"),
                    "details": check.get("details", ""),
                    "baseline_top_claims": baseline_top_claims,
                    "current_top_claims": current_top_claims,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Full report renderers
# ---------------------------------------------------------------------------

def format_compare_report(report: dict[str, Any]) -> str:
    """Render a human-readable parity comparison report."""
    summary = report["summary"]
    overview_rows = build_compare_overview_rows(report)
    flagged_rows = build_compare_check_rows(report, only_flagged=True)
    lines = [
        "Parity comparison report",
        f"- total: {summary['total']}",
        f"- passed: {summary['passed']}",
        f"- warnings: {summary['warnings']}",
        f"- failed: {summary['failed']}",
        f"- metric checks: {summary['checks']['total']}",
        f"- metric checks passed: {summary['checks']['passed']}",
        f"- metric checks warnings: {summary['checks']['warnings']}",
        f"- metric checks failed: {summary['checks']['failed']}",
    ]

    if overview_rows:
        lines.extend(
            [
                "",
                "## Question overview",
                "",
                _format_markdown_table(
                    [
                        {
                            "question_id": row["question_id"],
                            "status": row["status"],
                            "failed_checks": row["failed_checks"],
                            "warning_checks": row["warning_checks"],
                            "confidence": _pair_summary(
                                row["current_confidence"],
                                row["baseline_confidence"],
                                row["confidence_delta"],
                            ),
                            "rounds": _pair_summary(
                                row["current_rounds"],
                                row["baseline_rounds"],
                                row["rounds_delta"],
                            ),
                            "queries": _pair_summary(
                                row["current_queries"],
                                row["baseline_queries"],
                                row["queries_delta"],
                            ),
                            "citations": _pair_summary(
                                row["current_citations"],
                                row["baseline_citations"],
                                row["citations_delta"],
                            ),
                            "aspect": _pair_summary(
                                row["current_aspect_coverage"],
                                row["baseline_aspect_coverage"],
                                row["aspect_coverage_delta"],
                            ),
                            "source": _pair_summary(
                                row["current_source_quality"],
                                row["baseline_source_quality"],
                                row["source_quality_delta"],
                            ),
                            "claim": _pair_summary(
                                row["current_claim_quality"],
                                row["baseline_claim_quality"],
                                row["claim_quality_delta"],
                            ),
                        }
                        for row in overview_rows
                    ],
                    [
                        ("question_id", "Question"),
                        ("status", "Status"),
                        ("failed_checks", "Fail"),
                        ("warning_checks", "Warn"),
                        ("confidence", "Confidence"),
                        ("rounds", "Rounds"),
                        ("queries", "Queries"),
                        ("citations", "Citations"),
                        ("aspect", "Aspect"),
                        ("source", "Source"),
                        ("claim", "Claim"),
                    ],
                ),
            ]
        )

    lines.extend(["", "## Flagged checks", ""])
    if flagged_rows:
        lines.append(
            _format_markdown_table(
                flagged_rows,
                [
                    ("question_id", "Question"),
                    ("metric_label", "Metric"),
                    ("status", "Status"),
                    ("level", "Level"),
                    ("current", "Current"),
                    ("baseline", "Baseline"),
                    ("delta", "Delta"),
                    ("allowed_delta", "Allowed"),
                    ("details", "Details"),
                ],
            )
        )
    else:
        lines.append("No flagged checks.")

    claim_sections = _format_claim_sections(report)
    if claim_sections:
        lines.extend(["", "## Claim snapshots"])
        lines.extend(claim_sections)

    return "\n".join(lines)


def format_compare_csv(report: dict[str, Any]) -> str:
    """Render the full question x metric parity matrix as CSV."""
    rows = build_compare_check_rows(report)
    output = io.StringIO()
    fieldnames = [
        "question_id",
        "question",
        "category",
        "question_status",
        "metric_key",
        "metric_label",
        "level",
        "status",
        "baseline",
        "current",
        "delta",
        "allowed_delta",
        "details",
        "baseline_top_claims",
        "current_top_claims",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()
