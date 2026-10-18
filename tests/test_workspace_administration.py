"""Startup-boundary tests for workspace-share reconciliation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from inqtrix.services.workspace_administration import (
    ensure_workspace_share_reconciliation,
)


def _application() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            workspace_share_reconciliation_ready=False,
            workspace_share_reconciliation_lock=asyncio.Lock(),
        )
    )


@pytest.mark.asyncio
async def test_concurrent_readiness_reconciles_restricted_shares_once() -> None:
    application = _application()
    calls: list[str] = []

    async def reconcile(*, tenant_id: str) -> int:
        calls.append(tenant_id)
        await asyncio.sleep(0)
        return 2

    workspace_admin = SimpleNamespace(reconcile_workspace_shares=reconcile)

    results = await asyncio.gather(
        ensure_workspace_share_reconciliation(
            application,
            workspace_admin,
            tenant_id="default",
        ),
        ensure_workspace_share_reconciliation(
            application,
            workspace_admin,
            tenant_id="default",
        ),
    )

    assert calls == ["default"]
    assert sorted(results) == [0, 2]
    assert application.state.workspace_share_reconciliation_ready is True


@pytest.mark.asyncio
async def test_failed_reconciliation_keeps_startup_boundary_closed() -> None:
    application = _application()

    async def reconcile(*, tenant_id: str) -> int:
        raise ConnectionError(f"{tenant_id} unavailable")

    workspace_admin = SimpleNamespace(reconcile_workspace_shares=reconcile)

    with pytest.raises(ConnectionError, match="default unavailable"):
        await ensure_workspace_share_reconciliation(
            application,
            workspace_admin,
            tenant_id="default",
        )

    assert application.state.workspace_share_reconciliation_ready is False
