"""HTTP gate that keeps business routes behind the database contract.

The gate is pure ASGI because it only reads the request path and application
state. A ``BaseHTTPMiddleware`` task hop cancels downstream streaming work
when a browser closes an SSE response; that cancellation can also interrupt
async database-driver termination and return a dead connection to the pool.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.datastructures import State
from starlette.types import ASGIApp, Receive, Scope, Send

from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

_DATABASE_GATE_EXEMPT_PATHS = frozenset({"/health", "/readyz", "/metrics"})


class DatabaseContractGateMiddleware:
    """Reject product HTTP traffic until the database contract is ready."""

    def __init__(self, app: ASGIApp, *, state: State) -> None:
        self.app = app
        self._state = state

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] == "http"
            and scope.get("path") not in _DATABASE_GATE_EXEMPT_PATHS
            and not self._state.database_contract_ready
        ):
            response = error_response(
                503,
                ("The database schema and roles do not match the expected state. The "
                "migration job must finish successfully."),
                "database_not_ready",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_database_contract_gate(
    app: FastAPI,
    *,
    container: "AppContainer",
) -> None:
    """Block product requests until the runtime DB contract is verified.

    Health/readiness and metrics remain reachable so an orchestrator can
    diagnose and recover a failed rollout. ``/readyz`` refreshes the cached
    state, allowing a corrected database to admit traffic without weakening
    the per-request boundary.
    """
    app.state.database_contract_ready = (
        container.settings.storage.backend != "postgres"
    )
    share_reconciliation_required = (
        container.settings.sharing.restrict_to_workspace_members
    )
    app.state.workspace_share_reconciliation_ready = (
        not share_reconciliation_required
    )
    app.state.workspace_share_reconciliation_lock = asyncio.Lock()
    app.add_middleware(DatabaseContractGateMiddleware, state=app.state)
