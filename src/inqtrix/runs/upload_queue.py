"""Valkey stream dedicated to durable original-file uploads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inqtrix.runs.stream_queue import StreamJobQueue

UPLOAD_STREAM = "inqtrix:upload:jobs"
UPLOAD_DEAD_STREAM = "inqtrix:upload:dead"
GROUP = "workers"


@dataclass(frozen=True)
class QueuedUploadOperation:
    message_id: str
    operation_id: str
    tenant_id: str
    delivery_count: int


class ValkeyUploadQueue(StreamJobQueue[QueuedUploadOperation]):
    def __init__(
        self,
        *,
        url: str = "",
        consumer: str = "",
        stream: str = UPLOAD_STREAM,
        group: str = GROUP,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            stream=stream,
            dead_stream=UPLOAD_DEAD_STREAM,
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
    ) -> QueuedUploadOperation:
        return QueuedUploadOperation(
            message_id=message_id,
            operation_id=entity_id,
            tenant_id=tenant_id,
            delivery_count=delivery_count,
        )
