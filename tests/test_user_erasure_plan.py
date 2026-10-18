"""The erasure plan is checked against the schema, not against itself.

An erasure that misses a table does not fail: it reports success and
leaves the person's data behind, which is the worst possible outcome for
this particular feature.  The schema is therefore the authority here --
every column in the ORM that names a user must carry exactly one
disposition, so a table added later turns this gate red instead of being
quietly skipped.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
import sqlalchemy as sa

import inqtrix.storage as storage_package
from inqtrix.services.user_erasure import (
    ERASURE_PLAN,
    Disposition,
    PLANNED_COLUMNS,
    delegated_aggregates,
    rule_for,
    rules_with,
)

#: Columns whose name says "user" but that hold something else.
_NOT_A_USER_REFERENCE: frozenset[tuple[str, str]] = frozenset()


def _schema_user_columns() -> set[tuple[str, str]]:
    """Every ``(table, column)`` in the ORM that names a user."""
    for module in sorted(
        info.name
        for info in pkgutil.iter_modules(storage_package.__path__)
        if info.name.endswith("_orm")
    ):
        importlib.import_module(f"inqtrix.storage.{module}")

    tables: dict[str, sa.Table] = {}
    for module in sorted(
        info.name
        for info in pkgutil.iter_modules(storage_package.__path__)
        if info.name.endswith("_orm")
    ):
        loaded = importlib.import_module(f"inqtrix.storage.{module}")
        for value in vars(loaded).values():
            if isinstance(value, sa.MetaData):
                for name, table in value.tables.items():
                    tables.setdefault(name, table)

    found: set[tuple[str, str]] = set()
    for name, table in tables.items():
        for column in table.columns:
            if "user_id" in column.name or column.name == "owner_key":
                found.add((name, column.name))
    return found - _NOT_A_USER_REFERENCE


def test_the_probe_can_see() -> None:
    """Anchor: a negative result below must mean "none", not "blind"."""
    columns = _schema_user_columns()
    assert len(columns) > 50, columns
    assert ("users", "id") not in columns
    assert ("audit_log", "actor_user_id") in columns


def test_every_user_column_in_the_schema_has_a_disposition() -> None:
    missing = sorted(_schema_user_columns() - PLANNED_COLUMNS)
    assert missing == [], (
        "these columns name a user and the erasure plan does not say what "
        "happens to them, so an erasure would silently leave them behind: "
        + ", ".join(f"{table}.{column}" for table, column in missing)
    )


def test_the_plan_names_no_column_the_schema_does_not_have() -> None:
    stale = sorted(PLANNED_COLUMNS - _schema_user_columns())
    assert stale == [], (
        "the plan still disposes of columns that no longer exist: "
        + ", ".join(f"{table}.{column}" for table, column in stale)
    )


def test_each_column_is_disposed_of_exactly_once() -> None:
    seen: dict[tuple[str, str], int] = {}
    for rule in ERASURE_PLAN:
        seen[(rule.table, rule.column)] = seen.get((rule.table, rule.column), 0) + 1
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    assert duplicates == [], duplicates


def test_every_rule_states_a_reason_a_reader_can_check() -> None:
    thin = [
        f"{rule.table}.{rule.column}"
        for rule in ERASURE_PLAN
        if len(rule.reason.strip()) < 25
    ]
    assert thin == [], thin


def test_delegated_rules_name_the_aggregate_that_owns_them() -> None:
    """A delegated column without an aggregate delegates to nothing."""
    orphaned = [
        f"{rule.table}.{rule.column}"
        for rule in rules_with(Disposition.DELEGATE)
        if not rule.aggregate
    ]
    assert orphaned == []
    assert delegated_aggregates() == (
        "section",
        "source",
        "knowledge_collection",
        "vector_index",
        "agent_session",
        "knowledge_session",
        "run",
    )


def test_every_delegated_aggregate_has_a_surface_that_deletes_it() -> None:
    """A delegated aggregate nobody deletes is a residual on every erasure."""
    from inqtrix.services import user_erasure_aggregates as surfaces

    handled = {
        surfaces.AGENT_SESSION,
        surfaces.KNOWLEDGE_SESSION,
        surfaces.RUN,
        surfaces.SECTION,
        surfaces.SOURCE,
        surfaces.KNOWLEDGE_DOCUMENT,
        surfaces.KNOWLEDGE_COLLECTION,
        surfaces.VECTOR_INDEX,
    }
    missing = sorted(set(delegated_aggregates()) - handled)
    assert missing == [], missing


def test_only_the_deletion_machines_own_tombstones_are_left_in_place() -> None:
    """A row a rule leaves out is a row no erasure ever looks at again.

    The deletion machine ends every source deletion by keeping the source's
    lifecycle row as a tombstone, so a late request cannot resurrect it.
    That record is the machine's, not the account's data; anything else a
    rule left out would be data an erasure silently keeps.
    """
    from inqtrix.source_authority import SourceLifecycleState

    kept = sorted(
        (rule.table, rule.column, rule.tombstone)
        for rule in ERASURE_PLAN
        if rule.tombstone is not None
    )
    assert kept == [
        ("source_lifecycles", "owner_key", ("state", SourceLifecycleState.DELETED.value)),
        ("source_lifecycles", "owner_user_id", ("state", SourceLifecycleState.DELETED.value)),
    ]


def test_only_the_columns_a_check_binds_to_a_human_are_reassigned() -> None:
    """Every other reference to the account is cleared, not re-pointed."""
    reassigned = sorted(
        (rule.table, rule.column) for rule in rules_with(Disposition.REASSIGN)
    )
    assert reassigned == [
        ("editor_collaboration_comment_threads", "resolved_by_user_id"),
        ("editor_collaboration_updates", "actor_user_id"),
    ]


@pytest.mark.parametrize(
    ("table", "column", "disposition"),
    [
        # The two append-only tables: the application holds INSERT/SELECT
        # only, so any plan that plans to delete them plans a privilege
        # escalation.
        ("audit_log", "actor_user_id", Disposition.RETAIN),
        ("llm_usage", "user_id", Disposition.RETAIN),
        # A workspace other people work in cannot lose its creator.
        ("workspaces", "created_by_user_id", Disposition.REFUSE),
        # Acting on somebody else's row never makes the row theirs.
        ("quota_limits", "set_by_user_id", Disposition.CLEAR_REFERENCE),
        ("run_approvals", "decided_by_user_id", Disposition.CLEAR_REFERENCE),
        ("editor_patches", "decided_by_user_id", Disposition.CLEAR_REFERENCE),
        ("runs", "execution_actor_user_id", Disposition.CLEAR_REFERENCE),
        # The change chain of a colleague's document must stay contiguous,
        # and a human edit or a resolution must name a human.
        (
            "editor_collaboration_updates",
            "actor_user_id",
            Disposition.REASSIGN,
        ),
        (
            "editor_collaboration_comment_threads",
            "resolved_by_user_id",
            Disposition.REASSIGN,
        ),
        # Blobs, vectors and checkpoints are only reachable through their
        # rows.
        ("files", "owner_user_id", Disposition.DELEGATE),
        ("knowledge_collections", "created_by_user_id", Disposition.DELEGATE),
        ("vector_index_records", "created_by_user_id", Disposition.DELEGATE),
        ("runs", "created_by_user_id", Disposition.DELEGATE),
    ],
)
def test_the_dispositions_that_carry_the_reasoning(
    table: str, column: str, disposition: Disposition
) -> None:
    rule = rule_for(table, column)
    assert rule is not None, f"{table}.{column} has no rule"
    assert rule.disposition is disposition, (
        f"{table}.{column} is {rule.disposition}, expected {disposition}"
    )


def test_delegated_aggregates_come_before_the_rows_they_need() -> None:
    """The rows are the map to the blobs, so the map is read first."""
    order = [rule.disposition for rule in ERASURE_PLAN]
    last_delegate = max(
        index for index, value in enumerate(order)
        if value is Disposition.DELEGATE
    )
    first_retain = min(
        index for index, value in enumerate(order)
        if value is Disposition.RETAIN
    )
    assert last_delegate < first_retain


def test_the_delete_order_covers_every_row_the_plan_deletes() -> None:
    """A table the plan deletes but the order forgets is never deleted.

    The plan says WHAT happens to each column; the executor's order says
    WHEN. A column present in one and missing from the other is the exact
    shape of an erasure that reports success and leaves rows behind.
    """
    from inqtrix.services.user_erasure_executor import DELETE_ORDER

    owed = {
        (rule.table, rule.column)
        for rule in ERASURE_PLAN
        if rule.disposition is Disposition.DELETE_ROW
    }
    missing = sorted(owed - set(DELETE_ORDER))
    assert missing == [], (
        "the plan deletes these and the executor never reaches them: "
        + ", ".join(f"{table}.{column}" for table, column in missing)
    )


def test_the_delete_order_deletes_nothing_the_plan_keeps() -> None:
    from inqtrix.services.user_erasure_executor import DELETE_ORDER

    stale = sorted(
        entry
        for entry in DELETE_ORDER
        if (rule := rule_for(*entry)) is None
        or rule.disposition is not Disposition.DELETE_ROW
    )
    assert stale == [], (
        "the executor deletes rows the plan does not assign to it -- a "
        "delegated row deleted here would strand the data outside "
        "PostgreSQL that only its surface can reach: "
        + ", ".join(f"{table}.{column}" for table, column in stale)
    )


def test_documents_go_before_the_sweeps_in_other_peoples_documents() -> None:
    """The document is the cascade root for nine child tables, so it goes
    before the explicit sweeps that catch rows in OTHER people's documents.
    """
    from inqtrix.services.user_erasure_executor import DELETE_ORDER

    order = [entry[0] for entry in DELETE_ORDER]
    assert order.index("editor_documents") < order.index(
        "editor_collaboration_comment_messages"
    )
