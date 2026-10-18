"""Async engine construction and the tenant-scoped transaction helper.

The tenant helper is the single sanctioned way to touch tenant tables:
every call opens ONE transaction, switches to the restricted
application role, and sets the transaction-local ``inqtrix.tenant_id``
GUC the row-level-security policies read. Both steps are
deliberately transaction-scoped:

* ``SET LOCAL ROLE`` reverts at COMMIT/ROLLBACK, so pooled
  connections never leak the restricted role into unrelated work
  (e.g. migrations sharing a pool in tests).
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
import logging
import re
import threading
import weakref
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, AsyncIterator

from anyio import CancelScope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.util.concurrency import await_only

log = logging.getLogger("inqtrix")

TENANT_GUC = "inqtrix.tenant_id"
"""Name of the per-transaction GUC carrying the tenant id for RLS."""

_SAFE_ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
"""Allowed shape for the application role identifier. ``SET ROLE`` is a
utility statement and cannot take bind parameters, so the identifier is
validated against this conservative pattern before interpolation."""

_ENGINE_LOOPS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()
"""First event loop seen per POOLED engine, for :func:`_warn_on_loop_change`.
Weak keys so a disposed engine does not pin its loop."""

_LOOP_MISMATCH_REPORTED: "weakref.WeakSet[Any]" = weakref.WeakSet()
"""Engines already reported. The failure repeats every call; one warning
names the cause without drowning the log that carries the crash."""


def _warn_on_loop_change(session_factory: "async_sessionmaker[AsyncSession]") -> None:
    """Name the cause when a POOLED engine is touched from a second loop.

    asyncpg connections are event-loop-affine. A pooled engine therefore
    belongs to exactly ONE loop: either a persistent one (the HTTP loop, a
    job store's dedicated loop) or none at all. Reached from a second loop
    — the classic case being a sync run thread driving it through
    ``run_coro_sync``/``asyncio.run``, one fresh loop per call — the pool
    hands back a connection bound to a foreign or already-dead loop and
    ``pool_pre_ping`` fails with "Future attached to a different loop". The
    same reuse also corrupts asyncpg's protocol state and poisons the pool
    for every later borrower, including the request path.

    :mod:`inqtrix.sync_bridge`, :func:`build_engine` and
    ``build_run_thread_persistence`` all state the resulting invariant —
    anything reachable from a per-call loop MUST sit on a NullPool engine —
    and none of them could enforce it: not every caller goes through
    ``sync_bridge`` (the agents use bare ``asyncio.run``), so no wrapper
    sees them all. :func:`tenant_session` does; it is the one chokepoint
    every tenant-scoped read and write passes through.

    Warns rather than raises: the asyncpg failure follows within the same
    call anyway, so raising would only replace one error with another. The
    value is naming the cause — and catching the latent case that has not
    crashed yet because the pool happened to be empty.

    NullPool engines are skipped: holding no connection, they are
    loop-agnostic by construction and are exactly what the invariant asks
    for.
    """
    engine = session_factory.kw.get("bind")
    if engine is None or isinstance(engine.pool, NullPool):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    first = _ENGINE_LOOPS.get(engine)
    if first is None:
        _ENGINE_LOOPS[engine] = loop
        return
    if first is loop or engine in _LOOP_MISMATCH_REPORTED:
        return
    _LOOP_MISMATCH_REPORTED.add(engine)
    log.warning(
        "Pooled database engine %s reached from a second event loop. "
        "asyncpg connections are loop-affine: this pool is bound to the "
        "loop that first touched it, and the next checkout can fail with "
        "'Future attached to a different loop' or corrupt the connection "
        "for other callers. A store driven from a sync thread via "
        "asyncio.run must be built with null_pool=True.",
        id(engine),
    )


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

    Counts every pooled engine built in this process. NullPool stores are
    excluded: they hold nothing between operations and instead add one
    short-lived connection per operation in flight.
    """
    with _pooled_engine_lock:
        return len(_pooled_engine_capacities), sum(_pooled_engine_capacities)


def build_engine(
    database_url: str,
    *,
    null_pool: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: float = 30.0,
    command_timeout: float | None = None,
) -> AsyncEngine:
    """Create the async engine for the platform persistence layer.

    Args:
        database_url: SQLAlchemy async URL
            (``postgresql+asyncpg://...``). Constructor argument by
            design — only the settings bridge reads the environment.
        null_pool: When ``True``, use a :class:`NullPool` (a fresh
            connection per operation, none cached). asyncpg connections
            are event-loop-affine; a pooled connection reused across
            loops fails. NullPool makes the engine loop-AGNOSTIC, which
            a store that is called both from the async request loop AND
            from a sync worker thread via ``asyncio.run`` requires (the
            quota store; the durable run store solves the same problem
            with its own dedicated loop instead). The pool sizing
            arguments are ignored on this branch — NullPool holds no
            connections.
        pool_size: Persistent connections this engine keeps open.
            Default 5 (the SQLAlchemy default) keeps existing
            deployments byte-identical. Operators size it through
            ``StorageSettings.pool_kwargs()`` so the per-process budget
            — ``pooled_engines x (pool_size + max_overflow)`` — stays
            reviewable against Postgres ``max_connections``.
        max_overflow: Burst connections beyond ``pool_size``, closed
            again when idle (default 10, the SQLAlchemy default).
        pool_timeout: Seconds a caller waits for a free pooled
            connection before failing loudly (default 30, the
            SQLAlchemy default). Bounds the queueing latency a
            too-small pool would otherwise convert errors into.
        command_timeout: Per-statement wall-clock ceiling in seconds,
            enforced client-side by asyncpg (``None`` disables). Pre-ping
            only validates connections at checkout; this bounds the one
            remaining hang class — an in-flight statement on a silently
            dead connection — by turning it into an ordinary error.

    Returns:
        An :class:`AsyncEngine` with pre-ping (survives idle-timeout
        connection kills) and a 30-minute recycle below common
        infrastructure idle limits.
    """
    connect_args = (
        {"command_timeout": command_timeout} if command_timeout else {}
    )
    if null_pool:
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args=connect_args,
        )
    else:
        engine = create_async_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            connect_args=connect_args,
        )
        _record_pooled_engine(pool_size + max_overflow)
    _make_asyncpg_pool_invalidation_atomic(engine)
    _make_first_connect_mutex_loop_agnostic(engine)
    return engine


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


