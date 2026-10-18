from __future__ import annotations

import logging
import re
import uuid

from inqtrix.auth.log_redaction import (
    log_authorization_denial,
    pseudonymous_log_reference,
)


def test_log_references_are_stable_domain_separated_and_non_identifying():
    identifier = uuid.UUID("12345678-1234-5678-1234-567812345678")

    first = pseudonymous_log_reference("usr", identifier)
    second = pseudonymous_log_reference("usr", identifier)
    resource = pseudonymous_log_reference("res", identifier)

    assert first == second
    assert first != resource
    assert re.fullmatch(r"usr_[0-9a-f]{16}", first)
    assert str(identifier) not in first
    assert pseudonymous_log_reference("usr", None) == "none"


def test_authorization_log_omits_raw_ids_policy_detail_and_unsafe_labels(caplog):
    actor = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    tenant = "tenant-customer-name"
    resource = "document-owned-by-alice"

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        log_authorization_denial(
            logging.getLogger("inqtrix"),
            action="read\nforged=true",
            principal_kind="oidc_session",
            actor_user_id=actor,
            tenant_id=tenant,
            resource_type="editor_document",
            resource_id=resource,
        )

    message = caplog.messages[-1]
    assert message.startswith("authz denied: action=unknown kind=oidc_session")
    assert "actor_ref=usr_" in message
    assert "tenant_ref=ten_" in message
    assert "resource_type=editor_document" in message
    assert "resource_ref=res_" in message
    assert str(actor) not in message
    assert tenant not in message
    assert resource not in message
    assert "forged=true" not in message
    assert "permission=" not in message
    assert "reason=" not in message
