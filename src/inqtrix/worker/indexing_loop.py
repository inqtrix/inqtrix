"""Worker loop for durable knowledge-indexing operations.

The indexing twin of :class:`~inqtrix.worker.loop.WorkerLoop`. It reuses
the entire generic claim/fence/heartbeat/reclaim/reconcile/cancel
machinery in :class:`~inqtrix.worker.loop.BaseWorkerLoop` and supplies
only operation-specific dispatch, stale/cancel store calls, and execution of
either an isolated collection generation or an immutable document revision
against a fenced job handle.

The worker has no live principal, so it meters provider work against the
``QuotaSubject`` reconstructed from the persisted ``created_by_*``
attribution on the claimed row — exactly as the run worker does.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import TYPE_CHECKING, Callable

from inqtrix.auth.principal import Principal
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.execution_failures import classify_execution_failure
from inqtrix.indexing_failures import IndexingDependencyError
from inqtrix.knowledge.contextualize import (
    ContextualizationDependencyError,
    ContextualizationValidationError,
)
from inqtrix.knowledge.stores.ports import (
    GenerationValidationError,
    IndexGenerationSuperseded,
)
from inqtrix.quota.models import QuotaSubject
from inqtrix.server.indexing import (
    FencedIndexingJobHandle,
    IndexingOperationKind,
)
from inqtrix.services.indexing_service import execute_indexing_operation
from inqtrix.sync_bridge import run_coro_sync
from inqtrix.urls import sanitize_error
from inqtrix.worker.loop import _RECONCILE_MIN_AGE_SECONDS, BaseWorkerLoop

if TYPE_CHECKING:
    from inqtrix.runs.indexing_postgres import (
        ClaimedIndexingJob,
        PostgresIndexingJobStore,
    )
    from inqtrix.runs.indexing_queue import QueuedIndexingJob, ValkeyIndexingQueue
    from inqtrix.services.execution_dependency_authority import CollectionEditAuthorizer
    from inqtrix.services.knowledge_service import KnowledgeService
    from inqtrix.services.quota_service import QuotaService

log = logging.getLogger("inqtrix")


class IndexingWorkerLoop(BaseWorkerLoop["QueuedIndexingJob", "ClaimedIndexingJob"]):
    """Claim-and-execute loop for durable indexing operations.

    Args:
        store: Postgres indexing-job store (claims, events, terminal
            writes).
        queue: Indexing Valkey queue bound to this worker's consumer name.
        knowledge_service: The collection/document service whose store
            and preparation pipeline the worker drives.
        concurrency: Parallel indexing operations in this process.
        max_attempts: Delivery budget before dead-lettering.
        heartbeat_seconds: Idle-reset interval for in-flight entries.
        claim_idle_seconds: Reclaim threshold for entries whose owner
            stopped heartbeating.
        quota_service: Optional usage meter for incremental
            embedding-token accounting (keyed by the persisted user UUID).
    """

    def __init__(
        self,
        *,
        store: "PostgresIndexingJobStore",
        queue: "ValkeyIndexingQueue",
        knowledge_service: "KnowledgeService",
        concurrency: int,
        max_attempts: int,
        heartbeat_seconds: float,
        claim_idle_seconds: float,
        quota_service: "QuotaService | None" = None,
        authority: "CollectionEditAuthorizer | None" = None,
        claim_guard: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            store=store,
            queue=queue,
            concurrency=concurrency,
            max_attempts=max_attempts,
            heartbeat_seconds=heartbeat_seconds,
            claim_idle_seconds=claim_idle_seconds,
            claim_guard=claim_guard,
            thread_prefix="inqtrix-reindex",
        )
        self._knowledge_service = knowledge_service
        self._quota_service = quota_service
        self._authority = authority

    def _entity_id(self, job: "QueuedIndexingJob") -> str:
        return job.job_id

    def _stale_dispatch(self) -> list[tuple[str, str]]:
        return self._store.stale_queued_jobs(
            older_than_seconds=_RECONCILE_MIN_AGE_SECONDS
        )

    def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        return self._store.cancel_requested_jobs(watched)

    def _enqueue_dispatch(self, entity_id: str, tenant_id: str) -> None:
        self._queue.enqueue(job_id=entity_id, tenant_id=tenant_id)

    def _periodic_maintenance(self) -> None:
        report = run_coro_sync(self._knowledge_service.prune_expired_generations_all())
        if report["collections"] or report["chunks"]:
            log.info(
                "Generation retention removed %d chunks across %d collections",
                report["chunks"],
                report["collections"],
                extra={
                    "event": "knowledge.generation.retention.completed",
                    "collection_count": report["collections"],
                    "chunk_count": report["chunks"],
                },
            )

    def _execute(
        self,
        job: "QueuedIndexingJob",
        claimed: "ClaimedIndexingJob",
        cancel_event: threading.Event,
    ) -> None:
        if claimed.cancel_requested:
            cancel_event.set()
        handle = FencedIndexingJobHandle(
            self._store, job.job_id, cancel_event, claimed.attempt
        )
        # Root span + log correlation for the whole indexing segment;
        # embedding spans (provider tracing wrapper) nest under it. Worker
        # threads are reused — both undone in the outer finally.
        from contextlib import ExitStack as _ExitStack

        from inqtrix.observability import semconv
        from inqtrix.observability.context import (
            bind_log_context,
            reset_log_context,
        )
        from inqtrix.observability.otel import operation_span

        # Setup INSIDE the try so a telemetry failure never skips the
        # finally (_finish_active); a leaked slot would wedge the worker.
        telemetry_stack = _ExitStack()
        log_tokens: dict = {}
        old_message_acked = False
        try:
            telemetry_stack.enter_context(
                operation_span(
                    "inqtrix.indexing",
                    {
                        semconv.INQTRIX_RUN_ID: job.job_id,
                        semconv.INQTRIX_TENANT: job.tenant_id,
                        semconv.INQTRIX_ATTEMPT: claimed.attempt,
                        semconv.LANGFUSE_TRACE_NAME: "indexing",
                    },
                )
            )
            log_tokens = bind_log_context(
                run_id=job.job_id, tenant=job.tenant_id
            )
            from inqtrix.observability.context import bind_feature

            bind_feature("indexing")
            try:
                # Reconstruct quota attribution from the persisted canonical
                # user UUID — the worker has no live principal, but the
                # embedding-token spend must still count toward the
                # submitter's monthly quota.
                quota_subject: QuotaSubject | None = None
                principal: Principal | None = None
                actor_user_id: uuid.UUID | None = None
                has_user = claimed.created_by_user_id is not None
                has_tenant = claimed.created_by_tenant_id is not None
                if has_user != has_tenant:
                    raise AuthorizationRevoked(
                        "indexing job has incomplete requester attribution"
                    )
                if has_user:
                    if not claimed.created_by_tenant_id:
                        raise AuthorizationRevoked(
                            "indexing job has incomplete requester attribution"
                        )
                    actor_user_id = uuid.UUID(str(claimed.created_by_user_id))
                    principal = Principal(
                        user_id=actor_user_id,
                        kind="oidc_session",
                        tenant_id=claimed.created_by_tenant_id,
                        role="member",
                    )
                    if self._quota_service is not None:
                        quota_subject = QuotaSubject(
                            tenant_id=claimed.created_by_tenant_id,
                            user_id=actor_user_id,
                        )

                def _check_authority() -> None:
                    if self._authority is not None:
                        self._authority.check(
                            claimed.collection_id,
                            principal,
                        )

                execute_indexing_operation(
                    handle,
                    knowledge_service=self._knowledge_service,
                    operation_kind=claimed.operation_kind.value,
                    collection_id=claimed.collection_id,
                    embedding_model=claimed.embedding_model,
                    generation_id=claimed.generation_id,
                    document_id=claimed.document_id,
                    revision_id=claimed.revision_id,
                    quota_service=self._quota_service,
                    quota_subject=quota_subject,
                    authority_check=_check_authority,
                    actor_user_id=actor_user_id,
                    tenant_id=claimed.created_by_tenant_id,
                )
                if (
                    handle.cancelled
                    and claimed.operation_kind
                    == IndexingOperationKind.COLLECTION_GENERATION
                ):
                    run_coro_sync(
                        self._knowledge_service.discard_generation(
                            collection_id=claimed.collection_id,
                            generation_id=claimed.generation_id,
                            fence_job_id=job.job_id,
                            fence_attempt=claimed.attempt,
                            actor_user_id=actor_user_id,
                        )
                    )
            except ContextualizationDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except IndexingDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except ContextualizationValidationError as exc:
                handle.pause_validation(str(exc))
            except GenerationValidationError as exc:
                handle.pause_validation(str(exc))
            except IndexGenerationSuperseded:
                # A successor attempt owns the same generation. The stale
                # worker must neither clean its staging nor write terminal
                # state; both would corrupt the successor's work.
                log.warning(
                    "Worker %s lost the publication fence for indexing job %s",
                    self._store.worker_id,
                    job.job_id,
                )
            except Exception as exc:  # noqa: BLE001 — terminal-write then ack
                log.error(
                    "Indexing worker job %s failed (error_type=%s)",
                    job.job_id,
                    type(exc).__name__,
                )
                if (
                    claimed.operation_kind
                    == IndexingOperationKind.COLLECTION_GENERATION
                ):
                    try:
                        run_coro_sync(
                            self._knowledge_service.discard_generation(
                                collection_id=claimed.collection_id,
                                generation_id=claimed.generation_id,
                                fence_job_id=job.job_id,
                                fence_attempt=claimed.attempt,
                                actor_user_id=actor_user_id,
                            )
                        )
                    except Exception as cleanup_exc:  # noqa: BLE001 - terminal failure visible
                        log.error(
                            "Indexing job %s staging cleanup failed "
                            "(error_type=%s)",
                            job.job_id,
                            type(cleanup_exc).__name__,
                        )
                failure_code = classify_execution_failure(exc)
                handle.fail(sanitize_error(exc), error_type=failure_code)
                # The root span is still current here (the telemetry
                # stack closes in the outer finally). Without this a
                # failed indexing job renders as a clean span in the
                # waterfall — the run path marks its span the same way.
                from inqtrix.observability.otel import (
                    enrich_current_span,
                    mark_current_span_error,
                )

                mark_current_span_error(failure_code)
                enrich_current_span({semconv.INQTRIX_OUTCOME: semconv.OUTCOME_FAILED})
            if handle.terminal_landed:
                # Terminal state is committed; only now may the stream
                # forget the job.
                self._queue.ack(job.message_id)
                old_message_acked = True
                _count_worker_job("indexing", "terminal")
            else:
                # Fenced out: a superseding attempt owns the job AND this
                # very message id — acking here would strip the new
                # owner's crash-recovery entry.
                log.warning(
                    "Worker %s: indexing job %s was taken over during "
                    "execution; its result was fenced and the dispatch "
                    "remains with the new owner.",
                    self._store.worker_id,
                    job.job_id,
                )
                _count_worker_job("indexing", "fenced")
        except Exception as exc:  # noqa: BLE001 — Futures here are unobserved
            log.error(
                "Worker %s: terminal phase for indexing job %s failed; "
                "redelivery remains responsible (error_type=%s).",
                self._store.worker_id,
                job.job_id,
                type(exc).__name__,
            )
            _count_worker_job("indexing", "finalization_failed")
        finally:
            reset_log_context(log_tokens)
            _clear_feature_after_segment()
            telemetry_stack.close()
            self._finish_active(job, allow_successor=old_message_acked)


def _clear_feature_after_segment() -> None:
    """Reused threads must not leak feature label or ledger subject."""
    from inqtrix.observability.context import (
        clear_feature,
        clear_usage_subject,
    )

    clear_feature()
    clear_usage_subject()


def _count_worker_job(loop_name: str, outcome: str) -> None:
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is not None:
        metrics.count_worker_job(loop=loop_name, outcome=outcome)
