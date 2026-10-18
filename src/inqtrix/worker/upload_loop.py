"""Claim loop for durable original-file upload recovery."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from inqtrix.runs.upload_operations import UploadAttemptSuperseded
from inqtrix.services.upload_operation_service import (
    UploadBytesRequired,
    UploadExecutionDeferred,
)
from inqtrix.worker.loop import (
    _RECONCILE_MIN_AGE_SECONDS,
    BaseWorkerLoop,
    _count_worker_job,
)

if TYPE_CHECKING:
    from inqtrix.runs.upload_postgres import (
        ClaimedUploadOperation,
        PostgresUploadOperationStore,
    )
    from inqtrix.runs.upload_queue import QueuedUploadOperation, ValkeyUploadQueue
    from inqtrix.services.upload_operation_service import UploadOperationService

log = logging.getLogger("inqtrix")


class UploadWorkerLoop(
    BaseWorkerLoop["QueuedUploadOperation", "ClaimedUploadOperation"]
):
    def __init__(
        self,
        *,
        store: "PostgresUploadOperationStore",
        queue: "ValkeyUploadQueue",
        service: "UploadOperationService",
        concurrency: int,
        max_attempts: int,
        heartbeat_seconds: float,
        claim_idle_seconds: float,
        claim_guard=None,
    ) -> None:
        super().__init__(
            store=store,
            queue=queue,
            concurrency=concurrency,
            max_attempts=max_attempts,
            heartbeat_seconds=heartbeat_seconds,
            claim_idle_seconds=claim_idle_seconds,
            claim_guard=claim_guard,
            thread_prefix="inqtrix-upload",
        )
        self._service = service

    def _entity_id(self, job: "QueuedUploadOperation") -> str:
        return job.operation_id

    def _stale_dispatch(self) -> list[tuple[str, str]]:
        # Uploads execute here, so preparations skipped for want of a parser
        # are queued and dispatched here too; failing that never blocks the
        # stale pass below.
        try:
            for operation_id, tenant_id in self._service.queue_missing_preparations():
                self._enqueue_dispatch(operation_id, tenant_id)
        except Exception as exc:
            log.warning(
                "Missing upload preparations could not be queued (error_type=%s).",
                type(exc).__name__,
            )
        return self._store.stale_dispatches(
            older_than_seconds=_RECONCILE_MIN_AGE_SECONDS
        )

    def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        del watched
        return set()

    def _enqueue_dispatch(self, entity_id: str, tenant_id: str) -> None:
        self._queue.enqueue(operation_id=entity_id, tenant_id=tenant_id)

    def _execute(
        self,
        job: "QueuedUploadOperation",
        claimed: "ClaimedUploadOperation",
        cancel_event: threading.Event,
    ) -> None:
        del cancel_event
        ack = False
        try:
            try:
                self._service.execute_claimed(claimed)
            except (UploadBytesRequired, UploadExecutionDeferred):
                # Both states were persisted before control reached here.
                pass
            except UploadAttemptSuperseded:
                # The message now belongs to the newer attempt's owner —
                # acking OUR id would steal its crash-recovery entry.
                log.info(
                    "Upload operation %s was taken over by a newer attempt.",
                    job.operation_id,
                )
                return
            except Exception as exc:
                # The service persists a typed failure or retry before raising.
                log.warning(
                    "Upload operation %s ended with a visible error (error_type=%s).",
                    job.operation_id,
                    type(exc).__name__,
                )
            if self._store.is_attempt_current(
                job.operation_id,
                tenant_id=job.tenant_id,
                fence_attempt=claimed.attempt,
            ):
                # No terminal/retry state landed for this attempt: the
                # row is still running under this claim. Acking would
                # orphan it until the stale-row reconciler — leave the
                # message for redelivery instead.
                log.warning(
                    "Upload operation %s has no persisted terminal state; the message "
                    "remains available for redelivery.",
                    job.operation_id,
                )
                return
            self._queue.ack(job.message_id)
            ack = True
        except Exception as final_exc:  # noqa: BLE001 — Futures here are unobserved
            log.error(
                "Worker %s: terminal phase for upload operation %s failed "
                "(error_type=%s); redelivery remains responsible.",
                self._store.worker_id,
                job.operation_id,
                type(final_exc).__name__,
            )
            _count_worker_job("uploads", "finalization_failed")
        finally:
            self._finish_active(job, allow_successor=ack)
