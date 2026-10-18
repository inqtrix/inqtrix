"""PostgreSQL implementation of the editor-collaboration persistence port."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, func, insert, null, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.auth.permissions import (
    SharePermission,
    share_permissions_satisfying,
)
from inqtrix.project.editor_collaboration_ports import (
    CollaborationActivity,
    CollaborationChangeKind,
    CollaborationCommentActivity,
    CollaborationCommentMessage,
    CollaborationCommentPage,
    CollaborationCommentThread,
    CollaborationConflict,
    CollaborationDocumentNotFound,
    CollaborationDocumentState,
    CollaborationInstanceFenced,
    CollaborationInstanceLease,
    CollaborationLease,
    CollaborationLeaseInvalid,
    CollaborationLoadedState,
    CollaborationOpenPatch,
    CollaborationOpenPatchPage,
    CollaborationPatchState,
    CollaborationPolicyEvent,
    CollaborationPolicyPage,
    CollaborationPersistedCommand,
    CollaborationRateLimited,
    CollaborationSnapshot,
    CollaborationSnapshotCandidate,
    CollaborationSuggestionKind,
    CollaborationSuggestionRejection,
    CollaborationUpdate,
    CollaborationUpdateLookup,
    PersistCollaborationUpdate,
    PersistedCollaborationUpdate,
)
from inqtrix.project.editor_patch_ports import patch_created_at_ms
from inqtrix.storage.auth_orm import auth_sessions
from inqtrix.storage.db import tenant_session
from inqtrix.storage.editor_collaboration_orm import (
    editor_collaboration_comment_messages,
    editor_collaboration_comment_reads,
    editor_collaboration_comment_threads,
    editor_collaboration_instances,
    editor_collaboration_leases,
    editor_collaboration_snapshots,
    editor_collaboration_updates,
)
from inqtrix.storage.editor_guest_link_orm import (
    editor_document_guest_identities,
    editor_document_share_links,
)
from inqtrix.storage.editor_orm import editor_comments, editor_documents
from inqtrix.storage.editor_patch_orm import (
    editor_patch_created_at_ms,
    editor_patches,
)
from inqtrix.storage.identity_orm import audit_log, resource_shares, users
from inqtrix.storage.user_event_orm import user_events
from inqtrix.storage.resource_access import (
    VISIBLE_SHARE_PERMISSION,
    append_audit_row,
    append_resource_effects,
    lock_resource_access,
    revoke_resource_shares,
    visible_resource_select,
)
from inqtrix.storage.user_events_postgres import append_user_invalidation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger("inqtrix")


SUGGESTION_REMOVED_BY_EDIT_ACTION = "editor.collaboration_suggestion.removed_by_edit"
"""Audit action of one pending suggestion an edit removed by deleting every
character it proposed."""


@dataclass(frozen=True)
class _RejectedSuggestion:
    """One suggestion an update removed, resolved from its stored patch."""

    suggestion_id: str
    patch_id: str
    author_kind: str
    author_id: uuid.UUID
    kind: str | None
    patch_rejected: bool


def _patch_author(row: Any) -> tuple[str, uuid.UUID | None]:
    """The author of a stored patch: a guest identity or a member.

    Read from whichever creator column is set, independent of who acts on the
    patch now, so a guest may remove a member's suggestion and the reverse
    while the author is still checked.
    """
    guest_id = row["created_by_guest_identity_id"]
    if guest_id is not None:
        return "guest", guest_id
    return "user", row["created_by_user_id"]


def _require_sha256(value: str, *, field: str) -> None:
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _suggestion_kind_for_edit(
    edit: dict[str, Any],
) -> CollaborationSuggestionKind:
    """Infer the visible suggestion kind from an exact anchored edit."""
    find = edit.get("find")
    replacement = edit.get("text")
    position = edit.get("position")
    if position in {"append", "prepend"} or (not find and replacement):
        return "insertion"
    if find and not replacement:
        return "deletion"
    return "replacement"


async def _has_creator_private_suggestion_draft(
    session: "AsyncSession",
    *,
    tenant_id: str,
    document_id: str,
    creator_user_id: uuid.UUID,
    patch_id: str,
    publication_command_id: uuid.UUID,
) -> bool:
    """Whether a creator-private draft authorizes one shared patch publish."""
    draft_comment_id = await session.scalar(
        select(editor_comments.c.id)
        .where(
            editor_comments.c.tenant_id == tenant_id,
            editor_comments.c.document_id == document_id,
            editor_comments.c.created_by_user_id == creator_user_id,
            editor_comments.c.suggestion_draft["patch_id"].astext == patch_id,
            editor_comments.c.suggestion_draft["publication_command_id"].astext
            == str(publication_command_id),
        )
        .limit(1)
    )
    return draft_comment_id is not None


def _is_valid_slash_structure_supersession(
    *,
    patch_state: CollaborationPatchState,
    prior_active_ids: set[str],
    active_ids: set[str],
    stored_descriptors: dict[str, dict[str, Any]],
    incoming_descriptors: dict[str, dict[str, Any]],
    actor_user_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    source: str,
    change_kind: CollaborationChangeKind,
) -> bool:
    """Validate the persisted half of one sidecar-proven slash replacement.

    The Node policy owns the content-level proof that the removed insertion is
    a complete ``/query``. PostgreSQL independently pins the only membership
    transition that proof may authorize: one actor-owned insertion is replaced
    by one structure suggestion with identical patch metadata.
    """
    superseded_ids = set(patch_state.superseded_suggestion_ids)
    if (
        source != "human"
        or change_kind != "suggestion"
        or actor_user_id != created_by_user_id
        or len(prior_active_ids) != 1
        or len(active_ids) != 1
        or superseded_ids != prior_active_ids
        or active_ids & superseded_ids
        or patch_state.kinds != ("structure",)
    ):
        return False
    prior_id = next(iter(prior_active_ids))
    replacement_id = next(iter(active_ids))
    stored_prior = stored_descriptors.get(prior_id)
    reported_prior = incoming_descriptors.get(prior_id)
    replacement = incoming_descriptors.get(replacement_id)
    expected_metadata = {
        "patch_id": patch_state.patch_id,
        "author_id": str(created_by_user_id),
        "created_at": patch_state.created_at,
    }
    if (
        stored_prior is None
        or reported_prior != stored_prior
        or set(incoming_descriptors) != {prior_id, replacement_id}
    ):
        return False
    return (
        stored_prior.get("suggestion_id") == prior_id
        and stored_prior.get("kind") == "insertion"
        and all(
            stored_prior.get(field) == value
            for field, value in expected_metadata.items()
        )
        and replacement is not None
        and replacement.get("suggestion_id") == replacement_id
        and replacement.get("kind") == "structure"
        and all(
            replacement.get(field) == value
            for field, value in expected_metadata.items()
        )
    )


def _document_state(row: Any) -> CollaborationDocumentState:
    return CollaborationDocumentState(
        document_id=str(row.id),
        tenant_id=str(row.tenant_id),
        generation=int(row.collaboration_generation),
        schema_version=int(row.collaboration_schema_version),
        schema_hash=str(row.collaboration_schema_hash),
        persisted_sequence=int(row.persisted_sequence),
        projection_sequence=int(row.projection_sequence),
        content_markdown=str(row.content_markdown),
        projection_updated_at=row.projection_updated_at,
        owner_user_id=row.created_by_user_id,
        deleted_at=row.deleted_at,
    )


def _snapshot(row: Any) -> CollaborationSnapshot:
    return CollaborationSnapshot(
        document_id=str(row.document_id),
        tenant_id=str(row.tenant_id),
        generation=int(row.generation),
        covered_sequence=int(row.covered_sequence),
        state_update=bytes(row.state_update),
        state_vector=bytes(row.state_vector),
        state_hash=str(row.state_hash),
        projection_hash=str(row.projection_hash),
        schema_version=int(row.schema_version),
        schema_hash=str(row.schema_hash),
        created_at=float(row.created_at),
    )


def _collaboration_update(row: Any) -> CollaborationUpdate:
    return CollaborationUpdate(
        document_id=str(row.document_id),
        tenant_id=str(row.tenant_id),
        generation=int(row.generation),
        sequence=int(row.sequence),
        update_hash=str(row.update_hash),
        update_bytes=(
            bytes(row.update_bytes) if row.update_bytes is not None else None
        ),
        actor_user_id=row.actor_user_id,
        actor_guest_identity_id=row.actor_guest_identity_id,
        actor_kind=row.actor_kind,
        change_kind=row.change_kind,
        suggestion_ids=tuple(str(item) for item in (row.suggestion_ids or [])),
        change_summary=dict(row.change_summary or {}),
        decision_outcome=row.decision_outcome,
        command_id=row.command_id,
        created_at=float(row.created_at),
        payload_pruned_at=row.payload_pruned_at,
    )


def _lease(row: Any) -> CollaborationLease:
    return CollaborationLease(
        lease_id=row.lease_id,
        token_hash=str(row.token_hash),
        tenant_id=str(row.tenant_id),
        document_id=str(row.document_id),
        generation=int(row.generation),
        user_id=row.user_id,
        permission=row.permission,
        session_id=(str(row.session_id) if row.session_id is not None else None),
        issued_at=float(row.issued_at),
        expires_at=float(row.expires_at),
        last_validated_at=float(row.validated_at or row.issued_at),
        actor_kind=row.actor_kind,
        guest_identity_id=row.guest_identity_id,
        guest_link_id=row.guest_link_id,
        rotation_command_id=row.rotation_command_id,
        rotated_from_lease_id=row.rotated_from_lease_id,
        revoked_at=row.revoked_at,
    )


def _lease_values(lease: CollaborationLease) -> dict[str, Any]:
    return {
        "lease_id": lease.lease_id,
        "tenant_id": lease.tenant_id,
        "token_hash": lease.token_hash,
        "document_id": lease.document_id,
        "generation": lease.generation,
        "user_id": lease.user_id,
        "actor_kind": lease.actor_kind,
        "guest_identity_id": lease.guest_identity_id,
        "guest_link_id": lease.guest_link_id,
        "permission": lease.permission,
        "session_id": lease.session_id,
        "issued_at": lease.issued_at,
        "expires_at": lease.expires_at,
        "validated_at": lease.last_validated_at,
        "revoked_at": lease.revoked_at,
        "rotation_command_id": lease.rotation_command_id,
        "rotated_from_lease_id": lease.rotated_from_lease_id,
    }


def _comment_message(row: Any) -> CollaborationCommentMessage:
    return CollaborationCommentMessage(
        message_id=row.id,
        thread_id=row.thread_id,
        revision=int(row.revision),
        author_user_id=row.author_user_id,
        author_guest_identity_id=row.author_guest_identity_id,
        body_markdown=str(row.body_markdown),
        mention_user_ids=tuple(
            uuid.UUID(str(value)) for value in (row.mention_user_ids or [])
        ),
        created_at=float(row.created_at),
        edited_at=float(row.edited_at) if row.edited_at is not None else None,
        deleted_at=(
            float(row.deleted_at) if row.deleted_at is not None else None
        ),
    )


def _comment_thread(
    row: Any,
    messages: tuple[CollaborationCommentMessage, ...],
) -> CollaborationCommentThread:
    return CollaborationCommentThread(
        thread_id=row.id,
        document_id=str(row.document_id),
        generation=int(row.generation),
        revision=int(row.revision),
        status=row.status,
        created_by_user_id=row.created_by_user_id,
        created_by_guest_identity_id=row.created_by_guest_identity_id,
        resolved_by_user_id=row.resolved_by_user_id,
        resolved_by_guest_identity_id=row.resolved_by_guest_identity_id,
        resolved_at=(
            float(row.resolved_at) if row.resolved_at is not None else None
        ),
        anchor=dict(row.anchor or {}),
        quote_text=str(row.quote_text),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        deleted_at=(
            float(row.deleted_at) if row.deleted_at is not None else None
        ),
        messages=messages,
    )


def _persisted_command(
    row: Any,
    patch_rows: list[Any],
) -> CollaborationPersistedCommand:
    patch_ids = tuple(sorted(str(item.patch_id) for item in patch_rows))
    if not patch_ids:
        raise CollaborationConflict("command_metadata_missing")
    decision = None
    if row.change_kind == "decision":
        statuses = {str(item.status) for item in patch_rows}
        if statuses == {"accepted"}:
            decision = "accept"
        elif statuses == {"rejected"}:
            decision = "reject"
        else:
            raise CollaborationConflict("command_metadata_conflict")
    return CollaborationPersistedCommand(
        actor_kind=row.actor_kind,
        actor_user_id=row.actor_user_id,
        change_kind=row.change_kind,
        command_id=row.command_id,
        command_payload_hash=row.command_payload_hash,
        decision=decision,
        generation=int(row.generation),
        patch_ids=patch_ids,
        sequence=int(row.sequence),
        suggestion_ids=tuple(str(item) for item in (row.suggestion_ids or [])),
        update_hash=str(row.update_hash),
    )


async def _lock_instance_fence(
    session: "AsyncSession",
    *,
    tenant_id: str,
    instance_id: str,
    instance_epoch: int,
    now: float,
) -> Any:
    """Lock and validate the writer fence inside its mutation transaction.

    The instance row is read FOR KEY SHARE with the writer's instance id,
    epoch and an unexpired lease. Writes of the instance therefore do not
    exclude each other (the document row orders the writes of one
    document), and a renewal, a non-key UPDATE, never waits for them.

    The only lock that conflicts is the explicit FOR UPDATE that
    ``acquire_instance`` takes before a takeover. It waits for every write
    in flight, and it marks the replaced row version as key-updated.
    PostgreSQL lets a FOR KEY SHARE request lock a version that a committed
    non-key UPDATE replaced, and returns that old version without
    re-checking the predicate. The key-updated mark forbids this: the
    request follows the update chain, re-checks the predicate on the new
    version and finds no row for the old epoch, whether it asked while the
    takeover ran or after it committed. A takeover without that lock, or
    with FOR NO KEY UPDATE, FOR SHARE or FOR KEY SHARE, sets no mark, and a
    write of the old epoch could commit after it.

    Callers issue no further statement on this row in the same transaction,
    so the shared lock is never upgraded.
    """
    row = (
        await session.execute(
            select(editor_collaboration_instances)
            .where(
                editor_collaboration_instances.c.slot == "primary",
                editor_collaboration_instances.c.tenant_id == tenant_id,
                editor_collaboration_instances.c.instance_id == instance_id,
                editor_collaboration_instances.c.epoch == instance_epoch,
                editor_collaboration_instances.c.lease_expires_at > now,
            )
            .with_for_update(read=True, key_share=True)
        )
    ).one_or_none()
    if row is None:
        raise CollaborationInstanceFenced("stale collaboration instance")
    return row


def _lease_matches_update_actor(
    lease: Any,
    update_record: PersistCollaborationUpdate,
) -> bool:
    """Return whether one lease belongs to the update's exact actor union."""
    if update_record.actor_kind == "guest":
        return (
            lease.actor_kind == "guest"
            and lease.guest_identity_id
            == update_record.actor_guest_identity_id
        )
    return (
        lease.actor_kind == "user"
        and lease.user_id == update_record.actor_user_id
    )


def _same_rotation_authority(previous: Any, successor: Any) -> bool:
    """Require one rotation to preserve identity and connection scope."""
    return (
        successor.tenant_id == previous.tenant_id
        and successor.document_id == previous.document_id
        and int(successor.generation) == int(previous.generation)
        and successor.actor_kind == previous.actor_kind
        and successor.user_id == previous.user_id
        and successor.guest_identity_id == previous.guest_identity_id
        and successor.guest_link_id == previous.guest_link_id
        and successor.session_id == previous.session_id
    )


