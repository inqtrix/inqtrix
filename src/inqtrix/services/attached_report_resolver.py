"""Resolution of attached research reports against the caller's own runs.

A client sends run ids and nothing else. Existence, completion and the
report's name are read server-side under the CALLER's visibility, exactly
as the prompt-library rules are resolved: an id the caller cannot see
must fail loudly, because starting a run whose attachments silently
vanished is the failure this whole channel exists to prevent.

Only the NAME travels into the prompt. The body stays where it is and is
fetched later by ``read_research_report`` — a real report has a median of
~54k characters, and its sources only become citable by going through the
evidence ledger, which an inlined text could never do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from inqtrix.auth.context import UserContext

MAX_ATTACHED_REPORTS = 3
"""How many reports one run may carry.

Each one is a separate tool read of a median ~54k-character document, so
this is a real budget rather than a formality. Published in the
capability manifest and rendered by the composer from there.
"""


class AttachedReportError(Exception):
    """An attachment that cannot be honored as asked."""

    def __init__(self, messages: "Sequence[str]") -> None:
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


def resolve_attached_reports(
    report_ids: "Sequence[str]",
    *,
    run_store: Any,
    visible_to: "UserContext | None",
) -> list[dict[str, Any]]:
    """``{report_id, title, reference_count}`` per attached report.

    The title is the run's QUESTION: a research report carries no title
    of its own — not one of them has an H1 — and the question is what the
    Research Desk itself shows as the name.

    Raises:
        AttachedReportError: An unknown or invisible id, a run that is
            not a completed research report, or more than the cap.
    """
    wanted: list[str] = []
    for item in report_ids:
        value = str(item).strip()
        if value and value not in wanted:
            wanted.append(value)
    if not wanted:
        return []
    if len(wanted) > MAX_ATTACHED_REPORTS:
        raise AttachedReportError(
            [
                f'At most {MAX_ATTACHED_REPORTS} research reports per assignment (attached: {len(wanted)}).'
            ]
        )
    if run_store is None:
        raise AttachedReportError(
            ["Research reports are unavailable in this instance."]
        )
    resolved: list[dict[str, Any]] = []
    unknown: list[str] = []
    unfinished: list[str] = []
    for report_id in wanted:
        try:
            summary = run_store.get(report_id, visible_to=visible_to)
        except Exception:  # noqa: BLE001 — every failure is "not visible"
            unknown.append(report_id)
            continue
        if str(summary.get("status") or "") != "completed":
            unfinished.append(report_id)
            continue
        resolved.append(
            {
                "report_id": report_id,
                "title": str(summary.get("question") or report_id).strip(),
                "reference_count": _reference_count(
                    run_store, report_id, visible_to
                ),
            }
        )
    if unknown:
        raise AttachedReportError(
            ["Unknown or non-visible report: " + ", ".join(unknown)]
        )
    if unfinished:
        raise AttachedReportError(
            [
                "Report not completed yet: "
                + ", ".join(unfinished)
            ]
        )
    return resolved


def _reference_count(
    run_store: Any, report_id: str, visible_to: "UserContext | None"
) -> int:
    """How many sources the report carries, for the registry line.

    Best-effort: the count is a hint for the model, so a store that
    cannot answer costs the hint, never the attachment.
    """
    try:
        payload = run_store.result(report_id, visible_to=visible_to)
    except Exception:  # noqa: BLE001
        return 0
    references = payload.get("references") or []
    return len(references) if isinstance(references, list) else 0
