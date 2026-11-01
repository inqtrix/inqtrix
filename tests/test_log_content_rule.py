"""Log lines keep their diagnosis and lose the content.

Each test puts a marker where model output, user text or a configuration value
used to reach a log line (a review detail, an identifier the model made up, a
title, a URL, an override value, a validation input) and checks that the
line still says what happened, without the marker.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

MARKER = "zzmarker7"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


# --------------------------------------------------------------------- #
# Failure details at their source
# --------------------------------------------------------------------- #


class _Answer(BaseModel):
    title: str
    count: int


def _validation_error() -> ValidationError:
    try:
        _Answer.model_validate({"title": 7, "count": MARKER})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


def test_a_validation_failure_detail_names_types_and_fields_only() -> None:
    from inqtrix.execution_failures import safe_failure_detail

    detail = safe_failure_detail(_validation_error())

    assert MARKER not in detail
    assert "string_type@title" in detail
    assert "int_parsing@count" in detail


def test_a_structured_output_failure_detail_reads_its_source() -> None:
    from langchain.agents.structured_output import StructuredOutputValidationError
    from langchain_core.messages import AIMessage

    from inqtrix.execution_failures import safe_failure_detail

    exc = StructuredOutputValidationError("answer", _validation_error(), AIMessage(content=MARKER))

    detail = safe_failure_detail(exc)
    assert MARKER not in detail
    assert "int_parsing@count" in detail


class _Scores(BaseModel):
    scores: dict[str, int]


def test_a_validation_location_hides_keys_taken_from_the_data() -> None:
    from inqtrix.execution_failures import safe_failure_detail

    try:
        _Scores.model_validate({"scores": {f"Berlin {MARKER}": "x"}})
    except ValidationError as exc:
        detail = safe_failure_detail(exc)
    else:
        raise AssertionError("expected a validation error")

    assert MARKER not in detail
    assert detail == "int_parsing@scores.<key>"


def test_any_other_failure_detail_is_its_type_name() -> None:
    from inqtrix.execution_failures import safe_failure_detail

    assert safe_failure_detail(RuntimeError(MARKER)) == "RuntimeError"


def test_a_role_result_that_does_not_validate_keeps_no_model_text() -> None:
    from inqtrix.agents.kernel.work_profiles import WORK_RESULT_COMPLETED, settle_role_child

    profile = SimpleNamespace(name="probe", result_model=_Answer)
    child = {"status": "completed"}
    stored = {
        "work_result": {
            "profile": "probe",
            "status": WORK_RESULT_COMPLETED,
            "value": {"title": MARKER, "count": MARKER},
        }
    }

    outcome = settle_role_child(child, stored, profile)

    assert outcome is not None
    assert outcome.reason
    assert MARKER not in outcome.detail
    assert "int_parsing@count" in outcome.detail


def test_a_failed_role_child_is_named_by_its_error_type() -> None:
    from inqtrix.agents.kernel.work_profiles import settle_role_child

    profile = SimpleNamespace(name="probe", result_model=_Answer)
    child = {"status": "failed", "error": {"type": "server_error", "message": f"raw {MARKER}"}}

    outcome = settle_role_child(child, None, profile)

    assert outcome is not None
    assert MARKER not in outcome.detail
    assert outcome.detail == "server_error"


# --------------------------------------------------------------------- #
# Review details and identifiers the model wrote
# --------------------------------------------------------------------- #


def test_a_review_without_verdict_logs_reason_transport_and_length(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.agents.kernel.review_pass import TRANSPORT_ROLE, Review, reader_detail

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        reader_detail(Review(transport=TRANSPORT_ROLE, reason="schema_invalid", detail=f"{{{MARKER}}}"), "de")
        reader_detail(Review(transport=TRANSPORT_ROLE, reason="no_such_reason", detail=MARKER), "en")

    lines = _warnings(caplog)
    assert len(lines) == 2
    assert all(MARKER not in line for line in lines)
    assert "schema_invalid" in lines[0] and TRANSPORT_ROLE in lines[0]
    assert "detail_chars=11" in lines[0]
    assert "no_such_reason" in lines[1]


def test_identifiers_the_model_wrote_into_review_prose_are_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.agents.kernel import review_pass

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        cleaned = review_pass._without_stray_ids(
            f"Die Frist in source:ref_{MARKER} und source:ref_{MARKER}b blieb offen.", "de"
        )

    assert MARKER not in cleaned
    lines = _warnings(caplog)
    assert len(lines) == 1
    assert MARKER not in lines[0]
    assert "2" in lines[0]


def test_a_refused_review_edit_logs_its_reason_only(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.agents.kernel.review_edits import _refused

    item = {
        "finding_id": "finding_1",
        "origin": "review",
        "output_id": "answer",
        "kind": "evidence",
        "finding": f"Die Zahl fehlt {MARKER}",
        "quote": f"12,5 % laut {MARKER}",
    }
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _refused(item, "edit_drops_figure")

    (line,) = _warnings(caplog)
    assert line == "Review edit finding_1 not applied: edit_drops_figure."


# --------------------------------------------------------------------- #
# Titles, URLs, values and model text
# --------------------------------------------------------------------- #


def test_an_override_logs_field_names_not_values(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.services.overrides import AgentOverridesRequest, apply_overrides
    from inqtrix.settings import AgentSettings

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        apply_overrides(AgentSettings(), AgentOverridesRequest(model=f"gpt-{MARKER}"))

    lines = [record.getMessage() for record in caplog.records if "override" in record.getMessage().lower()]
    assert lines
    assert all(MARKER not in line for line in lines)
    assert any("model" in line for line in lines)


def test_a_label_shaped_reference_logs_no_url(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.agents.report_quality import _label_shaped

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _label_shaped("W4", {"url": f"https://example.org/{MARKER}?q={MARKER}"})

    lines = _warnings(caplog)
    assert lines
    assert all(MARKER not in line for line in lines)


def test_an_omitted_contextualization_batch_is_named_by_number(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.knowledge.contextualize import LLMChunkContextualizer

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        LLMChunkContextualizer._omitted_batch(batch_number=2, expected=3, how="refused")

    (line,) = _warnings(caplog)
    assert line == (
        "contextualization batch 2 was refused by the provider content filter; "
        "its 3 chunks are indexed without context"
    )


def test_unusable_covered_aspects_are_counted(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.nodes import _apply_evaluated_coverage

    state: dict[str, Any] = {"required_aspects": ["a", "b"], "round": 1}
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _apply_evaluated_coverage(
            state, f'COVERED_ASPECTS: ["{MARKER}", "{MARKER}x"]', evaluate_model="m"
        )

    lines = _warnings(caplog)
    assert lines
    assert all(MARKER not in line for line in lines)
    assert any("2" in line for line in lines)


def test_contextualization_retries_name_no_document_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.knowledge.contextualize import LLMChunkContextualizer
    from tests.test_contextualize import FilteringLLM, _thirty_chunks

    document, slices = _thirty_chunks()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        LLMChunkContextualizer(FilteringLLM(batch_chunks=5, times=2)).contextualize(
            document_title=f"Vertrag {MARKER}.pdf", document_text=document, chunks=slices
        )

    lines = _warnings(caplog)
    assert any("one visible retry" in line for line in lines)
    assert any("blocked twice" in line for line in lines)
    assert all(MARKER not in line for line in lines)


def test_discarded_checkpoints_name_no_document_title(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.knowledge.chunking import ChunkSlice
    from inqtrix.knowledge.contextualize import LLMChunkContextualizer
    from tests.test_contextualize import ScriptedContextLLM

    title = f"Vertrag {MARKER}.pdf"
    chunks = [ChunkSlice("a", 0, 1)]
    captured: list[Any] = []
    LLMChunkContextualizer(ScriptedContextLLM(["Kontext"])).contextualize(
        document_title=title, document_text="a", chunks=chunks, on_batch_checkpoint=captured.append
    )
    invalid = dataclasses.replace(captured[0], contexts=("",))

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        LLMChunkContextualizer(ScriptedContextLLM(["Neuer Kontext"])).contextualize(
            document_title=title, document_text="a", chunks=chunks, completed_batches=[invalid]
        )

    lines = [line for line in _warnings(caplog) if "checkpoints discarded" in line]
    assert lines
    assert all(MARKER not in line for line in lines)


def test_a_source_role_conflict_names_the_source_not_its_domain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.nodes import _pin_source_roles

    url = f"https://{MARKER}.example/bericht"
    record = {"canonical_url": url, "source_id": "src_1", "domain": f"{MARKER}.example"}
    state: dict[str, Any] = {"source_records": {"src_1": record}}
    roles = [
        {"url": url, "role": "reporting", "reason": "erste"},
        {"url": url, "role": "commentary_stakeholder", "reason": "zweite"},
    ]

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _applied, conflicts = _pin_source_roles(state, [record], roles)

    assert conflicts == 1
    (line,) = _warnings(caplog)
    assert MARKER not in line
    assert "source src_1" in line


# --------------------------------------------------------------------- #
# Retries, replans, revisions and skills
# --------------------------------------------------------------------- #


def test_a_transient_retry_names_the_task_form_and_the_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.agents.scheduler import TaskOutcome, execute_wave

    def executor(task: Any, attempt: int) -> Any:
        if attempt == 1:
            return TaskOutcome(
                status="failed",
                transient=True,
                failure_code="rate_limited",
                failure_reason=f"provider said {MARKER}",
            )
        return TaskOutcome(status="completed", summary="ok")

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        execute_wave(
            [SimpleNamespace(task_id=f"Plan {MARKER}")],
            executor=executor,
            max_parallel=1,
            cancelled=lambda: False,
            language="de",
        )

    (line,) = [line for line in _warnings(caplog) if "retrying" in line]
    assert MARKER not in line
    assert "Task <task> failed transiently (failure_code=rate_limited)" in line


def test_a_failed_replan_counts_its_dropped_analyses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from inqtrix.agents import planner
    from inqtrix.agents.planner import PlanningFailed
    from tests.agents.test_execution_resume_contract import _ScriptedPlannerLLM, _replan
    from tests.agents.test_execution_resume_contract import _task as _record_task
    from tests.agents.test_plan_validation import _analysis_delta, _new_task, _previous

    def leaves_the_synthesis_dependency(plan: Any, ids: set[str]) -> Any:
        # The validation after the drop fails, so the line is written.
        return plan.model_copy(update={"tasks": [task for task in plan.tasks if task.id not in ids]})

    monkeypatch.setattr(planner, "without_tasks", leaves_the_synthesis_dependency)
    dropped = f"Analyse_{MARKER}"
    delta = _analysis_delta(_new_task(dropped, "file_analysis"), _new_task("t4", "web_instant"))
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with pytest.raises(PlanningFailed):
            _replan(
                _ScriptedPlannerLLM([delta, dict(delta)]),
                _previous(_record_task("t1", "file_analysis", status="completed")),
                drop_new_task=lambda task: task.task_id == dropped,
            )

    (line,) = [line for line in _warnings(caplog) if "dropped analyses" in line]
    assert MARKER not in line
    assert "without the 1 dropped analyses" in line


def test_revision_ids_the_review_did_not_locate_are_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.agents.kernel.review_edits import apply_review_edits
    from inqtrix.agents.phase_models import DeepFindingRevision, DeepRevisionBundle

    bundle = DeepRevisionBundle(
        edits=[DeepFindingRevision(finding_id=f"finding_{MARKER}", replacement="Neu.")]
    )
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        apply_review_edits({}, [], bundle, allowed_references={}, identifiers=[])

    (line,) = _warnings(caplog)
    assert MARKER not in line
    assert "names 1 finding id(s)" in line


def _skill() -> SimpleNamespace:
    return SimpleNamespace(
        id="skl_1",
        label=f"Reise {MARKER}",
        clarification_points=[{"id": "p1", "name": "budget", "question": "Budget?"}],
    )


def _check_points(script: Any) -> None:
    from inqtrix.agents.skills_runtime import check_skill_points
    from tests.agents.test_workspace_agent import ScriptedLLM

    check_skill_points(
        ScriptedLLM(overrides={"SkillPointCheck": script}),
        skill=_skill(),
        question="Wohin?",
        history="",
        model=None,
        reasoning_effort=None,
        timeout=5.0,
    )


def test_a_skill_point_check_names_the_skill_by_its_id(caplog: pytest.LogCaptureFixture) -> None:
    unknown_point = {"points": [{"id": f"p_{MARKER}", "answered": True, "answer_from_context": "x"}]}
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _check_points(unknown_point)
        _check_points({"points": "not a list"})

    lines = [line for line in _warnings(caplog) if "SkillPointCheck for skill" in line]
    assert len(lines) == 2
    assert all("skl_1" in line and MARKER not in line for line in lines)


# --------------------------------------------------------------------- #
# Task references
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        ("t1", "t1"),
        ("t123", "t123"),
        ("s", "s"),
        ("t1234", "<task>"),
        ("Berlin_Mueller", "<task>"),
        ("Plan für Berlin", "<task>"),
        ("", "<task>"),
    ],
)
def test_task_ids_appear_only_in_the_planner_form(task_id: str, expected: str) -> None:
    from inqtrix.agents.plan_models import loggable_task_id

    assert loggable_task_id(task_id) == expected


def test_a_task_label_without_title_logs_the_planner_form(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.agents.algorithm import _task_label

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        _task_label(f"Plan {MARKER}", None, {"language": "de"})

    (line,) = _warnings(caplog)
    assert MARKER not in line
    assert "task <task>" in line


# --------------------------------------------------------------------- #
# Reader sentences promise no raw text in the log
# --------------------------------------------------------------------- #


def test_review_reader_sentences_promise_the_reason_not_the_details() -> None:
    from inqtrix.i18n import MESSAGES

    texts = [
        text
        for key, columns in MESSAGES.items()
        if key.startswith("review_unavailable_")
        for text in columns.values()
    ]
    assert texts
    promises = ("Einzelheiten", "details", "Rohtext", "raw text")
    assert not [text for text in texts if any(word in text for word in promises)]
