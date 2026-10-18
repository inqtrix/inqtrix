"""File-registry port and record types for the content layer."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class FileNotFound(KeyError):
    """Raised when a file id does not exist *for this principal*.

    Mirrors the platform hiding rule: absence and denied access are
    indistinguishable — callers map this to HTTP 404.
    """


@dataclass(frozen=True)
class FileRecord:
    """Metadata of one uploaded file (the bytes live in the object store).

    Attributes:
        id: Server-assigned stable identifier (``fl_...``).
        tenant_id: Tenant scope (uniform with every platform table).
        owner_user_id: Canonical local user UUID that uploaded the file — the
            authorization anchor for the creator-access rule.
        workspace_id: Optional client-side UI namespace (NOT an
            authorization input — same semantics as on runs).
        file_name: Original client filename, display only. Never used
            to build storage paths.
        content_type: Declared MIME type, echoed on download.
        size_bytes: Exact byte count, measured server-side while
            spooling (never trusted from the client).
        sha256: Content hash, computed server-side while spooling.
            Integrity anchor and future dedup key.
        object_key: Opaque blob key in the object store
            (``tenants/<tenant>/files/<uuid>``).
        created_at: Unix timestamp of the completed upload.
    """

    id: str
    tenant_id: str
    owner_user_id: uuid.UUID | None
    workspace_id: str | None
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    object_key: str
    created_at: float


@runtime_checkable
class FileRegistry(Protocol):
    """Persistence port for file metadata.

    Implementations: :class:`~inqtrix.content.memory.MemoryFileRegistry`
    (default, process-local) and the Postgres registry in
    :mod:`inqtrix.storage.content_postgres`. All methods are async so
    both backends wire identically into the async routers.
    """

    async def create(self, record: FileRecord) -> None:
        """Persist a new record (ids are caller-generated and unique)."""
        ...

    async def get(self, file_id: str, *, tenant_id: str) -> FileRecord:
        """Return the record or raise :class:`FileNotFound`."""
        ...

    async def list(
        self,
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[FileRecord]:
        """Records newest-first, filtered by tenant and the optional
        owner/namespace facets. ``owner_user_id=None`` leaves the repository
        query unfiltered; callers remain responsible for excluding scoped
        rows from anonymous/static views."""
        ...

    async def delete(self, file_id: str, *, tenant_id: str) -> FileRecord:
        """Remove and return the record or raise :class:`FileNotFound`
        (the caller still needs ``object_key`` for blob cleanup)."""
        ...
