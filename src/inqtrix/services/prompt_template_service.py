"""Prompt-template CRUD with live owner/direct-share authorization.

Reads need view, updates need edit, and deletion stays owner-only. No grant
map crosses a request boundary; each decision resolves the current share row.
Updates require the integer revision the caller loaded. A stale revision
raises :class:`PromptTemplateConflict` (HTTP 409); there is no unconditional
last-write-wins path.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Mapping

from inqtrix.auth.permissions import AccessMode, ResourceAccess, SharePermission
from inqtrix.content.default_prompt_seed import DEFAULT_PROMPT_SEEDS
from inqtrix.content.prompt_templates import (
    TEMPLATE_CATEGORIES,
    PromptTemplateConflict,
    PromptTemplateNotFound,
    PromptTemplateRecord,
    PromptTemplateRepository,
    new_template_id,
)

__all__ = [
    "PromptTemplateService",
    "PromptTemplateValidationError",
    "PromptTemplateConflict",
]

if TYPE_CHECKING:
    from inqtrix.auth.shares import ShareFacts
    from inqtrix.auth.permissions import AuthorizationService
    from inqtrix.auth.principal import UserContext
    from inqtrix.user_events import ResourceInvalidator


class PromptTemplateValidationError(ValueError):
    """Raised for client-payload problems (maps to HTTP 400)."""


def _validated_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the writable template fields."""
    title = str(payload.get("title", "") or "").strip()
    label = str(payload.get("label", "") or "").strip()
    content = str(payload.get("content_markdown", "") or "")
    if not title:
        raise PromptTemplateValidationError("Field 'title' is required")
    if not label:
        raise PromptTemplateValidationError("Field 'label' is required")
    if not content.strip():
        raise PromptTemplateValidationError(
            "Field 'content_markdown' is required"
        )
    category = payload.get("category")
    if category is not None:
        category = str(category)
        if category not in TEMPLATE_CATEGORIES:
            raise PromptTemplateValidationError(
                f"Field 'category' must be one of {', '.join(TEMPLATE_CATEGORIES)}"
            )
    visibility = payload.get("visibility") or {}
    if not isinstance(visibility, dict):
        raise PromptTemplateValidationError(
            "Field 'visibility' must be an object"
        )
    include = payload.get("include_in_autocomplete", True)
    if not isinstance(include, bool):
        raise PromptTemplateValidationError(
            "Field 'include_in_autocomplete' must be a boolean"
        )
    return {
        "title": title,
        "label": label,
        "category": category,
        "content_markdown": content,
        "visibility": dict(visibility),
        "include_in_autocomplete": include,
    }


