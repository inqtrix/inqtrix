"""Agent-kernel evaluation harness.

Runs kernel scenarios through the REAL serving path (`/v1/runs` on a
`register_routes` app — the same seam every kernel platform test uses)
and collects a full trial record: answer, references, artifacts, tool
calls, approvals, narrations, token usage, latency. Grading is
CODE-FIRST (citation resolution, tool routing, policy conformance);
an LLM judge is deliberately not part of this tier — subjective quality
grading needs a pinned cross-family judge and lives with the gated live
eval, never in the offline default suite.

Reliability is reported as ``pass^k`` (ALL k trials succeed), not just
``pass@k`` — a kernel that answers correctly four times out of five is
a different product from one that answers correctly every time.

The harness is provider-agnostic: CI runs it with scripted providers
(deterministic — it pins the HARNESS CONTRACT and the graders), the
gated live eval runs the same scenarios against real providers.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from math import comb
from pathlib import Path
from typing import Any, Callable

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"

_CITATION_LABEL = re.compile(r"\[([KW]\d+)\]")


@dataclass(frozen=True)
class KernelScenario:
    """One versioned German eval scenario for the kernel.

    Attributes:
        scenario_id: Stable id (baseline keys and artifacts reference it).
        question: The German user assignment.
        autonomy: Wire autonomy mode for the run.
        tool_directives: Admitted tool directives (e.g. ``web_research``).
        expect_citations: The answer must cite at least one resolvable
            ``[K#]/[W#]`` label.
        expected_tools: Tool names the run MUST have started (subset
            match against ``tool.started`` events).
        forbidden_tools: Tool names the run must NOT have started
            (policy conformance, e.g. children in a quick run).
        expect_gate: The run must park on a ``kind='tool'`` approval
            before any gated tool executes (strict/balanced policy).
        expected_gate_tool: When set, the parked approval must gate
            exactly this tool (identity check — a count-only gate grade
            cannot tell an approval for the right tool from the wrong
            one, nor that the tool did not execute before consent).
    """

    scenario_id: str
    question: str
    autonomy: str = "autonomous"
    tool_directives: tuple[str, ...] = ()
    expect_citations: bool = False
    expected_tools: frozenset[str] = frozenset()
    forbidden_tools: frozenset[str] = frozenset()
    expect_gate: bool = False
    expected_gate_tool: str = ""


@dataclass
class KernelTrialRecord:
    """Everything one kernel trial produced, ready for grading."""

    scenario_id: str
    trial: int
    status: str
    answer: str
    cited_labels: list[str]
    reference_labels: list[str]
    artifact_kinds: list[str]
    tools_started: list[str]
    approvals: list[dict[str, Any]]
    gated_tools: list[str]
    """Tool names named in the parked approvals' actions (gate identity)."""
    premature_tools: list[str]
    """Gated tools whose ``tool.started`` preceded the gate — a consent
    breach (a gated tool must never execute before its approval)."""
    narrations: list[str]
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    failures: list[str] = field(default_factory=list)
    """Grader verdicts: empty = the trial passes."""


def run_kernel_trial(
    client: Any,
    scenario: KernelScenario,
    *,
    trial: int,
    decide_gates: bool = True,
    timeout_s: float = 120.0,
) -> KernelTrialRecord:
    """Execute one scenario through ``/v1/runs`` and collect the record.

    ``decide_gates`` auto-approves pending tool gates (the eval measures
    outcome quality, not the human); ``expect_gate`` scenarios record
    the gate BEFORE approving, so policy conformance stays gradable.
    """
    body: dict[str, Any] = {
        "question": scenario.question,
        "mode": "agent_kernel",
        "autonomy": scenario.autonomy,
    }
    if scenario.tool_directives:
        body["tool_directives"] = list(scenario.tool_directives)
    started = time.monotonic()
    response = client.post("/v1/runs", json=body)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]

    seen_approvals: list[dict[str, Any]] = []
    deadline = started + timeout_s
    status = "unknown"
    while time.monotonic() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        status = summary["status"]
        if status in ("completed", "failed", "cancelled"):
            break
        if status == "waiting_for_approval" and decide_gates:
            rows = client.get(
                f"/v1/runs/{run_id}/approvals"
            ).json()["data"]
            for row in rows:
                if row["status"] != "pending":
                    continue
                seen_approvals.append(row)
                client.post(
                    f"/v1/runs/{run_id}/approvals/{row['approval_id']}",
                    json={"decision": "approve"},
                )
        elif status == "waiting_for_input" and decide_gates:
            # A run may also park on a clarification (ask_user). The eval
            # measures outcome, not the human, so answer with the model's
            # own default assumption (the first option, else free text) —
            # otherwise the run would hang to timeout and mis-read as a
            # failure rather than exercising the post-clarification path.
            rows = client.get(
                f"/v1/runs/{run_id}/clarifications"
            ).json()["data"]
            for row in rows:
                if row["status"] != "pending":
                    continue
                options = row.get("options") or []
                if options:
                    body = {"option_id": options[0].get("id") or ""}
                else:
                    body = {
                        "answer": row.get("default_assumption")
                        or "Bitte mit der besten Annahme fortfahren."
                    }
                client.post(
                    f"/v1/runs/{run_id}/clarifications/{row['clarification_id']}",
                    json=body,
                )
        time.sleep(0.05)
    latency = time.monotonic() - started

    result = client.get(f"/v1/runs/{run_id}/result").json()
    answer = str(result.get("answer") or "")
    references = result.get("references") or []
    usage = result.get("usage") or {}
    artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
    events = client.get(
        f"/v1/runs/{run_id}/events?format=json"
    ).json()["data"]
    tools_started = [
        str(e["data"].get("tool") or "")
        for e in events
        if e["type"] == "inqtrix.agent.tool.started"
    ]
    narrations = [
        str(e["data"].get("text") or "")
        for e in events
        if e["type"] == "inqtrix.agent.narration"
    ]
    # Gate identity + consent ordering, both from the ONE ordered event
    # stream: which tools the parked approvals actually gated, and
    # whether any gated tool actually EXECUTED before its approval was
    # requested (a consent breach the count-only grade cannot see).
    # ``tool.finished`` — not ``tool.started`` — is the execution signal:
    # the gate boundary emits a ``tool.started`` and then PARKS, so a
    # started-but-parked tool never reaches ``tool.finished`` until after
    # consent. Keying off ``started`` would false-positive on every gate.
    gated_tools = sorted(
        {
            str(action.get("tool") or "")
            for row in seen_approvals
            for action in (row.get("payload") or {}).get("actions", [])
            if action.get("tool")
        }
    )
    first_gate_index = next(
        (
            index
            for index, event in enumerate(events)
            if event["type"] == "inqtrix.agent.approval.requested"
        ),
        None,
    )
    # Keyed per CALL (tool:tool_call_id), never per tool name: two
    # distinct pre-gate executions of the same tool must both surface,
    # and a name-key would also blur them with the gated call itself.
    premature_tools = sorted(
        {
            (
                f"{event['data'].get('tool') or ''}"
                f":{event['data'].get('tool_call_id') or ''}"
            )
            for index, event in enumerate(events)
            if event["type"] == "inqtrix.agent.tool.finished"
            and str(event["data"].get("tool") or "") in gated_tools
            and first_gate_index is not None
            and index < first_gate_index
        }
    )
    return KernelTrialRecord(
        scenario_id=scenario.scenario_id,
        trial=trial,
        status=status,
        answer=answer,
        cited_labels=sorted(set(_CITATION_LABEL.findall(answer))),
        reference_labels=[
            str(ref.get("label") or "") for ref in references
        ],
        artifact_kinds=[str(row.get("kind") or "") for row in artifacts],
        tools_started=tools_started,
        approvals=seen_approvals,
        gated_tools=gated_tools,
        premature_tools=premature_tools,
        narrations=narrations,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_s=latency,
    )


