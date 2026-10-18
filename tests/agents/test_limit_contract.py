"""Durable Agent Desk limit-decision contract."""

from __future__ import annotations

from dataclasses import replace

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.discovery import build_probe_plan
from inqtrix.agents.kernel.deps import run_coro
from inqtrix.agents.limit_contract import (
    LIMIT_CHOICE_CANCEL,
    LIMIT_CHOICE_PARTIAL,
    AgentLimitGate,
    create_or_get_limit_gate,
    disproportionate_budget,
    effective_extended_limit,
    latest_terminal_limit_choice,
    next_extended_limit,
    parse_limit_gate,
    recorded_limit_choice,
)


def test_extension_is_monotonic_and_operator_bounded() -> None:
    assert next_extended_limit(current=30, ceiling=60) == 60
    assert next_extended_limit(current=30, ceiling=45, required=44) == 45
    assert next_extended_limit(current=60, ceiling=60) == 60


def test_discovery_budget_retains_visible_omission_count() -> None:
    plan = build_probe_plan(
        None,
        question="Aktuelle Preise?",
        collection_ids=[],
        max_calls=1,
        web_preview_allowed=True,
    )
    assert len(plan.probes) == 1
    assert plan.requested_count == 3
    assert plan.limit == 1
    assert plan.omitted_count == 2


def test_gate_creation_is_idempotent_and_persists_exact_reason() -> None:
    store = MemoryAgentControlStore()
    gate = AgentLimitGate(
        kind="tool_calls",
        used=29,
        current=30,
        proposed=33,
        ceiling=60,
    )
    first, first_created = create_or_get_limit_gate(
        store,
        run_id="run_1234567890ab",
        gate=gate,
        run_async=run_coro,
        language="de",
    )
    second, second_created = create_or_get_limit_gate(
        store,
        run_id="run_1234567890ab",
        gate=gate,
        run_async=run_coro,
        language="de",
    )

    assert first_created is True
    assert second_created is False
    assert second == first
    assert "29 Werkzeugaufrufe" in first.question
    assert "Kein Aufruf dieses Batches wurde ausgeführt" in first.question
    assert parse_limit_gate(first) == gate


def test_the_limit_card_is_written_in_the_runs_answer_language() -> None:
    """A gate card is a dialogue turn: it must reach the reader in the
    language the run is held in, and an unresolved language must fall back
    to English rather than silently to German."""
    gate = AgentLimitGate(kind="steps", used=4, current=3, proposed=6, ceiling=241)
    store = MemoryAgentControlStore()

    german, _ = create_or_get_limit_gate(
        store, run_id="run_de0000000000", gate=gate, run_async=run_coro,
        language="de",
    )
    assert "Ausführungsschritte" in german.question
    assert "Wie soll der Lauf fortgesetzt werden?" in german.question
    assert [option["label"] for option in german.options] == [
        "Auf 6 erweitern", "Teilstand übernehmen", "Lauf abbrechen",
    ]

    english, _ = create_or_get_limit_gate(
        store, run_id="run_en0000000000", gate=gate, run_async=run_coro,
        language="en",
    )
    assert "execution steps" in english.question
    assert "Ausführungsschritte" not in english.question
    assert [option["label"] for option in english.options] == [
        "Extend to 6", "Take the partial result", "Cancel the run",
    ]

    neutral, _ = create_or_get_limit_gate(
        store, run_id="run_xx0000000000", gate=gate, run_async=run_coro,
        language="",
    )
    assert "execution steps" in neutral.question


def test_a_free_text_answer_is_matched_in_both_languages() -> None:
    """The stable option_id is the contract; the label aliases are only a
    convenience for free-text answers and must not stay German-only once the
    card itself can be English."""
    gate = AgentLimitGate(kind="steps", used=4, current=3, proposed=6, ceiling=241)
    record, _ = create_or_get_limit_gate(
        MemoryAgentControlStore(), run_id="run_1234567890ab", gate=gate,
        run_async=run_coro, language="de",
    )
    for answer, expected in [
        ("Teilstand übernehmen", LIMIT_CHOICE_PARTIAL),
        ("Take the partial result", LIMIT_CHOICE_PARTIAL),
        ("Lauf abbrechen", LIMIT_CHOICE_CANCEL),
        ("Cancel the run", LIMIT_CHOICE_CANCEL),
    ]:
        answered = replace(record, status="answered", answer=answer, option_id="")
        assert recorded_limit_choice(answered) == expected, answer


