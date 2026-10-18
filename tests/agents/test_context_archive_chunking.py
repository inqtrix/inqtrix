"""Lossless multi-section context archive and imperative planner pin.

The archive must preserve compaction evictions beyond one section, and
the pinned web profile must prevent repeated ``plan_invalid`` results.
"""

from __future__ import annotations

import pytest
import asyncio

pytest.importorskip("deepagents")

from inqtrix.agents.kernel.deps import KernelDeps
from inqtrix.agents.context_archive import CONTEXT_ARCHIVE_SECTION_CHARS
from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.prompts import _planner_web_rule
from inqtrix.settings import AgentPlatformSettings


def _deps() -> KernelDeps:
    return KernelDeps(
        run_id="run_chunk_test",
        control=MemoryAgentControlStore(),
        platform=AgentPlatformSettings(),
        llm=None,
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )


def test_chunked_archive_preserves_exact_text_across_part_boundaries():
    deps = _deps()
    # Formatting belongs to the read projection, never the stored source.
    fence = chr(96) * 3
    body = f" \n{fence}python\n" + ("wert = 1\n\n\n" * 6_000) + "\t \n"
    ids = deps.append_context_archive_chunked("Komprimierter Verlauf", body)
    written = [asyncio.run(deps.control.get_artifact(deps.run_id, item))[0] for item in ids]
    assert len(ids) >= 2
    assert "".join(row.content_markdown for row in written) == body
    assert all(len(row.content_markdown) <= CONTEXT_ARCHIVE_SECTION_CHARS for row in written)


def test_chunked_archive_keeps_short_sections_singular():
    deps = _deps()
    ids = deps.append_context_archive_chunked("Titel", "kurzer Text")
    assert len(ids) == 1
    row, _ = asyncio.run(deps.control.get_artifact(deps.run_id, ids[0]))
    assert row.content_markdown == "kurzer Text"
    spaces = deps.append_context_archive_chunked("Titel", "   ")
    row, _ = asyncio.run(deps.control.get_artifact(deps.run_id, spaces[0]))
    assert row.content_markdown == "   "
    assert deps.append_context_archive_chunked("Titel", "") == []


def test_planner_pinned_profile_rule_is_imperative():
    pinned = _planner_web_rule(True, "deep")
    assert "MUST set params.profile=deep" in pinned
    assert "web_instant tasks NEVER carry a profile" in pinned
    # The ceiling variant keeps its explicit scope sentence.
    ceiling = _planner_web_rule(
        True, "schnell", max_profile="compact", max_instant_tasks=2
    )
    assert "ONLY web_research tasks may set params.profile" in ceiling
