"""A checkpoint loads only the types its checkpointer allows, and the kernel stops on any other.

Both savers get LangGraph's safe types plus ``CHECKPOINT_TYPES`` through the
serializer's constructor, so the list holds with and without
``LANGGRAPH_STRICT_MSGPACK``. A type outside it loads as raw data, is logged
and counted, and a kernel run whose checkpoint held one stops before any node
runs instead of going on with altered state.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("langchain")

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.base.id import uuid6

from inqtrix.agents.checkpointing import CheckpointerHandle, blocked_checkpoint_types

REPOSITORY = Path(__file__).resolve().parents[2]


@dataclass
class ForeignThing:
    value: str


def _put_and_load(saver) -> tuple[object, list[tuple[str, str]]]:
    config = {"configurable": {"thread_id": "foreign", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    version = saver.get_next_version(None, None)
    checkpoint["channel_values"] = {"thing": ForeignThing("x")}
    checkpoint["channel_versions"] = {"thing": version}
    saver.put(config, checkpoint, {}, {"thing": version})
    with blocked_checkpoint_types() as blocked:
        loaded = saver.get_tuple(config)
    return loaded.checkpoint["channel_values"]["thing"], blocked


def test_a_foreign_class_in_a_checkpoint_loads_as_raw_data_and_is_reported(monkeypatch, caplog):
    from inqtrix.observability import metrics_defs

    counted: list[str] = []
    monkeypatch.setattr(
        metrics_defs, "_active", SimpleNamespace(count_blocked_checkpoint_type=counted.append)
    )
    saver = CheckpointerHandle(database_url=None, max_connections=0).saver()

    with caplog.at_level(logging.WARNING, logger="inqtrix.agents.checkpointing"):
        value, blocked = _put_and_load(saver)

    assert not isinstance(value, ForeignThing)
    assert blocked == [(__name__, "ForeignThing")]
    assert counted == ["type"]
    assert "outside the allowed list" in caplog.text


def test_the_postgres_saver_gets_the_same_list(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Saver:
        def __init__(self, pool: object, *, serde: object = None) -> None:
            captured["serde"] = serde

        def setup(self) -> None:
            pass

    monkeypatch.setattr("langgraph.checkpoint.postgres.PostgresSaver", _Saver)
    monkeypatch.setattr("psycopg_pool.ConnectionPool", lambda *args, **kwargs: object())

    CheckpointerHandle(database_url="postgresql+asyncpg://user@localhost/inqtrix").saver()
    serde = captured["serde"]

    assert not isinstance(serde.loads_typed(serde.dumps_typed(ForeignThing("x"))), ForeignThing)


def test_the_list_holds_with_the_strict_switch_on_too() -> None:
    """The switch is read when LangGraph is imported, so it takes a process of its own."""
    code = textwrap.dedent(
        """
        import json
        from inqtrix.agents.checkpointing import CheckpointerHandle
        from tests.agents.test_checkpoint_types import ForeignThing, _put_and_load

        value, blocked = _put_and_load(CheckpointerHandle(database_url=None, max_connections=0).saver())
        print(json.dumps({"built": isinstance(value, ForeignThing), "blocked": blocked}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPOSITORY,
        env={**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "built": False,
        "blocked": [["tests.agents.test_checkpoint_types", "ForeignThing"]],
    }


def test_a_kernel_run_whose_checkpoint_held_a_foreign_type_stops_before_any_node(caplog) -> None:
    from inqtrix.providers.base import ToolCallRequest
    from tests.agents.test_kernel_prompt_events import ScriptedToolLLM, make_client, wait_status
    from tests.agents.test_kernel_tool_spans import _desk_text, _desk_turn

    llm = ScriptedToolLLM(
        [
            _desk_turn(ToolCallRequest(id="c_web", name="web_instant", arguments={"query": "extern"})),
            _desk_text("Nie erreicht."),
        ]
    )
    client = make_client(llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Suche bitte die aktuelle Quelle.",
                "mode": "agent_kernel",
                "autonomy": "strict",
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"waiting_for_approval"})

        # A checkpoint that is not the one the run wrote: an object in its state.
        saver = client.container.agent_checkpointer.saver()  # type: ignore[attr-defined]
        latest = saver.get_tuple({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}})
        checkpoint = copy.deepcopy(latest.checkpoint)
        version = saver.get_next_version(checkpoint["channel_versions"].get("todos"), None)
        checkpoint["id"] = str(uuid6())
        checkpoint["channel_values"]["todos"] = [ForeignThing("x")]
        checkpoint["channel_versions"]["todos"] = version
        saver.put(latest.config, checkpoint, latest.metadata, {"todos": version})
        calls = len(llm.chat_calls)

        (approval,) = [
            row
            for row in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
            if row["status"] == "pending"
        ]
        with caplog.at_level(logging.WARNING, logger="inqtrix.agents.checkpointing"):
            decided = client.post(
                f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
                json={"decision": "approve"},
            )
            assert decided.status_code == 200, decided.text
            summary = wait_status(client, run_id, {"failed"})

    assert len(llm.chat_calls) == calls
    assert "outside the allowed list" in caplog.text
    assert "stored state" in json.dumps(summary)
