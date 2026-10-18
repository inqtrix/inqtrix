"""Title ownership and revision rules shared by chat and agent sessions.

Activity timestamps are unrelated to title revisions. Automatic generation
can replace only an initial title at the revision it read. Manual edits win
over that generation; stale metadata echoes cannot restore an older title.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, case, or_, select, update

from inqtrix.project.scoped_upsert import ResourceScope

TitleSource = Literal["initial", "generated", "manual"]


class SessionTitleConflict(ValueError):
    """An unversioned client tried to overwrite a protected title."""


def legacy_title_condition(table: Any, title: Any, revision: int | None):
    if revision is not None:
        return True
    return or_(
        table.c.title == title,
        and_(table.c.title_revision == 0, table.c.title_source == "manual"),
    )


async def check_legacy_title_conflict(
    session, table, resource_id, scope, title, *, tenant_id
):
    """Distinguish a protected-title miss from an ownership miss after CAS."""
    conditions = [
        table.c.id == resource_id,
        table.c.tenant_id == tenant_id,
        table.c.created_by_user_id.is_not_distinct_from(scope.created_by_user_id),
        table.c.workspace_id.is_not_distinct_from(scope.workspace_id),
    ]
    if "lifecycle_status" in table.c:
        conditions.append(table.c.lifecycle_status == "active")
    current = (await session.execute(select(table).where(*conditions))).first()
    if current is not None:
        title_after_save(current, title=title, revision=None, source="manual")


def parse_title_intent(body: dict[str, Any]) -> tuple[int | None, TitleSource]:
    """Validate optional revision metadata; absent fields identify old clients."""
    revision = body.get("title_revision")
    source = body.get("title_source", "manual")
    if revision is not None and (
        type(revision) is not int or not 0 <= revision <= 2_147_483_647
    ):
        raise ValueError("title_revision must be a non-negative integer")
    if source not in ("initial", "generated", "manual"):
        raise ValueError("title_source must be initial, generated or manual")
    if "title_source" in body and revision is None:
        raise ValueError("title_source requires title_revision")
    return revision, source


@dataclass(frozen=True)
class TitleState:
    title: str
    title_revision: int = 0
    title_source: TitleSource = "manual"


def title_after_save(
    current: Any,
    *,
    title: str,
    revision: int | None,
    source: TitleSource,
) -> TitleState:
    """Apply the metadata writer's title intent without trusting its clock.

    Unversioned clients retain their title writes only on untouched legacy
    rows. Once a versioned title changes, those clients must reload before
    renaming. Echoes of the current title remain harmless.
    """
    old = TitleState(current.title, current.title_revision, current.title_source)
    if revision is None:
        if (
            old.title_revision == 0
            and old.title_source == "manual"
            and title != old.title
        ):
            return TitleState(title, 1, "manual")
        if title != old.title:
            raise SessionTitleConflict(
                "The title changed. Reload this client before renaming it."
            )
        return old
    allowed = (
        source == "manual"
        and (revision == old.title_revision or old.title_source != "manual")
    ) or (
        source == "initial"
        and old.title_source == "initial"
        and revision == old.title_revision
    )
    if not allowed or (title == old.title and source == old.title_source):
        return old
    return TitleState(title, old.title_revision + 1, source)


def title_update_columns(
    table: Any, *, title: Any, revision: int | None, source: TitleSource
) -> dict[str, Any]:
    """SQL equivalent of :func:`title_after_save`, evaluated under the row lock.

    The increment is part of the same UPDATE as the new title. It therefore
    follows committed writes to this row, not sequence allocation or client
    wall clocks. Other metadata continues to use its existing write contract.
    """
    if revision is None:
        legacy = and_(
            table.c.title_revision == 0,
            table.c.title_source == "manual",
            table.c.title != title,
        )
        return {
            "title": case((legacy, title), else_=table.c.title),
            "title_revision": case(
                (legacy, table.c.title_revision + 1), else_=table.c.title_revision
            ),
            "title_source": table.c.title_source,
        }
    allowed = or_(
        and_(
            source == "manual",
            or_(table.c.title_revision == revision, table.c.title_source != "manual"),
        ),
        and_(
            source == "initial",
            table.c.title_source == "initial",
            table.c.title_revision == revision,
        ),
    )
    changed = and_(allowed, or_(table.c.title != title, table.c.title_source != source))
    return {
        "title": case((changed, title), else_=table.c.title),
        "title_revision": case(
            (changed, table.c.title_revision + 1), else_=table.c.title_revision
        ),
        "title_source": case((changed, source), else_=table.c.title_source),
    }


def generated_title_update(
    table: Any,
    *,
    resource_id: str,
    tenant_id: str,
    scope: ResourceScope,
    title: str,
    revision: int,
):
    """Compare-and-set a generated title; never create or move a resource."""
    conditions = [
        table.c.id == resource_id,
        table.c.tenant_id == tenant_id,
        table.c.created_by_user_id.is_not_distinct_from(scope.created_by_user_id),
        table.c.workspace_id.is_not_distinct_from(scope.workspace_id),
        table.c.title_revision == revision,
        table.c.title_source == "initial",
    ]
    if "lifecycle_status" in table.c:
        conditions.append(table.c.lifecycle_status == "active")
    return (
        update(table)
        .where(*conditions)
        .values(
            title=title,
            title_source="generated",
            title_revision=table.c.title_revision + 1,
        )
        .returning(table.c.id)
    )
