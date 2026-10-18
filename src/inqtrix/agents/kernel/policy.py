"""Kernel tool-gating policy per autonomy mode.

THE one mapping from the run's autonomy to the deepagents
``interrupt_on`` configuration — the algorithm compiles one graph
variant per policy (interrupt_on is compile-time, the policy is fixed
per run). This mapping controls user confirmation; resource authorization
remains with each tool's scoped reader or writer.

Wire vocabulary stays the three-mode set (strict/balanced/autonomous);
the UI presents Standard(balanced)/Auto(autonomous). The plan-table
semantics:

======================  ==================================  ==========
Tool                    Standard (balanced)                 Auto
======================  ==================================  ==========
ask_user                free                                free
web_instant             GATED — query verbatim in args      free
search_project_...      gated only UN-scoped                free
read_project_document   free                                free
cite_document_passages  free (only text the run read)       free
read_research_report    free (only ATTACHED reports)        free
======================  ==================================  ==========

``strict`` gates external work and broad internal access. Reading evidence
already registered to the run does not need renewed consent; its reader
still authorizes the retained origin. Write-effect tools
(``propose_editor_patch``) are ALWAYS gated in every mode — they belong
to :data:`ALWAYS_GATED_TOOLS`, not to a mode table. Canvas reads and
writes are gated only in ``strict``; Standard and Auto keep the canvas
itself as their review surface.

The config dicts are LangChain ``InterruptOnConfig`` TypedDicts (plain
dicts at runtime — no langchain import needed here). Allowed decisions
mirror our ``APPROVAL_DECISIONS`` (approve/edit/reject); ``respond``
stays unexposed.
"""

from __future__ import annotations

from typing import Any

GATE_DECISIONS = ["approve", "edit", "reject"]
"""HITL decisions the platform exposes — 1:1 our approval verbs."""

ALWAYS_GATED_TOOLS: tuple[str, ...] = ("propose_editor_patch",)
"""Write-effect tools gated in EVERY mode (patches are never
autonomous — the user's document is the one surface the agent must not
touch unreviewed). Kept separate from the mode table so a new autonomy
mode can never accidentally free a write path. ``write_canvas`` is
deliberately NOT here: it stays free in Standard/Auto and is gated only
by the explicit ``strict`` table."""


def _gate(when: Any = None, *, user_conditional: bool = False) -> dict[str, Any]:
    config: dict[str, Any] = {"allowed_decisions": list(GATE_DECISIONS)}
    if when is not None:
        config["when"] = when
    if user_conditional:
        # Published-contract classification only (HITL ignores unknown
        # keys): TRUE means the predicate leaves a user-triggerable path
        # UNGATED (scoped knowledge search). The child-tool predicate is
        # NOT user-conditional — it merely skips a doomed multi-child
        # batch the guard voids, so every real delegation still gates and
        # the tool stays in ``kernel_gated_tools``.
        config["user_conditional"] = True
    return config


def _ungranted(tool_name: str) -> Any:
    """Gate predicate factory: fires unless a run-wide grant covers the tool.

    The grant set is per-run state and must NOT enter the compile-time
    ``interrupt_config_for`` signature (the graph cache is keyed on it) —
    it travels through the deps ContextVar exactly like the summarization
    trigger and ``_single_child_dispatch``'s state read. Fail-closed:
    without a deps context nothing counts as granted.
    """

    def _when(request: Any) -> bool:
        from inqtrix.agents.kernel.deps import kernel_deps

        try:
            deps = kernel_deps()
        except RuntimeError:
            # No segment context (``kernel_deps`` raises when unset) —
            # nothing counts as granted, the gate fires.
            return True
        return tool_name not in deps.tool_grants

    return _when


def _all_of(*predicates: Any) -> Any:
    """AND-compose gate predicates (the gate fires only if every one fires)."""

    def _when(request: Any) -> bool:
        return all(predicate(request) for predicate in predicates)

    return _when


