"""Durable user decisions for recoverable Agent Desk run limits.

Recoverable limits reuse the existing clarification row plus the native
run's ``waiting_for_input -> queued`` compare-and-swap.  The clarification is
the durable decision record; run events are only signals.  This deliberately
does not add another run lifecycle, queue, or model-authored budget channel.

Only checkpoint-safe limits belong here.  A caller may park after reaching a
limit when replay resumes from an authoritative checkpoint without repeating
already charged work.  Provider/token ceilings whose latest work is not yet
checkpointed remain non-recoverable typed stops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Callable, Literal

from inqtrix.i18n import t
from inqtrix.agents.control_ports import (
    AgentControlStore,
    ClarificationNotFound,
    ClarificationRecord,
)

LIMIT_REACHED_EVENT = "inqtrix.agent.limit.reached"
"""Audit/event signal emitted when a run parks at a recoverable limit."""
LIMIT_DECIDED_EVENT = "inqtrix.agent.limit.decided"
"""Audit signal emitted when a terminal partial/cancel choice is applied.

Extensions are already durably visible through the canonical
``clarification.answered`` event and row. Emitting a second apply event on
every checkpoint redelivery would make an exactly-once claim the event stream
cannot guarantee.
"""

LIMIT_CHOICE_EXTEND = "extend"
LIMIT_CHOICE_PARTIAL = "partial"
LIMIT_CHOICE_CANCEL = "cancel"

LimitKind = Literal["tool_calls", "steps"]
LimitChoice = Literal["extend", "partial", "cancel"]

_LIMIT_ID = re.compile(
    r"^clr_[A-Za-z0-9]+_limit_"
    r"(?P<kind>tool_calls|steps)_"
    r"(?P<used>[0-9]+)_"
    r"(?P<current>[0-9]+)_"
    r"(?P<proposed>[0-9]+)_"
    r"(?P<ceiling>[0-9]+)$"
)


@dataclass(frozen=True)
class AgentLimitGate:
    """One server-authored recoverable limit decision."""

    kind: LimitKind
    current: int
    proposed: int
    ceiling: int
    used: int

    @property
    def extendable(self) -> bool:
        return self.proposed > self.current and self.proposed <= self.ceiling

    def clarification_id(self, run_id: str) -> str:
        return (
            f"clr_{run_id[-12:]}_limit_{self.kind}_"
            f"{self.used}_{self.current}_{self.proposed}_{self.ceiling}"
        )

    def payload(self, *, clarification_id: str) -> dict[str, Any]:
        return {
            "clarification_id": clarification_id,
            "kind": self.kind,
            "used": self.used,
            "limit": self.current,
            "next_limit": self.proposed if self.extendable else None,
            "ceiling": self.ceiling,
            "extendable": self.extendable,
            "choices": [
                *([LIMIT_CHOICE_EXTEND] if self.extendable else []),
                LIMIT_CHOICE_PARTIAL,
                LIMIT_CHOICE_CANCEL,
            ],
        }


def next_extended_limit(*, current: int, ceiling: int, required: int = 0) -> int:
    """Return the next monotonic limit inside the operator ceiling.

    The step doubles the current allowance, while an overflowing model batch
    can require a larger minimum.  ``current`` is returned when the ceiling is
    already binding; the UI then offers partial completion or cancellation,
    never a fake extension.
    """

    current = max(1, int(current))
    ceiling = max(current, int(ceiling))
    required = max(current + 1, int(required or 0))
    return min(ceiling, max(current * 2, required))


def create_or_get_limit_gate(
    control: AgentControlStore,
    *,
    run_id: str,
    gate: AgentLimitGate,
    run_async: Callable[[Any], Any],
    language: str,
) -> tuple[ClarificationRecord, bool]:
    """Create the deterministic clarification once; return ``(row, created)``.

    ``language`` is the run's resolved answer language. The card is a dialogue
    turn, so it is written once in the language the run is being held in; the
    stable ``option_id`` (not the label) is what the answer is matched on.
    """

    clarification_id = gate.clarification_id(run_id)
    try:
        return run_async(
            control.get_clarification(run_id, clarification_id)
        ), False
    except ClarificationNotFound:
        pass

    words = {"language": language or ""}
    noun = t(
        words,
        "agent_limit_noun_tool_calls"
        if gate.kind == "tool_calls"
        else "agent_limit_noun_steps",
    )
    options: list[dict[str, str]] = []
    if gate.extendable:
        options.append(
            {
                "id": LIMIT_CHOICE_EXTEND,
                "label": t(
                    words,
                    "agent_limit_option_extend_label",
                    proposed=gate.proposed,
                ),
                "description": t(
                    words, "agent_limit_option_extend_description"
                ),
            }
        )
    options.extend(
        [
            {
                "id": LIMIT_CHOICE_PARTIAL,
                "label": t(words, "agent_limit_option_partial_label"),
                "description": t(
                    words, "agent_limit_option_partial_description"
                ),
            },
            {
                "id": LIMIT_CHOICE_CANCEL,
                "label": t(words, "agent_limit_option_cancel_label"),
                "description": t(
                    words, "agent_limit_option_cancel_description"
                ),
            },
        ]
    )
    ceiling_text = (
        t(
            words,
            "agent_limit_ceiling_extendable",
            proposed=gate.proposed,
            ceiling=gate.ceiling,
        )
        if gate.extendable
        else t(words, "agent_limit_ceiling_fixed", ceiling=gate.ceiling)
    )
    reached_text = t(
        words,
        "agent_limit_reached_tool_calls"
        if gate.kind == "tool_calls"
        else "agent_limit_reached_steps",
        used=gate.used,
        noun=noun,
        current=gate.current,
    )
    record = ClarificationRecord(
        clarification_id=clarification_id,
        run_id=run_id,
        question=(
            f"{reached_text}{ceiling_text}"
            + t(words, "agent_limit_question_suffix")
        ),
        options=tuple(options),
        default_assumption=t(words, "agent_limit_default_assumption"),
    )
    return run_async(control.create_clarification(record)), True


def parse_limit_gate(record: ClarificationRecord) -> AgentLimitGate | None:
    """Parse server-authored coordinates from a clarification id."""

    match = _LIMIT_ID.fullmatch(record.clarification_id)
    if match is None:
        return None
    kind = match.group("kind")
    used = int(match.group("used"))
    current = int(match.group("current"))
    proposed = int(match.group("proposed"))
    ceiling = int(match.group("ceiling"))
    if used < 0 or current < 1 or proposed < current or ceiling < proposed:
        return None
    return AgentLimitGate(
        kind=kind,  # type: ignore[arg-type]
        current=current,
        proposed=proposed,
        ceiling=ceiling,
        used=used,
    )


def recorded_limit_choice(record: ClarificationRecord) -> LimitChoice | None:
    """Return the validated choice of one answered limit clarification."""

    if record.status != "answered" or parse_limit_gate(record) is None:
        return None
    raw = (record.option_id or record.answer).strip().casefold()
    aliases = {
        LIMIT_CHOICE_EXTEND: LIMIT_CHOICE_EXTEND,
        "erweitern": LIMIT_CHOICE_EXTEND,
        "extend": LIMIT_CHOICE_EXTEND,
        LIMIT_CHOICE_PARTIAL: LIMIT_CHOICE_PARTIAL,
        "teilstand übernehmen": LIMIT_CHOICE_PARTIAL,
        "teilstand uebernehmen": LIMIT_CHOICE_PARTIAL,
        "take the partial result": LIMIT_CHOICE_PARTIAL,
        LIMIT_CHOICE_CANCEL: LIMIT_CHOICE_CANCEL,
        "lauf abbrechen": LIMIT_CHOICE_CANCEL,
        "abbrechen": LIMIT_CHOICE_CANCEL,
        "cancel the run": LIMIT_CHOICE_CANCEL,
        "cancel": LIMIT_CHOICE_CANCEL,
    }
    return aliases.get(raw)  # type: ignore[return-value]


def effective_extended_limit(
    control: AgentControlStore,
    *,
    run_id: str,
    kind: LimitKind,
    base: int,
    ceiling: int,
    run_async: Callable[[Any], Any],
) -> int:
    """Fold answered extension rows into one bounded effective limit."""

    effective = max(1, int(base))
    hard_ceiling = max(effective, int(ceiling))
    records = run_async(control.list_clarifications(run_id))
    for record in reversed(records):
        gate = parse_limit_gate(record)
        if gate is None or gate.kind != kind:
            continue
        if recorded_limit_choice(record) != LIMIT_CHOICE_EXTEND:
            continue
        effective = max(
            effective,
            min(hard_ceiling, gate.ceiling, gate.proposed),
        )
    return effective


def latest_terminal_limit_choice(
    control: AgentControlStore,
    *,
    run_id: str,
    run_async: Callable[[Any], Any],
) -> tuple[AgentLimitGate, LimitChoice, ClarificationRecord] | None:
    """Return the newest explicit partial/cancel choice, if any."""

    for record in run_async(control.list_clarifications(run_id)):
        gate = parse_limit_gate(record)
        choice = recorded_limit_choice(record)
        if gate is not None and choice in {
            LIMIT_CHOICE_PARTIAL,
            LIMIT_CHOICE_CANCEL,
        }:
            return gate, choice, record
    return None


def effective_tool_grants(
    control: AgentControlStore,
    *,
    run_id: str,
    run_async: Callable[[Any], Any],
) -> frozenset[str]:
    """Fold approved run-wide tool grants into one per-segment set.

    Mirrors :func:`effective_extended_limit`: the decided approval row is
    the durable decision record, folded fresh at every segment start. A
    grant is exactly an ``approve`` on a ``kind="tool"`` gate whose
    ``decision_payload`` carries ``approval_scope == "run"``; the granted
    names are the action tools of that gate. ``ALWAYS_GATED_TOOLS`` are
    excluded by construction — the service refuses to store such a grant,
    and this fold refuses to honor one that slipped in anyway.
    """
    from inqtrix.agents.kernel.policy import ALWAYS_GATED_TOOLS

    granted: set[str] = set()
    records = run_async(control.list_approvals(run_id))
    for record in records:
        if record.kind != "tool" or record.decision != "approve":
            continue
        if dict(record.decision_payload).get("approval_scope") != "run":
            continue
        for action in record.payload.get("actions") or []:
            name = str(action.get("tool") or "")
            if name and name not in ALWAYS_GATED_TOOLS:
                granted.add(name)
    return frozenset(granted)


BOUND_BUDGET_SIBLING_SHARE = 0.5
"""Share below which a sibling budget counts as barely spent.

