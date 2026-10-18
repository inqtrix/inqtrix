"""An empty knowledge boundary says so.

A run whose pinned collection set is empty — a user without any
collection, or without any visible one — must not get the same
"Keine Treffer in der Wissensdatenbank fuer: <query>" a normal
no-match search returns: the model could not tell the two apart and
would keep rephrasing its query against a store that holds nothing,
and the user would see no reason either. The cause travels from the
only layer that knows it (the capability that resolves the boundary)
instead of being guessed downstream.
"""

from __future__ import annotations

import asyncio

import pytest

from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
from inqtrix.agents.kernel.tools import build_kernel_tools
from inqtrix.capabilities.catalog.knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    KnowledgeSearchWarning,
    build_knowledge_capabilities,
)
from inqtrix.capabilities.contracts import CapabilityContext
from inqtrix.auth.principal import Principal


def _search_definition(service: object):
    for definition in build_knowledge_capabilities(service):  # type: ignore[arg-type]
        if definition.id == "knowledge.search":
            return definition
    raise AssertionError("knowledge.search capability missing")


class _UnusedService:
    """Any call here is the defect: an empty scope must not reach retrieval."""

    async def search_reported(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("retrieval must not run on an empty scope")

    async def assert_collections_visible(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("visibility check must not run on an empty scope")


def test_empty_boundary_reports_its_reason_instead_of_bare_no_hits():
    definition = _search_definition(_UnusedService())
    context = CapabilityContext(
        principal=Principal(user_id=None, kind="anonymous"),
        knowledge_collection_ids=frozenset(),
    )

    output = asyncio.run(
        definition.handler(KnowledgeSearchInput(query="Frist?"), context)
    )

    assert output.hits == []
    codes = [warning.code for warning in output.warnings]
    assert codes == ["knowledge.no_collections"]
    assert "nothing to search" in output.warnings[0].message


class _StubRegistry:
    """Registry returning one prepared knowledge.search output."""

    def __init__(self, output: KnowledgeSearchOutput):
        self._output = output

    async def invoke(self, _capability_id: str, _payload: dict, _context: object):
        return self._output


def _run_tool(output: KnowledgeSearchOutput) -> str:
    tools = {getattr(t, "name", ""): t for t in build_kernel_tools()}
    search = tools["search_project_knowledge"]
    deps = KernelDeps(
        run_id="run_p10_k4",
        control=None,  # type: ignore[arg-type]
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
        capability_registry=_StubRegistry(output),
    )
    try:
        set_kernel_deps(deps)
        return search.invoke({"query": "Frist?"})
    finally:
        set_kernel_deps(None)


EMPTY_STORE = KnowledgeSearchOutput(
    query="Frist?",
    hits=[],
    warnings=[
        KnowledgeSearchWarning(
            code="knowledge.no_collections",
            message="Keine Wissenssammlung im Zugriff dieses Laufs.",
            stage="scope",
        )
    ],
)
NO_MATCH = KnowledgeSearchOutput(query="Frist?", hits=[], warnings=[])


@pytest.mark.parametrize(
    ("output", "expected", "forbidden"),
    [
        (EMPTY_STORE, "NO knowledge collection in reach", "No hits"),
        (NO_MATCH, "No hits in the knowledge base", "NO knowledge collection"),
    ],
    ids=["empty-store", "no-match"],
)
def test_tool_text_distinguishes_empty_store_from_empty_result(
    output, expected, forbidden
):
    """The two outcomes must not share a sentence — this is the text the
    model reads and the user sees echoed in the answer."""
    rendered = _run_tool(output)

    assert expected in rendered
    assert forbidden not in rendered


def test_empty_store_text_tells_the_model_that_retrying_is_pointless():
    """Without this the model burns turns rephrasing the same query."""
    rendered = _run_tool(EMPTY_STORE)

    assert "change nothing" in rendered or "another source" in rendered
