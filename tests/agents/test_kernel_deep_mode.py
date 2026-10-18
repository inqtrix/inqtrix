"""Deep-mode kernel budgets, prompts, and verification.

Deep must verify MEASURABLY, not just cost more: the trajectory pins
that a deep run issues the rubric review call, that findings trigger
exactly ONE revision whose text becomes the final answer, and that a
normal run never touches the review path. The unit tests pin the
deterministic derivations (effort precedence, iteration ceiling,
prompt line, child profiles).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import logging
import re

import pytest

pytest.importorskip("deepagents")

from inqtrix.agents.prompts import build_kernel_user_message
from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm
from inqtrix.i18n import t
from inqtrix.observability.context import current_purpose
from inqtrix.pagination import encode_cursor
from inqtrix.providers.base import ChatTurn, ToolCallRequest
from inqtrix.runs.shared import (
    ANSWER_DRAFT_EVENT,
    answer_artifact_id,
    answer_draft_id,
    expand_run_event,
)

from tests.agents.test_kernel_prompt_events import (
    ScriptedToolLLM,
    _text_turn,
    make_client,
    wait_status,
)
from tests.agents.test_review_pass import _fenced, unquoted


@pytest.fixture(autouse=True)
def review_by_role(monkeypatch):
    """Every review in this file is the ROLE, the transport above the budget.

    The pass picks its transport by material size and these fixtures are
    small, so without this the review would run as one structured call and
    the scripted role turns would never be reached. The single call has its
    own tests; here the role is the subject.
    """
    from inqtrix.agents.kernel import review_pass

    monkeypatch.setattr(review_pass, "REVIEW_IN_PROMPT_BUDGET_TOKENS", 1)


REVIEW_RESULT_CALL = "DeepReviewVerdict"


class StructuredScriptedLLM(ScriptedToolLLM):
    """ScriptedToolLLM plus the scripted typed outputs of a Deep run.

    The review runs as a role child of the kernel. Its model call is
    recognised by the typed result on its tool surface and answered with the
    next scripted entry, delivered as that result; ``structured_calls``
    records it next to the revision, which stays a structured call.
    """

    def __init__(self, turns: list[Any], structured: list[Any]) -> None:
        super().__init__(turns)
        self._structured = list(structured)
        self.structured_calls: list[dict[str, Any]] = []
        self.review_tools: list[str] = []
        self.review_purposes: list[str] = []
        self.role_reads: list[Any] = []
        """Reads the review role performs before its verdict: each entry maps
        the role's transcript so far to the arguments of one tool call."""
        self.tool_choices: list[tuple[str, Any]] = []
        """Per model call: who called (``role`` or ``conversation``) and the
        ``tool_choice`` the provider received (``"absent"`` when none)."""

    def review_verdict(self, prompt: str) -> Any:
        if not self._structured:
            raise AssertionError("scripted structured provider ran out")
        return self._structured.pop(0)

    def chat(self, messages: Any, *, tools: Any = None, **kwargs: Any) -> ChatTurn:
        names = [item["function"]["name"] for item in tools or []]
        if REVIEW_RESULT_CALL not in names:
            self.tool_choices.append(("conversation", kwargs.get("tool_choice", "absent")))
            return super().chat(messages, tools=tools, **kwargs)
        self.tool_choices.append(("role", kwargs.get("tool_choice", "absent")))
        prompt = "\n".join(str(item.get("content") or "") for item in messages)
        self.review_tools = names
        self.chat_calls.append({"messages": list(messages), "tools": tools})
        bound = current_purpose()
        self.review_purposes.append(bound.purpose if bound is not None else "")
        if self.role_reads:
            name, arguments = self.role_reads.pop(0)(prompt)
            return ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(
                        id=f"call_role_read_{len(self.role_reads)}",
                        name=name,
                        arguments=arguments,
                    ),
                ),
                finish_reason="tool_calls",
                model="high-model",
                prompt_tokens=7,
                completion_tokens=3,
                raw=None,
            )
        self.structured_calls.append(
            {
                "prompt": prompt,
                "schema_name": REVIEW_RESULT_CALL,
                "model": kwargs.get("model"),
                "reasoning_effort": kwargs.get("reasoning_effort"),
            }
        )
        return ChatTurn(
            text="",
            tool_calls=(
                ToolCallRequest(
                    id=f"call_review_{len(self.structured_calls)}",
                    name=REVIEW_RESULT_CALL,
                    arguments=self.review_verdict(prompt),
                ),
            ),
            finish_reason="tool_calls",
            model="high-model",
            prompt_tokens=7,
            completion_tokens=3,
            raw=None,
        )

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(
        self, prompt: str, *, schema: Any, schema_name: str, **kwargs: Any
    ) -> Any:
        self.structured_calls.append(
            {
                "prompt": prompt,
                "schema_name": schema_name,
                "model": kwargs.get("model"),
                "reasoning_effort": kwargs.get("reasoning_effort"),
            }
        )
        if not self._structured:
            raise AssertionError("scripted structured provider ran out")
        return SimpleNamespace(
            parsed=self._structured.pop(0),
            prompt_tokens=7,
            completion_tokens=3,
        )


class CanvasReviewLLM(StructuredScriptedLLM):
    """Target the concrete canvas id the handover names.

    The review finds a missing heading on the canvas; the revision answers
    with :attr:`edits` and the re-check of its applied edits with
    :attr:`checks`. Every other structured call is unscripted and fails.
    """

    edits: tuple[dict[str, Any], ...] = (
        {"finding_id": "finding_1", "replacement": "## Unsicherheit\n\nUnsicherheit fehlt."},
    )
    checks: tuple[dict[str, Any], ...] = tuple(unquoted("finding_1")["findings"])

    def __init__(self, turns: list[Any], structured: list[Any]) -> None:
        super().__init__(turns, structured)
        self.role_reads = [
            lambda prompt: ("read_canvas", {"artifact_id": self._canvas_id(prompt)})
        ]

    @staticmethod
    def _canvas_id(prompt: str) -> str:
        match = re.search(r"canvas artifact_id=(\S+)", prompt)
        assert match, prompt
        return match.group(1)

    def review_verdict(self, prompt: str) -> Any:
        self.canvas_id = self._canvas_id(prompt)
        return {
            "complete": True,
            "grounded": True,
            "contradictions_named": False,
            "findings": [
                {
                    "target": "artifact",
                    "artifact_id": self.canvas_id,
                    "finding": "Canvas nennt die Unsicherheit nicht.",
                    "kind": "output_requirement",
                    "quote": "Unsicherheit fehlt.",
                    "basis_ids": [],
                }
            ],
        }

    def complete_structured(
        self, prompt: str, *, schema: Any, schema_name: str, **kwargs: Any
    ) -> Any:
        self.structured_calls.append(
            {"prompt": prompt, "schema_name": schema_name, **kwargs}
        )
        replies = {
            "DeepRevisionBundle": {"edits": list(self.edits)},
            "RevisionCheckVerdict": {"findings": list(self.checks)},
        }
        if schema_name not in replies:
            raise AssertionError(f"unscripted structured call {schema_name}")
        return SimpleNamespace(
            parsed=replies[schema_name], prompt_tokens=7, completion_tokens=3
        )


