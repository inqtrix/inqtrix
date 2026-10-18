"""Prompt-template CRUD (``/v1/prompt-templates*``).

The server half of the prompt library: list returns the caller's
visible set (owned plus accepted shared-in; ownerless only in unscoped
deployments) with the additive
``access`` annotation, writes follow the shared owned-resource rule
(edit grant for updates, owner-only deletion), and a deleted
template's shares are revoked in the same breath.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request

from inqtrix.agents.report_requirement import compose_report_requirement
from inqtrix.auth.permissions import (
    AccessMode,
    ResourceAccess,
    SharePermission,
)
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.content.prompt_templates import (
    PromptTemplateConflict,
    PromptTemplateNotFound,
    PromptTemplateRecord,
)
from inqtrix.services.prompt_template_service import (
    PromptTemplateValidationError,
)
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

_SCOPED_KINDS = frozenset({"oidc_session", "pat"})


def _template_payload(
    record: PromptTemplateRecord,
    *,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One template on the wire.

    ``composed_chars`` is the size this template takes as an attached
    rule in a result requirement — measured on the block
    :func:`compose_report_requirement` builds from it (label folded,
    body trimmed, frame markers defused), never by a second formula. A
    client sums these plus the separators to pre-check the published
    ``max_composed_chars`` ceiling without re-implementing the
    composition (and without counting UTF-16 units where the server
    counts code points).
    """
    return {
        "id": record.id,
        "title": record.title,
        "label": record.label,
        "category": record.category,
        "content_markdown": record.content_markdown,
        "composed_chars": len(
            compose_report_requirement(
                rules=[(record.label, record.content_markdown)]
            )
        ),
        "visibility": dict(record.visibility),
        "include_in_autocomplete": record.include_in_autocomplete,
        "revision": record.revision,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        **({"access": access} if access is not None else {}),
    }


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the prompt-template routes against the container.

    Raises:
        RuntimeError: When called without a wired template service —
            registration is a composition decision, not a runtime
            fallback.
    """
    service = container.prompt_template_service
    if service is None:
        raise RuntimeError(
            "build_router(prompt_templates) requires a wired template "
            "service."
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency

    async def _parsed_body(req: Request):
        try:
            body = await req.json()
        except Exception:
            return None
        return body if isinstance(body, dict) else None

    @router.get("/v1/prompt-templates")
    async def list_templates(
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """The caller's visible templates, newest first.

        The list is also the lazy seeding chokepoint: a scoped
        user's FIRST listing installs the stock prompts atomically —
        once, ever — before the visible set is read.
        """
        await service.ensure_default_templates(
            tenant_id=principal.tenant_id,
            visible_to=visible_to,
        )
        records = await service.list_visible(
            tenant_id=principal.tenant_id,
            visible_to=visible_to,
        )
        return {
            "object": "list",
            "data": [
                _template_payload(record, access=access.as_dict())
                for record, access in records
            ],
        }

    @router.post("/v1/prompt-templates", status_code=201)
    async def create_template(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """Create one template; scoped principals own what they create."""
        body = await _parsed_body(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            record = await service.create(
                body,
                tenant_id=principal.tenant_id,
                owner_user_id=(
                    principal.user_id
                    if principal.kind in _SCOPED_KINDS
                    else None
                ),
            )
        except PromptTemplateValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        access = ResourceAccess(
            AccessMode.OWNER
            if principal.kind in _SCOPED_KINDS
            else AccessMode.UNSCOPED
        )
        return _template_payload(record, access=access.as_dict())

    @router.put("/v1/prompt-templates/{template_id}")
    async def update_template(
        template_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Replace the writable fields (edit grant required).

        ``expected_revision`` is mandatory; a stale value yields 409.
        """
        body = await _parsed_body(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        raw_expected = body.get("expected_revision")
        if (
            isinstance(raw_expected, bool)
            or not isinstance(raw_expected, int)
            or raw_expected < 1
        ):
            return error_response(
                400,
                "Field 'expected_revision' must be a positive integer",
                "invalid_request_error",
            )
        try:
            record = await service.update(
                template_id,
                body,
                tenant_id=principal.tenant_id,
                expected_revision=raw_expected,
                visible_to=visible_to,
            )
        except PromptTemplateValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except PromptTemplateConflict as exc:
            return error_response(
                409,
                "The template has changed in the meantime",
                "conflict",
                current_revision=exc.current_revision,
            )
        except PromptTemplateNotFound:
            return error_response(
                404, "Template not found", "not_found"
            )
        mode = (
            AccessMode.OWNER
            if record.owner_user_id == principal.user_id
            else AccessMode.UNSCOPED
            if record.owner_user_id is None
            else AccessMode.SHARED
        )
        access = ResourceAccess(
            mode,
            SharePermission.EDIT if mode is AccessMode.SHARED else None,
        )
        return _template_payload(record, access=access.as_dict())

    @router.delete("/v1/prompt-templates/{template_id}", status_code=204)
    async def delete_template(
        template_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Delete one template (owner-only) and revoke its shares."""
        try:
            await service.delete(
                template_id,
                tenant_id=principal.tenant_id,
                visible_to=visible_to,
            )
        except PromptTemplateNotFound:
            return error_response(
                404, "Template not found", "not_found"
            )

    return router