async def _lock_append_lease(
    session: "AsyncSession",
    *,
    update_record: PersistCollaborationUpdate,
    now: float,
) -> Any:
    """Lock the current authority for one already-started client update.

    The caller already holds the document row: every update, lease issue,
    lease rotation, document deletion and guest-link revocation locks the
    document before its lease rows, so deleting
    the document or revoking a guest link never holds a lease row this
    update waits for while waiting for the document the update holds. An
    expired lease grants nothing and is never locked.

    A token refresh atomically revokes a lease before the WebSocket can
    replace the context captured by an update already in progress. Such an
    update may use exactly the active immediate successor while the original
    lease is still within its lifetime. All current access, session and
    permission checks remain downstream and use that successor.

    The successor is identified from the lease rows under their locks: the
    rotation link, the revocation stamp the rotation wrote and the
    successor's own state. It never depends on whether the update arrived
    before or after the rotation was stamped. Both stamps are taken before
    either transaction locks the lease row, possibly by different API
    processes, so a rotation stamped after an update arrived can still
    commit before the update reaches the row. A revocation that mints no
    successor, or one that revokes the successor, refuses the update at
    once.
    """
    supplied = (
        await session.execute(
            select(editor_collaboration_leases)
            .where(
                editor_collaboration_leases.c.tenant_id
                == update_record.tenant_id,
                editor_collaboration_leases.c.lease_id
                == update_record.lease_id,
                editor_collaboration_leases.c.document_id
                == update_record.document_id,
                editor_collaboration_leases.c.generation
                == update_record.generation,
                editor_collaboration_leases.c.expires_at > now,
            )
            .with_for_update()
        )
    ).one_or_none()
    if supplied is None or not _lease_matches_update_actor(
        supplied, update_record
    ):
        raise CollaborationLeaseInvalid()
    if supplied.revoked_at is None:
        return supplied

    successors = (
        await session.execute(
            select(editor_collaboration_leases)
            .where(
                editor_collaboration_leases.c.tenant_id
                == update_record.tenant_id,
                editor_collaboration_leases.c.rotated_from_lease_id
                == supplied.lease_id,
                editor_collaboration_leases.c.document_id
                == update_record.document_id,
                editor_collaboration_leases.c.generation
                == update_record.generation,
                editor_collaboration_leases.c.revoked_at.is_(None),
                editor_collaboration_leases.c.expires_at > now,
            )
            .limit(2)
            .with_for_update()
        )
    ).all()
    if len(successors) != 1:
        raise CollaborationLeaseInvalid()
    successor = successors[0]
    if (
        supplied.revoked_at != successor.issued_at
        or successor.rotation_command_id is None
        or not _same_rotation_authority(supplied, successor)
        or not _lease_matches_update_actor(successor, update_record)
    ):
        raise CollaborationLeaseInvalid()
    return successor


async def _lock_lease_rate_scope(
    session: "AsyncSession",
    *,
    tenant_id: str,
    user_id: uuid.UUID,
) -> None:
    """Serialize per-user issuance counts across document lock domains."""
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"editor-collaboration-lease:{tenant_id}:{user_id}",
                    0,
                )
            )
        )
    )


async def _lock_command_scope(
    session: "AsyncSession",
    *,
    tenant_id: str,
    command_id: uuid.UUID,
) -> None:
    """Order the persists of one tenant that carry the same command id.

    Persists of different documents share the writer fence, so without this
    lock two of them could both miss each other's row in the command lookup
    and one would fail on the global unique index instead of being answered
    as a command conflict. Transaction-scoped, as the pooler contract
    requires; the tenant in the key keeps tenants from waiting on each other.
    """
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"editor-collaboration-command:{tenant_id}:{command_id}",
                    0,
                )
            )
        )
    )


# Expired lease rows stay readable for one day (debugging a session storm
# needs the trail), then the actor who mints the next lease sweeps their own
# remains. Cleaning at the only two row-producing call sites keeps the table
# bounded without a cross-tenant maintenance door: the delete runs inside the
# caller's tenant scope, under the (tenant, user) advisory lock both paths
# already hold, and over the caller's own rows via ix_collaboration_leases_user.
_LEASE_SWEEP_GRACE_SECONDS = 24 * 3600.0


async def _sweep_expired_actor_leases(
    session: "AsyncSession",
    lease: CollaborationLease,
) -> None:
    await session.execute(
        delete(editor_collaboration_leases).where(
            editor_collaboration_leases.c.tenant_id == lease.tenant_id,
            (
                editor_collaboration_leases.c.user_id == lease.user_id
                if lease.actor_kind == "user"
                else editor_collaboration_leases.c.guest_identity_id
                == lease.guest_identity_id
            ),
            editor_collaboration_leases.c.expires_at
            < lease.issued_at - _LEASE_SWEEP_GRACE_SECONDS,
        )
    )