def _canvas_turn(content: str = "# Canvas\n\nUnsicherheit fehlt.") -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(
                id="call_canvas_deep",
                name="write_canvas",
                arguments={
                    "title": "Canvas",
                    "content_markdown": content,
                },
            ),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def test_deep_run_reviews_and_revises_once(monkeypatch):
    from inqtrix.observability import trace_scores
    recorded_verdicts = []
    original_record = trace_scores.record_deep_review_scores

    def record(verdict):
        recorded_verdicts.append((len(verdict.findings), verdict.complete, verdict.grounded, verdict.contradictions_named))
        original_record(verdict)

    monkeypatch.setattr(trace_scores, "record_deep_review_scores", record)
    llm = StructuredScriptedLLM(
        [_text_turn("Erste Antwort ohne Belege.")],
        [
            {
                "complete": True,
                "grounded": False,
                "contradictions_named": True,
                "findings": [
                    {
                        "target": "chat",
                        "artifact_id": "",
                        "finding": "Faktische Aussagen tragen keine Belege.",
                        "kind": "output_requirement",
                        "quote": "Erste Antwort ohne Belege.",
                        "basis_ids": [],
                    }
                ],
            },
            {
                "edits": [{"finding_id": "finding_1", "replacement": (
                    "## Einordnung\n\nErste Antwort ohne Belege."
                )}],
            },
            unquoted("finding_1"),
        ],
    )
    client = make_client(llm)
    with client:
        # The run reads a report, so its review answers to evidence and
        # re-checks the applied edit; a run without any source is reviewed
        # from general knowledge and re-checks nothing.
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Die Lage ist ruhig [E1].",
            "references": [{"label": "E1", "url": "https://example.org/lage"}],
        }, "Erste Antwort ohne Belege.", "sess-deep-flow")
        result = client.get(f"/v1/runs/{run_id}/result").json()
        assert result["answer"] == "## Einordnung\n\nErste Antwort ohne Belege."
        # Exactly review, revision and the re-check of its applied edit, in
        # that order, on the right models: the review and its re-check use
        # the review's selected tier, while the revision keeps the kernel's
        # automatic Deep effort.
        assert [c["schema_name"] for c in llm.structured_calls] == [
            "DeepReviewVerdict",
            "DeepRevisionBundle",
            "RevisionCheckVerdict",
        ]
        review, revision, recheck = llm.structured_calls
        assert review["model"] == "high-model"
        assert review["reasoning_effort"] is None
        assert (recheck["model"], recheck["reasoning_effort"]) == ("high-model", None)
        assert "rubric" in review["prompt"]
        assert "factual statement IN SUBSTANCE, with its conditions" in review["prompt"]
        assert revision["model"] == "high-model"
        assert revision["reasoning_effort"] == "high"
        assert "Faktische Aussagen tragen keine Belege." in revision["prompt"]
        # The deep prompt line reached the model's user message.
        first_user = str(llm.chat_calls[0]["messages"])
        assert "Deep mode" in first_user
        events = client.get(
            f"/v1/runs/{run_id}/events",
            params={"format": "json"},
        ).json()["data"]
        review_narrations = [
            event
            for event in events
            if event.get("data", {}).get("narration_id")
            == "kernel_deep_review"
        ]
        # Same narration_id twice (clients upsert): first the running
        # state, then the actual OUTCOME — never a promised revision
        # that silently failed.
        assert [n["data"]["final"] for n in review_narrations] == [
            False,
            False,
            True,
        ]
        assert "1 Befund" in review_narrations[1]["data"]["text"]
        assert recorded_verdicts == [(1, True, False, True)]
        # A heading cites no source, so its edit stays unverified.
        assert "überarbeitet, nicht nachgeprüft" in review_narrations[2]["data"]["text"]


@pytest.mark.parametrize("overrides, model, effort, source", [
    ({}, "high-model", "medium", "tier:high"),
    ({"model": "selected-model"}, "selected-model", None, "explicit_request"),
    ({"model": "selected-model", "effort": "low"}, "selected-model", "low", "explicit_request"),
    ({"model": "selected-model", "effort": "none"}, "selected-model", "none", "explicit_request"),
    ({"model_tier": "mid"}, "mid-model", "low", "tier:mid"),
    ({"model_tier": "fast"}, "fast-model", "none", "tier:fast"),
])
def test_deep_review_uses_selected_settings_without_kernel_effort_fallback(
    overrides, model, effort, source,
):
    llm = StructuredScriptedLLM([_text_turn("Eine kurze Antwort.")], [{
        "complete": True, "grounded": True,
        "contradictions_named": True, "findings": [],
    }])
    llm.models.tier_high_effort = "medium"
    llm.models.tier_mid_effort = "low"
    llm.models.tier_fast_effort = "none"
    client = make_client(llm)
    with client:
        response = client.post("/v1/runs", json={
            "question": "Antworte kurz.", "mode": "agent_kernel",
            "agent_overrides": {"depth": "deep", **overrides},
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert len(llm.structured_calls) == 1
        review = llm.structured_calls[0]
        assert review["schema_name"] == "DeepReviewVerdict"
        assert (review["model"], review["reasoning_effort"]) == (model, effort)
        role = client.get(f"/v1/runs/{run_id}/children").json()["data"][0]["run_id"]
        resolutions = {
            event["data"]["node"]: event["data"]
            for run in (run_id, role)
            for event in client.get(f"/v1/runs/{run}/events?format=json").json()["data"]
            if event["type"] == "inqtrix.node.model_resolution"
        }
        # The review resolves its model on its own run, under its own node.
        desc = resolutions["agent_deep_review"]
        assert (desc["model"], desc["effort"], desc["model_source"]) == (model, effort or "", source)
        # Deep may raise the loop's effort, but that is not a user selection.
        assert resolutions["agent_kernel"]["effort"] == overrides.get("effort", "high")


@pytest.mark.parametrize("overrides, model, effort", [
    ({}, "mid-model", "low"),
    ({"model": "chosen-model", "effort": "none"}, "chosen-model", "none"),
])
def test_deep_review_respects_skill_pins_and_explicit_request_priority(overrides, model, effort):
    from tests.agents.test_workspace_agent_skills import _create_skill

    llm = StructuredScriptedLLM([_text_turn("Kurze Antwort.")], [{
        "complete": True, "grounded": True,
        "contradictions_named": True, "findings": [],
    }])
    client = make_client(llm)
    with client:
        skill_id = _create_skill(
            client, clarification_points=[], instructions_markdown="Arbeite knapp.",
            model_tier="mid", effort="low", requires_plan="never",
        )
        response = client.post("/v1/runs", json={
            "question": "Antworte kurz.", "mode": "agent_kernel", "skill_ids": [skill_id],
            "agent_overrides": {"depth": "deep", **overrides},
        })
        assert response.status_code == 202, response.text
        summary = wait_status(client, response.json()["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert len(llm.structured_calls) == 1
        review = llm.structured_calls[0]
        assert (review["model"], review["reasoning_effort"]) == (model, effort)


def test_deep_revision_receives_every_review_finding():
    findings = [
        {
            "target": "chat",
            "artifact_id": "",
            "finding": f"Befund Nummer {index}",
            "kind": "output_requirement",
            "quote": f"Teil {index}.",
            "basis_ids": [],
        }
        for index in range(9)
    ]
    llm = StructuredScriptedLLM(
        [_text_turn(" ".join(f"Teil {index}." for index in range(9)))],
        [
            {
                "complete": False,
                "grounded": False,
                "contradictions_named": False,
                "findings": findings,
            },
            {"edits": [
                {"finding_id": f"finding_{index + 1}", "replacement": f"Korrigiert {index}."}
                for index in range(9)
            ]},
            unquoted(*(f"finding_{index + 1}" for index in range(9))),
        ],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Pruefe alles.",
                "mode": "agent_kernel",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert "Befund Nummer 8" in llm.structured_calls[1]["prompt"]


def test_deep_run_without_findings_keeps_the_answer():
    llm = StructuredScriptedLLM(
        [_text_turn("Kurze direkte Einordnung.")],
        [
            {
                "complete": True,
                "grounded": True,
                "contradictions_named": True,
                "findings": [],
            }
        ],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Kurzfrage?",
                "mode": "agent_kernel",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        result = client.get(f"/v1/runs/{run['run_id']}/result").json()
        assert result["answer"] == "Kurze direkte Einordnung."
        # Review ran, revision did NOT.
        assert [c["schema_name"] for c in llm.structured_calls] == [
            "DeepReviewVerdict"
        ]


def test_deep_review_pulls_the_retained_material_and_the_revision_gets_the_named_bases():
    """The review role reaches the run's whole retained material by reading it;
    the revision receives exactly the bases its findings name."""
    import json

    def reference_id(prompt: str) -> str:
        match = re.search(r'"reference_id": "(ref_[0-9a-f]+)"', prompt)
        assert match, prompt
        return match.group(1)

    # The source behind [W1] holds no stored text: the fix keeps its
    # sentence and adds the condition as a sentence of its own.
    llm = StructuredScriptedLLM([], [
        {"edits": [{"finding_id": "finding_1",
                    "replacement": "Fünf Jahre Garantie [W1]. Sie gilt bei Registrierung [W1]."}]},
    ])
    llm.role_reads = [
        lambda prompt: ("read_evidence", {}),
        lambda prompt: ("read_evidence", {"reference_id": reference_id(prompt)}),
    ]
    llm.review_verdict = lambda prompt: {
        "complete": False, "grounded": False, "contradictions_named": True,
        "findings": [{"target": "chat", "artifact_id": "", "finding": "Bedingung fehlt.",
                      "kind": "missing_information", "quote": "Fünf Jahre Garantie [W1].",
                      "basis_ids": ["source:" + reference_id(prompt)]}],
    }
    client = make_client(llm)
    with client:
        store = client.container.run_store
        body = "Die Garantie gilt fünf Jahre bei Registrierung. " * 80
        payload = {
            "answer": "Fünf Jahre bei Registrierung [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty"}],
            "metrics": {"evidence_contract_status": "needs_review", "confidence": 6,
                        "single_source_ratio": 0.75},
            "web_search_ledger": {"searches": {"query_warranty": {
                "query_id": "query_warranty", "source_run_id": "",
                "query": "Garantiebedingungen?", "provider_answer": body,
                "citations": [{"citation_id": "citation_warranty", "url": "https://example.org/warranty",
                               "grounded_support": "Fünf Jahre bei Registrierung."}],
            }}},
        }

        def seed(handle):
            payload["web_search_ledger"]["searches"]["query_warranty"]["source_run_id"] = handle.run_id
            handle.complete(payload)

        origin = store.submit(question="Garantiebedingungen?", stack_name="default",
                              work=seed, mode="research", kind="standard")["run_id"]
        wait_status(client, origin, {"completed"})
        llm._turns = [ChatTurn(text="", tool_calls=(ToolCallRequest(
            id="read_attached", name="read_research_report", arguments={"report_id": origin},
        ),), finish_reason="tool_calls", model="high-model", prompt_tokens=10, completion_tokens=5, raw=None),
            _text_turn("Fünf Jahre Garantie [W1].")]
        run = client.post("/v1/runs", json={
            "question": "Prüfe die Bedingung.", "mode": "agent_kernel", "autonomy": "autonomous",
            "session_id": "sess-deep-retained", "report_ids": [origin], "agent_overrides": {"depth": "deep"},
        }).json()
        assert wait_status(client, run["run_id"], {"completed", "failed"})["status"] == "completed"
        assert client.get(f"/v1/runs/{run['run_id']}/result").json()["answer"] == (
            "Fünf Jahre Garantie [W1]. Sie gilt bei Registrierung [W1]."
        )
        record = next(
            event["data"] for event in client.get(
                f"/v1/runs/{run['run_id']}/events", params={"format": "json"}
            ).json()["data"] if event["type"] == "inqtrix.agent.outcome"
        )
        # The web source behind [W1] is kept only as the provider's context:
        # the reach counts it as provider-only.
        assert (record["reach"], record["lines"]) == (
            {"reread": 1, "cited": 1, "provider_only": 1}, [],
        )
        # No re-check call: the web source behind [W1] is kept only as the
        # provider's context, so no quote under its label could hold.
        review, revision = llm.structured_calls
        # Pulled, not pushed: the full provider answer reached the role through
        # its own read, and the run diagnostics travelled in the handover.
        assert body.strip() in review["prompt"]
        assert '"evidence_contract_status": "needs_review"' in review["prompt"]
        raw = revision["prompt"].split('<untrusted_content source="review-basis">', 1)[1]
        context = json.loads(raw.split('</untrusted_content>', 1)[0])
        named = [item["basis_id"] for item in context["sources"]]
        assert named == ["source:" + reference_id(review["prompt"])]
        assert context["searches"]["query_warranty"]["provider_answer"] == body
        # The finding names no diagnostic, so none is handed to the revision.
        assert context["run_diagnostics"] == []


def _seeded_report(client, payload: dict) -> str:
    """A finished research report holding *payload*, by its run id."""
    store = client.container.run_store

    def seed(handle):
        for search in payload.get("web_search_ledger", {}).get("searches", {}).values():
            search["source_run_id"] = handle.run_id
        handle.complete(payload)

    origin = store.submit(question="Garantiebedingungen?", stack_name="default",
                          work=seed, mode="research", kind="standard")["run_id"]
    wait_status(client, origin, {"completed"})
    return origin


def _report_read_turn(origin: str) -> ChatTurn:
    """The kernel turn that reads the report *origin*: its sources enter the
    run's ledger, so the run's review answers to evidence."""
    return ChatTurn(text="", tool_calls=(ToolCallRequest(
        id="read_attached", name="read_research_report", arguments={"report_id": origin},
    ),), finish_reason="tool_calls", model="high-model", prompt_tokens=10, completion_tokens=5, raw=None)


def _deep_run_over_a_report(client, llm, payload: dict, answer: str, session_id: str) -> str:
    """One Deep kernel turn that reads a seeded research report and answers."""
    origin = _seeded_report(client, payload)
    llm._turns = [_report_read_turn(origin), _text_turn(answer)]
    run = client.post("/v1/runs", json={
        "question": "Prüfe die Bedingung.", "mode": "agent_kernel", "autonomy": "autonomous",
        "session_id": session_id, "report_ids": [origin], "agent_overrides": {"depth": "deep"},
    }).json()
    assert wait_status(client, run["run_id"], {"completed", "failed"})["status"] == "completed"
    return run["run_id"]


def _reference_ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r'"reference_id": "(ref_[0-9a-f]+)"', prompt)))


def test_a_finding_may_rest_on_a_source_the_role_read_but_the_output_does_not_cite():
    """The role reads the whole released ledger, not only what the answer cites.

    An omitted limitation is found exactly there: in a source the answer left
    out. Such a basis is known (the run's own ledger holds it) and read, so
    the finding is applied and the revision receives that source.
    """
    import json

    seen: dict[str, list[str]] = {}

    def read_the_uncited(prompt: str):
        seen["ids"] = _reference_ids(prompt)
        return ("read_evidence", {"reference_id": seen["ids"][1]})

    # Neither source holds stored text: the fix keeps the sentence and adds
    # the limitation as a sentence of its own, under the source it names.
    llm = StructuredScriptedLLM([], [
        {"edits": [{"finding_id": "finding_1",
                    "replacement": "Fünf Jahre Garantie [W1]. Das gilt nur in der EU [W2]."}]},
        unquoted("finding_1"),
    ])
    llm.role_reads = [lambda prompt: ("read_evidence", {}), read_the_uncited]
    llm.review_verdict = lambda prompt: {
        "complete": True, "grounded": False, "contradictions_named": False,
        "findings": [{"target": "chat", "artifact_id": "",
                      "finding": "Die Einschränkung aus der zweiten Quelle fehlt.",
                      "kind": "evidence_error", "quote": "Fünf Jahre Garantie [W1].",
                      "basis_ids": ["source:" + seen["ids"][1]]}],
    }
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre [E1]. Nur in der EU [E2].",
            "references": [{"label": "E1", "url": "https://example.org/warranty"},
                           {"label": "E2", "url": "https://example.org/limits"}],
            "web_search_ledger": {"searches": {}},
        }, "Fünf Jahre Garantie [W1].", "sess-deep-uncited-basis")
        assert client.get(f"/v1/runs/{run_id}/result").json()["answer"] == (
            "Fünf Jahre Garantie [W1]. Das gilt nur in der EU [W2]."
        )
    [revision] = [call for call in llm.structured_calls if call["schema_name"] == "DeepRevisionBundle"]
    raw = revision["prompt"].split('<untrusted_content source="review-basis">', 1)[1]
    context = json.loads(raw.split("</untrusted_content>", 1)[0])
    # Exactly the named basis travels to the revision — the uncited source.
    assert [item["basis_id"] for item in context["sources"]] == ["source:" + seen["ids"][1]]
    assert context["sources"][0]["reference"]["url"] == "https://example.org/limits"


