"""Durable Agent Desk answer publication ordering and fencing."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import ArtifactRevisionConflict
from inqtrix.execution_failures import RunExecutionFailure
from inqtrix.server.runs import RunHandle
from inqtrix.services.agent_answer_publisher import (
    AgentAnswerPublisher,
    answer_artifact_id,
)
from inqtrix.sync_bridge import run_coro_sync
from inqtrix.worker.loop import FencedRunHandle


class _EventStore:
    def __init__(self, control: MemoryAgentControlStore) -> None:
        self.control = control
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.snapshots: list[tuple[str, str, str, int]] = []

    def emit(
        self,
        _run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        fence_attempt: int | None = None,
    ) -> None:
        del fence_attempt
        self.events.append((event_type, dict(payload)))
        artifact_id = str(payload.get("artifact_id") or "")
        if artifact_id:
            artifact, _revisions = run_coro_sync(
                self.control.get_artifact(_run_id, artifact_id)
            )
            self.snapshots.append(
                (
                    event_type,
                    artifact.status,
                    artifact.content_markdown,
                    artifact.revision,
                )
            )


def _handle(
    control: MemoryAgentControlStore, run_id: str = "run_publication_order"
) -> tuple[RunHandle, _EventStore]:
    events = _EventStore(control)
    return RunHandle(events, run_id, threading.Event()), events  # type: ignore[arg-type]


def test_answer_body_is_hidden_until_final_revision_precedes_ready() -> None:
    control = MemoryAgentControlStore()
    handle, events = _handle(control)
    references = [
        {
            "label": "W2",
            "url": "https://example.test/source",
            "title": "Primärquelle",
            "tier": "primary",
        }
    ]

    AgentAnswerPublisher(control).publish(
        handle,
        "Belastbare Antwort [W2].",
        references=references,
    )

    event_types = [event_type for event_type, _payload in events.events]
    assert event_types[0:2] == [
        "inqtrix.agent.artifact.created",
        "inqtrix.answer.started",
    ]
    assert event_types[-2:] == [
        "inqtrix.agent.artifact.updated",
        "inqtrix.answer.ready",
    ]
    started = next(
        snapshot
        for snapshot in events.snapshots
        if snapshot[0] == "inqtrix.answer.started"
    )
    assert started[1:] == ("writing", "", 1)
    for snapshot in events.snapshots:
        if snapshot[0] == "inqtrix.output_text.delta":
            assert snapshot[1:] == ("writing", "", 1)
    ready = next(
        snapshot
        for snapshot in events.snapshots
        if snapshot[0] == "inqtrix.answer.ready"
    )
    assert ready[1:] == ("ready", "Belastbare Antwort [W2].", 2)

    artifact, _revisions = run_coro_sync(
        control.get_artifact(handle.run_id, events.events[0][1]["artifact_id"])
    )
    assert list(artifact.refs) == references


def test_redelivery_clears_previous_body_before_restreaming() -> None:
    control = MemoryAgentControlStore()
    publisher = AgentAnswerPublisher(control)
    first, _events = _handle(control, "run_publication_retry")
    publisher.publish(first, "Erste Antwort.", references=[])

    retry, retry_events = _handle(control, "run_publication_retry")
    publisher.publish(retry, "Korrigierte Antwort.", references=[])

    assert retry_events.events[0][0] == "inqtrix.agent.artifact.updated"
    started = next(
        snapshot
        for snapshot in retry_events.snapshots
        if snapshot[0] == "inqtrix.answer.started"
    )
    assert started[1:] == ("writing", "", 3)
    artifact, _revisions = run_coro_sync(
        control.get_artifact(
            retry.run_id, retry_events.events[0][1]["artifact_id"]
        )
    )
    assert artifact.status == "ready"
    assert artifact.content_markdown == "Korrigierte Antwort."
    assert artifact.revision == 4


class _FinalizeConflictStore(MemoryAgentControlStore):
    async def upsert_artifact(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        if kwargs.get("kind") == "answer" and kwargs.get("status") == "ready":
            raise ArtifactRevisionConflict(1)
        return await super().upsert_artifact(**kwargs)


def test_finalize_conflict_interrupts_stream_and_never_emits_ready() -> None:
    control = _FinalizeConflictStore()
    handle, events = _handle(control, "run_publication_conflict")

    with pytest.raises(RunExecutionFailure) as failure:
        AgentAnswerPublisher(control).publish(
            handle,
            "Diese Antwort darf nicht vorzeitig sichtbar sein.",
            references=[],
        )

    assert failure.value.error_type == "answer_publication_conflict"
    assert events.events[-1][0] == "inqtrix.answer.interrupted"
    assert events.events[-1][1]["stage"] == "finalizing"
    assert not any(
        event_type == "inqtrix.answer.ready"
        for event_type, _payload in events.events
    )
    artifact, _revisions = run_coro_sync(
        control.get_artifact(
            handle.run_id, events.events[0][1]["artifact_id"]
        )
    )
    assert artifact.status == "writing"
    assert artifact.content_markdown == ""


class _AttemptRecordingStore(MemoryAgentControlStore):
    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[int | None] = []

    async def upsert_artifact(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.attempts.append(kwargs.get("expected_run_attempt"))
        return await super().upsert_artifact(**kwargs)


def test_fenced_worker_attempt_reaches_both_artifact_transactions() -> None:
    control = _AttemptRecordingStore()
    events = _EventStore(control)
    handle = FencedRunHandle(
        events,  # type: ignore[arg-type]
        "run_publication_fenced",
        threading.Event(),
        attempt=7,
    )

    AgentAnswerPublisher(control).publish(handle, "Antwort.", references=[])

    assert control.attempts == [7, 7]


def test_publication_fence_with_pending_cancel_is_a_cancellation() -> None:
    """A fence that fires because the user's cancel superseded the attempt
    must terminalize as cancelled, not as a technical failure."""
    from inqtrix.agents.control_ports import (
        ArtifactNotFound,
        ArtifactPublicationFenced,
    )
    from inqtrix.exceptions import AgentCancelled

    class _FencingStore:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def emit(
            self,
            _run_id: str,
            event_type: str,
            payload: dict[str, Any],
            **_kwargs: Any,
        ) -> None:
            self.events.append((event_type, dict(payload)))

        async def get_artifact(self, run_id: str, artifact_id: str) -> Any:
            raise ArtifactNotFound(artifact_id)

        async def upsert_artifact(self, **_kwargs: Any) -> Any:
            raise ArtifactPublicationFenced(
                expected_attempt=1, current_attempt=2, status="running"
            )

    store = _FencingStore()
    handle = RunHandle(store, "run_cancel_fence_race", threading.Event())  # type: ignore[arg-type]
    handle.cancel_event.set()

    with pytest.raises(AgentCancelled):
        AgentAnswerPublisher(store).publish(  # type: ignore[arg-type]
            handle,
            "Nie publizierte Antwort.",
            references=[],
        )
    assert store.events[-1][0] == "inqtrix.answer.interrupted"


def test_publication_fence_without_cancel_stays_a_typed_failure() -> None:
    """Without a pending cancel the fence keeps its honest failure type."""
    from inqtrix.agents.control_ports import (
        ArtifactNotFound,
        ArtifactPublicationFenced,
    )

    class _FencingStore:
        def emit(
            self,
            _run_id: str,
            _event_type: str,
            _payload: dict[str, Any],
            **_kwargs: Any,
        ) -> None:
            return None

        async def get_artifact(self, run_id: str, artifact_id: str) -> Any:
            raise ArtifactNotFound(artifact_id)

        async def upsert_artifact(self, **_kwargs: Any) -> Any:
            raise ArtifactPublicationFenced(
                expected_attempt=1, current_attempt=2, status="running"
            )

    store = _FencingStore()
    handle = RunHandle(store, "run_plain_fence", threading.Event())  # type: ignore[arg-type]

    with pytest.raises(RunExecutionFailure) as excinfo:
        AgentAnswerPublisher(store).publish(  # type: ignore[arg-type]
            handle,
            "Nie publizierte Antwort.",
            references=[],
        )
    assert excinfo.value.error_type == "answer_publication_fenced"


def test_fence_with_committed_cancel_flag_cancels_before_poller_notices() -> None:
    """The store's own fence status carries the committed cancel flag; the
    classification must not depend on the 2s cancel poller having set the
    local event yet."""
    from inqtrix.agents.control_ports import (
        ArtifactNotFound,
        ArtifactPublicationFenced,
    )
    from inqtrix.exceptions import AgentCancelled

    class _FencingStore:
        def emit(
            self,
            _run_id: str,
            _event_type: str,
            _payload: dict[str, Any],
            **_kwargs: Any,
        ) -> None:
            return None

        async def get_artifact(self, run_id: str, artifact_id: str) -> Any:
            raise ArtifactNotFound(artifact_id)

        async def upsert_artifact(self, **_kwargs: Any) -> Any:
            raise ArtifactPublicationFenced(
                expected_attempt=1,
                current_attempt=1,
                status="cancel_requested",
            )

    store = _FencingStore()
    handle = RunHandle(store, "run_committed_cancel", threading.Event())  # type: ignore[arg-type]
    assert not handle.cancel_event.is_set()

    with pytest.raises(AgentCancelled):
        AgentAnswerPublisher(store).publish(  # type: ignore[arg-type]
            handle,
            "Nie publizierte Antwort.",
            references=[],
        )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Fasse den Quartalsbericht für die Geschäftsführung zusammen.", "Antwort"),
        ("Summarize the quarterly report for the board.", "Answer"),
    ],
)
def test_the_answer_row_is_named_by_the_rule_the_run_answers_by(
    question: str, expected: str
) -> None:
    """The row's name is the one piece of reader text this transport owns.

    It is not resolved by ``detect_ui_language``, which is the
    PROVISIONAL rule a run starts with and replaces; every boundary that
    names something for a reader uses ``resolve_answer_language``.
    Two rules for one question is how a run ends up with an "Answer" row
    beside an "Antwort" memo.

    This pins the two languages, not the resolver change: measured
    across the shapes that occur, the two rules render the SAME title
    everywhere, because an unresolved language falls back to English
    exactly as the provisional rule does. What still diverges is the
    classifier's decision, which does not travel with the answer - so a
    German run whose question carries no signal names this row "Answer"
    beside an "Antwort" memo. Closing that means carrying the language on
    the export payload, which is a change to the wire and a cut of its
    own; it is written down here so the next reader does not mistake a
    green test for that repair.
    """
    control = MemoryAgentControlStore()
    handle, _events = _handle(control, run_id="run_answer_language")
    publisher = AgentAnswerPublisher(control)

    publisher.publish(handle, "Die Antwort.", references=[], question=question)

    artifact, _revisions = run_coro_sync(
        control.get_artifact(
            "run_answer_language", answer_artifact_id("run_answer_language")
        )
    )
    assert artifact.title == expected