async def _active_guest_access(
    session: "AsyncSession",
    *,
    tenant_id: str,
    guest_identity_id: uuid.UUID,
    guest_link_id: uuid.UUID,
    document_id: str,
    generation: int,
    minimum: SharePermission,
    now: float,
    allow_comment: bool = False,
    guest_links_enabled: bool = True,
) -> Any | None:
    """Return the live guest identity/link pair for one required capability."""
    if not guest_links_enabled:
        return None
    row = (
        await session.execute(
            select(
                editor_document_guest_identities.c.id.label(
                    "guest_identity_id"
                ),
                editor_document_share_links.c.id.label("guest_link_id"),
                editor_document_share_links.c.permission,
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
                editor_document_guest_identities.c.tenant_id == tenant_id,
                editor_document_guest_identities.c.id == guest_identity_id,
                editor_document_guest_identities.c.link_id == guest_link_id,
                editor_document_guest_identities.c.document_id == document_id,
                editor_document_guest_identities.c.generation == generation,
                editor_document_guest_identities.c.revoked_at.is_(None),
                editor_document_guest_identities.c.expires_at > now,
                editor_document_share_links.c.document_id == document_id,
                editor_document_share_links.c.generation == generation,
                editor_document_share_links.c.revoked_at.is_(None),
                editor_document_share_links.c.expires_at > now,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    if allow_comment:
        return row if str(row.permission) in {"comment", "suggest", "edit"} else None
    permission_rank = {
        "view": 1,
        "comment": 1,
        "suggest": 2,
        "edit": 3,
    }
    minimum_rank = {
        SharePermission.VIEW: 1,
        SharePermission.SUGGEST: 2,
        SharePermission.EDIT: 3,
    }[minimum]
    if permission_rank.get(str(row.permission), 0) < minimum_rank:
        return None
    return row


async def _lock_comment_document(
    session: "AsyncSession",
    *,
    tenant_id: str,
    document_id: str,
    generation: int,
    actor_user_id: uuid.UUID | None,
    actor_guest_identity_id: uuid.UUID | None,
    guest_link_id: uuid.UUID | None,
    minimum: SharePermission,
    restrict_to_workspace_members: bool,
    sharing_enabled: bool,
    guest_links_enabled: bool,
    now: float,
    allow_comment: bool = False,
) -> tuple[Any, uuid.UUID]:
    lock_document = (
        select(editor_documents)
        .where(
            editor_documents.c.tenant_id == tenant_id,
            editor_documents.c.id == document_id,
        )
        .with_for_update()
    )
    is_guest = actor_guest_identity_id is not None or guest_link_id is not None
    if is_guest:
        if (
            actor_user_id is not None
            or actor_guest_identity_id is None
            or guest_link_id is None
        ):
            raise ValueError("shared-comment actor union is invalid")
        # The guest's identity and link are checked after the document row
        # is locked, the row that revoking the link or rotating its
        # password or token locks first. A change that holds the row has
        # committed when the check reads the guest's access, so the comment
        # is refused; one that arrives later waits for the comment. The row
        # is locked once, FOR UPDATE, the lock that orders the comment
        # revision: a shared lock taken first and raised later would let
        # two comments each hold a lock the other waits for.
        row = (await session.execute(lock_document)).one_or_none()
        access = await _active_guest_access(
            session,
            tenant_id=tenant_id,
            guest_identity_id=actor_guest_identity_id,
            guest_link_id=guest_link_id,
            document_id=document_id,
            generation=generation,
            minimum=minimum,
            now=now,
            allow_comment=allow_comment,
            guest_links_enabled=guest_links_enabled,
        )
        if row is None or access is None:
            raise CollaborationDocumentNotFound(document_id)
    else:
        if actor_user_id is None:
            raise ValueError("shared-comment actor is required")
        access = await lock_resource_access(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            resource_type="editor_document",
            resource_table=editor_documents,
            id_column=editor_documents.c.id,
            resource_id=document_id,
            owner_column=editor_documents.c.created_by_user_id,
            minimum=minimum,
            restrict_to_workspace_members=restrict_to_workspace_members,
            sharing_enabled=sharing_enabled,
        )
        if access is None or access.owner_user_id is None:
            raise CollaborationDocumentNotFound(document_id)
        row = (await session.execute(lock_document)).one()
    if (
        row.content_mode != "collaboration"
        or row.deleted_at is not None
        or int(row.collaboration_generation) != generation
    ):
        raise CollaborationConflict("generation_conflict")
    owner_user_id = row.created_by_user_id
    if owner_user_id is None:
        raise CollaborationDocumentNotFound(document_id)
    return row, owner_user_id


async def _read_comment_document(
    session: "AsyncSession",
    *,
    tenant_id: str,
    document_id: str,
    generation: int,
    actor_user_id: uuid.UUID | None,
    actor_guest_identity_id: uuid.UUID | None,
    guest_link_id: uuid.UUID | None,
    restrict_to_workspace_members: bool,
    sharing_enabled: bool,
    guest_links_enabled: bool,
    now: float,
) -> tuple[Any, uuid.UUID]:
    """Authorize a comment read/read-state write without mutation row locks.

    Listing a thread or advancing a personal read coordinate never changes the
    document authority. Taking the mutation path's user and document
    ``FOR UPDATE`` locks here creates an avoidable inversion with a comment
    transaction that owns the document and appends user-event foreign keys.
    A single MVCC snapshot is sufficient: callers either return content already
    authorized in that snapshot or update only the actor's personal read row.
    """
    is_guest = actor_guest_identity_id is not None or guest_link_id is not None
    if is_guest:
        if (
            actor_user_id is not None
            or actor_guest_identity_id is None
            or guest_link_id is None
        ):
            raise ValueError("shared-comment actor union is invalid")
        access = await _active_guest_access(
            session,
            tenant_id=tenant_id,
            guest_identity_id=actor_guest_identity_id,
            guest_link_id=guest_link_id,
            document_id=document_id,
            generation=generation,
            minimum=SharePermission.VIEW,
            now=now,
            guest_links_enabled=guest_links_enabled,
        )
        if access is None:
            raise CollaborationDocumentNotFound(document_id)
        statement = select(editor_documents).where(
            editor_documents.c.tenant_id == tenant_id,
            editor_documents.c.id == document_id,
            editor_documents.c.deleted_at.is_(None),
        )
    else:
        if actor_user_id is None:
            raise ValueError("shared-comment actor is required")
        statement = visible_resource_select(
            resource_table=editor_documents,
            id_column=editor_documents.c.id,
            owner_column=editor_documents.c.created_by_user_id,
            resource_type="editor_document",
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            restrict_to_workspace_members=restrict_to_workspace_members,
            sharing_enabled=sharing_enabled,
        ).where(editor_documents.c.id == document_id)
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise CollaborationDocumentNotFound(document_id)
    if (
        row.content_mode != "collaboration"
        or row.deleted_at is not None
        or int(row.collaboration_generation) != generation
    ):
        raise CollaborationConflict("generation_conflict")
    owner_user_id = row.created_by_user_id
    if owner_user_id is None:
        raise CollaborationDocumentNotFound(document_id)
    return row, owner_user_id


async def _comment_thread_in_session(
    session: "AsyncSession",
    *,
    tenant_id: str,
    document_id: str,
    generation: int,
    thread_id: uuid.UUID,
) -> CollaborationCommentThread:
    row = (
        await session.execute(
            select(editor_collaboration_comment_threads).where(
                editor_collaboration_comment_threads.c.tenant_id == tenant_id,
                editor_collaboration_comment_threads.c.document_id == document_id,
                editor_collaboration_comment_threads.c.generation == generation,
                editor_collaboration_comment_threads.c.id == thread_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise CollaborationConflict("comment_thread_not_found")
    message_rows = (
        await session.execute(
            select(editor_collaboration_comment_messages)
            .where(
                editor_collaboration_comment_messages.c.tenant_id == tenant_id,
                editor_collaboration_comment_messages.c.thread_id == thread_id,
            )
            .order_by(
                editor_collaboration_comment_messages.c.created_at,
                editor_collaboration_comment_messages.c.id,
            )
        )
    ).all()
    return _comment_thread(
        row,
        tuple(_comment_message(message) for message in message_rows),
    )


async def _validate_comment_mentions(
    session: "AsyncSession",
    *,
    tenant_id: str,
    document_id: str,
    owner_user_id: uuid.UUID,
    mention_user_ids: tuple[uuid.UUID, ...],
    sharing_enabled: bool,
) -> None:
    mentions = set(mention_user_ids)
    if not mentions:
        return
    shared_participants: set[uuid.UUID] = set()
    if sharing_enabled:
        shared_participants = set(
            (
                await session.execute(
                    select(resource_shares.c.recipient_user_id).where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.resource_type == "editor_document",
                        resource_shares.c.resource_id == document_id,
                        resource_shares.c.accepted_at.isnot(None),
                        resource_shares.c.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
    participants = shared_participants | {owner_user_id}
    active_mentions = set(
        (
            await session.execute(
                select(users.c.id).where(
                    users.c.tenant_id == tenant_id,
                    users.c.id.in_(mentions),
                    users.c.disabled_at.is_(None),
                )
            )
        ).scalars()
    )
    if not mentions.issubset(participants & active_mentions):
        raise ValueError("mentions must reference active document participants")


async def _append_comment_effects(
    session: "AsyncSession",
    *,
    tenant_id: str,
    document_id: str,
    actor_user_id: uuid.UUID | None,
    actor_guest_identity_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    action: str,
    mention_user_ids: tuple[uuid.UUID, ...] = (),
) -> None:
    await append_resource_effects(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_type=(
            "guest" if actor_guest_identity_id is not None else "user"
        ),
        detail=(
            {"guest_identity_id": str(actor_guest_identity_id)}
            if actor_guest_identity_id is not None
            else {}
        ),
        owner_user_id=owner_user_id,
        action=action,
        resource_type="editor_document",
        resource_id=document_id,
        scope="collaboration_comment_changed",
        additional_targets=mention_user_ids,
    )
    for mentioned_user_id in sorted(set(mention_user_ids), key=str):
        if actor_user_id is not None and mentioned_user_id == actor_user_id:
            continue
        await append_user_invalidation(
            session,
            tenant_id=tenant_id,
            target_user_id=mentioned_user_id,
            scope="collaboration_comment_mention",
            resource_type="editor_document",
            resource_id=document_id,
        )


async def _append_rejection_audit(
    session: "AsyncSession",
    *,
    update_record: PersistCollaborationUpdate,
    sequence: int,
    rejections: tuple[_RejectedSuggestion, ...],
) -> None:
    """One audit entry per suggestion an edit removed, in its transaction.

    The update row is pruned after the metadata retention period and a
    shrunk patch drops the descriptor, so the entry itself names the
    suggestion, its patch, its author (member or guest identity) and the
    actor. It carries no document content.
    """
    guest_actor = update_record.actor_kind == "guest"
    actor_id = (
        update_record.actor_guest_identity_id
        if guest_actor
        else update_record.actor_user_id
    )
    for rejection in rejections:
        detail: dict[str, object] = {
            "generation": update_record.generation,
            "sequence": sequence,
            "suggestion_id": rejection.suggestion_id,
            "patch_id": rejection.patch_id,
            "author_kind": rejection.author_kind,
            (
                "author_guest_identity_id"
                if rejection.author_kind == "guest"
                else "author_user_id"
            ): str(rejection.author_id),
            "patch_rejected": rejection.patch_rejected,
            "actor_kind": "guest" if guest_actor else "user",
            (
                "actor_guest_identity_id" if guest_actor else "actor_user_id"
            ): str(actor_id),
        }
        if rejection.kind is not None:
            detail["suggestion_kind"] = rejection.kind
        if guest_actor:
            detail["guest_identity_id"] = str(actor_id)
        await append_audit_row(
            session,
            tenant_id=update_record.tenant_id,
            actor_user_id=update_record.actor_user_id,
            actor_type="guest" if guest_actor else "user",
            action=SUGGESTION_REMOVED_BY_EDIT_ACTION,
            resource_type="editor_document",
            resource_id=update_record.document_id,
            detail=detail,
        )


class PostgresEditorCollaborationStore:
    """Durable collaboration state on the shared platform session factory."""

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
        restrict_to_workspace_members: bool,
        sharing_enabled: bool = True,
        guest_links_enabled: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled
        self._guest_links_enabled = guest_links_enabled

    def _session(
        self, tenant_id: str
    ) -> "AbstractAsyncContextManager[AsyncSession]":
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    async def load_state(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int | None = None,
    ) -> CollaborationLoadedState:
        async with self._session(tenant_id) as session:
            document_row = (
                await session.execute(
                    select(editor_documents).where(
                        editor_documents.c.tenant_id == tenant_id,
                        editor_documents.c.id == document_id,
                        editor_documents.c.content_mode == "collaboration",
                        editor_documents.c.deleted_at.is_(None),
                    )
                )
            ).one_or_none()
            if document_row is None:
                raise CollaborationDocumentNotFound(document_id)
            document = _document_state(document_row)
            if generation is not None and generation != document.generation:
                raise CollaborationConflict(
                    "generation_conflict",
                    current_sequence=document.persisted_sequence,
                )
            snapshot_rows = (
                await session.execute(
                    select(editor_collaboration_snapshots)
                    .where(
                        editor_collaboration_snapshots.c.tenant_id == tenant_id,
                        editor_collaboration_snapshots.c.document_id == document_id,
                        editor_collaboration_snapshots.c.generation
                        == document.generation,
                        editor_collaboration_snapshots.c.covered_sequence
                        <= document.persisted_sequence,
                    )
                    .order_by(
                        editor_collaboration_snapshots.c.covered_sequence.desc()
                    )
                    .limit(2)
                )
            ).all()
            if not snapshot_rows:
                log.error(
                    "Collaboration document %s has no verified snapshot.",
                    document_id,
                )
                raise CollaborationConflict("snapshot_missing")
            oldest_covered_sequence = int(snapshot_rows[-1].covered_sequence)
            update_rows = (
                await session.execute(
                    select(editor_collaboration_updates)
                    .where(
                        editor_collaboration_updates.c.tenant_id == tenant_id,
                        editor_collaboration_updates.c.document_id == document_id,
                        editor_collaboration_updates.c.generation
                        == document.generation,
                        editor_collaboration_updates.c.sequence
                        > oldest_covered_sequence,
                        editor_collaboration_updates.c.sequence
                        <= document.persisted_sequence,
                    )
                    .order_by(editor_collaboration_updates.c.sequence)
                )
            ).all()
        all_updates = tuple(_collaboration_update(row) for row in update_rows)
        candidates: list[CollaborationSnapshotCandidate] = []
        for snapshot_row in snapshot_rows:
            snapshot = _snapshot(snapshot_row)
            updates = tuple(
                item
                for item in all_updates
                if item.sequence > snapshot.covered_sequence
            )
            if (
                len(updates)
                != document.persisted_sequence - snapshot.covered_sequence
                or any(
                    item.sequence != snapshot.covered_sequence + offset
                    for offset, item in enumerate(updates, start=1)
                )
                or any(item.update_bytes is None for item in updates)
            ):
                log.error(
                    "Collaboration document %s has an incomplete update tail after sequence %s.",
                    document_id,
                    snapshot.covered_sequence,
                )
                raise CollaborationConflict("update_tail_incomplete")
            candidates.append(
                CollaborationSnapshotCandidate(snapshot=snapshot, updates=updates)
            )
        primary = candidates[0]
        return CollaborationLoadedState(
            document=document,
            snapshot=primary.snapshot,
            updates=primary.updates,
            fallback_candidates=tuple(candidates[1:]),
        )

    async def enable_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
        owner_user_id: uuid.UUID,
        expected_revision: int,
        expected_metadata_revision: int,
        schema_version: int,
        schema_hash: str,
        snapshot: CollaborationSnapshot,
        projection_markdown: str,
        now: float,
    ) -> CollaborationDocumentState:
        if (
            snapshot.document_id != document_id
            or snapshot.tenant_id != tenant_id
            or snapshot.generation != 1
            or snapshot.covered_sequence != 0
            or snapshot.schema_version != schema_version
            or snapshot.schema_hash != schema_hash
        ):
            raise ValueError("initial snapshot does not match the activation contract")
        _require_sha256(schema_hash, field="schema_hash")
        _require_sha256(snapshot.state_hash, field="state_hash")
        _require_sha256(snapshot.projection_hash, field="projection_hash")
        if not hmac.compare_digest(
            hashlib.sha256(snapshot.state_update).hexdigest(),
            snapshot.state_hash,
        ):
            raise ValueError("state_hash does not match state_update")
        if not hmac.compare_digest(
            hashlib.sha256(projection_markdown.encode("utf-8")).hexdigest(),
            snapshot.projection_hash,
        ):
            raise ValueError("projection_hash does not match projection_markdown")
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == tenant_id,
                        editor_documents.c.id == document_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or row.deleted_at is not None:
                raise CollaborationDocumentNotFound(document_id)
            if row.created_by_user_id != owner_user_id:
                raise CollaborationDocumentNotFound(document_id)
            if row.content_mode != "markdown":
                raise CollaborationConflict("mode_conflict")
            if int(row.revision) != expected_revision:
                raise CollaborationConflict("revision_conflict")
            if int(row.metadata_revision) != expected_metadata_revision:
                raise CollaborationConflict("metadata_conflict")
            active_share = (
                await session.scalar(
                    select(resource_shares.c.id)
                    .where(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.resource_type == "editor_document",
                        resource_shares.c.resource_id == document_id,
                        resource_shares.c.revoked_at.is_(None),
                    )
                    .limit(1)
                )
                if self._sharing_enabled
                else None
            )
            if active_share is not None:
                raise CollaborationConflict("share_conflict")
            await session.execute(
                insert(editor_collaboration_snapshots).values(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    generation=1,
                    covered_sequence=0,
                    state_update=snapshot.state_update,
                    state_vector=snapshot.state_vector,
                    state_hash=snapshot.state_hash,
                    projection_hash=snapshot.projection_hash,
                    schema_version=schema_version,
                    schema_hash=schema_hash,
                    created_at=snapshot.created_at,
                )
            )
            stored_row = (
                await session.execute(
                    update(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == tenant_id,
                        editor_documents.c.id == document_id,
                    )
                    .values(
                        content_mode="collaboration",
                        collaboration_generation=1,
                        collaboration_schema_version=schema_version,
                        collaboration_schema_hash=schema_hash,
                        persisted_sequence=0,
                        projection_sequence=0,
                        content_markdown=projection_markdown,
                        projection_updated_at=now,
                        metadata_revision=expected_metadata_revision + 1,
                        updated_at=now,
                    )
                    .returning(editor_documents)
                )
            ).one()
            await append_resource_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=owner_user_id,
                owner_user_id=owner_user_id,
                action="editor_document.collaboration_enabled",
                resource_type="editor_document",
                resource_id=document_id,
                scope="editor_documents",
            )
        return _document_state(stored_row)

    async def issue_lease(
        self,
        lease: CollaborationLease,
        *,
        max_active: int,
        max_issued_per_window: int,
        issued_since: float,
    ) -> CollaborationLease:
        if max_active < 1 or max_issued_per_window < 1:
            raise ValueError("lease limits must be positive")
        minimum = SharePermission(lease.permission)
        rate_actor_id = lease.user_id or lease.guest_identity_id
        if rate_actor_id is None:
            raise ValueError("lease actor is required")
        if (lease.actor_kind == "guest") != (lease.session_id is None):
            raise ValueError("lease session does not match its actor kind")
        async with self._session(lease.tenant_id) as session:
            await _lock_lease_rate_scope(
                session,
                tenant_id=lease.tenant_id,
                user_id=rate_actor_id,
            )
            if lease.actor_kind == "guest":
                if lease.guest_identity_id is None or lease.guest_link_id is None:
                    raise ValueError("guest lease actor is incomplete")
                # Lock order: the document row, then lease rows, the order
                # of revoking the guest link, rotating its password or token
                # and deleting the document. The new lease's foreign key
                # takes FOR KEY SHARE on the document row; that lock is
                # taken here, ahead of the access check and of the sweep of
                # the guest's expired leases, in the same mode. Such a change
                # in flight is waited for, and the access and document checks
                # below see it; one that arrives later waits for this issue
                # and revokes the lease it inserted.
                await session.execute(
                    select(editor_documents.c.id)
                    .where(
                        editor_documents.c.tenant_id == lease.tenant_id,
                        editor_documents.c.id == lease.document_id,
                    )
                    .with_for_update(read=True, key_share=True)
                )
                access = await _active_guest_access(
                    session,
                    tenant_id=lease.tenant_id,
                    guest_identity_id=lease.guest_identity_id,
                    guest_link_id=lease.guest_link_id,
                    document_id=lease.document_id,
                    generation=lease.generation,
                    minimum=minimum,
                    now=lease.issued_at,
                    guest_links_enabled=self._guest_links_enabled,
                )
                session_exists = access
            else:
                if lease.user_id is None:
                    raise ValueError("user lease actor is incomplete")
                access = await lock_resource_access(
                    session,
                    tenant_id=lease.tenant_id,
                    actor_user_id=lease.user_id,
                    resource_type="editor_document",
                    resource_table=editor_documents,
                    id_column=editor_documents.c.id,
                    resource_id=lease.document_id,
                    owner_column=editor_documents.c.created_by_user_id,
                    minimum=minimum,
                    restrict_to_workspace_members=(
                        self._restrict_to_workspace_members
                    ),
                    sharing_enabled=self._sharing_enabled,
                )
                session_exists = await session.scalar(
                    select(auth_sessions.c.id).where(
                        auth_sessions.c.tenant_id == lease.tenant_id,
                        auth_sessions.c.id == lease.session_id,
                        auth_sessions.c.user_id == lease.user_id,
                        auth_sessions.c.expires_at > lease.issued_at,
                    )
                )
            if access is None:
                raise CollaborationDocumentNotFound(lease.document_id)
            document_row = (
                await session.execute(
                    select(editor_documents).where(
                        editor_documents.c.tenant_id == lease.tenant_id,
                        editor_documents.c.id == lease.document_id,
                    )
                )
            ).one()
            if session_exists is None:
                raise CollaborationLeaseInvalid("session_invalid")
            if (
                document_row.content_mode != "collaboration"
                or document_row.deleted_at is not None
                or int(document_row.collaboration_generation) != lease.generation
            ):
                raise CollaborationConflict("generation_conflict")
            active_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(editor_collaboration_leases)
                    .where(
                        editor_collaboration_leases.c.tenant_id
                        == lease.tenant_id,
                        editor_collaboration_leases.c.document_id
                        == lease.document_id,
                        editor_collaboration_leases.c.generation
                        == lease.generation,
                        (
                            editor_collaboration_leases.c.user_id
                            == lease.user_id
                            if lease.actor_kind == "user"
                            else editor_collaboration_leases.c.guest_identity_id
                            == lease.guest_identity_id
                        ),
                        editor_collaboration_leases.c.revoked_at.is_(None),
                        editor_collaboration_leases.c.expires_at
                        > lease.issued_at,
                    )
                )
                or 0
            )
            if active_count >= max_active:
                raise CollaborationRateLimited("session_limit")
            issued_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(editor_collaboration_leases)
                    .where(
                        editor_collaboration_leases.c.tenant_id
                        == lease.tenant_id,
                        (
                            editor_collaboration_leases.c.user_id
                            == lease.user_id
                            if lease.actor_kind == "user"
                            else editor_collaboration_leases.c.guest_identity_id
                            == lease.guest_identity_id
                        ),
                        editor_collaboration_leases.c.issued_at >= issued_since,
                    )
                )
                or 0
            )
            if issued_count >= max_issued_per_window:
                raise CollaborationRateLimited("session_rate_limited")
            await _sweep_expired_actor_leases(session, lease)
            await session.execute(
                insert(editor_collaboration_leases).values(**_lease_values(lease))
            )
        return lease

    async def rotate_lease(
        self,
        *,
        previous_lease_id: uuid.UUID,
        previous_token_hash: str,
        replacement: CollaborationLease,
        max_issued_per_window: int,
        issued_since: float,
    ) -> CollaborationLease:
        if max_issued_per_window < 1:
            raise ValueError("lease limits must be positive")
        if (
            replacement.rotation_command_id is None
            or replacement.rotated_from_lease_id != previous_lease_id
        ):
            raise ValueError("replacement must identify its rotation command")
        rate_actor_id = replacement.user_id or replacement.guest_identity_id
        if rate_actor_id is None:
            raise ValueError("replacement lease actor is required")
        if (replacement.actor_kind == "guest") != (replacement.session_id is None):
            raise ValueError("replacement lease session does not match its actor kind")
        async with self._session(replacement.tenant_id) as session:
            await _lock_lease_rate_scope(
                session,
                tenant_id=replacement.tenant_id,
                user_id=rate_actor_id,
            )
            # Lock order: a person's access check (user row, workspace
            # memberships, document row, share), then the document row, and
            # only then lease rows, the order of an update and of deleting
            # the document or revoking a guest link. A guest's successor
            # checks the document foreign key, which takes FOR KEY SHARE on
            # the document row; that lock is taken here, ahead of the lease
            # rows, in the same mode. Where the access check admitted a
            # person, it already holds the row FOR UPDATE.
            locked_access = (
                await lock_resource_access(
                    session,
                    tenant_id=replacement.tenant_id,
                    actor_user_id=replacement.user_id,
                    resource_type="editor_document",
                    resource_table=editor_documents,
                    id_column=editor_documents.c.id,
                    resource_id=replacement.document_id,
                    owner_column=editor_documents.c.created_by_user_id,
                    minimum=SharePermission(replacement.permission),
                    restrict_to_workspace_members=(
                        self._restrict_to_workspace_members
                    ),
                    sharing_enabled=self._sharing_enabled,
                )
                if replacement.actor_kind != "guest"
                and replacement.user_id is not None
                else None
            )
            await session.execute(
                select(editor_documents.c.id)
                .where(
                    editor_documents.c.tenant_id == replacement.tenant_id,
                    editor_documents.c.id == replacement.document_id,
                )
                .with_for_update(read=True, key_share=True)
            )
            # The previous lease row is locked before the replacement a
            # replayed command already inserted, the order in which an update
            # still signed with the previous lease locks the two rows too
            # (_lock_append_lease). Both hold the document row first, so a
            # replay and such an update never wait for each other's lease.
            previous = (
                await session.execute(
                    select(editor_collaboration_leases)
                    .where(
                        editor_collaboration_leases.c.tenant_id
                        == replacement.tenant_id,
                        editor_collaboration_leases.c.lease_id
                        == previous_lease_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            existing_replacement = (
                await session.execute(
                    select(editor_collaboration_leases)
                    .where(
                        editor_collaboration_leases.c.tenant_id
                        == replacement.tenant_id,
                        editor_collaboration_leases.c.rotation_command_id
                        == replacement.rotation_command_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if previous is None or not hmac.compare_digest(
                str(previous.token_hash), previous_token_hash
            ):
                raise CollaborationLeaseInvalid("lease_invalid")
            if existing_replacement is not None:
                if (
                    existing_replacement.rotated_from_lease_id
                    != previous_lease_id
                    or existing_replacement.document_id
                    != replacement.document_id
                    or int(existing_replacement.generation)
                    != replacement.generation
                    or existing_replacement.user_id != replacement.user_id
                    or existing_replacement.actor_kind
                    != replacement.actor_kind
                    or existing_replacement.guest_identity_id
                    != replacement.guest_identity_id
                    or existing_replacement.guest_link_id
                    != replacement.guest_link_id
                    or existing_replacement.session_id != replacement.session_id
                ):
                    raise CollaborationConflict("rotation_command_conflict")
                if existing_replacement.revoked_at is not None:
                    raise CollaborationLeaseInvalid("lease_revoked")
                if float(existing_replacement.expires_at) <= replacement.issued_at:
                    raise CollaborationLeaseInvalid("lease_expired")
                return _lease(existing_replacement)
            if previous.revoked_at is not None:
                raise CollaborationLeaseInvalid("lease_revoked")
            if float(previous.expires_at) <= replacement.issued_at:
                raise CollaborationLeaseInvalid("lease_expired")
            if (
                previous.document_id != replacement.document_id
                or int(previous.generation) != replacement.generation
                or previous.user_id != replacement.user_id
                or previous.actor_kind != replacement.actor_kind
                or previous.guest_identity_id != replacement.guest_identity_id
                or previous.guest_link_id != replacement.guest_link_id
                or previous.session_id != replacement.session_id
            ):
                raise CollaborationLeaseInvalid("lease_invalid")
            if replacement.actor_kind == "guest":
                if (
                    replacement.guest_identity_id is None
                    or replacement.guest_link_id is None
                ):
                    raise CollaborationLeaseInvalid("lease_invalid")
                access = await _active_guest_access(
                    session,
                    tenant_id=replacement.tenant_id,
                    guest_identity_id=replacement.guest_identity_id,
                    guest_link_id=replacement.guest_link_id,
                    document_id=replacement.document_id,
                    generation=replacement.generation,
                    minimum=SharePermission(replacement.permission),
                    now=replacement.issued_at,
                    guest_links_enabled=self._guest_links_enabled,
                )
                active_session = access
            else:
                if replacement.user_id is None:
                    raise CollaborationLeaseInvalid("lease_invalid")
                access = locked_access
                active_session = await session.scalar(
                    select(auth_sessions.c.id).where(
                        auth_sessions.c.tenant_id == replacement.tenant_id,
                        auth_sessions.c.id == replacement.session_id,
                        auth_sessions.c.user_id == replacement.user_id,
                        auth_sessions.c.expires_at > replacement.issued_at,
                    )
                )
            if access is None or active_session is None:
                raise CollaborationLeaseInvalid("access_revoked")
            issued_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(editor_collaboration_leases)
                    .where(
                        editor_collaboration_leases.c.tenant_id
                        == replacement.tenant_id,
                        (
                            editor_collaboration_leases.c.user_id
                            == replacement.user_id
                            if replacement.actor_kind == "user"
                            else editor_collaboration_leases.c.guest_identity_id
                            == replacement.guest_identity_id
                        ),
                        editor_collaboration_leases.c.issued_at >= issued_since,
                    )
                )
                or 0
            )
            if issued_count >= max_issued_per_window:
                raise CollaborationRateLimited("session_rate_limited")
            await _sweep_expired_actor_leases(session, replacement)
            await session.execute(
                update(editor_collaboration_leases)
                .where(
                    editor_collaboration_leases.c.tenant_id
                    == replacement.tenant_id,
                    editor_collaboration_leases.c.lease_id
                    == previous_lease_id,
                )
                .values(revoked_at=replacement.issued_at)
            )
            await session.execute(
                insert(editor_collaboration_leases).values(
                    **_lease_values(replacement)
                )
            )
        return replacement

    async def introspect_lease(
        self,
        *,
        tenant_id: str,
        lease_id: uuid.UUID,
        token_hash: str,
        now: float,
    ) -> CollaborationLease:
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(editor_collaboration_leases)
                    .where(
                        editor_collaboration_leases.c.tenant_id == tenant_id,
                        editor_collaboration_leases.c.lease_id == lease_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or not hmac.compare_digest(str(row.token_hash), token_hash):
                raise CollaborationLeaseInvalid()
            if row.revoked_at is not None:
                raise CollaborationLeaseInvalid("access_revoked")
            if float(row.expires_at) <= now:
                raise CollaborationLeaseInvalid("lease_expired")
            if row.actor_kind == "guest":
                access = (
                    await _active_guest_access(
                        session,
                        tenant_id=tenant_id,
                        guest_identity_id=row.guest_identity_id,
                        guest_link_id=row.guest_link_id,
                        document_id=str(row.document_id),
                        generation=int(row.generation),
                        minimum=SharePermission(row.permission),
                        now=now,
                        guest_links_enabled=self._guest_links_enabled,
                    )
                    if row.guest_identity_id is not None
                    and row.guest_link_id is not None
                    else None
                )
                active_session = access
            else:
                access_row = (
                    await session.execute(
                        visible_resource_select(
                            resource_table=editor_documents,
                            id_column=editor_documents.c.id,
                            owner_column=editor_documents.c.created_by_user_id,
                            resource_type="editor_document",
                            tenant_id=tenant_id,
                            actor_user_id=row.user_id,
                            restrict_to_workspace_members=(
                                self._restrict_to_workspace_members
                            ),
                            sharing_enabled=self._sharing_enabled,
                        ).where(editor_documents.c.id == row.document_id)
                    )
                ).one_or_none()
                allowed_permissions = {
                    permission.value
                    for permission in share_permissions_satisfying(
                        "editor_document",
                        SharePermission(row.permission),
                    )
                }
                access = (
                    access_row
                    if access_row is not None
                    and (
                        access_row.created_by_user_id == row.user_id
                        or str(
                            access_row._mapping.get(
                                VISIBLE_SHARE_PERMISSION
                            )
                        )
                        in allowed_permissions
                    )
                    else None
                )
                active_session = await session.scalar(
                    select(auth_sessions.c.id).where(
                        auth_sessions.c.tenant_id == tenant_id,
                        auth_sessions.c.id == row.session_id,
                        auth_sessions.c.user_id == row.user_id,
                        auth_sessions.c.expires_at > now,
                    )
                )
            if access is None or active_session is None:
                # The access the lease grants is gone: the lease is revoked
                # for good, so an update, a rotation or an introspection
                # signed with it later is refused even if the access comes
                # back. The revocation commits with this transaction, and the
                # refusal is raised once it has.
                await session.execute(
                    update(editor_collaboration_leases)
                    .where(editor_collaboration_leases.c.lease_id == lease_id)
                    .values(revoked_at=now)
                )
                stored = None
            else:
                document_row = (
                    access
                    if row.actor_kind != "guest"
                    else (
                        await session.execute(
                            select(editor_documents).where(
                                editor_documents.c.tenant_id == tenant_id,
                                editor_documents.c.id == row.document_id,
                            )
                        )
                    ).one()
                )
                if (
                    document_row.content_mode != "collaboration"
                    or document_row.deleted_at is not None
                    or int(document_row.collaboration_generation)
                    != int(row.generation)
                ):
                    raise CollaborationLeaseInvalid("generation_conflict")
                stored = (
                    await session.execute(
                        update(editor_collaboration_leases)
                        .where(editor_collaboration_leases.c.lease_id == lease_id)
                        .values(validated_at=now)
                        .returning(editor_collaboration_leases)
                    )
                ).one()
        if stored is None:
            raise CollaborationLeaseInvalid("access_revoked")
        return _lease(stored)

    async def acquire_instance(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        now: float,
        lease_seconds: float,
    ) -> CollaborationInstanceLease:
        if not instance_id.strip() or lease_seconds <= 0:
            raise ValueError("instance_id and lease_seconds must be valid")
        async with self._session(tenant_id) as session:
            expires_at = now + lease_seconds
            # A live lease of another instance is refused before any lock:
            # writers hand the shared fence on without gaps, so the FOR UPDATE
            # below could wait for as long as they overlap. The decision is
            # the one taken under the lock (same ``now``, same comparison).
            # In between, a renewal or a third instance's takeover can change
            # the row, and both leave the lease foreign and live; only a
            # renewal stamped by an API replica whose clock lags could shorten
            # it, and then the standby simply asks again.
            current = (
                await session.execute(
                    select(
                        editor_collaboration_instances.c.instance_id,
                        editor_collaboration_instances.c.lease_expires_at,
                    ).where(
                        editor_collaboration_instances.c.slot == "primary",
                        editor_collaboration_instances.c.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
            if (
                current is not None
                and current.instance_id != instance_id
                and float(current.lease_expires_at) > now
            ):
                raise CollaborationInstanceFenced("primary instance lease is active")
            await session.execute(
                pg_insert(editor_collaboration_instances)
                .values(
                    slot="primary",
                    tenant_id=tenant_id,
                    instance_id=instance_id,
                    epoch=1,
                    lease_expires_at=expires_at,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        editor_collaboration_instances.c.tenant_id,
                        editor_collaboration_instances.c.slot,
                    ]
                )
            )
            # Load-bearing for the writer fence (see _lock_instance_fence).
            # Writers hold this row FOR KEY SHARE, which conflicts only with
            # FOR UPDATE. This lock makes a takeover wait for every write in
            # flight and marks the replaced row version as key-updated, so no
            # write of the old epoch commits after the takeover. Neither drop
            # it nor weaken it to FOR NO KEY UPDATE (SQLAlchemy's
            # ``key_share=True``), FOR SHARE or FOR KEY SHARE: each lets such
            # a write land. The takeover-ordering contracts of the PostgreSQL
            # collaboration suite pin it.
            row = (
                await session.execute(
                    select(editor_collaboration_instances)
                    .where(
                        editor_collaboration_instances.c.slot == "primary",
                        editor_collaboration_instances.c.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise CollaborationInstanceFenced("primary instance slot unavailable")
            if row.instance_id == instance_id and float(row.lease_expires_at) > now:
                stored = (
                    await session.execute(
                        update(editor_collaboration_instances)
                        .where(
                            editor_collaboration_instances.c.slot == "primary",
                            editor_collaboration_instances.c.tenant_id == tenant_id,
                        )
                        .values(lease_expires_at=expires_at, updated_at=now)
                        .returning(editor_collaboration_instances)
                    )
                ).one()
            elif float(row.lease_expires_at) > now:
                raise CollaborationInstanceFenced("primary instance lease is active")
            else:
                stored = (
                    await session.execute(
                        update(editor_collaboration_instances)
                        .where(
                            editor_collaboration_instances.c.slot == "primary",
                            editor_collaboration_instances.c.tenant_id == tenant_id,
                        )
                        .values(
                            instance_id=instance_id,
                            epoch=int(row.epoch) + 1,
                            lease_expires_at=expires_at,
                            updated_at=now,
                        )
                        .returning(editor_collaboration_instances)
                    )
                ).one()
        return CollaborationInstanceLease(
            instance_id=str(stored.instance_id),
            epoch=int(stored.epoch),
            lease_expires_at=float(stored.lease_expires_at),
            updated_at=float(stored.updated_at),
        )

    async def renew_instance(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        epoch: int,
        now: float,
        lease_seconds: float,
    ) -> CollaborationInstanceLease:
        async with self._session(tenant_id) as session:
            stored = (
                await session.execute(
                    update(editor_collaboration_instances)
                    .where(
                        editor_collaboration_instances.c.slot == "primary",
                        editor_collaboration_instances.c.tenant_id == tenant_id,
                        editor_collaboration_instances.c.instance_id == instance_id,
                        editor_collaboration_instances.c.epoch == epoch,
                        editor_collaboration_instances.c.lease_expires_at > now,
                    )
                    .values(lease_expires_at=now + lease_seconds, updated_at=now)
                    .returning(editor_collaboration_instances)
                )
            ).one_or_none()
        if stored is None:
            raise CollaborationInstanceFenced("instance lease was lost")
        return CollaborationInstanceLease(
            instance_id=str(stored.instance_id),
            epoch=int(stored.epoch),
            lease_expires_at=float(stored.lease_expires_at),
            updated_at=float(stored.updated_at),
        )

    async def validate_instance(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        epoch: int,
        now: float,
    ) -> CollaborationInstanceLease:
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(editor_collaboration_instances).where(
                        editor_collaboration_instances.c.slot == "primary",
                        editor_collaboration_instances.c.tenant_id == tenant_id,
                        editor_collaboration_instances.c.instance_id == instance_id,
                        editor_collaboration_instances.c.epoch == epoch,
                        editor_collaboration_instances.c.lease_expires_at > now,
                    )
                )
            ).one_or_none()
        if row is None:
            raise CollaborationInstanceFenced("instance lease was lost")
        return CollaborationInstanceLease(
            instance_id=str(row.instance_id),
            epoch=int(row.epoch),
            lease_expires_at=float(row.lease_expires_at),
            updated_at=float(row.updated_at),
        )

    async def get_current_instance(
        self,
        *,
        tenant_id: str,
        now: float,
    ) -> CollaborationInstanceLease | None:
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(editor_collaboration_instances).where(
                        editor_collaboration_instances.c.slot == "primary",
                        editor_collaboration_instances.c.tenant_id == tenant_id,
                        editor_collaboration_instances.c.lease_expires_at > now,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return CollaborationInstanceLease(
            instance_id=str(row.instance_id),
            epoch=int(row.epoch),
            lease_expires_at=float(row.lease_expires_at),
            updated_at=float(row.updated_at),
        )

    async def append_update(
        self, update_record: PersistCollaborationUpdate
    ) -> PersistedCollaborationUpdate:
        now = update_record.now or time.time()
        if (
            update_record.actor_kind == "guest"
            and (
                update_record.actor_user_id is not None
                or update_record.actor_guest_identity_id is None
            )
        ) or (
            update_record.actor_kind != "guest"
            and update_record.actor_guest_identity_id is not None
        ):
            raise ValueError("collaboration update actor union is invalid")
        if (
            update_record.actor_kind == "human"
            and update_record.actor_user_id is None
        ):
            raise ValueError("human collaboration update requires a user")
        if not update_record.update_bytes:
            raise ValueError("update_bytes must be non-empty")
        _require_sha256(update_record.update_hash, field="update_hash")
        if not hmac.compare_digest(
            hashlib.sha256(update_record.update_bytes).hexdigest(),
            update_record.update_hash,
        ):
            raise ValueError("update_hash does not match update_bytes")
        suggestion_ids = {item.suggestion_id for item in update_record.suggestions}
        if suggestion_ids != set(update_record.suggestion_ids):
            raise ValueError("suggestion descriptors do not match suggestion_ids")
        rejected_ids = update_record.rejected_suggestion_ids
        if len(set(rejected_ids)) != len(rejected_ids):
            raise ValueError("rejected_suggestion_ids must be unique")
        if rejected_ids:
            # Only an edit removes suggestions this way: a decision decides
            # through its patches, and only a person's edit lease may remove
            # suggestions.
            if (
                update_record.decision is not None
                or update_record.change_kind not in {"direct", "suggestion"}
                or update_record.actor_kind not in {"human", "guest"}
                or update_record.lease_id is None
            ):
                raise ValueError(
                    "only an edit under a lease may remove suggestions"
                )
            if set(rejected_ids) & suggestion_ids:
                raise ValueError(
                    "a removed suggestion cannot be reported as changed"
                )
        patch_ids = {item.patch_id for item in update_record.patches}
        if any(
            item.patch_id not in patch_ids for item in update_record.suggestions
        ):
            raise ValueError("suggestion descriptor references an unknown patch")
        if update_record.decision is None:
            if update_record.change_kind == "decision":
                raise ValueError("decision is required for decision updates")
        elif (
            update_record.change_kind != "decision"
            or update_record.command_id is None
            or not update_record.patches
        ):
            raise ValueError("decision metadata requires a command and patches")
        # A direct edit carries no suggestion descriptors. It names patches
        # only to show the suggestions it removed gone from them.
        if update_record.change_kind not in {"suggestion", "decision"} and (
            update_record.suggestions
            or (update_record.patches and not rejected_ids)
        ):
            raise ValueError("patch metadata is only valid for suggestion updates")
        command_id = update_record.command_id
        command_payload_hash = update_record.command_payload_hash
        if (command_id is None) != (command_payload_hash is None):
            raise ValueError("command_id and command_payload_hash must be paired")
        if command_payload_hash is not None:
            _require_sha256(command_payload_hash, field="command_payload_hash")
        async with self._session(update_record.tenant_id) as session:
            await _lock_instance_fence(
                session,
                tenant_id=update_record.tenant_id,
                instance_id=update_record.instance_id,
                instance_epoch=update_record.instance_epoch,
                now=now,
            )
            # Lock order: a person's access check (user row, workspace
            # memberships, document row, share), then the document row for
            # every actor, and only then lease rows. Deleting the document,
            # revoking a guest link and rotating a lease lock the document
            # before the lease rows too; an update that held its lease while
            # it waited for the document could hold a row such a transaction
            # waits for. Only the locks move ahead; every refusal below keeps
            # its order. The access is locked for the row's own kind and
            # judged further down against the minimum the lease sets.
            requested = (
                SharePermission.SUGGEST
                if update_record.change_kind == "suggestion"
                else SharePermission.EDIT
            )
            locked_access = (
                None
                if update_record.actor_kind == "guest"
                else await lock_resource_access(
                    session,
                    tenant_id=update_record.tenant_id,
                    actor_user_id=update_record.actor_user_id,
                    resource_type="editor_document",
                    resource_table=editor_documents,
                    id_column=editor_documents.c.id,
                    resource_id=update_record.document_id,
                    owner_column=editor_documents.c.created_by_user_id,
                    minimum=requested,
                    restrict_to_workspace_members=(
                        self._restrict_to_workspace_members
                    ),
                    sharing_enabled=self._sharing_enabled,
                )
            )
            document = (
                await session.execute(
                    select(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == update_record.tenant_id,
                        editor_documents.c.id == update_record.document_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            lease_row = None
            if update_record.lease_id is not None:
                lease_row = await _lock_append_lease(
                    session,
                    update_record=update_record,
                    now=now,
                )
                if (
                    (
                        update_record.change_kind == "suggestion"
                        and lease_row.permission not in {"suggest", "edit"}
                    )
                    or (
                        update_record.change_kind != "suggestion"
                        and lease_row.permission != "edit"
                    )
                    or (rejected_ids and lease_row.permission != "edit")
                ):
                    raise CollaborationLeaseInvalid("permission_denied")
            # The current access an update needs is the access it was judged
            # with, checked below as well as the lease: a lease outlives a
            # permission downgrade by up to its lifetime. The service judges
            # an edit lease's update with edit rights, so whatever kind its
            # row is reported as, it may shorten, format or remove another
            # person's pending suggestion. Only a suggest lease's row, or a
            # suggestion published without a lease, needs no more than
            # suggest access.
            minimum = (
                SharePermission.SUGGEST
                if update_record.change_kind == "suggestion"
                and (lease_row is None or lease_row.permission == "suggest")
                else SharePermission.EDIT
            )
            if lease_row is not None:
                if lease_row.actor_kind == "guest":
                    active_session = (
                        await _active_guest_access(
                            session,
                            tenant_id=update_record.tenant_id,
                            guest_identity_id=lease_row.guest_identity_id,
                            guest_link_id=lease_row.guest_link_id,
                            document_id=update_record.document_id,
                            generation=update_record.generation,
                            minimum=minimum,
                            now=now,
                            guest_links_enabled=self._guest_links_enabled,
                        )
                        if lease_row.guest_identity_id is not None
                        and lease_row.guest_link_id is not None
                        else None
                    )
                else:
                    active_session = await session.scalar(
                        select(auth_sessions.c.id).where(
                            auth_sessions.c.tenant_id == update_record.tenant_id,
                            auth_sessions.c.id == lease_row.session_id,
                            auth_sessions.c.user_id == lease_row.user_id,
                            auth_sessions.c.expires_at > now,
                        )
                    )
                if active_session is None:
                    raise CollaborationLeaseInvalid("session_invalid")
            elif (
                command_id is None
                or update_record.change_kind not in {"decision", "suggestion"}
            ):
                raise CollaborationLeaseInvalid("lease_required")
            if update_record.actor_kind == "guest":
                access = (
                    await _active_guest_access(
                        session,
                        tenant_id=update_record.tenant_id,
                        guest_identity_id=cast(
                            uuid.UUID,
                            update_record.actor_guest_identity_id,
                        ),
                        guest_link_id=lease_row.guest_link_id,
                        document_id=update_record.document_id,
                        generation=update_record.generation,
                        minimum=minimum,
                        now=now,
                        guest_links_enabled=self._guest_links_enabled,
                    )
                    if lease_row is not None
                    and lease_row.guest_link_id is not None
                    else None
                )
            else:
                # ``minimum`` is never weaker than ``requested``: only an edit
                # lease's suggestion row needs more, and the locked share
                # grant decides it.
                access = (
                    locked_access
                    if locked_access is not None
                    and locked_access.admits("editor_document", minimum)
                    else None
                )
            if access is None:
                raise CollaborationLeaseInvalid("access_revoked")
            if document is None or document.deleted_at is not None:
                raise CollaborationDocumentNotFound(update_record.document_id)
            if (
                document.content_mode != "collaboration"
                or int(document.collaboration_generation)
                != update_record.generation
            ):
                raise CollaborationConflict(
                    "generation_conflict",
                    current_sequence=int(document.persisted_sequence),
                )
            current_sequence = int(document.persisted_sequence)
            duplicate = (
                await session.execute(
                    select(
                        editor_collaboration_updates.c.sequence,
                        editor_collaboration_updates.c.command_id,
                        editor_collaboration_updates.c.command_payload_hash,
                    ).where(
                        editor_collaboration_updates.c.tenant_id
                        == update_record.tenant_id,
                        editor_collaboration_updates.c.document_id
                        == update_record.document_id,
                        editor_collaboration_updates.c.generation
                        == update_record.generation,
                        editor_collaboration_updates.c.update_hash
                        == update_record.update_hash,
                    )
                )
            ).one_or_none()
            if duplicate is not None:
                if (
                    duplicate.command_id != command_id
                    or duplicate.command_payload_hash != command_payload_hash
                ):
                    raise CollaborationConflict("command_conflict")
                return PersistedCollaborationUpdate(
                    sequence=int(duplicate.sequence),
                    persisted_sequence=current_sequence,
                    duplicate=True,
                    persisted_at=now,
                )
            if command_id is not None:
                await _lock_command_scope(
                    session,
                    tenant_id=update_record.tenant_id,
                    command_id=command_id,
                )
                command_row = (
                    await session.execute(
                        select(
                            editor_collaboration_updates.c.document_id,
                            editor_collaboration_updates.c.generation,
                            editor_collaboration_updates.c.sequence,
                            editor_collaboration_updates.c.update_hash,
                            editor_collaboration_updates.c.command_payload_hash,
                        ).where(
                            editor_collaboration_updates.c.command_id == command_id
                        )
                    )
                ).one_or_none()
                if command_row is not None:
                    if (
                        command_row.document_id == update_record.document_id
                        and int(command_row.generation) == update_record.generation
                        and command_row.update_hash == update_record.update_hash
                        and command_row.command_payload_hash
                        == command_payload_hash
                    ):
                        return PersistedCollaborationUpdate(
                            sequence=int(command_row.sequence),
                            persisted_sequence=current_sequence,
                            duplicate=True,
                            persisted_at=now,
                        )
                    raise CollaborationConflict("command_conflict")
            if (
                update_record.expected_sequence is not None
                and update_record.expected_sequence != current_sequence
            ):
                raise CollaborationConflict(
                    "sequence_conflict", current_sequence=current_sequence
                )
            next_sequence = current_sequence + 1
            rejections = await self._persist_patch_states(
                session,
                update_record=update_record,
                document=document,
                next_sequence=next_sequence,
                now=now,
            )
            await session.execute(
                insert(editor_collaboration_updates).values(
                    document_id=update_record.document_id,
                    tenant_id=update_record.tenant_id,
                    generation=update_record.generation,
                    sequence=next_sequence,
                    update_hash=update_record.update_hash,
                    update_bytes=update_record.update_bytes,
                    actor_user_id=update_record.actor_user_id,
                    actor_guest_identity_id=(
                        update_record.actor_guest_identity_id
                    ),
                    actor_kind=update_record.actor_kind,
                    change_kind=update_record.change_kind,
                    suggestion_ids=list(update_record.suggestion_ids),
                    change_summary=update_record.change_summary,
                    decision_outcome=update_record.decision_outcome,
                    command_id=command_id,
                    command_payload_hash=command_payload_hash,
                    created_at=now,
                    payload_pruned_at=None,
                    rejected_suggestion_ids=list(rejected_ids),
                )
            )
            await _append_rejection_audit(
                session,
                update_record=update_record,
                sequence=next_sequence,
                rejections=rejections,
            )
            await session.execute(
                update(editor_documents)
                .where(
                    editor_documents.c.tenant_id == update_record.tenant_id,
                    editor_documents.c.id == update_record.document_id,
                )
                .values(persisted_sequence=next_sequence, updated_at=now)
            )
        return PersistedCollaborationUpdate(
            sequence=next_sequence,
            persisted_sequence=next_sequence,
            duplicate=False,
            persisted_at=now,
        )

    async def _persist_patch_states(
        self,
        session: "AsyncSession",
        *,
        update_record: PersistCollaborationUpdate,
        document: Any,
        next_sequence: int,
        now: float,
    ) -> tuple[_RejectedSuggestion, ...]:
        """Persist patch membership, decisions and removals by edit.

        Runs in the update transaction. Returns the suggestions the update
        removed, each resolved from its stored patch, for the audit trail.
        """
        rejected_ids = set(update_record.rejected_suggestion_ids)
        if not update_record.patches:
            if rejected_ids:
                raise CollaborationConflict("rejected_suggestion_unknown")
            return ()
        actor_id = (
            update_record.actor_guest_identity_id
            if update_record.actor_kind == "guest"
            else update_record.actor_user_id
        )
        if actor_id is None:
            raise CollaborationConflict("patch_actor_missing")
        actor_author_kind = "guest" if update_record.actor_kind == "guest" else "user"
        suggestions_by_patch: dict[str, dict[str, dict[str, Any]]] = {}
        for suggestion in update_record.suggestions:
            suggestions_by_patch.setdefault(suggestion.patch_id, {})[
                suggestion.suggestion_id
            ] = {
                "suggestion_id": suggestion.suggestion_id,
                "patch_id": suggestion.patch_id,
                "author_id": str(suggestion.author_id),
                "created_at": suggestion.created_at,
                "kind": suggestion.kind,
            }
        rejections: list[_RejectedSuggestion] = []
        for patch_state in update_record.patches:
            if (
                update_record.decision is None
                and not patch_state.active_suggestion_ids
                and not rejected_ids
            ):
                raise CollaborationConflict("patch_suggestions_empty")
            row = (
                await session.execute(
                    select(editor_patches)
                    .where(
                        editor_patches.c.tenant_id == update_record.tenant_id,
                        editor_patches.c.patch_id == patch_state.patch_id,
                        editor_patches.c.document_id == update_record.document_id,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if row is None:
                private_assistant_publish = (
                    update_record.actor_kind == "assistant"
                    and update_record.actor_user_id is not None
                    and update_record.command_id is not None
                    and await _has_creator_private_suggestion_draft(
                        session,
                        tenant_id=update_record.tenant_id,
                        document_id=update_record.document_id,
                        creator_user_id=update_record.actor_user_id,
                        patch_id=patch_state.patch_id,
                        publication_command_id=update_record.command_id,
                    )
                )
                if (
                    (
                        update_record.actor_kind not in {"human", "guest"}
                        and not private_assistant_publish
                    )
                    or update_record.change_kind != "suggestion"
                    or patch_state.author_id != actor_id
                    or not patch_state.active_suggestion_ids
                ):
                    raise CollaborationConflict("patch_not_found")
                descriptors = suggestions_by_patch.get(patch_state.patch_id, {})
                if set(descriptors) != set(patch_state.active_suggestion_ids):
                    raise CollaborationConflict("patch_metadata_conflict")
                await session.execute(
                    insert(editor_patches).values(
                        patch_id=patch_state.patch_id,
                        tenant_id=update_record.tenant_id,
                        document_id=update_record.document_id,
                        run_id=None,
                        source="human",
                        status="pending",
                        edits=list(descriptors.values()),
                        summary="",
                        warnings=[],
                        revision_before=int(document.revision),
                        collaboration_generation=update_record.generation,
                        base_sequence=next_sequence - 1,
                        decision_sequence=None,
                        suggestion_ids=list(patch_state.active_suggestion_ids),
                        applied_revision=None,
                        applied_edit_ids=None,
                        note="",
                        created_by_user_id=(
                            patch_state.author_id
                            if update_record.actor_kind in {"human", "assistant"}
                            else None
                        ),
                        created_by_guest_identity_id=(
                            patch_state.author_id
                            if update_record.actor_kind == "guest"
                            else None
                        ),
                        decided_by_user_id=None,
                        decided_by_guest_identity_id=None,
                        command_id=update_record.command_id,
                        created_at=patch_state.created_at,
                        decided_at=None,
                    )
                )
                await self._clear_published_private_draft(
                    session,
                    update_record=update_record,
                    patch_id=patch_state.patch_id,
                    actor_user_id=update_record.actor_user_id,
                )
                continue
            author_kind, author_id = _patch_author(row)
            if (
                row["collaboration_generation"] != update_record.generation
                or author_id is None
                or author_id != patch_state.author_id
                or row["status"] != "pending"
            ):
                raise CollaborationConflict("patch_metadata_conflict")
            if update_record.decision is not None:
                if patch_state.active_suggestion_ids:
                    raise CollaborationConflict("patch_decision_incomplete")
                prior_ids = [str(value) for value in (row["suggestion_ids"] or [])]
                await session.execute(
                    update(editor_patches)
                    .where(
                        editor_patches.c.tenant_id == update_record.tenant_id,
                        editor_patches.c.patch_id == patch_state.patch_id,
                        editor_patches.c.status == "pending",
                    )
                    .values(
                        status=(
                            "accepted"
                            if update_record.decision == "accept"
                            else "rejected"
                        ),
                        decision_sequence=next_sequence,
                        applied_edit_ids=(
                            prior_ids
                            if update_record.decision == "accept"
                            else None
                        ),
                        decided_by_user_id=update_record.actor_user_id,
                        decided_by_guest_identity_id=(
                            update_record.actor_guest_identity_id
                        ),
                        command_id=update_record.command_id,
                        decided_at=now,
                    )
                )
                continue
            active_ids = set(patch_state.active_suggestion_ids)
            prior_active_ids = {
                str(item) for item in (row["suggestion_ids"] or [])
            }
            new_descriptors = suggestions_by_patch.get(patch_state.patch_id, {})
            removed_ids = prior_active_ids - active_ids
            rejected_here = removed_ids & rejected_ids
            if active_ids & rejected_ids:
                raise CollaborationConflict("rejected_suggestion_unknown")
            own_patch = author_kind == actor_author_kind and author_id == actor_id
            stored_descriptors = (
                {
                    str(item.get("suggestion_id")): dict(item)
                    for item in (row["edits"] or [])
                    if isinstance(item, dict) and item.get("suggestion_id")
                }
                if row["source"] == "human"
                else {}
            )
            if not own_patch:
                # Another person's patch: an update may only take away the
                # suggestions it removed, never add, describe or supersede.
                if (
                    not rejected_here
                    or removed_ids != rejected_here
                    or not active_ids.issubset(prior_active_ids)
                    or new_descriptors
                    or patch_state.superseded_suggestion_ids
                ):
                    raise CollaborationConflict("patch_author_conflict")
            else:
                if any(
                    descriptor["author_id"] != str(author_id)
                    for descriptor in new_descriptors.values()
                ):
                    raise CollaborationConflict("patch_author_conflict")
                unaccounted = removed_ids - rejected_here
                if unaccounted:
                    if not _is_valid_slash_structure_supersession(
                        patch_state=patch_state,
                        prior_active_ids=prior_active_ids,
                        active_ids=active_ids,
                        stored_descriptors=stored_descriptors,
                        incoming_descriptors=new_descriptors,
                        actor_user_id=actor_id,
                        created_by_user_id=author_id,
                        source=row["source"],
                        change_kind=update_record.change_kind,
                    ):
                        raise CollaborationConflict("patch_membership_shrink")
                elif patch_state.superseded_suggestion_ids:
                    raise CollaborationConflict("patch_supersession_invalid")
                if update_record.change_kind != "suggestion" and (
                    new_descriptors or not active_ids.issubset(prior_active_ids)
                ):
                    raise CollaborationConflict("patch_metadata_conflict")
            known_ids = prior_active_ids | set(new_descriptors)
            if not active_ids.issubset(known_ids):
                raise CollaborationConflict("patch_metadata_conflict")
            if not active_ids:
                # Every suggestion of the patch was removed by this edit:
                # the patch is rejected, by the update's actor, at its
                # sequence. Its membership and descriptors stay as the
                # record of what was removed.
                if not prior_active_ids or removed_ids != rejected_here:
                    raise CollaborationConflict("patch_suggestions_empty")
                await session.execute(
                    update(editor_patches)
                    .where(
                        editor_patches.c.tenant_id == update_record.tenant_id,
                        editor_patches.c.patch_id == patch_state.patch_id,
                        editor_patches.c.status == "pending",
                    )
                    .values(
                        status="rejected",
                        decision_sequence=next_sequence,
                        applied_edit_ids=None,
                        decided_by_user_id=update_record.actor_user_id,
                        decided_by_guest_identity_id=(
                            update_record.actor_guest_identity_id
                        ),
                        decided_at=now,
                    )
                )
            else:
                values: dict[str, Any] = {
                    "suggestion_ids": list(patch_state.active_suggestion_ids),
                    "command_id": (
                        update_record.command_id
                        if update_record.command_id is not None
                        else row["command_id"]
                    ),
                }
                if row["source"] == "human":
                    descriptors = dict(stored_descriptors)
                    descriptors.update(new_descriptors)
                    if not active_ids.issubset(descriptors):
                        raise CollaborationConflict("patch_metadata_conflict")
                    values["edits"] = [
                        descriptors[suggestion_id]
                        for suggestion_id in patch_state.active_suggestion_ids
                    ]
                await session.execute(
                    update(editor_patches)
                    .where(
                        editor_patches.c.tenant_id == update_record.tenant_id,
                        editor_patches.c.patch_id == patch_state.patch_id,
                        editor_patches.c.status == "pending",
                    )
                    .values(**values)
                )
                await self._clear_published_private_draft(
                    session,
                    update_record=update_record,
                    patch_id=patch_state.patch_id,
                    actor_user_id=update_record.actor_user_id,
                )
            for suggestion_id in sorted(rejected_here):
                descriptor = stored_descriptors.get(suggestion_id) or {}
                rejections.append(
                    _RejectedSuggestion(
                        suggestion_id=suggestion_id,
                        patch_id=patch_state.patch_id,
                        author_kind=author_kind,
                        author_id=author_id,
                        kind=(
                            str(descriptor["kind"])
                            if descriptor.get("kind") is not None
                            else None
                        ),
                        patch_rejected=not active_ids,
                    )
                )
        # Every suggestion the update names as removed was pending in a
        # patch of this document and generation, and is gone from it now.
        if {item.suggestion_id for item in rejections} != rejected_ids:
            raise CollaborationConflict("rejected_suggestion_unknown")
        return tuple(rejections)

    @staticmethod
    async def _clear_published_private_draft(
        session: "AsyncSession",
        *,
        update_record: PersistCollaborationUpdate,
        patch_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        """Clear the creator-private draft in the shared-publish transaction."""
        if (
            update_record.decision is not None
            or update_record.change_kind != "suggestion"
            or actor_user_id is None
        ):
            return
        await session.execute(
            update(editor_comments)
            .where(
                editor_comments.c.tenant_id == update_record.tenant_id,
                editor_comments.c.document_id == update_record.document_id,
                editor_comments.c.created_by_user_id == actor_user_id,
                editor_comments.c.suggestion_draft["patch_id"].astext
                == patch_id,
            )
            .values(suggestion_draft=null())
        )

    async def lookup_command(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
    ) -> CollaborationPersistedCommand | None:
        _require_sha256(command_payload_hash, field="command_payload_hash")
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(editor_collaboration_updates).where(
                        editor_collaboration_updates.c.command_id == command_id
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            if (
                row.document_id != document_id
                or int(row.generation) != generation
                or row.command_payload_hash != command_payload_hash
                or row.change_kind not in {"decision", "suggestion"}
                or row.actor_user_id is None
            ):
                raise CollaborationConflict("command_conflict")
            patch_rows = (
                await session.execute(
                    select(
                        editor_patches.c.patch_id,
                        editor_patches.c.status,
                    ).where(
                        editor_patches.c.tenant_id == tenant_id,
                        editor_patches.c.document_id == document_id,
                        editor_patches.c.collaboration_generation == generation,
                        editor_patches.c.command_id == command_id,
                    )
                )
            ).all()
        return _persisted_command(row, patch_rows)

    async def lookup_updates_by_hashes(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        update_hashes: tuple[str, ...],
        instance_id: str,
        instance_epoch: int,
        now: float,
    ) -> tuple[CollaborationUpdateLookup, ...]:
        """Resolve update hashes after validating the scoped generation."""
        if (
            len(update_hashes) > 1000
            or len(set(update_hashes)) != len(update_hashes)
        ):
            raise ValueError("update_hashes must contain at most 1000 unique digests")
        for update_hash in update_hashes:
            _require_sha256(update_hash, field="update_hash")
        async with self._session(tenant_id) as session:
            await _lock_instance_fence(
                session,
                tenant_id=tenant_id,
                instance_id=instance_id,
                instance_epoch=instance_epoch,
                now=now,
            )
            # FOR SHARE waits for a persist of this document that holds the
            # row, so its outcome is found here instead of "not found" a
            # moment before it commits. The shared fence does not order them.
            document = (
                await session.execute(
                    select(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == tenant_id,
                        editor_documents.c.id == document_id,
                        editor_documents.c.content_mode == "collaboration",
                        editor_documents.c.deleted_at.is_(None),
                    )
                    .with_for_update(read=True)
                )
            ).one_or_none()
            if document is None:
                raise CollaborationDocumentNotFound(document_id)
            if int(document.collaboration_generation) != generation:
                raise CollaborationConflict(
                    "generation_conflict",
                    current_sequence=int(document.persisted_sequence),
                )
            if not update_hashes:
                return ()
            rows = (
                await session.execute(
                    select(
                        editor_collaboration_updates.c.update_hash,
                        editor_collaboration_updates.c.sequence,
                    ).where(
                        editor_collaboration_updates.c.tenant_id == tenant_id,
                        editor_collaboration_updates.c.document_id == document_id,
                        editor_collaboration_updates.c.generation == generation,
                        editor_collaboration_updates.c.update_hash.in_(update_hashes),
                    )
                )
            ).all()
        sequence_by_hash = {
            str(row.update_hash): int(row.sequence) for row in rows
        }
        return tuple(
            CollaborationUpdateLookup(
                update_hash=update_hash,
                sequence=sequence_by_hash[update_hash],
            )
            for update_hash in update_hashes
            if update_hash in sequence_by_hash
        )

    async def store_snapshot(
        self,
        snapshot: CollaborationSnapshot,
        *,
        projection_markdown: str,
        instance_id: str,
        instance_epoch: int,
        now: float,
    ) -> None:
        _require_sha256(snapshot.state_hash, field="state_hash")
        _require_sha256(snapshot.projection_hash, field="projection_hash")
        _require_sha256(snapshot.schema_hash, field="schema_hash")
        if not hmac.compare_digest(
            hashlib.sha256(snapshot.state_update).hexdigest(),
            snapshot.state_hash,
        ):
            raise ValueError("state_hash does not match state_update")
        if not hmac.compare_digest(
            hashlib.sha256(projection_markdown.encode("utf-8")).hexdigest(),
            snapshot.projection_hash,
        ):
            raise ValueError("projection_hash does not match projection_markdown")
        async with self._session(snapshot.tenant_id) as session:
            await _lock_instance_fence(
                session,
                tenant_id=snapshot.tenant_id,
                instance_id=instance_id,
                instance_epoch=instance_epoch,
                now=now,
            )
            document = (
                await session.execute(
                    select(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == snapshot.tenant_id,
                        editor_documents.c.id == snapshot.document_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if document is None or document.deleted_at is not None:
                raise CollaborationDocumentNotFound(snapshot.document_id)
            if (
                document.content_mode != "collaboration"
                or int(document.collaboration_generation) != snapshot.generation
                or int(document.collaboration_schema_version)
                != snapshot.schema_version
                or document.collaboration_schema_hash != snapshot.schema_hash
            ):
                raise CollaborationConflict("schema_conflict")
            if snapshot.covered_sequence > int(document.persisted_sequence):
                raise CollaborationConflict(
                    "snapshot_ahead",
                    current_sequence=int(document.persisted_sequence),
                )
            statement = pg_insert(editor_collaboration_snapshots).values(
                document_id=snapshot.document_id,
                tenant_id=snapshot.tenant_id,
                generation=snapshot.generation,
                covered_sequence=snapshot.covered_sequence,
                state_update=snapshot.state_update,
                state_vector=snapshot.state_vector,
                state_hash=snapshot.state_hash,
                projection_hash=snapshot.projection_hash,
                schema_version=snapshot.schema_version,
                schema_hash=snapshot.schema_hash,
                created_at=snapshot.created_at,
            )
            statement = statement.on_conflict_do_nothing(
                index_elements=[
                    editor_collaboration_snapshots.c.document_id,
                    editor_collaboration_snapshots.c.generation,
                    editor_collaboration_snapshots.c.covered_sequence,
                ]
            )
            result = await session.execute(statement)
            if not result.rowcount:
                existing = (
                    await session.execute(
                        select(editor_collaboration_snapshots).where(
                            editor_collaboration_snapshots.c.document_id
                            == snapshot.document_id,
                            editor_collaboration_snapshots.c.generation
                            == snapshot.generation,
                            editor_collaboration_snapshots.c.covered_sequence
                            == snapshot.covered_sequence,
                        )
                    )
                ).one()
                if (
                    existing.state_hash != snapshot.state_hash
                    or existing.projection_hash != snapshot.projection_hash
                    or existing.schema_hash != snapshot.schema_hash
                ):
                    raise CollaborationConflict("snapshot_conflict")
            if snapshot.covered_sequence >= int(document.projection_sequence):
                await session.execute(
                    update(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == snapshot.tenant_id,
                        editor_documents.c.id == snapshot.document_id,
                        editor_documents.c.collaboration_generation
                        == snapshot.generation,
                        editor_documents.c.projection_sequence
                        <= snapshot.covered_sequence,
                    )
                    .values(
                        content_markdown=projection_markdown,
                        projection_sequence=snapshot.covered_sequence,
                        projection_updated_at=snapshot.created_at,
                        updated_at=snapshot.created_at,
                    )
                )

    async def update_projection(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        covered_sequence: int,
        content_markdown: str,
        projection_hash: str,
        now: float,
    ) -> CollaborationDocumentState:
        _require_sha256(projection_hash, field="projection_hash")
        if not hmac.compare_digest(
            hashlib.sha256(content_markdown.encode("utf-8")).hexdigest(),
            projection_hash,
        ):
            raise ValueError("projection_hash does not match content_markdown")
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == tenant_id,
                        editor_documents.c.id == document_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or row.deleted_at is not None:
                raise CollaborationDocumentNotFound(document_id)
            if (
                row.content_mode != "collaboration"
                or int(row.collaboration_generation) != generation
            ):
                raise CollaborationConflict("generation_conflict")
            if covered_sequence > int(row.persisted_sequence):
                raise CollaborationConflict(
                    "projection_ahead",
                    current_sequence=int(row.persisted_sequence),
                )
            if covered_sequence < int(row.projection_sequence):
                return _document_state(row)
            stored = (
                await session.execute(
                    update(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == tenant_id,
                        editor_documents.c.id == document_id,
                    )
                    .values(
                        content_markdown=content_markdown,
                        projection_sequence=covered_sequence,
                        projection_updated_at=now,
                        updated_at=now,
                    )
                    .returning(editor_documents)
                )
            ).one()
        return _document_state(stored)

    async def list_activity(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        before_sequence: int | None,
        author_user_id: uuid.UUID | None,
        change_kind: CollaborationChangeKind | None,
        limit: int,
    ) -> tuple[CollaborationActivity, ...]:
        statement = select(
            editor_collaboration_updates.c.sequence,
            editor_collaboration_updates.c.actor_user_id,
            editor_collaboration_updates.c.actor_guest_identity_id,
            editor_collaboration_updates.c.actor_kind,
            editor_collaboration_updates.c.change_kind,
            editor_collaboration_updates.c.suggestion_ids,
            editor_collaboration_updates.c.change_summary,
            editor_collaboration_updates.c.decision_outcome,
            editor_collaboration_updates.c.command_id,
            editor_collaboration_updates.c.created_at,
            editor_collaboration_updates.c.rejected_suggestion_ids,
        ).where(
            editor_collaboration_updates.c.tenant_id == tenant_id,
            editor_collaboration_updates.c.document_id == document_id,
            editor_collaboration_updates.c.generation == generation,
            # A row that changed nothing a reader sees (a formatting cleanup
            # sent after a reconnect) stays stored for replay, but is no
            # entry of the history. A summary without the claim is listed.
            ~editor_collaboration_updates.c.change_summary.contains(
                {"changes_document": False}
            ),
        )
        if before_sequence is not None:
            statement = statement.where(
                editor_collaboration_updates.c.sequence < before_sequence
            )
        if author_user_id is not None:
            statement = statement.where(
                editor_collaboration_updates.c.actor_user_id == author_user_id
            )
        if change_kind is not None:
            statement = statement.where(
                editor_collaboration_updates.c.change_kind == change_kind
            )
        statement = statement.order_by(
            editor_collaboration_updates.c.sequence.desc()
        ).limit(max(1, min(limit, 201)))
        async with self._session(tenant_id) as session:
            rows = (await session.execute(statement)).all()
        return tuple(
            CollaborationActivity(
                sequence=int(row.sequence),
                actor_user_id=row.actor_user_id,
                actor_guest_identity_id=row.actor_guest_identity_id,
                actor_kind=row.actor_kind,
                change_kind=row.change_kind,
                suggestion_ids=tuple(
                    str(item) for item in (row.suggestion_ids or [])
                ),
                change_summary=dict(row.change_summary or {}),
                decision_outcome=row.decision_outcome,
                command_id=row.command_id,
                created_at=float(row.created_at),
                rejected_suggestion_ids=tuple(
                    str(item) for item in (row.rejected_suggestion_ids or [])
                ),
            )
            for row in rows
        )

    async def list_comment_activity(
        self,
        *,
        tenant_id: str,
        document_id: str,
        before_id: int | None,
        author_user_id: uuid.UUID | None,
        limit: int,
    ) -> tuple[CollaborationCommentActivity, ...]:
        statement = select(
            audit_log.c.id,
            audit_log.c.actor_user_id,
            audit_log.c.action,
            audit_log.c.occurred_at,
        ).where(
            audit_log.c.tenant_id == tenant_id,
            audit_log.c.resource_type == "editor_document",
            audit_log.c.resource_id == document_id,
            audit_log.c.action.like("editor.collaboration_comment.%"),
        )
        if before_id is not None:
            statement = statement.where(audit_log.c.id < before_id)
        if author_user_id is not None:
            statement = statement.where(
                audit_log.c.actor_user_id == author_user_id
            )
        statement = statement.order_by(audit_log.c.id.desc()).limit(
            max(1, min(limit, 201))
        )
        async with self._session(tenant_id) as session:
            rows = (await session.execute(statement)).all()
        return tuple(
            CollaborationCommentActivity(
                id=int(row.id),
                actor_user_id=row.actor_user_id,
                action=str(row.action),
                created_at=float(row.occurred_at.timestamp()),
            )
            for row in rows
        )

    async def list_suggestion_rejections(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        sequences: tuple[int, ...],
        limit: int,
    ) -> tuple[CollaborationSuggestionRejection, ...]:
        """Read the suggestions the given update rows removed from their
        audit entries."""
        if not sequences:
            return ()
        detail = audit_log.c.detail
        statement = select(audit_log.c.detail).where(
            audit_log.c.tenant_id == tenant_id,
            audit_log.c.action == SUGGESTION_REMOVED_BY_EDIT_ACTION,
            audit_log.c.resource_type == "editor_document",
            audit_log.c.resource_id == document_id,
            detail["generation"].astext == str(int(generation)),
            detail["sequence"].astext.in_(
                sorted({str(int(value)) for value in sequences})
            ),
        )
        statement = statement.order_by(audit_log.c.id.desc()).limit(
            max(1, min(limit, 1000))
        )
        async with self._session(tenant_id) as session:
            rows = (await session.execute(statement)).all()

        def identity(value: object) -> uuid.UUID | None:
            return uuid.UUID(str(value)) if value else None

        rejections: list[CollaborationSuggestionRejection] = []
        for row in rows:
            item = dict(row.detail or {})
            rejections.append(
                CollaborationSuggestionRejection(
                    sequence=int(item["sequence"]),
                    suggestion_id=str(item["suggestion_id"]),
                    patch_id=str(item["patch_id"]),
                    author_user_id=identity(item.get("author_user_id")),
                    author_guest_identity_id=identity(
                        item.get("author_guest_identity_id")
                    ),
                )
            )
        return tuple(rejections)

    async def list_open_patches(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        before: tuple[float, str] | None,
        author_user_id: uuid.UUID | None,
        suggestion_kind: CollaborationSuggestionKind | None,
        limit: int,
    ) -> CollaborationOpenPatchPage:
        bounded_limit = max(1, min(limit, 200))
        statement = select(editor_patches).where(
            editor_patches.c.tenant_id == tenant_id,
            editor_patches.c.document_id == document_id,
            editor_patches.c.collaboration_generation == generation,
            editor_patches.c.status == "pending",
            func.jsonb_array_length(editor_patches.c.suggestion_ids) > 0,
        )
        if before is not None:
            # The cursor carries the raw time of the page's last row; both
            # sides compare in the one unit the page is ordered by.
            statement = statement.where(
                tuple_(editor_patch_created_at_ms, editor_patches.c.patch_id)
                < tuple_(patch_created_at_ms(before[0]), before[1])
            )
        if author_user_id is not None:
            statement = statement.where(
                editor_patches.c.created_by_user_id == author_user_id
            )
        if suggestion_kind is not None:
            statement = statement.where(
                text(
                    """
                    EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                            CAST(editor_patches.edits AS jsonb)
                        ) AS items(edit)
                        WHERE CASE
                            WHEN editor_patches.source = 'human'
                                THEN CASE items.edit ->> 'kind'
                                    WHEN 'modification' THEN 'replacement'
                                    ELSE items.edit ->> 'kind'
                                END
                            WHEN items.edit ->> 'position'
                                IN ('append', 'prepend')
                                OR (
                                    COALESCE(items.edit ->> 'find', '') = ''
                                    AND COALESCE(items.edit ->> 'text', '') <> ''
                                )
                                THEN 'insertion'
                            WHEN COALESCE(items.edit ->> 'find', '') <> ''
                                AND COALESCE(items.edit ->> 'text', '') = ''
                                THEN 'deletion'
                            ELSE 'replacement'
                        END = :suggestion_kind
                    )
                    """
                ).bindparams(suggestion_kind=suggestion_kind)
            )
        statement = statement.order_by(
            editor_patch_created_at_ms.desc(),
            editor_patches.c.patch_id.desc(),
        ).limit(bounded_limit + 1)
        async with self._session(tenant_id) as session:
            rows = (await session.execute(statement)).mappings().all()
        page_rows = rows[:bounded_limit]
        next_cursor = (
            (
                float(page_rows[-1]["created_at"]),
                str(page_rows[-1]["patch_id"]),
            )
            if len(rows) > bounded_limit and page_rows
            else None
        )
        result: list[CollaborationOpenPatch] = []
        for row in page_rows:
            author_kind, author_id = _patch_author(row)
            if author_id is None:
                log.error(
                    "Collaboration patch %s has no author identity.",
                    row["patch_id"],
                )
                raise CollaborationConflict("patch_metadata_conflict")
            edits = row["edits"] or []
            exact_edits: tuple[dict[str, Any], ...] | None
            if row["source"] == "human":
                exact_edits = None
                kinds = tuple(
                    dict.fromkeys(
                        str(item.get("kind"))
                        for item in edits
                        if isinstance(item, dict)
                        and item.get("kind")
                        in {
                            "insertion",
                            "deletion",
                            "replacement",
                            "format",
                            "structure",
                            "modification",
                        }
                    )
                )
                kinds = tuple(
                    "replacement" if kind == "modification" else kind
                    for kind in kinds
                )
            else:
                exact_edits = tuple(
                    dict(item) for item in edits if isinstance(item, dict)
                )
                kinds = tuple(
                    dict.fromkeys(_suggestion_kind_for_edit(item) for item in exact_edits)
                )
            if not kinds:
                log.error(
                    "Collaboration patch %s has no suggestion kind.",
                    row["patch_id"],
                )
                raise CollaborationConflict("patch_metadata_conflict")
            if suggestion_kind is not None and suggestion_kind not in kinds:
                log.error(
                    "Collaboration patch %s disagrees with its SQL suggestion kind.",
                    row["patch_id"],
                )
                raise CollaborationConflict("patch_metadata_conflict")
            result.append(
                CollaborationOpenPatch(
                    patch_id=str(row["patch_id"]),
                    author_user_id=author_id if author_kind == "user" else None,
                    author_guest_identity_id=(
                        author_id if author_kind == "guest" else None
                    ),
                    created_at=float(row["created_at"]),
                    suggestion_ids=tuple(
                        str(item) for item in (row["suggestion_ids"] or [])
                    ),
                    kinds=kinds,
                    exact_edits=exact_edits,
                )
            )
        return CollaborationOpenPatchPage(
            patches=tuple(result),
            next_cursor=next_cursor,
        )

    async def list_comment_threads(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        since_revision: int,
        status: str,
        limit: int,
    ) -> CollaborationCommentPage:
        if since_revision < 0 or status not in {"all", "open", "resolved"}:
            raise ValueError("invalid shared-comment filters")
        bounded_limit = max(1, min(limit, 200))
        async with self._session(tenant_id) as session:
            document, owner_user_id = await _read_comment_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                guest_link_id=guest_link_id,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                guest_links_enabled=self._guest_links_enabled,
                now=time.time(),
            )
            current_revision = int(document.collaboration_comment_revision)
            statement = select(editor_collaboration_comment_threads).where(
                editor_collaboration_comment_threads.c.tenant_id == tenant_id,
                editor_collaboration_comment_threads.c.document_id == document_id,
                editor_collaboration_comment_threads.c.generation == generation,
                editor_collaboration_comment_threads.c.revision > since_revision,
            )
            # Incremental pages return transitions out of the selected status
            # as well, so clients can remove a row that was resolved remotely.
            if since_revision == 0 and status != "all":
                statement = statement.where(
                    editor_collaboration_comment_threads.c.status == status
                )
            # A deleted thread is absent from a fresh load, but an incremental
            # page MUST still carry it once: a client that already holds the
            # card learns it is gone only from this page. Filtering it here
            # would leave the card standing until a reload.
            if since_revision == 0:
                statement = statement.where(
                    editor_collaboration_comment_threads.c.deleted_at.is_(None)
                )
            thread_rows = (
                await session.execute(
                    statement.order_by(
                        editor_collaboration_comment_threads.c.revision,
                        editor_collaboration_comment_threads.c.id,
                    ).limit(bounded_limit + 1)
                )
            ).all()
            has_more = len(thread_rows) > bounded_limit
            page_rows = thread_rows[:bounded_limit]
            page_revision = (
                int(page_rows[-1].revision)
                if has_more and page_rows
                else current_revision
            )
            thread_ids = tuple(row.id for row in page_rows)
            messages_by_thread: dict[
                uuid.UUID, list[CollaborationCommentMessage]
            ] = {thread_id: [] for thread_id in thread_ids}
            if thread_ids:
                message_rows = (
                    await session.execute(
                        select(editor_collaboration_comment_messages)
                        .where(
                            editor_collaboration_comment_messages.c.tenant_id
                            == tenant_id,
                            editor_collaboration_comment_messages.c.thread_id.in_(
                                thread_ids
                            ),
                        )
                        .order_by(
                            editor_collaboration_comment_messages.c.created_at,
                            editor_collaboration_comment_messages.c.id,
                        )
                    )
                ).all()
                for message_row in message_rows:
                    messages_by_thread[message_row.thread_id].append(
                        _comment_message(message_row)
                    )
            if actor_guest_identity_id is not None:
                read_row = await session.scalar(
                    select(
                        editor_document_guest_identities.c.last_read_revision
                    ).where(
                        editor_document_guest_identities.c.tenant_id
                        == tenant_id,
                        editor_document_guest_identities.c.id
                        == actor_guest_identity_id,
                    )
                )
            else:
                read_row = (
                    await session.execute(
                        select(
                            editor_collaboration_comment_reads.c.last_read_revision
                        ).where(
                            editor_collaboration_comment_reads.c.tenant_id
                            == tenant_id,
                            editor_collaboration_comment_reads.c.document_id
                            == document_id,
                            editor_collaboration_comment_reads.c.generation
                            == generation,
                            editor_collaboration_comment_reads.c.user_id
                            == actor_user_id,
                        )
                    )
                ).scalar_one_or_none()
            shared_participants: set[uuid.UUID] = set()
            if self._sharing_enabled:
                shared_participants = set(
                    (
                        await session.execute(
                            select(resource_shares.c.recipient_user_id).where(
                                resource_shares.c.tenant_id == tenant_id,
                                resource_shares.c.resource_type
                                == "editor_document",
                                resource_shares.c.resource_id == document_id,
                                resource_shares.c.accepted_at.isnot(None),
                                resource_shares.c.revoked_at.is_(None),
                            )
                        )
                    ).scalars()
                )
            participant_ids = shared_participants | {owner_user_id}
            active_participant_ids = tuple(
                sorted(
                    (
                        await session.execute(
                            select(users.c.id).where(
                                users.c.tenant_id == tenant_id,
                                users.c.id.in_(participant_ids),
                                users.c.disabled_at.is_(None),
                            )
                        )
                    ).scalars(),
                    key=str,
                )
            )
            guest_participant_ids = tuple(
                sorted(
                    {
                        guest_id
                        for thread in (
                            _comment_thread(
                                row,
                                tuple(messages_by_thread.get(row.id, ())),
                            )
                            for row in page_rows
                        )
                        for guest_id in (
                            thread.created_by_guest_identity_id,
                            thread.resolved_by_guest_identity_id,
                            *(
                                message.author_guest_identity_id
                                for message in thread.messages
                            ),
                        )
                        if guest_id is not None
                    },
                    key=str,
                )
            )
            return CollaborationCommentPage(
                threads=tuple(
                    _comment_thread(
                        row,
                        tuple(messages_by_thread.get(row.id, ())),
                    )
                    for row in page_rows
                ),
                revision=page_revision,
                last_read_revision=int(read_row or 0),
                current_revision=current_revision,
                has_more=has_more,
                participant_user_ids=active_participant_ids,
                participant_guest_identity_ids=guest_participant_ids,
            )

    async def create_comment_thread(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        anchor: dict[str, Any],
        quote_text: str,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        async with self._session(tenant_id) as session:
            document, owner_user_id = await _lock_comment_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                guest_link_id=guest_link_id,
                minimum=SharePermission.SUGGEST,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                guest_links_enabled=self._guest_links_enabled,
                now=now,
                allow_comment=True,
            )
            existing = (
                await session.execute(
                    select(editor_collaboration_comment_threads).where(
                        editor_collaboration_comment_threads.c.tenant_id
                        == tenant_id,
                        editor_collaboration_comment_threads.c.id == thread_id,
                    )
                )
            ).one_or_none()
            if existing is not None:
                if (
                    existing.last_command_id == command_id
                    and existing.last_command_payload_hash
                    == command_payload_hash
                    and existing.last_command_kind == "create"
                ):
                    return await _comment_thread_in_session(
                        session,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        generation=generation,
                        thread_id=thread_id,
                    )
                raise CollaborationConflict("comment_command_conflict")
            # A new thread overlaps no other thread, so it has no revision
            # precondition: whoever commented since the author's last load,
            # the thread lands. The document lock taken above still gives it
            # the next revision. A reply, an edit, a resolve or a delete
            # checks the revision of its own thread instead.
            await _validate_comment_mentions(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                owner_user_id=owner_user_id,
                mention_user_ids=mention_user_ids,
                sharing_enabled=self._sharing_enabled,
            )
            revision = int(document.collaboration_comment_revision) + 1
            await session.execute(
                insert(editor_collaboration_comment_threads).values(
                    id=thread_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    generation=generation,
                    revision=revision,
                    status="open",
                    created_by_user_id=actor_user_id,
                    created_by_guest_identity_id=actor_guest_identity_id,
                    resolved_by_user_id=None,
                    resolved_at=None,
                    anchor=anchor,
                    quote_text=quote_text,
                    created_at=now,
                    updated_at=now,
                    last_command_id=command_id,
                    last_command_payload_hash=command_payload_hash,
                    last_command_kind="create",
                )
            )
            await session.execute(
                insert(editor_collaboration_comment_messages).values(
                    id=message_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    thread_id=thread_id,
                    revision=revision,
                    author_user_id=actor_user_id,
                    author_guest_identity_id=actor_guest_identity_id,
                    body_markdown=body_markdown,
                    mention_user_ids=[str(value) for value in mention_user_ids],
                    created_at=now,
                    edited_at=None,
                    deleted_at=None,
                    last_command_id=command_id,
                    last_command_payload_hash=command_payload_hash,
                    last_command_kind="create",
                )
            )
            await session.execute(
                update(editor_documents)
                .where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.id == document_id,
                )
                .values(collaboration_comment_revision=revision)
            )
            await _append_comment_effects(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                owner_user_id=owner_user_id,
                action="editor.collaboration_comment.created",
                mention_user_ids=mention_user_ids,
            )
            return await _comment_thread_in_session(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                thread_id=thread_id,
            )

    async def add_comment_reply(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        async with self._session(tenant_id) as session:
            document, owner_user_id = await _lock_comment_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                guest_link_id=guest_link_id,
                minimum=SharePermission.SUGGEST,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                guest_links_enabled=self._guest_links_enabled,
                now=now,
                allow_comment=True,
            )
            existing_message = (
                await session.execute(
                    select(editor_collaboration_comment_messages).where(
                        editor_collaboration_comment_messages.c.tenant_id
                        == tenant_id,
                        editor_collaboration_comment_messages.c.id == message_id,
                    )
                )
            ).one_or_none()
            if existing_message is not None:
                if (
                    existing_message.thread_id == thread_id
                    and existing_message.last_command_id == command_id
                    and existing_message.last_command_payload_hash
                    == command_payload_hash
                    and existing_message.last_command_kind == "reply"
                ):
                    return await _comment_thread_in_session(
                        session,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        generation=generation,
                        thread_id=thread_id,
                    )
                raise CollaborationConflict("comment_command_conflict")
            thread_row = (
                await session.execute(
                    select(editor_collaboration_comment_threads)
                    .where(
                        editor_collaboration_comment_threads.c.tenant_id
                        == tenant_id,
                        editor_collaboration_comment_threads.c.document_id
                        == document_id,
                        editor_collaboration_comment_threads.c.generation
                        == generation,
                        editor_collaboration_comment_threads.c.id == thread_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if thread_row is None:
                raise CollaborationConflict("comment_thread_not_found")
            if thread_row.status != "open":
                raise CollaborationConflict("comment_thread_resolved")
            if int(thread_row.revision) != expected_revision:
                raise CollaborationConflict(
                    "comment_revision_conflict",
                    current_sequence=int(thread_row.revision),
                )
            await _validate_comment_mentions(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                owner_user_id=owner_user_id,
                mention_user_ids=mention_user_ids,
                sharing_enabled=self._sharing_enabled,
            )
            revision = int(document.collaboration_comment_revision) + 1
            await session.execute(
                insert(editor_collaboration_comment_messages).values(
                    id=message_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    thread_id=thread_id,
                    revision=revision,
                    author_user_id=actor_user_id,
                    author_guest_identity_id=actor_guest_identity_id,
                    body_markdown=body_markdown,
                    mention_user_ids=[str(value) for value in mention_user_ids],
                    created_at=now,
                    edited_at=None,
                    deleted_at=None,
                    last_command_id=command_id,
                    last_command_payload_hash=command_payload_hash,
                    last_command_kind="reply",
                )
            )
            await session.execute(
                update(editor_collaboration_comment_threads)
                .where(
                    editor_collaboration_comment_threads.c.tenant_id
                    == tenant_id,
                    editor_collaboration_comment_threads.c.id == thread_id,
                )
                .values(revision=revision, updated_at=now)
            )
            await session.execute(
                update(editor_documents)
                .where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.id == document_id,
                )
                .values(collaboration_comment_revision=revision)
            )
            await _append_comment_effects(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                owner_user_id=owner_user_id,
                action="editor.collaboration_comment.replied",
                mention_user_ids=mention_user_ids,
            )
            return await _comment_thread_in_session(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                thread_id=thread_id,
            )

    async def update_comment_message(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str | None,
        mention_user_ids: tuple[uuid.UUID, ...],
        delete_message: bool,
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        command_kind = "delete" if delete_message else "edit"
        async with self._session(tenant_id) as session:
            document, owner_user_id = await _lock_comment_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                guest_link_id=guest_link_id,
                minimum=SharePermission.SUGGEST,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                guest_links_enabled=self._guest_links_enabled,
                now=now,
                allow_comment=True,
            )
            thread_row = (
                await session.execute(
                    select(editor_collaboration_comment_threads)
                    .where(
                        editor_collaboration_comment_threads.c.tenant_id
                        == tenant_id,
                        editor_collaboration_comment_threads.c.document_id
                        == document_id,
                        editor_collaboration_comment_threads.c.generation
                        == generation,
                        editor_collaboration_comment_threads.c.id == thread_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if thread_row is None:
                raise CollaborationConflict("comment_thread_not_found")
            message_row = (
                await session.execute(
                    select(editor_collaboration_comment_messages)
                    .where(
                        editor_collaboration_comment_messages.c.tenant_id
                        == tenant_id,
                        editor_collaboration_comment_messages.c.thread_id
                        == thread_id,
                        editor_collaboration_comment_messages.c.id == message_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if message_row is None:
                raise CollaborationConflict("comment_message_not_found")
            if (
                message_row.last_command_id == command_id
                and message_row.last_command_payload_hash == command_payload_hash
                and message_row.last_command_kind == command_kind
            ):
                return await _comment_thread_in_session(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    generation=generation,
                    thread_id=thread_id,
                )
            if int(thread_row.revision) != expected_revision:
                raise CollaborationConflict(
                    "comment_revision_conflict",
                    current_sequence=int(thread_row.revision),
                )
            if (
                message_row.author_user_id != actor_user_id
                or message_row.author_guest_identity_id
                != actor_guest_identity_id
            ):
                raise CollaborationConflict("comment_author_required")
            if message_row.deleted_at is not None:
                raise CollaborationConflict("comment_message_deleted")
            if not delete_message:
                await _validate_comment_mentions(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    owner_user_id=owner_user_id,
                    mention_user_ids=mention_user_ids,
                    sharing_enabled=self._sharing_enabled,
                )
            revision = int(document.collaboration_comment_revision) + 1
            values: dict[str, Any] = {
                "revision": revision,
                "last_command_id": command_id,
                "last_command_payload_hash": command_payload_hash,
                "last_command_kind": command_kind,
            }
            if delete_message:
                values.update(
                    body_markdown="",
                    mention_user_ids=[],
                    deleted_at=now,
                )
            else:
                values.update(
                    body_markdown=body_markdown,
                    mention_user_ids=[
                        str(value) for value in mention_user_ids
                    ],
                    edited_at=now,
                )
            await session.execute(
                update(editor_collaboration_comment_messages)
                .where(
                    editor_collaboration_comment_messages.c.tenant_id
                    == tenant_id,
                    editor_collaboration_comment_messages.c.id == message_id,
                )
                .values(**values)
            )
            await session.execute(
                update(editor_collaboration_comment_threads)
                .where(
                    editor_collaboration_comment_threads.c.tenant_id
                    == tenant_id,
                    editor_collaboration_comment_threads.c.id == thread_id,
                )
                .values(revision=revision, updated_at=now)
            )
            await session.execute(
                update(editor_documents)
                .where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.id == document_id,
                )
                .values(collaboration_comment_revision=revision)
            )
            await _append_comment_effects(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                owner_user_id=owner_user_id,
                action=(
                    "editor.collaboration_comment.message_deleted"
                    if delete_message
                    else "editor.collaboration_comment.message_edited"
                ),
                mention_user_ids=(
                    () if delete_message else mention_user_ids
                ),
            )
            return await _comment_thread_in_session(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                thread_id=thread_id,
            )

    async def set_comment_thread_status(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        status: str,
        can_moderate: bool,
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        if status not in {"open", "resolved"}:
            raise ValueError("invalid shared-comment status")
        command_kind = "resolve" if status == "resolved" else "reopen"
        async with self._session(tenant_id) as session:
            document, owner_user_id = await _lock_comment_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                guest_link_id=guest_link_id,
                minimum=SharePermission.SUGGEST,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                guest_links_enabled=self._guest_links_enabled,
                now=now,
                allow_comment=True,
            )
            thread_row = (
                await session.execute(
                    select(editor_collaboration_comment_threads)
                    .where(
                        editor_collaboration_comment_threads.c.tenant_id
                        == tenant_id,
                        editor_collaboration_comment_threads.c.document_id
                        == document_id,
                        editor_collaboration_comment_threads.c.generation
                        == generation,
                        editor_collaboration_comment_threads.c.id == thread_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if thread_row is None:
                raise CollaborationConflict("comment_thread_not_found")
            if (
                thread_row.last_command_id == command_id
                and thread_row.last_command_payload_hash == command_payload_hash
                and thread_row.last_command_kind == command_kind
            ):
                return await _comment_thread_in_session(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    generation=generation,
                    thread_id=thread_id,
                )
            if int(thread_row.revision) != expected_revision:
                raise CollaborationConflict(
                    "comment_revision_conflict",
                    current_sequence=int(thread_row.revision),
                )
            if (
                (
                    thread_row.created_by_user_id != actor_user_id
                    or thread_row.created_by_guest_identity_id
                    != actor_guest_identity_id
                )
                and not can_moderate
            ):
                raise CollaborationConflict("comment_resolve_forbidden")
            if thread_row.status == status:
                raise CollaborationConflict("comment_status_conflict")
            revision = int(document.collaboration_comment_revision) + 1
            await session.execute(
                update(editor_collaboration_comment_threads)
                .where(
                    editor_collaboration_comment_threads.c.tenant_id
                    == tenant_id,
                    editor_collaboration_comment_threads.c.id == thread_id,
                )
                .values(
                    revision=revision,
                    status=status,
                    resolved_by_user_id=(
                        actor_user_id if status == "resolved" else None
                    ),
                    resolved_by_guest_identity_id=(
                        actor_guest_identity_id
                        if status == "resolved"
                        else None
                    ),
                    resolved_at=now if status == "resolved" else None,
                    updated_at=now,
                    last_command_id=command_id,
                    last_command_payload_hash=command_payload_hash,
                    last_command_kind=command_kind,
                )
            )
            await session.execute(
                update(editor_documents)
                .where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.id == document_id,
                )
                .values(collaboration_comment_revision=revision)
            )
            await _append_comment_effects(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                owner_user_id=owner_user_id,
                action=(
                    "editor.collaboration_comment.resolved"
                    if status == "resolved"
                    else "editor.collaboration_comment.reopened"
                ),
            )
            return await _comment_thread_in_session(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                thread_id=thread_id,
            )

    async def delete_comment_thread(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        """Retire a whole thread the actor started.

        Resolving hides a discussion that happened; this is for one that was
        never meant to exist. The row and its messages stay for the audit and
        every read path skips them, so the card is gone for the people in the
        document while the record is not.

        Deliberately NOT moderation: unlike resolving, this removes other
        people's replies from view, so only the thread's own author may do it
        — the same rule the per-message actions already follow.
        """
        command_kind = "delete"
        async with self._session(tenant_id) as session:
            document, owner_user_id = await _lock_comment_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                guest_link_id=guest_link_id,
                minimum=SharePermission.SUGGEST,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                guest_links_enabled=self._guest_links_enabled,
                now=now,
                allow_comment=True,
            )
            thread_row = (
                await session.execute(
                    select(editor_collaboration_comment_threads)
                    .where(
                        editor_collaboration_comment_threads.c.tenant_id
                        == tenant_id,
                        editor_collaboration_comment_threads.c.document_id
                        == document_id,
                        editor_collaboration_comment_threads.c.generation
                        == generation,
                        editor_collaboration_comment_threads.c.id == thread_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if thread_row is None:
                raise CollaborationConflict("comment_thread_not_found")
            if (
                thread_row.last_command_id == command_id
                and thread_row.last_command_payload_hash == command_payload_hash
                and thread_row.last_command_kind == command_kind
            ):
                return await _comment_thread_in_session(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    generation=generation,
                    thread_id=thread_id,
                )
            if thread_row.deleted_at is not None:
                raise CollaborationConflict("comment_thread_not_found")
            if int(thread_row.revision) != expected_revision:
                raise CollaborationConflict(
                    "comment_revision_conflict",
                    current_sequence=int(thread_row.revision),
                )
            if (
                thread_row.created_by_user_id != actor_user_id
                or thread_row.created_by_guest_identity_id
                != actor_guest_identity_id
            ):
                raise CollaborationConflict("comment_delete_forbidden")
            revision = int(document.collaboration_comment_revision) + 1
            await session.execute(
                update(editor_collaboration_comment_threads)
                .where(
                    editor_collaboration_comment_threads.c.tenant_id
                    == tenant_id,
                    editor_collaboration_comment_threads.c.id == thread_id,
                )
                .values(
                    revision=revision,
                    deleted_at=now,
                    updated_at=now,
                    last_command_id=command_id,
                    last_command_payload_hash=command_payload_hash,
                    last_command_kind=command_kind,
                )
            )
            await session.execute(
                update(editor_documents)
                .where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.id == document_id,
                )
                .values(collaboration_comment_revision=revision)
            )
            await _append_comment_effects(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                owner_user_id=owner_user_id,
                action="editor.collaboration_comment.deleted",
            )
            return await _comment_thread_in_session(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                thread_id=thread_id,
            )

    async def mark_comments_read(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        revision: int,
        now: float,
    ) -> int:
        if revision < 0:
            raise ValueError("revision must be non-negative")
        async with self._session(tenant_id) as session:
            document, _owner_user_id = await _read_comment_document(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                actor_user_id=actor_user_id,
                actor_guest_identity_id=actor_guest_identity_id,
                guest_link_id=guest_link_id,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                guest_links_enabled=self._guest_links_enabled,
                now=now,
            )
            current = int(document.collaboration_comment_revision)
            if revision > current:
                raise CollaborationConflict(
                    "comment_revision_conflict",
                    current_sequence=current,
                )
            if actor_guest_identity_id is not None:
                await session.execute(
                    update(editor_document_guest_identities)
                    .where(
                        editor_document_guest_identities.c.tenant_id
                        == tenant_id,
                        editor_document_guest_identities.c.id
                        == actor_guest_identity_id,
                    )
                    .values(
                        last_read_revision=func.greatest(
                            editor_document_guest_identities.c.last_read_revision,
                            revision,
                        ),
                        last_seen_at=now,
                    )
                )
            else:
                await session.execute(
                    pg_insert(editor_collaboration_comment_reads)
                    .values(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        generation=generation,
                        user_id=actor_user_id,
                        last_read_revision=revision,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=(
                            editor_collaboration_comment_reads.c.tenant_id,
                            editor_collaboration_comment_reads.c.document_id,
                            editor_collaboration_comment_reads.c.generation,
                            editor_collaboration_comment_reads.c.user_id,
                        ),
                        set_={
                            "last_read_revision": func.greatest(
                                editor_collaboration_comment_reads.c.last_read_revision,
                                revision,
                            ),
                            "updated_at": now,
                        },
                    )
                )
            return revision

    async def current_policy_cursor(self, *, tenant_id: str) -> int:
        """Return the tenant's greatest committed content-free event ID."""
        async with self._session(tenant_id) as session:
            current = await session.scalar(
                select(func.max(user_events.c.id)).where(
                    user_events.c.tenant_id == tenant_id
                )
            )
        return int(current) if current is not None else 0

    async def policy_events_after(
        self,
        *,
        tenant_id: str,
        cursor: int,
        limit: int,
    ) -> CollaborationPolicyPage:
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        bounded_limit = max(1, min(limit, 500))
        async with self._session(tenant_id) as session:
            bounds = (
                await session.execute(
                    select(
                        func.min(user_events.c.id),
                        func.max(user_events.c.id),
                    ).where(user_events.c.tenant_id == tenant_id)
                )
            ).one()
            oldest = int(bounds[0]) if bounds[0] is not None else 0
            current = int(bounds[1]) if bounds[1] is not None else 0
            if cursor > current and cursor != 0:
                return CollaborationPolicyPage(
                    (), current, reset_required=True
                )
            if cursor and oldest and cursor < oldest - 1:
                return CollaborationPolicyPage(
                    (), current, reset_required=True
                )
            rows = (
                await session.execute(
                    select(user_events)
                    .where(
                        user_events.c.tenant_id == tenant_id,
                        user_events.c.id > cursor,
                        user_events.c.resource_type.in_(
                            ("editor_document", "user")
                        ),
                    )
                    .order_by(user_events.c.id)
                    .limit(bounded_limit + 1)
                )
            ).all()
        if len(rows) > bounded_limit:
            return CollaborationPolicyPage((), current, reset_required=True)
        return CollaborationPolicyPage(
            events=tuple(
                CollaborationPolicyEvent(
                    id=int(row.id),
                    target_user_id=row.target_user_id,
                    scope=str(row.scope),
                    resource_type=str(row.resource_type),
                    resource_id=(
                        str(row.resource_id)
                        if row.resource_id is not None
                        else None
                    ),
                )
                for row in rows
            ),
            current_cursor=current,
        )

    async def compact(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        instance_id: str,
        instance_epoch: int,
        now: float,
        payload_retention_seconds: float,
        metadata_retention_seconds: float,
    ) -> tuple[int, int]:
        if payload_retention_seconds < 0 or metadata_retention_seconds <= 0:
            raise ValueError("retention windows must be non-negative")
        async with self._session(tenant_id) as session:
            await _lock_instance_fence(
                session,
                tenant_id=tenant_id,
                instance_id=instance_id,
                instance_epoch=instance_epoch,
                now=now,
            )
            snapshot_sequences = list(
                (
                    await session.execute(
                        select(
                            editor_collaboration_snapshots.c.covered_sequence
                        )
                        .where(
                            editor_collaboration_snapshots.c.tenant_id
                            == tenant_id,
                            editor_collaboration_snapshots.c.document_id
                            == document_id,
                            editor_collaboration_snapshots.c.generation
                            == generation,
                        )
                        .order_by(
                            editor_collaboration_snapshots.c.covered_sequence.desc()
                        )
                    )
                ).scalars()
            )
            if not snapshot_sequences:
                raise CollaborationConflict("snapshot_missing")
            retained_snapshot_sequences = snapshot_sequences[:2]
            covered_sequence = int(retained_snapshot_sequences[-1])
            payload_result = await session.execute(
                update(editor_collaboration_updates)
                .where(
                    editor_collaboration_updates.c.tenant_id == tenant_id,
                    editor_collaboration_updates.c.document_id == document_id,
                    editor_collaboration_updates.c.generation == generation,
                    editor_collaboration_updates.c.sequence <= covered_sequence,
                    editor_collaboration_updates.c.update_bytes.isnot(None),
                    editor_collaboration_updates.c.created_at
                    <= now - payload_retention_seconds,
                )
                .values(update_bytes=None, payload_pruned_at=now)
            )
            metadata_result = await session.execute(
                delete(editor_collaboration_updates).where(
                    editor_collaboration_updates.c.tenant_id == tenant_id,
                    editor_collaboration_updates.c.document_id == document_id,
                    editor_collaboration_updates.c.generation == generation,
                    editor_collaboration_updates.c.sequence <= covered_sequence,
                    editor_collaboration_updates.c.update_bytes.is_(None),
                    editor_collaboration_updates.c.created_at
                    <= now - metadata_retention_seconds,
                )
            )
            if len(snapshot_sequences) > 2:
                await session.execute(
                    delete(editor_collaboration_snapshots).where(
                        editor_collaboration_snapshots.c.tenant_id == tenant_id,
                        editor_collaboration_snapshots.c.document_id == document_id,
                        editor_collaboration_snapshots.c.generation == generation,
                        editor_collaboration_snapshots.c.covered_sequence.in_(
                            snapshot_sequences[2:]
                        ),
                    )
                )
        return (
            int(payload_result.rowcount or 0),
            int(metadata_result.rowcount or 0),
        )

    async def tombstone_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
        owner_user_id: uuid.UUID,
        now: float,
    ) -> int:
        async with self._session(tenant_id) as session:
            access = await lock_resource_access(
                session,
                tenant_id=tenant_id,
                actor_user_id=owner_user_id,
                resource_type="editor_document",
                resource_table=editor_documents,
                id_column=editor_documents.c.id,
                resource_id=document_id,
                owner_column=editor_documents.c.created_by_user_id,
                minimum=SharePermission.VIEW,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                owner_only=True,
            )
            if access is None:
                raise CollaborationDocumentNotFound(document_id)
            row = (
                await session.execute(
                    select(editor_documents)
                    .where(
                        editor_documents.c.tenant_id == tenant_id,
                        editor_documents.c.id == document_id,
                    )
                    .with_for_update()
                )
            ).one()
            if row.content_mode != "collaboration":
                raise CollaborationConflict("mode_conflict")
            if row.deleted_at is not None:
                return int(row.collaboration_generation)
            next_generation = int(row.collaboration_generation) + 1
            recipients = await revoke_resource_shares(
                session,
                tenant_id=tenant_id,
                resource_type="editor_document",
                resource_id=document_id,
                revoked_by_user_id=owner_user_id,
            )
            await session.execute(
                update(editor_collaboration_leases)
                .where(
                    editor_collaboration_leases.c.tenant_id == tenant_id,
                    editor_collaboration_leases.c.document_id == document_id,
                    editor_collaboration_leases.c.revoked_at.is_(None),
                )
                # A lease minted by a transaction that held the document
                # before this delete can carry a later request stamp than the
                # delete's own; no lease is revoked before it was issued.
                .values(
                    revoked_at=func.greatest(
                        now, editor_collaboration_leases.c.issued_at
                    )
                )
            )
            await session.execute(
                update(editor_documents)
                .where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.id == document_id,
                )
                .values(
                    collaboration_generation=next_generation,
                    metadata_revision=int(row.metadata_revision) + 1,
                    deleted_at=now,
                    updated_at=now,
                )
            )
            await append_resource_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=owner_user_id,
                owner_user_id=owner_user_id,
                action="editor_document.deleted",
                resource_type="editor_document",
                resource_id=document_id,
                scope="editor_documents",
                additional_targets=recipients,
            )
        return next_generation

    async def purge_tombstones(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        instance_epoch: int,
        now: float,
        retention_seconds: float,
    ) -> int:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        async with self._session(tenant_id) as session:
            await _lock_instance_fence(
                session,
                tenant_id=tenant_id,
                instance_id=instance_id,
                instance_epoch=instance_epoch,
                now=now,
            )
            result = await session.execute(
                delete(editor_documents).where(
                    editor_documents.c.tenant_id == tenant_id,
                    editor_documents.c.content_mode == "collaboration",
                    editor_documents.c.deleted_at.isnot(None),
                    editor_documents.c.deleted_at <= now - retention_seconds,
                )
            )
        return int(result.rowcount or 0)
