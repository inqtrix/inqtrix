"""A batch the agent may not start is a failed tool step.

``delegate_batch`` refuses a batch of the wrong size, an assignment without
an objective or with an unknown mode, and an assignment the run is not
allowed to start. Every refusal starts no child and is a failure under the
one contract of the tool texts: the transcript settles the call as failed,
and a run that parks afterwards does not count it as a use of the web.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain")

from langchain_core.messages import ToolMessage

from inqtrix.agents.kernel.algorithm import _checkpointed_tool_use_counts
from inqtrix.agents.kernel.tools import SOURCE_TOOL_FAILURE_PREFIXES
from tests.agents.test_kernel_child_recency import _run

_RESEARCH = {"objective": "Aktuelle Urteile zur Förderung.", "mode": "research"}


@pytest.mark.parametrize(
    ("assignments", "says"),
    [
        ([], "1 to 3 assignments"),
        ([_RESEARCH, _RESEARCH, _RESEARCH, _RESEARCH], "1 to 3 assignments"),
        ([_RESEARCH, {"objective": "  ", "mode": "research"}], "assignment 2"),
        ([{"objective": "Marktüberblick.", "mode": "survey"}], "assignment 1"),
        # The tier of the run delegates research and no mission.
        ([_RESEARCH, {"objective": "Schreibe ein Memo.", "mode": "deep_mission"}], "assignment 2"),
    ],
    ids=["empty", "too many", "no objective", "unknown mode", "not allowed"],
)
def test_a_refused_batch_is_a_failed_tool_step(monkeypatch, assignments: list[dict[str, Any]], says: str) -> None:
    run = _run(monkeypatch, "delegate_batch", {"assignments": assignments}, tier="gruendlich")
    after_a_park = SimpleNamespace(values={"messages": [
        ToolMessage(content=run["reply"], name="delegate_batch", tool_call_id="call_child_recency"),
    ]})
    marks = sum(run["reply"].count(prefix) for prefix in SOURCE_TOOL_FAILURE_PREFIXES)
    assert (run["children"], run["received"]) == ([], {})
    # One mark of the contract, in front: a denial handed up from the
    # admission of an assignment does not bring a second one along.
    assert run["reply"].startswith(SOURCE_TOOL_FAILURE_PREFIXES) and marks == 1 and says in run["reply"], run["reply"]
    assert (run["settled"], _checkpointed_tool_use_counts(after_a_park)) == (
        ["error"], {"web": 0, "knowledge": 0},
    )