class _LoopAgnosticOnceMutex:
    """Serialize an engine's first-connect dialect initialization across event loops.

    SQLAlchemy runs ``dialect.initialize`` exactly once per engine, on the
    first connection, and guards that run with a mutex that EVERY connection
    of the engine takes until the first run has completed
    (``event/attr.py: _exec_w_sync_on_first_run``). For async engines that
    mutex is an ``asyncio.Lock`` (``AsyncAdaptedLock``). An ``asyncio.Lock``
    binds itself to the loop of its first contended waiter and wakes that
    waiter through the *foreign* loop's ``call_soon`` from whichever loop
    releases it: with the per-call loops the workspace agent drives from
    executor threads (``asyncio.run`` per tool call), a second contender
    raises "bound to a different event loop" and the first one never wakes
    up -- its loop sleeps in ``epoll_wait`` without a timer, holding an
    authenticated connection that never sends a statement (for example
    three parallel ``search_context`` calls that make the first connections
    of the agent-sessions engine in a worker process).

    This mutex satisfies the same ``_MutexProtocol`` with a thread lock, so it
    is correct across threads and loops, and it never blocks the calling
    loop: a contender that misses the lock yields to its own loop with a
    short sleep instead of parking the thread, which keeps a holder that
    awaits the database on the SAME loop (the API process) runnable. The
    lock is only ever contended during a process's first connections of one
    engine; afterwards SQLAlchemy skips the guard entirely.
    """

    _POLL_SECONDS = 0.005

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> bool:
        while not self._lock.acquire(blocking=False):
            await_only(asyncio.sleep(self._POLL_SECONDS))
        return True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.release()


def _make_first_connect_mutex_loop_agnostic(engine: AsyncEngine) -> None:
    """Install :class:`_LoopAgnosticOnceMutex` on the pool's connect events.

    Set before the first connection so SQLAlchemy's lazily created
    ``AsyncAdaptedLock`` never comes into existence for this engine
    (``_get_exec_once_mutex`` returns a preset mutex unchanged). Done here,
    at the one place every engine is built, because the hazard is a property
    of how the engine is DRIVEN (per-call loops) and no store can see that.
    The private attribute is pinned by a test so a library upgrade fails
    loudly instead of hanging silently.
    """

    dispatch = engine.sync_engine.pool.dispatch
    for name in ("connect", "first_connect"):
        collection = getattr(dispatch, name).for_modify(dispatch)
        collection._exec_once_mutex = _LoopAgnosticOnceMutex()


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory bound to *engine*.

    ``expire_on_commit=False`` is mandatory for async SQLAlchemy:
    attribute access after commit must not trigger implicit IO
    (``MissingGreenlet``).
    """
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
    _warn_on_loop_change(session_factory)
    session = session_factory()
    transaction = session.begin()
    session_entered = False
    transaction_entered = False
    try:
        await session.__aenter__()
        session_entered = True
        await transaction.__aenter__()
        transaction_entered = True
        if app_role:
            # Identifier validated above; SET ROLE cannot be
            # parameterized (Postgres utility statement).
            await session.execute(text(f'SET LOCAL ROLE "{app_role}"'))
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
