"""A plan decision's requirement: parts always travel with the composed text."""

from __future__ import annotations

import pytest

from inqtrix.agents.control_ports import ApprovalRecord, public_decision_payload


def _decision(payload: dict) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="apr_1",
        run_id="run_1",
        kind="plan",
        status="approved",
        decision="approve",
        decision_payload=payload,
    )


def test_a_decision_without_its_parts_is_named():
    # A decision that stored composed text without its parts was rebuilt
    # silently as typed text, or failed later with a bare KeyError.
    from inqtrix.agents.control_ports import (
        DecisionWithoutRequirementParts,
        decided_requirement_parts,
    )

    with pytest.raises(DecisionWithoutRequirementParts, match="apr_1"):
        decided_requirement_parts({"report_guidance": "Kurz."}, approval_id="apr_1")
    parts = {"free_text": "Kurz.", "rules": []}
    assert decided_requirement_parts(
        {"report_guidance": "Kurz.", "report_requirement": parts}, approval_id="apr_1"
    ) == parts


def test_the_public_payload_never_hands_out_the_composed_text():
    # A decision without parts was returned as stored, composed text included.
    legacy = public_decision_payload(_decision({"report_guidance": "[Rule: x]\nBody"}))
    assert "report_guidance" not in legacy
    parts = {"free_text": "", "rules": [{"template_id": "pt_x", "label": "x", "revision": 1}]}
    current = public_decision_payload(
        _decision({"report_guidance": "[Rule: x]\nBody", "report_requirement": parts})
    )
    assert current == {"report_requirement": parts}


@pytest.mark.asyncio
async def test_a_later_decision_is_no_part_of_the_rules_in_force():
    # The rules a pending gate starts from are the ones the last EARLIER
    # decision set, else the submitted ones. A newer decided gate next to
    # it is not something the gate flow produces today, so only this
    # direct state pins that it would not count.
    from types import SimpleNamespace

    from inqtrix.agents.report_requirement import (
        compose_requirement_blocks,
        rule_requirement_block,
    )
    from inqtrix.services.agent_control_service import AgentControlService

    submitted = {"template_id": "pt_a", "label": "a", "revision": 1}
    later = {"template_id": "pt_b", "label": "b", "revision": 1}
    block_a = rule_requirement_block("a", "Gliedere klar.")
    block_b = rule_requirement_block("b", "Kurz halten.")
    pending = ApprovalRecord(
        approval_id="apr_pending", run_id="run_1", kind="plan", status="pending"
    )
    newer = ApprovalRecord(
        approval_id="apr_newer",
        run_id="run_1",
        kind="replan",
        status="approved",
        decision="approve",
        decision_payload={
            "report_guidance": compose_requirement_blocks([block_b]),
            "report_requirement": {"free_text": "", "rules": [later]},
        },
    )

    async def list_approvals(run_id):
        return [newer, pending]  # newest first

    service = AgentControlService(
        store=SimpleNamespace(list_approvals=list_approvals),
        run_store=SimpleNamespace(
            execution_request_body=lambda run_id: {
                "report_requirement": compose_requirement_blocks([block_a]),
                "report_rules": [submitted],
            }
        ),
    )
    in_force = await service._rules_in_force("run_1", pending)
    assert in_force == {"pt_a": (submitted, block_a)}
