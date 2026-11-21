"""How a finished child run projects onto the outcome its parent reads.

A child row the store no longer finds is a named failure, and any other
storage error stays loud.
"""

from __future__ import annotations

import pytest

from inqtrix.agents.scheduler import project_child_run_outcome
from inqtrix.exceptions import RunNotFound


def test_child_projection_only_treats_canonical_not_found_as_missing() -> None:
    class MissingStore:
        def get(self, _run_id: str) -> dict[str, object]:
            raise RunNotFound("child")

    missing = project_child_run_outcome(MissingStore(), "child")
    assert missing is not None
    assert missing.failure_code == "child_row_missing"

    class BrokenStore:
        def get(self, _run_id: str) -> dict[str, object]:
            raise ConnectionError("database unavailable")

    with pytest.raises(ConnectionError, match="database unavailable"):
        project_child_run_outcome(BrokenStore(), "child")

