"""The cutover fence rides inside every durable-claim transaction.

Four stores implement the durable claim separately; a fence on only one
leaves three doors open. The fence must also be the FIRST statement of the
transaction -- which these tests pin without any domain seeding: even a
claim for a MISSING id must hit the fence before it can answer ``None``,
because the fence runs before the row lookup.

What this deliberately does NOT cover (the documented limit): a migration
still in flight carries the old revision until its step commits; stopping
that needs the canonical migration workflow, not this check.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
from inqtrix.runs.indexing_postgres import PostgresIndexingJobStore
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.runs.upload_postgres import PostgresUploadOperationStore
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.migration_contract import (
    SCHEMA_HEAD_REVISION,
    SchemaHeadMismatch,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
TENANT = "default"


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest.fixture()
def version_control():
    """Flip the installed revision and GUARANTEE it is restored.

    A SYNC fixture on purpose: the first draft was an async fixture handed
    to sync tests, whose teardown silently never ran and stranded the
    shared database on the fake revision -- poisoning every later suite.
    Each call builds and disposes its own engine inside ``asyncio.run``,
    so there is no loop affinity to violate and the restore in the
    teardown always executes.
    """

    def _set(revision: str) -> None:
        async def go() -> None:
            engine = build_engine(TEST_DATABASE_URL)
            try:
                factory = build_session_factory(engine)
                async with factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "UPDATE alembic_version SET version_num = :rev"
                            ),
                            {"rev": revision},
                        )
            finally:
                await engine.dispose()

        import asyncio

        asyncio.run(go())

    yield _set
    _set(SCHEMA_HEAD_REVISION)


def _stores():
    # recover_orphans=False everywhere: these stores run against the
    # SHARED integration database, and an eager orphan sweep in a
    # constructor would blanket-fail other suites' in-flight rows.
    suffix = uuid.uuid4().hex[:8]
    return {
        "runs": PostgresRunStore(
            engine=build_engine(TEST_DATABASE_URL),
            app_role=APP_ROLE,
            queue=None,
            recover_orphans=False,
            max_concurrent=2,
            max_queue_size=10,
            completed_ttl_seconds=300,
            worker_id=f"fence-{suffix}",
        ),
        "indexing": PostgresIndexingJobStore(
            engine=build_engine(TEST_DATABASE_URL),
            app_role=APP_ROLE,
            queue=None,
            recover_orphans=False,
            max_concurrent=2,
            max_queue_size=10,
            completed_ttl_seconds=300,
            history_limit=2,
            worker_id=f"fence-{suffix}",
        ),
        "deletion": PostgresDeletionOperationStore(
            engine=build_engine(TEST_DATABASE_URL),
            app_role=APP_ROLE,
            queue=None,
            recover_orphans=False,
            max_concurrent=1,
            completed_ttl_seconds=3600,
            worker_id=f"fence-{suffix}",
        ),
        "upload": PostgresUploadOperationStore(
            engine=build_engine(TEST_DATABASE_URL),
            app_role=APP_ROLE,
            queue=None,
            recover_orphans=False,
            worker_id=f"fence-{suffix}",
        ),
    }


def _claim(store, domain: str, entity_id: str):
    del domain  # all four claims share the takeover-flag signature
    return store.claim_for_execution(entity_id, TENANT, allow_takeover=False)


@pytest.mark.parametrize("domain", ["runs", "indexing", "deletion", "upload"])
def test_a_stale_head_refuses_the_claim_in_every_domain(
    domain, version_control
) -> None:
    """Wrong revision -> the claim raises; right revision -> normal answer.

    Uses a missing id on purpose: the fence must fire BEFORE the row
    lookup, so even "no such entity" is unreachable on a moved head. A
    fence placed after the lookup would pass this file's green case and
    silently skip the very writes it exists to stop.
    """
    stores = _stores()
    store = stores[domain]
    try:
        missing = f"fence-missing-{uuid.uuid4().hex[:8]}"
        # Green case first: current head answers normally (None for a
        # missing id) -- proves the fence does not break healthy claims.
        assert _claim(store, domain, missing) is None

        version_control("9999_fake_future_head")
        with pytest.raises(SchemaHeadMismatch, match="Schema head changed under the worker"):
            _claim(store, domain, missing)

        version_control(SCHEMA_HEAD_REVISION)
        assert _claim(store, domain, missing) is None
    finally:
        for s in stores.values():
            s.close()


def test_mid_batch_head_flip_stops_the_next_claim(version_control) -> None:
    """Claim one, flip the head, the NEXT claim of the same batch refuses.

    A per-job forced probe would cover this scenario only with a window
    between check and claim; here the check and the claim share one
    transaction, so no window is left.
    """
    stores = _stores()
    store = stores["runs"]
    try:
        first = f"fence-batch-{uuid.uuid4().hex[:8]}"
        second = f"fence-batch-{uuid.uuid4().hex[:8]}"
        assert store.claim_for_execution(
            first, TENANT, allow_takeover=False
        ) is None

        version_control("9999_fake_future_head")
        with pytest.raises(SchemaHeadMismatch):
            store.claim_for_execution(second, TENANT, allow_takeover=False)
    finally:
        for s in stores.values():
            s.close()
