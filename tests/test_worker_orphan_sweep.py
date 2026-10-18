"""Orphan-sweep gating of the durable job stores.

Locks the second root cause behind the failed Agent-Desk run: the
QUEUE-BACKED worker also constructs its stores with ``queue=None``
(claim-mode wiring). The run queue additionally enters the store through the
separate ``dispatch_queue`` seam for child submissions and parent wakes, so
the historical ``queue is None`` inference made every worker
start blanket-fail all queued/running runs of the deployment
("Verwaister Run ... nach Neustart als fehlgeschlagen markiert").
"""

from __future__ import annotations

import ast
from pathlib import Path

import inqtrix.worker.__main__ as worker_main
from inqtrix.runs.durable_store import resolve_orphan_sweep


def test_default_keeps_no_queue_inference() -> None:
    """API single-process semantics unchanged: no queue -> sweep."""
    assert resolve_orphan_sweep(None, None) is True
    assert resolve_orphan_sweep(object(), None) is False


def test_explicit_flag_overrides_the_inference() -> None:
    """The worker's claim-mode ``queue=None`` must be able to opt out."""
    assert resolve_orphan_sweep(None, False) is False
    assert resolve_orphan_sweep(object(), True) is True


def test_worker_constructs_every_store_without_orphan_sweep() -> None:
    """Both worker store constructions pass ``recover_orphans=False``.

    Checked at the call sites (AST) because ``main()`` cannot run
    without live Postgres/Valkey: removing the keyword from either
    constructor call silently re-enables the deployment-wide sweep,
    which is exactly the regression this guards.
    """
    source = Path(worker_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    store_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("PostgresRunStore", "PostgresIndexingJobStore")
    ]
    assert {call.func.id for call in store_calls} == {
        "PostgresRunStore",
        "PostgresIndexingJobStore",
    }
    for call in store_calls:
        flags = {
            keyword.arg: getattr(keyword.value, "value", None)
            for keyword in call.keywords
        }
        assert flags.get("recover_orphans") is False, (
            f"{call.func.id} im Worker muss recover_orphans=False "
            "uebergeben (queue=None ist dort claim-mode wiring)."
        )
        if call.func.id == "PostgresRunStore":
            dispatch = next(
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "dispatch_queue"
            )
            assert isinstance(dispatch, ast.Name)
            assert dispatch.id == "queue"


def test_worker_run_store_uses_durable_retention() -> None:
    """The worker's terminal-run TTL is the DURABLE retention.

    Wiring ``run_completed_ttl_seconds`` (the 300s in-memory replay
    window) here would make the worker's lazy cleanup delete every terminal
    run — reports, answers, child runs — five minutes after completion.
    AST-checked for the same reason as the sweep guard.
    """
    source = Path(worker_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_store_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PostgresRunStore"
    )
    ttl = next(
        keyword.value
        for keyword in run_store_call.keywords
        if keyword.arg == "completed_ttl_seconds"
    )
    assert isinstance(ttl, ast.Attribute)
    assert ttl.attr == "run_durable_retention_seconds", (
        "Der Worker-Store muss run_durable_retention_seconds verdrahten "
        "— run_completed_ttl_seconds ist das In-Memory-Replay-Fenster."
    )
