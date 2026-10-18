"""Count, before anything is destroyed, what an erasure would reach.

An irreversible action has to be legible at the moment it is taken, not
only in the documentation that explains it afterwards.  This module
answers one question against the live database -- "if this account went,
what exactly would go with it?" -- by walking
:data:`~inqtrix.services.user_erasure.ERASURE_PLAN` and counting each
rule, so the number the administrator confirms and the work the erasure
performs come from the SAME definition and cannot drift apart.

It reads and counts.  It never writes.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from inqtrix.services.user_erasure import (
    ERASURE_PLAN,
    Disposition,
    ErasureRule,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

_TABLES: dict[str, sa.Table] | None = None


def _tables() -> dict[str, sa.Table]:
    """Every ORM table, keyed by name.

    The storage layer keeps one ``MetaData`` per module rather than one
    global registry, so the lookup collects them once and caches the
    result; a plan rule names a table by string precisely so this module
    does not have to import all of them by hand.
    """
    global _TABLES
    if _TABLES is not None:
        return _TABLES
    import inqtrix.storage as storage_package

    collected: dict[str, sa.Table] = {}
    for info in sorted(
        pkgutil.iter_modules(storage_package.__path__), key=lambda i: i.name
    ):
        if not info.name.endswith("_orm"):
            continue
        module = importlib.import_module(f"inqtrix.storage.{info.name}")
        for value in vars(module).values():
            if isinstance(value, sa.MetaData):
                for name, table in value.tables.items():
                    collected.setdefault(name, table)
    _TABLES = collected
    return collected


@dataclass(frozen=True)
class InventoryLine:
    """One plan rule with the number of rows it currently matches."""

    table: str
    column: str
    disposition: Disposition
    reason: str
    rows: int
    aggregate: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "disposition": self.disposition.value,
            "reason": self.reason,
            "rows": self.rows,
        }
        if self.aggregate:
            payload["aggregate"] = self.aggregate
        return payload


@dataclass(frozen=True)
class ErasureInventory:
    """What an erasure of one account would reach, counted."""

    user_id: uuid.UUID
    lines: tuple[InventoryLine, ...]
    #: Rows that belong to OTHER people and carry this user's work inside
    #: them; the number the confirmation has to show, because it is the
    #: part an administrator cannot undo for somebody else.
    foreign_rows: int = 0
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def deleted_rows(self) -> int:
        return sum(
            line.rows
            for line in self.lines
            if line.disposition is Disposition.DELETE_ROW
        )

    @property
    def cleared_rows(self) -> int:
        return sum(
            line.rows
            for line in self.lines
            if line.disposition is Disposition.CLEAR_REFERENCE
        )

    @property
    def delegated_rows(self) -> int:
        return sum(
            line.rows
            for line in self.lines
            if line.disposition is Disposition.DELEGATE
        )

    @property
    def reassigned_rows(self) -> int:
        return sum(
            line.rows
            for line in self.lines
            if line.disposition is Disposition.REASSIGN
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "deleted_rows": self.deleted_rows,
            "cleared_rows": self.cleared_rows,
            "delegated_rows": self.delegated_rows,
            "reassigned_rows": self.reassigned_rows,
            "foreign_rows": self.foreign_rows,
            "blockers": list(self.blockers),
            "lines": [
                line.to_payload() for line in self.lines if line.rows > 0
            ],
        }


def _match(table: sa.Table, rule: ErasureRule, user_id: uuid.UUID) -> Any:
    """The WHERE clause that selects the rows one rule governs."""
    clause = _names_the_user(table, rule, user_id)
    if rule.tombstone is None:
        return clause
    # The deletion machine's own record of a finished deletion stays on
    # purpose; it is neither the account's data nor a residual.
    column, state = rule.tombstone
    return sa.and_(clause, table.c[column] != state)


def _names_the_user(table: sa.Table, rule: ErasureRule, user_id: uuid.UUID) -> Any:
    """The rows whose ``rule.column`` points at the account."""
    column = table.c[rule.column]
    if rule.column == "mention_user_ids":
        # A JSONB array of ids; `@>` asks whether the array CONTAINS this
        # one and uses the GIN index the schema already carries.
        #
        # The right-hand side must be a JSON ARRAY. Casting the rendered
        # text `["<id>"]` to JSONB does not produce one -- it produces a
        # JSON *string* whose characters happen to look like an array, and
        # an array never contains a string, so the query would answer 0 for
        # every user while looking perfectly correct. Binding a Python list
        # is what serialises to an array.
        return column.op("@>")(
            sa.literal([str(user_id)], postgresql.JSONB)
        )
    if rule.column == "owner_key":
        # The object-store prefix IS the user id, stored as text.
        return column == str(user_id)
    return column == user_id


async def collect_inventory(
    session: "AsyncSession",
    *,
    user_id: uuid.UUID,
) -> ErasureInventory:
    """Count every plan rule against the live rows of one account.

    Rules of disposition ``RETAIN`` are counted too but reported with
    their own disposition: the administrator should see that the audit
    trail keeps its rows, rather than discover it afterwards.
    """
    tables = _tables()
    lines: list[InventoryLine] = []

    for rule in ERASURE_PLAN:
        table = tables.get(rule.table)
        if table is None or rule.column not in table.c:
            # The plan gate keeps this impossible; counting zero rather
            # than raising keeps a preview useful on a partial schema.
            continue
        lines.append(
            InventoryLine(
                table=rule.table,
                column=rule.column,
                disposition=rule.disposition,
                reason=rule.reason,
                rows=await _count(session, table, rule, user_id),
                aggregate=rule.aggregate,
            )
        )

    foreign = await _foreign_content_rows(session, user_id=user_id)
    return ErasureInventory(
        user_id=user_id,
        lines=tuple(lines),
        foreign_rows=foreign,
        blockers=await erasure_blockers(session, user_id=user_id),
    )


async def erasure_blockers(
    session: "AsyncSession",
    *,
    user_id: uuid.UUID,
) -> tuple[str, ...]:
    """Why an erasure of this account cannot start, or nothing.

    Only the plan's ``REFUSE`` rules block: everything else has a defined
    move -- delete, delegate, reassign, clear or retain. The preview and
    the start of an erasure both ask THIS function, so a confirmation that
    shows no blocker never runs into a refusal it did not show.
    """
    tables = _tables()
    reasons: list[str] = []
    for rule in ERASURE_PLAN:
        if rule.disposition is not Disposition.REFUSE:
            continue
        table = tables.get(rule.table)
        if table is None or rule.column not in table.c:
            continue
        count = await _count(session, table, rule, user_id)
        if count:
            reasons.append(
                f"{count} row(s) in {rule.table}.{rule.column}: {rule.reason}"
            )
    return tuple(reasons)


async def _count(
    session: "AsyncSession",
    table: sa.Table,
    rule: ErasureRule,
    user_id: uuid.UUID,
) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(table)
                .where(_match(table, rule, user_id))
            )
        ).scalar()
        or 0
    )


async def _foreign_content_rows(
    session: "AsyncSession",
    *,
    user_id: uuid.UUID,
) -> int:
    """Rows other people wrote that this erasure would take with it.

    Comment messages hang off the DOCUMENT, not off their author, so
    deleting a document deletes every message inside it -- including the
    ones colleagues wrote. That count is the single most important number
    on the confirmation, because it is the only loss the administrator
    inflicts on somebody who was never asked.
    """
    tables = _tables()
    documents = tables.get("editor_documents")
    threads = tables.get("editor_collaboration_comment_threads")
    messages = tables.get("editor_collaboration_comment_messages")
    if documents is None or threads is None or messages is None:
        return 0
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(
                    messages.join(threads, threads.c.id == messages.c.thread_id)
                    .join(documents, documents.c.id == threads.c.document_id)
                )
                .where(
                    documents.c.created_by_user_id == user_id,
                    messages.c.author_user_id.is_not(None),
                    messages.c.author_user_id != user_id,
                )
            )
        ).scalar()
        or 0
    )
