"""Run comparison engine -- compare_runs and related helpers."""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from inqtrix.parity._types import Issue, MetricCheck, ParityTolerance, QuestionSpec


def _citation_delta_allowed(reference_value: int, tolerance: ParityTolerance) -> int:
    return max(
        tolerance.min_citation_delta,
        int(math.ceil(reference_value * tolerance.citation_fraction)),
    )


def _count_delta_allowed(reference_value: int, tolerance: ParityTolerance) -> int:
    return max(
        tolerance.min_count_map_delta,
        int(math.ceil(reference_value * tolerance.count_map_fraction)),
    )


def _keyword_hits(answer: str, expected_keywords: tuple[str, ...]) -> int:
    answer_lower = (answer or "").lower()
    return sum(1 for keyword in expected_keywords if keyword.lower() in answer_lower)


def _append_issue(issues: list[Issue], level: str, message: str) -> None:
    issues.append(Issue(level=level, message=message))


def _numeric_delta(baseline: Any, current: Any) -> int | float | None:
    if isinstance(baseline, bool) or isinstance(current, bool):
        return None
    if isinstance(baseline, (int, float)) and isinstance(current, (int, float)):
        return current - baseline
    return None


def _check_status(level: str, exceeded: bool) -> str:
    if not exceeded:
        return "pass"
    return "fail" if level == "error" else "warn"


def _append_check(
    checks: list[MetricCheck],
    *,
    key: str,
    label: str,
    level: str,
    exceeded: bool,
    baseline: Any,
    current: Any,
    delta: int | float | None = None,
    allowed_delta: int | float | None = None,
    details: str = "",
) -> None:
    checks.append(
        MetricCheck(
            key=key,
            label=label,
            level=level,
            status=_check_status(level, exceeded),
            baseline=baseline,
            current=current,
            delta=delta,
            allowed_delta=allowed_delta,
            details=details,
        )
    )


def _aggregate_checks(checks: list[MetricCheck]) -> dict[str, int]:
    summary = {"total": len(checks), "passed": 0, "warnings": 0, "failed": 0}
    for check in checks:
        if check.status == "fail":
            summary["failed"] += 1
        elif check.status == "warn":
            summary["warnings"] += 1
        else:
            summary["passed"] += 1
    return summary


