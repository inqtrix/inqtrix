"""Worker entry point: ``python -m inqtrix.worker`` / ``inqtrix-worker``.

Builds the same composition as the HTTP server (providers, strategies,
registry, resolver) plus worker-scoped Postgres stores and Valkey queues
bound to this worker's consumer name, then runs the claim loops until
SIGTERM/SIGINT. Requires ``INQTRIX_STORAGE_BACKEND=postgres`` and
``INQTRIX_QUEUE_BACKEND=valkey`` — the zero-infrastructure default
deployment has no worker.

One process runs four claim loops when knowledge is enabled: research
runs, durable reindex jobs, aggregate deletions, and upload recovery share the same
fencing/heartbeat/reclaim
machinery (:class:`~inqtrix.worker.loop.BaseWorkerLoop`) over separate
Valkey streams. With knowledge disabled, run and deletion loops remain;
the missing reindex consumer is logged rather than silently omitted.

Graceful shutdown: stop claiming, drain in-flight jobs for a bounded
window, then exit. Jobs that did not finish are NOT cancelled — the
worker's heartbeat silence hands their dispatch entries to another
worker (at-least-once redelivery, absorbed by the job-row state machine).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import socket
import threading
import time
from typing import Callable

from inqtrix.logging_config import configure_logging, read_logging_env
from inqtrix.providers import create_providers
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.runs.valkey_queue import ValkeyRunQueue
from inqtrix.server.container import build_container
from inqtrix.settings import Settings
from inqtrix.strategies import (
    create_default_strategies,
    resolve_claim_extract_model,
)
from inqtrix.sync_bridge import run_coro_sync
from inqtrix.worker.indexing_loop import IndexingWorkerLoop
from inqtrix.worker.loop import (
    BaseWorkerLoop,
    WorkerClaimGuardError,
    WorkerClaimUnavailableError,
    WorkerLoop,
)

log = logging.getLogger("inqtrix")

_DRAIN_SECONDS = 90.0
# A healthy contract is re-proved once a minute: every probe opens its own
# connection and reads thousands of catalog rows, and per-claim safety does
# not depend on this cadence (the schema fence runs inside every claim
# transaction). A lost database is retried every few seconds so claims resume
# quickly after an outage.
_DATABASE_CONTRACT_INTERVAL_SECONDS = 60.0
_DATABASE_CONTRACT_RETRY_SECONDS = 5.0


class _DatabaseClaimGuard:
    """Coalesce and latch the periodic PostgreSQL claim-safety probe.

    Both run and indexing loops share one instance. The lock prevents two
    concurrent probes, while a latched failure ensures the worker never
    resumes claims after observing an unsafe role or schema revision.
    """

    def __init__(
        self,
        *,
        database_url: str,
        app_role: str,
        login_policy: str,
        interval_seconds: float = _DATABASE_CONTRACT_INTERVAL_SECONDS,
        retry_seconds: float = _DATABASE_CONTRACT_RETRY_SECONDS,
    ) -> None:
        self._database_url = database_url
        self._app_role = app_role
        self._login_policy = login_policy
        self._interval_seconds = interval_seconds
        self._retry_seconds = retry_seconds
        self._lock = threading.Lock()
        self._next_check = 0.0
        self._probe_count = 0
        self._probe_seconds = 0.0
        self._failure: str | None = None
        self._unavailable: str | None = None

    def __call__(self) -> None:
        """Verify when due, or fail immediately after any prior violation."""
        self._verify(force=False)

    _PROBE_REPORT_EVERY = 50

    def _record_probe(self, seconds: float) -> None:
        """Say what the probe costs at its remaining call sites.

        The probe runs at worker start and on the coalesced interval (one
        shared guard across all loops) -- NOT per claimed job; per-job
        protection is the in-transaction schema fence (assert_schema_head)
        inside every durable-claim transaction. Each probe still opens its
        own connection, runs the schema and grant checks, and disposes the
        engine again under a process-wide lock, so the counter grows with
        wall-clock intervals: a rapidly growing count or a rising mean
        signals probe contention, not claim traffic.
        """
        self._probe_count += 1
        self._probe_seconds += seconds
        if self._probe_count % self._PROBE_REPORT_EVERY:
            return
        log.info(
            "Claim contract probe: %d runs, mean %.0f ms, total %.1f s since process "
            "start.",
            self._probe_count,
            1000 * self._probe_seconds / self._probe_count,
            self._probe_seconds,
        )

    def latch_failure(self, message: str) -> None:
        """Stick the guard shut from outside the probe.

        The in-transaction schema fence fires inside a store's claim
        transaction, not inside this guard's own probe -- but its verdict
        is about the PROCESS (stale code), so it must stop every loop
        sharing this guard, exactly as a failed probe does. Idempotent;
        the first message wins.
        """
        with self._lock:
            if self._failure is None:
                self._failure = message

    def verify_now(self) -> None:
        """Force an uncoalesced probe; used only by the startup waits.

        The durable claim boundary itself is fenced in-transaction via
        assert_schema_head, so no per-claim forced probe exists.
        """
        self._verify(force=True)

    def _verify(self, *, force: bool) -> None:
        """Run the shared latched probe, optionally bypassing coalescing."""
        from inqtrix.storage.runtime_contract import (
            DatabaseRuntimeUnavailableError,
            verify_database_url_runtime_contract,
        )
        from inqtrix.urls import sanitize_log_message

        with self._lock:
            if self._failure is not None:
                raise WorkerClaimGuardError(self._failure)
            now = time.monotonic()
            if self._unavailable is not None and now < self._next_check:
                raise WorkerClaimUnavailableError(
                    self._unavailable,
                    retry_after_seconds=self._next_check - now,
                )
            if not force and now < self._next_check:
                return
            try:
                probe_started = time.monotonic()
                run_coro_sync(
                    verify_database_url_runtime_contract(
                        self._database_url,
                        app_role=self._app_role,
                        login_policy=self._login_policy,
                    )
                )
                self._record_probe(time.monotonic() - probe_started)
            except DatabaseRuntimeUnavailableError as exc:
                self._unavailable = sanitize_log_message(exc)
                self._next_check = now + self._retry_seconds
                cause = exc.__cause__ or exc
                log.warning(
                    "Worker database runtime contract temporarily "
                    "unavailable; new claims pause for %.1fs "
                    "(error_type=%s)",
                    self._retry_seconds,
                    type(cause).__name__,
                    extra={
                        "event": "worker.database_contract_unavailable",
                        "retry_after_seconds": self._retry_seconds,
                    },
                )
                raise WorkerClaimUnavailableError(
                    self._unavailable,
                    retry_after_seconds=self._retry_seconds,
                ) from None
            except Exception as exc:
                self._failure = sanitize_log_message(exc)
                raise WorkerClaimGuardError(self._failure) from None
            recovered = self._unavailable is not None
            self._unavailable = None
            self._next_check = now + self._interval_seconds
            if recovered:
                log.info(
                    "Worker database runtime contract recovered; "
                    "new claims resume.",
                    extra={"event": "worker.database_contract_recovered"},
                )


def _wait_for_database_claim_contract(
    guard: Callable[[], None],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Keep bootstrap alive for transient outages, not unsafe contracts."""
    while True:
        try:
            guard()
        except WorkerClaimUnavailableError as exc:
            sleep(exc.retry_after_seconds)
            continue
        return


