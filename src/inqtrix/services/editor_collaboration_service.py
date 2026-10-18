"""Application service for editor live-collaboration orchestration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, TypeVar, cast

from inqtrix.auth.permissions import AccessMode, SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import encode_cursor
from inqtrix.project.editor_collaboration_ports import (
    CollaborationConflict,
    CollaborationChangeKind,
    CollaborationCommentThread,
    CollaborationDocumentState,
    CollaborationInstanceLease,
    CollaborationLease,
    CollaborationLeaseInvalid,
    CollaborationLoadedState,
    CollaborationPermission,
    CollaborationPersistedCommand,
    CollaborationSnapshot,
    CollaborationSuggestionKind,
    CollaborationSuggestionRejection,
    CollaborationUpdateLookup,
    EditorCollaborationStore,
    PersistCollaborationUpdate,
    PersistedCollaborationUpdate,
)
from inqtrix.project.editor_ports import DocumentNotFound
from inqtrix.services.collaboration_client import (
    CollaborationDecisionResult,
    CollaborationNodeClient,
    CollaborationProjection,
    CollaborationSuggestionResult,
    CollaborationServiceUnavailable,
)
from inqtrix.services.user_erasure import DELETED_ACCOUNT_ISSUER

if TYPE_CHECKING:
    from inqtrix.auth.directory import MirroredUser, UserDirectory
    from inqtrix.project.editor_guest_links import (
        EditorGuestAccess,
        EditorGuestActorProfile,
        EditorGuestLinkStore,
    )
    from inqtrix.services.editor_persistence_service import EditorPersistenceService
    from inqtrix.settings import CollaborationSettings

log = logging.getLogger("inqtrix")

_ROOM_PATTERN = re.compile(
    r"^inqtrix-editor-v1:(?P<document>[A-Za-z0-9_-]{1,160}):g(?P<generation>[1-9][0-9]*)$"
)
_TOKEN_VERSION = "cl1"
_MAX_DECISION_PATCHES = 5_000
_MAX_PROJECTION_CONVERGENCE_ATTEMPTS = 3
_COMMENT_DEADLOCK_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1)
# How many removed suggestions one history entry lists by name; the entry
# always carries the full count, so a longer list is visibly shortened.
_REJECTED_SUGGESTIONS_LISTED = 20
# Upper bound of removal entries read for one history page.
_REJECTION_READ_LIMIT = 1_000
_CommentResult = TypeVar("_CommentResult")
_COLORS = (
    "#2563EB",
    "#059669",
    "#DC2626",
    "#7C3AED",
    "#C2410C",
    "#0F766E",
    "#A21CAF",
    "#4D7C0F",
)


class CollaborationAuthenticationRequired(PermissionError):
    """Raised when a non-cookie principal requests a collaboration lease."""


class CollaborationDocumentTooLarge(ValueError):
    """Raised when conversion would exceed the configured document limit."""


class CollaborationProtocolConflict(RuntimeError):
    """Raised when the client protocol or schema version is incompatible."""


@dataclass(frozen=True)
class CollaborationActivityPage:
    """Presentation-ready activity rows with a raw-history continuation."""

    items: tuple[dict[str, Any], ...]
    next_cursor: int | str | None


class EditorCollaborationService:
    """Coordinates access, leases, Node transformations, and durable storage."""

    def __init__(
        self,
        *,
        store: EditorCollaborationStore,
        documents: "EditorPersistenceService",
        node: CollaborationNodeClient,
        settings: "CollaborationSettings",
        users: "UserDirectory",
        guest_links: "EditorGuestLinkStore | None" = None,
    ) -> None:
        self._store = store
        self._documents = documents
        self._node = node
        self._settings = settings
        self._users = users
        self._guest_links = guest_links
        self._secret = settings.secret.encode("utf-8")

    async def service_available(self) -> bool:
        """Whether the configured sidecar currently reports readiness."""
        return await self._node.available()

    async def ready_instance(self) -> CollaborationInstanceLease | None:
        """Return the stable DB-fenced instance only while Node is ready."""
        first = await self._store.get_current_instance(
            tenant_id=self._settings.tenant_id,
            now=time.time(),
        )
        if first is None or not await self._node.available():
            return None
        current = await self._store.get_current_instance(
            tenant_id=self._settings.tenant_id,
            now=time.time(),
        )
        if current is None or (
            current.instance_id != first.instance_id
            or current.epoch != first.epoch
        ):
            log.warning(
                "Collaboration instance changed while its public readiness "
                "probe was in flight."
            )
            return None
        return current

    async def enable_document(
        self,
        *,
        document_id: str,
        expected_revision: int,
        expected_metadata_revision: int,
        schema_version: int,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> CollaborationDocumentState:
        """Irreversibly convert an owner-only Markdown document atomically."""
        self._require_cookie_principal(principal)
        if schema_version != self._settings.schema_version:
            raise CollaborationProtocolConflict("schema_conflict")
        document = await self._documents.get_document(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        if document.created_by_user_id != principal.user_id:
            raise DocumentNotFound(document_id)
        if len(document.content_markdown.encode("utf-8")) > self._settings.max_document_bytes:
            raise CollaborationDocumentTooLarge(document_id)
        if not await self._node.available():
            raise CollaborationServiceUnavailable(
                "collaboration service is not ready"
            )
        converted = await self._node.convert(
            document_id=document_id,
            markdown=document.content_markdown,
            schema_version=schema_version,
            max_document_bytes=self._settings.max_document_bytes,
        )
        now = time.time()
        snapshot = CollaborationSnapshot(
            document_id=document_id,
            tenant_id=principal.tenant_id,
            generation=1,
            covered_sequence=0,
            state_update=converted.state_update,
            state_vector=converted.state_vector,
            state_hash=converted.state_hash,
            projection_hash=converted.projection_hash,
            schema_version=schema_version,
            schema_hash=converted.schema_hash,
            created_at=now,
        )
        return await self._store.enable_document(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            owner_user_id=cast(uuid.UUID, principal.user_id),
            expected_revision=expected_revision,
            expected_metadata_revision=expected_metadata_revision,
            schema_version=schema_version,
            schema_hash=converted.schema_hash,
            snapshot=snapshot,
            projection_markdown=converted.projection_markdown,
            now=now,
        )

    async def create_session(
        self,
        *,
        document_id: str,
        protocol_version: int,
        schema_version: int,
        current_lease_token: str | None = None,
        rotation_command_id: uuid.UUID | None = None,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        """Issue a short-lived opaque lease for one visible document room."""
        self._require_cookie_principal(principal)
        self._require_versions(protocol_version, schema_version)
        document, access = await self._documents.get_document_with_access(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        if document.content_mode != "collaboration":
            raise CollaborationConflict("mode_conflict")
        if not await self._node.available():
            raise CollaborationServiceUnavailable(
                "collaboration service is not ready"
            )
        permission = cast(
            CollaborationPermission,
            (
                "edit"
                if access.mode is not AccessMode.SHARED
                else cast(SharePermission, access.permission).value
            ),
        )
        now = time.time()
        if current_lease_token is None and rotation_command_id is not None:
            raise ValueError("rotation_command_id requires lease_token")
        previous_lease_id: uuid.UUID | None = None
        if current_lease_token is not None:
            previous = self._decode_token(current_lease_token)
            if previous["tenant_id"] != principal.tenant_id:
                raise CollaborationLeaseInvalid("lease_invalid")
            previous_lease_id = previous["lease_id"]
            if rotation_command_id is None:
                rotation_command_id = uuid.uuid4()
        lease_id = uuid.uuid4()
        expires_at = now + self._settings.lease_ttl_seconds
        token = self._encode_token(
            lease_id=lease_id,
            tenant_id=principal.tenant_id,
            expires_at=expires_at,
        )
        lease = CollaborationLease(
            lease_id=lease_id,
            token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
            tenant_id=principal.tenant_id,
            document_id=document_id,
            generation=document.collaboration_generation,
            user_id=cast(uuid.UUID, principal.user_id),
            permission=permission,
            session_id=cast(str, principal.session_id),
            issued_at=now,
            expires_at=expires_at,
            last_validated_at=now,
            rotation_command_id=rotation_command_id,
            rotated_from_lease_id=previous_lease_id,
        )
        if current_lease_token is None:
            stored_lease = await self._store.issue_lease(
                lease,
                max_active=self._settings.max_sessions_per_user_document,
                max_issued_per_window=self._settings.session_rate_per_minute,
                issued_since=now - 60,
            )
        else:
            stored_lease = await self._store.rotate_lease(
                previous_lease_id=cast(uuid.UUID, previous_lease_id),
                previous_token_hash=hashlib.sha256(
                    current_lease_token.encode("ascii")
                ).hexdigest(),
                replacement=lease,
                max_issued_per_window=self._settings.session_rate_per_minute,
                issued_since=now - 60,
            )
        token = self._encode_token(
            lease_id=stored_lease.lease_id,
            tenant_id=stored_lease.tenant_id,
            expires_at=stored_lease.expires_at,
        )
        if not hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).hexdigest(),
            stored_lease.token_hash,
        ):
            log.error("Collaboration lease token reconstruction failed.")
            raise CollaborationConflict("lease_reconstruction_conflict")
        if stored_lease.expires_at - now <= 1:
            raise CollaborationLeaseInvalid("lease_expired")
        refresh_after = min(
            now + self._settings.token_refresh_seconds,
            stored_lease.expires_at - 0.5,
        )
        return {
            "websocket_path": "/collaboration",
            "room": self.room_name(
                document_id, document.collaboration_generation
            ),
            "lease_token": token,
            "expires_at": stored_lease.expires_at,
            "refresh_after": refresh_after,
            "provider_flush_ms": self._settings.provider_flush_ms,
            **self._published_transport_limits(),
            "access": permission,
            "initial_write_mode": permission,
            "user": {
                "id": str(principal.user_id),
                "name": principal.display_name or principal.email or "User",
                "color": self.user_color(cast(uuid.UUID, principal.user_id)),
            },
            "protocol_version": self._settings.protocol_version,
            "schema_version": self._settings.schema_version,
        }

    async def create_guest_session(
        self,
        *,
        access: "EditorGuestAccess",
        protocol_version: int,
        schema_version: int,
        current_lease_token: str | None = None,
        rotation_command_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Issue a link- and identity-bound lease without a platform account."""
        self._require_versions(protocol_version, schema_version)
        if self._guest_links is None:
            raise CollaborationAuthenticationRequired("guest links unavailable")
        if not await self._node.available():
            raise CollaborationServiceUnavailable(
                "collaboration service is not ready"
            )
        lease_permission = self._guest_lease_permission(access.link.permission)
        now = time.time()
        if current_lease_token is None and rotation_command_id is not None:
            raise ValueError("rotation_command_id requires lease_token")
        previous_lease_id: uuid.UUID | None = None
        if current_lease_token is not None:
            previous = self._decode_token(current_lease_token)
            if previous["tenant_id"] != access.link.tenant_id:
                raise CollaborationLeaseInvalid("lease_invalid")
            previous_lease_id = previous["lease_id"]
            if rotation_command_id is None:
                rotation_command_id = uuid.uuid4()
        lease_id = uuid.uuid4()
        expires_at = min(
            now + self._settings.lease_ttl_seconds,
            access.identity.expires_at,
            access.link.expires_at,
        )
        token = self._encode_token(
            lease_id=lease_id,
            tenant_id=access.link.tenant_id,
            expires_at=expires_at,
        )
        lease = CollaborationLease(
            lease_id=lease_id,
            token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
            tenant_id=access.link.tenant_id,
            document_id=access.link.document_id,
            generation=access.link.generation,
            user_id=None,
            actor_kind="guest",
            guest_identity_id=access.identity.id,
            guest_link_id=access.link.id,
            permission=lease_permission,
            session_id=None,
            issued_at=now,
            expires_at=expires_at,
            last_validated_at=now,
            rotation_command_id=rotation_command_id,
            rotated_from_lease_id=previous_lease_id,
        )
        if current_lease_token is None:
            stored_lease = await self._store.issue_lease(
                lease,
                max_active=self._settings.max_sessions_per_user_document,
                max_issued_per_window=self._settings.session_rate_per_minute,
                issued_since=now - 60,
            )
        else:
            stored_lease = await self._store.rotate_lease(
                previous_lease_id=cast(uuid.UUID, previous_lease_id),
                previous_token_hash=hashlib.sha256(
                    current_lease_token.encode("ascii")
                ).hexdigest(),
                replacement=lease,
                max_issued_per_window=self._settings.session_rate_per_minute,
                issued_since=now - 60,
            )
        token = self._encode_token(
            lease_id=stored_lease.lease_id,
            tenant_id=stored_lease.tenant_id,
            expires_at=stored_lease.expires_at,
        )
        if not hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).hexdigest(),
            stored_lease.token_hash,
        ):
            raise CollaborationConflict("lease_reconstruction_conflict")
        if stored_lease.expires_at - now <= 1:
            raise CollaborationLeaseInvalid("lease_expired")
        refresh_after = min(
            now + self._settings.token_refresh_seconds,
            stored_lease.expires_at - 0.5,
        )
        display_name = access.identity.display_name or (
            f"Guest {access.link.label}"
        )
        return {
            "websocket_path": "/collaboration",
            "room": self.room_name(
                access.link.document_id,
                access.link.generation,
            ),
            "lease_token": token,
            "expires_at": stored_lease.expires_at,
            "refresh_after": refresh_after,
            "provider_flush_ms": self._settings.provider_flush_ms,
            **self._published_transport_limits(),
            "access": access.link.permission,
            "initial_write_mode": access.link.permission,
            "user": {
                "id": str(access.identity.id),
                "name": display_name,
                "color": self.user_color(access.identity.id),
                "kind": "guest",
                "link_label": access.link.label,
            },
            "protocol_version": self._settings.protocol_version,
            "schema_version": self._settings.schema_version,
        }

    async def introspect_lease(
        self,
        *,
        token: str,
        room: str,
        instance_id: str,
        epoch: int,
    ) -> dict[str, Any]:
        """Validate signature, DB lease, current access, room, and instance."""
        token_payload = self._decode_token(token)
        tenant_id = token_payload["tenant_id"]
        self._require_tenant(tenant_id)
        policy_cursor = await self._store.current_policy_cursor(
            tenant_id=tenant_id,
        )
        now = time.time()
        await self._store.validate_instance(
            tenant_id=tenant_id,
            instance_id=instance_id,
            epoch=epoch,
            now=now,
        )
        lease = await self._store.introspect_lease(
            tenant_id=tenant_id,
            lease_id=token_payload["lease_id"],
            token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
            now=now,
        )
        document_id, generation = self.parse_room(room)
        if (
            lease.document_id != document_id
            or lease.generation != generation
            or abs(lease.expires_at - token_payload["expires_at"]) > 0.001
        ):
            raise CollaborationProtocolConflict("generation_conflict")
        state = await self._store.load_state(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=generation,
        )
        if lease.actor_kind == "guest":
            if self._guest_links is None or lease.guest_identity_id is None:
                raise CollaborationAuthenticationRequired("guest is inactive")
            guest = await self._guest_links.guest_identity_by_id(
                tenant_id=tenant_id,
                guest_identity_id=lease.guest_identity_id,
                now=now,
            )
            if guest is None:
                raise CollaborationAuthenticationRequired("guest is inactive")
            identity, link = guest
            if (
                lease.guest_link_id != link.id
                or lease.permission != self._guest_lease_permission(link.permission)
            ):
                raise CollaborationAuthenticationRequired("guest permission changed")
            user_payload = {
                "id": str(identity.id),
                "name": identity.display_name or f"Guest {link.label}",
                "color": self.user_color(identity.id),
                "kind": "guest",
                "link_label": link.label,
            }
        else:
            if lease.user_id is None:
                raise CollaborationAuthenticationRequired("user is inactive")
            profile = await self._users.find_by_user_id(
                tenant_id=tenant_id,
                user_id=lease.user_id,
            )
            if profile is None or profile.disabled_at is not None:
                raise CollaborationAuthenticationRequired("user is inactive")
            user_payload = self._profile_payload(profile)
        session_id = lease.session_id
        if lease.actor_kind == "guest":
            if lease.guest_identity_id is None:
                raise CollaborationAuthenticationRequired("guest is inactive")
            session_id = str(lease.guest_identity_id)
        elif session_id is None:
            raise CollaborationAuthenticationRequired("user is inactive")
        return {
            "valid": True,
            "lease_id": str(lease.lease_id),
            "tenant_id": tenant_id,
            "document_id": document_id,
            "generation": generation,
            "permission": lease.permission,
            "policy_cursor": policy_cursor,
            "session_id": session_id,
            "expires_at": lease.expires_at,
            "protocol_version": self._settings.protocol_version,
            "schema_version": state.document.schema_version,
            "schema_hash": state.document.schema_hash,
            "user": user_payload,
        }

    def _published_transport_limits(self) -> dict[str, int]:
        """Return the transport limits the collaboration service enforces.

        A session hands them to the browser so it can pace and bound what it
        sends. They come from the same variables Node reads, with the same
        defaults and bounds, so a session never announces a limit the
        service does not apply.
        """
        return {
            "update_rate_count": self._settings.update_rate_count,
            "update_rate_window_seconds": (
                self._settings.update_rate_window_seconds
            ),
            "max_queued_frames": self._settings.max_queued_frames,
            "max_frame_bytes": self._settings.max_frame_bytes,
            "max_queued_bytes": self._settings.max_queued_bytes,
        }

    @staticmethod
    def _guest_lease_permission(permission: str) -> CollaborationPermission:
        return cast(
            CollaborationPermission,
            "view" if permission in {"view", "comment"} else permission,
        )

    async def acquire_instance(
        self,
        *,
        instance_id: str,
        lease_seconds: float,
        protocol_version: int,
        schema_version: int,
        tenant_id: str,
    ) -> CollaborationInstanceLease:
        self._require_tenant(tenant_id)
        self._require_versions(protocol_version, schema_version)
        if lease_seconds != self._settings.instance_lease_seconds:
            raise CollaborationProtocolConflict("instance_lease_conflict")
        return await self._store.acquire_instance(
            tenant_id=tenant_id,
            instance_id=instance_id,
            now=time.time(),
            lease_seconds=lease_seconds,
        )

    async def renew_instance(
        self,
        *,
        instance_id: str,
        epoch: int,
        lease_seconds: float,
        tenant_id: str,
    ) -> CollaborationInstanceLease:
        self._require_tenant(tenant_id)
        if lease_seconds != self._settings.instance_lease_seconds:
            raise CollaborationProtocolConflict("instance_lease_conflict")
        return await self._store.renew_instance(
            tenant_id=tenant_id,
            instance_id=instance_id,
            epoch=epoch,
            now=time.time(),
            lease_seconds=lease_seconds,
        )

    async def load_state(
        self,
        *,
        document_id: str,
        generation: int,
        instance_id: str,
        epoch: int,
        tenant_id: str,
    ) -> CollaborationLoadedState:
        self._require_tenant(tenant_id)
        await self._store.validate_instance(
            tenant_id=tenant_id,
            instance_id=instance_id,
            epoch=epoch,
            now=time.time(),
        )
        return await self._store.load_state(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=generation,
        )

    async def persist_update(
        self,
        *,
        update: PersistCollaborationUpdate,
    ) -> PersistedCollaborationUpdate:
        """Persist one already schema-validated Node update."""
        self._require_tenant(update.tenant_id)
        if len(update.update_bytes) > self._settings.max_frame_bytes:
            raise CollaborationDocumentTooLarge("update frame")
        return await self._store.append_update(update)

    async def lookup_command(
        self,
        *,
        document_id: str,
        generation: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        instance_id: str,
        epoch: int,
        tenant_id: str,
    ) -> CollaborationPersistedCommand | None:
        """Resolve a prior fenced command before the sidecar mutates a Y.Doc."""
        self._require_tenant(tenant_id)
        await self._store.validate_instance(
            tenant_id=tenant_id,
            instance_id=instance_id,
            epoch=epoch,
            now=time.time(),
        )
        return await self._store.lookup_command(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=generation,
            command_id=command_id,
            command_payload_hash=command_payload_hash,
        )

    async def lookup_updates(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        update_hashes: tuple[str, ...],
        instance_id: str,
        epoch: int,
    ) -> tuple[CollaborationUpdateLookup, ...]:
        """Resolve durable update hashes behind the active instance fence."""
        self._require_tenant(tenant_id)
        return await self._store.lookup_updates_by_hashes(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=generation,
            update_hashes=update_hashes,
            instance_id=instance_id,
            instance_epoch=epoch,
            now=time.time(),
        )

    async def policy_events(
        self, *, tenant_id: str, cursor: int, limit: int
    ) -> dict[str, Any]:
        """Return content-free invalidations from the existing event feed."""
        self._require_tenant(tenant_id)
        if cursor < 0 or limit < 1:
            raise ValueError("invalid policy-event cursor or limit")
        page = await self._store.policy_events_after(
            tenant_id=tenant_id,
            cursor=cursor,
            limit=min(limit, 500),
        )
        return {
            "events": [
                {
                    "id": event.id,
                    "target_user_id": str(event.target_user_id),
                    "scope": event.scope,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                }
                for event in page.events
            ],
            "cursor": page.current_cursor,
            "reset_required": page.reset_required,
        }

    async def store_snapshot(
        self,
        *,
        snapshot: CollaborationSnapshot,
        projection_markdown: str,
        instance_id: str,
        epoch: int,
        tenant_id: str,
    ) -> None:
        self._require_tenant(tenant_id)
        if len(projection_markdown.encode("utf-8")) > self._settings.max_document_bytes:
            raise CollaborationDocumentTooLarge(snapshot.document_id)
        if snapshot.tenant_id != tenant_id:
            raise CollaborationProtocolConflict("tenant_conflict")
        now = time.time()
        await self._store.store_snapshot(
            snapshot,
            projection_markdown=projection_markdown,
            instance_id=instance_id,
            instance_epoch=epoch,
            now=now,
        )

    async def flush_projection(
        self,
        *,
        document_id: str,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> CollaborationProjection:
        """Drain Node durability and publish an exact current Markdown projection."""
        document = await self._documents.get_document(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        if document.content_mode != "collaboration":
            raise CollaborationConflict("mode_conflict")
        minimum_sequence = document.persisted_sequence
        for _attempt in range(_MAX_PROJECTION_CONVERGENCE_ATTEMPTS):
            projection = await self._node.project(
                document_id=document_id,
                generation=document.collaboration_generation,
                minimum_sequence=minimum_sequence,
            )
            if (
                projection.generation != document.collaboration_generation
                or projection.sequence < minimum_sequence
                or projection.schema_hash != document.collaboration_schema_hash
            ):
                raise CollaborationProtocolConflict("projection_conflict")
            updated = await self._store.update_projection(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=projection.generation,
                covered_sequence=projection.sequence,
                content_markdown=projection.markdown,
                projection_hash=projection.projection_hash,
                now=time.time(),
            )
            if (
                updated.projection_sequence == projection.sequence
                and updated.persisted_sequence == projection.sequence
            ):
                return replace(
                    projection,
                    authoritative_sequence=updated.persisted_sequence,
                )
            minimum_sequence = updated.persisted_sequence

        log.warning(
            "Collaboration projection did not converge for document %s "
            "after %d attempts (current_sequence=%d).",
            document_id,
            _MAX_PROJECTION_CONVERGENCE_ATTEMPTS,
            minimum_sequence,
        )
        raise CollaborationConflict(
            "projection_not_current",
            current_sequence=minimum_sequence,
        )

    async def decide(
        self,
        *,
        document_id: str,
        patch_ids: tuple[str, ...],
        decision: Literal["accept", "reject"],
        expected_sequence: int,
        command_id: uuid.UUID,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> CollaborationDecisionResult:
        """Execute an idempotent owner/editor suggestion decision through Node."""
        self._require_cookie_principal(principal)
        document = await self._documents.get_document(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.EDIT,
        )
        if document.content_mode != "collaboration":
            raise CollaborationConflict("mode_conflict")
        if expected_sequence < 0:
            raise ValueError("expected_sequence must be non-negative")
        if (
            not patch_ids
            or len(patch_ids) > _MAX_DECISION_PATCHES
            or len(set(patch_ids)) != len(patch_ids)
        ):
            raise ValueError("patch_ids must be a bounded non-empty unique list")
        return await self._node.decide(
            document_id=document_id,
            generation=document.collaboration_generation,
            expected_sequence=expected_sequence,
            command_id=command_id,
            patch_ids=patch_ids,
            decision=decision,
            actor_user_id=cast(uuid.UUID, principal.user_id),
        )

    async def publish_suggestion(
        self,
        *,
        document_id: str,
        patch_id: str,
        target_markdown: str,
        actor_kind: Literal["assistant", "agent"],
        expected_sequence: int,
        command_id: uuid.UUID,
        principal: Principal,
        visible_to: UserContext | None,
        base_markdown: str | None = None,
    ) -> CollaborationSuggestionResult:
        """Publish one private AI patch as a durable shared suggestion.

        ``base_markdown`` is the projection ``target_markdown`` was built on.
        With it, the collaboration service places the AI change onto the
        document as it is now and refuses only when another writer changed
        that very passage (``base_changed``); without it, the document must
        still be at ``expected_sequence``.
        """
        self._require_cookie_principal(principal)
        document = await self._documents.get_document(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.SUGGEST,
        )
        if document.content_mode != "collaboration":
            raise CollaborationConflict("mode_conflict")
        if any(
            len(markdown.encode("utf-8")) > self._settings.max_document_bytes
            for markdown in (target_markdown, base_markdown or "")
        ):
            raise CollaborationDocumentTooLarge(document_id)
        return await self._node.publish_suggestion(
            document_id=document_id,
            generation=document.collaboration_generation,
            expected_sequence=expected_sequence,
            command_id=command_id,
            patch_id=patch_id,
            actor_kind=actor_kind,
            actor_user_id=cast(uuid.UUID, principal.user_id),
            target_markdown=target_markdown,
            base_markdown=base_markdown,
        )

    async def list_activity(
        self,
        *,
        document_id: str,
        view: Literal["open", "history"],
        before_sequence: int | None,
        open_before: tuple[float, str] | None,
        author_user_id: uuid.UUID | None,
        type_filter: str | None,
        limit: int,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> CollaborationActivityPage:
        """Return open patches or grouped history with a raw-row cursor.

        Open-patch ``preview`` contains exact stored edits only when the patch
        source provides them. Human Yjs patches intentionally return ``None``
        because their exact current text is resolved from the live document.
        """
        allowed_types = (
            {
                "insertion",
                "deletion",
                "replacement",
                "format",
                "structure",
            }
            if view == "open"
            else {"direct", "suggestion", "decision", "system", "comment"}
        )
        if type_filter is not None and type_filter not in allowed_types:
            raise ValueError("invalid activity type")
        document = await self._documents.get_document_facts(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        if document.content_mode != "collaboration":
            raise CollaborationConflict("mode_conflict")
        page_limit = max(1, min(limit, 200))
        if view == "open":
            open_page = await self._store.list_open_patches(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=document.collaboration_generation,
                before=open_before,
                author_user_id=author_user_id,
                suggestion_kind=cast(
                    CollaborationSuggestionKind | None, type_filter
                ),
                limit=page_limit,
            )
            rows = open_page.patches
            profiles = await self._users.profiles_for_user_ids(
                tenant_id=principal.tenant_id,
                user_ids=tuple(
                    sorted(
                        {
                            row.author_user_id
                            for row in rows
                            if row.author_user_id is not None
                        },
                        key=str,
                    )
                ),
            )
            open_guest_profiles = await self._guest_actor_profiles(
                principal.tenant_id,
                {
                    row.author_guest_identity_id
                    for row in rows
                    if row.author_guest_identity_id is not None
                },
            )
            return CollaborationActivityPage(
                items=tuple(
                    {
                        "patch_id": row.patch_id,
                        "author": self._comment_actor(
                            row.author_user_id,
                            row.author_guest_identity_id,
                            profiles,
                            open_guest_profiles,
                        ),
                        "created_at": row.created_at,
                        "suggestion_ids": list(row.suggestion_ids),
                        "type": row.kinds[0] if len(row.kinds) == 1 else "mixed",
                        "types": list(row.kinds),
                        "preview": (
                            {"edits": [dict(edit) for edit in row.exact_edits]}
                            if row.exact_edits is not None
                            else None
                        ),
                    }
                    for row in rows
                ),
                next_cursor=(
                    encode_cursor(*open_page.next_cursor)
                    if open_page.next_cursor is not None
                    else None
                ),
            )
        if type_filter == "comment":
            comment_rows = await self._store.list_comment_activity(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                before_id=before_sequence,
                author_user_id=author_user_id,
                limit=page_limit + 1,
            )
            has_more = len(comment_rows) > page_limit
            page_rows = comment_rows[:page_limit]
            profiles = await self._users.profiles_for_user_ids(
                tenant_id=principal.tenant_id,
                user_ids=tuple(
                    sorted(
                        {
                            row.actor_user_id
                            for row in page_rows
                            if row.actor_user_id is not None
                        },
                        key=str,
                    )
                ),
            )
            return CollaborationActivityPage(
                items=tuple(
                    {
                        "from_sequence": row.id,
                        "to_sequence": row.id,
                        "type": "comment",
                        "actor_kind": "human",
                        "actor": self._activity_actor(
                            row.actor_user_id,
                            "human",
                            (
                                profiles.get(row.actor_user_id)
                                if row.actor_user_id is not None
                                else None
                            ),
                        ),
                        "comment_action": row.action.rsplit(".", 1)[-1],
                        "suggestion_ids": [],
                        "command_id": None,
                        "created_at": row.created_at,
                        "summary": {
                            "edits": [],
                            "omitted_edit_count": 0,
                        },
                        "update_count": 1,
                        "outcome": None,
                        "rejected_suggestions": [],
                        "rejected_suggestion_count": 0,
                    }
                    for row in page_rows
                ),
                next_cursor=(
                    page_rows[-1].id
                    if has_more and page_rows
                    else None
                ),
            )
        rows = await self._store.list_activity(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            generation=document.collaboration_generation,
            before_sequence=before_sequence,
            author_user_id=author_user_id,
            change_kind=cast(CollaborationChangeKind | None, type_filter),
            limit=page_limit + 1,
        )
        has_more = len(rows) > page_limit
        page_rows = rows[:page_limit]
        next_cursor = (
            page_rows[-1].sequence if has_more and page_rows else None
        )
        rejecting = tuple(
            row.sequence for row in page_rows if row.rejected_suggestion_ids
        )
        rejections = (
            await self._store.list_suggestion_rejections(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=document.collaboration_generation,
                sequences=rejecting,
                limit=_REJECTION_READ_LIMIT,
            )
            if rejecting
            else ()
        )
        rejections_by_sequence: dict[
            int, list[CollaborationSuggestionRejection]
        ] = {}
        for rejection in rejections:
            rejections_by_sequence.setdefault(rejection.sequence, []).append(
                rejection
            )
        profiles = await self._users.profiles_for_user_ids(
            tenant_id=principal.tenant_id,
            user_ids=tuple(
                sorted(
                    {
                        row.actor_user_id
                        for row in page_rows
                        if row.actor_user_id is not None
                    }
                    | {
                        rejection.author_user_id
                        for rejection in rejections
                        if rejection.author_user_id is not None
                    },
                    key=str,
                )
            ),
        )
        guest_profiles = await self._guest_actor_profiles(
            principal.tenant_id,
            {
                row.actor_guest_identity_id
                for row in page_rows
                if row.actor_guest_identity_id is not None
            }
            | {
                rejection.author_guest_identity_id
                for rejection in rejections
                if rejection.author_guest_identity_id is not None
            },
        )
        items: list[dict[str, Any]] = []
        for row in page_rows:
            if row.actor_guest_identity_id is not None:
                actor = self._guest_actor(row.actor_guest_identity_id, guest_profiles)
            else:
                actor = self._activity_actor(
                    row.actor_user_id,
                    row.actor_kind,
                    (
                        profiles.get(row.actor_user_id)
                        if row.actor_user_id is not None
                        else None
                    ),
                )
            # An edit that removed suggestions stands on its own: folding it
            # into neighbouring edits would hide whom it concerned.
            if (
                row.change_kind == "direct"
                and not row.rejected_suggestion_ids
                and items
                and items[-1]["type"] == "direct"
                and not items[-1]["rejected_suggestion_count"]
                and items[-1]["actor"]["id"] == actor["id"]
                and float(items[-1]["created_at"]) - row.created_at <= 60
            ):
                items[-1]["from_sequence"] = row.sequence
                items[-1]["update_count"] += 1
                prior_summary = items[-1]["summary"]
                next_edits = list(row.change_summary.get("edits", []))
                available = max(0, 3 - len(prior_summary["edits"]))
                prior_summary["edits"].extend(next_edits[:available])
                prior_summary["omitted_edit_count"] += (
                    int(row.change_summary.get("omitted_edit_count", 0))
                    + max(0, len(next_edits) - available)
                )
                continue
            items.append(
                {
                    "from_sequence": row.sequence,
                    "to_sequence": row.sequence,
                    "type": row.change_kind,
                    "actor_kind": row.actor_kind,
                    "actor": actor,
                    "suggestion_ids": list(row.suggestion_ids),
                    "command_id": (
                        str(row.command_id) if row.command_id is not None else None
                    ),
                    "created_at": row.created_at,
                    "summary": {
                        "edits": [
                            dict(edit)
                            for edit in row.change_summary.get("edits", [])
                            if isinstance(edit, dict)
                        ][:3],
                        "omitted_edit_count": int(
                            row.change_summary.get(
                                "omitted_edit_count", 0
                            )
                        ),
                    },
                    "update_count": 1,
                    "outcome": row.decision_outcome,
                    "rejected_suggestions": [
                        {
                            "suggestion_id": rejection.suggestion_id,
                            "patch_id": rejection.patch_id,
                            "author": self._rejection_author(
                                rejection, profiles, guest_profiles
                            ),
                        }
                        for rejection in sorted(
                            rejections_by_sequence.get(row.sequence, []),
                            key=lambda item: item.suggestion_id,
                        )[:_REJECTED_SUGGESTIONS_LISTED]
                    ],
                    "rejected_suggestion_count": len(
                        row.rejected_suggestion_ids
                    ),
                }
            )
        return CollaborationActivityPage(
            items=tuple(items),
            next_cursor=next_cursor,
        )

    async def list_comments(
        self,
        *,
        document_id: str,
        since_revision: int,
        status: Literal["all", "open", "resolved"],
        limit: int,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        """Return an incremental, author-enriched shared-comment snapshot."""
        self._require_cookie_principal(principal)
        document, access = await self._documents.get_document_with_access(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        if document.content_mode != "collaboration":
            raise CollaborationConflict("mode_conflict")
        actor_user_id = cast(uuid.UUID, principal.user_id)
        page = await self._store.list_comment_threads(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            generation=document.collaboration_generation,
            actor_user_id=actor_user_id,
            since_revision=since_revision,
            status=status,
            limit=limit,
        )
        profiles = await self._comment_profiles(
            principal.tenant_id,
            page.threads,
            extra_user_ids=page.participant_user_ids,
        )
        guest_profiles = await self._comment_guest_profiles(
            principal.tenant_id,
            page.threads,
            extra_guest_ids=page.participant_guest_identity_ids,
        )
        can_moderate = (
            access.mode is not AccessMode.SHARED
            or access.permission is SharePermission.EDIT
        )
        return {
            "object": "list",
            "data": [
                self._comment_thread_payload(
                    thread,
                    profiles=profiles,
                    guest_profiles=guest_profiles,
                    actor_user_id=actor_user_id,
                    actor_guest_identity_id=None,
                    can_moderate=can_moderate,
                )
                for thread in page.threads
            ],
            "revision": page.revision,
            "current_revision": (
                page.current_revision
                if page.current_revision is not None
                else page.revision
            ),
            "has_more": page.has_more,
            "last_read_revision": page.last_read_revision,
            "participants": [
                self._comment_actor(
                    user_id,
                    None,
                    profiles,
                    guest_profiles,
                )
                for user_id in page.participant_user_ids
            ]
            + [
                self._comment_actor(
                    None,
                    guest_id,
                    profiles,
                    guest_profiles,
                )
                for guest_id in page.participant_guest_identity_ids
            ],
        }

    async def create_comment(
        self,
        *,
        document_id: str,
        generation: int,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        anchor: dict[str, Any],
        quote_text: str,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        self._require_cookie_principal(principal)
        document, access = await self._comment_document(
            document_id=document_id,
            generation=generation,
            minimum=SharePermission.SUGGEST,
            visible_to=visible_to,
        )
        actor_user_id = cast(uuid.UUID, principal.user_id)
        command_hash = self._comment_command_hash(
            "create",
            document_id,
            generation,
            thread_id,
            message_id,
            anchor,
            quote_text,
            body_markdown,
            mention_user_ids,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.create_comment_thread(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                thread_id=thread_id,
                message_id=message_id,
                anchor=anchor,
                quote_text=quote_text,
                body_markdown=body_markdown,
                mention_user_ids=mention_user_ids,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._comment_mutation_payload(
            principal=principal,
            thread=thread,
            access=access,
        )

    async def reply_to_comment(
        self,
        *,
        document_id: str,
        generation: int,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        self._require_cookie_principal(principal)
        _document, access = await self._comment_document(
            document_id=document_id,
            generation=generation,
            minimum=SharePermission.SUGGEST,
            visible_to=visible_to,
        )
        actor_user_id = cast(uuid.UUID, principal.user_id)
        command_hash = self._comment_command_hash(
            "reply",
            document_id,
            generation,
            thread_id,
            message_id,
            body_markdown,
            mention_user_ids,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.add_comment_reply(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                thread_id=thread_id,
                message_id=message_id,
                body_markdown=body_markdown,
                mention_user_ids=mention_user_ids,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._comment_mutation_payload(
            principal=principal,
            thread=thread,
            access=access,
        )

    async def update_comment_message(
        self,
        *,
        document_id: str,
        generation: int,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str | None,
        mention_user_ids: tuple[uuid.UUID, ...],
        delete_message: bool,
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        self._require_cookie_principal(principal)
        _document, access = await self._comment_document(
            document_id=document_id,
            generation=generation,
            minimum=SharePermission.SUGGEST,
            visible_to=visible_to,
        )
        actor_user_id = cast(uuid.UUID, principal.user_id)
        command_hash = self._comment_command_hash(
            "delete" if delete_message else "edit",
            document_id,
            generation,
            thread_id,
            message_id,
            body_markdown,
            mention_user_ids,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.update_comment_message(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                thread_id=thread_id,
                message_id=message_id,
                body_markdown=body_markdown,
                mention_user_ids=mention_user_ids,
                delete_message=delete_message,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._comment_mutation_payload(
            principal=principal,
            thread=thread,
            access=access,
        )

    async def set_comment_status(
        self,
        *,
        document_id: str,
        generation: int,
        thread_id: uuid.UUID,
        status: Literal["open", "resolved"],
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        self._require_cookie_principal(principal)
        _document, access = await self._comment_document(
            document_id=document_id,
            generation=generation,
            minimum=SharePermission.SUGGEST,
            visible_to=visible_to,
        )
        actor_user_id = cast(uuid.UUID, principal.user_id)
        can_moderate = (
            access.mode is not AccessMode.SHARED
            or access.permission is SharePermission.EDIT
        )
        command_hash = self._comment_command_hash(
            "resolve" if status == "resolved" else "reopen",
            document_id,
            generation,
            thread_id,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.set_comment_thread_status(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                thread_id=thread_id,
                status=status,
                can_moderate=can_moderate,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._comment_mutation_payload(
            principal=principal,
            thread=thread,
            access=access,
        )

    async def delete_comment_thread(
        self,
        *,
        document_id: str,
        generation: int,
        thread_id: uuid.UUID,
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> dict[str, Any]:
        """Retire a whole thread its author started by accident.

        Resolving hides a discussion that happened; this is for one that was
        never meant to exist. The row and its messages stay for the audit and
        vanish from every read path, so the card is gone for the people in the
        document while the record is not.

        Deliberately NOT moderation, unlike resolving: this removes other
        people's replies from view, so the store admits only the thread's own
        author -- the same rule the per-message actions already follow.
        """
        self._require_cookie_principal(principal)
        _document, access = await self._comment_document(
            document_id=document_id,
            generation=generation,
            minimum=SharePermission.SUGGEST,
            visible_to=visible_to,
        )
        command_hash = self._comment_command_hash(
            "delete",
            document_id,
            generation,
            thread_id,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.delete_comment_thread(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=cast(uuid.UUID, principal.user_id),
                thread_id=thread_id,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._comment_mutation_payload(
            principal=principal,
            thread=thread,
            access=access,
        )

    async def mark_comments_read(
        self,
        *,
        document_id: str,
        generation: int,
        revision: int,
        principal: Principal,
        visible_to: UserContext | None,
    ) -> int:
        self._require_cookie_principal(principal)
        await self._comment_document(
            document_id=document_id,
            generation=generation,
            minimum=SharePermission.VIEW,
            visible_to=visible_to,
        )
        return await self._retry_comment_transaction(
            lambda: self._store.mark_comments_read(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=cast(uuid.UUID, principal.user_id),
                revision=revision,
                now=time.time(),
            )
        )

    async def list_guest_comments(
        self,
        *,
        access: "EditorGuestAccess",
        since_revision: int,
        status: Literal["all", "open", "resolved"],
        limit: int,
    ) -> dict[str, Any]:
        """Read the shared discussion through one scoped guest identity."""
        page = await self._store.list_comment_threads(
            tenant_id=access.link.tenant_id,
            document_id=access.link.document_id,
            generation=access.link.generation,
            actor_user_id=None,
            actor_guest_identity_id=access.identity.id,
            guest_link_id=access.link.id,
            since_revision=since_revision,
            status=status,
            limit=limit,
        )
        profiles = await self._comment_profiles(
            access.link.tenant_id,
            page.threads,
            extra_user_ids=page.participant_user_ids,
        )
        guest_profiles = await self._comment_guest_profiles(
            access.link.tenant_id,
            page.threads,
            extra_guest_ids=page.participant_guest_identity_ids,
        )
        can_moderate = access.link.permission == "edit"
        return {
            "object": "list",
            "data": [
                self._comment_thread_payload(
                    thread,
                    profiles=profiles,
                    guest_profiles=guest_profiles,
                    actor_user_id=None,
                    actor_guest_identity_id=access.identity.id,
                    can_moderate=can_moderate,
                )
                for thread in page.threads
            ],
            "revision": page.revision,
            "current_revision": (
                page.current_revision
                if page.current_revision is not None
                else page.revision
            ),
            "has_more": page.has_more,
            "last_read_revision": page.last_read_revision,
            "participants": [
                self._comment_actor(
                    user_id,
                    None,
                    profiles,
                    guest_profiles,
                )
                for user_id in page.participant_user_ids
            ]
            + [
                self._comment_actor(
                    None,
                    guest_id,
                    profiles,
                    guest_profiles,
                )
                for guest_id in page.participant_guest_identity_ids
            ],
        }

    async def create_guest_comment(
        self,
        *,
        access: "EditorGuestAccess",
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        anchor: dict[str, Any],
        quote_text: str,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
    ) -> dict[str, Any]:
        self._require_guest_comment_access(access)
        command_hash = self._comment_command_hash(
            "create",
            access.link.document_id,
            access.link.generation,
            thread_id,
            message_id,
            anchor,
            quote_text,
            body_markdown,
            mention_user_ids,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.create_comment_thread(
                tenant_id=access.link.tenant_id,
                document_id=access.link.document_id,
                generation=access.link.generation,
                actor_user_id=None,
                actor_guest_identity_id=access.identity.id,
                guest_link_id=access.link.id,
                thread_id=thread_id,
                message_id=message_id,
                anchor=anchor,
                quote_text=quote_text,
                body_markdown=body_markdown,
                mention_user_ids=mention_user_ids,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._guest_comment_mutation_payload(
            access=access,
            thread=thread,
        )

    async def reply_to_guest_comment(
        self,
        *,
        access: "EditorGuestAccess",
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
    ) -> dict[str, Any]:
        self._require_guest_comment_access(access)
        command_hash = self._comment_command_hash(
            "reply",
            access.link.document_id,
            access.link.generation,
            thread_id,
            message_id,
            body_markdown,
            mention_user_ids,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.add_comment_reply(
                tenant_id=access.link.tenant_id,
                document_id=access.link.document_id,
                generation=access.link.generation,
                actor_user_id=None,
                actor_guest_identity_id=access.identity.id,
                guest_link_id=access.link.id,
                thread_id=thread_id,
                message_id=message_id,
                body_markdown=body_markdown,
                mention_user_ids=mention_user_ids,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._guest_comment_mutation_payload(
            access=access,
            thread=thread,
        )

    async def update_guest_comment_message(
        self,
        *,
        access: "EditorGuestAccess",
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str | None,
        mention_user_ids: tuple[uuid.UUID, ...],
        delete_message: bool,
        expected_revision: int,
        command_id: uuid.UUID,
    ) -> dict[str, Any]:
        self._require_guest_comment_access(access)
        command_hash = self._comment_command_hash(
            "delete" if delete_message else "edit",
            access.link.document_id,
            access.link.generation,
            thread_id,
            message_id,
            body_markdown,
            mention_user_ids,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.update_comment_message(
                tenant_id=access.link.tenant_id,
                document_id=access.link.document_id,
                generation=access.link.generation,
                actor_user_id=None,
                actor_guest_identity_id=access.identity.id,
                guest_link_id=access.link.id,
                thread_id=thread_id,
                message_id=message_id,
                body_markdown=body_markdown,
                mention_user_ids=mention_user_ids,
                delete_message=delete_message,
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._guest_comment_mutation_payload(
            access=access,
            thread=thread,
        )

    async def set_guest_comment_status(
        self,
        *,
        access: "EditorGuestAccess",
        thread_id: uuid.UUID,
        status: Literal["open", "resolved"],
        expected_revision: int,
        command_id: uuid.UUID,
    ) -> dict[str, Any]:
        self._require_guest_comment_access(access)
        command_hash = self._comment_command_hash(
            "resolve" if status == "resolved" else "reopen",
            access.link.document_id,
            access.link.generation,
            thread_id,
            expected_revision,
        )
        thread = await self._retry_comment_transaction(
            lambda: self._store.set_comment_thread_status(
                tenant_id=access.link.tenant_id,
                document_id=access.link.document_id,
                generation=access.link.generation,
                actor_user_id=None,
                actor_guest_identity_id=access.identity.id,
                guest_link_id=access.link.id,
                thread_id=thread_id,
                status=status,
                can_moderate=access.link.permission == "edit",
                expected_revision=expected_revision,
                command_id=command_id,
                command_payload_hash=command_hash,
                now=time.time(),
            )
        )
        return await self._guest_comment_mutation_payload(
            access=access,
            thread=thread,
        )

    async def mark_guest_comments_read(
        self,
        *,
        access: "EditorGuestAccess",
        revision: int,
    ) -> int:
        return await self._retry_comment_transaction(
            lambda: self._store.mark_comments_read(
                tenant_id=access.link.tenant_id,
                document_id=access.link.document_id,
                generation=access.link.generation,
                actor_user_id=None,
                actor_guest_identity_id=access.identity.id,
                guest_link_id=access.link.id,
                revision=revision,
                now=time.time(),
            )
        )

    async def _retry_comment_transaction(
        self,
        operation: Callable[[], Awaitable[_CommentResult]],
    ) -> _CommentResult:
        """Retry PostgreSQL deadlock victims at the idempotent command seam.

        Shared-comment writes lock the document, permission rows, authors and
        the transactional invalidation outbox. Concurrent read-state updates
        can make PostgreSQL choose either transaction as a deadlock victim.
        Every mutation already carries a stable command id and PostgreSQL
        rolls a deadlock victim back completely, so replaying the whole store
        operation is safe and preserves exactly-once persistence.
        """
        for attempt in range(len(_COMMENT_DEADLOCK_RETRY_DELAYS) + 1):
            try:
                return await operation()
            except Exception as error:
                if (
                    not _is_postgres_deadlock(error)
                    or attempt >= len(_COMMENT_DEADLOCK_RETRY_DELAYS)
                ):
                    raise
                delay = _COMMENT_DEADLOCK_RETRY_DELAYS[attempt]
                log.warning(
                    "Shared-comment transaction deadlocked; retrying "
                    "(attempt %s/%s, delay %.3fs).",
                    attempt + 1,
                    len(_COMMENT_DEADLOCK_RETRY_DELAYS),
                    delay,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable shared-comment retry state")

    @staticmethod
    def _require_guest_comment_access(access: "EditorGuestAccess") -> None:
        if access.link.permission not in {"comment", "suggest", "edit"}:
            raise CollaborationAuthenticationRequired(
                "guest comment permission required"
            )
        if access.identity.display_name is None:
            raise CollaborationAuthenticationRequired(
                "guest display name required"
            )

    async def _guest_comment_mutation_payload(
        self,
        *,
        access: "EditorGuestAccess",
        thread: CollaborationCommentThread,
    ) -> dict[str, Any]:
        profiles = await self._comment_profiles(
            access.link.tenant_id,
            (thread,),
        )
        guest_profiles = await self._comment_guest_profiles(
            access.link.tenant_id,
            (thread,),
        )
        return {
            "revision": thread.revision,
            "thread": self._comment_thread_payload(
                thread,
                profiles=profiles,
                guest_profiles=guest_profiles,
                actor_user_id=None,
                actor_guest_identity_id=access.identity.id,
                can_moderate=access.link.permission == "edit",
            ),
        }

    async def _comment_document(
        self,
        *,
        document_id: str,
        generation: int,
        minimum: SharePermission,
        visible_to: UserContext | None,
    ):
        document, access = await self._documents.get_document_with_access(
            document_id,
            visible_to=visible_to,
            minimum=minimum,
        )
        if document.content_mode != "collaboration":
            raise CollaborationConflict("mode_conflict")
        if document.collaboration_generation != generation:
            raise CollaborationConflict("generation_conflict")
        return document, access

    async def _comment_mutation_payload(
        self,
        *,
        principal: Principal,
        thread: CollaborationCommentThread,
        access,
    ) -> dict[str, Any]:
        profiles = await self._comment_profiles(
            principal.tenant_id,
            (thread,),
        )
        guest_profiles = await self._comment_guest_profiles(
            principal.tenant_id,
            (thread,),
        )
        actor_user_id = cast(uuid.UUID, principal.user_id)
        return {
            "revision": thread.revision,
            "thread": self._comment_thread_payload(
                thread,
                profiles=profiles,
                guest_profiles=guest_profiles,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=None,
                can_moderate=(
                    access.mode is not AccessMode.SHARED
                    or access.permission is SharePermission.EDIT
                ),
            ),
        }

    async def _comment_profiles(
        self,
        tenant_id: str,
        threads: tuple[CollaborationCommentThread, ...],
        *,
        extra_user_ids: tuple[uuid.UUID, ...] = (),
    ) -> dict[uuid.UUID, "MirroredUser"]:
        user_ids: set[uuid.UUID] = set(extra_user_ids)
        for thread in threads:
            if thread.created_by_user_id is not None:
                user_ids.add(thread.created_by_user_id)
            if thread.resolved_by_user_id is not None:
                user_ids.add(thread.resolved_by_user_id)
            for message in thread.messages:
                if message.author_user_id is not None:
                    user_ids.add(message.author_user_id)
                user_ids.update(message.mention_user_ids)
        return await self._users.profiles_for_user_ids(
            tenant_id=tenant_id,
            user_ids=tuple(sorted(user_ids, key=str)),
        )

    async def _comment_guest_profiles(
        self,
        tenant_id: str,
        threads: tuple[CollaborationCommentThread, ...],
        *,
        extra_guest_ids: tuple[uuid.UUID, ...] = (),
    ) -> dict[uuid.UUID, "EditorGuestActorProfile"]:
        if self._guest_links is None:
            return {}
        guest_ids: set[uuid.UUID] = set(extra_guest_ids)
        for thread in threads:
            if thread.created_by_guest_identity_id is not None:
                guest_ids.add(thread.created_by_guest_identity_id)
            if thread.resolved_by_guest_identity_id is not None:
                guest_ids.add(thread.resolved_by_guest_identity_id)
            for message in thread.messages:
                if message.author_guest_identity_id is not None:
                    guest_ids.add(message.author_guest_identity_id)
        return await self._guest_links.guest_actor_profiles(
            tenant_id=tenant_id,
            guest_identity_ids=tuple(sorted(guest_ids, key=str)),
        )

    def _comment_thread_payload(
        self,
        thread: CollaborationCommentThread,
        *,
        profiles: dict[uuid.UUID, "MirroredUser"],
        guest_profiles: dict[uuid.UUID, "EditorGuestActorProfile"],
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None,
        can_moderate: bool,
    ) -> dict[str, Any]:
        return {
            "id": str(thread.thread_id),
            "document_id": thread.document_id,
            "generation": thread.generation,
            "revision": thread.revision,
            "status": thread.status,
            "author": self._comment_actor(
                thread.created_by_user_id,
                thread.created_by_guest_identity_id,
                profiles,
                guest_profiles,
            ),
            "resolved_by": (
                self._comment_actor(
                    thread.resolved_by_user_id,
                    thread.resolved_by_guest_identity_id,
                    profiles,
                    guest_profiles,
                )
                if (
                    thread.resolved_by_user_id is not None
                    or thread.resolved_by_guest_identity_id is not None
                )
                else None
            ),
            "resolved_at": thread.resolved_at,
            "anchor": dict(thread.anchor),
            "quote": thread.quote_text,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
            # A deleted thread reaches a client exactly once, on the
            # incremental page that follows the deletion, so the card it
            # already holds can go. A fresh load never sees it.
            "deleted_at": thread.deleted_at,
            "can_resolve": (
                can_moderate
                or (
                    thread.created_by_user_id == actor_user_id
                    and thread.created_by_guest_identity_id
                    == actor_guest_identity_id
                )
            ),
            # Deliberately NOT moderation: unlike resolving, deleting removes
            # other people's replies from view, so only the author may.
            "can_delete": (
                thread.deleted_at is None
                and thread.created_by_user_id == actor_user_id
                and thread.created_by_guest_identity_id
                == actor_guest_identity_id
            ),
            "messages": [
                {
                    "id": str(message.message_id),
                    "revision": message.revision,
                    "author": self._comment_actor(
                        message.author_user_id,
                        message.author_guest_identity_id,
                        profiles,
                        guest_profiles,
                    ),
                    "body_markdown": (
                        None
                        if message.deleted_at is not None
                        else message.body_markdown
                    ),
                    "mentions": [
                        self._activity_actor(
                            mentioned_user_id,
                            "human",
                            profiles.get(mentioned_user_id),
                        )
                        for mentioned_user_id in message.mention_user_ids
                    ],
                    "created_at": message.created_at,
                    "edited_at": message.edited_at,
                    "deleted_at": message.deleted_at,
                    "can_edit": (
                        message.deleted_at is None
                        and message.author_user_id == actor_user_id
                        and message.author_guest_identity_id
                        == actor_guest_identity_id
                    ),
                    "can_delete": (
                        message.deleted_at is None
                        and message.author_user_id == actor_user_id
                        and message.author_guest_identity_id
                        == actor_guest_identity_id
                    ),
                }
                for message in thread.messages
            ],
        }

    def _comment_actor(
        self,
        user_id: uuid.UUID | None,
        guest_identity_id: uuid.UUID | None,
        profiles: dict[uuid.UUID, "MirroredUser"],
        guest_profiles: dict[uuid.UUID, "EditorGuestActorProfile"],
    ) -> dict[str, str | None]:
        if guest_identity_id is not None:
            return self._guest_actor(guest_identity_id, guest_profiles)
        return self._activity_actor(
            user_id,
            "human",
            profiles.get(user_id) if user_id is not None else None,
        )

    @staticmethod
    def _guest_actor(
        guest_identity_id: uuid.UUID,
        guest_profiles: dict[uuid.UUID, "EditorGuestActorProfile"],
    ) -> dict[str, str | None]:
        """A guest by the name they gave, or by the label of their link."""
        guest = guest_profiles.get(guest_identity_id)
        label = guest.link_label if guest is not None else "—"
        name = (
            guest.display_name
            if guest is not None and guest.display_name
            else f"Guest {label}"
        )
        return {
            "id": str(guest_identity_id),
            "name": name,
            "kind": "guest",
            "link_label": label,
        }

    def _rejection_author(
        self,
        rejection: CollaborationSuggestionRejection,
        profiles: dict[uuid.UUID, "MirroredUser"],
        guest_profiles: dict[uuid.UUID, "EditorGuestActorProfile"],
    ) -> dict[str, str | None]:
        return self._comment_actor(
            rejection.author_user_id,
            rejection.author_guest_identity_id,
            profiles,
            guest_profiles,
        )

    async def _guest_actor_profiles(
        self,
        tenant_id: str,
        guest_identity_ids: set[uuid.UUID],
    ) -> dict[uuid.UUID, "EditorGuestActorProfile"]:
        if self._guest_links is None or not guest_identity_ids:
            return {}
        return await self._guest_links.guest_actor_profiles(
            tenant_id=tenant_id,
            guest_identity_ids=tuple(sorted(guest_identity_ids, key=str)),
        )

    @staticmethod
    def _comment_command_hash(*values: Any) -> str:
        encoded = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _activity_actor(
        user_id: uuid.UUID | None,
        actor_kind: str,
        profile: "MirroredUser | None",
    ) -> dict[str, str | None]:
        actor: dict[str, str | None] = {
            "id": str(user_id) if user_id is not None else None,
            "name": (
                (profile.display_name or profile.email or "User")
                if profile is not None
                else actor_kind.title()
            ),
        }
        if profile is not None and profile.issuer == DELETED_ACCOUNT_ISSUER:
            # Work an erased account left in this document: the interface
            # names its placeholder in the reader's language.
            actor["kind"] = "deleted"
        return actor

    async def run_maintenance(
        self,
        *,
        tenant_id: str,
        document_id: str | None,
        generation: int | None,
        instance_id: str,
        epoch: int,
    ) -> dict[str, int]:
        """Run fenced retention after Node has stored a verified snapshot."""
        self._require_tenant(tenant_id)
        now = time.time()
        payloads = 0
        metadata = 0
        if document_id is not None:
            if generation is None:
                raise ValueError("generation is required for document maintenance")
            payloads, metadata = await self._store.compact(
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                instance_id=instance_id,
                instance_epoch=epoch,
                now=now,
                payload_retention_seconds=(
                    self._settings.update_payload_retention_seconds
                ),
                metadata_retention_seconds=(
                    self._settings.activity_retention_seconds
                ),
            )
        purged = await self._store.purge_tombstones(
            tenant_id=tenant_id,
            instance_id=instance_id,
            instance_epoch=epoch,
            now=now,
            retention_seconds=self._settings.tombstone_retention_seconds,
        )
        return {
            "payloads_pruned": payloads,
            "metadata_pruned": metadata,
            "tombstones_purged": purged,
        }

    async def aclose(self) -> None:
        """Release the private HTTP client pool."""
        await self._node.aclose()

    @staticmethod
    def room_name(document_id: str, generation: int) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", document_id):
            raise ValueError("document_id cannot be encoded as a room name")
        if generation < 1:
            raise ValueError("generation must be positive")
        return f"inqtrix-editor-v1:{document_id}:g{generation}"

    @staticmethod
    def parse_room(room: str) -> tuple[str, int]:
        match = _ROOM_PATTERN.fullmatch(room)
        if match is None:
            raise CollaborationProtocolConflict("invalid_room")
        return match.group("document"), int(match.group("generation"))

    @staticmethod
    def user_color(user_id: uuid.UUID) -> str:
        digest = hashlib.sha256(user_id.bytes).digest()
        return _COLORS[digest[0] % len(_COLORS)]

    def _profile_payload(self, profile: "MirroredUser") -> dict[str, str]:
        return {
            "id": str(profile.user_id),
            "name": profile.display_name or profile.email or "User",
            "color": self.user_color(profile.user_id),
        }

    def _require_cookie_principal(self, principal: Principal) -> None:
        if (
            principal.kind != "oidc_session"
            or principal.user_id is None
            or principal.session_id is None
        ):
            raise CollaborationAuthenticationRequired(
                "collaboration requires a cookie-authenticated user session"
            )

    def _require_versions(self, protocol_version: int, schema_version: int) -> None:
        if protocol_version != self._settings.protocol_version:
            raise CollaborationProtocolConflict("protocol_conflict")
        if schema_version != self._settings.schema_version:
            raise CollaborationProtocolConflict("schema_conflict")

    def _require_tenant(self, tenant_id: str) -> None:
        """Fence every Node-facing operation to the configured deployment."""
        if not isinstance(tenant_id, str) or not hmac.compare_digest(
            tenant_id,
            self._settings.tenant_id,
        ):
            raise CollaborationProtocolConflict("tenant_conflict")

    def _encode_token(
        self,
        *,
        lease_id: uuid.UUID,
        tenant_id: str,
        expires_at: float,
    ) -> str:
        payload = json.dumps(
            {
                "lease_id": str(lease_id),
                "tenant_id": tenant_id,
                "expires_at": expires_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = _base64url(payload)
        signing_input = f"{_TOKEN_VERSION}.{encoded}".encode("ascii")
        signature = _base64url(
            hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        )
        return f"{_TOKEN_VERSION}.{encoded}.{signature}"

    def _decode_token(self, token: str) -> dict[str, Any]:
        if len(token) > 4096 or re.fullmatch(
            r"cl1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token
        ) is None:
            raise CollaborationLeaseInvalid("lease_invalid")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
            raise CollaborationLeaseInvalid("lease_invalid")
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii", errors="strict")
        expected = _base64url(
            hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        )
        if not hmac.compare_digest(parts[2], expected):
            raise CollaborationLeaseInvalid("lease_invalid")
        try:
            payload = json.loads(_unbase64url(parts[1]))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise CollaborationLeaseInvalid("lease_invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "lease_id",
            "tenant_id",
            "expires_at",
        }:
            raise CollaborationLeaseInvalid("lease_invalid")
        try:
            lease_id = uuid.UUID(payload["lease_id"])
        except (TypeError, ValueError, AttributeError) as exc:
            raise CollaborationLeaseInvalid("lease_invalid") from exc
        tenant_id = payload["tenant_id"]
        expires_at = payload["expires_at"]
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
        ):
            raise CollaborationLeaseInvalid("lease_invalid")
        if float(expires_at) <= time.time():
            raise CollaborationLeaseInvalid("lease_expired")
        return {
            "lease_id": lease_id,
            "tenant_id": tenant_id,
            "expires_at": float(expires_at),
        }


def _is_postgres_deadlock(error: BaseException) -> bool:
    """Recognize SQLSTATE 40P01 through SQLAlchemy/asyncpg wrappers."""
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if (
            getattr(current, "sqlstate", None) == "40P01"
            or getattr(current, "pgcode", None) == "40P01"
        ):
            return True
        for candidate in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(candidate, BaseException):
                pending.append(candidate)
    return False


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unbase64url(value: str) -> str:
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    ).decode("utf-8")
