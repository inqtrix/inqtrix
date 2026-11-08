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
    operations, provenance, responses, routing, _ = calibration.collect(str(tmp_path))
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
    operations, provenance, responses, routing, _ = calibration.collect(str(tmp_path))
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


def test_a_system_message_that_is_only_the_security_notice_identifies_nothing(calibration):
    from inqtrix.untrusted_content import UNTRUSTED_CONTENT_RULE as notice
    from prompt_routing import system_prompt

    classify = [{"role": "system", "content": notice}, {"role": "user", "content": "Classify the question."}]
    plan = [{"role": "system", "content": notice}, {"role": "user", "content": "Plan the searches."}]
    assert calibration.signature(system_prompt(classify)) != calibration.signature(system_prompt(plan))
    # A schema names the contract, so the changing content of the prompt stays out of the identity.
    assert (
        calibration.signature(system_prompt(classify, schema_name="Judgement"), schema_name="Judgement")
        == calibration.signature(system_prompt(plan, schema_name="Judgement"), schema_name="Judgement")
    )
    own = [{"role": "system", "content": f"You analyse work. {notice}"}, {"role": "user", "content": "Anything."}]
    assert system_prompt(own).startswith("You analyse work.")


def test_calibration_leaves_different_contracts_on_one_identity_ambiguous(calibration, tmp_path):
    _write_export(tmp_path, [_generation("a", purpose="first"), _generation("b", purpose="second")], "research")
    _, provenance, _, routing, _ = calibration.collect(str(tmp_path))
    assert provenance["merged_signatures"] == 0
    assert set(routing) == {"research.first.llm", "research.second.llm"}


def _catalogue(module, tmp_path, *, operations, responses, routing, provenance=None, version=3):
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
    operations, provenance, responses, routing, _ = calibration.collect(str(traces))
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


def _kernel_turn(identifier, *, start, content="", calls=(), system="You are the agent.", user="Assignment:\nRecorded task", run="run_a"):
    entry = _generation(identifier, purpose="agent_kernel", name="chat", output=content)
    entry["startTime"] = start
    entry["metadata"]["attributes"]["inqtrix.run_id"] = run
    entry["input"] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    entry["output"][0]["tool_calls"] = list(calls)
    return entry


def _call(name, **arguments):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _tools(*names):
    return [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in names]


def _kernel_catalogue(calibration, module, tmp_path, observations):
    traces = tmp_path / "traces"
    _write_export(traces, observations, "agent_kernel")
    operations, provenance, responses, routing, conversations = calibration.collect(str(traces))
    catalogue = _catalogue(module, tmp_path, operations=operations, responses=responses, routing=routing, provenance=provenance)
    catalogue.conversations = conversations
    return catalogue, conversations


def test_calibration_keeps_tool_calls_in_recorded_order(calibration, tmp_path):
    later = _kernel_turn("done", start="2026-10-31T10:00:09Z", content="Document created.")
    first = _kernel_turn("tool", start="2026-10-31T10:00:01Z", calls=[_call("create_document", title="T")])
    _write_export(tmp_path, [later, first], "agent_kernel")
    operations, provenance, responses, routing, conversations = calibration.collect(str(tmp_path))
    assert operations["agent_kernel.agent_kernel.llm"]["samples"] == 2
    assert provenance["conversation_operations"] == ["agent_kernel.agent_kernel.llm"]
    recorded = conversations["agent_kernel.agent_kernel.llm"]
    assert [conversation["assignment"] for conversation in recorded] == ["Recorded task"]
    assert recorded[0]["turns"][0]["tool_calls"] == [{"name": "create_document", "arguments": {"title": "T"}}]
    assert recorded[0]["turns"][1]["content"] == "Document created."
    assert routing["agent_kernel.agent_kernel.llm"]


def test_calibration_gives_a_research_child_its_own_mode_under_a_kernel_trace(calibration, tmp_path):
    child = _generation("answer", purpose="answer")
    child["metadata"]["attributes"]["inqtrix.run_id"] = "run_child"
    agent = {"id": "child", "type": "AGENT", "name": "inqtrix.run", "metadata": {"attributes": {"inqtrix.run_id": "run_child", "inqtrix.mode": "research"}}}
    _write_export(tmp_path, [agent, child], "agent_kernel")
    operations, *_ = calibration.collect(str(tmp_path))
    assert set(operations) == {"research.answer.llm"}


