"""PostgreSQL store for secure editor guest links."""

from __future__ import annotations

import hmac
import uuid
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, insert, select, update

from inqtrix.project.editor_guest_links import (
    EditorDocumentShareLink,
    EditorGuestAccess,
    EditorGuestActorProfile,
    EditorGuestIdentity,
    EditorGuestLinkConflict,
    EditorGuestLinkExpired,
    EditorGuestLinkNotFound,
    EditorShareLinkPermission,
)
from inqtrix.storage.db import tenant_session
from inqtrix.storage.editor_collaboration_orm import editor_collaboration_leases
from inqtrix.storage.editor_guest_link_orm import (
    editor_document_guest_identities,
    editor_document_share_links,
)
from inqtrix.storage.editor_orm import editor_documents
from inqtrix.storage.user_events_postgres import append_user_invalidation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _link(row: Any) -> EditorDocumentShareLink:
    return EditorDocumentShareLink(
        id=row.id,
        tenant_id=str(row.tenant_id),
        document_id=str(row.document_id),
        generation=int(row.generation),
        label=str(row.label),
        permission=row.permission,
        token_digest=str(row.token_digest),
        password_hash=str(row.password_hash),
        created_by_user_id=row.created_by_user_id,
        revision=int(row.revision),
        expires_at=float(row.expires_at),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        revoked_at=(
            float(row.revoked_at) if row.revoked_at is not None else None
        ),
        successful_open_count=int(row.successful_open_count),
        session_count=int(row.session_count),
        last_accessed_at=(
            float(row.last_accessed_at)
            if row.last_accessed_at is not None
            else None
        ),
        last_command_id=row.last_command_id,
        last_command_payload_hash=str(row.last_command_payload_hash),
        last_command_kind=str(row.last_command_kind),
    )


def _identity(row: Any) -> EditorGuestIdentity:
    return EditorGuestIdentity(
        id=row.id,
        tenant_id=str(row.tenant_id),
        link_id=row.link_id,
        document_id=str(row.document_id),
        generation=int(row.generation),
        display_name=row.display_name,
        session_token_digest=str(row.session_token_digest),
        created_at=float(row.created_at),
        last_seen_at=float(row.last_seen_at),
        expires_at=float(row.expires_at),
        revoked_at=(
            float(row.revoked_at) if row.revoked_at is not None else None
        ),
        open_count=int(row.open_count),
        last_read_revision=int(row.last_read_revision),
    )


