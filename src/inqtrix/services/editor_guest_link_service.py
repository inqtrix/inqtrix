"""Secure owner and guest workflows for account-less editor links."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from inqtrix.auth.credentials import hash_password, verify_password
from inqtrix.auth.principal import Principal
from inqtrix.auth.guest_ratelimit import GuestLinkRateLimiter
from inqtrix.project.editor_guest_links import (
    EditorDocumentShareLink,
    EditorGuestAccess,
    EditorGuestIdentity,
    EditorGuestLinkExpired,
    EditorGuestLinkNotFound,
    EditorGuestLinkRateLimited,
    EditorGuestLinkStore,
    EditorShareLinkPermission,
)

if TYPE_CHECKING:
    from inqtrix.services.editor_collaboration_service import (
        EditorCollaborationService,
    )
    from inqtrix.settings import EditorGuestLinkSettings

_LINK_VERSION = "egl1"
_SESSION_VERSION = "egs1"
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PERMISSIONS = frozenset({"view", "comment", "suggest", "edit"})


@dataclass(frozen=True)
class EditorGuestUnlock:
    """One successful unlock; the raw session token becomes an HTTP-only cookie."""

    access: EditorGuestAccess
    session_token: str


class EditorGuestLinkService:
    """Constructor-injected guest-link authority with no plaintext persistence."""

    def __init__(
        self,
        *,
        store: EditorGuestLinkStore,
        collaboration: "EditorCollaborationService",
        settings: "EditorGuestLinkSettings",
        public_base_url: str,
        rate_limiter: GuestLinkRateLimiter,
    ) -> None:
        self._store = store
        self._collaboration = collaboration
        self._settings = settings
        self._public_base_url = public_base_url.rstrip("/")
        self._secret = settings.token_hmac_secret.encode("utf-8")
        self._rate_limiter = rate_limiter
        self._dummy_password_hash = hash_password(
            "inqtrix-editor-guest-link-uniform-timing"
        )

    @staticmethod
    def _owner(principal: Principal) -> uuid.UUID:
        if principal.kind not in {"oidc_session", "pat"} or principal.user_id is None:
            raise EditorGuestLinkNotFound()
        return principal.user_id

    @staticmethod
    def _payload_hash(*parts: object) -> str:
        payload = json.dumps(
            parts,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _derived_bytes(self, command_id: uuid.UUID, purpose: str) -> bytes:
        return hmac.new(
            self._secret,
            f"{purpose}:{command_id}".encode("ascii"),
            hashlib.sha256,
        ).digest()

    def _derived_password(self, command_id: uuid.UUID) -> str:
        material = self._derived_bytes(command_id, "password")
        characters = [
            _PASSWORD_ALPHABET[byte % len(_PASSWORD_ALPHABET)]
            for byte in material[:20]
        ]
        return "-".join(
            "".join(characters[index : index + 4])
            for index in range(0, len(characters), 4)
        )

    def _encode_token(
        self,
        *,
        version: Literal["egl1", "egs1"],
        tenant_id: str,
        material: bytes,
    ) -> str:
        tenant = base64.urlsafe_b64encode(tenant_id.encode("utf-8")).decode(
            "ascii"
        ).rstrip("=")
        random_part = base64.urlsafe_b64encode(material).decode("ascii").rstrip(
            "="
        )
        signing_input = f"{version}.{tenant}.{random_part}".encode("ascii")
        signature = base64.urlsafe_b64encode(
            hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        return f"{version}.{tenant}.{random_part}.{signature}"

    def _decode_token(
        self,
        token: str,
        *,
        version: Literal["egl1", "egs1"],
    ) -> str:
        if len(token) > 1024:
            raise EditorGuestLinkNotFound()
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != version:
            raise EditorGuestLinkNotFound()
        signing_input = ".".join(parts[:3]).encode("ascii", errors="strict")
        expected = base64.urlsafe_b64encode(
            hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(parts[3], expected):
            raise EditorGuestLinkNotFound()
        try:
            padding = "=" * (-len(parts[1]) % 4)
            tenant_id = base64.urlsafe_b64decode(
                (parts[1] + padding).encode("ascii")
            ).decode("utf-8")
        except (ValueError, UnicodeError):
            raise EditorGuestLinkNotFound() from None
        if not tenant_id.strip() or len(tenant_id) > 160:
            raise EditorGuestLinkNotFound()
        return tenant_id

    def _digest(self, token: str, *, purpose: str) -> str:
        return hmac.new(
            self._secret,
            f"{purpose}:{token}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _link_token(self, tenant_id: str, command_id: uuid.UUID) -> str:
        return self._encode_token(
            version=_LINK_VERSION,
            tenant_id=tenant_id,
            material=self._derived_bytes(command_id, "link-token"),
        )

    async def create_link(
        self,
        *,
        document_id: str,
        permission: EditorShareLinkPermission,
        ttl_seconds: int | None,
        command_id: uuid.UUID,
        principal: Principal,
        generation: int,
    ) -> dict[str, Any]:
        actor_user_id = self._owner(principal)
        if permission not in _PERMISSIONS:
            raise ValueError("invalid permission")
        ttl = ttl_seconds or self._settings.default_ttl_seconds
        if ttl < 3600 or ttl > self._settings.max_ttl_seconds:
            raise ValueError("ttl_seconds must be between one hour and the limit")
        now = time.time()
        token = self._link_token(principal.tenant_id, command_id)
        password = self._derived_password(command_id)
        link_id = uuid.UUID(
            bytes=self._derived_bytes(command_id, "link-id")[:16],
            version=4,
        )
        label = (
            base64.b32encode(link_id.bytes[:3]).decode("ascii").rstrip("=")[:4]
        )
        command_hash = self._payload_hash(
            "create",
            document_id,
            permission,
            ttl,
            generation,
        )
        stored = await self._store.create_link(
            EditorDocumentShareLink(
                id=link_id,
                tenant_id=principal.tenant_id,
                document_id=document_id,
                generation=generation,
                label=label,
                permission=permission,
                token_digest=self._digest(token, purpose="link"),
                password_hash=hash_password(password),
                created_by_user_id=actor_user_id,
                revision=1,
                expires_at=now + ttl,
                created_at=now,
                updated_at=now,
                last_command_id=command_id,
                last_command_payload_hash=command_hash,
                last_command_kind="create",
            )
        )
        return {
            **self.link_payload(stored),
            "url": f"{self._public_base_url}/s/{token}",
            "password": password,
        }

    async def list_links(
        self,
        *,
        document_id: str,
        principal: Principal,
    ) -> tuple[dict[str, Any], ...]:
        links = await self._store.list_links(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            actor_user_id=self._owner(principal),
        )
        return tuple(self.link_payload(link) for link in links)

    async def access_summary(
        self,
        *,
        document_id: str,
        principal: Principal,
        window_seconds: int,
    ) -> dict[str, int | float | None]:
        now = time.time()
        summary = await self._store.access_summary(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            actor_user_id=self._owner(principal),
            since=now - window_seconds,
            now=now,
        )
        if self._settings.stats_enabled:
            return summary
        return {
            "guest_link_count": summary["guest_link_count"],
            "guest_open_count": 0,
            "guest_session_count": 0,
            "last_guest_accessed_at": None,
        }

    async def update_link(
        self,
        *,
        document_id: str,
        link_id: uuid.UUID,
        permission: EditorShareLinkPermission | None,
        ttl_seconds: int | None,
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
    ) -> dict[str, Any]:
        if permission is not None and permission not in _PERMISSIONS:
            raise ValueError("invalid permission")
        if ttl_seconds is not None and not (
            3600 <= ttl_seconds <= self._settings.max_ttl_seconds
        ):
            raise ValueError("ttl_seconds must be between one hour and the limit")
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        stored = await self._store.update_link(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            link_id=link_id,
            actor_user_id=self._owner(principal),
            permission=permission,
            expires_at=expires_at,
            password_hash=None,
            token_digest=None,
            expected_revision=expected_revision,
            command_id=command_id,
            command_payload_hash=self._payload_hash(
                "update",
                document_id,
                link_id,
                permission,
                ttl_seconds,
                expected_revision,
            ),
            command_kind="update",
            now=now,
        )
        return self.link_payload(stored)

    async def rotate_password(
        self,
        *,
        document_id: str,
        link_id: uuid.UUID,
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
    ) -> dict[str, Any]:
        password = self._derived_password(command_id)
        stored = await self._store.update_link(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            link_id=link_id,
            actor_user_id=self._owner(principal),
            permission=None,
            expires_at=None,
            password_hash=hash_password(password),
            token_digest=None,
            expected_revision=expected_revision,
            command_id=command_id,
            command_payload_hash=self._payload_hash(
                "rotate_password",
                document_id,
                link_id,
                expected_revision,
            ),
            command_kind="rotate_password",
            now=time.time(),
        )
        return {**self.link_payload(stored), "password": password}

    async def rotate_token(
        self,
        *,
        document_id: str,
        link_id: uuid.UUID,
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
    ) -> dict[str, Any]:
        """Replace the link half of the credential; the password is kept.

        The old URL stops resolving the moment the digest is replaced, which
        is what separates this from creating a second link: creating one
        leaves the leaked URL alive.
        """
        token = self._link_token(principal.tenant_id, command_id)
        stored = await self._store.update_link(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            link_id=link_id,
            actor_user_id=self._owner(principal),
            permission=None,
            expires_at=None,
            password_hash=None,
            token_digest=self._digest(token, purpose="link"),
            expected_revision=expected_revision,
            command_id=command_id,
            command_payload_hash=self._payload_hash(
                "rotate_token",
                document_id,
                link_id,
                expected_revision,
            ),
            command_kind="rotate_token",
            now=time.time(),
        )
        return {
            **self.link_payload(stored),
            "url": f"{self._public_base_url}/s/{token}",
        }

    async def revoke_link(
        self,
        *,
        document_id: str,
        link_id: uuid.UUID,
        expected_revision: int,
        command_id: uuid.UUID,
        principal: Principal,
    ) -> dict[str, Any]:
        stored = await self._store.revoke_link(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            link_id=link_id,
            actor_user_id=self._owner(principal),
            expected_revision=expected_revision,
            command_id=command_id,
            command_payload_hash=self._payload_hash(
                "revoke",
                document_id,
                link_id,
                expected_revision,
            ),
            now=time.time(),
        )
        return self.link_payload(stored)

    async def describe_token(self, token: str) -> dict[str, Any]:
        tenant_id = self._decode_token(token, version=_LINK_VERSION)
        link, document_title = await self._store.resolve_link(
            tenant_id=tenant_id,
            token_digest=self._digest(token, purpose="link"),
            now=time.time(),
        )
        return {
            "document_title": document_title,
            "expires_at": link.expires_at,
            "label": link.label,
            "permission": link.permission,
            "password_required": True,
        }

    async def unlock(
        self,
        *,
        token: str,
        password: str,
        display_name: str | None,
        throttle_key: str,
    ) -> EditorGuestUnlock:
        if await self._rate_limiter.locked(throttle_key):
            raise EditorGuestLinkRateLimited()
        now = time.time()
        try:
            tenant_id = self._decode_token(token, version=_LINK_VERSION)
            link, _title = await self._store.resolve_link(
                tenant_id=tenant_id,
                token_digest=self._digest(token, purpose="link"),
                now=now,
            )
            password_valid = verify_password(link.password_hash, password)
        except (EditorGuestLinkNotFound, EditorGuestLinkExpired):
            verify_password(self._dummy_password_hash, password)
            await self._rate_limiter.record_failure(throttle_key)
            raise EditorGuestLinkNotFound() from None
        if not password_valid:
            await self._rate_limiter.record_failure(throttle_key)
            raise EditorGuestLinkNotFound()
        await self._rate_limiter.reset(throttle_key)
        name = self._normalize_display_name(display_name)
        session_token = self._encode_token(
            version=_SESSION_VERSION,
            tenant_id=tenant_id,
            material=secrets.token_bytes(32),
        )
        identity = EditorGuestIdentity(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            link_id=link.id,
            document_id=link.document_id,
            generation=link.generation,
            display_name=name,
            session_token_digest=self._digest(session_token, purpose="session"),
            created_at=now,
            last_seen_at=now,
            expires_at=link.expires_at,
        )
        access = await self._store.create_guest_identity(
            identity,
            verified_token_digest=link.token_digest,
            verified_password_hash=link.password_hash,
            stats_enabled=self._settings.stats_enabled,
            now=now,
        )
        return EditorGuestUnlock(access=access, session_token=session_token)

    async def session(
        self,
        session_token: str,
        *,
        display_name: str | None = None,
    ) -> EditorGuestAccess:
        tenant_id = self._decode_token(session_token, version=_SESSION_VERSION)
        return await self._store.resolve_guest_identity(
            tenant_id=tenant_id,
            session_token_digest=self._digest(
                session_token, purpose="session"
            ),
            now=time.time(),
            display_name=(
                self._normalize_display_name(display_name)
                if display_name is not None
                else None
            ),
            stats_enabled=self._settings.stats_enabled,
        )

    async def create_collaboration_session(
        self,
        *,
        session_token: str,
        protocol_version: int,
        schema_version: int,
        current_lease_token: str | None,
        rotation_command_id: uuid.UUID | None,
        display_name: str | None,
    ) -> dict[str, Any]:
        access = await self.session(
            session_token,
            display_name=display_name,
        )
        if access.link.permission != "view" and access.identity.display_name is None:
            raise ValueError("display_name_required")
        return await self._collaboration.create_guest_session(
            access=access,
            protocol_version=protocol_version,
            schema_version=schema_version,
            current_lease_token=current_lease_token,
            rotation_command_id=rotation_command_id,
        )

    @staticmethod
    def _normalize_display_name(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.strip().split())
        if not normalized or len(normalized) > 80:
            raise ValueError("display_name must contain 1 to 80 characters")
        return normalized

    @staticmethod
    def link_payload(link: EditorDocumentShareLink) -> dict[str, Any]:
        return {
            "id": str(link.id),
            "label": link.label,
            "permission": link.permission,
            "revision": link.revision,
            "expires_at": link.expires_at,
            "created_at": link.created_at,
            "updated_at": link.updated_at,
            "revoked_at": link.revoked_at,
            "successful_open_count": link.successful_open_count,
            "session_count": link.session_count,
            "last_accessed_at": link.last_accessed_at,
        }

    @staticmethod
    def guest_payload(access: EditorGuestAccess) -> dict[str, Any]:
        return {
            "document": {
                "id": access.link.document_id,
                "title": access.document_title,
                "content_markdown": access.content_markdown,
                "generation": access.link.generation,
                "persisted_sequence": access.persisted_sequence,
                "projection_sequence": access.projection_sequence,
                "comment_revision": access.comment_revision,
            },
            "guest": {
                "id": str(access.identity.id),
                "display_name": access.identity.display_name,
                "link_label": access.link.label,
            },
            "permission": access.link.permission,
            "expires_at": access.identity.expires_at,
        }

    def throttle_key(self, *, token: str, source_ip: str) -> str:
        token_key = self._digest(token, purpose="throttle")[:24]
        return f"editor-guest:{token_key}:{source_ip}"
