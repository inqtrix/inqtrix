"""Contract validation functions for parity question sets."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from inqtrix.parity._types import Issue, QuestionSpec


def validate_question_contract(questions: list[QuestionSpec]) -> list[Issue]:
    """Validate the local canonical question set."""
    issues: list[Issue] = []
    seen_ids: set[str] = set()

    if not questions:
        return [Issue(level="error", message="No questions defined.")]

    for question in questions:
        if not question.id:
            issues.append(Issue(level="error", message="Found a question without an ID."))
            continue
        if question.id in seen_ids:
            issues.append(Issue(level="error", message=f'Duplicate question ID: {question.id}'))
        seen_ids.add(question.id)

        if not question.question:
            issues.append(Issue(level="error", message=f"Leerer Fragetext bei {question.id}."))
        if not question.category:
            issues.append(Issue(level="error", message=f"Leere Kategorie bei {question.id}."))
        if not question.expected_keywords:
            issues.append(
                Issue(
                    level="warning",
                    message=f'No expected_keywords configured for {question.id}.',
                )
            )

    return issues


def compare_question_contracts(
    local_questions: list[QuestionSpec],
    reference_questions: list[QuestionSpec],
) -> list[Issue]:
    """Compare local canonical questions against a reference file."""
    issues: list[Issue] = []
    local_by_id = {question.id: question for question in local_questions}
    reference_by_id = {question.id: question for question in reference_questions}

    local_ids = set(local_by_id)
    reference_ids = set(reference_by_id)

    for missing_id in sorted(reference_ids - local_ids):
        issues.append(Issue(level="error", message=f"Fehlende Referenzfrage: {missing_id}"))
    for extra_id in sorted(local_ids - reference_ids):
        issues.append(Issue(level="warning", message=f'Additional local question: {extra_id}'))

    for question_id in sorted(local_ids & reference_ids):
        local = local_by_id[question_id]
        reference = reference_by_id[question_id]

        if local.question != reference.question:
            issues.append(Issue(level="error", message=f"Fragetext drift bei {question_id}."))
        if local.category != reference.category:
            issues.append(Issue(level="error", message=f"Kategorie drift bei {question_id}."))
        if list(local.expected_keywords) != list(reference.expected_keywords):
            issues.append(
                Issue(level="warning", message=f"expected_keywords drift bei {question_id}.")
            )

    return issues


def build_contract_report(
    *,
    questions: list[QuestionSpec],
    reference_questions: list[QuestionSpec] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a local contract report for canonical parity assets."""
    issues = validate_question_contract(questions)

    if reference_questions is not None:
        issues.extend(compare_question_contracts(questions, reference_questions))

    if baseline is not None:
        baseline_ids = {
            result.get("question_id")
            for result in baseline.get("results", [])
            if result.get("question_id")
        }
        for question in questions:
            if question.id not in baseline_ids:
                issues.append(
                    Issue(
                        level="warning",
                        message=f'No baseline for {question.id} in the supplied directory.',
                    )
                )

    summary = {
        "errors": sum(1 for issue in issues if issue.level == "error"),
        "warnings": sum(1 for issue in issues if issue.level == "warning"),
        "question_count": len(questions),
    }
    return {"summary": summary, "issues": [asdict(issue) for issue in issues]}


def format_contract_report(report: dict[str, Any]) -> str:
    """Render a human-readable contract report."""
    summary = report["summary"]
    lines = [
        "Question contract report",
        f"- questions: {summary['question_count']}",
        f"- errors: {summary['errors']}",
        f"- warnings: {summary['warnings']}",
    ]
    for issue in report.get("issues", []):
        lines.append(f"- {issue['level']}: {issue['message']}")
    return "\n".join(lines)
