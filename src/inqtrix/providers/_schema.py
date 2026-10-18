"""Normalize a JSON Schema to the OpenAI/Azure strict structured-output contract.

Structured Output is a provider capability with a provider-SPECIFIC schema
contract. The OpenAI/Azure strict variant requires, for every object node, that
``required`` lists EVERY property key and that ``additionalProperties`` is
``false``, and it rejects the ``default`` keyword. Pydantic's
``model_json_schema()`` only marks default-free fields ``required`` and emits
``default`` for the rest, so a model with any defaulted field (e.g. the shared
:class:`~inqtrix.agents.plan_models.ExecutionPlanModel`, which legitimately
carries defaults for its approval-edit/validator role) produces a schema the
strict endpoint rejects.

This module is the ONE place that adapts a canonical, provider-agnostic schema to
that contract. Providers speaking the strict OpenAI json_schema protocol call it
from inside their own ``complete_structured`` — the adaptation lives IN the
provider (like its token-budget-parameter and reasoning-effort adaptation), never
in the agent, the models, or the generic structured-call helper. Callers may
therefore author their Pydantic models naturally. The transform is keyed on the
strict CONTRACT, not on any model id, so it applies uniformly to every strict
OpenAI model and is unaffected by provider swaps.
"""

from __future__ import annotations

import copy
from typing import Any

_COMPOSITE_KEYS = ("anyOf", "allOf", "oneOf")


def strictify_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of *schema* satisfying the strict-output contract.

    For every object node (the root, each ``$defs`` entry, nested
    ``properties`` values, ``items``/``prefixItems`` and the branches of
    ``anyOf``/``allOf``/``oneOf``): set ``required`` to ALL property keys and
    ``additionalProperties`` to ``False``. Remove every ``default`` keyword
    (strict outputs reject it). The transform is idempotent and does not mutate
    the caller's schema. Value constraints (``minLength``/``minimum`` …) are
    preserved — they are part of the modern strict contract and stay enforced by
    the model's own validation of the reply, so no guidance is lost.
    """
    return _strictify_node(copy.deepcopy(schema))


def _strictify_node(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    # `default` is rejected by strict outputs on any node; the model emits every
    # (now-required) field explicitly, so no default is needed at the wire level.
    node.pop("default", None)
    defs = node.get("$defs")
    if isinstance(defs, dict):
        for key, value in defs.items():
            defs[key] = _strictify_node(value)
    properties = node.get("properties")
    if isinstance(properties, dict):
        for key, value in properties.items():
            properties[key] = _strictify_node(value)
        # The whole strict contract: every property required, no extra keys.
        node["required"] = list(properties.keys())
        node["additionalProperties"] = False
    for comp in _COMPOSITE_KEYS:
        if comp in node:
            node[comp] = _strictify_node(node[comp])
    if "items" in node:
        node["items"] = _strictify_node(node["items"])
    if "prefixItems" in node:
        node["prefixItems"] = _strictify_node(node["prefixItems"])
    return node
