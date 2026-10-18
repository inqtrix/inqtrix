"""Research reports as agent input.

The operator's use case: take two finished research reports, apply a
prompt-library rule, and write a Sprechzettel — then fact-check one
paragraph and ADD the new source to the document's source list.

The interesting properties are not "a field arrives" but: the attachment
is the consent, a report's own sources become citable in the new run, and
nothing is ever dropped in silence.
"""

from __future__ import annotations

from typing import Any

import pytest

from inqtrix.services.attached_report_resolver import (
    MAX_ATTACHED_REPORTS,
    AttachedReportError,
    resolve_attached_reports,
)


class _Store:
    """A run store with two completed reports and one still running."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {
            "run_a": {"status": "completed", "question": "Batterieverordnung?"},
            "run_b": {"status": "completed", "question": "AI Act?"},
            "run_live": {"status": "running", "question": "Laeuft noch"},
        }
        self.results: dict[str, dict[str, Any]] = {
            "run_a": {"answer": "## Bericht", "references": [{"label": "E1"}]},
            "run_b": {"answer": "## Bericht", "references": []},
        }

    def get(self, run_id: str, *, visible_to: Any = None) -> dict[str, Any]:
        if run_id not in self.rows:
            raise KeyError(run_id)
        return self.rows[run_id]

    def result(self, run_id: str, *, visible_to: Any = None) -> dict[str, Any]:
        return self.results[run_id]


def test_a_report_is_named_by_its_question():
    """A research report has no title — not one of them carries an H1 —
    so the run's question is what the Research Desk shows and what the
    registry line must use."""
    resolved = resolve_attached_reports(
        ["run_a"], run_store=_Store(), visible_to=None
    )
    assert resolved == [
        {
            "report_id": "run_a",
            "title": "Batterieverordnung?",
            "reference_count": 1,
        }
    ]


def test_an_invisible_report_refuses_the_submission():
    """Silently dropping it would start a run under inputs the user
    believes are attached."""
    with pytest.raises(AttachedReportError) as excinfo:
        resolve_attached_reports(
            ["run_a", "run_missing"], run_store=_Store(), visible_to=None
        )
    assert "run_missing" in str(excinfo.value)


def test_an_unfinished_report_refuses_instead_of_attaching_nothing():
    """A running report has no text at all; attaching it would give the
    model a name it can never read."""
    with pytest.raises(AttachedReportError) as excinfo:
        resolve_attached_reports(
            ["run_live"], run_store=_Store(), visible_to=None
        )
    assert "run_live" in str(excinfo.value)


def test_the_cap_is_a_refusal_not_a_trim():
    """No silent caps: attaching a fourth report must not quietly drop
    one of the first three."""
    with pytest.raises(AttachedReportError) as excinfo:
        resolve_attached_reports(
            [f"run_{index}" for index in range(MAX_ATTACHED_REPORTS + 1)],
            run_store=_Store(),
            visible_to=None,
        )
    assert str(MAX_ATTACHED_REPORTS) in str(excinfo.value)


def test_the_same_report_twice_is_one_attachment():
    resolved = resolve_attached_reports(
        ["run_a", "run_a"], run_store=_Store(), visible_to=None
    )
    assert len(resolved) == 1


def test_a_store_that_cannot_count_sources_still_attaches():
    """The count is a hint for the model; losing it must never cost the
    attachment itself."""

    class _NoResults(_Store):
        def result(self, run_id: str, *, visible_to: Any = None):
            raise RuntimeError("no result access")

    resolved = resolve_attached_reports(
        ["run_a"], run_store=_NoResults(), visible_to=None
    )
    assert resolved[0]["reference_count"] == 0
    assert resolved[0]["title"] == "Batterieverordnung?"


def test_the_registry_line_names_reports_without_inlining_them():
    """Two real reports are ~107k characters; inlining them would freeze
    that into the first user message and repeat it every model turn."""
    from inqtrix.agents.prompts import build_kernel_user_message

    message = build_kernel_user_message(
        "Schreibe einen Sprechzettel.",
        attached_reports=[
            {"report_id": "run_a", "title": "Batterie", "reference_count": 14}
        ],
    )
    assert "read_research_report" in message
    assert "run_a" in message
    assert "Batterie" in message
    assert "14 sources" in message
    assert "the text is NOT here" in message


def test_an_attached_report_reaches_the_kernel_with_the_title_the_user_saw():
    """The kernel takes the attachment's id as the consent and its title as
    the words of a read row, so the row never shows the run id."""
    pytest.importorskip("deepagents")
    from types import SimpleNamespace

    from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm, _report_args_preview

    algo = KernelAgentAlgorithm(
        control_store=None,  # type: ignore[arg-type]
        checkpointer=None,  # type: ignore[arg-type]
        platform=SimpleNamespace(),  # type: ignore[arg-type]
    )
    context = SimpleNamespace(
        agent_settings=SimpleNamespace(), authority_check=None, cancel_token=None,
        event_sink=lambda *_: None, principal=None, providers=SimpleNamespace(search=None),
        run_id="run_new", token_budget=0, workspace_id="",
    )
    request = SimpleNamespace(
        attached_reports=[
            {"report_id": "run_a", "title": "Batterieverordnung?", "reference_count": 1}
        ],
        document_id="", knowledge_filters={}, messages=[],
        question="Schreibe einen Sprechzettel.",
    )
    deps = algo._build_deps("run_new", None, context, request)
    assert deps.attached_report_ids == ("run_a",)
    assert _report_args_preview({"report_id": "run_a"}, deps) == "Batterieverordnung?"


def test_no_attachment_adds_no_section():
    from inqtrix.agents.prompts import build_kernel_user_message

    assert "Attached research reports" not in build_kernel_user_message("Auftrag.")