def test_a_basis_outside_the_runs_ledger_opens_that_finding_and_keeps_the_rest():
    """Reading a stored search answer registers its citations in the ROLE's ledger.

    Such a reference does not exist in the ledger of the run under work, so a
    finding that names it cannot be confirmed there. It becomes an open point
    on its own; the other findings of the same verdict are still applied.
    """
    seen: dict[str, str] = {}

    def read_the_foreign_citation(prompt: str):
        known = _reference_ids(prompt)
        seen["foreign"] = known[-1]
        return ("read_evidence", {"reference_id": seen["foreign"]})

    llm = StructuredScriptedLLM([], [
        {"edits": [{"finding_id": "finding_1",
                    "replacement": "## Registrierung\n\nRegistrierung nötig?"}]},
        unquoted("finding_1"),
    ])
    llm.role_reads = [
        lambda prompt: ("read_evidence", {"query_id": "query_warranty"}),
        read_the_foreign_citation,
    ]
    llm.review_verdict = lambda prompt: {
        "complete": True, "grounded": False, "contradictions_named": True,
        "findings": [
            {"target": "chat", "artifact_id": "", "finding": "Die Zahl steht so nicht in der Zweitquelle.",
             "kind": "evidence_error", "quote": "Fünf Jahre Garantie [W1].",
             "basis_ids": ["source:" + seen["foreign"]]},
            {"target": "chat", "artifact_id": "", "finding": "Der Satz ist als Frage formuliert.",
             "kind": "output_requirement", "quote": "Registrierung nötig?", "basis_ids": []},
        ],
    }
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre bei Registrierung [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty"}],
            "web_search_ledger": {"searches": {"query_warranty": {
                "query_id": "query_warranty", "source_run_id": "",
                "query": "Garantiebedingungen?", "provider_answer": "Fünf Jahre bei Registrierung.",
                "citations": [
                    {"citation_id": "c1", "url": "https://example.org/warranty",
                     "grounded_support": "Fünf Jahre bei Registrierung."},
                    {"citation_id": "c2", "url": "https://example.org/second",
                     "grounded_support": "Drei Jahre ohne Registrierung."},
                ],
            }}},
        }, "Fünf Jahre Garantie [W1].\n\nRegistrierung nötig?", "sess-deep-foreign-basis")
        # The confirmed finding was applied, the unconfirmed one left its passage alone.
        assert client.get(f"/v1/runs/{run_id}/result").json()["answer"] == (
            "Fünf Jahre Garantie [W1].\n\n## Registrierung\n\nRegistrierung nötig?"
        )
        events = client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]
        record = next(e["data"] for e in events if e["type"] == "inqtrix.agent.outcome")
        assert [(line["kind"], line["reasons"]) for line in record["lines"]] == [
            ("finding", ["basis_unconfirmed"]),
        ]
        assert "Die Zahl steht so nicht in der Zweitquelle." in record["lines"][0]["text"]
        applied = next(e["data"] for e in events if e["data"].get("kind") == "deep_review_findings")
        assert [item["finding"] for item in applied["findings"]] == ["Der Satz ist als Frage formuliert."]


