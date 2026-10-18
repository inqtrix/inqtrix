"""Shared fail-closed checkpoint restart contract for both agent engines."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from inqtrix.agents.checkpoint_guard import (
    CheckpointRestartUnsafe,
    ensure_checkpoint_restart_safe,
)
from inqtrix.agents.control_ports import PlanNotFound


@dataclass
class _ProbeControl:
    plan: bool = False
    approvals: list[Any] | None = None
    clarifications: list[Any] | None = None
    artifacts: list[Any] | None = None
    fail_at: str = ""

    async def get_plan(self, run_id: str) -> tuple[Any, list[Any]]:
        self._raise_if("plan")
        if not self.plan:
            raise PlanNotFound(run_id)
        return object(), []

    async def list_approvals(self, run_id: str) -> list[Any]:
        self._raise_if("approvals")
        return list(self.approvals or [])

    async def list_clarifications(self, run_id: str) -> list[Any]:
        self._raise_if("clarifications")
        return list(self.clarifications or [])

    async def list_artifacts(
        self, run_id: str, *, limit: int
    ) -> tuple[list[Any], None]:
        self._raise_if("artifacts")
        assert limit == 1
        return list(self.artifacts or []), None

    def _raise_if(self, source: str) -> None:
        if self.fail_at == source:
            raise OSError(f"{source} unavailable")


@dataclass
class _ProbeRuns:
    rows: list[Any] | None = None
    fail: bool = False

    def children(self, run_id: str) -> list[Any]:
        if self.fail:
            raise OSError("children unavailable")
        return list(self.rows or [])


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _guard(
    control: _ProbeControl, runs: _ProbeRuns | None = None
) -> None:
    ensure_checkpoint_restart_safe(
        "run_guard",
        control=control,  # type: ignore[arg-type]
        run_store=runs or _ProbeRuns(),  # type: ignore[arg-type]
        run_async=_run,
    )


def test_complete_empty_probe_allows_fresh_start() -> None:
    _guard(_ProbeControl())


@pytest.mark.parametrize(
    ("control", "runs"),
    [
        (_ProbeControl(plan=True), _ProbeRuns()),
        (_ProbeControl(approvals=[object()]), _ProbeRuns()),
        (_ProbeControl(clarifications=[object()]), _ProbeRuns()),
        (_ProbeControl(artifacts=[object()]), _ProbeRuns()),
        (_ProbeControl(), _ProbeRuns(rows=[{"run_id": "child"}])),
    ],
    ids=["plan", "approval", "clarification", "artifact-only", "child-only"],
)
def test_any_durable_execution_evidence_blocks_restart(
    control: _ProbeControl, runs: _ProbeRuns
) -> None:
    with pytest.raises(CheckpointRestartUnsafe, match="checkpoint for this agent run was lost"):
        _guard(control, runs)


@pytest.mark.parametrize(
    "control",
    [
        _ProbeControl(fail_at="plan"),
        _ProbeControl(fail_at="approvals"),
        _ProbeControl(fail_at="clarifications"),
        _ProbeControl(fail_at="artifacts"),
    ],
    ids=["plan", "approvals", "clarifications", "artifacts"],
)
def test_any_control_store_uncertainty_fails_closed(
    control: _ProbeControl,
) -> None:
    with pytest.raises(CheckpointRestartUnsafe, match="could not be fully verified"):
        _guard(control)


def test_child_store_uncertainty_fails_closed() -> None:
    with pytest.raises(CheckpointRestartUnsafe, match="could not be fully verified"):
        _guard(_ProbeControl(), _ProbeRuns(fail=True))
