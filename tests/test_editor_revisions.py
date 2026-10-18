"""Prompt and skill edits use mandatory monotonic revisions."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace

import pytest

from inqtrix.content.prompt_templates import (
    MemoryPromptTemplateRepository,
    PromptTemplateConflict,
    PromptTemplateRecord,
)
from inqtrix.content.skills import (
    MemorySkillRepository,
    SkillConflict,
    SkillRecord,
)


@pytest.mark.asyncio
async def test_two_prompt_writers_on_one_revision_have_one_winner() -> None:
    repository = MemoryPromptTemplateRepository()
    now = time.time()
    owner_user_id = uuid.uuid4()
    original = await repository.create(
        PromptTemplateRecord(
            id="pt_revision",
            tenant_id="default",
            owner_user_id=owner_user_id,
            title="Original",
            label="original",
            category="instruction",
            content_markdown="Original body",
            created_at=now,
            updated_at=now,
        )
    )

    results = await asyncio.gather(
        repository.update(
            replace(original, title="Writer A"),
            expected_revision=original.revision,
            actor_user_id=owner_user_id,
        ),
        repository.update(
            replace(original, title="Writer B"),
            expected_revision=original.revision,
            actor_user_id=owner_user_id,
        ),
        return_exceptions=True,
    )

    winners = [
        item for item in results if isinstance(item, PromptTemplateRecord)
    ]
    conflicts = [
        item for item in results if isinstance(item, PromptTemplateConflict)
    ]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert winners[0].revision == 2
    assert conflicts[0].current_revision == 2


@pytest.mark.asyncio
async def test_two_skill_writers_on_one_revision_have_one_winner() -> None:
    repository = MemorySkillRepository()
    now = time.time()
    owner_user_id = uuid.uuid4()
    original = await repository.create(
        SkillRecord(
            id="sk_revision",
            tenant_id="default",
            owner_user_id=owner_user_id,
            label="revision",
            title="Original",
            instructions_markdown="Original body",
            created_at=now,
            updated_at=now,
        )
    )

    results = await asyncio.gather(
        repository.update(
            replace(original, title="Writer A"),
            expected_revision=original.revision,
            actor_user_id=owner_user_id,
        ),
        repository.update(
            replace(original, title="Writer B"),
            expected_revision=original.revision,
            actor_user_id=owner_user_id,
        ),
        return_exceptions=True,
    )

    winners = [item for item in results if isinstance(item, SkillRecord)]
    conflicts = [item for item in results if isinstance(item, SkillConflict)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert winners[0].revision == 2
    assert conflicts[0].current_revision == 2