def test_deep_negative_verdict_without_findings_is_fail_closed():
    llm = StructuredScriptedLLM(
        [_text_turn("Bestehende Antwort.")],
        [
            {
                "complete": False,
                "grounded": True,
                "contradictions_named": True,
                "findings": [],
            }
        ],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Pruefe vollstaendig.",
                "mode": "agent_kernel",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        result = client.get(f"/v1/runs/{run['run_id']}/result").json()
        assert result["answer"] == "Bestehende Antwort."
        assert [call["schema_name"] for call in llm.structured_calls] == [
            "DeepReviewVerdict"
        ]
        events = client.get(
            f"/v1/runs/{run['run_id']}/events",
            params={"format": "json"},
        ).json()["data"]
        terminal = [
            item["data"]["text"]
            for item in events
            if item.get("data", {}).get("narration_id")
            == "kernel_deep_review"
            and item["data"].get("final")
        ]
        # The fixture question carries no resolvable language, so the review
        # line reads in the neutral fallback rather than silently in German.
        assert "Verification pass: Review unavailable, result unchecked" in terminal


def test_deep_chat_finding_with_unchanged_revision_is_rejected():
    llm = StructuredScriptedLLM(
        [_text_turn("Unveraendert.")],
        [
            {
                "complete": False,
                "grounded": True,
                "contradictions_named": True,
                "findings": [
                    {
                        "target": "chat",
                        "artifact_id": "",
                        "finding": "Ein Teil fehlt.",
                        "kind": "output_requirement",
                        "quote": "Unveraendert.",
                        "basis_ids": [],
                    }
                ],
            },
            {"edits": [{"finding_id": "finding_1", "replacement": "Unveraendert."}]},
        ],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Vollstaendig antworten.",
                "mode": "agent_kernel",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        result = client.get(f"/v1/runs/{run['run_id']}/result").json()
        assert result["answer"] == "Unveraendert."
        events = client.get(
            f"/v1/runs/{run['run_id']}/events",
            params={"format": "json"},
        ).json()["data"]
        # The fixture question resolves to the neutral fallback language.
        assert any(
            item.get("data", {}).get("text", "").endswith(
                "Reviewed from general knowledge, without sources · 1 finding not applied"
            )
            and item.get("data", {}).get("final")
            for item in events
        )


def test_deep_review_receives_effective_source_policy():
    llm = StructuredScriptedLLM(
        [_text_turn("Antwort.")],
        [
            {
                "complete": True,
                "grounded": True,
                "contradictions_named": True,
                "findings": [],
            }
        ],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Nur intern.",
                "mode": "agent_kernel",
                "source_policy": {
                    "web": "disabled",
                    "knowledge": "available",
                },
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        prompt = llm.structured_calls[0]["prompt"]
        assert '"web": "disabled"' in prompt
        assert '"knowledge": "available"' in prompt


FORMAT_QUESTION = "Soll der Bericht als Tabelle oder als Aufzählung kommen?"


def _format_question_turn() -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(
                id="call_ask_format",
                name="ask_user",
                arguments={
                    "question": FORMAT_QUESTION,
                    "options": ["Als Tabelle", "Als Aufzählung"],
                    "default_assumption": "Der Bericht kommt als Tabelle.",
                },
            ),
        ),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def test_a_kernel_review_after_ask_user_carries_the_users_answer():
    """The review checks a step the assignment ordered (ask first) against
    the server's record of it. After an answered ask_user, the review role
    reads the question, the chosen option and the user's free text in the
    record's own fence; the revision reads the question and the option
    fenced and the free text after the record, as the user's requirement."""
    import json

    llm = StructuredScriptedLLM(
        [_format_question_turn(), _text_turn("Die Fristen im Überblick.")],
        [
            {
                "complete": False,
                "grounded": True,
                "contradictions_named": True,
                "findings": [
                    {
                        "target": "chat",
                        "artifact_id": "",
                        "finding": "Die Fristentabelle fehlt.",
                        "kind": "output_requirement",
                        "quote": "Die Fristen im Überblick.",
                        "basis_ids": [],
                    }
                ],
            },
            {"edits": [{"finding_id": "finding_1", "replacement": (
                "## Fristen\n\nDie Fristen im Überblick."
            )}]},
            unquoted("finding_1"),
        ],
    )
    client = make_client(llm)
    with client:
        run_id = client.post(
            "/v1/runs",
            json={
                "question": "Stell mir zuerst eine Rückfrage und fasse dann die Fristen zusammen.",
                "mode": "agent_kernel",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()["run_id"]
        wait_status(client, run_id, {"waiting_for_input"})
        row = client.get(f"/v1/runs/{run_id}/clarifications").json()["data"][0]
        answered = client.post(
            f"/v1/runs/{run_id}/clarifications/{row['clarification_id']}",
            json={"answers": {"q1": {"option_ids": ["q1_o2"], "text": "mit Fristentabelle"}}},
        )
        assert answered.status_code == 200, answered.text
        summary = wait_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
    review, revision = llm.structured_calls[:2]
    assert (review["schema_name"], revision["schema_name"]) == (
        REVIEW_RESULT_CALL, "DeepRevisionBundle",
    )
    entry = {
        "number": 1, "round": 1, "question": FORMAT_QUESTION,
        "chosen_options": ["Als Aufzählung"], "status": "answered", "default_assumption": "",
    }
    assert json.loads(_fenced(review["prompt"], "run-clarifications")) == [
        {**entry, "free_text": "mit Fristentabelle"},
    ]
    assert json.loads(_fenced(revision["prompt"], "run-clarifications")) == [entry]
    after_record = revision["prompt"].split('<untrusted_content source="run-clarifications">', 1)[1]
    between = after_record.split("</untrusted_content>", 1)[1].split("Output bundle so far:", 1)[0]
    assert "mit Fristentabelle" in between
    assert "<untrusted_content" not in between


def test_deep_canvas_finding_updates_the_target_with_batch_cas():
    llm = CanvasReviewLLM(
        [_canvas_turn(), _text_turn("Chat bleibt unveraendert.")],
        [],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle und pruefe ein Canvas.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-deep-canvas",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        artifacts = client.get(f"/v1/runs/{run['run_id']}/artifacts").json()["data"]
        canvas = next(item for item in artifacts if item["kind"] == "deliverable")
        detail = client.get(
            f"/v1/runs/{run['run_id']}/artifacts/{canvas['artifact_id']}"
        ).json()
        assert detail["revision"] == 2
        assert "## Unsicherheit\n\nUnsicherheit fehlt." in detail["content_markdown"]
        # The canvas travelled as an address; the role read it with its own
        # windowed reader, and the assignment arrived inside the handover.
        review_prompt = llm.structured_calls[0]["prompt"]
        assert "Unsicherheit fehlt" in review_prompt
        assert "Assignment of the run under work" in review_prompt
        # the batch revision was the ONE silent artifact write path —
        # it must now emit the shared updated payload (file chip + live
        # canvas refresh depend on it).
        events = client.get(
            f"/v1/runs/{run['run_id']}/events?format=json"
        ).json()["data"]
        batch_updates = [
            event["data"]
            for event in events
            if event["type"] == "inqtrix.agent.artifact.updated"
            and event["data"].get("artifact_id") == canvas["artifact_id"]
            and event["data"].get("revision") == 2
        ]
        assert batch_updates, "deep revision emitted no artifact.updated"
        payload = batch_updates[-1]
        assert payload["kind"] == "deliverable"
        assert payload["title"] == detail["title"]
        assert payload["from_revision"] == 1
        assert payload["lines_added"] >= 1
        assert "lines_removed" in payload


def test_deep_canvas_revision_leaves_chat_out_of_the_edit_contract():
    """Canvas findings revise only their passage, with no chat reproduction."""
    llm = CanvasReviewLLM(
        [_canvas_turn(), _text_turn("Chat bleibt unveraendert.")],
        [],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle und pruefe ein Canvas.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-deep-drift",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        result = client.get(f"/v1/runs/{run['run_id']}/result").json()
        from inqtrix.agents.phase_models import DeepRevisionBundle

        assert set(DeepRevisionBundle.model_fields) == {"edits"}
        assert result["answer"] == "Chat bleibt unveraendert."
        artifacts = client.get(f"/v1/runs/{run['run_id']}/artifacts").json()["data"]
        canvas = next(item for item in artifacts if item["kind"] == "deliverable")
        detail = client.get(
            f"/v1/runs/{run['run_id']}/artifacts/{canvas['artifact_id']}"
        ).json()
        assert detail["revision"] == 2
        assert "## Unsicherheit\n\nUnsicherheit fehlt." in detail["content_markdown"]
        events = client.get(
            f"/v1/runs/{run['run_id']}/events?format=json"
        ).json()["data"]
        terminal = [
            item["data"]["text"]
            for item in events
            if item.get("data", {}).get("narration_id") == "kernel_deep_review"
            and item["data"].get("final")
        ]
        assert terminal == [
            "Verifikations-Durchlauf: Geprüft aus Modellwissen, ohne Quellen · 1 Befund überarbeitet"
        ]


def test_deep_review_findings_are_visible_as_activity():
    """The findings themselves (text + target) reach the run stream as one
    activity row BEFORE the revision call — a narration carries only
    their count, and a failed revision must not hide what was found."""
    llm = CanvasReviewLLM(
        [_canvas_turn(), _text_turn("Chat bleibt unveraendert.")],
        [],
    )
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle und pruefe ein Canvas.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-deep-findings",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        events = client.get(
            f"/v1/runs/{run['run_id']}/events?format=json"
        ).json()["data"]
        rows = [
            item
            for item in events
            if item["type"] == "inqtrix.agent.activity"
            and item["data"].get("kind") == "deep_review_findings"
        ]
        assert len(rows) == 1
        data = rows[0]["data"]
        # The flagged passage and what is wrong with it — and NOTHING that
        # belongs to the edit machinery. Offsets and the internal finding
        # and basis ids stay inside the pass; a payload that carries them
        # invites a surface to show them to a reader.
        assert data["findings"] == [
            {
                "quote": "Unsicherheit fehlt.",
                "finding": "Canvas nennt die Unsicherheit nicht.",
                "target": llm.canvas_id,
            }
        ]
        assert data["label"] == "Verifikation: 1 Befund"
        assert data["detail"].startswith("Canvas ")
        assert data["detail"].endswith(": Canvas nennt die Unsicherheit nicht.")
        assert llm.canvas_id not in data["detail"], "title, not id, faces the user"
        assert data["scope"] == "run" and data["phase"] == "execution"
        running = next(
            index
            for index, item in enumerate(events)
            if "Überarbeitung läuft" in item.get("data", {}).get("text", "")
        )
        assert events.index(rows[0]) < running


def test_deep_store_failure_has_terminal_narration_and_preserves_outputs():
    llm = CanvasReviewLLM(
        [_canvas_turn(), _text_turn("Chat bleibt unveraendert.")],
        [],
    )
    client = make_client(llm)

    async def fail_store(**kwargs):
        del kwargs
        raise RuntimeError("store offline")

    client.container.agent_control_service.store.revise_session_artifacts_atomically = (  # type: ignore[attr-defined]
        fail_store
    )
    with client:
        run = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle und pruefe ein Canvas.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-deep-store-failure",
                "agent_overrides": {"depth": "deep"},
            },
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        result = client.get(f"/v1/runs/{run['run_id']}/result").json()
        assert result["answer"] == "Chat bleibt unveraendert."
        events = client.get(
            f"/v1/runs/{run['run_id']}/events", params={"format": "json"}
        ).json()["data"]
        assert any(
            item.get("data", {}).get("text", "")
            == "Verifikations-Durchlauf: Geprüft aus Modellwissen, ohne Quellen · 1 Befund nicht angewendet"
            and item.get("data", {}).get("final")
            for item in events
        )
        # The findings were emitted before the store failed.
        assert any(
            item["type"] == "inqtrix.agent.activity"
            and item["data"].get("kind") == "deep_review_findings"
            for item in events
        )
        # The applied finding is named after all, with the store error as its reason.
        assert [
            item["data"]["detail"] for item in events
            if item["type"] == "inqtrix.agent.activity"
            and item["data"].get("kind") == "critic_revision_discarded"
        ] == ["Befund nicht angewendet (Speicherfehler): Canvas nennt die Unsicherheit nicht."]


def _deep_chat_run(llm, question: str = "Wie ist die Lage?", **body: Any) -> tuple[Any, str]:
    client = make_client(llm)
    with client:
        run = client.post("/v1/runs", json={
            "question": question, "mode": "agent_kernel",
            "agent_overrides": {"depth": "deep"}, **body,
        }).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        events = client.get(
            f"/v1/runs/{run['run_id']}/events", params={"format": "json"}
        ).json()["data"]
    return events, run["run_id"]


def _final_review_narration(events: list[dict[str, Any]]) -> str:
    return [
        event["data"]["text"] for event in events
        if event.get("data", {}).get("narration_id") == "kernel_deep_review"
        and event["data"].get("final")
    ][-1]


def _chat_finding(quote: str) -> dict[str, Any]:
    return {"target": "chat", "artifact_id": "", "finding": f"{quote} gliedern.",
            "kind": "output_requirement", "quote": quote, "basis_ids": []}


WARRANTY_EXCERPT = "Die Garantie gilt fünf Jahre bei Registrierung."
"""The stored text of the one source of :func:`test_the_kernel_review_narration_is_the_records_summary`."""


def test_the_kernel_review_narration_is_the_records_summary():
    """The final review narration says what the run's outcome record says:
    the answer's one statement and its state. The record's details count
    what the review did: only the edit the re-check held is "behoben", the
    other applied edit is revised and not re-checked, and the named finding
    is counted, not swallowed. The held edit rests on a source the role
    re-read, and its row quotes that source's text; the heading cites none,
    so it stays unverified."""
    seen: dict[str, str] = {}

    def read_the_source(prompt: str):
        [seen["id"]] = _reference_ids(prompt)
        return ("read_evidence", {"reference_id": seen["id"]})

    sourced = {
        "finding_id": "finding_1",
        "rows": [{
            "segment_id": "s1",
            "basis": [{"label": "W1", "quote": "fünf Jahre bei Registrierung"}],
            "force": "same", "bound": "same", "actor_reach": "same",
            "reservations": "same", "links": "same",
            "presents_as": "fact", "basis_shows": "fact",
            "already_in_replaced_text": False,
            "before": "change_asked", "restated_in": "",
            "finding_quote": "Bedingung der Registrierung fehlt",
        }],
        "fixes_named_defect": "yes",
        "reason": "The source states the condition the replacement adds.",
    }
    # The check and its confirmation answer alike: a hold takes both.
    llm = StructuredScriptedLLM([], [
        {"edits": [
            {"finding_id": "finding_1",
             "replacement": "Fünf Jahre Garantie bei Registrierung [W1]."},
            {"finding_id": "finding_2", "replacement": "## B\n\nBeta."},
        ]},
        *[{"findings": [sourced, *unquoted("finding_2")["findings"]]}] * 2,
    ])
    llm.role_reads = [lambda prompt: ("read_evidence", {}), read_the_source]
    llm.review_verdict = lambda prompt: {
        "complete": False, "grounded": True, "contradictions_named": True,
        "findings": [
            {"target": "chat", "artifact_id": "",
             "finding": "Die Bedingung der Registrierung fehlt.",
             "kind": "evidence_error", "quote": "Fünf Jahre Garantie [W1].",
             "basis_ids": ["source:" + seen["id"]]},
            _chat_finding("Beta."),
            _chat_finding("Gamma."),
        ],
    }
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre bei Registrierung [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty",
                            "excerpt": WARRANTY_EXCERPT}],
            "web_search_ledger": {"searches": {}},
        }, "Fünf Jahre Garantie [W1].\n\nBeta.\n\nGamma.\n\nGamma.", "sess-deep-narration")
        events = client.get(
            f"/v1/runs/{run_id}/events", params={"format": "json"}
        ).json()["data"]
    [record] = [event["data"] for event in events if event["type"] == "inqtrix.agent.outcome"]
    assert (_final_review_narration(events), record["summary"], record["details"]) == (
        t({"language": "de"}, "kernel_review_prefix", text=record["summary"]),
        "Geprüft · 1 Aussage: 1 mit Quelltext",
        "1 Befund behoben und nachgeprüft · 1 Befund überarbeitet, nicht "
        "nachgeprüft (1 ohne eindeutiges Prüfergebnis) · 1 Befund nicht angewendet · "
        "1 von 1 zitierten Beleg nachgelesen",
    )


def test_a_passing_review_with_unchecked_points_names_them():
    """A verdict without findings that left points unchecked has not passed
    everything: the narration names the points instead of saying the
    answer stands."""
    llm = StructuredScriptedLLM([_text_turn("Die Lage ist ruhig.")], [
        {"complete": True, "grounded": True, "contradictions_named": True, "findings": [],
         "not_checked": ["Quelle A nicht gelesen.", "Quelle B nicht gelesen."]},
    ])
    events, _ = _deep_chat_run(llm)
    assert _final_review_narration(events) == (
        "Verifikations-Durchlauf: Geprüft aus Modellwissen, ohne Quellen · 2 Punkte nicht geprüft"
    )


def _unapplied_details(events: list[dict[str, Any]]) -> list[str]:
    return [
        event["data"]["detail"] for event in events
        if event["type"] == "inqtrix.agent.activity"
        and event["data"].get("kind") == "critic_revision_discarded"
    ]


def test_findings_that_cannot_be_located_are_narrated_as_open():
    """"Als offen vermerkt" holds: the activity beside it names the finding."""
    llm = StructuredScriptedLLM([_text_turn("Erste Antwort.")], [
        {"complete": False, "grounded": True, "contradictions_named": True,
         "findings": [_chat_finding("Diesen Satz gibt es nicht.")]},
    ])
    events, _ = _deep_chat_run(llm)
    assert (_final_review_narration(events), _unapplied_details(events)) == (
        "Verifikations-Durchlauf: Geprüft aus Modellwissen, ohne Quellen · 1 Befund nicht angewendet",
        ["Befund nicht angewendet (zitierte Stelle steht nicht im Text): "
         "Diesen Satz gibt es nicht. gliedern."],
    )


def test_a_revision_that_breaks_the_rules_still_says_what_was_named_before_it():
    """The finding named at locate time is counted with the one the
    revision's rules named, and both are listed in the verdict's order."""
    llm = StructuredScriptedLLM([_text_turn("Alpha.\n\nGamma.\n\nGamma.")], [
        {"complete": False, "grounded": True, "contradictions_named": True,
         "findings": [_chat_finding("Alpha."), _chat_finding("Gamma.")]},
        {"edits": [{"finding_id": "finding_1", "replacement": "Alpha."}]},
    ])
    events, _ = _deep_chat_run(llm)
    assert (_final_review_narration(events), _unapplied_details(events)) == (
        "Verifikations-Durchlauf: Geprüft aus Modellwissen, ohne Quellen · 2 Befunde nicht angewendet",
        ["Befund nicht angewendet (Überarbeitung ließ die Stelle unverändert): Alpha. gliedern.; "
         "Befund nicht angewendet (zitierte Stelle steht mehrfach im Text): Gamma. gliedern."],
    )


class _CanvasNamedChatAppliedLLM(CanvasReviewLLM):
    """A canvas finding whose edit deletes its passage, refused whatever the
    run holds, and a chat finding whose edit inserts a heading."""

    edits = (
        {"finding_id": "finding_1", "replacement": ""},
        {"finding_id": "finding_2", "replacement": "## Chat\n\nChat bleibt unveraendert."},
    )
    checks = tuple(unquoted("finding_2")["findings"])

    def review_verdict(self, prompt: str) -> Any:
        verdict = super().review_verdict(prompt)
        verdict["findings"].append(_chat_finding("Chat bleibt unveraendert."))
        return verdict


def test_a_canvas_without_an_applied_edit_is_not_rewritten():
    """An unchanged canvas written anyway becomes a new revision with a chip
    for nothing."""
    llm = _CanvasNamedChatAppliedLLM([_canvas_turn(), _text_turn("Chat bleibt unveraendert.")], [])
    client = make_client(llm)
    with client:
        run = client.post("/v1/runs", json={
            "question": "Erstelle und pruefe ein Canvas.", "mode": "agent_kernel",
            "autonomy": "autonomous", "session_id": "sess-deep-canvas-named",
            "agent_overrides": {"depth": "deep"},
        }).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        canvas = next(
            item for item in client.get(f"/v1/runs/{run['run_id']}/artifacts").json()["data"]
            if item["kind"] == "deliverable"
        )
        detail = client.get(f"/v1/runs/{run['run_id']}/artifacts/{canvas['artifact_id']}").json()
        events = client.get(f"/v1/runs/{run['run_id']}/events?format=json").json()["data"]
        answer = client.get(f"/v1/runs/{run['run_id']}/result").json()["answer"]
    assert answer == "## Chat\n\nChat bleibt unveraendert.", "precondition: the chat edit applied"
    assert (detail["revision"], [
        event for event in events
        if event["type"] == "inqtrix.agent.artifact.updated"
        and event["data"].get("artifact_id") == canvas["artifact_id"]
        and event["data"].get("revision", 0) > 1
    ]) == (1, [])


class _KeptAndRejectedCanvasLLM(CanvasReviewLLM):
    """Two missing headings on the canvas. The first edit adds its heading
    and stays unverified; the second adds its heading but drops a word of
    its passage, a removal its finding does not ask for, and the re-check
    rejects it."""

    edits = (
        {"finding_id": "finding_1", "replacement": "## Unsicherheit\n\nUnsicherheit fehlt."},
        {"finding_id": "finding_2", "replacement": "## Risiko\n\nRisiko fehlt."},
    )
    checks = (
        *unquoted("finding_1")["findings"],
        {"finding_id": "finding_2",
         "rows": [{**unquoted("finding_2")["findings"][0]["rows"][0], "before": "not_asked"}],
         "fixes_named_defect": "yes", "reason": "A word is gone and nothing asks for it."},
    )

    def review_verdict(self, prompt: str) -> Any:
        verdict = super().review_verdict(prompt)
        verdict["findings"].append({
            "target": "artifact", "artifact_id": self.canvas_id,
            "finding": "Canvas nennt das Risiko nicht.", "kind": "output_requirement",
            "quote": "Risiko fehlt völlig.", "basis_ids": [],
        })
        return verdict


def test_a_canvas_keeps_the_edit_the_recheck_rejects():
    """The re-check can only confirm an edit, never take one back: the
    canvas is written once, with both applied edits, the rejected one
    included, and the run's outcome counts both as revised, not
    re-checked. The run reads a report first, so its review answers to
    evidence and the re-check runs."""
    llm = _KeptAndRejectedCanvasLLM([], [])
    client = make_client(llm)
    with client:
        origin = _seeded_report(client, {
            "answer": "Das Risiko ist offen [E1].",
            "references": [{"label": "E1", "url": "https://example.org/risiko"}],
        })
        llm._turns = [
            _report_read_turn(origin),
            _canvas_turn("# Canvas\n\nUnsicherheit fehlt.\n\nRisiko fehlt völlig."),
            _text_turn("Chat bleibt unveraendert."),
        ]
        run = client.post("/v1/runs", json={
            "question": "Erstelle und pruefe ein Canvas.", "mode": "agent_kernel",
            "autonomy": "autonomous", "session_id": "sess-deep-canvas-recheck",
            "report_ids": [origin], "agent_overrides": {"depth": "deep"},
        }).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        canvas = next(
            item for item in client.get(f"/v1/runs/{run['run_id']}/artifacts").json()["data"]
            if item["kind"] == "deliverable"
        )
        detail = client.get(f"/v1/runs/{run['run_id']}/artifacts/{canvas['artifact_id']}").json()
        events = client.get(
            f"/v1/runs/{run['run_id']}/events", params={"format": "json"}
        ).json()["data"]
    [record] = [event["data"] for event in events if event["type"] == "inqtrix.agent.outcome"]
    assert (
        detail["revision"], detail["content_markdown"],
        record["revision"], record["applied"], record["verified"],
    ) == (
        2, "# Canvas\n\n## Unsicherheit\n\nUnsicherheit fehlt.\n\n## Risiko\n\nRisiko fehlt.",
        "unverified", 2, 0,
    )


def test_normal_run_never_touches_the_review_path():
    llm = StructuredScriptedLLM([_text_turn("Direkte Antwort.")], [])
    client = make_client(llm)
    with client:
        run = client.post(
            "/v1/runs",
            json={"question": "Frage?", "mode": "agent_kernel"},
        ).json()
        summary = wait_status(client, run["run_id"], {"completed", "failed"})
        assert summary["status"] == "completed", summary
        assert llm.structured_calls == []
        first_user = str(llm.chat_calls[0]["messages"])
        assert "Deep mode" not in first_user


def test_deep_prompt_line_and_user_message_composition():
    deep = build_kernel_user_message("Auftrag.", depth="deep")
    assert "Deep mode" in deep
    assert "run_deep_mission" in deep
    normal = build_kernel_user_message("Auftrag.")
    assert "Deep mode" not in normal
    assert "clarify doubtful statements with read_evidence" in deep
    assert "A word-count or format requirement is no research need" in deep
    assert "After a mission" not in normal


def test_tool_discipline_names_the_mission_memo_as_the_deliverable():
    """The doctrine names a finished mission's memo the deliverable, so
    the kernel does not re-synthesize it into its own canvas or verify it
    with a second research child."""
    from inqtrix.agents.prompts import build_agent_kernel_system_prompt

    prompt = build_agent_kernel_system_prompt()
    assert "The memo of a completed mission is the deliverable" in prompt
    assert "Read existing evidence with read_evidence" in prompt


def _deps_without_children(run_id: str = "run_deep") -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        run_service=SimpleNamespace(
            run_store=SimpleNamespace(children=lambda _run_id: [])
        ),
    )


