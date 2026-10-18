"""Valkey-Streams job queue for durable reindex dispatch.

The reindex twin of :mod:`inqtrix.runs.valkey_queue`: a SEPARATE stream
and consumer group from the run queue (a run worker must never claim a
reindex entry it cannot execute), over the shared
:class:`~inqtrix.runs.stream_queue.StreamJobQueue` mechanics. The
payload carries only ``job_id`` + ``tenant_id``; everything else
(collection, embedding model, submitter) rebuilds from the durable
``indexing_jobs`` row, so the closure never has to cross the process
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inqtrix.runs.stream_queue import StreamJobQueue

INDEX_JOB_STREAM = "inqtrix:index:jobs"
INDEX_DEAD_STREAM = "inqtrix:index:dead"
GROUP = "workers"


@dataclass(frozen=True)
class QueuedIndexingJob:
    """One claimed reindex-dispatch message.

    Attributes:
        message_id: Stream entry id — doubles as the ack handle.
        job_id: Reindex job to execute.
        tenant_id: Tenant the job row lives in (needed before the row
            can be read under row-level security).
        delivery_count: How often this entry has been delivered;
            ``> 1`` means redelivery (crash recovery or reclaim).
    """

    message_id: str
    job_id: str
    tenant_id: str
    delivery_count: int


class ValkeyIndexingQueue(StreamJobQueue[QueuedIndexingJob]):
    """Reindex-dispatch queue over the shared stream-queue mechanics.

    Args:
        url: Valkey connection URL (``redis://`` scheme).
        consumer: Consumer name within the worker group — the worker id.
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
        stream: str = INDEX_JOB_STREAM,
        group: str = GROUP,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            stream=stream,
            dead_stream=INDEX_DEAD_STREAM,
            group=group,
            entity_field="job_id",
            url=url,
            consumer=consumer,
            client=client,
        )

    def enqueue(self, *, job_id: str, tenant_id: str) -> None:
        """Append one dispatch message for *job_id*."""
        self._enqueue(entity_id=job_id, tenant_id=tenant_id)

    def _make_job(
        self,
        *,
        message_id: str,
        entity_id: str,
        tenant_id: str,
        delivery_count: int,
    ) -> QueuedIndexingJob:
        return QueuedIndexingJob(
            message_id=message_id,
            job_id=entity_id,
            tenant_id=tenant_id,
            delivery_count=delivery_count,
        )