class PostgresEditorGuestLinkStore:
    """Tenant-RLS store with owner checks and immediate session revocation."""

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role

    def _session(
        self, tenant_id: str
    ) -> AbstractAsyncContextManager["AsyncSession"]:
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    @staticmethod
    async def _owned_document(
        session: "AsyncSession",
        *,
        tenant_id: str,
        document_id: str,
        actor_user_id: uuid.UUID,
        lock: bool,
    ) -> Any:
        statement = select(editor_documents).where(
            editor_documents.c.tenant_id == tenant_id,
            editor_documents.c.id == document_id,
            editor_documents.c.created_by_user_id == actor_user_id,
            editor_documents.c.deleted_at.is_(None),
        )
        if lock:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise EditorGuestLinkNotFound(document_id)
        if row.content_mode != "collaboration":
            raise EditorGuestLinkConflict("mode_conflict")
        return row

    @staticmethod
    def _command_replay(
        row: Any,
        *,
        command_id: uuid.UUID,
        command_payload_hash: str,
        command_kind: str,
    ) -> bool:
        if row.last_command_id != command_id:
            return False
        if (
            not hmac.compare_digest(
                str(row.last_command_payload_hash),
                command_payload_hash,
            )
            or row.last_command_kind != command_kind
        ):
            raise EditorGuestLinkConflict("command_conflict")
        return True

    async def create_link(
        self,
        link: EditorDocumentShareLink,
    ) -> EditorDocumentShareLink:
        async with self._session(link.tenant_id) as session:
            document = await self._owned_document(
                session,
                tenant_id=link.tenant_id,
                document_id=link.document_id,
                actor_user_id=link.created_by_user_id,
                lock=True,
            )
            if int(document.collaboration_generation) != link.generation:
                raise EditorGuestLinkConflict("generation_conflict")
            replay = (
                await session.execute(
                    select(editor_document_share_links).where(
                        editor_document_share_links.c.tenant_id
                        == link.tenant_id,
                        editor_document_share_links.c.last_command_id
                        == link.last_command_id,
                    )
                )
            ).one_or_none()
            if replay is not None:
                self._command_replay(
                    replay,
                    command_id=link.last_command_id,  # type: ignore[arg-type]
                    command_payload_hash=link.last_command_payload_hash,
                    command_kind="create",
                )
                return _link(replay)
            row = (
                await session.execute(
                    insert(editor_document_share_links)
                    .values(
                        id=link.id,
                        tenant_id=link.tenant_id,
                        document_id=link.document_id,
                        generation=link.generation,
                        label=link.label,
                        permission=link.permission,
                        token_digest=link.token_digest,
                        password_hash=link.password_hash,
                        created_by_user_id=link.created_by_user_id,
                        revision=1,
                        expires_at=link.expires_at,
                        created_at=link.created_at,
                        updated_at=link.updated_at,
                        revoked_at=None,
                        successful_open_count=0,
                        session_count=0,
                        last_accessed_at=None,
                        last_command_id=link.last_command_id,
                        last_command_payload_hash=link.last_command_payload_hash,
                        last_command_kind="create",
                    )
                    .returning(editor_document_share_links)
                )
            ).one()
        return _link(row)

    async def list_links(
        self,
        *,
        tenant_id: str,
        document_id: str,
        actor_user_id: uuid.UUID,
    ) -> tuple[EditorDocumentShareLink, ...]:
        async with self._session(tenant_id) as session:
            await self._owned_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                lock=False,
            )
            rows = (
                await session.execute(
                    select(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.document_id == document_id,
                    )
                    .order_by(
                        editor_document_share_links.c.created_at.desc(),
                        editor_document_share_links.c.id.desc(),
                    )
                )
            ).all()
        return tuple(_link(row) for row in rows)

    async def access_summary(
        self,
        *,
        tenant_id: str,
        document_id: str,
        actor_user_id: uuid.UUID,
        since: float,
        now: float,
    ) -> dict[str, int | float | None]:
        """Return bounded aggregate guest activity for one owner-visible window."""
        async with self._session(tenant_id) as session:
            await self._owned_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                lock=False,
            )
            active_links = (
                await session.execute(
                    select(func.count())
                    .select_from(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.document_id == document_id,
                        editor_document_share_links.c.revoked_at.is_(None),
                        editor_document_share_links.c.expires_at > now,
                    )
                )
            ).scalar_one()
            activity = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                editor_document_guest_identities.c.open_count
                            ),
                            0,
                        ).label("open_count"),
                        func.count().label("session_count"),
                        func.max(
                            editor_document_guest_identities.c.last_seen_at
                        ).label("last_accessed_at"),
                    ).where(
                        editor_document_guest_identities.c.tenant_id
                        == tenant_id,
                        editor_document_guest_identities.c.document_id
                        == document_id,
                        editor_document_guest_identities.c.created_at >= since,
                    )
                )
            ).one()
        return {
            "guest_link_count": int(active_links),
            "guest_open_count": int(activity.open_count),
            "guest_session_count": int(activity.session_count),
            "last_guest_accessed_at": (
                float(activity.last_accessed_at)
                if activity.last_accessed_at is not None
                else None
            ),
        }

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
        command_kind: str,
        now: float,
    ) -> EditorDocumentShareLink:
        async with self._session(tenant_id) as session:
            await self._owned_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            row = (
                await session.execute(
                    select(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.document_id == document_id,
                        editor_document_share_links.c.id == link_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise EditorGuestLinkNotFound(str(link_id))
            if self._command_replay(
                row,
                command_id=command_id,
                command_payload_hash=command_payload_hash,
                command_kind=command_kind,
            ):
                return _link(row)
            if row.revoked_at is not None:
                raise EditorGuestLinkExpired()
            if int(row.revision) != expected_revision:
                raise EditorGuestLinkConflict(
                    "revision_conflict",
                    current_revision=int(row.revision),
                )
            values: dict[str, Any] = {
                "revision": expected_revision + 1,
                "updated_at": now,
                "last_command_id": command_id,
                "last_command_payload_hash": command_payload_hash,
                "last_command_kind": command_kind,
            }
            if permission is not None:
                values["permission"] = permission
            if expires_at is not None:
                values["expires_at"] = expires_at
            if password_hash is not None:
                values["password_hash"] = password_hash
            if token_digest is not None:
                values["token_digest"] = token_digest
            stored = (
                await session.execute(
                    update(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.id == link_id,
                    )
                    .values(**values)
                    .returning(editor_document_share_links)
                )
            ).one()
            # Either half of the two-part credential going stale ends every
            # session opened with the old pair: a rotation that leaves the
            # already-connected guest inside would revoke nothing.
            if password_hash is not None or token_digest is not None:
                await self._revoke_guest_sessions(
                    session,
                    tenant_id=tenant_id,
                    link_id=link_id,
                    now=now,
                )
            await append_user_invalidation(
                session,
                tenant_id=tenant_id,
                target_user_id=actor_user_id,
                scope="collaboration_guest_policy",
                resource_type="editor_document",
                resource_id=document_id,
            )
        return _link(stored)

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
    ) -> EditorDocumentShareLink:
        async with self._session(tenant_id) as session:
            await self._owned_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            row = (
                await session.execute(
                    select(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.document_id == document_id,
                        editor_document_share_links.c.id == link_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise EditorGuestLinkNotFound(str(link_id))
            if self._command_replay(
                row,
                command_id=command_id,
                command_payload_hash=command_payload_hash,
                command_kind="revoke",
            ):
                return _link(row)
            if int(row.revision) != expected_revision:
                raise EditorGuestLinkConflict(
                    "revision_conflict",
                    current_revision=int(row.revision),
                )
            stored = (
                await session.execute(
                    update(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.id == link_id,
                    )
                    .values(
                        revision=expected_revision + 1,
                        revoked_at=now,
                        updated_at=now,
                        last_command_id=command_id,
                        last_command_payload_hash=command_payload_hash,
                        last_command_kind="revoke",
                    )
                    .returning(editor_document_share_links)
                )
            ).one()
            await self._revoke_guest_sessions(
                session,
                tenant_id=tenant_id,
                link_id=link_id,
                now=now,
            )
            await append_user_invalidation(
                session,
                tenant_id=tenant_id,
                target_user_id=actor_user_id,
                scope="collaboration_guest_policy",
                resource_type="editor_document",
                resource_id=document_id,
            )
        return _link(stored)

    @staticmethod
    async def _revoke_guest_sessions(
        session: "AsyncSession",
        *,
        tenant_id: str,
        link_id: uuid.UUID,
        now: float,
    ) -> None:
        # The caller holds the document and the link row. A guest identity
        # opened, or a guest lease minted, by a transaction that held one of
        # them first can carry a later request stamp than this revocation's
        # own; nothing is revoked before it was created or issued.
        await session.execute(
            update(editor_document_guest_identities)
            .where(
                editor_document_guest_identities.c.tenant_id == tenant_id,
                editor_document_guest_identities.c.link_id == link_id,
                editor_document_guest_identities.c.revoked_at.is_(None),
            )
            .values(
                revoked_at=func.greatest(
                    now, editor_document_guest_identities.c.created_at
                )
            )
        )
        await session.execute(
            update(editor_collaboration_leases)
            .where(
                editor_collaboration_leases.c.tenant_id == tenant_id,
                editor_collaboration_leases.c.guest_link_id == link_id,
                editor_collaboration_leases.c.revoked_at.is_(None),
            )
            .values(
                revoked_at=func.greatest(
                    now, editor_collaboration_leases.c.issued_at
                )
            )
        )

    async def resolve_link(
        self,
        *,
        tenant_id: str,
        token_digest: str,
        now: float,
    ) -> tuple[EditorDocumentShareLink, str]:
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(
                        *editor_document_share_links.c,
                        editor_documents.c.title.label("document_title"),
                        editor_documents.c.collaboration_generation.label(
                            "current_generation"
                        ),
                        editor_documents.c.deleted_at.label("document_deleted_at"),
                    )
                    .join(
                        editor_documents,
                        (editor_documents.c.tenant_id
                         == editor_document_share_links.c.tenant_id)
                        & (editor_documents.c.id
                           == editor_document_share_links.c.document_id),
                    )
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.token_digest == token_digest,
                    )
                )
            ).one_or_none()
            if row is None:
                raise EditorGuestLinkNotFound()
            if (
                row.revoked_at is not None
                or float(row.expires_at) <= now
                or row.document_deleted_at is not None
                or int(row.current_generation) != int(row.generation)
            ):
                raise EditorGuestLinkExpired()
        return _link(row), str(row.document_title)

    async def create_guest_identity(
        self,
        identity: EditorGuestIdentity,
        *,
        verified_token_digest: str,
        verified_password_hash: str,
        stats_enabled: bool,
        now: float,
    ) -> EditorGuestAccess:
        async with self._session(identity.tenant_id) as session:
            # Lock order: the document row, then the link row, the order of
            # revoking the link or rotating its credential. The new
            # identity's foreign key takes FOR KEY SHARE on the document
            # row; that lock is taken here, ahead of the link row, in the
            # same mode, so an unlock never holds the link while it waits
            # for a revocation or rotation that holds the document.
            await session.execute(
                select(editor_documents.c.id)
                .where(
                    editor_documents.c.tenant_id == identity.tenant_id,
                    editor_documents.c.id == identity.document_id,
                )
                .with_for_update(read=True, key_share=True)
            )
            link_row = (
                await session.execute(
                    select(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id
                        == identity.tenant_id,
                        editor_document_share_links.c.id == identity.link_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            # The password was checked against the pair the caller resolved,
            # before this transaction and without a lock. A rotation of
            # either half that committed since then, or that held the
            # document row while this transaction waited for it above,
            # already swept the sessions it could see, so the pair is
            # compared here, under the lock that writes the session, and a
            # replaced one is refused like a revocation.
            if (
                link_row is None
                or link_row.revoked_at is not None
                or float(link_row.expires_at) <= now
                or link_row.document_id != identity.document_id
                or int(link_row.generation) != identity.generation
                or not hmac.compare_digest(
                    str(link_row.token_digest), verified_token_digest
                )
                or not hmac.compare_digest(
                    str(link_row.password_hash), verified_password_hash
                )
            ):
                raise EditorGuestLinkExpired()
            row = (
                await session.execute(
                    insert(editor_document_guest_identities)
                    .values(
                        id=identity.id,
                        tenant_id=identity.tenant_id,
                        link_id=identity.link_id,
                        document_id=identity.document_id,
                        generation=identity.generation,
                        display_name=identity.display_name,
                        session_token_digest=identity.session_token_digest,
                        created_at=now,
                        last_seen_at=now,
                        expires_at=identity.expires_at,
                        revoked_at=None,
                        open_count=1,
                        last_read_revision=0,
                    )
                    .returning(editor_document_guest_identities)
                )
            ).one()
            if stats_enabled:
                await session.execute(
                    update(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id
                        == identity.tenant_id,
                        editor_document_share_links.c.id == identity.link_id,
                    )
                    .values(
                        successful_open_count=(
                            editor_document_share_links.c.successful_open_count
                            + 1
                        ),
                        session_count=(
                            editor_document_share_links.c.session_count + 1
                        ),
                        last_accessed_at=now,
                    )
                )
            document = await self._guest_document(
                session,
                tenant_id=identity.tenant_id,
                document_id=identity.document_id,
                generation=identity.generation,
            )
        return self._guest_access(_link(link_row), _identity(row), document)

    async def resolve_guest_identity(
        self,
        *,
        tenant_id: str,
        session_token_digest: str,
        now: float,
        display_name: str | None = None,
        stats_enabled: bool = True,
    ) -> EditorGuestAccess:
        async with self._session(tenant_id) as session:
            # Lock order: the link row, then its guest identity, the order
            # of revoking the link or rotating its credential, which holds
            # the link while it revokes the link's identities. The identity
            # is read without a lock only to find its link, and read again
            # under the link's lock, where a revocation or rotation that
            # committed in between is visible.
            seen = (
                await session.execute(
                    select(editor_document_guest_identities).where(
                        editor_document_guest_identities.c.tenant_id
                        == tenant_id,
                        editor_document_guest_identities.c.session_token_digest
                        == session_token_digest,
                    )
                )
            ).one_or_none()
            if (
                seen is None
                or seen.revoked_at is not None
                or float(seen.expires_at) <= now
            ):
                raise EditorGuestLinkExpired()
            link_row = (
                await session.execute(
                    select(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.id == seen.link_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            # FOR NO KEY UPDATE, the mode of the update below, which changes
            # no key column. A guest lease row checks its foreign keys on
            # this identity and on the link with FOR KEY SHARE, possibly the
            # identity first; that check does not wait for this lock, so a
            # lease issued or rotated for this guest while the link is held
            # here waits at the link without holding a lock this waits for.
            row = (
                await session.execute(
                    select(editor_document_guest_identities)
                    .where(
                        editor_document_guest_identities.c.tenant_id
                        == tenant_id,
                        editor_document_guest_identities.c.id == seen.id,
                    )
                    .with_for_update(key_share=True)
                )
            ).one_or_none()
            if (
                row is None
                or row.revoked_at is not None
                or float(row.expires_at) <= now
                or link_row is None
                or row.link_id != link_row.id
                or link_row.revoked_at is not None
                or float(link_row.expires_at) <= now
                or int(link_row.generation) != int(row.generation)
            ):
                raise EditorGuestLinkExpired()
            identity_values: dict[str, Any] = {"last_seen_at": now}
            if display_name is not None:
                identity_values["display_name"] = display_name
            stored = (
                await session.execute(
                    update(editor_document_guest_identities)
                    .where(
                        editor_document_guest_identities.c.tenant_id == tenant_id,
                        editor_document_guest_identities.c.id == row.id,
                    )
                    .values(**identity_values)
                    .returning(editor_document_guest_identities)
                )
            ).one()
            if stats_enabled:
                await session.execute(
                    update(editor_document_share_links)
                    .where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.id == row.link_id,
                    )
                    .values(last_accessed_at=now)
                )
            document = await self._guest_document(
                session,
                tenant_id=tenant_id,
                document_id=str(row.document_id),
                generation=int(row.generation),
            )
        return self._guest_access(_link(link_row), _identity(stored), document)

    async def guest_identity_by_id(
        self,
        *,
        tenant_id: str,
        guest_identity_id: uuid.UUID,
        now: float,
    ) -> tuple[EditorGuestIdentity, EditorDocumentShareLink] | None:
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(editor_document_guest_identities).where(
                        editor_document_guest_identities.c.tenant_id
                        == tenant_id,
                        editor_document_guest_identities.c.id
                        == guest_identity_id,
                    )
                )
            ).one_or_none()
            if (
                row is None
                or row.revoked_at is not None
                or float(row.expires_at) <= now
            ):
                return None
            link_row = (
                await session.execute(
                    select(editor_document_share_links).where(
                        editor_document_share_links.c.tenant_id == tenant_id,
                        editor_document_share_links.c.id == row.link_id,
                        editor_document_share_links.c.revoked_at.is_(None),
                        editor_document_share_links.c.expires_at > now,
                    )
                )
            ).one_or_none()
            if link_row is None:
                return None
        return _identity(row), _link(link_row)

    async def guest_actor_profiles(
        self,
        *,
        tenant_id: str,
        guest_identity_ids: tuple[uuid.UUID, ...],
    ) -> dict[uuid.UUID, EditorGuestActorProfile]:
        """Resolve durable display metadata without requiring a live session."""
        if not guest_identity_ids:
            return {}
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(
                        editor_document_guest_identities.c.id,
                        editor_document_guest_identities.c.display_name,
                        editor_document_share_links.c.label,
                    )
                    .join(
                        editor_document_share_links,
                        (
                            editor_document_share_links.c.tenant_id
                            == editor_document_guest_identities.c.tenant_id
                        )
                        & (
                            editor_document_share_links.c.id
                            == editor_document_guest_identities.c.link_id
                        ),
                    )
                    .where(
                        editor_document_guest_identities.c.tenant_id
                        == tenant_id,
                        editor_document_guest_identities.c.id.in_(
                            guest_identity_ids
                        ),
                    )
                )
            ).all()
        return {
            row.id: EditorGuestActorProfile(
                id=row.id,
                display_name=row.display_name,
                link_label=str(row.label),
            )
            for row in rows
        }

    @staticmethod
    async def _guest_document(
        session: "AsyncSession",
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
    ) -> Any:
        row = (
            await session.execute(
                select(editor_documents).where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.id == document_id,
                    editor_documents.c.content_mode == "collaboration",
                    editor_documents.c.collaboration_generation == generation,
                    editor_documents.c.deleted_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            raise EditorGuestLinkExpired()
        return row

    @staticmethod
    def _guest_access(
        link: EditorDocumentShareLink,
        identity: EditorGuestIdentity,
        document: Any,
    ) -> EditorGuestAccess:
        return EditorGuestAccess(
            link=link,
            identity=identity,
            document_title=str(document.title),
            content_markdown=str(document.content_markdown),
            persisted_sequence=int(document.persisted_sequence),
            projection_sequence=int(document.projection_sequence),
            comment_revision=int(document.collaboration_comment_revision),
        )