def test_deep_verify_reviews_the_memo_of_a_completed_mission_child():
    """The mission's memo is the deliverable of a delegated run: the
    verification bundle carries the run's own canvases first, then the
    run's memo and the memo of every COMPLETED mission child — never a
    research child's or a failed mission's — each document once."""
    listed: list[tuple[str, str]] = []
    catalog = {
        ("run_deep", "deliverable"): ["art_canvas"],
        ("run_deep", "memo"): ["art_canvas"],  # same id → listed once
        ("run_child_mission", "memo"): ["art_sess_memo"],
        ("run_child_research", "memo"): ["art_never"],
        ("run_child_failed", "memo"): ["art_never_failed"],
    }

    class CatalogControl:
        async def list_artifacts(self, run_id, *, kind, limit, after=None):
            listed.append((run_id, kind))
            ids = catalog.get((run_id, kind), [])
            return [SimpleNamespace(artifact_id=i) for i in ids], None

        async def get_artifact(self, run_id, artifact_id):
            return SimpleNamespace(artifact_id=artifact_id, run_id=run_id), []

    children = [
        {"run_id": "run_child_mission", "mode": "workspace_agent", "status": "completed"},
        {"run_id": "run_child_research", "mode": "research", "status": "completed"},
        {"run_id": "run_child_failed", "mode": "workspace_agent", "status": "failed"},
    ]
    deps = SimpleNamespace(
        run_id="run_deep",
        run_service=SimpleNamespace(
            run_store=SimpleNamespace(children=lambda run_id: list(children))
        ),
    )
    algorithm = object.__new__(KernelAgentAlgorithm)
    algorithm._control = CatalogControl()

    canvases = algorithm._current_run_canvases(deps)

    assert [c.artifact_id for c in canvases] == ["art_canvas", "art_sess_memo"]
    assert ("run_child_research", "memo") not in listed
    assert ("run_child_failed", "memo") not in listed


