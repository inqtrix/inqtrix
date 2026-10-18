"""The opt-in live fixture cannot affect unmarked or unrelated requests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai import InternalServerError, RateLimitError

from inqtrix.agents.kernel.deps import set_kernel_deps
from inqtrix.observability.context import bind_purpose

spec = importlib.util.spec_from_file_location(
    "provider_fault_fixture", Path(__file__).parent / "verification/fixtures/provider-faults.py"
)
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


@pytest.fixture(autouse=True)
def clear_context():
    yield
    set_kernel_deps(None)


def test_unmarked_requests_and_other_purposes_pass_through_verbatim():
    fixture = fixture_module.ProviderFaults("inqv-fixture-isolation")
    original = Mock(return_value=object())
    kwargs = {"model": "fixture", "messages": [{"role": "user", "content": "ordinary question"}]}
    set_kernel_deps(SimpleNamespace(question="ordinary question"))
    with bind_purpose("agent_kernel"):
        assert fixture.dispatch(original, kwargs) is original.return_value
    original.assert_called_once_with(**kwargs)
    assert fixture.counts == {}
    question = "inqv-fixture-isolation MARKE2 diagnostic"
    kwargs["messages"][0]["content"] = question
    set_kernel_deps(SimpleNamespace(question=question))
    with bind_purpose("session_title"):
        assert fixture.dispatch(original, kwargs) is original.return_value
    assert fixture.counts == {}


def test_marker_in_old_history_cannot_select_a_different_current_case():
    fixture = fixture_module.ProviderFaults("inqv-fixture-history")
    original = Mock()
    question = "inqv-fixture-history MARKE5 diagnostic"
    set_kernel_deps(SimpleNamespace(question=question))
    kwargs = {"model": "fixture", "messages": [{"role": "user", "content": (
        "Earlier: inqv-fixture-history MARKE2 diagnostic. Current: " + question
    )}]}
    with bind_purpose("agent_kernel"):
        with pytest.raises(InternalServerError):
            fixture.dispatch(original, kwargs)
        with pytest.raises(RateLimitError):
            fixture.dispatch(original, kwargs)
        response = fixture.dispatch(original, kwargs)
    assert "erfolgreich fortgesetzt" in response.choices[0].message.content
    assert fixture.counts == {"transport": 3}
    original.assert_not_called()


@pytest.mark.parametrize("marker, reads, fact", [
    ("MARKE11", 2, "MARKEZIEL=83"),
    ("MARKE12", 3, "MARKEKATALOG=47"),
])
def test_archive_fixture_reveals_the_answer_only_after_real_continuation_reads(marker, reads, fact):
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.kernel.tools import build_kernel_tools
    from inqtrix.settings import AgentPlatformSettings

    fixture = fixture_module.ProviderFaults("inqv-fixture-archive")
    question = f"inqv-fixture-archive {marker} Read the prepared source."
    deps = KernelDeps(
        run_id="run_fixture_archive", question=question, control=MemoryAgentControlStore(),
        platform=AgentPlatformSettings(), llm=None, model=None, reasoning_effort=None, timeout=1,
    )
    set_kernel_deps(deps)
    read = next(tool for tool in build_kernel_tools() if tool.name == "read_canvas")
    original = Mock(return_value="real answer")
    kwargs = {"model": "fixture", "messages": [{"role": "user", "content": question}]}
    with bind_purpose("agent_kernel"):
        for index in range(reads):
            assert fact not in json.dumps(kwargs["messages"])
            response = fixture.dispatch(original, kwargs)
            call = response.choices[0].message.tool_calls[0]
            assert call.function.name == "read_canvas"
            arguments = json.loads(call.function.arguments)
            if marker == "MARKE11" and index == 1:
                assert arguments["offset"] > 60_000
                assert len(arguments["content_hash"]) == 64
            result = read.invoke(arguments)
            assert not result.startswith("Tool error")
            kwargs["messages"].append({"role": "tool", "content": result})
        assert fact in json.dumps(kwargs["messages"])
        assert fixture.dispatch(original, kwargs) == "real answer"
    original.assert_called_once_with(**kwargs)


def test_synthetic_bulk_search_is_limited_to_its_current_verification_assignment(monkeypatch):
    import inqtrix.providers as providers
    from inqtrix.search_result import GroundedSearchResult

    fixture = fixture_module.ProviderFaults("inqv-fixture-bulk-search")
    live = GroundedSearchResult(answer="Ordinary provider response")
    search = SimpleNamespace(search=Mock(return_value=live))
    monkeypatch.setattr(providers, "_build_search_provider", lambda *a, **kw: search)
    monkeypatch.setattr(providers, "_build_llm_provider", Mock())
    fixture.install()
    instrumented = providers._build_search_provider(None, None)
    set_kernel_deps(SimpleNamespace(question="inqv-fixture-bulk-search MARKE15 Diagnose"))
    captured = instrumented.search("Synthetic source")
    assert len(captured.answer) > 60_000 and captured.answer.endswith("Abschließender Prüfwert: 93.\n")
    for question in ("Ordinary task", "inqv-other-run MARKE15 Diagnose", "inqv-fixture-bulk-search MARKE14 Follow up"):
        set_kernel_deps(SimpleNamespace(question=question))
        assert instrumented.search("Ordinary query") is live
