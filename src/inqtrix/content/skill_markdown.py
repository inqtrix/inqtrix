"""SKILL.md serialization with agentskills.io compatibility.

One skill file = YAML frontmatter + the instruction body. The standard
fields (``name``, ``description``) stay at the top level so foreign
tools read the file as a plain agent skill; every Inqtrix-enforced
policy field rides under the ``x-inqtrix`` extension key and round-trips
losslessly. Import returns the WRITABLE payload shape — the service's
validator stays the single gate (an imported file gets no side door
around the placeholder/point rules).
"""

from __future__ import annotations

from typing import Any, Mapping

import yaml

from inqtrix.content.skills import SkillRecord

_FRONTMATTER_DELIMITER = "---"

_X_INQTRIX_FIELDS = (
    "title",
    "when_to_use",
    "clarification_points",
    "deliverable",
    "allowed_tools",
    "requires_plan",
    "invocation",
    "argument_hint",
    "model_tier",
    "effort",
    "include_in_autocomplete",
)
"""Policy fields serialized under ``x-inqtrix`` — exactly the writable
record fields minus the two standard ones (label -> ``name``,
description) and the body (``instructions_markdown``)."""


class SkillMarkdownError(ValueError):
    """Raised when a SKILL.md file cannot be parsed (maps to HTTP 400)."""


class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML aliases.

    ``safe_load`` still EXPANDS anchors/aliases, so a small imported
    frontmatter can blow up exponentially (billion-laughs) before any
    validation runs. Skill frontmatter has no legitimate use for
    aliases — refusing them keeps parse cost linear in input size.
    """

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError(
                "YAML aliases and anchors are not allowed in frontmatter."
            )
        return super().compose_node(parent, index)


def skill_to_markdown(record: SkillRecord) -> str:
    """Serialize one skill as a SKILL.md document."""
    frontmatter: dict[str, Any] = {
        "name": record.label,
        "description": record.description,
        "x-inqtrix": {
            "title": record.title,
            "when_to_use": record.when_to_use,
            "clarification_points": [
                dict(point) for point in record.clarification_points
            ],
            "deliverable": record.deliverable,
            "allowed_tools": list(record.allowed_tools),
            "requires_plan": record.requires_plan,
            "invocation": record.invocation,
            "argument_hint": record.argument_hint,
            "model_tier": record.model_tier,
            "effort": record.effort,
            "include_in_autocomplete": record.include_in_autocomplete,
        },
    }
    header = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    return (
        f"{_FRONTMATTER_DELIMITER}\n{header}\n{_FRONTMATTER_DELIMITER}\n\n"
        f"{record.instructions_markdown.strip()}\n"
    )


def skill_from_markdown(text: str) -> dict[str, Any]:
    """Parse one SKILL.md document into the writable payload shape.

    Foreign skills (no ``x-inqtrix`` block) import with Inqtrix
    defaults; unknown extension keys are ignored VISIBLY by simply not
    round-tripping (the export writes only the known set).

    Raises:
        SkillMarkdownError: Missing/invalid frontmatter or a non-string
            ``name`` — the caller maps this to HTTP 400. Everything
            beyond the file shape is the service validator's job.
    """
    stripped = text.lstrip("﻿").strip()
    if not stripped.startswith(_FRONTMATTER_DELIMITER):
        raise SkillMarkdownError(
            "SKILL.md requires a YAML frontmatter block (---)."
        )
    remainder = stripped[len(_FRONTMATTER_DELIMITER):]
    if f"\n{_FRONTMATTER_DELIMITER}" not in remainder:
        raise SkillMarkdownError(
            "The frontmatter block is not closed (---)."
        )
    header, body = remainder.split(f"\n{_FRONTMATTER_DELIMITER}", 1)
    try:
        frontmatter = yaml.load(header, Loader=_NoAliasSafeLoader) or {}
    except yaml.YAMLError as exc:
        raise SkillMarkdownError(
            f'Frontmatter is not valid YAML: {exc}'
        ) from exc
    if not isinstance(frontmatter, Mapping):
        raise SkillMarkdownError("Frontmatter must be a YAML object.")
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillMarkdownError("Frontmatter requires a 'name' field.")
    extension = frontmatter.get("x-inqtrix") or {}
    if not isinstance(extension, Mapping):
        raise SkillMarkdownError("'x-inqtrix' must be a YAML object.")
    payload: dict[str, Any] = {
        "label": name.strip(),
        "description": str(frontmatter.get("description", "") or ""),
        "instructions_markdown": body.strip(),
    }
    for field in _X_INQTRIX_FIELDS:
        if field in extension:
            payload[field] = extension[field]
    # A foreign skill carries no display title — the label doubles as
    # one so the import never fails on a missing cosmetic field.
    payload.setdefault("title", name.strip())
    return payload
