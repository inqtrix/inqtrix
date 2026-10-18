"""In-memory file registry: the no-infrastructure default."""

from __future__ import annotations

import uuid
import threading

from inqtrix.content.ports import FileNotFound, FileRecord


class MemoryFileRegistry:
    """Dictionary-backed registry (process-local, lost on restart).

    The deployment default — consistent with the run store and the
    identity backend. Operators wanting durable file metadata switch
    ``INQTRIX_STORAGE_BACKEND=postgres``; blobs written by the local
    object store survive restarts either way, orphaned blobs of a
    memory-mode registry are a documented dev-mode trade-off.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, FileRecord] = {}

    async def create(self, record: FileRecord) -> None:
        """Store one record (ids are generated unique by the service)."""
        with self._lock:
            self._records[record.id] = record

    async def get(self, file_id: str, *, tenant_id: str) -> FileRecord:
        """Return the record; unknown ids and foreign tenants are
        indistinguishably absent."""
        with self._lock:
            record = self._records.get(file_id)
            if record is None or record.tenant_id != tenant_id:
                raise FileNotFound(file_id)
            return record

    async def list(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[FileRecord]:
        """Newest-first listing with tenant/owner/namespace facets."""
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and (owner_user_id is None or record.owner_user_id == owner_user_id)
                and (workspace_id is None or record.workspace_id == workspace_id)
            ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def delete(self, file_id: str, *, tenant_id: str) -> FileRecord:
        """Remove and return the record or raise :class:`FileNotFound`."""
        with self._lock:
            record = self._records.get(file_id)
            if record is None or record.tenant_id != tenant_id:
                raise FileNotFound(file_id)
            del self._records[file_id]
            return record
