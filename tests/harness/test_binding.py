"""What a claim does with a run, from its binding and the contracts this worker carries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from inqtrix.harness.contracts import binding as binding_module
from inqtrix.harness.contracts.binding import (
    RETIRED_WORKFLOWS,
    BindingVerdict,
    RunBinding,
    check_run_binding,
)

_WORKER = {"agent_kernel": 2, "research": 1}


def _check(
    mode: str, contract: int, *, has_checkpoint_state: bool = True
) -> BindingVerdict:
    return check_run_binding(
        RunBinding(mode=mode, workflow_contract=contract),
        _WORKER,
        has_checkpoint_state=has_checkpoint_state,
    )


def test_a_run_bound_to_the_contract_the_worker_carries_continues() -> None:
    assert _check("agent_kernel", 2) is BindingVerdict.MATCH


def test_a_run_with_checkpoint_state_bound_to_an_older_contract_ends() -> None:
    assert _check("agent_kernel", 1) is BindingVerdict.RETIRE_OLDER


def test_a_run_without_checkpoint_state_bound_to_an_older_contract_is_stamped_anew() -> None:
    assert _check("agent_kernel", 1, has_checkpoint_state=False) is BindingVerdict.RESTAMP


def test_a_run_bound_to_a_newer_contract_waits_for_a_newer_worker() -> None:
    assert _check("research", 2) is BindingVerdict.DEFER_NEWER
    assert _check("research", 2, has_checkpoint_state=False) is BindingVerdict.DEFER_NEWER


def test_a_mode_the_worker_has_not_registered_is_unavailable_and_never_retired() -> None:
    assert _check("knowledge", 1) is BindingVerdict.UNAVAILABLE
    assert _check("knowledge", 1, has_checkpoint_state=False) is BindingVerdict.UNAVAILABLE


def test_a_retired_mode_ends_whether_or_not_a_worker_registers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        binding_module, "RETIRED_WORKFLOWS", frozenset({"research", "workspace_agent"})
    )

    assert _check("research", 1) is BindingVerdict.RETIRED_MODE
    assert _check("workspace_agent", 1) is BindingVerdict.RETIRED_MODE


def test_a_binding_names_a_contract_of_at_least_one() -> None:
    with pytest.raises(ValidationError):
        RunBinding(mode="research", workflow_contract=0)


def test_no_mode_is_retired_yet() -> None:
    """A mode listed here ends its runs visibly; one only missing on a worker never does."""
    assert RETIRED_WORKFLOWS == frozenset()
