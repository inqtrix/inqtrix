"""Valkey stream dedicated to durable aggregate deletions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inqtrix.runs.stream_queue import StreamJobQueue

DELETION_STREAM = "inqtrix:deletion:jobs"
DELETION_DEAD_STREAM = "inqtrix:deletion:dead"
GROUP = "workers"


@dataclass(frozen=True)
class QueuedDeletionOperation:
    message_id: str
    operation_id: str
    tenant_id: str
    delivery_count: int


class ValkeyDeletionQueue(StreamJobQueue[QueuedDeletionOperation]):
    def __init__(
        self,
        *,
        url: str = "",
        consumer: str = "",
        stream: str = DELETION_STREAM,
        group: str = GROUP,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            stream=stream,
            dead_stream=DELETION_DEAD_STREAM,
            group=group,
            entity_field="operation_id",
            url=url,
            consumer=consumer,
            client=client,
        )

    def enqueue(self, *, operation_id: str, tenant_id: str) -> None:
        self._enqueue(entity_id=operation_id, tenant_id=tenant_id)

    def _make_job(
        self,
        *,
        message_id: str,
        entity_id: str,
        tenant_id: str,
        delivery_count: int,
    ) -> QueuedDeletionOperation:
        return QueuedDeletionOperation(
            message_id=message_id,
            operation_id=entity_id,
            tenant_id=tenant_id,
            delivery_count=delivery_count,
        )
