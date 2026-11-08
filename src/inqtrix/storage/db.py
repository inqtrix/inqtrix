"""Async engine construction and the tenant-scoped transaction helper.

The tenant helper is the single sanctioned way to touch tenant tables:
every call opens ONE transaction, switches to the restricted
application role, and sets the transaction-local ``inqtrix.tenant_id``
GUC the row-level-security policies read. Both steps are
deliberately transaction-scoped:

* ``set_config('role', ..., true)``, the function form of ``SET LOCAL
  ROLE``, reverts at COMMIT/ROLLBACK, so pooled connections never leak
  the restricted role into unrelated work (e.g. migrations sharing a
  pool in tests).
* ``set_config(..., is_local => true)`` reverts the tenant id the
  same way — the only safe form under connection pooling, where a
  session-scoped value would leak the previous tenant to the next
  request (PgBouncer transaction mode skips reset queries entirely).

The policies fail closed on top of this: the
``inqtrix_current_tenant_id()`` helper raises when the GUC is unset
*or* empty (after a transaction-local value reverts, the parameter
survives as an empty string for the connection lifetime — the
documented ``current_setting`` gotcha), so a forgotten
``tenant_session`` produces a loud error, never another tenant's rows.

Request cancellation cannot be allowed to interrupt transaction
finalization. ``tenant_session`` shields rollback/commit and session close
from both AnyIO level cancellation and direct asyncio task cancellation, so
an abandoned HTTP response cannot strand a checked-out pool connection.

Transaction-pooler contract (PgBouncer ``pool_mode=transaction``): this
module is deliberately pooler-safe — no session-scoped SET, no
LISTEN/NOTIFY, no session advisory locks — and MUST stay that way; a
future session-scoped feature would silently break behind a pooler.
Two operational requirements remain on the DEPLOYMENT side: (1) asyncpg
prepared statements need either ``?prepared_statement_cache_size=0`` in
the URL or PgBouncer >= 1.21 with ``max_prepared_statements`` tracking
(the bundled compose/helm pgbouncer sets both); (2) Alembic migrations
(``inqtrix-migrate``) connect DIRECTLY to Postgres, never through the
pooler — DDL and long transactions do not multiplex.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, AsyncIterator, Iterator

from anyio import CancelScope
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import InvalidatePoolError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

log = logging.getLogger(__name__)

TENANT_GUC = "inqtrix.tenant_id"
"""Name of the per-transaction GUC carrying the tenant id for RLS."""

_SAFE_ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
"""Allowed shape for the application role identifier. A misconfigured role
fails here, in the application's words, not in the first statement of a
request."""

_PING_AFTER_IDLE_SECONDS = 1.0
"""How long a pooled connection may sit unused before its next checkout checks
it. The check is three round trips and a transaction otherwise needs about
five; a connection in steady use comes around again within milliseconds."""

_RETURNED_AT = "inqtrix_returned_at"

_pooled_engine_capacities: list[int] = []
_pooled_engine_lock = threading.Lock()


def _record_pooled_engine(max_connections: int) -> None:
    """Note one more pooled engine and what it may hold.

    Counted here because this is the only place a pooled engine is made.
    Stating the budget anywhere else means enumerating the call sites by
    hand, and a hand-kept enumeration is wrong the moment a store is
    added without anyone noticing.
    """
    with _pooled_engine_lock:
        _pooled_engine_capacities.append(max_connections)


def pooled_connection_budget() -> tuple[int, int]:
    """Return how many pooled engines exist and what they may hold together.

    A running process has one. One-off probes on a NullPool engine hold
    nothing between operations and are not counted.
    """
    with _pooled_engine_lock:
        return len(_pooled_engine_capacities), sum(_pooled_engine_capacities)


def _dumps_json_column(value: Any) -> str:
    """Serialize a JSON column value; Postgres refuses NaN and Infinity.

    One such number would fail the whole write, so it is stored as ``null`` and
    said out loud. The log names how many and never a key, because keys in
    metadata are user data. Everything else stays byte-identical to the default
    serializer: event payloads are replayed verbatim.
    """
    try:
        return json.dumps(value, allow_nan=False)
    except ValueError:
        pass
    replaced = 0

    def finite(item: Any) -> Any:
        nonlocal replaced
        if isinstance(item, float) and not math.isfinite(item):
            replaced += 1
            return None
        if isinstance(item, dict):
            return {key: finite(element) for key, element in item.items()}
        if isinstance(item, (list, tuple)):
            return [finite(element) for element in item]
        return item

    text_form = json.dumps(finite(value))
    if replaced:
        log.warning(
            "A JSON column value held %d numbers that are not finite; stored as null.",
            replaced,
        )
    return text_form


def build_engine(
    database_url: str,
    *,
    null_pool: bool = False,
    pool_size: int = 15,
    max_overflow: int = 0,
    pool_timeout: float = 30.0,
    command_timeout: float | None = None,
) -> AsyncEngine:
    """Create an async engine for the platform persistence layer.

    A serving process builds exactly one, in its composition root, and hands it
    to every store. asyncpg connections belong to the loop that opened them, so
    the engine belongs to the process loop.

    Args:
        database_url: SQLAlchemy async URL
            (``postgresql+asyncpg://...``). Constructor argument by
            design — only the settings bridge reads the environment.
        null_pool: When ``True``, use a :class:`NullPool` (a fresh
            connection per operation, none cached). Only for one-off probes
            and command line tools, which are not worth a pool. The pool
            sizing arguments are ignored on this branch.
        pool_size: Connections this engine keeps once opened. They open on
            demand and stay until recycled. Operators size it through
            ``StorageSettings.pool_kwargs()`` so the per-process budget
            stays reviewable against Postgres ``max_connections``.
        max_overflow: Burst connections beyond ``pool_size``. The pool closes
            them on return, so a process that stays above ``pool_size`` keeps
            reconnecting; the default is none.
        pool_timeout: Seconds a caller waits for a free pooled
            connection before failing loudly (default 30, the
            SQLAlchemy default). Bounds the queueing latency a
            too-small pool would otherwise convert errors into.
        command_timeout: Per-statement wall-clock ceiling in seconds,
            enforced client-side by asyncpg (``None`` disables). The
            connection check runs at checkout only; this bounds the one
            remaining hang class — an in-flight statement on a silently
            dead connection — by turning it into an ordinary error.

    Returns:
        An :class:`AsyncEngine` that checks a connection left idle (survives
        idle-timeout connection kills) and recycles after 30 minutes, below
        common infrastructure idle limits.
    """
    connect_args = (
        {"command_timeout": command_timeout} if command_timeout else {}
    )
    if null_pool:
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args=connect_args,
            json_serializer=_dumps_json_column,
        )
    else:
        engine = create_async_engine(
            database_url,
            pool_recycle=1800,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            connect_args=connect_args,
            json_serializer=_dumps_json_column,
        )
        _record_pooled_engine(pool_size + max_overflow)
        _check_connections_left_idle(engine)
    _make_asyncpg_pool_invalidation_atomic(engine)
    return engine


class DatabasePoolCollector:
    """Prometheus collector: how full the pool of this process is at scrape time."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._pool = engine.pool

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        yield GaugeMetricFamily(
            "inqtrix_database_pool_size",
            "Connections the pool of this process may keep.",
            value=self._pool.size(),
        )
        yield GaugeMetricFamily(
            "inqtrix_database_pool_in_use",
            "Connections of this process's pool in use right now.",
            value=self._pool.checkedout(),
        )


def build_process_engine(storage: Any) -> AsyncEngine | None:
    """The one database engine of a process, or ``None`` without Postgres.

    Every store of the process shares it, so its connections have one limit.
    Whoever composes the process disposes it, after everything that uses it has
    stopped.

    Args:
        storage: The resolved ``StorageSettings``.

    Raises:
        RuntimeError: Postgres is configured without a database URL.
    """
    if storage.backend != "postgres":
        return None
    if not storage.database_url.strip():
        raise RuntimeError(
            "INQTRIX_STORAGE_BACKEND=postgres requires INQTRIX_DATABASE_URL to be set."
        )
    return build_engine(storage.database_url, **storage.pool_kwargs())


def resolve_process_engine(auth_provider: Any, storage: Any) -> AsyncEngine | None:
    """The engine an auth provider built beforehand brought, else a new one.

    The documented library path builds the provider first. Its stores already sit
    on the engine of the process, so a second one would double the limit. A
    provider built from other settings would put every store on one database and
    the checkpointer, the budget and the worker on another, so that refuses to
    start.

    Raises:
        RuntimeError: The provider's database is not the one the settings name.
    """
    engine = getattr(auth_provider, "database_engine", None)
    if engine is None:
        return build_process_engine(storage)
    if engine.url != make_url(storage.database_url):
        raise RuntimeError(
            "The auth provider sits on another database than INQTRIX_DATABASE_URL "
            "names. Build it from the same settings as the app."
        )
    return engine


def _check_connections_left_idle(engine: AsyncEngine) -> None:
    """Check a pooled connection at checkout only when it sat unused.

    SQLAlchemy's ``pool_pre_ping`` checks every checkout, and on asyncpg that is
    BEGIN, an empty statement and ROLLBACK. A transaction holds its connection
    through each round trip, so with a database a few milliseconds away the check
    alone takes more than a third of what the pool can serve.

    A connection returned a moment ago was alive a moment ago. One that died in
    that moment fails its caller like a statement in flight would, and that failure
    replaces the pool. What the check is for stays covered: a connection an idle
    timeout or a restart killed while it sat in the pool. As with ``pool_pre_ping``,
    a failed check replaces every older connection.
    """
    dialect = engine.sync_engine.dialect

    @event.listens_for(engine.sync_engine, "checkin")
    def note_return(dbapi_connection: Any, record: Any) -> None:
        record.info[_RETURNED_AT] = time.monotonic()

    @event.listens_for(engine.sync_engine, "checkout")
    def check_if_left_idle(dbapi_connection: Any, record: Any, proxy: Any) -> None:
        returned_at = record.info.get(_RETURNED_AT)
        if returned_at is None or time.monotonic() - returned_at < _PING_AFTER_IDLE_SECONDS:
            return
        try:
            dialect.do_ping(dbapi_connection)
        except dialect.loaded_dbapi.Error as error:
            if dialect.is_disconnect(error, dbapi_connection, None):
                raise InvalidatePoolError() from error
            raise


def _make_asyncpg_pool_invalidation_atomic(engine: AsyncEngine) -> None:
    """Let a cancelled asyncpg termination finish pool bookkeeping.

    SQLAlchemy's asyncpg adapter force-closes the driver connection after a
    graceful close is cancelled, then re-raises ``CancelledError``. The pool
    clears the invalidated connection record only after ``do_terminate``
    returns, so that re-raise otherwise leaves a closed handle available for
    a later checkout. Suppressing cancellation only at this dialect hook lets
    invalidation finish; the original query cancellation still propagates
    from SQLAlchemy's execution path.
    """

    dialect = engine.sync_engine.dialect
    terminate = dialect.do_terminate

    def terminate_without_interrupting_pool_state(
        dbapi_connection: Any,
    ) -> None:
        try:
            terminate(dbapi_connection)
        except asyncio.CancelledError:
            return

    dialect.do_terminate = terminate_without_interrupting_pool_state


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory bound to *engine*.

    ``expire_on_commit=False`` is mandatory for async SQLAlchemy:
    attribute access after commit must not trigger implicit IO
    (``MissingGreenlet``).

    Raises:
        RuntimeError: There is no engine. Without this a store would only fail at
            its first statement, long after the composition went wrong.
    """
    if engine is None:
        raise RuntimeError(
            "A store on Postgres needs the engine of its process; see build_process_engine."
        )
    return async_sessionmaker(engine, expire_on_commit=False)


async def _finalize_tenant_session(
    session: AsyncSession,
    transaction: AsyncSessionTransaction,
    *,
    session_entered: bool,
    transaction_entered: bool,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: TracebackType | None,
) -> bool:
    """Finish the transaction and session even inside a cancelled request.

    Starlette uses AnyIO's level cancellation: after a client disconnects,
    every await in the cancelled request scope is cancelled again. Plain
    ``async with`` finalizers therefore may never finish their rollback or
    return the asyncpg connection to SQLAlchemy's pool. The nested AnyIO
    shield protects that finalization from the request scope; the asyncio
    task and shield also preserve it if a caller uses direct task
    cancellation instead.
    """

    async def cleanup() -> bool:
        suppressed = False
        session_exit = (exc_type, exc, traceback)
        try:
            if transaction_entered:
                try:
                    suppressed = bool(
                        await transaction.__aexit__(
                            exc_type,
                            exc,
                            traceback,
                        )
                    )
                    if suppressed:
                        session_exit = (None, None, None)
                except BaseException as cleanup_error:
                    session_exit = (
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    )
                    log.exception("Tenant database transaction cleanup failed")
                    raise
            return suppressed
        finally:
            if session_entered:
                try:
                    await session.__aexit__(*session_exit)
                except BaseException:
                    log.exception("Tenant database session cleanup failed")
                    raise

    with CancelScope(shield=True):
        cleanup_task = asyncio.create_task(
            cleanup(),
            name="inqtrix-tenant-session-cleanup",
        )
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # ``CancelScope(shield=True)`` handles Starlette/AnyIO
            # cancellation. A direct ``Task.cancel()`` can still interrupt
            # this task, so wait for the held cleanup task before preserving
            # that cancellation.
            await cleanup_task
            raise


@asynccontextmanager
async def tenant_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    app_role: str,
) -> AsyncIterator[AsyncSession]:
    """One tenant-scoped transaction against the identity schema.

    Opens a session, begins a transaction, switches to *app_role*
    (when non-empty), sets the transaction-local tenant GUC, and
    yields the session. Commit happens on clean exit, rollback on
    exception. Callers must finish their work inside the context —
    a second transaction on the same session would run without the
    GUC and fail loudly in the RLS helper.

    Args:
        session_factory: Factory from :func:`build_session_factory`.
        tenant_id: Tenant whose rows this transaction may touch.
            Must be non-empty — an empty tenant id would otherwise
            silently match zero rows instead of failing.
        app_role: Restricted Postgres role to run as (see
            ``StorageSettings.app_role``). Empty skips the switch.

    Raises:
        ValueError: On an empty *tenant_id* or an *app_role* that does
            not match the conservative identifier pattern (defense
            against SQL injection through configuration).
    """
    if not tenant_id.strip():
        raise ValueError("tenant_id must be non-empty for a tenant session")
    if app_role and not _SAFE_ROLE_PATTERN.fullmatch(app_role):
        raise ValueError(f"app_role has an unsafe identifier shape: {app_role!r}")
    session = session_factory()
    transaction = session.begin()
    session_entered = False
    transaction_entered = False
    try:
        await session.__aenter__()
        session_entered = True
        await transaction.__aenter__()
        transaction_entered = True
        # One statement: this runs in every transaction, and the transaction holds
        # its connection through every round trip.
        if app_role:
            await session.execute(
                text("SELECT set_config('role', :role, true), set_config(:guc, :tenant, true)"),
                {"role": app_role, "guc": TENANT_GUC, "tenant": tenant_id},
            )
        else:
            await session.execute(
                text("SELECT set_config(:guc, :tenant, true)"),
                {"guc": TENANT_GUC, "tenant": tenant_id},
            )
        yield session
    except BaseException as error:
        suppressed = await _finalize_tenant_session(
            session,
            transaction,
            session_entered=session_entered,
            transaction_entered=transaction_entered,
            exc_type=type(error),
            exc=error,
            traceback=error.__traceback__,
        )
        if suppressed:
            return
        raise
    else:
        await _finalize_tenant_session(
            session,
            transaction,
            session_entered=session_entered,
            transaction_entered=transaction_entered,
            exc_type=None,
            exc=None,
            traceback=None,
        )
