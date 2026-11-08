"""Tests for the core AlgorithmRegistry and the built-in registrations."""

from __future__ import annotations

import pytest

from inqtrix.core.algorithms import AlgorithmRegistry, UnknownAlgorithm
from inqtrix.core.results import RunRequest
from inqtrix.research.web_research import DirectLlmAlgorithm, WebResearchAlgorithm
from inqtrix.server.container import build_default_registry


def test_default_registry_registers_builtins_in_presentation_order():
    registry = build_default_registry()
    assert registry.ids() == ("research", "direct_llm")
    assert isinstance(registry.get("research"), WebResearchAlgorithm)
    assert isinstance(registry.get("direct_llm"), DirectLlmAlgorithm)


def test_the_registry_names_the_contract_each_mode_carries():
    assert build_default_registry().workflow_contracts() == {
        "research": 1,
        "direct_llm": 1,
    }


def test_duplicate_registration_fails_loudly():
    registry = build_default_registry()
    with pytest.raises(ValueError, match="already registered: research"):
        registry.register(WebResearchAlgorithm())


def test_unknown_algorithm_lists_available_ids():
    registry = build_default_registry()
    with pytest.raises(UnknownAlgorithm) as excinfo:
        registry.get("knowledge")
    assert "Available: research, direct_llm" in str(excinfo.value)


def test_manifest_carries_id_display_name_and_capabilities():
    registry = build_default_registry()
    manifest = registry.manifest()
    assert [entry["id"] for entry in manifest] == ["research", "direct_llm"]
    research = manifest[0]
    assert research["display_name"] == "Web Research"
    assert research["requires"] == ["llm", "web_search"]
    assert research["streams_events"] is True
    direct = manifest[1]
    assert direct["requires"] == ["llm"]


def test_custom_algorithm_registration_is_supported():
    class _CustomAlgorithm:
        id = "custom_mode"
        display_name = "Custom"
        contract_version = 1

        def capabilities(self) -> dict:
            return {"requires": []}

        def run(self, request, *, runtime, context):
            raise NotImplementedError

    registry = AlgorithmRegistry()
    registry.register(_CustomAlgorithm())
    assert registry.ids() == ("custom_mode",)


@pytest.mark.parametrize("contract_version", [None, 0, True, "1"])
def test_an_algorithm_without_a_workflow_contract_is_refused(contract_version):
    """Every run is stamped with its mode's contract, so a mode without one
    fails when it is registered, not at its first run."""

    class _Unbound:
        id = "unbound"
        display_name = "Unbound"

        def capabilities(self) -> dict:
            return {}

        def run(self, request, *, runtime, context):
            raise NotImplementedError

    algorithm = _Unbound()
    if contract_version is not None:
        algorithm.contract_version = contract_version
    registry = AlgorithmRegistry()

    with pytest.raises(ValueError, match="unbound names no workflow contract"):
        registry.register(algorithm)
    assert registry.ids() == ()


def test_web_research_child_enforces_inherited_source_policy(
    monkeypatch: pytest.MonkeyPatch,
):
    """A disabled inherited web source cannot reach the child graph."""
    invoked = False

    def fake_execute(request, context):
        nonlocal invoked
        invoked = True
        return {"answer": "should not run"}

    monkeypatch.setattr(
        "inqtrix.research.web_research._execute_graph", fake_execute
    )
    request = RunRequest(
        mode="research",
        question="x",
        source_policy={"web": "disabled", "knowledge": "available"},
    )

    with pytest.raises(PermissionError, match="source_policy.web=disabled"):
        WebResearchAlgorithm().run(request, runtime=None, context=None)

    assert invoked is False


def test_run_request_rejects_unsupported_delegated_web_recency() -> None:
    with pytest.raises(ValueError, match="web_recency"):
        RunRequest(
            mode="research",
            question="x",
            web_recency="hour",  # type: ignore[arg-type]
        )