def _unscoped_knowledge_search(request: Any) -> bool:
    """Gate predicate: only an UN-scoped project-wide search interrupts.

    A search the user already scoped to explicit collections is the
    plan-approved shape of internal retrieval; the broad sweep over
    everything visible is what Standard asks about first.

    Scope comes from two places and BOTH count: the model's own
    ``collection_ids`` argument, and the run-level scope the user pinned
    at submission. Reading only the argument mis-read every chip-scoped
    run as project-wide — the model never learns about the run pin, so
    it cannot repeat it in its arguments. Fail-closed: without a segment
    context the gate fires.
    """
    args = getattr(request, "tool_call", {}).get("args", {}) or {}
    if args.get("collection_ids"):
        return False
    from inqtrix.agents.kernel.deps import kernel_deps

    try:
        deps = kernel_deps()
    except RuntimeError:
        return True
    return not getattr(deps, "knowledge_scope_explicit", False)


def _single_child_dispatch(request: Any) -> bool:
    """Gate predicate on every child tool: gate only a SINGLE dispatch.

    Two or more child-tool calls in one model turn are voided pre-
    dispatch by ``KernelChildBatchGuardMiddleware`` (corrective
    ToolMessages, nothing submits). Gating them anyway would park an
    approval whose actions can never execute — an approve becomes a
    lie (the guard refuses right after), and a reject double-answers
    the calls (HITL keeps the call on the AIMessage and answers it;
    the guard, running after HITL, would answer again — duplicate
    ToolMessages per call id that providers reject). Skipping the gate
    keeps exactly one authority per call: the guard for the doomed
    batch, HITL for the single dispatch.
    """
    from inqtrix.agents.kernel.middleware import (
        last_turn_child_calls,
        turn_has_interrupting_call,
    )

    state = getattr(request, "state", None) or {}
    last_ai, child_calls = last_turn_child_calls(
        state.get("messages") or []
    )
    if turn_has_interrupting_call(last_ai):
        # ask_user + child turns are voided by the batch guard —
        # gating the doomed child would double-answer it (see the
        # guard's docstring for the single-authority rule).
        return False
    return len(child_calls) <= 1


def interrupt_config_for(autonomy: str) -> dict[str, Any] | None:
    """The ``interrupt_on`` mapping of one policy variant.

    Args:
        autonomy: Wire autonomy mode (``strict``/``balanced``/
            ``autonomous``); unknown values fail loudly — a typo must
            never silently run ungated.

    Returns:
        Tool-name -> InterruptOnConfig mapping, or ``None`` when no
        tool gates (HITL middleware entirely omitted).
    """
    if autonomy == "autonomous":
        gated: dict[str, Any] = {}
    elif autonomy == "balanced":
        # Run-wide grants apply ONLY here: strict stays per-call by
        # design, autonomous has no grantable gates. Each predicate ANDs
        # the existing condition with the ungranted check, so a grant can
        # never widen a gate beyond suppressing it for its own tool.
        gated = {
            "web_instant": _gate(when=_ungranted("web_instant")),
            "search_project_knowledge": _gate(
                when=_all_of(
                    _unscoped_knowledge_search,
                    _ungranted("search_project_knowledge"),
                ),
                user_conditional=True,
            ),
            "run_web_research": _gate(
                when=_all_of(
                    _single_child_dispatch, _ungranted("run_web_research")
                )
            ),
            "run_deep_mission": _gate(
                when=_all_of(
                    _single_child_dispatch, _ungranted("run_deep_mission")
                )
            ),
            "delegate_batch": _gate(
                when=_all_of(
                    _single_child_dispatch, _ungranted("delegate_batch")
                )
            ),
            "load_skill": _gate(when=_ungranted("load_skill")),
        }
    elif autonomy == "strict":
        gated = {
            "web_instant": _gate(),
            "search_project_knowledge": _gate(),
            "read_project_document": _gate(),
            "read_canvas": _gate(),
            "search_context": _gate(),
            "read_context": _gate(),
            # Reading an ATTACHED report is a read like read_canvas: free
            # in the normal modes, reviewed in strict. The attachment
            # itself is already the user's consent — the tool refuses
            # every id the user did not attach.
            "read_research_report": _gate(),
            "write_canvas": _gate(),
            "read_editor_document": _gate(),
            "search_editor_document": _gate(),
            "run_web_research": _gate(when=_single_child_dispatch),
            "run_deep_mission": _gate(when=_single_child_dispatch),
            "delegate_batch": _gate(when=_single_child_dispatch),
            "load_skill": _gate(),
        }
    else:
        raise ValueError(f'Unknown autonomy mode: {autonomy!r}')
    for tool_name in ALWAYS_GATED_TOOLS:
        gated[tool_name] = _gate()
    return gated or None
