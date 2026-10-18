"""Account-preferences endpoints (project tier).

The singleton per-user settings surface: ``GET`` / ``PUT
/v1/account/preferences``. The row is keyed by the authenticated principal's
canonical ``users.id`` UUID, so a scoped caller can address only their own row.
Anonymous and static-key principals have no user row and receive 404. GET also
returns 404 when the user has never saved preferences (the frontend then keeps
its own default theme/locale).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request

from inqtrix.auth.principal import Principal
from inqtrix.project.account_preferences_ports import AccountPreferences
from inqtrix.services.account_preferences_service import (
    AccountPreferencesValidationError,
)
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def _payload(p: AccountPreferences) -> dict[str, Any]:
    return {
        "contrast_mode": p.contrast_mode, "locale": p.locale, "theme": p.theme,
        "theme_preset": p.theme_preset, "user_bubble_tone": p.user_bubble_tone,
        "enable_agent_memory": p.enable_agent_memory,
        "chat_model_tier": p.chat_model_tier,
        "agent_model_tier": p.agent_model_tier,
        "updated_at": p.updated_at,
    }


def _tier(body: dict[str, Any], key: str) -> str:
    """Read a model-tier field without the ``str(...)`` cast the other fields use.

    ``str(None)`` is the string ``'None'`` — a value the service would reject
    with a confusing message, and one a client sends simply by omitting the
    key or sending JSON ``null``. Both mean "no preference" here, which is the
    empty string.
    """
    value = body.get(key)
    return "" if value is None else str(value)


def build_router(container: "AppContainer") -> APIRouter:
    service = container.account_preferences_service
    if service is None:
        raise RuntimeError("build_router(account_preferences) requires a wired service.")
    router = APIRouter()
    principal_dep = container.principal_dependency

    @router.get("/v1/account/preferences")
    async def get_preferences(principal: Principal = Depends(principal_dep)):
        if principal.user_id is None:
            return error_response(404, "Not found", "not_found")
        prefs = await service.get_preferences(user_id=principal.user_id)
        if prefs is None:
            return error_response(404, "No preferences saved", "not_found")
        return _payload(prefs)

    @router.put("/v1/account/preferences")
    async def save_preferences(req: Request, principal: Principal = Depends(principal_dep)):
        if principal.user_id is None:
            return error_response(404, "Not found", "not_found")
        body = await _json_object(req)
        if not isinstance(body, dict):
            return error_response(400, "Invalid body", "invalid_request_error")
        if not isinstance(body.get("updated_at"), (int, float)):
            return error_response(400, "updated_at is required", "invalid_request_error")
        try:
            prefs = await service.save_preferences(
                user_id=principal.user_id,
                contrast_mode=str(body.get("contrast_mode", "standard")),
                locale=str(body.get("locale", "en")),
                theme=str(body.get("theme", "system")),
                theme_preset=str(body.get("theme_preset", "standard")),
                user_bubble_tone=str(body.get("user_bubble_tone", "gray")),
                enable_agent_memory=bool(body.get("enable_agent_memory", False)),
                chat_model_tier=_tier(body, "chat_model_tier"),
                agent_model_tier=_tier(body, "agent_model_tier"),
                updated_at=float(body["updated_at"]),
            )
        except AccountPreferencesValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return _payload(prefs)

    return router


async def _json_object(req: Request) -> Any:
    try:
        return await req.json()
    except Exception:
        return None