def _verified_database_claim_contract(
    guard: Callable[[], None],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Hold the bootstrap until the contract holds, and NAME what stopped it.

    The message a failed bootstrap dies with is the whole diagnosis an
    operator gets: the process exits, and a container log line is the
    only thing left. Naming one hypothesis — that the orchestrated
    migration job had not reached schema head — while suppressing the
    exception that actually occurred would report a role that cannot log
    in as a schema problem and start the search in the wrong place.

    So the cause travels, twice over: in the message, and as the chained
    exception behind it. It is scrubbed on the way, because a database
    error carries the DSN and the DSN carries the password — the claim
    guard already does this for what it raises, and this covers whatever
    else reaches here.
    """
    from inqtrix.urls import sanitize_log_message

    try:
        _wait_for_database_claim_contract(guard, sleep=sleep)
    except Exception as exc:
        cause = sanitize_log_message(exc)
        log.error(
            "Worker database runtime contract failed before queue claims "
            "(error_type=%s): %s",
            type(exc).__name__,
            cause,
        )
        # The MESSAGE names the cause, scrubbed. The CHAIN is kept only
        # where the cause is sanitized BY CONSTRUCTION: a
        # WorkerClaimGuardError carries the scrubbed text and nothing else,
        # so standing behind the new error costs nothing and helps whoever
        # reads the traceback. Any other exception reaching this broad
        # `except` is a raw driver error, and `raise ... from exc` makes the
        # interpreter print its repr and traceback verbatim — a connection
        # string travels inside those (urls.py:493-497). Naming a cause and
        # printing it raw are two different things.
        raise RuntimeError(
            "Worker database runtime contract failed before queue claims: "
            f"{cause}"
        ) from (exc if isinstance(exc, WorkerClaimGuardError) else None)


_TRACE_RETENTION_INTERVAL_SECONDS = 6 * 3600.0
_TRACE_RETENTION_INITIAL_DELAY_SECONDS = 120.0
_DAILY_RETENTION_INTERVAL_SECONDS = 24 * 3600.0
_DAILY_RETENTION_INITIAL_DELAY_SECONDS = 300.0


def _start_retention_thread(
    *,
    name: str,
    label: str,
    stop_event: "threading.Event",
    run_pass: "Callable[[], int | None]",
    interval_seconds: float,
    initial_delay_seconds: float,
    event: str,
    retention_days: int,
) -> None:
    """Start ONE retention daemon thread (traces, audit, usage ledger).

    Deliberately NOT a ``_periodic_maintenance`` hook: maintenance runs
    synchronously on a claim thread, and a database or HTTP cleanup pass
    must never stall claims. A failing pass costs one WARNING, never the
    worker. Overlapping replicas are harmless — every pass is a DELETE
    (or a delete request) that is idempotent by construction.

    Args:
        run_pass: Executes one pass; may return a pruned-row count that
            is logged when non-zero.
    """

    def _run() -> None:
        delay = initial_delay_seconds
        while not stop_event.wait(delay):
            delay = interval_seconds
            try:
                pruned = run_pass()
                if pruned:
                    log.info(
                        "%s: deleted %d entries older than %d days.",
                        label,
                        pruned,
                        retention_days,
                        extra={
                            "event": event,
                            "pruned": pruned,
                            "retention_days": retention_days,
                        },
                    )
            except Exception:  # noqa: BLE001 — cleanup never kills the worker
                log.warning("%s pass failed.", label, exc_info=True)

    threading.Thread(target=_run, name=name, daemon=True).start()
    log.info(
        "%s enabled: every %.0fh, entries older than %d days.",
        label,
        interval_seconds / 3600,
        retention_days,
    )


def _start_trace_retention_thread(
    settings, stop_event: "threading.Event"
) -> None:
    """Langfuse trace retention — otlp mode with a positive day count."""
    observability = settings.observability
    days = int(observability.trace_retention_days or 0)
    if observability.tracing != "otlp" or days <= 0:
        return

    def _pass() -> None:
        from inqtrix.observability.trace_retention import run_trace_retention

        run_trace_retention(settings)

    _start_retention_thread(
        name="inqtrix-trace-retention",
        label="Trace-Retention",
        stop_event=stop_event,
        run_pass=_pass,
        interval_seconds=_TRACE_RETENTION_INTERVAL_SECONDS,
        initial_delay_seconds=_TRACE_RETENTION_INITIAL_DELAY_SECONDS,
        event="trace.retention.completed",
        retention_days=days,
    )


def _start_audit_retention_thread(
    settings, stop_event: "threading.Event", audit_sink
) -> None:
    """audit_log retention through the SECURITY DEFINER prune door."""
    days = int(settings.observability.audit_retention_days or 0)
    prune = getattr(audit_sink, "prune_audit_log", None)
    if days <= 0 or prune is None:
        return
    _start_retention_thread(
        name="inqtrix-audit-retention",
        label="Audit-Retention",
        stop_event=stop_event,
        run_pass=lambda: run_coro_sync(prune(days=days)),
        interval_seconds=_DAILY_RETENTION_INTERVAL_SECONDS,
        initial_delay_seconds=_DAILY_RETENTION_INITIAL_DELAY_SECONDS,
        event="audit.retention.completed",
        retention_days=days,
    )


def _start_usage_retention_thread(
    settings, stop_event: "threading.Event", usage_store
) -> None:
    """llm_usage retention through the SECURITY DEFINER prune door."""
    days = int(settings.observability.usage_retention_days or 0)
    prune = getattr(usage_store, "prune", None)
    if days <= 0 or prune is None:
        return
    _start_retention_thread(
        name="inqtrix-usage-retention",
        label="Usage-Retention",
        stop_event=stop_event,
        run_pass=lambda: run_coro_sync(prune(days=days)),
        interval_seconds=_DAILY_RETENTION_INTERVAL_SECONDS,
        initial_delay_seconds=_DAILY_RETENTION_INITIAL_DELAY_SECONDS,
        event="usage.retention.completed",
        retention_days=days,
    )


def _start_object_orphan_sweep_thread(
    settings, stop_event: "threading.Event", session_factory
) -> None:
    """Object-store/registry consistency sweep (files without a row)."""
    grace_hours = float(settings.storage.object_orphan_grace_hours or 0.0)
    if grace_hours <= 0 or session_factory is None:
        return
    if settings.storage.backend != "postgres":
        return

    def _pass() -> int:
        from inqtrix.server.container import build_object_store
        from inqtrix.storage.object_orphan_sweep import (
            sweep_orphaned_file_objects,
        )

        return sweep_orphaned_file_objects(
            object_store=build_object_store(settings),
            session_factory=session_factory,
            app_role=settings.storage.app_role,
            grace_seconds=grace_hours * 3600.0,
        )

    _start_retention_thread(
        name="inqtrix-object-orphan-sweep",
        label="Object orphan sweep",
        stop_event=stop_event,
        run_pass=_pass,
        interval_seconds=_DAILY_RETENTION_INTERVAL_SECONDS,
        initial_delay_seconds=_DAILY_RETENTION_INITIAL_DELAY_SECONDS,
        event="object_store.orphan_sweep.completed",
        retention_days=max(1, round(grace_hours / 24.0)),
    )


# Loop class name -> (display name, settings group, field name). Keyed by the
# class actually constructed, so a ceiling is only ever reported for a loop
# this process really runs. The environment-variable name is read from the
# field itself rather than spelled again here, or renaming the alias would
# leave this message pointing at a variable that no longer exists.
_DOMAIN_CEILINGS = {
    "IndexingWorkerLoop": ("Indexing", "knowledge", "reindex_max_concurrent"),
    "UploadWorkerLoop": ("Upload", "server", "upload_max_concurrent"),
    "DeletionWorkerLoop": ("Deletion", "server", "deletion_max_concurrent"),
}


def _ceiling_alias(group: object, field: str) -> str:
    """The environment-variable name a settings field is bound to."""
    return type(group).model_fields[field].alias or field


def _effective_loop_widths(settings: "Settings", loops: "list") -> "list[int]":
    """The width each constructed loop actually runs at.

    Not the process-wide value: a loop bound by its own domain ceiling has
    fewer jobs in flight and therefore asks for fewer connections. Counting
    the process-wide value for all of them would overstate the budget and
    warn on deployments that fit.
    """
    process_wide = settings.queue.worker_concurrency
    widths = []
    for loop in loops:
        entry = _DOMAIN_CEILINGS.get(type(loop).__name__)
        if entry is None:
            widths.append(process_wide)
            continue
        _, group_name, field = entry
        widths.append(
            min(process_wide, getattr(getattr(settings, group_name), field))
        )
    return widths


def _report_binding_ceilings(settings: "Settings", loops: "list") -> None:
    """Name every constructed loop whose ceiling sits below the worker-wide one.

    Each loop runs at the LOWER of ``INQTRIX_WORKER_CONCURRENCY`` and its
    domain ceiling. Raising only the former therefore leaves some loops
    where they were, which looks like the raise did not take effect.

    Only loops this process actually built are named: a worker without the
    knowledge profile runs no indexing loop, and reporting a ceiling for it
    would describe work that cannot happen here.
    """
    process_wide = settings.queue.worker_concurrency
    built = {type(loop).__name__ for loop in loops}
    binding = []
    for key, (name, group_name, field) in _DOMAIN_CEILINGS.items():
        if key not in built:
            continue
        group = getattr(settings, group_name)
        value = getattr(group, field)
        if value < process_wide:
            binding.append(f"{name} {value} ({_ceiling_alias(group, field)})")
    if binding:
        log.info(
            "Worker workload limits under INQTRIX_WORKER_CONCURRENCY=%d: %s. These "
            "loops use the lower value.",
            process_wide,
            ", ".join(binding),
        )


def main() -> None:
    """Run one worker process until SIGTERM/SIGINT."""
    logging_env = read_logging_env()
    configure_logging(
        enabled=logging_env.enabled,
        level=logging_env.level,
        console=logging_env.console,
        json_format=logging_env.json_format,
    )
    settings = Settings()
    # Same instance pepper as the API server: worker log lines must carry
    # the SAME subject pseudonyms, otherwise cross-process correlation
    # (the whole point of the pepper) breaks exactly where runs execute.
    from inqtrix.auth.log_redaction import configure_stable_pseudonyms

    configure_stable_pseudonyms(settings.auth.pseudonym_pepper)
    from inqtrix.observability.otel import setup_tracing

    setup_tracing(settings, service_role="worker")
    if settings.storage.backend != "postgres":
        raise RuntimeError(
            ("The worker requires INQTRIX_STORAGE_BACKEND=postgres; without durable job "
            "rows there is nothing to process.")
        )
    if settings.queue.backend != "valkey" or not settings.queue.valkey_url.strip():
        raise RuntimeError(
            ("The worker requires INQTRIX_QUEUE_BACKEND=valkey and INQTRIX_VALKEY_URL to"
            " be set.")
        )
    if not settings.storage.database_url.strip():
        raise RuntimeError(
            "INQTRIX_STORAGE_BACKEND=postgres requires INQTRIX_DATABASE_URL to be set."
        )

    database_claim_guard = _DatabaseClaimGuard(
        database_url=settings.storage.database_url,
        app_role=settings.storage.app_role,
        login_policy=settings.storage.runtime_login_policy,
    )
    _verified_database_claim_contract(database_claim_guard)

    worker_id = (
        f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
    )

    from inqtrix.storage.db import build_engine

    queue = ValkeyRunQueue(
        url=settings.queue.valkey_url, consumer=worker_id
    )
    store = PostgresRunStore(
        engine=build_engine(
            settings.storage.database_url,
            **settings.storage.pool_kwargs(),
        ),
        app_role=settings.storage.app_role,
        # queue=None here is CLAIM-MODE wiring (the ValkeyRunQueue goes
        # to the WorkerLoop, not the store), not in-process ownership —
        # without the explicit recover_orphans=False the store would
        # infer no-queue mode and blanket-fail every queued/running run
        # of the deployment on worker start ("Verwaister Run ...").
        # Crash recovery in queue mode is stream reclaim + the TTL
        # sweeps, never the orphan sweep.
        queue=None,
        # Submission and wake dispatch are separate from claim ownership:
        # children created inside a worker and parents woken by terminal
        # children must return to the shared stream, while queue=None above
        # remains the load-bearing claim-mode contract.
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=(
            settings.server.run_max_concurrent
            or settings.server.max_concurrent
        ),
        max_queue_size=settings.server.run_queue_max_size,
        # DURABLE retention (default 90d), NEVER the in-memory replay TTL
        # (run_completed_ttl_seconds, 300s): the worker's lazy cleanup
        # runs on every store access. Applying the short replay TTL here
        # would delete terminal reports, answers, and child runs after
        # only a few minutes.
        completed_ttl_seconds=settings.server.run_durable_retention_seconds,
        incognito_ttl_seconds=settings.server.run_incognito_retention_seconds,
        worker_id=worker_id,
        # The per-user cap is an ADMISSION bound firing in submit() --
        # and the worker DOES call submit(): a workspace agent running
        # here submits its child runs through this very store
        # (kernel/tools.py delegate paths -> run_service.submit). Child
        # runs count against the parent's user, so the cap governs
        # worker-side child submission exactly as it does API-side
        # submission. It does NOT re-check already-admitted runs the
        # worker merely claims and executes.
        max_concurrent_per_user=settings.server.run_max_concurrent_per_user,
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
        audit_service_starts=settings.observability.audit_service_starts,
    )
    # The reindex consumer runs in the SAME process when knowledge is
    # enabled: its store claims rows itself (queue=None worker-claim
    # mode, like the run store) over a separate reindex stream. Built
    # before the container so it can be injected (avoids the container
    # constructing an unused dispatch-mode store).
    index_store = None
    index_queue = None
    if settings.knowledge.enabled:
        from inqtrix.indexing_auto_resume import AutoResumePolicy
        from inqtrix.runs.indexing_postgres import PostgresIndexingJobStore
        from inqtrix.runs.indexing_queue import ValkeyIndexingQueue

        index_store = PostgresIndexingJobStore(
            engine=build_engine(
                settings.storage.database_url,
                **settings.storage.pool_kwargs(),
            ),
            app_role=settings.storage.app_role,
            # Claim-mode wiring like the run store above: never sweep.
            queue=None,
            recover_orphans=False,
            max_concurrent=settings.knowledge.reindex_max_concurrent,
            max_queue_size=settings.knowledge.reindex_queue_max_size,
            completed_ttl_seconds=(
                settings.knowledge.reindex_completed_ttl_seconds
            ),
            history_limit=settings.knowledge.reindex_history_limit,
            # Pauses happen here: the plan for their automatic resume too.
            auto_resume=AutoResumePolicy(
                max_attempts=settings.knowledge.reindex_auto_resume_attempts
            ),
            worker_id=worker_id,
            restrict_to_workspace_members=(
                settings.sharing.restrict_to_workspace_members
            ),
            sharing_enabled=settings.sharing.enabled,
        )
        index_queue = ValkeyIndexingQueue(
            url=settings.queue.valkey_url, consumer=worker_id
        )

    from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
    from inqtrix.runs.deletion_queue import ValkeyDeletionQueue

    deletion_store = PostgresDeletionOperationStore(
        engine=build_engine(
            settings.storage.database_url,
            **settings.storage.pool_kwargs(),
        ),
        app_role=settings.storage.app_role,
        queue=None,
        recover_orphans=False,
        max_concurrent=settings.server.deletion_max_concurrent,
        completed_ttl_seconds=(
            settings.server.deletion_receipt_retention_seconds
        ),
        # Same bound as the API: both processes must agree on when an
        # undispatchable operation is given up on, or one keeps reviving
        # what the other just failed.
        dispatch_timeout_seconds=(
            settings.server.deletion_dispatch_timeout_seconds
        ),
        worker_id=worker_id,
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
    )
    deletion_queue = ValkeyDeletionQueue(
        url=settings.queue.valkey_url, consumer=worker_id
    )

    from inqtrix.runs.upload_postgres import PostgresUploadOperationStore
    from inqtrix.runs.upload_queue import ValkeyUploadQueue

    upload_store = PostgresUploadOperationStore(
        engine=build_engine(
            settings.storage.database_url,
            **settings.storage.pool_kwargs(),
        ),
        app_role=settings.storage.app_role,
        queue=None,
        recover_orphans=False,
        worker_id=worker_id,
    )
    upload_queue = ValkeyUploadQueue(
        url=settings.queue.valkey_url, consumer=worker_id
    )

    providers = create_providers(settings)
    strategies = create_default_strategies(
        settings.agent,
        llm=providers.llm,
        claim_extract_model=resolve_claim_extract_model(
            providers.llm,
            fallback=settings.models.effective_claim_extract_model,
        ),
        claim_extract_timeout=settings.agent.claim_extract_timeout,
    )
    container = build_container(
        providers=providers,
        strategies=strategies,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(
            settings.server.max_concurrent
        ),
        run_store=store,
        indexing_store=index_store,
        deletion_store=deletion_store,
        upload_store=upload_store,
        # The workspace agent drives the permission/identity repositories
        # from sync worker threads via per-call asyncio.run; a pooled
        # asyncpg connection reused across those short-lived loops crashes
        # ("Future attached to a different loop"). NullPool makes the
        # shared platform engine loop-agnostic here (the API stays pooled).
        platform_persistence_null_pool=True,
    )
    indexing_authority = None
    if container.knowledge_service is not None:
        from inqtrix.services.execution_dependency_authority import (
            CollectionEditAuthorizer,
        )

        indexing_authority = CollectionEditAuthorizer(
            authorization=container.permission_service,
            knowledge_service=container.knowledge_service,
            user_lookup=container.run_user_lookup,
        )

    # The worker has no auth provider, so the container's quota service
    # (gated on oidc) is never built here. Build it directly when quotas
    # are enabled: the worker meters by the canonical user UUID persisted on
    # the job row, not by a live principal, so the auth-mode gate does not apply.
    # It records via the loop-agnostic NullPool store on the executor
    # thread (record_blocking) — never admits (admission stays in the API).
    quota_service = None
    if settings.quota.enabled:
        from inqtrix.services.quota_service import QuotaService
        from inqtrix.storage.quota_postgres import PostgresQuotaStore

        quota_service = QuotaService(
            store=PostgresQuotaStore(
                database_url=settings.storage.database_url,
                app_role=settings.storage.app_role,
            ),
            settings=settings.quota,
        )
    if container.asset_deletion_service is not None:
        container.asset_deletion_service.bind_quota_service(quota_service)
    if container.upload_operation_service is not None:
        container.upload_operation_service.bind_quota_service(quota_service)

    loops: list[BaseWorkerLoop] = [
        WorkerLoop(
            store=store,
            queue=queue,
            resolver=container.resolver,
            registry=container.registry,
            runtime=container.runtime,
            concurrency=settings.queue.worker_concurrency,
            max_attempts=settings.queue.worker_max_attempts,
            heartbeat_seconds=settings.queue.worker_heartbeat_seconds,
            claim_idle_seconds=settings.queue.worker_claim_idle_seconds,
            quota_service=quota_service,
            dependency_authorizer=container.run_service.dependency_authorizer,
            answer_publisher=container.run_service.answer_publisher,
            claim_guard=database_claim_guard,
        )
    ]
    if (
        index_store is not None
        and index_queue is not None
        and container.knowledge_service is not None
    ):
        loops.append(
            IndexingWorkerLoop(
                store=index_store,
                queue=index_queue,
                knowledge_service=container.knowledge_service,
                concurrency=min(
                    settings.queue.worker_concurrency,
                    settings.knowledge.reindex_max_concurrent,
                ),
                max_attempts=settings.queue.worker_max_attempts,
                heartbeat_seconds=settings.queue.worker_heartbeat_seconds,
                claim_idle_seconds=settings.queue.worker_claim_idle_seconds,
                quota_service=quota_service,
                authority=indexing_authority,
                claim_guard=database_claim_guard,
            )
        )
    else:
        # Visibility over a silent no-op: a knowledge-disabled worker
        # simply has no reindex stream to consume.
        log.info(
            "Worker %s: Knowledge engine disabled; no reindex consumer.",
            worker_id,
        )
    if container.asset_deletion_service is not None:
        from inqtrix.worker.deletion_loop import DeletionWorkerLoop

        loops.append(
            DeletionWorkerLoop(
                store=deletion_store,
                queue=deletion_queue,
                service=container.asset_deletion_service,
                concurrency=min(
                    settings.queue.worker_concurrency,
                    settings.server.deletion_max_concurrent,
                ),
                max_attempts=settings.queue.worker_max_attempts,
                heartbeat_seconds=settings.queue.worker_heartbeat_seconds,
                claim_idle_seconds=settings.queue.worker_claim_idle_seconds,
                claim_guard=database_claim_guard,
            )
        )
    if container.upload_operation_service is not None:
        from inqtrix.worker.upload_loop import UploadWorkerLoop

        loops.append(
            UploadWorkerLoop(
                store=upload_store,
                queue=upload_queue,
                service=container.upload_operation_service,
                concurrency=min(
                    settings.queue.worker_concurrency,
                    settings.server.upload_max_concurrent,
                ),
                max_attempts=settings.queue.worker_max_attempts,
                heartbeat_seconds=settings.queue.worker_heartbeat_seconds,
                claim_idle_seconds=settings.queue.worker_claim_idle_seconds,
                claim_guard=database_claim_guard,
            )
        )

    stop_event = threading.Event()
    loop_failure: list[BaseException] = []
    failure_lock = threading.Lock()

    def _request_stop(signum: int, _frame: object) -> None:
        log.info("Worker %s: received signal %d.", worker_id, signum)
        for active_loop in loops:
            active_loop.request_stop()
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    def _run_claim_loop(active_loop: BaseWorkerLoop) -> None:
        try:
            active_loop.run_forever()
        except BaseException as exc:
            with failure_lock:
                loop_failure.append(exc)
            log.error(
                "Worker database/claim contract failed; stopping all claims "
                "(error_type=%s)",
                type(exc).__name__,
            )
            for loop_to_stop in loops:
                loop_to_stop.request_stop()
            stop_event.set()

    log.info(
        "Inqtrix worker starting | worker_id=%s | loops=%d "
        "| worker_concurrency=%d "
        "| max_attempts=%d | heartbeat=%.0fs | claim_idle=%.0fs",
        worker_id,
        len(loops),
        settings.queue.worker_concurrency,
        settings.queue.worker_max_attempts,
        settings.queue.worker_heartbeat_seconds,
        settings.queue.worker_claim_idle_seconds,
    )
    # A domain ceiling that binds below the process-wide one changes how much
    # work this replica actually does, so it is said rather than inferred from
    # throughput later.
    _report_binding_ceilings(settings, loops)
    # The engines exist by now, so the budget comes from what was built
    # rather than from a count kept by hand alongside them.
    from inqtrix.storage.connection_budget import report_connection_budget

    run_coro_sync(
        report_connection_budget(
            database_url=settings.storage.database_url,
            process_label="worker process",
            pool_size=settings.storage.pool_size,
            pool_max_overflow=settings.storage.pool_max_overflow,
            # Every loop this process runs drives a NullPool bundle: one
            # connection per operation, none while a job waits. They hold
            # nothing at rest, so no pool count sees them -- but each loop
            # can ask for one per job it has in flight, and the sum is what
            # the server has to survive. The ceilings are the effective
            # ones, so a bound loop is counted at its real width.
            # The worker executes agent runs too: the checkpointer's own
            # psycopg pool holds connections no engine count can see --
            # same line item the API process reports.
            extra_connections=(
                container.agent_checkpointer.max_connections
                if container.agent_checkpointer is not None
                else 0
            ),
            extra_label="agent checkpointer",
            transient_peak=sum(_effective_loop_widths(settings, loops)),
            transient_label="worker loops, NullPool",
            # In the worker the run lane is sized by execution, not
            # admission: RUN_MAX_CONCURRENT never fires here.
            transient_knob="INQTRIX_WORKER_CONCURRENCY",
        )
    )
    # Metrics holder BEFORE the claim loops start: jobs claimed in the
    # startup window (including the immediate crash-recovery drain)
    # would otherwise record nothing.
    from inqtrix.worker.metrics import start_worker_metrics

    start_worker_metrics(settings)
    # Usage-ledger recorder — same startup-window rule as the metrics
    # holder; the shutdown path below flushes the last buffer.
    from inqtrix.usage.recorder import install_usage_recorder

    usage_recorder = install_usage_recorder(settings)
    threads = [
        threading.Thread(
            target=_run_claim_loop,
            args=(active_loop,),
            name=f"inqtrix-claim-{index}",
            daemon=True,
        )
        for index, active_loop in enumerate(loops)
    ]
    for thread in threads:
        thread.start()
    _start_trace_retention_thread(settings, stop_event)
    _start_audit_retention_thread(
        settings, stop_event, container.permission_service.audit_sink
    )
    _start_usage_retention_thread(settings, stop_event, usage_recorder.store)
    _start_object_orphan_sweep_thread(
        settings, stop_event, container.session_factory
    )
    # The signal handler must run on the main thread; block here until it
    # fires, then let each loop's run_forever observe its stop flag.
    stop_event.wait()
    for thread in threads:
        thread.join(timeout=_DRAIN_SECONDS)

    drained = all(active_loop.drain(_DRAIN_SECONDS) for active_loop in loops)
    log.info(
        "Inqtrix worker stopping | worker_id=%s | drained=%s",
        worker_id,
        drained,
    )
    # Flush the last span batch before exit (BatchSpanProcessor would
    # otherwise drop it — the documented SIGTERM span-loss window).
    from inqtrix.observability.otel import shutdown_tracing

    shutdown_tracing()
    # Same for the last ledger buffer (fail-safe: close never raises out
    # of the flusher; the os._exit paths below may still drop seconds).
    from inqtrix.usage.recorder import set_active_usage_recorder

    usage_recorder.close()
    set_active_usage_recorder(None)
    _aclose_usage_store = getattr(usage_recorder.store, "aclose", None)
    if callable(_aclose_usage_store):
        # Its NullPool engine must be disposed like every other store's
        # (the flusher's throwaway loops cannot do it).
        asyncio.run(_aclose_usage_store())
    if quota_service is not None:
        asyncio.run(quota_service.aclose())
    if loop_failure and not drained:
        os._exit(1)
    if loop_failure:
        # The claim loop's own exception was captured on its thread; it is
        # the only thing that says WHAT failed. Naming the generic contract
        # and dropping it left the operator with a shutdown notice and no
        # diagnosis.
        from inqtrix.urls import sanitize_log_message

        raise RuntimeError(
            "Worker claim contract failed; in-flight jobs were drained and "
            "the process must be restarted after deployment repair: "
            f"{sanitize_log_message(loop_failure[0])}"
            # The MESSAGE names the cause, scrubbed. The CHAIN stays cut:
            # `raise ... from exc` makes the interpreter print the cause's
            # own repr and traceback above this one, and a driver exception
            # carries the DSN it failed to connect with (urls.py:493-497).
            # Naming the cause and printing it raw are two different things;
            # only the first is owed to the reader.
        ) from None
    if not drained:
        # Executor threads are non-daemon and would block interpreter
        # exit for up to the full job duration; the orchestrator's
        # SIGKILL window plus stream redelivery is the designed
        # recovery path, so leave decisively.
        os._exit(0)


if __name__ == "__main__":
    main()
