"""Persisted task summaries must come from the table, not from literals.

A pin that only checks ``MESSAGES`` is the weak kind: it stays green while a
call site quietly spells a sentence inline again. This gate reads the CALL
SITES instead, so re-inlining one is red.

**Why the rule is "no literal" and not "no German literal".** The first
version of this gate looked for German. It missed ``"Synthese
fehlgeschlagen."`` -- two words, no umlaut, no stopword -- and so does the
product's own :func:`detect_ui_language`, which is built for sentences. A
language detector is the wrong instrument for a fragment. The invariant that
actually holds is stronger and needs no detector: a summary that outlives its
run has to follow the run's language, so it is ALWAYS ``t(...)`` and never a
constant, whatever language the constant happens to be in.

Deliberately narrow: only the ``summary`` / ``result_summary`` fields of the
agent runtime. Identifiers, wire values and prompt text are out of scope -- a
wider literal gate has to exempt those and is a separate decision.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SUMMARY_FIELDS = {"summary", "result_summary"}
# A local that is later handed to one of those fields is the same defect in
# a different shape -- the realistic one, so a gate that only reads keyword
# arguments misses the realistic regression.
SUMMARY_NAMES = ("_summary",)
AGENTS = pathlib.Path("src/inqtrix/agents")

# The one accepted exception, named by FUNCTION rather than by file.
# ``requested_task_cancellation`` is the store's own pure state machine: it
# is called from ``control_memory`` and ``agent_control_postgres``, which
# hold neither a run nor its language, and pushing the answer language into
# the storage layer would be a layering violation for cosmetics. Localizing
# it would mean rendering from its ``failure_code`` in the UI.
#
# Exempting the whole FILE would be too coarse: it would hide the settlement
# texts in the same module that a user reads after pressing Stop.
ALLOWED_FUNCTIONS = {"requested_task_cancellation"}


def _is_sentence(text: str) -> bool:
    """Prose, as opposed to a key, a field name or a separator.

    Structural, not linguistic: a sentence has a space in it and ends with
    sentence punctuation. That separates ``"Synthese abgeschlossen."`` from
    ``"memo_markdown"``, ``"; "`` and ``"cancelled"`` without asking any
    detector what language it is in -- the question that has no reliable
    answer for a two-word fragment.

    Known bound, stated rather than hidden: a user-facing sentence written
    WITHOUT closing punctuation slips past. Every summary in this codebase
    ends in a full stop, and widening the rule would start flagging keys.
    """
    stripped = text.strip()
    return " " in stripped and stripped.endswith((".", "!", "?"))


def _summary_literals(source: str) -> list[tuple[int, str]]:
    """Call sites that hand a summary field a hardcoded string.

    Functions in :data:`ALLOWED_FUNCTIONS` are skipped whole, so the
    exemption is as narrow as its reason.
    """
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ALLOWED_FUNCTIONS
        ):
            skip.update(
                child.lineno
                for child in ast.walk(node)
                if hasattr(child, "lineno")
            )
    def _literals(value: ast.AST) -> list[tuple[int, str]]:
        """Constants outside any ``t(...)`` call.

        A key handed to the translator is the CORRECT form, so its subtree
        is not evidence of a hardcoded sentence. Without this the gate
        flags its own solution.
        """
        out: list[tuple[int, str]] = []
        stack: list[ast.AST] = [value]
        while stack:
            node = stack.pop()
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
            ):
                continue
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.lineno not in skip
                and _is_sentence(node.value)
            ):
                out.append((node.lineno, node.value))
            stack.extend(ast.iter_child_nodes(node))
        return out

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in SUMMARY_FIELDS:
            found.extend(_literals(node.value))
        elif isinstance(node, ast.Assign) and node.value is not None:
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(
                n in SUMMARY_FIELDS or n.endswith(SUMMARY_NAMES)
                for n in names
            ):
                found.extend(_literals(node.value))
    return sorted(set(found))


@pytest.mark.parametrize(
    "path",
    sorted(AGENTS.rglob("*.py")),
    ids=lambda p: str(p.relative_to(AGENTS)),
)
def test_no_hardcoded_summary_at_a_call_site(path: pathlib.Path):
    found = _summary_literals(path.read_text(encoding="utf-8"))
    assert not found, (
        f"{path}: a persisted summary outlives its run and must follow the "
        f"run language, so it belongs in inqtrix.i18n.MESSAGES and reaches "
        f"the call site through t(...). Hardcoded at "
        + ", ".join(f"line {line} ({text[:40]!r})" for line, text in found)
    )


def test_the_gate_reads_call_sites_not_the_table():
    """Prove the instrument on both a German and an ENGLISH literal.

    The English case is the one the earlier German-only version let through.
    """
    assert _summary_literals(
        'TaskOutcome(status="failed", summary="Synthese fehlgeschlagen.")'
    )
    assert _summary_literals(
        'TaskOutcome(status="failed", summary="Synthesis failed.")'
    )
    assert not _summary_literals(
        'TaskOutcome(status="failed", summary=t(state, "some_key"))'
    )
    # An empty default is not a sentence and stays allowed.
    assert not _summary_literals('TaskOutcome(status="ok", summary="")')
    # The shape the original code had: a local, handed on later.
    assert _summary_literals('pending_summary = "Wegen Abbruch nicht."')
    # Keys, field names and separators are not prose and must not be flagged.
    assert not _summary_literals(
        'T(summary=task_result_summary(r.get("memo_markdown", "") or ""))'
    )
    assert not _summary_literals('T(summary=task_result_summary("; ".join(x)))')
    # ... but a prose fallback inside the same derivation IS flagged.
    assert _summary_literals(
        'T(summary=task_result_summary(r.get("m", "") or "Synthese fertig."))'
    )
    assert not _summary_literals('pending_summary = t(words, "some_key")')
    # The gate must not flag its own solution: keys inside t(...) are fine,
    # including the branch condition of a conditional expression.
    assert not _summary_literals(
        'pending_summary = t(w, "a_key" if status == "cancelled" else "b_key")'
    )


def test_the_exemption_is_a_function_not_a_file():
    """Only the named function is exempt; its neighbours are not."""
    source = (
        "def requested_task_cancellation(current):\n"
        '    return transitioned(result_summary="Aufgabe abgebrochen.")\n'
        "\n"
        "async def settle_terminal_plan_tasks(store, run_id):\n"
        '    await store.transition(result_summary="Wegen Abbruch nicht ausgef\u00fchrt.")\n'
    )
    found = _summary_literals(source)
    assert [text for _, text in found] == ["Wegen Abbruch nicht ausgeführt."]