def test_calibration_merges_one_instruction_recorded_under_two_modes(calibration, tmp_path):
    (tmp_path / "one.json").write_text(json.dumps({"payload": {"name": "run:workspace_agent", "observations": [_generation("a", purpose="review", output="first")]}}))
    (tmp_path / "two.json").write_text(json.dumps({"payload": {"name": "run:agent_kernel", "observations": [_generation("b", purpose="review", output="second")]}}))
    operations, provenance, responses, routing, _ = calibration.collect(str(tmp_path))
    assert provenance["merged_signatures"] == 1
    assert len(routing) == 1
    assert sorted(reply["content"] for pool in responses.values() for reply in pool) == ["first", "second"]


def test_replay_walks_the_recorded_tool_calls_turn_by_turn(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    catalogue, _ = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("one", start="2026-10-31T10:00:01Z", calls=[_call("write_todos", todos=[]), _call("run_web_research", question="Q")]),
        _kernel_turn("two", start="2026-10-31T10:00:05Z", calls=[_call("read_canvas", artifact_id="art_recorded_del_1")]),
        _kernel_turn("end", start="2026-10-31T10:00:09Z", content="Fertig."),
    ])
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    tools = _tools("write_todos", "run_web_research", "read_canvas")
    system = {"role": "system", "content": "You are the agent."}
    user = {"role": "user", "content": "Preamble\n\nAssignment:\nRecorded task (run 7)"}
    first = client.post("/v1/chat/completions", json={"messages": [system, user], "tools": tools}).json()["choices"][0]
    assert first["finish_reason"] == "tool_calls"
    assert [c["function"]["name"] for c in first["message"]["tool_calls"]] == ["write_todos", "run_web_research"]
    assert json.loads(first["message"]["tool_calls"][1]["function"]["arguments"]) == {"question": "Q"}
    held = [system, user, first["message"], {"role": "tool", "tool_call_id": "x", "content": "saved as art_thisrun_del_9"}]
    second = client.post("/v1/chat/completions", json={"messages": held, "tools": tools}).json()["choices"][0]
    assert [json.loads(c["function"]["arguments"]) for c in second["message"]["tool_calls"]] == [{"artifact_id": "art_thisrun_del_9"}]
    third = client.post("/v1/chat/completions", json={"messages": [*held, second["message"]], "tools": tools}).json()["choices"][0]
    assert third["finish_reason"] == "stop"
    assert third["message"]["content"] == "Fertig."
    assert "tool_calls" not in third["message"]


def test_a_replayed_answer_cites_only_evidence_this_request_knows(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    catalogue, _ = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("end", start="2026-10-31T10:00:09Z", content="Erstens [W1], zweitens [W7] und [K2, W9], drittens [K3]."),
    ])
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    messages = [
        {"role": "system", "content": "You are the agent."},
        {"role": "user", "content": "Assignment:\nRecorded task"},
        {"role": "tool", "tool_call_id": "x", "content": "[W1] Erste Quelle\n[W2] Zweite Quelle"},
    ]
    choice = client.post("/v1/chat/completions", json={"messages": messages, "tools": _tools("read_evidence")}).json()["choices"][0]
    # W7 and W9 point at known web labels; no knowledge label is known, so K2 and K3 go.
    assert choice["message"]["content"] == "Erstens [W1], zweitens [W2] und [W2], drittens ."


def test_fitting_citations_leaves_everything_else_alone():
    module = sys.modules["mock_providers_under_test"]
    known = module._known_labels([{"role": "user", "content": "[W1]"}])
    assert module._fit_citations("Kein Beleg, nur [Hinweis] und \\[W7\\].", known) == "Kein Beleg, nur [Hinweis] und \\[W7\\]."
    assert module._fit_citations('{"text": "Satz [W4]."}', known) == '{"text": "Satz [W1]."}'
    assert module._fit_citations('{"evidence_labels": ["W4", "K2"]}', known) == '{"evidence_labels": ["W1", "K2"]}'


def test_labels_in_the_instructions_are_no_evidence():
    module = sys.modules["mock_providers_under_test"]
    known = module._known_labels([
        {"role": "system", "content": "Cite like this: [K1] [W2]."},
        {"role": "tool", "content": "[W5] Quelle"},
    ])
    assert known == {"W": ["W5"]}


