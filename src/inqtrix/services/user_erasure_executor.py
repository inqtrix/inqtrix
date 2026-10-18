"""Carry out the erasure the plan describes, in short resumable steps.

An account's erasure is a deletion operation of the target kind ``user``,
run by the deletion worker in stages -- never as one long transaction:

* **Start.** ``begin_erasure`` closes every way into the account under the
  tenant's security lock and records the operation on the ``users`` row.
  That runs before the operation is submitted; the first step here only
  checks that the account is still marked by THIS operation.
* **Aggregates.** Data outside the foreign-key graph -- library assets with
  their blobs, knowledge collections and documents with their vectors,
  vector indexes, sessions and runs with their checkpoints -- goes through
  the owning surface: the deletion machine or the run store, acting as the
  account's owner. Nothing here re-implements their ordering.
* **Rows.** The account's own rows go in short batches, roots before leaves
  so the schema's cascades do the bulk; references on rows that belong to
  other people are then reassigned or cleared, also in batches. None of
  this holds the tenant lock, so sign-ins and everybody else's work carry
  on while a large account is erased.
* **End.** Nothing but the retained audit and usage rows may still name the
  account. ``finish_erasure`` then removes the ``users`` row and writes the
  audit entry, under the lock once more.

Every step is idempotent, so a crashed or failed attempt resumes where it
stopped, and the operation stays visible until the account is gone.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.runs.deletion_operations import DeletionStage
from inqtrix.services.user_erasure import (
    DELETED_ACCOUNT_DISPLAY_NAME,
    DELETED_ACCOUNT_EMAIL,
    DELETED_ACCOUNT_ISSUER,
    DELETED_ACCOUNT_SUBJECT,
    ERASURE_PLAN,
    Disposition,
    rule_for,
)
from inqtrix.services.user_erasure_inventory import (
    _match,
    _tables,
    collect_inventory,
)
from inqtrix.storage.db import tenant_session
from inqtrix.sync_bridge import run_coro_sync

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from inqtrix.runs.deletion_operations import DeletionJobHandle

log = logging.getLogger("inqtrix")

#: Rows per statement. Each batch is its own short transaction, so no
#: lock -- row or tenant -- is held across a large account.
BATCH_SIZE = 500

#: Delete order for the account's OWN rows. Roots first so the schema's
#: cascades do the bulk; the repeats afterwards catch the rows that live in
#: OTHER people's aggregates, which no cascade reaches. Delegated aggregates
#: are not listed: they are gone before this order runs, and a delegated
#: row that is still here is a residual the end step names instead of
#: deleting it past the surface that owns its outside data.
DELETE_ORDER: tuple[tuple[str, str], ...] = (
    # Editor: the document is the cascade root.
    ("editor_documents", "created_by_user_id"),
    ("editor_patches", "created_by_user_id"),
    ("editor_collaboration_comment_messages", "author_user_id"),
    ("editor_collaboration_comment_threads", "created_by_user_id"),
    ("editor_collaboration_comment_reads", "user_id"),
    ("editor_collaboration_leases", "user_id"),
    ("editor_comments", "created_by_user_id"),
    ("editor_document_share_links", "created_by_user_id"),
    ("editor_folders", "created_by_user_id"),
    # Chat and folders.
    ("chat_threads", "created_by_user_id"),
    ("chat_thread_groups", "created_by_user_id"),
    ("agent_session_groups", "created_by_user_id"),
    ("knowledge_session_groups", "created_by_user_id"),
    ("indexing_jobs", "created_by_user_id"),
    # Receipts of the user's own past deletions.
    ("deletion_operation_assets", "created_by_user_id"),
    ("deletion_operations", "created_by_user_id"),
    # Access and membership.
    ("resource_shares", "recipient_user_id"),
    ("resource_shares", "granted_by_user_id"),
    ("invitations", "invited_by_user_id"),
    ("workspace_members", "user_id"),
    # Library content and personal state.
    ("prompt_templates", "owner_user_id"),
    ("prompt_template_seed_markers", "user_id"),
    ("skill_templates", "owner_user_id"),
    ("agent_feedback", "user_id"),
    ("agent_memory_candidates", "user_id"),
    ("quota_usage_counters", "subject_user_id"),
    ("quota_usage_adjustments", "subject_user_id"),
    ("quota_stock_lifecycles", "subject_user_id"),
    ("quota_limits", "subject_user_id"),
    ("account_preferences", "user_id"),
    ("auth_sessions", "user_id"),
    ("local_credentials", "user_id"),
    ("personal_access_tokens", "owner_user_id"),
    ("user_authorization_generations", "user_id"),
    ("user_events", "target_user_id"),
)

#: A document is a cascade root with possibly thousands of children, so it
#: goes alone in its transaction.
_ONE_PER_BATCH = frozenset({"editor_documents"})


@dataclass(frozen=True)
class AggregateRef:
    """One aggregate the account owns, addressed the way its surface needs."""

    kind: str
    target_ids: tuple[str, ...]
    workspace_id: str | None = None


class AggregateDeletions(Protocol):
    """The surfaces that own the account's data outside the FK graph."""

    def pending(
        self, *, tenant_id: str, user_id: uuid.UUID
    ) -> tuple[AggregateRef, ...]:
        """Every aggregate the account still owns, in deletion order."""

    def delete(
        self, ref: AggregateRef, *, tenant_id: str, user_id: uuid.UUID
    ) -> None:
        """Delete one aggregate completely, or raise why it could not."""