def compare_runs(
    baseline: dict[str, Any],
    current: dict[str, Any],
    questions: list[QuestionSpec],
    tolerance: ParityTolerance | None = None,
) -> dict[str, Any]:
    """Compare two run artifacts under semantic parity tolerances."""
    tolerance = tolerance or ParityTolerance()
    baseline_by_id = {
        result.get("question_id"): result
        for result in baseline.get("results", [])
        if result.get("question_id")
    }
    current_by_id = {
        result.get("question_id"): result
        for result in current.get("results", [])
        if result.get("question_id")
    }

    question_reports: list[dict[str, Any]] = []
    summary = {
        "total": 0,
        "passed": 0,
        "warnings": 0,
        "failed": 0,
        "checks": {"total": 0, "passed": 0, "warnings": 0, "failed": 0},
    }

    for question in questions:
        summary["total"] += 1
        issues: list[Issue] = []
        checks: list[MetricCheck] = []
        baseline_result = baseline_by_id.get(question.id)
        current_result = current_by_id.get(question.id)

        if baseline_result is None:
            message = f"Baseline fehlt fuer {question.id}."
            _append_check(
                checks,
                key="baseline_presence",
                label="Baseline artifact present",
                level="error",
                exceeded=True,
                baseline=True,
                current=False,
                allowed_delta=0,
                details=message,
            )
            _append_issue(issues, "error", message)
        if current_result is None:
            message = f"Aktueller Run fehlt fuer {question.id}."
            _append_check(
                checks,
                key="current_run_presence",
                label="Current run artifact present",
                level="error",
                exceeded=True,
                baseline=True,
                current=False,
                allowed_delta=0,
                details=message,
            )
            _append_issue(issues, "error", message)

        baseline_metrics = (baseline_result or {}).get("metrics", {})
        current_metrics = (current_result or {}).get("metrics", {})

        if baseline_result is not None and current_result is not None:
            baseline_conf = int(baseline_metrics.get("final_confidence", 0) or 0)
            current_conf = int(current_metrics.get("final_confidence", 0) or 0)
            confidence_delta = current_conf - baseline_conf
            confidence_exceeded = abs(confidence_delta) > tolerance.confidence_delta
            message = f"Confidence drift bei {question.id}: {current_conf} vs {baseline_conf}."
            _append_check(
                checks,
                key="final_confidence",
                label="Confidence",
                level="error",
                exceeded=confidence_exceeded,
                baseline=baseline_conf,
                current=current_conf,
                delta=confidence_delta,
                allowed_delta=tolerance.confidence_delta,
                details=message if confidence_exceeded else "",
            )
            if confidence_exceeded:
                _append_issue(
                    issues,
                    "error",
                    message,
                )

            baseline_rounds = int(baseline_metrics.get("total_rounds", 0) or 0)
            current_rounds = int(current_metrics.get("total_rounds", 0) or 0)
            rounds_delta = current_rounds - baseline_rounds
            rounds_exceeded = tolerance.exact_round_match and baseline_rounds != current_rounds
            message = f"Rundenzahl drift bei {question.id}: {current_rounds} vs {baseline_rounds}."
            _append_check(
                checks,
                key="total_rounds",
                label="Rounds",
                level="error",
                exceeded=rounds_exceeded,
                baseline=baseline_rounds,
                current=current_rounds,
                delta=rounds_delta,
                allowed_delta=0 if tolerance.exact_round_match else None,
                details=message if rounds_exceeded else "",
            )
            if rounds_exceeded:
                _append_issue(
                    issues,
                    "error",
                    message,
                )

            baseline_queries = int(baseline_metrics.get("total_queries", 0) or 0)
            current_queries = int(current_metrics.get("total_queries", 0) or 0)
            query_delta = current_queries - baseline_queries
            query_exceeded = abs(query_delta) > tolerance.query_delta
            message = f"Query-Drift bei {question.id}: {current_queries} vs {baseline_queries}."
            _append_check(
                checks,
                key="total_queries",
                label="Queries",
                level="warning",
                exceeded=query_exceeded,
                baseline=baseline_queries,
                current=current_queries,
                delta=query_delta,
                allowed_delta=tolerance.query_delta,
                details=message if query_exceeded else "",
            )
            if query_exceeded:
                _append_issue(
                    issues,
                    "warning",
                    message,
                )

            baseline_citations = int(baseline_metrics.get("total_citations", 0) or 0)
            current_citations = int(current_metrics.get("total_citations", 0) or 0)
            citation_delta = current_citations - baseline_citations
            allowed_citation_delta = _citation_delta_allowed(baseline_citations, tolerance)
            citation_exceeded = abs(citation_delta) > allowed_citation_delta
            message = (
                f"Zitations-Drift bei {question.id}: {current_citations} vs {baseline_citations}."
            )
            _append_check(
                checks,
                key="total_citations",
                label="Citations",
                level="error",
                exceeded=citation_exceeded,
                baseline=baseline_citations,
                current=current_citations,
                delta=citation_delta,
                allowed_delta=allowed_citation_delta,
                details=message if citation_exceeded else "",
            )
            if citation_exceeded:
                _append_issue(
                    issues,
                    "error",
                    message,
                )

            for key, label, delta in (
                ("aspect_coverage_final", "Aspect coverage", tolerance.aspect_coverage_delta),
                ("source_quality_score_final", "Source quality", tolerance.source_quality_delta),
                ("claim_quality_score_final", "Claim quality", tolerance.claim_quality_delta),
            ):
                baseline_value = float(baseline_metrics.get(key, 0.0) or 0.0)
                current_value = float(current_metrics.get(key, 0.0) or 0.0)
                value_delta = current_value - baseline_value
                exceeded = abs(value_delta) > delta
                message = (
                    f"{label} drift bei {question.id}: "
                    f"{current_value:.3f} vs {baseline_value:.3f}."
                )
                _append_check(
                    checks,
                    key=key,
                    label=label,
                    level="error",
                    exceeded=exceeded,
                    baseline=baseline_value,
                    current=current_value,
                    delta=value_delta,
                    allowed_delta=delta,
                    details=message if exceeded else "",
                )
                if exceeded:
                    _append_issue(
                        issues,
                        "error",
                        message,
                    )

            for key, label in (
                ("evidence_consistency_final", "Evidence consistency"),
                ("evidence_sufficiency_final", "Evidence sufficiency"),
            ):
                baseline_value = int(baseline_metrics.get(key, 0) or 0)
                current_value = int(current_metrics.get(key, 0) or 0)
                value_delta = current_value - baseline_value
                exceeded = abs(value_delta) > tolerance.evidence_score_delta
                message = f"{label} drift bei {question.id}: {current_value} vs {baseline_value}."
                _append_check(
                    checks,
                    key=key,
                    label=label,
                    level="error",
                    exceeded=exceeded,
                    baseline=baseline_value,
                    current=current_value,
                    delta=value_delta,
                    allowed_delta=tolerance.evidence_score_delta,
                    details=message if exceeded else "",
                )
                if exceeded:
                    _append_issue(
                        issues,
                        "error",
                        message,
                    )

            for key in (
                "competing_events_detected",
                "stagnation_detected",
                "falsification_triggered",
                "utility_stop_triggered",
                "plateau_stop_triggered",
            ):
                baseline_value = bool(baseline_metrics.get(key, False))
                current_value = bool(current_metrics.get(key, False))
                exceeded = baseline_value != current_value
                message = f"Stop-/Signal-Drift bei {question.id}: {key} differs."
                _append_check(
                    checks,
                    key=key,
                    label=key,
                    level="error",
                    exceeded=exceeded,
                    baseline=baseline_value,
                    current=current_value,
                    allowed_delta=0,
                    details=message if exceeded else "",
                )
                if exceeded:
                    _append_issue(
                        issues,
                        "error",
                        message,
                    )

            for key, label in (
                ("source_tier_counts_final", "source tier counts"),
                ("claim_status_counts_final", "claim status counts"),
            ):
                baseline_counts = baseline_metrics.get(key, {}) or {}
                current_counts = current_metrics.get(key, {}) or {}
                for count_key in sorted(set(baseline_counts) | set(current_counts)):
                    baseline_value = int(baseline_counts.get(count_key, 0) or 0)
                    current_value = int(current_counts.get(count_key, 0) or 0)
                    allowed_count_delta = _count_delta_allowed(baseline_value, tolerance)
                    count_delta = current_value - baseline_value
                    exceeded = abs(count_delta) > allowed_count_delta
                    message = (
                        f"{label} drift bei {question.id}: {count_key}="
                        f"{current_value} vs {baseline_value}."
                    )
                    _append_check(
                        checks,
                        key=f"{key}.{count_key}",
                        label=f"{label}: {count_key}",
                        level="warning",
                        exceeded=exceeded,
                        baseline=baseline_value,
                        current=current_value,
                        delta=count_delta,
                        allowed_delta=allowed_count_delta,
                        details=message if exceeded else "",
                    )
                    if exceeded:
                        _append_issue(
                            issues,
                            "warning",
                            message,
                        )

            answer = str(current_result.get("answer", ""))
            hit_count = _keyword_hits(answer, question.expected_keywords)
            expected_keyword_count = len(question.expected_keywords)
            missing_keywords = expected_keyword_count > 0 and hit_count == 0
            message = (
                f"Keine expected_keywords im aktuellen Answer fuer {question.id} gefunden."
            )
            _append_check(
                checks,
                key="expected_keywords",
                label="Expected keyword hits",
                level="warning",
                exceeded=missing_keywords,
                baseline=expected_keyword_count,
                current=hit_count,
                delta=hit_count - expected_keyword_count if expected_keyword_count else 0,
                details=message if missing_keywords else "",
            )
            if missing_keywords:
                _append_issue(
                    issues,
                    "warning",
                    message,
                )

        check_summary = _aggregate_checks(checks)
        for key, value in check_summary.items():
            summary["checks"][key] += value

        if any(issue.level == "error" for issue in issues):
            status = "fail"
            summary["failed"] += 1
        elif issues:
            status = "warn"
            summary["warnings"] += 1
        else:
            status = "pass"
            summary["passed"] += 1

        question_reports.append(
            {
                "question_id": question.id,
                "question": question.question,
                "category": question.category,
                "status": status,
                "issues": [asdict(issue) for issue in issues],
                "checks": [asdict(check) for check in checks],
                "check_summary": check_summary,
                "baseline_metrics": baseline_metrics,
                "current_metrics": current_metrics,
                "baseline_top_claims": list((baseline_result or {}).get("top_claims", []) or []),
                "current_top_claims": list((current_result or {}).get("top_claims", []) or []),
            }
        )

    return {
        "summary": summary,
        "baseline_meta": baseline.get("meta", {}),
        "current_meta": current.get("meta", {}),
        "questions": question_reports,
    }