# -- code-first graders ---------------------------------------------------- #


def grade_outcome(record: KernelTrialRecord) -> None:
    """The run must complete with a non-empty answer AND answer artifact."""
    if record.status != "completed":
        record.failures.append(f"status={record.status}")
    if not record.answer.strip():
        record.failures.append("empty_answer")
    if "answer" not in record.artifact_kinds:
        record.failures.append("missing_answer_artifact")


def grade_citations(record: KernelTrialRecord) -> None:
    """Every cited label must resolve; cited answers surface cited-only refs."""
    unresolved = [
        label
        for label in record.cited_labels
        if label not in record.reference_labels
    ]
    if unresolved:
        record.failures.append(f"unresolved_citations={unresolved}")
    if record.cited_labels and set(record.reference_labels) - set(
        record.cited_labels
    ):
        # The cited-only contract: a citing answer lists exactly the
        # cited subset (basis fallback applies only to uncited answers).
        record.failures.append("references_exceed_citations")


def grade_scenario_expectations(
    record: KernelTrialRecord, scenario: KernelScenario
) -> None:
    """Routing + policy conformance against the scenario's contract."""
    if scenario.expect_citations and not record.cited_labels:
        record.failures.append("expected_citations_missing")
    missing = scenario.expected_tools - set(record.tools_started)
    if missing:
        record.failures.append(f"expected_tools_missing={sorted(missing)}")
    forbidden = scenario.forbidden_tools & set(record.tools_started)
    if forbidden:
        record.failures.append(f"forbidden_tools_used={sorted(forbidden)}")
    if scenario.expect_gate and not record.approvals:
        record.failures.append("expected_gate_never_parked")
    if not scenario.expect_gate and record.approvals:
        record.failures.append("unexpected_gate")
    if (
        scenario.expected_gate_tool
        and scenario.expected_gate_tool not in record.gated_tools
    ):
        record.failures.append(
            f"gate_tool_mismatch expected={scenario.expected_gate_tool} "
            f"got={record.gated_tools}"
        )
    if record.premature_tools:
        record.failures.append(
            f"tool_ran_before_consent={record.premature_tools}"
        )


