"""Each checkpointed workflow keeps the shape its contract version names.

A change a stored run could not resume into is a break: it raises the
workflow's ``contract_version``, after which runs bound to the old one end
visibly instead of resuming into a graph that no longer fits them. An added
tool is no break and only re-pins. Re-pin with
``uv run python -m tests.architecture.workflow_pins``.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

pytest.importorskip("langchain")

from tests.architecture.workflow_pins import (
    ADDITION,
    BREAK,
    SAME,
    compare,
    current_shapes,
    load_pins,
)

RESUME_TEST = (
    Path(__file__).resolve().parents[1] / "harness" / "test_resume_with_added_tool.py"
)


@pytest.fixture(scope="module")
def shapes() -> dict:
    return current_shapes()


@pytest.fixture()
def kernel(shapes) -> dict:
    return copy.deepcopy(shapes["agent_kernel"])


def test_every_checkpointed_workflow_keeps_the_shape_of_its_contract(shapes) -> None:
    pins = load_pins()

    assert set(pins) == set(shapes)
    for mode, shape in shapes.items():
        pinned = pins[mode]
        assert pinned["contract_version"] == shape["contract_version"], (
            f"{mode} carries contract {shape['contract_version']}, the pin names "
            f"{pinned['contract_version']}: re-pin."
        )
        verdict, changes = compare(pinned, shape)
        assert verdict != BREAK, (
            f"{mode}: {'; '.join(changes)}. A stored run cannot resume into this: "
            "raise its contract_version, then re-pin."
        )
        assert verdict == SAME, (
            f"{mode}: {'; '.join(changes)}. No contract increment needed: re-pin."
        )


def test_a_renamed_middleware_is_a_break(kernel) -> None:
    changed = copy.deepcopy(kernel)
    graph = changed["graphs"]["balanced"]
    old, new = "KernelSufficiencyMiddleware.before_model", "KernelSufficiency.before_model"
    graph["nodes"] = sorted(new if node == old else node for node in graph["nodes"])
    graph["edges"] = sorted(
        [new if end == old else end for end in edge[:2]] + [edge[2]]
        for edge in graph["edges"]
    )

    assert compare(kernel, changed)[0] == BREAK


def test_a_changed_edge_between_the_same_nodes_is_a_break(kernel) -> None:
    changed = copy.deepcopy(kernel)
    edges = changed["graphs"]["strict"]["edges"]
    edges[0] = [edges[0][0], edges[0][1], not edges[0][2]]

    assert changed["graphs"]["strict"]["nodes"] == kernel["graphs"]["strict"]["nodes"]
    assert compare(kernel, changed)[0] == BREAK


def test_another_channel_kind_or_snapshot_frequency_is_a_break(kernel) -> None:
    other_kind = copy.deepcopy(kernel)
    other_kind["graphs"]["autonomous"]["channels"]["messages"]["class"] = (
        "BinaryOperatorAggregate"
    )
    other_frequency = copy.deepcopy(kernel)
    other_frequency["graphs"]["autonomous"]["channels"]["messages"][
        "snapshot_frequency"
    ] = 25

    assert compare(kernel, other_kind)[0] == BREAK
    assert compare(kernel, other_frequency)[0] == BREAK


def test_a_removed_or_changed_tool_or_interrupt_kind_is_a_break(kernel) -> None:
    removed = copy.deepcopy(kernel)
    del removed["tools"]["read_evidence"]
    reshaped = copy.deepcopy(kernel)
    reshaped["tools"]["read_evidence"] = "0" * 16
    interrupted = copy.deepcopy(kernel)
    interrupted["interrupts"]["review"] = ["kind"]

    assert compare(kernel, removed) == (BREAK, ["tool read_evidence removed"])
    assert compare(kernel, reshaped) == (BREAK, ["tool read_evidence changed its arguments"])
    assert compare(kernel, interrupted)[0] == BREAK


def test_another_transcript_marker_format_is_a_break(kernel) -> None:
    """A run parked under one format loses its skill lock and read receipts under another."""
    changed = copy.deepcopy(kernel)
    changed["transcript_markers"] = ["[skill_ready:{skill_id}@{revision}]", kernel["transcript_markers"][1]]

    assert compare(kernel, changed) == (BREAK, ["transcript markers"])


def test_another_type_a_checkpoint_may_hold_is_a_break(kernel) -> None:
    """A stored run written under one type list may hold what the other refuses."""
    changed = copy.deepcopy(kernel)
    changed["checkpoint_types"] = [*kernel["checkpoint_types"], ["some.module", "Thing"]]

    assert compare(kernel, changed) == (BREAK, ["checkpoint types"])


def test_an_added_tool_is_an_addition_because_a_stored_run_resumes_with_it(kernel) -> None:
    """The rule rests on the resume test; without it an added tool would be a break."""
    changed = copy.deepcopy(kernel)
    changed["tools"]["new_tool"] = "f" * 16

    assert compare(kernel, changed) == (ADDITION, ["tool new_tool added"])
    assert "def test_a_run_parked_on_an_open_call_resumes_with_an_added_tool(" in (
        RESUME_TEST.read_text(encoding="utf-8")
    )


def test_a_new_variant_is_an_addition_and_a_removed_one_a_break(kernel) -> None:
    """No stored run was written by a variant that only appears."""
    grown = copy.deepcopy(kernel)
    grown["graphs"]["profile:new_role"] = copy.deepcopy(kernel["graphs"]["profile:review"])
    shrunk = copy.deepcopy(kernel)
    del shrunk["graphs"]["profile:review"]

    assert compare(kernel, grown) == (ADDITION, ["graph profile:new_role added"])
    assert compare(kernel, shrunk) == (BREAK, ["graph profile:review removed"])


def test_the_tool_hash_sees_every_argument_and_nested_type_but_no_prose() -> None:
    from typing import Literal

    from langchain_core.tools import tool
    from pydantic import Field, create_model

    from tests.architecture.workflow_pins import _schema_hash

    def titled(kind: object) -> object:
        @tool(args_schema=create_model("WriteArgs", title=(kind, ...)))
        def write(title: object) -> str:
            """Write a document."""
            return ""

        return write

    def listed(states: tuple[str, ...]) -> object:
        item = create_model("Item", status=(Literal[states], ...))

        @tool(args_schema=create_model("PlanArgs", items=(list[item], ...)))
        def plan(items: object) -> str:
            """Keep a plan."""
            return ""

        return plan

    def described(text: str) -> object:
        @tool(args_schema=create_model("SearchArgs", query=(str, Field(description=text))))
        def search(query: object) -> str:
            """Search."""
            return ""

        return search

    assert _schema_hash(titled(str)) != _schema_hash(titled(list[str]))
    assert _schema_hash(listed(("open", "done"))) != _schema_hash(listed(("open", "closed")))
    assert _schema_hash(described("What to find.")) == _schema_hash(described("The words to look for."))
