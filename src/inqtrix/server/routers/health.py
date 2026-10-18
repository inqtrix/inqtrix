"""Open discovery endpoints: ``/health`` and ``/v1/models``.

Deliberately unauthenticated (no principal dependency) so Kubernetes
probes and model-discovery clients keep working without credentials.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")

# How long the product gate keeps its state through CONSECUTIVE
# "unavailable" probe readings before failing closed. Well above one
# load spike (a single probe timeout lasts one healthcheck cycle of a few
# seconds), well below "indefinitely": past this, sustained
# unreachability is treated as a possible masked contract break.
_UNAVAILABLE_KEEP_OPEN_SECONDS = 120.0


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the health/model-discovery routes against the container."""
    router = APIRouter()
    health_service = container.health_service

    @router.get("/health")
    def health():
        status_code, payload = health_service.health_payload()
        if status_code == 200:
            return payload
        return JSONResponse(status_code=status_code, content=payload)

    @router.get("/readyz")
    async def readyz(request: Request):
        """Readiness: are the stateful dependencies reachable?

        Liveness (``/health``) stays provider-only; THIS is what a
        load balancer keys traffic on — a pod with a dead database or
        queue answers 503 here and drains instead of serving 500s.
        """
        from inqtrix.services.system_runtime import readiness_payload

        status_code, payload = await readiness_payload(container)
        # No default: a payload without the key is shape drift, and a
        # silent fallback here would freeze the gate forever while
        # misattributing the cause. Better a loud 500 on /readyz.
        contract_state = payload["database_contract"]
        if contract_state == "unavailable":
            # An UNREACHABLE database proves nothing about the schema/role
            # contract, so the product gate KEEPS its state: closing it
            # here once turned a single slow 2s probe under a load spike
            # into a full-healthcheck-interval 503 outage for every
            # product route while the system underneath was healthy.
            # BOUNDED, not forever: only Kubernetes drains an unready pod
            # (compose and the launcher keep routing), and a wrong-schema
            # database whose heavy contract probe consistently exceeds
            # its bound would otherwise read "unavailable" indefinitely —
            # after sustained unreachability the gate fails closed:
            # integrity over availability. The share-reconciliation
            # recovery below needs a VERIFIED database and is skipped.
            now = time.monotonic()
            since = getattr(
                request.app.state, "database_contract_unavailable_since", None
            )
            if since is None:
                since = now
                request.app.state.database_contract_unavailable_since = since
            database_ready = bool(
                getattr(request.app.state, "database_contract_ready", False)
            )
            elapsed = now - since
            if database_ready and elapsed > _UNAVAILABLE_KEEP_OPEN_SECONDS:
                database_ready = False
                log.error(
                    "readyz: database continuously unreachable for %.0fs; closing the "
                    "business-route gate (integrity before availability).",
                    elapsed,
                )
            else:
                log.warning(
                    "readyz: database probe unreachable for %.0fs; business-route gate "
                    "retains its state (%s).",
                    elapsed,
                    "open" if database_ready else "closed",
                )
            request.app.state.database_contract_ready = database_ready
            return JSONResponse(status_code=status_code, content=payload)
        request.app.state.database_contract_unavailable_since = None
        database_ready = contract_state in {"ok", "skipped"}
        if not database_ready:
            log.error(
                "readyz: confirmed database contract violation; closing the "
                "business-route gate."
            )
        if (
            database_ready
            and container.settings.sharing.restrict_to_workspace_members
            and not request.app.state.workspace_share_reconciliation_ready
        ):
            from inqtrix.services.workspace_administration import (
                ensure_workspace_share_reconciliation,
            )

            try:
                revoked = await ensure_workspace_share_reconciliation(
                    request.app,
                    container.workspace_admin,
                    tenant_id="default",
                )
                log.warning(
                    "Workspace-Share-Reconciliation completed during database "
                    "contract recovery; revoked=%d.",
                    revoked,
                )
            except Exception as exc:
                database_ready = False
                payload["checks"]["database"] = "unavailable"
                payload["status"] = "not_ready"
                status_code = 503
                log.warning(
                    "Database contract recovered but workspace-share "
                    "reconciliation failed; business routes remain gated: %s",
                    type(exc).__name__,
                )
        request.app.state.database_contract_ready = database_ready
        if status_code == 200:
            return payload
        return JSONResponse(status_code=status_code, content=payload)

    @router.get("/v1/models")
    def list_models():
        return health_service.models_payload()

    return router
