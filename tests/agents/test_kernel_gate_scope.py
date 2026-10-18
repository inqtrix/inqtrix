"""The knowledge gate names the collections it may reach.

A knowledge approval that asks about "the project's internal
collections" without naming a single one — while the run's boundary is
already pinned at submission and enforced server-side — makes the user
decide blind. That is
the defect; these tests pin that the card carries the names, that a
non-knowledge gate stays untouched, and that an unreadable catalog omits
the line instead of inventing one (No Silent Fallbacks).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from inqtrix.agents.kernel.algorithm import _knowledge_scope_names


class _StubRegistry:
    """Capability registry returning a fixed, already scope-filtered list."""

    def __init__(self, names: list[str] | None = None, error: Exception | None = None):
        self._names = names or []
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    async def invoke(self, capability_id: str, payload: dict, _context: object):
        self.calls.append((capability_id, payload))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in self._names]
        )


def _deps(registry: object) -> SimpleNamespace:
    return SimpleNamespace(capability_registry=registry, capability_context=None)


KNOWLEDGE_ACTION = [{"tool": "search_project_knowledge", "args": {"query": "x"}}]


def test_knowledge_gate_names_its_collections_sorted():
    registry = _StubRegistry(["Vertraege", "EU-AI-Act-vec"])

    names = _knowledge_scope_names(_deps(registry), KNOWLEDGE_ACTION)

    # Sorted so the approval payload stays replay-identical.
    assert names == ["EU-AI-Act-vec", "Vertraege"]
    assert registry.calls == [("knowledge.collections.list", {})]


def test_document_read_gate_carries_the_same_scope():
    registry = _StubRegistry(["EU-AI-Act-vec"])

    names = _knowledge_scope_names(
        _deps(registry),
        [{"tool": "read_project_document", "args": {"document_id": "kd_1"}}],
    )

    assert names == ["EU-AI-Act-vec"]


def test_non_knowledge_gate_asks_no_catalog_at_all():
    registry = _StubRegistry(["EU-AI-Act-vec"])

    names = _knowledge_scope_names(
        _deps(registry), [{"tool": "web_instant", "args": {"query": "x"}}]
    )

    assert names is None
    assert registry.calls == []


def test_empty_boundary_is_reported_as_empty_not_as_absent():
    """A run pinned to NOTHING is a real state and must stay visible —
    it is the difference between 'searches nothing' and 'we don't know'."""
    names = _knowledge_scope_names(_deps(_StubRegistry([])), KNOWLEDGE_ACTION)

    assert names == []


def test_unreadable_catalog_omits_the_line_and_warns(caplog):
    registry = _StubRegistry(error=RuntimeError("catalog down"))

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        names = _knowledge_scope_names(_deps(registry), KNOWLEDGE_ACTION)

    assert names is None
    assert any("Knowledge scope for approval" in record.message for record in caplog.records)


def test_missing_registry_omits_the_line():
    assert _knowledge_scope_names(_deps(None), KNOWLEDGE_ACTION) is None
