"""Regression tests for the strict structured-output schema hardener.

Azure/OpenAI strict outputs reject a schema whose (legitimately) defaulted
fields are absent from ``required`` and which carries ``default`` keys.
:func:`~inqtrix.providers._schema.strictify_json_schema` centralises the
adaptation so models may carry defaults freely. The fixture model below has
the shape that failed: defaulted fields at every level, nested object
models, a nullable enum and value constraints.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from inqtrix.core.results import WebRecency
from inqtrix.providers._schema import strictify_json_schema


class TaskParams(BaseModel):
    """Tuning knobs of one task, every one of them optional."""

    model_config = ConfigDict(extra="forbid")

    profile: str | None = Field(default=None, description="Retrieval profile, or null.")
    recency: WebRecency | None = Field(default=None, description="Recency filter, or null.")
    collection_ids: list[str] | None = None


class Task(BaseModel):
    """One task: constrained text, defaulted lists and a nested model."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    objective: str = ""
    queries: list[str] = Field(default_factory=list, description="Task questions.")
    params: TaskParams = Field(default_factory=TaskParams)


class TaskList(BaseModel):
    """The root: one required list beside defaulted fields."""

    model_config = ConfigDict(extra="forbid")

    summary_markdown: str = ""
    tasks: list[Task] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)


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
    strict = strictify_json_schema(TaskList.model_json_schema())
    nodes = list(_object_nodes(strict))
    # Root + task + params are all covered.
    assert len(nodes) >= 3
    for node in nodes:
        assert set(node["required"]) == set(node["properties"])
        assert node["additionalProperties"] is False


def test_default_keyword_is_removed_everywhere() -> None:
    # The fixture emits `default` for its defaulted fields; strict outputs
    # reject the keyword.
    raw = TaskList.model_json_schema()
    assert '"default"' in json.dumps(raw)
    strict = strictify_json_schema(raw)
    assert '"default"' not in json.dumps(strict)


def test_nullable_anyof_and_constraints_are_preserved() -> None:
    strict = strictify_json_schema(TaskList.model_json_schema())
    # TaskParams.recency is enum | None — keep its anyOf+null shape.
    recency = strict["$defs"]["TaskParams"]["properties"]["recency"]
    assert "null" in {branch.get("type") for branch in recency["anyOf"]}
    value_schema = next(
        branch for branch in recency["anyOf"] if branch.get("type") == "string"
    )
    assert value_schema["enum"] == ["day", "week", "month", "year"]
    # Value constraints stay (Pydantic still enforces them on the reply).
    assert "maxLength" in json.dumps(strict)


def test_transform_is_idempotent_and_pure() -> None:
    raw = TaskList.model_json_schema()
    before = json.dumps(raw, sort_keys=True)
    once = strictify_json_schema(raw)
    twice = strictify_json_schema(once)
    assert once == twice
    # The caller's schema is never mutated (deep copy).
    assert json.dumps(raw, sort_keys=True) == before