Half is not a threshold anybody tuned: it is the point at which a sibling is
no longer merely unused but demonstrably not the cost the run is paying. A
binding budget whose siblings sit under it is measuring something other than
what it stands for.
"""


def disproportionate_budget(
    limits: Mapping[str, Mapping[str, Any]],
    *,
    bound: str,
    share: float = BOUND_BUDGET_SIBLING_SHARE,
) -> str:
    """Name a budget that BOUND while its siblings were barely spent.

    *bound* is the budget that actually stopped the run, named by the caller
    that stopped it. It is not re-derived from the spent counts, because the
    count of a budget that rejected a whole batch is SMALLER than its limit:
    the refused calls never executed and are not counted. Asking
    ``used >= limit`` would therefore see a binding budget only when the
    rejected batch happened to begin exactly on the ceiling, and stay silent
    for every partial overshoot — the common case.

    Three budgets bound one run — tool calls, steps, tokens — and they are
    meant to bound the same thing, its cost. When one of them stops a run at
    100% while the others sit below half, they no longer measure one cost:
    the binding one has become a proxy for something it does not track, and
    the run was stopped by the proxy rather than by its price. That is a
    finding about the budget, not about the run, and it is invisible in
    every per-limit message, each of which reports only its own number.

    Returns the finding as one line, or ``""`` when no budget bound or the
    siblings were spent too.
    """
    spent: dict[str, tuple[int, int]] = {}
    for kind, row in limits.items():
        limit = int(row.get("limit") or 0)
        if limit > 0:
            spent[kind] = (max(0, int(row.get("used") or 0)), limit)
    if bound not in spent or len(spent) < 2:
        return ""
    siblings = {
        other: pair for other, pair in spent.items() if other != bound
    }
    if not all(
        used < share * limit for used, limit in siblings.values()
    ):
        return ""
    used, limit = spent[bound]
    return f"{bound} bound at {used}/{limit} while " + ", ".join(
        f"{other}={other_used}/{other_limit}"
        for other, (other_used, other_limit) in sorted(siblings.items())
    )
