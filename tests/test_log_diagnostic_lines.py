"""Diagnostic lines an operator needs: what started, how it ended, what was retried.

Lines carry the mode, a status, durations, ids and codes; never content.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.observability.context import current_log_context

MARKER = "zzmarker7"


def _messages(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if needle in record.getMessage()]


# --------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------- #


def _chat(fn: Any) -> Any:
    from inqtrix.observability.otel import chat_thread_call

    return chat_thread_call(fn, mode="direct_llm", principal=None)


def test_a_chat_turn_logs_its_start_and_end(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix"):
        _chat(lambda: SimpleNamespace(raw={"usage": {}}, cancelled=False))()

    lines = _messages(caplog, "Chat ")
    assert lines[0] == "Chat started (mode=direct_llm)."
    assert lines[1].startswith("Chat ended (status=completed, mode=direct_llm, seconds=")
    assert len(lines) == 2


@pytest.mark.parametrize(
    ("exc_name", "expected"),
    [
        ("AgentTokenBudgetExceeded", "status=failed, failure_code=token_budget_exceeded, error_type=AgentTokenBudgetExceeded"),
        ("AgentCancelled", "status=cancelled"),
    ],
)
def test_a_chat_turn_that_raises_ends_by_its_class(
    caplog: pytest.LogCaptureFixture, exc_name: str, expected: str
) -> None:
    import inqtrix.exceptions as exceptions

    exc_type = getattr(exceptions, exc_name)

    def _raise() -> Any:
        raise exc_type(MARKER)

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        with pytest.raises(exc_type):
            _chat(_raise)()

    (ended,) = _messages(caplog, "Chat ended")
    assert expected in ended
    assert MARKER not in ended


@pytest.mark.parametrize(
    ("result_state", "expected"),
    [
        (
            {"cancelled": True, "cancel_reason": "token_budget_exceeded"},
            "status=failed, failure_code=token_budget_exceeded",
        ),
        ({"cancelled": True, "cancel_reason": "client_requested_cancel"}, "status=cancelled"),
        (
            {"_terminal_failure": {"type": "rate_limited", "message": MARKER}},
            "status=failed, failure_code=rate_limited",
        ),
    ],
    ids=["budget", "client-cancel", "terminal-failure"],
)
def test_a_chat_turn_that_returns_a_stop_ends_by_its_class(
    caplog: pytest.LogCaptureFixture, result_state: dict[str, Any], expected: str
) -> None:
    from inqtrix.core.results import AgentResult

    result = AgentResult(answer="", raw={"answer": "", "usage": {}, "result_state": result_state})
    with caplog.at_level(logging.INFO, logger="inqtrix"):
        _chat(lambda: result)()

    (ended,) = _messages(caplog, "Chat ended")
    assert ended.startswith(f"Chat ended ({expected}, mode=direct_llm, seconds=")
    assert MARKER not in ended


# --------------------------------------------------------------------- #
# Upload retries
# --------------------------------------------------------------------- #


def test_an_upload_retry_is_logged_once_it_landed(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.services.upload_operation_service import (
        UploadExecutionDeferred,
        UploadOperationService,
    )

    record = SimpleNamespace(
        operation_id="op_1", tenant_id="t", created_by_user_id="u", workspace_id="w"
    )
    class Operations:
        async def aqueue_retry(self, *args: Any, **kwargs: Any) -> bool:
            return True

        async def aget(self, *args: Any, **kwargs: Any) -> Any:
            return record

    service = UploadOperationService.__new__(UploadOperationService)
    service.operations = Operations()
    service._max_attempts = 3

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with pytest.raises(UploadExecutionDeferred):
            asyncio.run(
                service._defer_dependency_failure(
                    record, 1, RuntimeError(MARKER), error_type="dependency_unavailable"
                )
            )

    (line,) = _messages(caplog, "op_1")
    assert "retry" in line
    assert "attempt 1 of 3" in line
    assert "failure_code=dependency_unavailable" in line
    assert MARKER not in line


def test_an_upload_retry_that_did_not_land_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.services.upload_operation_service import (
        UploadAttemptSuperseded,
        UploadOperationService,
    )

    record = SimpleNamespace(
        operation_id="op_1", tenant_id="t", created_by_user_id="u", workspace_id="w"
    )
    service = UploadOperationService.__new__(UploadOperationService)

    class Operations:
        async def aqueue_retry(self, *args: Any, **kwargs: Any) -> bool:
            return False

    service.operations = Operations()
    service._max_attempts = 3

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with pytest.raises(UploadAttemptSuperseded):
            asyncio.run(
                service._defer_dependency_failure(
                    record, 1, RuntimeError(MARKER), error_type="dependency_unavailable"
                )
            )

    assert not _messages(caplog, "op_1")


# --------------------------------------------------------------------- #
# Job context of the upload and deletion loops
# --------------------------------------------------------------------- #


async def _acknowledged(message_id: str) -> None:
    del message_id


def test_the_upload_loop_binds_the_job_context() -> None:
    from inqtrix.worker.upload_loop import UploadWorkerLoop

    seen: dict[str, str] = {}

    def _execute_claimed(claimed: Any) -> None:
        seen.update(current_log_context())

    loop = UploadWorkerLoop.__new__(UploadWorkerLoop)
    loop._service = SimpleNamespace(execute_claimed=_execute_claimed)
    loop._store = SimpleNamespace(is_attempt_current=lambda *args, **kwargs: False)
    loop._queue = SimpleNamespace(ack=_acknowledged)
    loop._active = {}
    job = SimpleNamespace(message_id="m1", operation_id="upl_1", tenant_id="tenant-a")

    loop._execute(job, SimpleNamespace(attempt=1), threading.Event())

    assert seen.get("run_id") == "upl_1"
    assert seen.get("tenant") == "tenant-a"


def test_the_deletion_loop_binds_the_job_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from inqtrix.worker import deletion_loop
    from inqtrix.worker.deletion_loop import DeletionWorkerLoop

    seen: dict[str, str] = {}

    def _execute_claimed_deletion(store: Any, service: Any, claimed: Any) -> Any:
        seen.update(current_log_context())
        return SimpleNamespace(terminal_landed=True)

    monkeypatch.setattr(deletion_loop, "execute_claimed_deletion", _execute_claimed_deletion)
    loop = DeletionWorkerLoop.__new__(DeletionWorkerLoop)
    loop._service = SimpleNamespace()
    loop._store = SimpleNamespace(worker_id="w1")
    loop._queue = SimpleNamespace(ack=_acknowledged)
    loop._active = {}
    job = SimpleNamespace(message_id="m1", operation_id="del_1", tenant_id="tenant-a")

    loop._execute(job, SimpleNamespace(attempt=1), threading.Event())

    assert seen.get("run_id") == "del_1"
    assert seen.get("tenant") == "tenant-a"


# --------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------- #


def test_a_loop_stop_request_is_not_an_info_line(caplog: pytest.LogCaptureFixture) -> None:
    from inqtrix.worker.loop import BaseWorkerLoop

    loop = BaseWorkerLoop.__new__(BaseWorkerLoop)
    loop._store = SimpleNamespace(worker_id="w1")
    loop._stop = asyncio.Event()

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        loop.request_stop()

    assert loop._stop.is_set()
    assert not [record for record in caplog.records if record.levelno >= logging.INFO]
