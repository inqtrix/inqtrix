"""The revision that stores the last report profile and Stufe, read without a database.

The Postgres proof of the same revision runs in
``test_account_last_tiers_migration_postgres.py``; these checks pin what the
revision itself executes, so they hold in every offline run.
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa

from inqtrix.storage.account_orm import account_preferences

REVISION = "0102_account_last_tiers"
PREVIOUS = "0101_retire_mission_skill_tools"
# Written out, not read from the code under test: the revision freezes the
# vocabularies it was written for.
DOMAINS = {
    "last_research_profile": ("", "schnell", "compact", "deep"),
    "last_agent_tier": ("", "schnell", "gruendlich", "tief"),
}


def _migration():
    return importlib.import_module(f"inqtrix.storage.migrations.versions.{REVISION}")


def _executed(monkeypatch, step: str) -> list[str]:
    migration = _migration()
    executed: list[str] = []
    monkeypatch.setattr(
        migration.op, "execute", lambda statement: executed.append(" ".join(str(statement).split())),
    )
    getattr(migration, step)()
    return executed


def test_the_revision_follows_the_skill_tool_revision() -> None:
    migration = _migration()
    assert migration.revision == REVISION
    assert migration.down_revision == PREVIOUS


def test_the_upgrade_adds_both_columns_without_a_pick_and_with_their_exact_domains(
    monkeypatch,
) -> None:
    statements = "\n".join(_executed(monkeypatch, "upgrade"))

    for column, values in DOMAINS.items():
        assert (
            f"ALTER TABLE account_preferences ADD COLUMN IF NOT EXISTS {column} "
            "text NOT NULL DEFAULT ''"
        ) in statements
        literals = ", ".join(f"'{value}'" for value in values)
        assert (
            f"ADD CONSTRAINT ck_account_preferences_{column} "
            f"CHECK ({column} IN ({literals}))"
        ) in statements
        # The lookup that keeps the rule from being added twice is bound to
        # this table: a rule of the same name on the table of another schema
        # in the same database must not stand in for it.
        assert (
            f"WHERE conname = 'ck_account_preferences_{column}' "
            "AND conrelid = 'account_preferences'::regclass"
        ) in statements


def test_the_downgrade_drops_the_rules_and_then_the_columns(monkeypatch) -> None:
    assert _executed(monkeypatch, "downgrade") == [
        statement
        for column in DOMAINS
        for statement in (
            f"ALTER TABLE account_preferences DROP CONSTRAINT IF EXISTS ck_account_preferences_{column}",
            f"ALTER TABLE account_preferences DROP COLUMN IF EXISTS {column}",
        )
    ]


def test_the_table_declares_both_columns_as_the_revision_adds_them() -> None:
    for column in DOMAINS:
        declared = account_preferences.c[column]
        assert isinstance(declared.type, sa.Text), column
        assert declared.nullable is False, column
        assert str(declared.server_default.arg) == "''", column
