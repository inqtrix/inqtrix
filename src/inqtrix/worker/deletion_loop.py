"""Claim loop for durable aggregate deletion operations."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from inqtrix.runs.deletion_execution import execute_claimed_deletion
from inqtrix.worker.loop import BaseWorkerLoop, _RECONCILE_MIN_AGE_SECONDS

if TYPE_CHECKING:
    from inqtrix.runs.deletion_postgres import (
        ClaimedDeletionOperation,
        PostgresDeletionOperationStore,
    )
    from inqtrix.runs.deletion_queue import (
        QueuedDeletionOperation,
        ValkeyDeletionQueue,
    )
    from inqtrix.services.asset_deletion_service import AssetDeletionService

log = logging.getLogger("inqtrix")


class DeletionWorkerLoop(
    BaseWorkerLoop["QueuedDeletionOperation", "ClaimedDeletionOperation"]
):
    def __init__(
        self,
        *,
        store: "PostgresDeletionOperationStore",
        queue: "ValkeyDeletionQueue",
        service: "AssetDeletionService",
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
            thread_prefix="inqtrix-delete",
        )
        self._service = service

    def _entity_id(self, job: "QueuedDeletionOperation") -> str:
        return job.operation_id

    def _stale_dispatch(self) -> list[tuple[str, str]]:
        return self._store.stale_queued_operations(
            older_than_seconds=_RECONCILE_MIN_AGE_SECONDS
        )

    def _cancel_requested(self, watched: dict[str, str]) -> set[str]:
        return self._store.cancel_requested_operations(watched)

    def _enqueue_dispatch(self, entity_id: str, tenant_id: str) -> None:
        self._queue.enqueue(operation_id=entity_id, tenant_id=tenant_id)

    def _execute(
        self,
        job: "QueuedDeletionOperation",
        claimed: "ClaimedDeletionOperation",
        cancel_event: threading.Event,
    ) -> None:
        del cancel_event  # deletion is intentionally not reversible mid-saga
        old_message_acked = False
        try:
            handle = execute_claimed_deletion(self._store, self._service, claimed)
            if handle.terminal_landed:
                self._queue.ack(job.message_id)
                old_message_acked = True
            else:
                log.warning(
                    "Worker %s: deletion operation %s was fenced by a newer attempt; "
                    "the message remains with the new owner.",
                    self._store.worker_id,
                    job.operation_id,
                )
        except Exception as exc:
            log.error(
                "Worker %s: terminal phase for deletion operation %s failed; redelivery"
                " remains responsible (error_type=%s).",
                self._store.worker_id,
                job.operation_id,
                type(exc).__name__,
            )
        finally:
            self._finish_active(job, allow_successor=old_message_acked)