def test_a_replayed_document_cites_only_evidence_this_request_knows(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    recorded = {"title": "Memo", "content_markdown": "Aussage [W7] und [K3].", "evidence_labels": ["W9"]}
    catalogue, _ = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("one", start="2026-10-31T10:00:01Z", calls=[_call("write_canvas", **recorded)]),
        _kernel_turn("end", start="2026-10-31T10:00:09Z", content="Fertig."),
    ])
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    messages = [
        {"role": "system", "content": "You are the agent."},
        {"role": "user", "content": "Assignment:\nRecorded task"},
        {"role": "tool", "tool_call_id": "x", "content": "[W1] Erste Quelle"},
    ]
    choice = client.post("/v1/chat/completions", json={"messages": messages, "tools": _tools("write_canvas")}).json()["choices"][0]
    arguments = json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"])
    assert arguments["content_markdown"] == "Aussage [W1] und ."
    assert arguments["evidence_labels"] == ["W1"]


def test_a_refused_document_is_counted(client):
    before = client.get("/admin/stats").json()["evidence_errors"]
    messages = [{"role": "user", "content": "Frage"}, {"role": "tool", "tool_call_id": "x", "content": "Evidence error: unknown labels. The document was not saved."}]
    assert client.post("/v1/chat/completions", json={"messages": messages}).status_code == 200
    assert client.get("/admin/stats").json()["evidence_errors"] == before + 1


def _knowledge_prompt(*entries: tuple[str, str]) -> list[dict]:
    excerpts = "\n\n".join(f"[{label}] Titel (Abschnitt 1)\n{text}" for label, text in entries)
    return [
        {"role": "system", "content": "You answer from documents."},
        {"role": "user", "content": f'QUESTION:\nFrage\n\nDOCUMENT EXCERPTS:\n<untrusted_content source="knowledge-evidence">\n{excerpts}\n</untrusted_content>\n\nAnswer with QUOTES and ANSWER.'},
    ]


def test_a_replayed_knowledge_answer_passes_the_real_grounding_check():
    from inqtrix.knowledge.grounding import GroundingStatus, check_grounding

    module = sys.modules["mock_providers_under_test"]
    first = "# Titel\n\n## Abschnitt\n\nDiese Zeile ist die erste Fundstelle im Beleg.\nKurz.\nUnd hier steht eine zweite Fundstelle."
    second = "Eine weitere Stelle aus dem anderen Beleg."
    recorded = (
        'QUOTES:\n[K1] "Ein Satz aus einem ganz anderen Dokument."\n'
        '[K2] „Eine weitere Stelle aus dem anderen Beleg.“\n[K9] "Aus einem Beleg, den es hier nicht gibt."\n\n'
        "ANSWER:\n# Antwort\n\nText mit Beleg. [K1] [K9]"
    )
    messages = _knowledge_prompt(("K1", first), ("K2", second))
    fitted = module._fit_quotes(module._fit_citations(recorded, module._known_labels(messages)), messages)
    report = check_grounding(fitted, [first, second])
    assert report.status == GroundingStatus.VERIFIED
    assert [quote.label for quote in report.quotes] == ["K1", "K2", "K2"]
    assert report.answer == "# Antwort\n\nText mit Beleg. [K1] [K2]"
    # The unchanged answer is what the caller would reject, so this test can tell.
    assert check_grounding(recorded, [first, second]).status != GroundingStatus.VERIFIED


def test_fitting_quotes_leaves_every_other_reply_alone():
    module = sys.modules["mock_providers_under_test"]
    messages = _knowledge_prompt(("K1", "Eine Fundstelle mit genug Zeichen."))
    assert module._fit_quotes("Nur ein Text ohne Abschnitte. [K1]", messages) == "Nur ein Text ohne Abschnitte. [K1]"
    none = 'QUOTES:\nNONE\n\nANSWER:\nDie Auszüge beantworten die Frage nicht.'
    assert module._fit_quotes(none, messages) == none
    without_evidence = [{"role": "user", "content": "Keine Auszüge."}]
    recorded = 'QUOTES:\n[K1] "Satz"\n\nANSWER:\nText'
    assert module._fit_quotes(recorded, without_evidence) == recorded