def test_operator_ceiling_never_offers_a_fake_extension() -> None:
    store = MemoryAgentControlStore()
    gate = AgentLimitGate(
        kind="steps", used=145, current=145, proposed=145, ceiling=145
    )
    record, _ = create_or_get_limit_gate(
        store,
        run_id="run_1234567890ab",
        gate=gate,
        run_async=run_coro,
        language="de",
    )
    assert [option["id"] for option in record.options] == ["partial", "cancel"]
    assert "lässt keine Erweiterung zu" in record.question


def test_answered_extension_folds_idempotently_without_resetting_usage() -> None:
    store = MemoryAgentControlStore()
    gate = AgentLimitGate(
        kind="tool_calls", used=30, current=30, proposed=60, ceiling=60
    )
    record, _ = create_or_get_limit_gate(
        store,
        run_id="run_1234567890ab",
        gate=gate,
        run_async=run_coro,
        language="de",
    )
    run_coro(
        store.create_clarification(
            replace(record, status="answered", option_id="extend")
        )
    )

    first = effective_extended_limit(
        store,
        run_id=record.run_id,
        kind="tool_calls",
        base=30,
        ceiling=60,
        run_async=run_coro,
    )
    second = effective_extended_limit(
        store,
        run_id=record.run_id,
        kind="tool_calls",
        base=30,
        ceiling=60,
        run_async=run_coro,
    )
    assert first == second == 60
    assert parse_limit_gate(record).used == 30  # type: ignore[union-attr]


def test_partial_choice_is_terminal_only_after_answer_is_persisted() -> None:
    store = MemoryAgentControlStore()
    gate = AgentLimitGate(
        kind="steps", used=73, current=73, proposed=145, ceiling=145
    )
    pending, _ = create_or_get_limit_gate(
        store,
        run_id="run_1234567890ab",
        gate=gate,
        run_async=run_coro,
        language="de",
    )
    assert latest_terminal_limit_choice(
        store, run_id=pending.run_id, run_async=run_coro
    ) is None

    run_coro(
        store.create_clarification(
            replace(
                pending,
                status="answered",
                option_id=LIMIT_CHOICE_PARTIAL,
            )
        )
    )
    terminal = latest_terminal_limit_choice(
        store, run_id=pending.run_id, run_async=run_coro
    )
    assert terminal is not None
    parsed, choice, record = terminal
    assert parsed == gate
    assert choice == LIMIT_CHOICE_PARTIAL
    assert record.status == "answered"


def test_effective_tool_grants_folds_only_run_scoped_tool_approves() -> None:
    """The grant fold: only approve + kind=tool + approval_scope=run grants;
    always-gated tools stay excluded even if such a row slipped in."""
    from inqtrix.agents.control_ports import ApprovalRecord
    from inqtrix.agents.limit_contract import effective_tool_grants

    store = MemoryAgentControlStore()
    run_id = "run_grants567890"

    def _row(
        approval_id: str,
        *,
        kind: str = "tool",
        decision: str = "approve",
        scope: str | None = "run",
        tool: str = "web_instant",
    ) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=approval_id,
            run_id=run_id,
            kind=kind,
            status="approved" if decision else "pending",
            payload={"actions": [{"tool": tool, "args": {}, "summary": ""}]},
            decision=decision,
            decision_payload=(
                {"approval_scope": scope} if scope is not None else {}
            ),
        )

    rows = [
        _row("apr_granted"),
        _row("apr_once", scope=None),
        _row("apr_rejected", decision="reject"),
        _row("apr_plan", kind="plan"),
        _row("apr_skill", tool="load_skill"),
        # Defense in depth: the service refuses this, and the fold
        # refuses to honor one that slipped in anyway.
        _row("apr_patch", tool="propose_editor_patch"),
    ]
    for row in rows:
        run_coro(store.create_approval(row))

    grants = effective_tool_grants(store, run_id=run_id, run_async=run_coro)
    assert grants == frozenset({"web_instant", "load_skill"})
    assert (
        effective_tool_grants(store, run_id="run_other", run_async=run_coro)
        == frozenset()
    )


# -- a budget that bound for the wrong reason ------------------------------- #


