"""Valkey-Streams job queue for native run dispatch.

The queue carries only dispatch messages (``run_id`` + ``tenant_id``);
the Postgres run row stays the source of truth, so a lost message is
recoverable (the worker's reconciler re-enqueues stale queued rows)
and a duplicated message is harmless (the guarded claim makes
redelivery idempotent).

Stream layout (verified against the Valkey 9.x docs, 2026-06):

* ``inqtrix:runs:jobs`` — one consumer group ``workers``; NEVER
  trimmed with MAXLEN (XAUTOCLAIM silently drops trimmed entries from
  the pending list, which would lose jobs); entries are XACKed and
  XDELed after the run reaches a terminal state.
* ``inqtrix:runs:dead`` — dead-letter stream for jobs that exceeded
  the delivery budget; carries the original payload plus diagnosis.

The XADD/claim/reclaim/heartbeat/dead-letter mechanics live in
:class:`~inqtrix.runs.stream_queue.StreamJobQueue` (shared with the
reindex-job queue); this class only fixes the run stream names, the
``run_id`` payload field, and the :class:`QueuedJob` dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inqtrix.runs.stream_queue import StreamJobQueue

JOB_STREAM = "inqtrix:runs:jobs"
DEAD_STREAM = "inqtrix:runs:dead"
GROUP = "workers"


@dataclass(frozen=True)
class QueuedJob:
    """One claimed dispatch message.

    Attributes:
        message_id: Stream entry id — doubles as the ack handle.
        run_id: Run to execute.
        tenant_id: Tenant the run row lives in (needed before the row
            can be read under row-level security).
        delivery_count: How often this entry has been delivered;
            ``> 1`` means redelivery (crash recovery or reclaim).
    """

    message_id: str
    run_id: str
    tenant_id: str
    delivery_count: int


class ValkeyRunQueue(StreamJobQueue[QueuedJob]):
    """Run-dispatch queue over the shared stream-queue mechanics.

    Args:
        url: Valkey connection URL (``redis://`` scheme).
        consumer: Consumer name within the worker group — the worker
            id. May stay empty for enqueue-only callers (the API
            process never reads the stream).
        stream: Job stream key; override only in tests.
        group: Consumer group name; override only in tests.
        client: Pre-built client (tests inject ``fakeredis`` here);
            ``None`` builds one from *url*.
    """

    def __init__(
        self,
        *,
        url: str = "",
        consumer: str = "",
        stream: str = JOB_STREAM,
        group: str = GROUP,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            stream=stream,
            dead_stream=DEAD_STREAM,
            group=group,
            entity_field="run_id",
            url=url,
            consumer=consumer,
            client=client,
        )

    def enqueue(self, *, run_id: str, tenant_id: str) -> None:
        """Append one dispatch message for *run_id*."""
        self._enqueue(entity_id=run_id, tenant_id=tenant_id)

    def _make_job(
        self,
        *,
        message_id: str,
        entity_id: str,
        tenant_id: str,
        delivery_count: int,
    ) -> QueuedJob:
        return QueuedJob(
            message_id=message_id,
            run_id=entity_id,
            tenant_id=tenant_id,
            delivery_count=delivery_count,
        )
