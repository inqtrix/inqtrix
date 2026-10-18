"""Postgres implementation of the prompt-template repository.

Same conventions as the file registry: every operation runs in one
tenant-scoped transaction under the restricted app role, RLS is the
second defense behind the explicit ``tenant_id`` predicates, and
absence equals foreign-tenant invisibility
(:class:`PromptTemplateNotFound` for both).
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any

from collections.abc import Sequence

from sqlalchemy import Text, any_, bindparam, delete, insert, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.content.prompt_templates import (
    PromptTemplateConflict,
    PromptTemplateNotFound,
    PromptTemplateRecord,
)
from inqtrix.storage.db import tenant_session
from inqtrix.storage.prompt_template_orm import (
    prompt_template_seed_markers,
    prompt_templates,
)
from inqtrix.storage.resource_access import (
    VISIBLE_SHARE_PERMISSION,
    append_resource_effects,
    listed_resource_access,
    lock_active_users,
    lock_resource_access,
    revoke_resource_shares,
    visible_resource_select,
)
from inqtrix.auth.permissions import ResourceAccess, SharePermission
from inqtrix.auth.shares import ShareFacts

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _record_from_row(row: Any) -> PromptTemplateRecord:
    return PromptTemplateRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        title=row.title,
        label=row.label,
        category=row.category,
        content_markdown=row.content_markdown,
        visibility=dict(row.visibility or {}),
        include_in_autocomplete=row.include_in_autocomplete,
        revision=int(row.revision),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresPromptTemplateRepository:
    """Prompt templates over the ``prompt_templates`` table.

    Args:
        session_factory: Factory from
            :func:`inqtrix.storage.db.build_session_factory`.
        app_role: Restricted Postgres role for the tenant sessions.
    """

    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        app_role: str,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._app_role = app_role
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled

    @property
    def atomic_resource_effects(self) -> bool:
        """Whether mutations include audit, shares, and invalidations."""
        return True

    def _session(
        self, tenant_id: str
    ) -> "AbstractAsyncContextManager[AsyncSession]":
        return tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        )

    async def create(self, record: PromptTemplateRecord) -> PromptTemplateRecord:
        async with self._session(record.tenant_id) as session:
            if record.owner_user_id is not None and not await lock_active_users(
                session,
                tenant_id=record.tenant_id,
                user_ids=(record.owner_user_id,),
            ):
                raise PromptTemplateNotFound(record.id)
            await session.execute(
                insert(prompt_templates).values(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    owner_user_id=record.owner_user_id,
                    title=record.title,
                    label=record.label,
                    category=record.category,
                    content_markdown=record.content_markdown,
                    visibility=record.visibility,
                    include_in_autocomplete=record.include_in_autocomplete,
                    revision=record.revision,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            await append_resource_effects(
                session,
                tenant_id=record.tenant_id,
                actor_user_id=record.owner_user_id,
                owner_user_id=record.owner_user_id,
                action="prompt_template.created",
                resource_type="prompt_template",
                resource_id=record.id,
                scope="prompt_templates",
            )
        return record

    async def seed_default_templates(
        self,
        records: "Sequence[PromptTemplateRecord]",
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> bool:
        """Claim the seed marker and insert *records* in ONE transaction.

        The ``ON CONFLICT DO NOTHING`` insert of the marker row is the
        serialization point: exactly one of any concurrent first
        listings claims it; a crash rolls marker AND rows back together.
        A user who already owns templates (client-sync push races the
        first listing) gets the marker without rows — stock content
        never injects into a grown library.
        """
        now = time.time()
        async with self._session(tenant_id) as session:
            claimed = await session.execute(
                pg_insert(prompt_template_seed_markers)
                .values(
                    tenant_id=tenant_id, user_id=user_id, seeded_at=now
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "user_id"]
                )
            )
            if not claimed.rowcount:
                return False
            # Same active-owner guard as create(): a user disabled in
            # this instant must not gain rows.
            if not await lock_active_users(
                session, tenant_id=tenant_id, user_ids=(user_id,)
            ):
                raise PromptTemplateNotFound("seed")
            owns_any = await session.scalar(
                select(prompt_templates.c.id)
                .where(
                    prompt_templates.c.tenant_id == tenant_id,
                    prompt_templates.c.owner_user_id == user_id,
                )
                .limit(1)
            )
            if owns_any is not None:
                return False
            for record in records:
                await session.execute(
                    insert(prompt_templates).values(
                        id=record.id,
                        tenant_id=record.tenant_id,
                        owner_user_id=record.owner_user_id,
                        title=record.title,
                        label=record.label,
                        category=record.category,
                        content_markdown=record.content_markdown,
                        visibility=record.visibility,
                        include_in_autocomplete=(
                            record.include_in_autocomplete
                        ),
                        revision=record.revision,
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                    )
                )
        return True

    async def get(
        self, template_id: str, *, tenant_id: str
    ) -> PromptTemplateRecord:
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(prompt_templates).where(
                        prompt_templates.c.tenant_id == tenant_id,
                        prompt_templates.c.id == template_id,
                    )
                )
            ).one_or_none()
        if row is None:
            raise PromptTemplateNotFound(template_id)
        return _record_from_row(row)

    async def list_for_tenant(
        self, *, tenant_id: str
    ) -> list[PromptTemplateRecord]:
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(prompt_templates)
                    .where(prompt_templates.c.tenant_id == tenant_id)
                    .order_by(prompt_templates.c.created_at.desc())
                )
            ).all()
        return [_record_from_row(row) for row in rows]

    async def share_facts(
        self, template_ids: Sequence[str], *, tenant_id: str
    ) -> dict[str, ShareFacts]:
        wanted = sorted(set(template_ids))
        if not wanted:
            return {}
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(
                        prompt_templates.c.id,
                        prompt_templates.c.owner_user_id,
                        prompt_templates.c.title,
                    ).where(
                        prompt_templates.c.tenant_id == tenant_id,
                        prompt_templates.c.id
                        == any_(bindparam("template_ids", wanted, type_=ARRAY(Text))),
                    )
                )
            ).all()
        return {
            row.id: ShareFacts(owner_user_id=row.owner_user_id, title=row.title)
            for row in rows
        }

    async def list_visible_for_user(
        self,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> list[tuple[PromptTemplateRecord, ResourceAccess]]:
        """List owned and accepted-shared templates in one live SQL query."""
        statement = visible_resource_select(
            resource_table=prompt_templates,
            id_column=prompt_templates.c.id,
            owner_column=prompt_templates.c.owner_user_id,
            resource_type="prompt_template",
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        ).order_by(prompt_templates.c.created_at.desc())
        async with self._session(tenant_id) as session:
            rows = (await session.execute(statement)).all()
        return [
            (
                _record_from_row(row),
                listed_resource_access(
                    owner_user_id=row.owner_user_id,
                    actor_user_id=actor_user_id,
                    share_permission=getattr(row, VISIBLE_SHARE_PERMISSION),
                ),
            )
            for row in rows
        ]

    async def update(
        self,
        record: PromptTemplateRecord,
        *,
        expected_revision: int,
        actor_user_id: uuid.UUID | None,
    ) -> PromptTemplateRecord:
        now = time.time()
        async with self._session(record.tenant_id) as session:
            access = await lock_resource_access(
                session,
                tenant_id=record.tenant_id,
                actor_user_id=actor_user_id,
                resource_type="prompt_template",
                resource_table=prompt_templates,
                id_column=prompt_templates.c.id,
                resource_id=record.id,
                owner_column=prompt_templates.c.owner_user_id,
                minimum=SharePermission.EDIT,
                restrict_to_workspace_members=(
                    self._restrict_to_workspace_members
                ),
                sharing_enabled=self._sharing_enabled,
            )
            if access is None:
                raise PromptTemplateNotFound(record.id)
            where = [
                prompt_templates.c.tenant_id == record.tenant_id,
                prompt_templates.c.id == record.id,
                prompt_templates.c.revision == expected_revision,
            ]
            row = (
                await session.execute(
                    update(prompt_templates)
                    .where(*where)
                    .values(
                        title=record.title,
                        label=record.label,
                        category=record.category,
                        content_markdown=record.content_markdown,
                        visibility=record.visibility,
                        include_in_autocomplete=record.include_in_autocomplete,
                        revision=expected_revision + 1,
                        updated_at=now,
                    )
                    .returning(prompt_templates)
                )
            ).one_or_none()
            if row is not None:
                stored = _record_from_row(row)
                await append_resource_effects(
                    session,
                    tenant_id=record.tenant_id,
                    actor_user_id=actor_user_id,
                    owner_user_id=access.owner_user_id,
                    action="prompt_template.updated",
                    resource_type="prompt_template",
                    resource_id=record.id,
                    scope="prompt_templates",
                )
                return stored
            current_revision = await session.scalar(
                select(prompt_templates.c.revision).where(
                    prompt_templates.c.tenant_id == record.tenant_id,
                    prompt_templates.c.id == record.id,
                )
            )
            if current_revision is not None:
                raise PromptTemplateConflict(
                    record.id, int(current_revision)
                )
        raise PromptTemplateNotFound(record.id)

    async def delete(
        self,
        template_id: str,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        async with self._session(tenant_id) as session:
            access = await lock_resource_access(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                resource_type="prompt_template",
                resource_table=prompt_templates,
                id_column=prompt_templates.c.id,
                resource_id=template_id,
                owner_column=prompt_templates.c.owner_user_id,
                minimum=SharePermission.VIEW,
                restrict_to_workspace_members=(
                    self._restrict_to_workspace_members
                ),
                sharing_enabled=self._sharing_enabled,
                owner_only=True,
            )
            if access is None:
                raise PromptTemplateNotFound(template_id)
            recipients = await revoke_resource_shares(
                session,
                tenant_id=tenant_id,
                resource_type="prompt_template",
                resource_id=template_id,
                revoked_by_user_id=actor_user_id,
            )
            result = await session.execute(
                delete(prompt_templates).where(
                    prompt_templates.c.tenant_id == tenant_id,
                    prompt_templates.c.id == template_id,
                )
            )
            if result.rowcount:
                await append_resource_effects(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    owner_user_id=access.owner_user_id,
                    action="prompt_template.deleted",
                    resource_type="prompt_template",
                    resource_id=template_id,
                    scope="prompt_templates",
                    additional_targets=recipients,
                )
        if not result.rowcount:
            raise PromptTemplateNotFound(template_id)