def test_a_binding_budget_whose_siblings_are_barely_spent_is_a_finding() -> None:
    """The measured defect, as the numbers a run ends with.

    Tool calls stopped a review at 110% while its steps sat at 19% and its
    tokens at 26%. Every per-limit message states its own number, so nothing
    reported that the three budgets had stopped measuring one cost — the run
    simply looked too big for its ceiling.
    """
    finding = disproportionate_budget({
        "tool_calls": {"used": 30, "limit": 30},
        "steps": {"used": 47, "limit": 249},
        "tokens": {"used": 234_294, "limit": 900_000},
    }, bound="tool_calls")
    assert finding.startswith("tool_calls bound at 30/30 while")
    assert "steps=47/249" in finding
    assert "tokens=234294/900000" in finding


def test_a_budget_that_bound_together_with_its_siblings_is_no_finding() -> None:
    """A run that spent everything is expensive, not mismeasured.

    This is the counter-case that keeps the finding from becoming noise:
    exhausting a ceiling is the ordinary way a large run ends, and a warning
    on every one of them would say nothing.
    """
    assert disproportionate_budget({
        "tool_calls": {"used": 30, "limit": 30},
        "steps": {"used": 200, "limit": 249},
        "tokens": {"used": 800_000, "limit": 900_000},
    }, bound="tool_calls") == ""


def test_a_budget_the_run_does_not_publish_is_no_finding() -> None:
    """The named cause must be one of the budgets this run actually has.

    A run without a token ceiling publishes two limits, and a caller that
    names a third has nothing to compare: the check stays silent rather
    than inventing the missing row. The name itself is taken as fact — it
    comes from the gate that stopped the run or from the typed status that
    ended a role, not from a guess about the counts.
    """
    spent = {
        "tool_calls": {"used": 12, "limit": 30},
        "steps": {"used": 47, "limit": 249},
    }
    assert disproportionate_budget(spent, bound="tokens") == ""
    assert disproportionate_budget(spent, bound="") == ""


def test_a_single_budget_cannot_be_disproportionate_to_itself() -> None:
    """With nothing to compare against there is no proportion to judge.

    A run under a token budget of 0 (unbounded) publishes two limits, not
    three; the check must stay silent rather than invent a sibling.
    """
    assert disproportionate_budget(
        {"tool_calls": {"used": 30, "limit": 30}}, bound="tool_calls"
    ) == ""
    assert disproportionate_budget({}, bound="tool_calls") == ""


def test_a_rejected_batch_leaves_the_count_below_the_ceiling_and_is_still_a_finding() -> None:
    """The case a count-derived check cannot see.

    The middleware rejects the WHOLE batch that would overshoot, so the
    calls it refused never execute and never count. A run stopped at 26 of
    30 by a batch of six is the common shape; only a run whose rejected
    batch began exactly on the ceiling ever reaches 30 of 30. Asking the
    spent count "did you reach your limit?" would therefore stay silent for
    every ordinary overshoot — which is why the caller names the cause.
    """
    finding = disproportionate_budget({
        "tool_calls": {"used": 26, "limit": 30},
        "steps": {"used": 47, "limit": 249},
        "tokens": {"used": 234_294, "limit": 900_000},
    }, bound="tool_calls")
    assert finding.startswith("tool_calls bound at 26/30 while")
    assert "steps=47/249" in finding


def test_the_park_names_the_gate_as_the_cause_and_the_role_its_status() -> None:
    """The wiring, not the predicate: who tells the check what stopped the run.

    A predicate nobody calls with the truth is decoration. Both callers hold
    the fact already — the conversational park builds a gate FOR the limit it
    reached, and a role's typed status says whether its budget ran out — so
    the check must read it from there and from nowhere else. Deleting either
    hand-over leaves the log silent about a budget that bound.
    """
    import ast
    import inspect

    from inqtrix.agents.kernel import algorithm

    tree = ast.parse(inspect.getsource(algorithm))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_log_disproportionate_budget"
    ]
    assert len(calls) == 2, "one hand-over per place a budget can stop a run"
    bound = {
        ast.unparse(keyword.value)
        for call in calls
        for keyword in call.keywords
        if keyword.arg == "bound"
    }
    assert bound == {
        "gate.kind",
        "'tool_calls' if status == WORK_RESULT_BUDGET_EXHAUSTED else ''",
    }, bound
