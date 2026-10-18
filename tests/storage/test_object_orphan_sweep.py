"""Postgres integration tests for the object-store orphan sweep.

Same gating and conventions as the content suite: disposable database via
``INQTRIX_TEST_DATABASE_URL``, registry writes through the restricted app
role, RLS active. The tests are deliberately SYNC on a NullPool engine —
that mirrors the production shape (a worker retention thread without a
running loop, per-call loops via ``run_coro_sync``), where a pooled
engine would crash with cross-loop connection reuse.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import text

from inqtrix.content.ports import FileRecord
from inqtrix.storage.content_orm import files
from inqtrix.storage.content_postgres import PostgresFileRegistry
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.object_orphan_sweep import sweep_orphaned_file_objects
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_USER_ID = canonical_user_id("orphan-sweep-owner")


@pytest.fixture(scope="session", autouse=True)
def sweep_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest.fixture()
def session_factory():
    """NullPool factory seeded synchronously (production thread shape)."""
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    factory = build_session_factory(engine)

    async def _seed() -> None:
        async with factory() as session:
            async with session.begin():
                bypasses = (
                    await session.execute(
                        text(
                            "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).scalar_one()
                if not bypasses:
                    pytest.fail(
                        "INQTRIX_TEST_DATABASE_URL must connect as a "
                        "superuser/BYPASSRLS user (cross-tenant cleanup)."
                    )
                await session.execute(files.delete())
                await ensure_canonical_users(session, (OWNER_USER_ID,))

    asyncio.run(_seed())
    yield factory
    asyncio.run(engine.dispose())


class _RecordingObjectStore:
    """Deterministic inventory double for the sweep contract."""

    def __init__(self, objects: dict[str, float]) -> None:
        self.objects = dict(objects)
        self.deleted: list[str] = []

    def list_keys(self, prefix: str):
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield key, self.objects[key]

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


def _record(file_id: str, tenant_id: str = "default") -> FileRecord:
    return FileRecord(
        id=file_id,
        tenant_id=tenant_id,
        owner_user_id=OWNER_USER_ID,
        workspace_id=None,
        file_name="beleg.pdf",
        content_type="application/pdf",
        size_bytes=42,
        sha256="cd" * 32,
        object_key=f"tenants/{tenant_id}/files/{file_id}",
        created_at=time.time(),
    )


def test_sweep_deletes_only_old_unregistered_objects(
    session_factory, monkeypatch, caplog
) -> None:
    import inqtrix.storage.object_orphan_sweep as sweep_module

    references = []

    def reference(namespace, identifier):
        references.append((namespace, identifier))
        return "res_0123456789abcdef"

    monkeypatch.setattr(sweep_module, "pseudonymous_log_reference", reference)
    registry = PostgresFileRegistry(
        session_factory=session_factory, app_role=APP_ROLE
    )
    live_id = f"fl_{uuid.uuid4().hex}"
    asyncio.run(registry.create(_record(live_id)))

    now = time.time()
    old = now - 7200.0
    young = now - 60.0
    store = _RecordingObjectStore(
        {
            f"tenants/default/files/{live_id}": old,
            "tenants/default/files/fl_orphan_old": old,
            "tenants/default/files/fl_orphan_young": young,
            "tenants/default/other/fl_not_a_file_key": old,
        }
    )

    deleted = sweep_orphaned_file_objects(
        object_store=store,
        session_factory=session_factory,
        app_role=APP_ROLE,
        grace_seconds=3600.0,
        now=now,
    )

    assert deleted == 1
    assert store.deleted == ["tenants/default/files/fl_orphan_old"]
    assert f"tenants/default/files/{live_id}" in store.objects
    assert "tenants/default/files/fl_orphan_young" in store.objects
    assert "tenants/default/other/fl_not_a_file_key" in store.objects
    assert references == [("res", "tenants/default/files/fl_orphan_old")]
    warnings = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("Orphan object sweep:")
    ]
    assert warnings == [
        "Orphan object sweep: deleted object reference res_0123456789abcdef "
        "without a files row (age 7200s)."
    ]


def test_sweep_skips_tenants_without_any_visible_row(
    session_factory, caplog, monkeypatch
) -> None:
    """Objects but zero registry rows is indistinguishable from a broken
    tenant context — the sweep must refuse to guess and delete nothing."""
    import inqtrix.storage.object_orphan_sweep as sweep_module

    references = []

    def reference(namespace, identifier):
        references.append((namespace, identifier))
        return "ten_0123456789abcdef"

    monkeypatch.setattr(sweep_module, "pseudonymous_log_reference", reference)
    now = time.time()
    store = _RecordingObjectStore(
        {
            "tenants/ghost/files/fl_maybe_orphan_1": now - 7200.0,
            "tenants/ghost/files/fl_maybe_orphan_2": now - 7200.0,
        }
    )

    with caplog.at_level("WARNING", logger="inqtrix"):
        deleted = sweep_orphaned_file_objects(
            object_store=store,
            session_factory=session_factory,
            app_role=APP_ROLE,
            grace_seconds=3600.0,
            now=now,
        )

    assert deleted == 0
    assert store.deleted == []
    assert references == [("ten", "ghost")]
    warnings = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("Orphan object sweep:")
    ]
    assert warnings == [
        "Orphan object sweep: tenant reference ten_0123456789abcdef has 2 "
        "objects but no matching files row; skipping this tenant to avoid "
        "cleanup under the wrong context."
    ]
