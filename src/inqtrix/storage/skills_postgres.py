"""Postgres implementation of the skill repository.

Same conventions as the prompt-template repository: every operation
runs in one tenant-scoped transaction under the restricted app role,
RLS is the second defense behind the explicit ``tenant_id`` predicates,
and absence equals foreign-tenant invisibility (:class:`SkillNotFound`
for both). The optimistic-concurrency precondition rides inside the
UPDATE's WHERE (one atomic compare-and-set).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import Text, any_, bindparam, delete, insert, select, update
from sqlalchemy.dialects.postgresql import ARRAY

from inqtrix.auth.permissions import ResourceAccess, SharePermission
from inqtrix.auth.shares import ShareFacts

from inqtrix.content.skills import (
    SkillConflict,
    SkillNotFound,
    SkillRecord,
)
from inqtrix.storage.db import tenant_session
from inqtrix.storage.skill_orm import skill_templates
from inqtrix.storage.resource_access import (
    VISIBLE_SHARE_PERMISSION,
    append_resource_effects,
    listed_resource_access,
    lock_active_users,
    lock_resource_access,
    revoke_resource_shares,
    visible_resource_select,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _record_from_row(row: Any) -> SkillRecord:
    return SkillRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        label=row.label,
        title=row.title,
        description=row.description,
        when_to_use=row.when_to_use,
        instructions_markdown=row.instructions_markdown,
        clarification_points=tuple(row.clarification_points or []),
        deliverable=row.deliverable,
        allowed_tools=tuple(row.allowed_tools or []),
        requires_plan=row.requires_plan,
        invocation=row.invocation,
        argument_hint=row.argument_hint,
        model_tier=row.model_tier,
        effort=row.effort,
        include_in_autocomplete=row.include_in_autocomplete,
        revision=int(row.revision),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _writable_values(record: SkillRecord) -> dict[str, Any]:
    return {
        "label": record.label,
        "title": record.title,
        "description": record.description,
        "when_to_use": record.when_to_use,
        "instructions_markdown": record.instructions_markdown,
        "clarification_points": list(record.clarification_points),
        "deliverable": record.deliverable,
        "allowed_tools": list(record.allowed_tools),
        "requires_plan": record.requires_plan,
        "invocation": record.invocation,
        "argument_hint": record.argument_hint,
        "model_tier": record.model_tier,
        "effort": record.effort,
        "include_in_autocomplete": record.include_in_autocomplete,
    }


class PostgresSkillRepository:
    """Skills over the ``skill_templates`` table.

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

    async def create(self, record: SkillRecord) -> SkillRecord:
        async with self._session(record.tenant_id) as session:
            if record.owner_user_id is not None and not await lock_active_users(
                session,
                tenant_id=record.tenant_id,
                user_ids=(record.owner_user_id,),
            ):
                raise SkillNotFound(record.id)
            await session.execute(
                insert(skill_templates).values(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    owner_user_id=record.owner_user_id,
                    revision=record.revision,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    **_writable_values(record),
                )
            )
            await append_resource_effects(
                session,
                tenant_id=record.tenant_id,
                actor_user_id=record.owner_user_id,
                owner_user_id=record.owner_user_id,
                action="skill_template.created",
                resource_type="skill_template",
                resource_id=record.id,
                scope="skills",
            )
        return record

    async def get(self, skill_id: str, *, tenant_id: str) -> SkillRecord:
        async with self._session(tenant_id) as session:
            row = (
                await session.execute(
                    select(skill_templates).where(
                        skill_templates.c.tenant_id == tenant_id,
                        skill_templates.c.id == skill_id,
                    )
                )
            ).one_or_none()
        if row is None:
            raise SkillNotFound(skill_id)
        return _record_from_row(row)

    async def list_for_tenant(self, *, tenant_id: str) -> list[SkillRecord]:
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(skill_templates)
                    .where(skill_templates.c.tenant_id == tenant_id)
                    .order_by(skill_templates.c.created_at.desc())
                )
            ).all()
        return [_record_from_row(row) for row in rows]

    async def share_facts(
        self, skill_ids: Sequence[str], *, tenant_id: str
    ) -> dict[str, ShareFacts]:
        wanted = sorted(set(skill_ids))
        if not wanted:
            return {}
        async with self._session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(
                        skill_templates.c.id,
                        skill_templates.c.owner_user_id,
                        skill_templates.c.title,
                    ).where(
                        skill_templates.c.tenant_id == tenant_id,
                        skill_templates.c.id
                        == any_(bindparam("skill_ids", wanted, type_=ARRAY(Text))),
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
    ) -> list[tuple[SkillRecord, ResourceAccess]]:
        """List owned and accepted-shared skills in one live SQL query."""
        statement = visible_resource_select(
            resource_table=skill_templates,
            id_column=skill_templates.c.id,
            owner_column=skill_templates.c.owner_user_id,
            resource_type="skill_template",
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        ).order_by(skill_templates.c.created_at.desc())
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
        record: SkillRecord,
        *,
        expected_revision: int,
        actor_user_id: uuid.UUID | None,
    ) -> SkillRecord:
        now = time.time()
        async with self._session(record.tenant_id) as session:
            access = await lock_resource_access(
                session,
                tenant_id=record.tenant_id,
                actor_user_id=actor_user_id,
                resource_type="skill_template",
                resource_table=skill_templates,
                id_column=skill_templates.c.id,
                resource_id=record.id,
                owner_column=skill_templates.c.owner_user_id,
                minimum=SharePermission.EDIT,
                restrict_to_workspace_members=(
                    self._restrict_to_workspace_members
                ),
                sharing_enabled=self._sharing_enabled,
            )
            if access is None:
                raise SkillNotFound(record.id)
            where = [
                skill_templates.c.tenant_id == record.tenant_id,
                skill_templates.c.id == record.id,
                skill_templates.c.revision == expected_revision,
            ]
            row = (
                await session.execute(
                    update(skill_templates)
                    .where(*where)
                    .values(
                        revision=expected_revision + 1,
                        updated_at=now,
                        **_writable_values(record),
                    )
                    .returning(skill_templates)
                )
            ).one_or_none()
            if row is not None:
                stored = _record_from_row(row)
                await append_resource_effects(
                    session,
                    tenant_id=record.tenant_id,
                    actor_user_id=actor_user_id,
                    owner_user_id=access.owner_user_id,
                    action="skill_template.updated",
                    resource_type="skill_template",
                    resource_id=record.id,
                    scope="skills",
                )
                return stored
            current_revision = await session.scalar(
                select(skill_templates.c.revision).where(
                    skill_templates.c.tenant_id == record.tenant_id,
                    skill_templates.c.id == record.id,
                )
            )
            if current_revision is not None:
                raise SkillConflict(record.id, int(current_revision))
        raise SkillNotFound(record.id)

    async def delete(
        self,
        skill_id: str,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        async with self._session(tenant_id) as session:
            access = await lock_resource_access(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                resource_type="skill_template",
                resource_table=skill_templates,
                id_column=skill_templates.c.id,
                resource_id=skill_id,
                owner_column=skill_templates.c.owner_user_id,
                minimum=SharePermission.VIEW,
                restrict_to_workspace_members=(
                    self._restrict_to_workspace_members
                ),
                sharing_enabled=self._sharing_enabled,
                owner_only=True,
            )
            if access is None:
                raise SkillNotFound(skill_id)
            recipients = await revoke_resource_shares(
                session,
                tenant_id=tenant_id,
                resource_type="skill_template",
                resource_id=skill_id,
                revoked_by_user_id=actor_user_id,
            )
            result = await session.execute(
                delete(skill_templates).where(
                    skill_templates.c.tenant_id == tenant_id,
                    skill_templates.c.id == skill_id,
                )
            )
            if result.rowcount:
                await append_resource_effects(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    owner_user_id=access.owner_user_id,
                    action="skill_template.deleted",
                    resource_type="skill_template",
                    resource_id=skill_id,
                    scope="skills",
                    additional_targets=recipients,
                )
        if not result.rowcount:
            raise SkillNotFound(skill_id)
