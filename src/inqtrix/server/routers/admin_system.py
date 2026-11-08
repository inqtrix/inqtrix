"""Instance-admin system endpoints: runtime and run bindings."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.services.request_parsing import error_response
from inqtrix.services.system_runtime import system_runtime_payload_checked

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger(__name__)

_BINDING_LIST_LIMIT = 1000
"""Runs named per request; ``runs_total`` and ``truncated`` say when more match."""

_CONTRACT_MAX = 2**31 - 1
"""The highest contract version the run table can store (a 32-bit integer)."""

_MODE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""What a mode name looks like: the id of a registered algorithm."""


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the instance-admin system surface against the container."""
    router = APIRouter()
    provider = container.auth_provider
    principal_dep = container.principal_dependency

    @router.get("/v1/admin/system/runtime")
    async def runtime(request: Request):
        """Return the sanitized runtime categories for an instance admin."""
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        return await system_runtime_payload_checked(container)

    @router.get("/v1/admin/system/run-bindings")
    async def run_bindings(request: Request):
        """Open runs by workflow binding, read before raising a contract and on the way back.

        ``below`` names the open runs a raised contract ends; ``above`` those a
        rolled-back worker cannot run, which stop its claims while they wait.
        """
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        bounds: dict[str, int | None] = {}
        for name in ("below", "above"):
            raw = request.query_params.get(name)
            if raw is None:
                bounds[name] = None
                continue
            try:
                bounds[name] = int(raw)
            except ValueError:
                bounds[name] = 0
            if not 1 <= bounds[name] <= _CONTRACT_MAX:
                return error_response(
                    400,
                    f"{name} must be a contract version from 1 to {_CONTRACT_MAX}",
                    "invalid_request_error",
                )
        overview = await container.run_store.abinding_overview(
            mode=request.query_params.get("mode") or None,
            below=bounds["below"],
            above=bounds["above"],
            limit=_BINDING_LIST_LIMIT,
        )
        return {"contracts": container.registry.workflow_contracts(), **overview}

    @router.post("/v1/admin/system/run-bindings/cancel")
    async def cancel_run_bindings(request: Request):
        """Cancel the open runs of one mode that this server cannot run.

        Those are the runs bound above this server's contract for the mode, or
        every open run of a mode it does not register: no worker of this
        version takes them, and each one stops the run claims of the worker
        that claims it. A run bound below stays: with a stored checkpoint it
        ends at its next claim, without one the claim binds it to the current
        contract.
        """
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        try:
            body = await request.json()
        except ValueError:
            body = None
        mode = body.get("mode") if isinstance(body, dict) else None
        if not isinstance(mode, str) or _MODE_NAME.fullmatch(mode) is None:
            return error_response(
                400,
                "mode must be a mode name such as agent_kernel",
                "invalid_request_error",
            )
        principal, _session, _mirror = resolved
        contracts = container.registry.workflow_contracts()
        outcome = await container.run_store.acancel_outside_binding(
            mode=mode,
            workflow_contracts=contracts,
            actor_user_id=principal.user_id,
        )
        log.warning(
            "Instance admin %s cancelled the open runs of mode %s this server cannot "
            "run (its contract: %s): %d ended, %d asked to stop.",
            pseudonymous_log_reference("usr", principal.user_id),
            mode,
            contracts.get(mode, "not registered"),
            outcome["cancelled"],
            outcome["cancel_requested"],
        )
        return {"mode": mode, "server_contract": contracts.get(mode), **outcome}

    return router
