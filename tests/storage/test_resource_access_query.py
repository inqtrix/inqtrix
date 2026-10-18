"""Static security contract for PostgreSQL owned/shared list queries."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql

from inqtrix.auth.permissions import AccessMode, SharePermission
from inqtrix.storage.editor_orm import editor_documents
from inqtrix.storage.identity_orm import resource_shares
from inqtrix.storage.prompt_template_orm import prompt_templates
from inqtrix.storage.resource_access import (
    LockedResourceAccess,
    lock_active_users,
    lock_resource_access,
    listed_resource_access,
    visible_resource_select,
)


ACTOR_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _sql(*, actor_user_id: uuid.UUID | None, restrict: bool) -> str:
    statement = visible_resource_select(
        resource_table=prompt_templates,
        id_column=prompt_templates.c.id,
        owner_column=prompt_templates.c.owner_user_id,
        resource_type="prompt_template",
        tenant_id="default",
        actor_user_id=actor_user_id,
        restrict_to_workspace_members=restrict,
    )
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _editor_sql() -> str:
    statement = visible_resource_select(
        resource_table=editor_documents,
        id_column=editor_documents.c.id,
        owner_column=editor_documents.c.created_by_user_id,
        resource_type="editor_document",
        tenant_id="default",
        actor_user_id=ACTOR_ID,
        restrict_to_workspace_members=False,
    )
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_scoped_list_query_is_one_live_owner_or_accepted_share_decision() -> None:
    sql = _sql(actor_user_id=ACTOR_ID, restrict=True)

    assert "LEFT OUTER JOIN resource_shares" in sql
    assert "visible_prompt_template_share.accepted_at IS NOT NULL" in sql
    assert "visible_prompt_template_share.revoked_at IS NULL" in sql
    assert "visible_prompt_template_share.recipient_user_id" in sql
    assert "users.disabled_at IS NULL" in sql
    assert "workspace_members" in sql
    assert "prompt_templates.owner_user_id" in sql


def test_list_queries_filter_permissions_by_resource_type() -> None:
    prompt_sql = _sql(actor_user_id=ACTOR_ID, restrict=False)
    editor_sql = _editor_sql()

    assert "permission IN ('view', 'edit')" in prompt_sql
    assert "permission IN ('view', 'suggest', 'edit')" in editor_sql
    assert "editor_documents.deleted_at IS NULL" in editor_sql


class _AccessResult:
    def __init__(
        self,
        *,
        first: Any = None,
        scalars: tuple[uuid.UUID, ...] = (),
    ) -> None:
        self._first = first
        self._scalars = scalars

    def first(self) -> Any:
        return self._first

    def scalars(self) -> tuple[uuid.UUID, ...]:
        return self._scalars


class _AccessSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self._results = iter(
            (
                _AccessResult(first=("ed_1", ACTOR_ID)),
                _AccessResult(scalars=(ACTOR_ID,)),
                _AccessResult(first=("ed_1", ACTOR_ID)),
            )
        )

    async def execute(self, statement: Any) -> _AccessResult:
        self.statements.append(statement)
        return next(self._results)


@pytest.mark.asyncio
async def test_active_user_authorization_uses_fk_compatible_share_locks() -> None:
    session = _AccessSession()
    session._results = iter((_AccessResult(scalars=(ACTOR_ID,)),))

    assert await lock_active_users(
        cast(Any, session),
        tenant_id="default",
        user_ids=(ACTOR_ID,),
    )
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR SHARE" in sql
    assert "FOR UPDATE" not in sql


@pytest.mark.asyncio
async def test_editor_access_lock_hides_soft_deleted_documents() -> None:
    session = _AccessSession()

    access = await lock_resource_access(
        cast(Any, session),
        tenant_id="default",
        actor_user_id=ACTOR_ID,
        resource_type="editor_document",
        resource_table=editor_documents,
        id_column=editor_documents.c.id,
        resource_id="ed_1",
        owner_column=editor_documents.c.created_by_user_id,
        minimum=SharePermission.VIEW,
        restrict_to_workspace_members=False,
    )

    assert access is not None
    resource_statements = (session.statements[0], session.statements[2])
    for statement in resource_statements:
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "editor_documents.deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_locked_share_access_admits_only_what_its_grant_satisfies() -> None:
    """A recipient's locked access carries the grant that admitted it.

    A caller that locked the access for a weaker minimum judges a stronger
    one against that same grant, without a second lock round trip; the
    owner's access satisfies every minimum.
    """
    owner_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    session = _AccessSession()
    session._results = iter(
        (
            _AccessResult(first=("ed_1", owner_id)),
            _AccessResult(scalars=(ACTOR_ID,)),
            _AccessResult(first=("ed_1", owner_id)),
            _AccessResult(
                first=SimpleNamespace(id=uuid.uuid4(), permission="suggest")
            ),
        )
    )

    access = await lock_resource_access(
        cast(Any, session),
        tenant_id="default",
        actor_user_id=ACTOR_ID,
        resource_type="editor_document",
        resource_table=editor_documents,
        id_column=editor_documents.c.id,
        resource_id="ed_1",
        owner_column=editor_documents.c.created_by_user_id,
        minimum=SharePermission.SUGGEST,
        restrict_to_workspace_members=False,
    )

    assert access is not None
    assert access.share_permission is SharePermission.SUGGEST
    assert access.admits("editor_document", SharePermission.SUGGEST)
    assert not access.admits("editor_document", SharePermission.EDIT)
    edit_grant = LockedResourceAccess(
        owner_user_id=owner_id,
        share_permission=SharePermission.EDIT,
    )
    assert edit_grant.admits("editor_document", SharePermission.EDIT)
    owner = LockedResourceAccess(owner_user_id=ACTOR_ID)
    assert owner.admits("editor_document", SharePermission.EDIT)


def test_share_schema_enforces_resource_specific_permissions() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in resource_shares.constraints
        if constraint.name is not None
    }
    permission_rule = constraints["ck_resource_shares_permission"]
    type_rule = constraints["ck_resource_shares_type"]

    for resource_type in (
        "run",
        "knowledge_collection",
        "prompt_template",
        "skill_template",
    ):
        assert (
            f"resource_type = '{resource_type}' AND "
            "permission IN ('view', 'edit')"
        ) in permission_rule
    assert (
        "resource_type = 'editor_document' AND "
        "permission IN ('view', 'suggest', 'edit')"
    ) in permission_rule
    assert "'editor_document'" in type_rule


def test_unscoped_list_query_exposes_only_ownerless_rows() -> None:
    sql = _sql(actor_user_id=None, restrict=False)

    assert "prompt_templates.owner_user_id IS NULL" in sql
    assert "JOIN resource_shares" not in sql


def test_visible_row_access_annotation_preserves_share_permission() -> None:
    owner = listed_resource_access(
        owner_user_id=ACTOR_ID,
        actor_user_id=ACTOR_ID,
        share_permission=None,
    )
    shared = listed_resource_access(
        owner_user_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        actor_user_id=ACTOR_ID,
        share_permission="edit",
    )
    suggested = listed_resource_access(
        owner_user_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        actor_user_id=ACTOR_ID,
        share_permission="suggest",
    )

    assert owner.mode is AccessMode.OWNER
    assert shared.mode is AccessMode.SHARED
    assert shared.permission is SharePermission.EDIT
    assert suggested.mode is AccessMode.SHARED
    assert suggested.permission is SharePermission.SUGGEST
