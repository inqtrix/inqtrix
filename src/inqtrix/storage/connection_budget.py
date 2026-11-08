"""Report what this process may hold against what the server allows.

Written down only as prose, the budget arithmetic would leave an operator to add
up what a process may hold and compare the result against a server setting nobody
reads. This module derives every part of it and, when it does not fit, says so out
loud at startup.

Deliberately a warning and never a refusal. ``max_connections`` belongs to
whoever runs the database — which for most deployments is not Inqtrix at all —
and a process that stops because it *might* exhaust a limit is worse than one
that says so and keeps serving.
"""

from __future__ import annotations

import logging
log = logging.getLogger(__name__)

# A transaction pooler multiplexes many client connections onto few server
# ones, so the app-side total is not the number to compare. These are the
# ports the bundled pooler listens on; a URL pointing at one means the
# comparison would be meaningless rather than reassuring.
_POOLER_PORTS = (6432,)
_POOLER_HOST_MARKERS = ("pgbouncer", "pooler")


def _looks_pooled(database_url: str) -> bool:
    """Report whether the URL appears to address a transaction pooler."""
    lowered = (database_url or "").lower()
    if any(marker in lowered for marker in _POOLER_HOST_MARKERS):
        return True
    return any(f":{port}/" in lowered for port in _POOLER_PORTS)


async def _server_max_connections(database_url: str) -> int | None:
    """Read the server's own ceiling, or ``None`` when it cannot be read.

    Opens its own short-lived connection: the check must not depend on the
    engine it reports on, and a one-off adds nothing to the budget at rest.
    """
    from sqlalchemy import text

    from inqtrix.storage.db import build_engine

    engine = build_engine(database_url, null_pool=True)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SHOW max_connections"))
            value = result.scalar()
    finally:
        await engine.dispose()
    return int(value) if value is not None else None


async def report_connection_budget(
    *,
    database_url: str,
    process_label: str,
    pool_size: int,
    pool_max_overflow: int,
    extra_connections: int = 0,
    extra_label: str = "",
    vector_write_slots: int = 0,
) -> None:
    """Log this process's connection budget and warn when it cannot fit.

    Args:
        database_url: The configured URL. Used to notice a transaction
            pooler in front of the server, and to open one short-lived
            connection that reads the server ceiling.
        process_label: What to call this process in the message.
        pool_size: Configured connections of the engine.
        pool_max_overflow: Configured burst connections of the engine.
        extra_connections: Connections held outside the engine.
        extra_label: What those extra connections belong to.
        vector_write_slots: Jobs of this process that may each hold a connection
            for the length of a write to the vector index. Every other statement
            of the process waits while they do, so they must not fill the pool.
    """
    from inqtrix.storage.db import pooled_connection_budget

    engines, pooled = pooled_connection_budget()
    total = pooled + extra_connections
    detail = (
        f" + {extra_connections} ({extra_label})" if extra_connections else ""
    )
    log.info(
        "Postgres connection budget | pool_size=%d max_overflow=%d | %d engines "
        "-> up to %d%s connections per %s.",
        pool_size,
        pool_max_overflow,
        engines,
        pooled,
        detail,
        process_label,
    )
    if pool_max_overflow:
        log.warning(
            "INQTRIX_DATABASE_POOL_MAX_OVERFLOW=%d: connections above "
            "INQTRIX_DATABASE_POOL_SIZE close when they return, so this %s keeps "
            "reconnecting whenever it stays above it. Raise "
            "INQTRIX_DATABASE_POOL_SIZE instead.",
            pool_max_overflow,
            process_label,
        )
    if vector_write_slots >= pool_size + pool_max_overflow:
        log.warning(
            "INQTRIX_REINDEX_MAX_CONCURRENT + INQTRIX_DELETION_MAX_CONCURRENT = %d "
            "jobs can each hold a connection while they write to the vector index, "
            "and this %s has %d. Everything else on its database waits while they "
            "do. Raise INQTRIX_DATABASE_POOL_SIZE or lower the job limits.",
            vector_write_slots,
            process_label,
            pool_size + pool_max_overflow,
        )

    if _looks_pooled(database_url):
        log.info(
            "Connection budget was not checked against max_connections: the database "
            "URL points to a transaction pooler. Its max_client_conn and backend pool "
            "size apply instead of the server limit."
        )
        return

    try:
        ceiling = await _server_max_connections(database_url)
    except Exception as exc:  # noqa: BLE001 — an unreachable server is routine
        log.info(
            "Connection budget was not checked against max_connections (%s). Startup "
            "continues; the calculated budget above remains valid.",
            type(exc).__name__,
        )
        return

    if ceiling is None:
        return
    if total > ceiling:
        log.warning(
            "Connection budget exceeds the server limit: this %s can occupy up to %d "
            "connections, max_connections=%d, and each additional replica brings its "
            "own budget. Reduce INQTRIX_DATABASE_POOL_SIZE / "
            "INQTRIX_DATABASE_POOL_MAX_OVERFLOW, increase max_connections, or add a "
            "transaction pooler.",
            process_label,
            total,
            ceiling,
        )
