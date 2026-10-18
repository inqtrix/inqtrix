"""Regression tests for the strict structured-output schema hardener.

Guards the exact failure that took down the agent PLAN phase: Azure/OpenAI
strict outputs reject ``ExecutionPlanModel`` because its (legitimately)
defaulted fields are absent from ``required`` and it carries ``default`` keys.
:func:`~inqtrix.providers._schema.strictify_json_schema` centralises the
adaptation so models may carry defaults freely (retiring the fragile
"author models without defaults" convention — gotcha #6).
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from inqtrix.agents.plan_models import ExecutionPlanModel
from inqtrix.providers._schema import strictify_json_schema


def _object_nodes(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every object node (root, ``$defs`` entries, nested) of a JSON schema."""
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("properties"), dict):
            yield node
        for key in ("$defs", "properties"):
            sub = node.get(key)
            if isinstance(sub, dict):
                stack.extend(sub.values())
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            sub = node.get(key)
            if isinstance(sub, list):
                stack.extend(sub)
        if isinstance(node.get("items"), dict):
            stack.append(node["items"])


def test_every_object_becomes_fully_required_with_no_extra_props() -> None:
    strict = strictify_json_schema(ExecutionPlanModel.model_json_schema())
    nodes = list(_object_nodes(strict))
    # Root + task + params are all covered. The deprecated task-budget
    # bridge is intentionally absent from the planner schema.
    assert len(nodes) >= 3
    for node in nodes:
        assert set(node["required"]) == set(node["properties"])
        assert node["additionalProperties"] is False


def test_default_keyword_is_removed_everywhere() -> None:
    # ExecutionPlanModel emits `default` for defaulted planner fields;
    # strict outputs reject the keyword.
    raw = ExecutionPlanModel.model_json_schema()
    assert '"default"' in json.dumps(raw)
    strict = strictify_json_schema(raw)
    assert '"default"' not in json.dumps(strict)


def test_nullable_anyof_and_constraints_are_preserved() -> None:
    strict = strictify_json_schema(ExecutionPlanModel.model_json_schema())
    # PlanTaskParams.recency is enum | None — keep its anyOf+null shape.
    recency = strict["$defs"]["PlanTaskParams"]["properties"]["recency"]
    assert "null" in {branch.get("type") for branch in recency["anyOf"]}
    value_schema = next(
        branch for branch in recency["anyOf"] if branch.get("type") == "string"
    )
    assert value_schema["enum"] == ["day", "week", "month", "year"]
    # Value constraints stay (Pydantic still enforces them on the reply).
    assert "maxLength" in json.dumps(strict)


def test_transform_is_idempotent_and_pure() -> None:
    raw = ExecutionPlanModel.model_json_schema()
    before = json.dumps(raw, sort_keys=True)
    once = strictify_json_schema(raw)
    twice = strictify_json_schema(once)
    assert once == twice
    # The caller's schema is never mutated (deep copy).
    assert json.dumps(raw, sort_keys=True) == before
