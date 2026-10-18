"""Ports and immutable models for account-less editor guest links."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

EditorShareLinkPermission = Literal["view", "comment", "suggest", "edit"]


class EditorGuestLinkNotFound(KeyError):
    """The link, document, or guest session is absent or tenant-invisible."""


class EditorGuestLinkConflict(RuntimeError):
    """An optimistic revision, command, or generation no longer matches."""

    def __init__(self, reason: str, *, current_revision: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current_revision = current_revision


class EditorGuestLinkExpired(PermissionError):
    """The link or guest session expired or was revoked."""


class EditorGuestLinkRateLimited(PermissionError):
    """Password attempts for a token/source pair are temporarily blocked."""


@dataclass(frozen=True)
class EditorDocumentShareLink:
    id: uuid.UUID
    tenant_id: str
    document_id: str
    generation: int
    label: str
    permission: EditorShareLinkPermission
    token_digest: str
    password_hash: str
    created_by_user_id: uuid.UUID
    revision: int
    expires_at: float
    created_at: float
    updated_at: float
    revoked_at: float | None = None
    successful_open_count: int = 0
    session_count: int = 0
    last_accessed_at: float | None = None
    last_command_id: uuid.UUID | None = None
    last_command_payload_hash: str = ""
    last_command_kind: str = "create"


@dataclass(frozen=True)
class EditorGuestIdentity:
    id: uuid.UUID
    tenant_id: str
    link_id: uuid.UUID
    document_id: str
    generation: int
    display_name: str | None
    session_token_digest: str
    created_at: float
    last_seen_at: float
    expires_at: float
    revoked_at: float | None = None
    open_count: int = 1
    last_read_revision: int = 0


@dataclass(frozen=True)
class EditorGuestAccess:
    link: EditorDocumentShareLink
    identity: EditorGuestIdentity
    document_title: str
    content_markdown: str
    persisted_sequence: int
    projection_sequence: int
    comment_revision: int


@dataclass(frozen=True)
class EditorGuestActorProfile:
    """Stable display metadata for historical guest-authored activity."""

    id: uuid.UUID
    display_name: str | None
    link_label: str


@runtime_checkable
class EditorGuestLinkStore(Protocol):
    async def access_summary(
        self,
        *,
        tenant_id: str,
        document_id: str,
        actor_user_id: uuid.UUID,
        since: float,
        now: float,
    ) -> dict[str, int | float | None]: ...

    async def create_link(
        self,
        link: EditorDocumentShareLink,
    ) -> EditorDocumentShareLink: ...

    async def list_links(
        self,
        *,
        tenant_id: str,
        document_id: str,
        actor_user_id: uuid.UUID,
    ) -> tuple[EditorDocumentShareLink, ...]: ...

    async def update_link(
        self,
        *,
        tenant_id: str,
        document_id: str,
        link_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        permission: EditorShareLinkPermission | None,
        expires_at: float | None,
        password_hash: str | None,
        token_digest: str | None,
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        command_kind: Literal["update", "rotate_password", "rotate_token"],
        now: float,
    ) -> EditorDocumentShareLink: ...

    async def revoke_link(
        self,
        *,
        tenant_id: str,
        document_id: str,
        link_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> EditorDocumentShareLink: ...

    async def resolve_link(
        self,
        *,
        tenant_id: str,
        token_digest: str,
        now: float,
    ) -> tuple[EditorDocumentShareLink, str]: ...

    async def create_guest_identity(
        self,
        identity: EditorGuestIdentity,
        *,
        verified_token_digest: str,
        verified_password_hash: str,
        stats_enabled: bool,
        now: float,
    ) -> EditorGuestAccess:
        """Open a guest session on a link that still carries the verified pair.

        ``verified_token_digest`` and ``verified_password_hash`` are the
        credential pair the caller resolved and checked the password
        against. The password check runs before this call and outside any
        lock, so the store compares the pair with the link's current one
        under the lock that writes the identity: a password or token
        replaced in between refuses the session like a revoked link.
        """
        ...

    async def resolve_guest_identity(
        self,
        *,
        tenant_id: str,
        session_token_digest: str,
        now: float,
        display_name: str | None = None,
        stats_enabled: bool = True,
    ) -> EditorGuestAccess: ...

    async def guest_identity_by_id(
        self,
        *,
        tenant_id: str,
        guest_identity_id: uuid.UUID,
        now: float,
    ) -> tuple[EditorGuestIdentity, EditorDocumentShareLink] | None: ...

    async def guest_actor_profiles(
        self,
        *,
        tenant_id: str,
        guest_identity_ids: tuple[uuid.UUID, ...],
    ) -> dict[uuid.UUID, EditorGuestActorProfile]: ...
