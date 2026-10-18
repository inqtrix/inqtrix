"""SKILL.md serialization roundtrip.

Secures: export -> import -> re-validate reproduces every writable
field byte-for-byte, foreign files without the x-inqtrix block import
with defaults, and malformed files fail loudly.
"""

from __future__ import annotations

import time
import uuid

import pytest

from inqtrix.content.skill_markdown import (
    SkillMarkdownError,
    skill_from_markdown,
    skill_to_markdown,
)
from inqtrix.content.skills import SkillRecord, new_skill_id
from inqtrix.services.skill_service import _validated_fields


def _record() -> SkillRecord:
    now = time.time()
    return SkillRecord(
        id=new_skill_id(),
        tenant_id="default",
        owner_user_id=uuid.uuid4(),
        label="sprechzettel",
        title="Sprechzettel",
        description="Kompakter Sprechzettel fuer Termine.",
        when_to_use="Wenn Stichpunkte fuer einen Termin gebraucht werden.",
        instructions_markdown=(
            "Erstelle einen Sprechzettel fuer {{anlass}}.\n\n"
            "- Kernbotschaften zuerst\n- Maximal eine Seite"
        ),
        clarification_points=(
            {
                "id": "p1",
                "name": "anlass",
                "question": "Fuer welchen Anlass?",
                "options": [
                    {"id": "p1_o1", "label": "Vorstand", "description": ""}
                ],
                "required": True,
                "default_assumption": "Interner Termin",
            },
        ),
        deliverable="talking_points",
        allowed_tools=("search_project_knowledge",),
        requires_plan="never",
        invocation="model_allowed",
        argument_hint="Anlass und Kernbotschaft",
        model_tier="mid",
        effort="low",
        include_in_autocomplete=True,
        created_at=now,
        updated_at=now,
    )


def test_roundtrip_preserves_every_writable_field():
    record = _record()
    payload = skill_from_markdown(skill_to_markdown(record))
    # The service validator is the single gate — an imported payload
    # must pass it and reproduce the record's writable fields.
    fields = _validated_fields(payload)
    assert fields["label"] == record.label
    assert fields["title"] == record.title
    assert fields["description"] == record.description
    assert fields["when_to_use"] == record.when_to_use
    assert fields["instructions_markdown"] == record.instructions_markdown
    assert fields["clarification_points"] == record.clarification_points
    assert fields["deliverable"] == record.deliverable
    assert fields["allowed_tools"] == record.allowed_tools
    assert fields["requires_plan"] == record.requires_plan
    assert fields["invocation"] == record.invocation
    assert fields["argument_hint"] == record.argument_hint
    assert fields["model_tier"] == record.model_tier
    assert fields["effort"] == record.effort
    assert fields["include_in_autocomplete"] is True


def test_foreign_skill_imports_with_defaults():
    payload = skill_from_markdown(
        "---\n"
        "name: code-review\n"
        "description: Reviews code changes.\n"
        "---\n\n"
        "Review the given diff carefully.\n"
    )
    assert payload["label"] == "code-review"
    assert payload["title"] == "code-review"
    assert payload["instructions_markdown"] == (
        "Review the given diff carefully."
    )
    fields = _validated_fields(payload)
    assert fields["requires_plan"] == "auto"
    assert fields["invocation"] == "user_only"
    assert fields["clarification_points"] == ()


def test_malformed_files_fail_loudly():
    with pytest.raises(SkillMarkdownError):
        skill_from_markdown("kein frontmatter")
    with pytest.raises(SkillMarkdownError):
        skill_from_markdown("---\nname: x\nkein abschluss")
    with pytest.raises(SkillMarkdownError):
        skill_from_markdown("---\n- liste\n- statt\n---\nobjekt")
    with pytest.raises(SkillMarkdownError):
        skill_from_markdown("---\ndescription: ohne name\n---\nbody")