def test_deep_canvas_bundle_paginates_beyond_fifty_outputs():
    rows = [SimpleNamespace(artifact_id=f"art_{index}") for index in range(51)]

    class PagedControl:
        async def list_artifacts(self, run_id, *, kind, limit, after=None):
            assert run_id == "run_deep"
            assert limit == 50
            if kind == "memo":
                return [], None
            assert kind == "deliverable"
            if after is None:
                return rows[:50], encode_cursor(1.0, "art_49")
            assert after == (1.0, "art_49")
            return rows[50:], None

        async def get_artifact(self, run_id, artifact_id):
            assert run_id == "run_deep"
            return SimpleNamespace(artifact_id=artifact_id), []

    algorithm = object.__new__(KernelAgentAlgorithm)
    algorithm._control = PagedControl()
    canvases = algorithm._current_run_canvases(_deps_without_children())

    assert [item.artifact_id for item in canvases] == [
        f"art_{index}" for index in range(51)
    ]


def test_deep_child_research_uses_the_deep_wire_profile(monkeypatch):
    """Deep forces the DEEP report profile on child research — as the
    LITERAL ReportProfile value ('deep'); the German label 'gruendlich'
    would 400 the child resolve and fail the whole run."""
    from tests.agents.test_kernel_child_runs import (
        ScriptedToolLLM as ChildScriptedLLM,
        _text_turn as child_text_turn,
        _tool_turn as child_tool_turn,
        make_client as make_child_client,
        wait_status as wait_child_status,
    )

    llm = ChildScriptedLLM(
        [
            child_tool_turn(
                "call_deepchild",
                "run_web_research",
                {"question": "Marktlage 2026"},
            ),
            child_text_turn("Fertig auf Basis des Kindberichts."),
        ]
    )
    client = make_child_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Analysiere die Marktlage gruendlich.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "session_id": "sess-deep-child",
                "agent_overrides": {"depth": "deep"},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        summary = wait_child_status(client, run_id, {"completed", "failed"})
        assert summary["status"] == "completed", summary
        children = [
            child
            for child in client.get(f"/v1/runs/{run_id}/children").json()["data"]
            if child["mode"] == "research"
        ]
        assert len(children) == 1
        assert children[0]["agent_overrides"]["report_profile"] == "deep"


