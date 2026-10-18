"""The kernel run knows WHICH collections it may search.

The mission engine has listed its collections in the planner prompt
since day one; the kernel had nothing. It searched blind — rephrasing
queries instead of narrowing to a named collection — while its own tool
docstring promised "projektweit in allem Sichtbaren", which the run's
pinned boundary contradicts. These tests pin the three states the block
must distinguish: named collections, an explicitly EMPTY boundary, and
an unreadable catalog (block omitted, never a false emptiness).
"""

from __future__ import annotations

from inqtrix.agents.prompts import build_kernel_user_message

CATALOG = [
    {"name": "EU-AI-Act-vec", "collection_id": "kc_a7be", "document_count": 11},
    {"name": "Vertraege", "collection_id": "kc_1234", "document_count": 3},
]


def test_message_names_every_admitted_collection_with_its_id():
    message = build_kernel_user_message("Frage?", collection_catalog=CATALOG)

    assert "Knowledge collections released for this run" in message
    assert "- EU-AI-Act-vec -> kc_a7be (11 documents)" in message
    assert "- Vertraege -> kc_1234 (3 documents)" in message


def test_message_states_the_scope_rule_the_tool_actually_follows():
    """The old docstring promised a project-wide sweep; the run is
    pinned. The rule must match reality or the model narrows wrongly."""
    message = build_kernel_user_message("Frage?", collection_catalog=CATALOG)

    assert "without collection_ids" in message
    assert "EXACTLY this release" in message


def test_empty_boundary_is_stated_not_hidden():
    message = build_kernel_user_message("Frage?", collection_catalog=[])

    assert "no collection released for this run" in message
    # ... and it tells the model that searching anyway is pointless.
    assert "NO project knowledge" in message
    assert "do not search for it" in message
    assert "Mention the absence only when the assignment demands project knowledge" in message
    assert "say so openly" not in message


def test_unreadable_catalog_omits_the_block_entirely():
    """None must never render as "no collections" — that would make an
    infrastructure failure look like an empty knowledge base."""
    message = build_kernel_user_message("Frage?", collection_catalog=None)

    assert "Knowledge collections released" not in message
    assert "no collection released" not in message


def test_tool_docstring_no_longer_promises_a_project_wide_sweep():
    """The docstring IS the model's contract for the argument; a false
    promise there is a silent scope lie."""
    from inqtrix.agents.kernel import tools

    with open(tools.__file__, encoding="utf-8") as handle:
        source = handle.read()

    assert "project-wide" not in source
    assert 'the list "Knowledge collections released for this run"' in source
