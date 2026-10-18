"""Every endpoint of the load-measurement provider stand-in must answer.

A stand-in that returns errors does not stop a load run: the pipeline treats
a failed provider call as non-fatal, produces an evidence-free answer, and
reports the run as completed. The measurement then describes a system that
did almost no work — and looks like a success.

So these tests exercise each route the application actually calls and require
a well-formed reply. They deliberately run without the response corpus, which
is untracked operator data: a fresh checkout must still be able to prove the
routes are wired.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_MOCK = (
    Path(__file__).resolve().parent / "load" / "mock-providers" / "mock_providers.py"
)


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    """Load the stand-in with an empty corpus and return a test client.

    A missing corpus is a supported, reported state — the stand-in serves
    synthesised replies and says so on /healthz. The environment is restored
    afterwards so the module under test cannot leak settings into the rest
    of the session.
    """
    from pytest import MonkeyPatch

    corpus = tmp_path_factory.mktemp("corpus-absent")
    patch = MonkeyPatch()
    patch.syspath_prepend(str(_MOCK.parent))
    patch.setenv("INQTRIX_MOCK_CORPUS", str(corpus))
    patch.setenv("INQTRIX_MOCK_LATENCY_SCALE", "0")
    patch.delenv("INQTRIX_MOCK_SELECT", raising=False)

    spec = importlib.util.spec_from_file_location("mock_providers_under_test", _MOCK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield TestClient(module.app)
    finally:
        patch.undo()
        sys.modules.pop(spec.name, None)


def test_health_reports_the_missing_corpus_rather_than_hiding_it(client) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "degraded"
    assert any("corpus" in note for note in body["degraded"])


@pytest.mark.parametrize(
    "path",
    [
        "/azure/openai/v1/chat/completions",
        "/v1/chat/completions",
    ],
)
def test_chat_completion_routes_answer(client, path) -> None:
    response = client.post(
        path,
        json={"messages": [{"role": "system", "content": "Beliebige Anweisung."}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "usage" in body


def test_structured_request_returns_json_matching_the_requested_schema(client) -> None:
    """Chunk contextualisation needs exactly as many entries as it asked for."""
    schema = {
        "type": "object",
        "properties": {
            "contexts": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "chunk_number": {"type": "integer"},
                        "context": {"type": "string", "minLength": 1},
                    },
                },
            }
        },
    }
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "system", "content": "Du situierst Textabschnitte."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "inqtrix_knowledge_chunk_contexts_v1",
                                "schema": schema},
            },
        },
    )
    assert response.status_code == 200, response.text
    parsed = json.loads(response.json()["choices"][0]["message"]["content"])
    contexts = parsed["contexts"]
    assert len(contexts) == 3
    assert [entry["chunk_number"] for entry in contexts] == [1, 2, 3]
    assert all(entry["context"] for entry in contexts)


def test_embeddings_return_one_vector_per_input_in_order(client) -> None:
    response = client.post(
        "/v1/embeddings", json={"input": ["alpha", "beta", "gamma"]}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [entry["index"] for entry in data] == [0, 1, 2]
    widths = {len(entry["embedding"]) for entry in data}
    assert len(widths) == 1


@pytest.mark.parametrize(
    "path",
    ["/perplexity/v1/responses", "/v1/responses"],
)
def test_search_routes_return_positional_results(client, path) -> None:
    """Extracted claims reference results by position, so ids start at one."""
    response = client.post(path, json={"input": [{"role": "user", "content": "q"}]})
    assert response.status_code == 200, response.text
    outputs = response.json()["output"]
    results = next(o for o in outputs if o["type"] == "search_results")["results"]
    assert results
    assert [entry["id"] for entry in results] == list(range(1, len(results) + 1))
    assert all(entry["url"] and entry["snippet"] for entry in results)


def test_foundry_and_anthropic_routes_answer(client) -> None:
    foundry = client.post(
        "/foundry/openai/v1/responses",
        json={"input": [{"role": "user", "content": "q"}]},
    )
    assert foundry.status_code == 200, foundry.text
    assert foundry.json()["output"][0]["content"][0]["type"] == "output_text"

    anthropic = client.post(
        "/anthropic/v1/messages",
        json={"system": "Beliebige Anweisung.", "messages": []},
    )
    assert anthropic.status_code == 200, anthropic.text
    assert anthropic.json()["content"][0]["type"] == "text"


def test_stats_count_every_served_call(client) -> None:
    """The evidence that a measurement reached no real provider."""
    before = client.get("/admin/stats").json()["total_calls"]
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "system", "content": "Noch eine Anweisung."}]},
    )
    after = client.get("/admin/stats").json()
    assert after["total_calls"] > before
    # An unrecognised instruction must be reported, never served quietly.
    assert after["unmatched_instructions"]


@pytest.fixture
def calibration(client):
    spec = importlib.util.spec_from_file_location("provider_calibration_under_test", _MOCK.with_name("calibrate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generation(identifier, *, purpose, schema_name="", output="answer", kind="GENERATION", name="text_completion"):
    return {
        "id": identifier, "type": kind, "name": name,
        "parentObservationId": "run", "latency": 2.5,
        "promptTokens": 100, "completionTokens": 25,
        "model": "fixture-model",
        "metadata": {"purpose": purpose, "attributes": {
            "inqtrix.request.schema_name": schema_name,
        }},
        "input": [{"role": "system", "content": "Write a concise document using the provided material."}],
        "output": [{"role": "assistant", "content": output}],
    }


def _write_export(directory, observations, mode="workspace_agent"):
    directory.mkdir(exist_ok=True)
    (directory / "trace.json").write_text(json.dumps({"payload": {
        "name": f"run:{mode}", "observations": [
            {"id": "run", "type": "AGENT", "name": "inqtrix.run"},
            *observations,
        ],
    }}))


def test_calibration_separates_purposes_and_response_schemas(calibration, tmp_path):
    _write_export(tmp_path, [
        _generation("intake", purpose="agent_intake", schema_name="AssignmentProfile", output="intake"),
        _generation("outline", purpose="agent_synthesis", schema_name="ReportOutline", output="outline"),
        _generation("section", purpose="agent_synthesis", schema_name="SectionText", output="section"),
        _generation("review", purpose="agent_deep_review", schema_name="DeepReviewVerdict", output="review"),
    ])
    operations, provenance, responses, routing = calibration.collect(str(tmp_path))
    expected = {
        "workspace_agent.agent_intake.AssignmentProfile.llm": "intake",
        "workspace_agent.agent_synthesis.ReportOutline.llm": "outline",
        "workspace_agent.agent_synthesis.SectionText.llm": "section",
        "workspace_agent.agent_deep_review.DeepReviewVerdict.llm": "review",
    }
    assert set(operations) == set(responses) == set(routing) == set(expected)
    assert provenance["provider_call_samples"] == 4
    assert {key: [reply["content"] for reply in pool] for key, pool in responses.items()} == {
        key: [value] for key, value in expected.items()
    }


def test_calibration_uses_nearest_operation_when_purpose_is_absent(calibration):
    observation = {"id": "call", "name": "text_completion", "parentObservationId": "inner"}
    parents = {
        "inner": {"id": "inner", "name": "extract", "parentObservationId": "outer"},
        "outer": {"id": "outer", "name": "search"},
    }
    assert calibration._node_of(observation, parents) == "extract"


@pytest.mark.parametrize("kind", ["SPAN", "GENERATION"])
def test_calibration_keeps_web_search_on_its_search_channel(calibration, tmp_path, kind):
    _write_export(tmp_path, [_generation("web", purpose="search", name="web_search", kind=kind)], "research")
    operations, provenance, responses, routing = calibration.collect(str(tmp_path))
    assert set(operations) == {"research.web_search.web_search"}
    assert operations["research.web_search.web_search"]["prompt_tokens_median"] == 100
    assert provenance["provider_call_samples"] == 1
    assert not responses and not routing


@pytest.mark.parametrize("language", ["de", "en"])
def test_capture_and_replay_remove_shared_framing_before_identifying_operation(calibration, language):
    from inqtrix.prompt_language import directive_for
    from prompt_routing import signature

    first = f"{directive_for(language)}\n\nToday's date: 10. September 2026.\n\nAnalyse the assignment."
    second = f"{directive_for(language)}\n\nToday's date: 2027-01-02.\n\nAnalyse the assignment."
    assert json.loads(calibration.signature(first)) == ["", "analyse the assignment."]
    assert signature(second) == calibration.signature(first)
    embedded = "You analyse assignments. Today's date: 2027-01-02.\nUse the requested schema."
    assert json.loads(signature(embedded)) == ["", "you analyse assignments. use the requested schema."]


def _catalogue(module, tmp_path, *, operations, responses, routing, provenance=None, version=2):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"operations": operations}))
    for key, replies in responses.items():
        (corpus / f"{key}.json").write_text(json.dumps(replies))
    (corpus / "manifest.json").write_text(json.dumps({
        "routing_version": version,
        "routing_signatures": routing,
        "responses_per_operation": {key: len(value) for key, value in responses.items()},
        "provenance": provenance or {},
    }))
    return module.Catalogue(str(profile), str(corpus))


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/azure/openai/v1/chat/completions", "/anthropic/v1/messages"])
def test_replay_uses_the_requested_schema_when_system_roles_are_shared(client, calibration, tmp_path, monkeypatch, path):
    module = sys.modules["mock_providers_under_test"]
    traces = tmp_path / "traces"
    _write_export(traces, [
        _generation("outline", purpose="agent_synthesis", schema_name="ReportOutline", output='{"sections":["Introduction"]}'),
        _generation("section", purpose="agent_synthesis", schema_name="SectionText", output='{"text":"Section body"}'),
    ])
    operations, provenance, responses, routing = calibration.collect(str(traces))
    catalogue = _catalogue(module, tmp_path, operations=operations, responses=responses, routing=routing, provenance=provenance)
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    assert catalogue.degraded == []
    for name, expected in [("ReportOutline", {"sections": ["Introduction"]}), ("SectionText", {"text": "Section body"})]:
        messages = [{"role": "system", "content": "Write a concise document using the provided material."}, {"role": "user", "content": "A different topic"}]
        if path == "/anthropic/v1/messages":
            body = {"system": messages[0]["content"], "messages": messages[1:], "output_config": {"format": {"type": "json_schema", "schema": {"title": name, "type": "object"}}}}
        else:
            body = {"messages": messages, "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": {"type": "object"}}}}
        response = client.post(path, json=body)
        assert response.status_code == 200
        payload = response.json()
        text = payload["content"][0]["text"] if path == "/anthropic/v1/messages" else payload["choices"][0]["message"]["content"]
        assert json.loads(text) == expected
        usage = payload["usage"]
        assert usage.get("input_tokens", usage.get("prompt_tokens")) == 100


def test_replay_rejects_ambiguous_and_merely_similar_instruction_openings(client, tmp_path):
    module = sys.modules["mock_providers_under_test"]
    key = json.dumps(["", "analyse the supplied task carefully and produce the first format."])
    catalogue = _catalogue(module, tmp_path, operations={}, responses={}, routing={"first": [key], "second": [key]})
    assert catalogue.resolve("Analyse the supplied task carefully and produce the first format.")[0] == ""
    assert any("ambiguous" in note for note in catalogue.degraded)
    catalogue.signatures = [(key, "first")]
    assert catalogue.resolve("Analyse the supplied task carefully and produce the other format.")[0] == ""


def test_old_routing_corpus_requires_recalibration(client, tmp_path):
    module = sys.modules["mock_providers_under_test"]
    catalogue = _catalogue(module, tmp_path, operations={}, responses={}, routing={"old": ["analyse the supplied task"]}, version=None)
    assert any("routing format" in note for note in catalogue.degraded)
    assert catalogue.resolve("Analyse the supplied task")[0] == ""


def test_unmatched_request_diagnostics_do_not_copy_prompt_content(client, caplog):
    module = sys.modules["mock_providers_under_test"]
    stats = module.Stats()
    probe = "Private assignment wording that must remain in the protected corpus"
    with caplog.at_level("WARNING", logger="inqtrix.mock-providers"):
        assert stats.record_unmatched(probe, 0)
        assert not stats.record_unmatched(probe, 0)
    counts = stats.snapshot()["unmatched_instructions"]
    assert len(counts) == 1 and list(counts.values()) == [2]
    assert probe not in str(counts)
    assert probe not in caplog.text
    assert "NOT calibrated" in caplog.text


def test_tool_call_capture_cannot_be_replayed_as_a_terminal_acknowledgement(calibration, tmp_path):
    call = _generation("tool", purpose="agent_kernel", name="chat", output="I will create the document.")
    call["output"][0]["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "create_document", "arguments": "{}"}}]
    final = _generation("done", purpose="agent_kernel", name="chat", output="Document created.")
    _write_export(tmp_path, [call, final], "agent_kernel")
    operations, provenance, responses, routing = calibration.collect(str(tmp_path))
    assert operations["agent_kernel.agent_kernel.llm"]["samples"] == 2
    assert provenance["non_replayable_operations"] == ["agent_kernel.agent_kernel.llm"]
    assert not responses and not routing


@pytest.mark.parametrize("path", ["/perplexity/v1/responses", "/foundry/openai/v1/responses"])
@pytest.mark.parametrize("prompt_tokens", [0, 8100])
def test_search_replay_reports_the_calibrated_token_counts(client, monkeypatch, path, prompt_tokens):
    module = sys.modules["mock_providers_under_test"]
    monkeypatch.setitem(module.CATALOGUE.operations, "research.web_search.web_search", {
        "prompt_tokens_median": prompt_tokens, "completion_tokens_median": 1250,
    })
    response = client.post(path, json={"input": "A test question"})
    assert response.status_code == 200
    assert response.json()["usage"] == {"input_tokens": prompt_tokens, "output_tokens": 1250}
