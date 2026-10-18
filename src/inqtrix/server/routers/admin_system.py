"""Instance-admin system runtime endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.services.system_runtime import system_runtime_payload_checked

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the read-only system runtime surface against the container."""
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

    return router