def test_tier_prompt_sections_spell_out_the_ladder():
    """Gruendlich names its rungs and the missing mission; schnell keeps
    the chat answer but allows a document on explicit request."""
    gruendlich = build_kernel_user_message("Auftrag.", tier="gruendlich")
    assert "Tier: gruendlich (thorough) — the middle of the ladder" in gruendlich
    assert (
        "There is no mission (run_deep_mission) in this tier"
        in gruendlich
    )
    schnell = build_kernel_user_message("Auftrag.", tier="schnell")
    assert "when the user explicitly asks for a file" in schnell
    assert "middle of the ladder" not in schnell
    # Retained evidence is index-first; the doctrine bounds the reads.
    assert "read it at most once, and only when the question needs it" in schnell
    assert "at most ONE knowledge search" in schnell
    assert "read it at most once" not in gruendlich
    assert "middle of the ladder" not in build_kernel_user_message("Auftrag.")


def test_todo_doctrine_puts_write_todos_in_the_same_turn_as_the_work_tool():
    """A measured middle-rung report spent 5 of 12 model turns on
    ``write_todos`` alone (8 super-steps each). The doctrine says: the
    todo update rides along with the next tool call — several tool calls
    of one turn run in parallel — never as a turn of its own."""
    from inqtrix.agents.prompts import build_agent_kernel_system_prompt

    prompt = build_agent_kernel_system_prompt()
    assert "in the SAME turn as the next tool call" in prompt
    assert "a turn only for write_todos costs a whole model turn" in prompt
    # Without this rule the model delegates while "Umfang festlegen" stays
    # the in-progress item for the whole delegation — the list names the
    # wrong step as current.
    assert "NOT the preparation item before it" in prompt
    assert "that one is done with the assignment text" in prompt


def test_normal_step_allowance_buys_fourteen_tool_turns():
    """Normal and deep buy the same 14 tool turns: a Gruendlich report
    with one research child, one follow-up search, todo bookkeeping and a
    document needs 12, and a ceiling of 8 would park it for an extension
    decision. Deep buys more tool calls and the verification pass
    instead."""
    from inqtrix.agents.kernel.algorithm import (
        _ANSWER_TURN_SUPERSTEPS,
        _SUPERSTEPS_PER_TOOL_TURN,
    )
    from inqtrix.settings import AgentPlatformSettings

    platform = AgentPlatformSettings()
    assert platform.kernel_max_iterations == (
        _ANSWER_TURN_SUPERSTEPS + 14 * _SUPERSTEPS_PER_TOOL_TURN
    )
    assert platform.kernel_max_iterations == platform.kernel_max_iterations_deep
    assert platform.kernel_max_iterations_extension_ceiling == (
        platform.kernel_max_iterations_extension_ceiling_deep
    )
    assert platform.kernel_max_tool_calls < platform.kernel_max_tool_calls_deep


def test_an_explicit_output_form_is_the_only_form_rule_the_kernel_states():
    """One output-form rule reaches the model, never two.

    The user's explicit choice is stated as overriding every routing
    criterion; a tier line that also states its own form default puts two
    contradicting server instructions into the same message and leaves the
    choice to the model: a canvas the user chose can come back as a chat
    answer. The precedence is the mission machine's: an explicit choice
    first, the tier's default only without one.
    """
    for tier in ("schnell", "gruendlich"):
        for form in ("canvas", "chat"):
            message = build_kernel_user_message("Auftrag.", tier=tier, response_form=form)
            assert "The user demands the output form" in message
            assert "answer in the chat; write a canvas document" not in message
            assert "You write a canvas document when" not in message
    # Without an explicit choice the tier's default stands, in both tiers.
    for form in ("", "auto"):
        assert "answer in the chat; write a canvas document only when" in (
            build_kernel_user_message("Auftrag.", tier="schnell", response_form=form)
        )
        assert "You write a canvas document when the user asks" in (
            build_kernel_user_message("Auftrag.", tier="gruendlich", response_form=form)
        )


# -- the reader reads the answer while the review runs ------------------------ #


PASSING_VERDICT = {"complete": True, "grounded": True, "contradictions_named": True, "findings": []}


def _drafts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["data"] for event in events if event["type"] == ANSWER_DRAFT_EVENT]


def _published(events: list[dict[str, Any]]) -> str:
    return "".join(
        event["data"]["delta"] for event in events
        if event["type"] == "inqtrix.output_text.delta"
    )


def test_the_reader_reads_the_draft_while_the_review_runs():
    """The answer is shown before the review starts, as one durable event.
    The revision is published as the answer and leaves the draft its own
    text, so the reader can see what the review changed."""
    llm = StructuredScriptedLLM([_text_turn("Erste Antwort.")], [
        {"complete": False, "grounded": True, "contradictions_named": True,
         "findings": [_chat_finding("Erste Antwort.")]},
        {"edits": [{"finding_id": "finding_1", "replacement": "## Lage\n\nErste Antwort."}]},
    ])
    events, run_id = _deep_chat_run(llm)
    kinds = [event["type"] for event in events]
    review_starts = next(
        index for index, event in enumerate(events)
        if event.get("data", {}).get("narration_id") == "kernel_deep_review"
    )
    assert (_drafts(events), _published(events)) == (
        [{"artifact_id": answer_artifact_id(run_id), "draft_id": answer_draft_id(run_id),
          "markdown": "Erste Antwort.", "reference_labels": []}],
        "## Lage\n\nErste Antwort.",
    )
    assert kinds.index(ANSWER_DRAFT_EVENT) < review_starts < kinds.index("inqtrix.answer.started")