class PromptTemplateService:
    """Application service over the prompt-template repository.

    Args:
        repository: The persistence backend.
        durable: Whether *repository* survives server restarts. Drives
            ``features.prompt_templates`` in the capability manifest:
            a browser must not adopt a VOLATILE store as its sync
            truth — after a restart the empty store would read as
            "everything deleted". The routes stay mounted regardless
            (API consumers may use a memory-backed deployment
            knowingly).
    """

    def __init__(
        self,
        *,
        repository: PromptTemplateRepository,
        authorization: "AuthorizationService",
        durable: bool = True,
        invalidator: "ResourceInvalidator | None" = None,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._durable = durable
        self._invalidator = invalidator

    @property
    def durable(self) -> bool:
        """Whether templates survive a server restart."""
        return self._durable

    async def create(
        self,
        payload: Mapping[str, Any],
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
    ) -> PromptTemplateRecord:
        """Create one template; scoped principals own what they create."""
        now = time.time()
        record = PromptTemplateRecord(
            id=new_template_id(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_at=now,
            updated_at=now,
            **_validated_fields(payload),
        )
        stored = await self._repository.create(record)
        await self._invalidate(stored)
        return stored

    async def ensure_default_templates(
        self,
        *,
        tenant_id: str,
        visible_to: "UserContext | None",
    ) -> bool:
        """Seed the stock prompts once per scoped user, lazily.

        Called by the LIST route before listing: the client hydrates
        templates on app start, so this fires right after any first
        login — every auth mode including the very first admin, with no
        auth-layer coupling. Unscoped deployments (no user identity)
        never seed; the demo mode covers that showcase. Returns whether
        templates were inserted.
        """
        if visible_to is None:
            return False
        user_id = visible_to.principal.user_id
        if user_id is None:
            return False
        now = time.time()
        records = [
            PromptTemplateRecord(
                id=new_template_id(),
                tenant_id=tenant_id,
                owner_user_id=user_id,
                created_at=now,
                updated_at=now,
                # The shared validator also guards the generated seed
                # module: a broken generation fails LOUDLY here instead
                # of storing a defective template.
                **_validated_fields(seed),
            )
            for seed in DEFAULT_PROMPT_SEEDS
        ]
        seeded = await self._repository.seed_default_templates(
            records, tenant_id=tenant_id, user_id=user_id
        )
        if seeded:
            for record in records:
                await self._invalidate(record)
        return seeded

    async def list_visible(
        self,
        *,
        tenant_id: str,
        visible_to: "UserContext | None" = None,
    ) -> list[tuple[PromptTemplateRecord, ResourceAccess]]:
        """Visible templates with the public authoritative access annotation."""
        optimized = getattr(self._repository, "list_visible_for_user", None)
        if callable(optimized):
            return await optimized(
                tenant_id=tenant_id,
                actor_user_id=(
                    visible_to.principal.user_id
                    if visible_to is not None
                    else None
                ),
            )
        visible: list[tuple[PromptTemplateRecord, ResourceAccess]] = []
        for record in await self._repository.list_for_tenant(
            tenant_id=tenant_id
        ):
            try:
                access = await self._access(record, visible_to)
            except PromptTemplateNotFound:
                continue
            visible.append((record, access))
        return visible

    async def update(
        self,
        template_id: str,
        payload: Mapping[str, Any],
        *,
        tenant_id: str,
        expected_revision: int,
        visible_to: "UserContext | None" = None,
    ) -> PromptTemplateRecord:
        """Replace the writable fields (edit grant required).

        *expected_revision* is the mandatory compare-and-swap precondition.
        """
        current = await self._repository.get(template_id, tenant_id=tenant_id)
        await self._access(current, visible_to, minimum=SharePermission.EDIT)
        updated = PromptTemplateRecord(
            id=current.id,
            tenant_id=current.tenant_id,
            owner_user_id=current.owner_user_id,
            revision=current.revision,
            created_at=current.created_at,
            updated_at=time.time(),
            **_validated_fields(payload),
        )
        stored = await self._repository.update(
            updated,
            expected_revision=expected_revision,
            actor_user_id=(
                visible_to.principal.user_id if visible_to is not None else None
            ),
        )
        await self._invalidate(stored)
        return stored

    async def delete(
        self,
        template_id: str,
        *,
        tenant_id: str,
        visible_to: "UserContext | None" = None,
    ) -> None:
        """Delete one template (owner-only; shares never delete)."""
        current = await self._repository.get(template_id, tenant_id=tenant_id)
        access = await self._access(current, visible_to)
        if access.mode is AccessMode.SHARED:
            raise PromptTemplateNotFound(template_id)
        await self._repository.delete(
            template_id,
            tenant_id=tenant_id,
            actor_user_id=(
                visible_to.principal.user_id if visible_to is not None else None
            ),
        )
        if self._invalidator is not None and not getattr(
            self._repository, "atomic_resource_effects", False
        ):
            await self._invalidator.revoke_deleted(
                tenant_id=current.tenant_id,
                owner_user_id=current.owner_user_id,
                resource_type="prompt_template",
                resource_id=current.id,
                scope="prompt_templates",
                actor_user_id=(
                    visible_to.principal.user_id
                    if visible_to is not None
                    else None
                ),
            )

    async def _invalidate(self, record: PromptTemplateRecord) -> None:
        """Publish fallback effects only for volatile repositories."""
        if self._invalidator is None or getattr(
            self._repository, "atomic_resource_effects", False
        ):
            return
        await self._invalidator.invalidate(
            tenant_id=record.tenant_id,
            owner_user_id=record.owner_user_id,
            resource_type="prompt_template",
            resource_id=record.id,
            scope="prompt_templates",
        )

    async def _access(
        self,
        record: PromptTemplateRecord,
        visible_to: "UserContext | None",
        *,
        minimum: SharePermission = SharePermission.VIEW,
    ) -> ResourceAccess:
        """Resolve current owner/direct-share access for one record."""
        if visible_to is None:
            if record.owner_user_id is None:
                return ResourceAccess(AccessMode.UNSCOPED)
            raise PromptTemplateNotFound(record.id)
        access = await self._authorization.resolve_resource_access(
            visible_to.principal,
            owner_user_id=record.owner_user_id,
            resource_tenant_id=record.tenant_id,
            resource_type="prompt_template",
            resource_id=record.id,
            minimum=minimum,
        )
        if access is None:
            raise PromptTemplateNotFound(record.id)
        return access

    async def owner_user_id(
        self, tenant_id: str, template_id: str
    ) -> uuid.UUID | None:
        """Owner lookup for the share layer (``None`` = unshareable)."""
        try:
            record = await self._repository.get(
                template_id, tenant_id=tenant_id
            )
        except PromptTemplateNotFound:
            return None
        return record.owner_user_id

    async def title(self, tenant_id: str, template_id: str) -> str | None:
        """Title lookup for the share surface (``None`` = absent, so the
        share lifecycle views skip it)."""
        try:
            record = await self._repository.get(
                template_id, tenant_id=tenant_id
            )
        except PromptTemplateNotFound:
            return None
        return record.title

    async def share_facts(
        self, tenant_id: str, template_ids: Sequence[str]
    ) -> dict[str, "ShareFacts"]:
        """Owner and title of many templates in one read (share layer)."""
        return await self._repository.share_facts(template_ids, tenant_id=tenant_id)