def test_replay_drops_and_counts_a_call_this_request_cannot_make(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    catalogue, _ = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("one", start="2026-10-31T10:00:01Z", calls=[_call("run_web_research", question="Q"), _call("read_evidence", reference_id="ref_recorded01")]),
        _kernel_turn("end", start="2026-10-31T10:00:09Z", content="Fertig."),
    ])
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    before = client.get("/admin/stats").json()["tool_calls_dropped"].get("agent_kernel.agent_kernel.llm", 0)
    messages = [{"role": "system", "content": "You are the agent."}, {"role": "user", "content": "Assignment:\nRecorded task"}]
    choice = client.post("/v1/chat/completions", json={"messages": messages, "tools": _tools("read_evidence")}).json()["choices"][0]
    # Neither call can run: one tool is not offered, the other has no reference to read.
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "Fertig."
    assert client.get("/admin/stats").json()["tool_calls_dropped"]["agent_kernel.agent_kernel.llm"] == before + 2


def test_replay_prefers_a_recording_whose_tools_the_request_offers(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    catalogue, _ = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("mission", start="2026-10-31T10:00:01Z", user="Assignment:\nWith mission", run="run_m", calls=[_call("run_deep_mission", assignment="A")]),
        _kernel_turn("plain", start="2026-10-31T10:00:02Z", user="Assignment:\nWithout tools", run="run_p", content="Direkt."),
        _kernel_turn("plain-tool", start="2026-10-31T10:00:03Z", user="Assignment:\nWith todos", run="run_t", calls=[_call("write_todos", todos=[])]),
    ])
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    for ordinal in range(8):
        messages = [{"role": "system", "content": "You are the agent."}, {"role": "user", "content": f"Assignment:\nAnother task {ordinal}"}]
        choice = client.post("/v1/chat/completions", json={"messages": messages, "tools": _tools("write_todos")}).json()["choices"][0]
        names = [c["function"]["name"] for c in choice["message"].get("tool_calls") or []]
        assert "run_deep_mission" not in names


def test_replay_takes_the_recording_of_the_same_tier_for_the_same_assignment(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    catalogue, conversations = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("mid", start="2026-10-31T10:00:01Z", user="Tier: gruendlich (thorough)\nAssignment:\nSame task", run="run_g", calls=[_call("run_web_research", question="Q")]),
        _kernel_turn("deep", start="2026-10-31T10:00:02Z", user="Tier: tief (deep)\nAssignment:\nSame task", run="run_t", calls=[_call("run_deep_mission", assignment="A")]),
    ])
    assert sorted(entry["tier"] for entry in conversations["agent_kernel.agent_kernel.llm"]) == ["gruendlich", "tief"]
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    deep = [{"role": "system", "content": "You are the agent."}, {"role": "user", "content": "Tier: tief (deep)\nAssignment:\nSame task, run 3"}]
    # The deep tier also offers the middle tier's tool: only the tier tells the two recordings apart.
    choice = client.post("/v1/chat/completions", json={"messages": deep, "tools": _tools("run_web_research", "run_deep_mission")}).json()["choices"][0]
    assert [c["function"]["name"] for c in choice["message"]["tool_calls"]] == ["run_deep_mission"]


def test_replay_skips_a_turn_it_cannot_make_and_still_reaches_the_typed_result(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    catalogue, _ = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("read", start="2026-10-31T10:00:01Z", calls=[_call("read_evidence", reference_id="ref_recorded01")]),
        _kernel_turn("verdict", start="2026-10-31T10:00:05Z", calls=[_call("DeepReviewVerdict", complete=True)]),
    ])
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    messages = [{"role": "system", "content": "You are the agent."}, {"role": "user", "content": "Review this."}]
    choice = client.post("/v1/chat/completions", json={"messages": messages, "tools": _tools("read_evidence", "DeepReviewVerdict")}).json()["choices"][0]
    assert [c["function"]["name"] for c in choice["message"]["tool_calls"]] == ["DeepReviewVerdict"]


