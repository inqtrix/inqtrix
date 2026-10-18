"""Instance-admin knowledge-maintenance endpoints.

A thin, session-only instance-admin surface for store hygiene: orphan-vector
reconciliation and the same crash-convergent generation-retention sweep used
by the indexing worker. Gated by the shared
:func:`require_instance_admin` guard like every other admin surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.knowledge.stores.ports import GenerationPruneError
from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the knowledge-maintenance surface against the container."""
    router = APIRouter()
    provider = container.auth_provider
    principal_dep = container.principal_dependency

    @router.post("/v1/admin/knowledge/reconcile")
    async def reconcile(request: Request):
        """Delete orphan vectors whose canonical Postgres rows are gone.

        Returns the count + details of removed document groups. Only the
        Postgres-canonical tier owns this reconcile; on other stores it is a
        visible 409 rather than a silent no-op.
        """
        _resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        service = container.knowledge_service
        store = getattr(getattr(service, "knowledge", None), "store", None)
        reconcile_fn = getattr(store, "reconcile_orphans", None)
        if reconcile_fn is None:
            return error_response(
                409,
                ("The current knowledge store does not support reconciliation (Postgres "
                "tier only)."),
                "conflict",
            )
        return await reconcile_fn()

    @router.post("/v1/admin/knowledge/generations:prune")
    async def prune_generations(request: Request):
        """Run the same crash-convergent retention sweep as the worker."""
        _resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        service = container.knowledge_service
        store = getattr(getattr(service, "knowledge", None), "store", None)
        if service is None or not bool(
            getattr(store, "supports_safe_reindex", False)
        ):
            return error_response(
                409,
                "The current knowledge store does not support generation retention.",
                "conflict",
            )
        try:
            return await service.prune_expired_generations_all()
        except GenerationPruneError as exc:
            return error_response(
                503,
                ("At least one generation could not be fully cleaned up and remains "
                "visibly retryable."),
                "generation_cleanup_failed",
                generation_ids=list(exc.generation_ids),
            )

    return router