def grade_trial(
    record: KernelTrialRecord, scenario: KernelScenario
) -> KernelTrialRecord:
    """Run every code grader; ``record.failures`` empty = pass."""
    grade_outcome(record)
    grade_citations(record)
    grade_scenario_expectations(record, scenario)
    return record


# -- reliability metrics ---------------------------------------------------- #


def pass_hat_k(successes: int, trials: int, k: int) -> float:
    """Unbiased ``pass^k`` estimator: P(k sampled trials ALL succeed).

    ``C(s, k) / C(n, k)`` over the observed trials — the reliability
    metric (all-k-succeed), stricter than ``pass@k`` (any-of-k).
    """
    if trials < k:
        raise ValueError(f"need >= {k} trials, got {trials}")
    if successes < k:
        return 0.0
    return comb(successes, k) / comb(trials, k)


def summarize_trials(
    records: list[KernelTrialRecord], *, k: int
) -> dict[str, Any]:
    """Per-scenario metrics block for the artifact/baseline."""
    successes = sum(1 for record in records if not record.failures)
    latencies = sorted(record.latency_s for record in records)
    mid = latencies[len(latencies) // 2] if latencies else 0.0
    return {
        "trials": len(records),
        "successes": successes,
        "pass_rate": successes / len(records) if records else 0.0,
        "pass_hat_k": pass_hat_k(successes, len(records), k)
        if len(records) >= k
        else None,
        "p50_latency_s": round(mid, 3),
        "p95_latency_s": round(
            latencies[max(0, int(len(latencies) * 0.95) - 1)], 3
        )
        if latencies
        else 0.0,
        "total_tokens": sum(
            record.prompt_tokens + record.completion_tokens
            for record in records
        ),
        "failures": sorted(
            {failure for record in records for failure in record.failures}
        ),
    }


def write_kernel_eval_artifact(
    name: str, per_scenario: dict[str, dict[str, Any]]
) -> Path:
    """Persist the eval outcome for review (same idiom as answer eval)."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"{name}.json"
    path.write_text(
        json.dumps(per_scenario, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def run_scenario_trials(
    client_factory: Callable[[], Any],
    scenario: KernelScenario,
    *,
    trials: int,
    k: int,
) -> tuple[list[KernelTrialRecord], dict[str, Any]]:
    """Run ``trials`` independent trials (fresh client each — no state
    bleed between trials) and summarize with ``pass^k``."""
    records: list[KernelTrialRecord] = []
    for trial in range(trials):
        client = client_factory()
        with client:
            record = run_kernel_trial(client, scenario, trial=trial)
        records.append(grade_trial(record, scenario))
    return records, summarize_trials(records, k=k)
