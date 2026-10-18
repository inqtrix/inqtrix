"""Live authorization for dependencies pinned into a native run request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.sync_bridge import run_coro_sync

if TYPE_CHECKING:
    from inqtrix.auth.directory import UserDirectory
    from inqtrix.auth.permissions import AuthorizationService
    from inqtrix.auth.principal import Principal
    from inqtrix.core.results import RunRequest
    from inqtrix.services.knowledge_service import KnowledgeService
    from inqtrix.services.skill_service import SkillService


class ExecutionDependencyAuthorizer:
    """Recheck the actor, pinned collections and skill revisions at every safepoint."""

    def __init__(
        self,
        *,
        authorization: "AuthorizationService",
        knowledge_service: "KnowledgeService | None",
        skill_service: "SkillService | None",
        user_lookup: "UserDirectory | None",
    ) -> None:
        self._authorization = authorization
        self._knowledge_service = knowledge_service
        self._skill_service = skill_service
        self._user_lookup = user_lookup
        """Live actor directory. Owned here rather than by each caller so
        the actor probe rides the same safepoint pass and event loop as
        the permission resolve instead of a separate per-call loop of its
        own."""

    def check(
        self,
        request: "RunRequest",
        principal: "Principal | None",
    ) -> None:
        """Synchronously fail when the actor or any pinned dependency is gone."""
        run_coro_sync(self._check_async(request, principal))

    async def _check_async(
        self,
        request: "RunRequest",
        principal: "Principal | None",
    ) -> None:
        if principal is not None and principal.user_id is not None:
            if self._user_lookup is None:
                raise AuthorizationRevoked(
                    "execution has no live user lookup"
                )
            user = await self._user_lookup.find_by_user_id(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
            )
            if user is None or user.disabled_at is not None:
                raise AuthorizationRevoked(
                    "effective actor is missing or disabled"
                )
        visible_to = (
            await self._authorization.resolve_user_context(principal)
            if principal is not None
            else None
        )
        collection_ids = [
            str(item)
            for item in (
                request.knowledge_filters.get("collection_ids") or []
            )
        ]
        if collection_ids:
            if self._knowledge_service is None:
                raise AuthorizationRevoked(
                    "pinned knowledge collections cannot be authorized"
                )
            try:
                await self._knowledge_service.assert_collections_visible(
                    collection_ids,
                    visible_to=visible_to,
                )
            except Exception as exc:
                raise AuthorizationRevoked(
                    "access to a pinned knowledge collection was revoked"
                ) from exc

        if request.skill_ids:
            if self._skill_service is None:
                raise AuthorizationRevoked(
                    "pinned skills cannot be authorized"
                )
            tenant_id = principal.tenant_id if principal is not None else "default"
            for skill_id in request.skill_ids:
                try:
                    record, _access = await self._skill_service.get_visible(
                        skill_id,
                        tenant_id=tenant_id,
                        visible_to=visible_to,
                    )
                except Exception as exc:
                    raise AuthorizationRevoked(
                        f"access to pinned skill {skill_id} was revoked"
                    ) from exc
                if request.skill_revisions.get(skill_id) != record.revision:
                    raise AuthorizationRevoked(
                        f"pinned skill {skill_id} changed revision"
                    )


class CollectionEditAuthorizer:
    """Live requester and edit-share check for a reindex job."""

    def __init__(
        self,
        *,
        authorization: "AuthorizationService",
        knowledge_service: "KnowledgeService",
        user_lookup: "UserDirectory | None",
    ) -> None:
        self._authorization = authorization
        self._knowledge_service = knowledge_service
        self._user_lookup = user_lookup

    def check(
        self,
        collection_id: str,
        principal: "Principal | None",
    ) -> None:
        """Fail closed if requester or current collection edit access is gone."""
        run_coro_sync(self._check_async(collection_id, principal))

    async def _check_async(
        self,
        collection_id: str,
        principal: "Principal | None",
    ) -> None:
        if principal is not None and principal.user_id is not None:
            if self._user_lookup is None:
                raise AuthorizationRevoked(
                    "reindex execution has no live user lookup"
                )
            user = await self._user_lookup.find_by_user_id(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
            )
            if user is None or user.disabled_at is not None:
                raise AuthorizationRevoked(
                    "reindex requester is missing or disabled"
                )
        try:
            visible_to = (
                await self._authorization.resolve_user_context(principal)
                if principal is not None
                else None
            )
            collection = (
                await self._knowledge_service.knowledge.store.get_collection(
                    collection_id
                )
            )
            from inqtrix.auth.permissions import SharePermission

            await self._knowledge_service.collection_access(
                collection,
                visible_to,
                minimum=SharePermission.EDIT,
            )
        except AuthorizationRevoked:
            raise
        except Exception as exc:
            raise AuthorizationRevoked(
                "reindex requester lost collection edit access"
            ) from exc