class ErasureResiduals(RuntimeError):
    """Rows still name the account after every step ran."""

    def __init__(self, residuals: tuple[str, ...]) -> None:
        super().__init__(
            "the account is still referenced by: " + ", ".join(residuals)
        )
        self.residuals = residuals


class UserErasure:
    """Run one account's erasure operation, stage by stage."""

    def __init__(
        self,
        *,
        lifecycle: Any,
        session_factory: Any,
        app_role: str,
        aggregates: AggregateDeletions,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._lifecycle = lifecycle
        self._session_factory = session_factory
        self._app_role = app_role
        self._aggregates = aggregates
        self._batch_size = max(1, int(batch_size))

    def execute(
        self,
        handle: "DeletionJobHandle",
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        """Run every remaining stage; the operation completes at the end."""
        exists, marked_by = run_coro_sync(
            self._lifecycle.erasure_operation_id(
                tenant_id=tenant_id, user_id=user_id
            )
        )
        if not exists:
            # An earlier attempt removed the account and lost its terminal
            # write; finishing is all that is left.
            handle.complete()
            return
        if marked_by != handle.operation_id:
            raise RuntimeError(
                "the account is not being erased by this operation"
            )

        refs = self._aggregates.pending(tenant_id=tenant_id, user_id=user_id)
        total = len(refs) + 2
        for done, ref in enumerate(refs, start=1):
            handle.assert_current()
            self._aggregates.delete(ref, tenant_id=tenant_id, user_id=user_id)
            # One checkpoint per aggregate: a large account's erasure shows
            # how far it got, not only that its slowest stage is still open.
            handle.progress(
                DeletionStage.AGGREGATES_REMOVED,
                completed_items=done,
                total_items=total,
            )
        if not refs:
            handle.progress(
                DeletionStage.AGGREGATES_REMOVED,
                completed_items=0,
                total_items=total,
            )

        handle.assert_current()
        placeholder = run_coro_sync(self._delete_rows(tenant_id, user_id))
        log.info(
            "Account erasure %s: rows removed, references moved to %s.",
            handle.operation_id,
            placeholder,
        )
        handle.progress(
            DeletionStage.ROWS_REMOVED,
            completed_items=len(refs) + 1,
            total_items=total,
        )

        handle.assert_current()
        residuals = run_coro_sync(self._residuals(tenant_id, user_id))
        if residuals:
            raise ErasureResiduals(residuals)
        handle.progress(
            DeletionStage.RESIDUALS_VERIFIED,
            completed_items=total,
            total_items=total,
        )
        handle.assert_current()
        run_coro_sync(
            self._lifecycle.finish_erasure(
                tenant_id=tenant_id,
                user_id=user_id,
                operation_id=handle.operation_id,
                actor_user_id=actor_user_id,
            )
        )
        handle.complete()

    # -- rows --------------------------------------------------------------- #

    def _session(self, tenant_id: str):
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    async def _delete_rows(self, tenant_id: str, user_id: uuid.UUID) -> uuid.UUID:
        """Delete owned rows, then move or clear what others' rows point at.

        Deletes run first on purpose: a human collaboration update and a
        resolved thread carry a CHECK that requires an actor, and inside
        the account's OWN documents those rows go with the document. What
        is left pointing at the account afterwards belongs to somebody
        else.
        """
        tables = _tables()
        for name, column in DELETE_ORDER:
            table = tables.get(name)
            rule = rule_for(name, column)
            if table is None or rule is None or column not in table.c:
                continue
            limit = 1 if name in _ONE_PER_BATCH else self._batch_size
            while await self._delete_batch(
                tenant_id, table, _match(table, rule, user_id), limit
            ):
                pass

        async with self._session(tenant_id) as db:
            placeholder = await ensure_deleted_account(db, tenant_id=tenant_id)
        for rule in ERASURE_PLAN:
            if rule.disposition not in (
                Disposition.REASSIGN,
                Disposition.CLEAR_REFERENCE,
            ):
                continue
            table = tables.get(rule.table)
            if table is None or rule.column not in table.c:
                continue
            while await self._rewrite_batch(
                tenant_id, table, rule, user_id, placeholder
            ):
                pass
        return placeholder

    async def _delete_batch(
        self, tenant_id: str, table: sa.Table, predicate: Any, limit: int
    ) -> int:
        ctid = sa.literal_column("ctid")
        async with self._session(tenant_id) as db:
            result = await db.execute(
                sa.delete(table).where(
                    ctid.in_(
                        sa.select(ctid)
                        .select_from(table)
                        .where(predicate)
                        .limit(limit)
                    )
                )
            )
            return int(result.rowcount or 0)

    async def _rewrite_batch(
        self,
        tenant_id: str,
        table: sa.Table,
        rule: Any,
        user_id: uuid.UUID,
        placeholder: uuid.UUID,
    ) -> int:
        ctid = sa.literal_column("ctid")
        batch = ctid.in_(
            sa.select(ctid)
            .select_from(table)
            .where(_match(table, rule, user_id))
            .limit(self._batch_size)
        )
        if rule.column == "mention_user_ids":
            # Strip ONE id out of a JSONB array without disturbing the
            # rest of somebody else's message. The last mention leaves an
            # empty array, built in SQL: a bound "[]" would arrive as a
            # JSON string and fail the column's array CHECK.
            values = {
                "mention_user_ids": sa.func.coalesce(
                    sa.select(sa.func.jsonb_agg(sa.column("value")))
                    .select_from(
                        sa.func.jsonb_array_elements_text(
                            table.c.mention_user_ids
                        ).alias("value")
                    )
                    .where(sa.column("value") != str(user_id))
                    .scalar_subquery(),
                    sa.func.jsonb_build_array(),
                )
            }
        elif rule.disposition is Disposition.REASSIGN:
            values = {rule.column: placeholder}
        else:
            values = {rule.column: None}
        async with self._session(tenant_id) as db:
            result = await db.execute(sa.update(table).where(batch).values(values))
            return int(result.rowcount or 0)

    async def _residuals(self, tenant_id: str, user_id: uuid.UUID) -> tuple[str, ...]:
        """What still names the account, except the rows kept by design."""
        async with self._session(tenant_id) as db:
            inventory = await collect_inventory(db, user_id=user_id)
        return tuple(
            f"{line.table}.{line.column} ({line.rows})"
            for line in inventory.lines
            if line.rows and line.disposition is not Disposition.RETAIN
        )


async def ensure_deleted_account(
    session: "AsyncSession", *, tenant_id: str
) -> uuid.UUID:
    """The tenant's deleted-account placeholder, created on first use.

    An ordinary ``users`` row so every foreign key and CHECK holds, disabled
    from birth and without a credential, so it can never sign in.
    """
    from inqtrix.storage.identity_orm import users

    await session.execute(
        pg_insert(users)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            issuer=DELETED_ACCOUNT_ISSUER,
            subject=DELETED_ACCOUNT_SUBJECT,
            email=DELETED_ACCOUNT_EMAIL,
            display_name=DELETED_ACCOUNT_DISPLAY_NAME,
            disabled_at=sa.func.now(),
        )
        .on_conflict_do_nothing(constraint="uq_users_tenant_issuer_subject")
    )
    return (
        await session.execute(
            sa.select(users.c.id).where(
                users.c.tenant_id == tenant_id,
                users.c.issuer == DELETED_ACCOUNT_ISSUER,
                users.c.subject == DELETED_ACCOUNT_SUBJECT,
            )
        )
    ).scalar_one()
