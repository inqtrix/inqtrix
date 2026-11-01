"""SCIM user provisioning: what a DELETE does, from the tombstone to the erasure."""

from __future__ import annotations

import logging
import uuid

import pytest

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.lifecycle import ProvisioningActor, UserLifecycleStatus
from inqtrix.auth.scim import SCIM_ERASE_MAX_PER_HOUR, ScimUsers

USER = uuid.UUID("33333333-3333-4333-8333-333333333333")
REASON = "1 row(s) in workspaces.created_by_user_id: reassign it first"


class _Lifecycle:
    """The lifecycle commands a SCIM DELETE runs, with scripted answers."""

    def __init__(
        self,
        *,
        deprovision: UserLifecycleStatus = UserLifecycleStatus.UPDATED,
        erasure: UserLifecycleStatus = UserLifecycleStatus.UPDATED,
    ) -> None:
        self._deprovision = deprovision
        self._erasure = erasure
        self.calls: list[tuple] = []

    async def deprovision(self, *, tenant_id, user_id):
        self.calls.append(("deprovision", tenant_id, user_id))
        return self._deprovision

    async def begin_erasure(
        self, *, tenant_id, user_id, actor, operation_id, hourly_limit=None
    ):
        self.calls.append(("begin_erasure", tenant_id, user_id, actor, hourly_limit))
        if self._erasure is UserLifecycleStatus.UPDATED:
            return self._erasure, {"operation_id": operation_id}
        if self._erasure is UserLifecycleStatus.REFUSED:
            return self._erasure, {"refused": [REASON]}
        return self._erasure, {}


class _Deletions:
    def __init__(self) -> None:
        self.started: list[tuple] = []

    async def start_user_erasure(self, user_id, *, actor, tenant_id, operation_id):
        self.started.append((user_id, actor, tenant_id, operation_id))
        return {"operation_id": operation_id, "status": "queued"}


def _scim(lifecycle, deletions, delete_action="disable") -> ScimUsers:
    return ScimUsers(
        lifecycle=lifecycle,
        users=MemoryUserDirectory(),
        source_issuer="gateway:corp-sso",
        deletions=deletions,
        delete_action=delete_action,
    )


@pytest.mark.asyncio
async def test_a_delete_deprovisions_and_erases_nothing_by_default(caplog):
    lifecycle, deletions = _Lifecycle(), _Deletions()

    with caplog.at_level(logging.INFO, logger="inqtrix.auth.scim"):
        status = await _scim(lifecycle, deletions).delete(USER)

    assert status is UserLifecycleStatus.UPDATED
    assert lifecycle.calls == [("deprovision", "default", USER)]
    assert deletions.started == []
    assert "outcome=deprovisioned" in caplog.text
    assert str(USER) not in caplog.text


@pytest.mark.asyncio
async def test_a_delete_with_erase_starts_the_erasure_as_provisioning():
    lifecycle, deletions = _Lifecycle(), _Deletions()

    status = await _scim(lifecycle, deletions, "erase").delete(USER)

    assert status is UserLifecycleStatus.UPDATED
    assert lifecycle.calls[1] == (
        "begin_erasure",
        "default",
        USER,
        ProvisioningActor(),
        SCIM_ERASE_MAX_PER_HOUR,
    )
    ((user_id, actor, tenant_id, operation_id),) = deletions.started
    assert (user_id, actor, tenant_id) == (USER, ProvisioningActor(), "default")
    assert operation_id.startswith("del_")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "logged"),
    [
        (UserLifecycleStatus.LIMITED, "outcome=scim_erase_limited"),
        (UserLifecycleStatus.REFUSED, "outcome=erasure_refused"),
    ],
    ids=["limited", "refused"],
)
async def test_an_erasure_that_cannot_start_leaves_the_account_deactivated(
    caplog, outcome, logged
):
    lifecycle, deletions = _Lifecycle(erasure=outcome), _Deletions()

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.scim"):
        status = await _scim(lifecycle, deletions, "erase").delete(USER)

    # For SCIM the resource is gone either way: the answer stays the one of
    # the deprovisioning, and the reason is visible in the log.
    assert status is UserLifecycleStatus.UPDATED
    assert deletions.started == []
    assert logged in caplog.text
    assert str(USER) not in caplog.text


@pytest.mark.asyncio
async def test_a_delete_that_finds_no_account_starts_nothing():
    lifecycle, deletions = _Lifecycle(deprovision=UserLifecycleStatus.NOT_FOUND), _Deletions()

    status = await _scim(lifecycle, deletions, "erase").delete(USER)

    assert status is UserLifecycleStatus.NOT_FOUND
    assert lifecycle.calls == [("deprovision", "default", USER)]
    assert deletions.started == []


def test_erase_needs_the_deletion_machine():
    with pytest.raises(ValueError, match="erase"):
        _scim(_Lifecycle(), None, "erase")


class _FailingDeletions(_Deletions):
    async def start_user_erasure(self, user_id, *, actor, tenant_id, operation_id):
        raise RuntimeError(f"queue down for {user_id}")


@pytest.mark.asyncio
async def test_an_erasure_that_fails_to_start_leaves_the_account_marked_for_erasure(caplog):
    # The account is already marked for erasure; an administrator resumes it.
    lifecycle = _Lifecycle()

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.scim"):
        status = await _scim(lifecycle, _FailingDeletions(), "erase").delete(USER)

    assert status is UserLifecycleStatus.UPDATED
    assert "outcome=scim_erase_start_failed" in caplog.text
    assert "marked for erasure" in caplog.text
    assert str(USER) not in caplog.text


@pytest.mark.asyncio
async def test_any_other_result_of_the_erasure_start_has_a_fixed_class(caplog):
    lifecycle, deletions = _Lifecycle(erasure=UserLifecycleStatus.NOT_FOUND), _Deletions()

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.scim"):
        await _scim(lifecycle, deletions, "erase").delete(USER)

    assert "outcome=scim_erase_not_started" in caplog.text
    assert "status=not_found" in caplog.text


def test_every_class_the_gateway_and_scim_lines_name_is_a_gateway_outcome():
    # One vocabulary: the class in a log line is a value of GatewayOutcome.
    import re
    from pathlib import Path
    from typing import get_args

    import inqtrix
    from inqtrix.auth.gateway import GatewayOutcome

    package = Path(inqtrix.__file__).parent
    sources = [
        package / "auth" / "gateway.py",
        package / "auth" / "scim.py",
        package / "server" / "routers" / "scim.py",
        package.parent / "inqtrix_web_gateway" / "identity_edge.py",
    ]
    named = {
        name
        for source in sources
        for name in re.findall(r"outcome=([a-z_]+)", source.read_text(encoding="utf-8"))
    }

    assert named
    assert named <= set(get_args(GatewayOutcome)), sorted(named - set(get_args(GatewayOutcome)))
