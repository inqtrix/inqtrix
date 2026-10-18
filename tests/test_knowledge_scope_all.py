"""The session knowledge switch: ``knowledge_filters.scope = "all"``.

The switch is the user's explicit choice exactly like a chip: it resolves
to every collection the signed-in caller may see, is pinned at submission
and reported on the execution snapshot — never a silent "search
everything" and never accepted without a visibility boundary.
"""

from __future__ import annotations

import pytest

from inqtrix.agents.source_policy import SourcePolicy, execution_payload, knowledge_scope_of
from inqtrix.execution_authority import requested_knowledge_scope
from tests.test_routes import _make_app


def test_requested_scope_reads_chips_before_the_switch() -> None:
    assert requested_knowledge_scope({}) == (None, [])
    assert requested_knowledge_scope({"scope": "all"}) == ("all", [])
    assert requested_knowledge_scope({"collection_ids": ["kc_1"]}) == ("pinned", ["kc_1"])
    # A chip narrows the switch: ids win.
    assert requested_knowledge_scope({"scope": "all", "collection_ids": ["kc_1"]}) == ("pinned", ["kc_1"])
    assert requested_knowledge_scope({"collection_ids": []}) == (None, [])
    with pytest.raises(ValueError):
        requested_knowledge_scope({"scope": "everything"})


def test_snapshot_reports_the_knowledge_boundary_choice() -> None:
    assert knowledge_scope_of({"scope": "all", "explicit": True}) == "all"
    assert knowledge_scope_of({"explicit": True, "collection_ids": ["kc_1"]}) == "pinned"
    assert knowledge_scope_of({"explicit": False, "collection_ids": ["kc_1", "kc_2"]}) == "inherited"
    payload = execution_payload(
        effective_mode="agent_kernel",
        response_form="chat",
        depth="normal",
        model="m",
        reasoning_effort=None,
        source_policy=SourcePolicy(),
        consent_reason="autonomous_policy",
        knowledge_scope="all",
        knowledge_collection_count=3,
    )
    assert payload["knowledge_scope"] == "all"
    assert payload["knowledge_collection_count"] == 3
    # Default: older call sites publish the honest "inherited" boundary.
    assert execution_payload(
        effective_mode="agent_kernel", response_form="chat", depth="normal", model="m",
        reasoning_effort=None, source_policy=SourcePolicy(), consent_reason="x",
    )["knowledge_scope"] == "inherited"


@pytest.mark.parametrize(
    "collection_ids", [[123], [""], [None], [{"id": "kc_1"}], "kc_1"]
)
def test_malformed_collection_ids_are_refused_not_widened(collection_ids) -> None:
    """A chip list the server cannot read is a bad request: reading it as
    "no chips" would silently widen the run to every visible collection."""
    # The route-test app runs without agent modes; the admission rules are
    # mode-independent, so a research run exercises the same code path.
    client = _make_app()
    response = client.post(
        "/v1/runs",
        json={"question": "Was gilt?", "mode": "research",
              "knowledge_filters": {"collection_ids": collection_ids}},
    )
    assert response.status_code == 400
    assert "collection_ids" in response.json()["error"]["message"]


def test_children_inherit_the_scope_label_with_the_boundary() -> None:
    """A delegated child reports the same provenance as its parent: the
    session switch stays "all", never "pinned" for the identical set."""
    from inqtrix.execution_authority import inherited_knowledge_filters

    assert inherited_knowledge_filters(frozenset({"kc_b", "kc_a"}), explicit=True, scope="all") == {
        "collection_ids": ["kc_a", "kc_b"], "explicit": True, "scope": "all",
    }
    assert inherited_knowledge_filters(frozenset({"kc_a"}), explicit=True) == {
        "collection_ids": ["kc_a"], "explicit": True,
    }
    assert inherited_knowledge_filters(None, explicit=False, scope="all") == {}
    from inqtrix.agents.source_policy import knowledge_boundary_payload

    assert knowledge_boundary_payload({"collection_ids": ["kc_a", "kc_b"], "explicit": True, "scope": "all"}) == {
        "knowledge_scope": "all", "knowledge_collection_count": 2,
    }
    assert knowledge_boundary_payload({"collection_ids": ["kc_a"], "explicit": True}) == {
        "knowledge_scope": "pinned", "knowledge_collection_count": 1,
    }


def test_unknown_scope_is_refused_not_ignored() -> None:
    client = _make_app()
    response = client.post(
        "/v1/runs",
        json={"question": "Was gilt?", "mode": "research", "knowledge_filters": {"scope": "everything"}},
    )
    assert response.status_code == 400


def test_explicit_knowledge_without_a_knowledge_store_is_refused_before_any_cost() -> None:
    """A chip on a stack without the knowledge service is refused with the
    reason before any cost instead of starting a run that drops the scope."""
    client = _make_app()
    response = client.post(
        "/v1/runs",
        json={"question": "Was gilt?", "mode": "research", "knowledge_filters": {"collection_ids": ["kc_1"]}},
    )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "knowledge_store_unavailable"
    assert error["reason"] == "operator_disabled"