def test_a_draft_the_review_passes_is_the_published_answer():
    """Without a finding the published answer is the draft, and both name
    the labels the text cites, so its citations render alike."""
    llm = StructuredScriptedLLM([], [dict(PASSING_VERDICT)])
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre bei Registrierung [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty",
                            "excerpt": WARRANTY_EXCERPT}],
            "web_search_ledger": {"searches": {}},
        }, "Fünf Jahre Garantie [W1].", "sess-deep-draft-passes")
        events = client.get(
            f"/v1/runs/{run_id}/events", params={"format": "json"}
        ).json()["data"]
    [draft] = _drafts(events)
    [started] = [event["data"] for event in events if event["type"] == "inqtrix.answer.started"]
    assert (draft["markdown"], draft["reference_labels"]) == (
        _published(events), started["reference_labels"],
    ) == ("Fünf Jahre Garantie [W1].", ["W1"])


def test_a_stufe_that_reviews_nothing_shows_no_draft():
    """Without a review the answer is published as it is; a draft would
    announce a check that never comes."""
    llm = StructuredScriptedLLM([_text_turn("Erste Antwort.")], [])
    events, _ = _deep_chat_run(llm, agent_overrides={"agent_tier": "schnell"})
    assert (_drafts(events), _published(events)) == ([], "Erste Antwort.")


def test_a_draft_that_fails_its_citation_check_is_not_shown(monkeypatch, caplog):
    """A draft whose check could not remove an internal identifier is held
    back, and the log says so; the reviewed answer takes today's path
    through the same check."""
    from inqtrix.agents.kernel import algorithm as kernel_algorithm
    from inqtrix.agents.report_quality import CitationValidationFailed

    checked: list[str] = []
    validate = kernel_algorithm._validate_kernel_answer_citations

    def fail_the_first(deps: Any, answer: str, ledger: list[dict[str, Any]]) -> str:
        checked.append(answer)
        if len(checked) == 1:
            raise CitationValidationFailed("An identifier stays.", usage={})
        return validate(deps, answer, ledger)

    monkeypatch.setattr(kernel_algorithm, "_validate_kernel_answer_citations", fail_the_first)
    llm = StructuredScriptedLLM([_text_turn("Erste Antwort.")], [dict(PASSING_VERDICT)])
    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            events, _ = _deep_chat_run(llm)
    finally:
        logger.removeHandler(caplog.handler)
    assert (_drafts(events), _published(events), checked) == (
        [], "Erste Antwort.", ["Erste Antwort.", "Erste Antwort."],
    )
    assert any("answer draft is not shown" in record.getMessage() for record in caplog.records)


def test_the_draft_crosses_the_event_boundary_whole_however_long():
    """A draft is shown whole or not at all: the boundary's bound for audit
    prose does not cut it, and its allowlist keeps every other field out."""
    payload = {"artifact_id": "art_x_answer", "draft_id": "drf_x_answer",
               "markdown": "Satz. " * 6000, "reference_labels": ["W1"]}
    _, [(event_type, stored)] = expand_run_event(
        ANSWER_DRAFT_EVENT, {**payload, "usage": {"prompt_tokens": 1}}, status="running",
    )
    assert (event_type, stored) == (ANSWER_DRAFT_EVENT, payload)


def test_the_draft_is_shown_after_its_citation_check():
    """A label no source carries reaches the reader not even in the draft:
    the draft passes the published answer's check, and without a finding
    the two are one text."""
    repaired = {"markdown": "Erste Antwort."}
    llm = StructuredScriptedLLM([_text_turn("Erste Antwort [K9].")], [
        dict(repaired), dict(PASSING_VERDICT), dict(repaired),
    ])
    events, _ = _deep_chat_run(llm)
    [draft] = _drafts(events)
    assert ("[K9]" in draft["markdown"], draft["markdown"]) == (False, _published(events))


WARRANTY_LEDGER = {"searches": {"query_warranty": {
    "query_id": "query_warranty", "source_run_id": "",
    "query": "Garantiebedingungen?", "provider_answer": "Garantie bei Registrierung.",
    "citations": [{"citation_id": "citation_warranty", "url": "https://example.org/warranty",
                   "grounded_support": "Garantie bei Registrierung."}],
}}}


def test_the_record_counts_the_answer_its_citation_check_publishes(monkeypatch):
    """The statements are counted on the answer as published: a citation
    check after the review that changes the answer changes the count."""
    from inqtrix.agents.kernel import algorithm as kernel_algorithm

    checked: list[str] = []
    original = kernel_algorithm._validate_kernel_answer_citations

    def check(deps: Any, answer: str, ledger: Any) -> str:
        checked.append(answer)
        result = original(deps, answer, ledger)
        # Only the check of the reviewed answer adds a cited sentence.
        return result + "\n\nDie Garantie gilt weltweit [W1]." if len(checked) > 1 else result

    monkeypatch.setattr(kernel_algorithm, "_validate_kernel_answer_citations", check)
    llm = StructuredScriptedLLM([], [
        {"complete": True, "grounded": True, "contradictions_named": True, "findings": []},
    ])
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre bei Registrierung [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty"}],
            "web_search_ledger": WARRANTY_LEDGER,
        }, "Fünf Jahre Garantie [W1].", "sess-deep-published")
        events = client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]
    [record] = [item["data"] for item in events if item["type"] == "inqtrix.agent.outcome"]
    assert (len(checked), record["claims"]["chat"]["total"]) == (2, 2)


def test_the_record_counts_each_canvas_as_the_revision_left_it():
    """A canvas the revision wrote counts as written, every other canvas as
    the review read it: an unchanged canvas is not written again."""
    from inqtrix.agents.kernel import algorithm as kernel_algorithm

    assert kernel_algorithm._written_outputs(
        {"chat": "alt", "art_1": "alt 1", "art_2": "alt 2"},
        {"chat": "neu", "art_1": "neu 1", "art_2": "neu 2"},
        {"art_1"},
    ) == {"chat": "neu", "art_1": "neu 1", "art_2": "alt 2"}


def test_a_finding_only_the_providers_summary_could_check_marks_the_answer_without_a_revision():
    """The kernel asks no revision for a passage its review could only
    measure against the search provider's summary: the answer stays as
    written, the passage is listed as marked, and no revision is discarded.
    The outcome record counts the answer's statement as insufficiently
    supported and marks its passage in the published text."""
    def reference_id(prompt: str) -> str:
        match = re.search(r'"reference_id": "(ref_[0-9a-f]+)"', prompt)
        assert match, prompt
        return match.group(1)

    llm = StructuredScriptedLLM([], [])
    llm.role_reads = [
        lambda prompt: ("read_evidence", {}),
        lambda prompt: ("read_evidence", {"reference_id": reference_id(prompt)}),
    ]
    llm.review_verdict = lambda prompt: {
        "complete": True, "grounded": False, "contradictions_named": True,
        "findings": [{"target": "chat", "artifact_id": "", "finding": "W1 nennt keine Dauer.",
                      "kind": "evidence_error", "quote": "Fünf Jahre Garantie [W1].",
                      "basis_ids": ["source:" + reference_id(prompt)]}],
    }
    client = make_client(llm)
    with client:
        run_id = _deep_run_over_a_report(client, llm, {
            "answer": "Fünf Jahre bei Registrierung [E1].",
            "references": [{"label": "E1", "url": "https://example.org/warranty"}],
            "web_search_ledger": {"searches": {"query_warranty": {
                "query_id": "query_warranty", "source_run_id": "",
                "query": "Garantiebedingungen?", "provider_answer": "Garantie bei Registrierung.",
                "citations": [{"citation_id": "citation_warranty", "url": "https://example.org/warranty",
                               "grounded_support": "Garantie bei Registrierung."}],
            }}},
        }, "Fünf Jahre Garantie [W1].", "sess-deep-marked")
        assert client.get(f"/v1/runs/{run_id}/result").json()["answer"] == "Fünf Jahre Garantie [W1]."
        events = client.get(f"/v1/runs/{run_id}/events", params={"format": "json"}).json()["data"]
    kinds = [item["data"].get("kind") for item in events if item["type"] == "inqtrix.agent.activity"]
    assert "critic_revision_discarded" not in kinds
    [listed] = [
        item["data"] for item in events
        if item["type"] == "inqtrix.agent.activity" and item["data"].get("kind") == "deep_review_findings"
    ]
    assert [entry.get("marked") for entry in listed["findings"]] == [True]
    assert "DeepRevisionBundle" not in [call["schema_name"] for call in llm.structured_calls]
    [record] = [item["data"] for item in events if item["type"] == "inqtrix.agent.outcome"]
    assert record["claims"] == {"chat": {
        "total": 1, "verified": 0, "source_text": 0, "attributed": 0, "insufficient": 1,
    }}
    assert [(mark["quote"], mark["reason"]) for mark in record["marks"]] == [
        ("Fünf Jahre Garantie [W1].", "finding"),
    ]
