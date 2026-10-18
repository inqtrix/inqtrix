"""Canonical execution-block preservation in compact run snapshots."""

from __future__ import annotations

import logging

import pytest

from inqtrix.state import build_run_snapshot


def test_build_run_snapshot_preserves_canonical_agent_execution() -> None:
    execution = {
        "effective_mode": "agent_kernel",
        "response_form": "chat",
        "depth": "normal",
        "model": "model-a",
        "reasoning_effort": "high",
        "source_policy": {"web": "disabled", "knowledge": "available"},
        "knowledge_scope": "all",
        "knowledge_collection_count": 3,
        "consent_reason": "permission_policy",
        "tool_use_counts": {"web": 0, "knowledge": 2},
        "web_searches": 0,
        "limits": {},
        "tool_grants": [],
        "source_coverage": [],
        "work_profile": "review",
    }

    snapshot = build_run_snapshot(
        {"done": True, "execution": execution},
        current_node="agent_kernel",
        last_message="completed",
    )

    assert snapshot["execution"] == execution


def test_snapshot_keeps_the_knowledge_boundary_and_defaults_older_payloads() -> None:
    """The typed projection is an allowlist: a key the model does not declare
    silently vanishes from every snapshot, result and event. Pinned here at
    the projection, not at the payload builder."""
    base = {
        "effective_mode": "agent_kernel",
        "response_form": "chat",
        "depth": "normal",
        "model": "model-a",
        "reasoning_effort": "",
        "source_policy": {"web": "disabled", "knowledge": "available"},
        "consent_reason": "permission_policy",
        "tool_use_counts": {"web": 0, "knowledge": 1},
        "limits": {},
        "tool_grants": [],
    }
    pinned = build_run_snapshot(
        {"done": True, "execution": {**base, "knowledge_scope": "pinned", "knowledge_collection_count": 2}},
        current_node="agent_kernel",
        last_message="completed",
    )["execution"]
    assert pinned["knowledge_scope"] == "pinned"
    assert pinned["knowledge_collection_count"] == 2
    # A payload written before the field existed still validates and reads
    # as "not recorded" (null / 0) instead of asserting a choice.
    legacy = build_run_snapshot(
        {"done": True, "execution": base}, current_node="agent_kernel", last_message="completed",
    )["execution"]
    assert legacy["knowledge_scope"] is None
    assert legacy["knowledge_collection_count"] == 0
    # Searches the provider ran were not recorded before the field existed.
    assert legacy["web_searches"] is None
    with pytest.raises(RuntimeError):
        build_run_snapshot(
            {"done": True, "execution": {**base, "knowledge_scope": "everything"}},
            current_node="agent_kernel", last_message="completed",
        )


def test_invalid_execution_snapshot_logs_validation_code_without_payload(caplog) -> None:
    logger = logging.getLogger("inqtrix")
    previous_level = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.WARNING)
    private_invalid_value = "PRIVATE_INVALID_EXECUTION_VALUE"

    try:
        with pytest.raises(RuntimeError, match="invalid agent execution block"):
            build_run_snapshot(
                {
                    "done": False,
                    "execution": {
                        "effective_mode": private_invalid_value,
                    },
                },
                current_node="agent_kernel",
                last_message="failed",
            )
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous_level)

    assert "error_code=ValidationError" in caplog.text
    assert "error_count=" in caplog.text
    assert private_invalid_value not in caplog.text
