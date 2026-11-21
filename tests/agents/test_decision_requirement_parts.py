"""A stored plan decision of a mission run reads as the parts the user chose:
the composed rule text it stored for the mission's resume is never handed out."""

from __future__ import annotations


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


def test_the_public_payload_never_hands_out_the_composed_text():
    # A decision without parts was returned as stored, composed text included.
    legacy = public_decision_payload(_decision({"report_guidance": "[Rule: x]\nBody"}))
    assert "report_guidance" not in legacy
    parts = {"free_text": "", "rules": [{"template_id": "pt_x", "label": "x", "revision": 1}]}
    current = public_decision_payload(
        _decision({"report_guidance": "[Rule: x]\nBody", "report_requirement": parts})
    )
    assert current == {"report_requirement": parts}


