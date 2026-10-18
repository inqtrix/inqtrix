"""Candidate-only memory reflection for completed workspace-agent runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from inqtrix.agents.patterns._structured import StructuredOutcome, structured_call
from inqtrix.agents.phase_models import MemoryReflection
from inqtrix.agents.prompts import (
    agent_memory_system_prompt,
    build_agent_memory_reflection_prompt,
)

if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider


def run_memory_reflection(
    llm: "LLMProvider",
    *,
    question: str,
    memo_markdown: str,
    critic_digest: str,
    task_digest: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    language: str | None = None,
) -> StructuredOutcome:
    """Extract safe, user-reviewable long-term memory candidates."""
    return structured_call(
        llm,
        prompt=build_agent_memory_reflection_prompt(
            question=question,
            memo_markdown=memo_markdown,
            critic_digest=critic_digest,
            task_digest=task_digest,
            language=language,
        ),
        model_cls=MemoryReflection,
        node="agent_memory_reflection",
        system=agent_memory_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