def test_a_request_without_tools_gets_a_recorded_answer_and_never_a_tool_call(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    traces = tmp_path / "traces"
    _write_export(traces, [
        _kernel_turn("tool", start="2026-10-31T10:00:01Z", calls=[_call("write_todos", todos=[])]),
        _kernel_turn("end", start="2026-10-31T10:00:09Z", content="Fertig."),
    ], "agent_kernel")
    operations, provenance, responses, routing, conversations = calibration.collect(str(traces))
    catalogue = _catalogue(module, tmp_path, operations=operations, responses=responses, routing=routing, provenance=provenance)
    catalogue.conversations = conversations
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    choice = client.post("/v1/chat/completions", json={"messages": [{"role": "system", "content": "You are the agent."}]}).json()["choices"][0]
    assert choice["message"]["content"] == "Fertig."
    assert "tool_calls" not in choice["message"]


def test_in_flight_returns_to_zero_and_a_disconnect_ends_the_delay(monkeypatch):
    import asyncio

    module = sys.modules["mock_providers_under_test"]
    stats = module.Stats()
    monkeypatch.setattr(module, "STATS", stats)
    monkeypatch.setattr(module, "_sample_latency", lambda operation, ordinal: 30.0)

    class Hangup:
        async def is_disconnected(self) -> bool:
            assert stats.snapshot()["in_flight"] == 1
            assert stats.snapshot()["in_flight_per_operation"] == {"any": 1}
            return True

    started = __import__("time").monotonic()
    asyncio.run(module._serve("any", Hangup()))
    assert __import__("time").monotonic() - started < 5
    assert stats.snapshot()["in_flight"] == 0
    assert stats.snapshot()["in_flight_per_operation"] == {}
    assert stats.snapshot()["disconnected"] == 1


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


def _responses_wire(create_kwargs: dict) -> dict:
    """The Responses request the product sends for a Chat Completions request."""
    from inqtrix.providers._openai_responses import responses_request

    return responses_request(create_kwargs, provider="azure")


def _product_completion(body: dict):
    """The answer as the product reads it: SDK model, then its own parser."""
    from openai.types.responses import Response

    from inqtrix.providers._openai_responses import chat_completion_from_response

    return chat_completion_from_response(Response.model_validate(body), url="http://mock/azure/openai/v1/responses")


def test_a_responses_request_of_a_gpt6_card_is_answered_in_the_form_the_product_reads(client) -> None:
    """The GPT-6 cards call the Responses API; a stand-in that serves only
    Chat Completions answers them 404, and a load run fails before any load
    is reached."""
    from inqtrix.providers._openai_responses import uses_responses

    assert uses_responses("gpt-6-luna")
    wire = _responses_wire({
        "model": "gpt-6-luna",
        "messages": [
            {"role": "system", "content": "Beliebige Anweisung."},
            {"role": "user", "content": "Frage."},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "mock_shape", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}},
        },
    })
    response = client.post("/azure/openai/v1/responses", json=wire)
    assert response.status_code == 200, response.text
    completion = _product_completion(response.json())
    content = completion.choices[0].message.content
    assert completion.choices[0].finish_reason == "stop"
    assert set(json.loads(content)) == {"answer"}
    assert completion.usage is not None


def test_replay_walks_the_recorded_tool_calls_over_the_responses_api(client, calibration, tmp_path, monkeypatch):
    module = sys.modules["mock_providers_under_test"]
    catalogue, _ = _kernel_catalogue(calibration, module, tmp_path, [
        _kernel_turn("one", start="2026-10-31T10:00:01Z", calls=[_call("write_todos", todos=[]), _call("run_web_research", question="Q")]),
        _kernel_turn("two", start="2026-10-31T10:00:05Z", calls=[_call("read_canvas", artifact_id="art_recorded_del_1")]),
        _kernel_turn("end", start="2026-10-31T10:00:09Z", content="Fertig."),
    ])
    monkeypatch.setattr(module, "CATALOGUE", catalogue)
    tools = _tools("write_todos", "run_web_research", "read_canvas")
    system = {"role": "system", "content": "You are the agent."}
    user = {"role": "user", "content": "Preamble\n\nAssignment:\nRecorded task (run 7)"}

    def turn(messages: list[dict]):
        wire = _responses_wire({"model": "gpt-6-luna", "messages": messages, "tools": tools})
        return _product_completion(client.post("/azure/openai/v1/responses", json=wire).json()).choices[0]

    first = turn([system, user])
    assert first.finish_reason == "tool_calls"
    assert [call.function.name for call in first.message.tool_calls] == ["write_todos", "run_web_research"]
    assistant = first.message.model_dump(exclude_none=True)
    held = [system, user, assistant, {"role": "tool", "tool_call_id": first.message.tool_calls[1].id, "content": "saved as art_thisrun_del_9"}]
    second = turn(held)
    assert [json.loads(call.function.arguments) for call in second.message.tool_calls] == [{"artifact_id": "art_thisrun_del_9"}]
    third = turn([*held, second.message.model_dump(exclude_none=True), {"role": "tool", "tool_call_id": second.message.tool_calls[0].id, "content": "Canvas."}])
    assert third.finish_reason == "stop"
    assert third.message.content == "Fertig."
