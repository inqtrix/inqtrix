"""Consistency sweep between the object store and the ``files`` registry.

The upload path writes the blob before committing its registry row, so a
crash in that window leaves an object nobody references. Nothing else ever
deletes such an orphan: the registry row is the source of truth for every
regular delete path, and a blob without a row is unreachable by design.
This sweep is the maintenance counterpart — it lists the physical
inventory, checks it against the registry per tenant (the ``files`` table
is row-level-secured by tenant), and removes objects that have no row and
are older than a grace window.

Safety posture over completeness:

* Only keys matching the canonical ``tenants/<tenant>/files/<id>`` layout
  are considered; anything else in the bucket is never touched.
* The registry check runs inside :func:`tenant_session` so RLS answers for
  the RIGHT tenant. A tenant whose listing has objects but whose registry
  shows ZERO rows is skipped with a WARNING instead of swept — that shape
  is indistinguishable from a misconfigured tenant context, and a wrong
  context here would mass-delete live data.
* Every deletion is logged individually; the sweep is loud, never silent.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import bindparam, text

from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.storage.db import tenant_session
from inqtrix.sync_bridge import run_coro_sync

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from inqtrix.storage.object_store import ObjectStore

log = logging.getLogger("inqtrix")

_FILE_KEY_PATTERN = re.compile(
    r"^tenants/(?P<tenant>[^/]+)/files/(?P<file_id>[^/]+)$"
)
_REGISTRY_CHUNK_SIZE = 500


@dataclass(frozen=True)
class _ListedObject:
    key: str
    file_id: str
    last_modified: float


async def _registered_file_ids(
    session: "AsyncSession", file_ids: list[str]
) -> set[str]:
    """Return the subset of *file_ids* that exist for the session tenant."""
    known: set[str] = set()
    statement = text("SELECT id FROM files WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    for start in range(0, len(file_ids), _REGISTRY_CHUNK_SIZE):
        chunk = file_ids[start : start + _REGISTRY_CHUNK_SIZE]
        result = await session.execute(statement, {"ids": chunk})
        known.update(str(row[0]) for row in result)
    return known


async def _tenant_registered_ids(
    session_factory: "async_sessionmaker[AsyncSession]",
    *,
    tenant_id: str,
    app_role: str,
    file_ids: list[str],
) -> set[str]:
    async with tenant_session(
        session_factory, tenant_id=tenant_id, app_role=app_role
    ) as session:
        return await _registered_file_ids(session, file_ids)


def sweep_orphaned_file_objects(
    *,
    object_store: "ObjectStore",
    session_factory: "async_sessionmaker[AsyncSession]",
    app_role: str,
    grace_seconds: float,
    now: float | None = None,
) -> int:
    """Delete file blobs that have no registry row, one tenant at a time.

    Returns the number of deleted objects. Raises whatever the object
    store listing raises — an unlistable backend must fail the pass
    loudly instead of reporting a clean zero.
    """
    reference_time = time.time() if now is None else now
    by_tenant: dict[str, list[_ListedObject]] = {}
    for key, last_modified in object_store.list_keys("tenants/"):
        match = _FILE_KEY_PATTERN.match(key)
        if match is None:
            continue
        by_tenant.setdefault(match.group("tenant"), []).append(
            _ListedObject(
                key=key,
                file_id=match.group("file_id"),
                last_modified=last_modified,
            )
        )

    deleted = 0
    for tenant_id in sorted(by_tenant):
        listed = by_tenant[tenant_id]
        registered = run_coro_sync(
            _tenant_registered_ids(
                session_factory,
                tenant_id=tenant_id,
                app_role=app_role,
                file_ids=[entry.file_id for entry in listed],
            )
        )
        if not registered:
            # Objects but zero visible rows: either every object is truly
            # orphaned, or the tenant context did not resolve. The two are
            # indistinguishable from here, and the second would turn this
            # sweep into a mass delete — skip loudly, never guess.
            log.warning(
                "Orphan object sweep: tenant reference %s has %d objects but no matching files "
                "row; skipping this tenant to avoid cleanup under the wrong context.",
                pseudonymous_log_reference("ten", tenant_id),
                len(listed),
            )
            continue
        for entry in listed:
            if entry.file_id in registered:
                continue
            if reference_time - entry.last_modified < grace_seconds:
                continue
            object_store.delete(entry.key)
            deleted += 1
            log.warning(
                "Orphan object sweep: deleted object reference %s without a files row (age "
                "%.0fs).",
                pseudonymous_log_reference("res", entry.key),
                reference_time - entry.last_modified,
            )
    return deleted
