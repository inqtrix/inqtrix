"""File upload/download orchestration (registry + object store + authz).

The only place that combines the three collaborators:

* metadata and listing facts come from the
  :class:`~inqtrix.content.ports.FileRegistry`,
* bytes go to / come from the
  :class:`~inqtrix.storage.object_store.ObjectStore`,
* access decisions go through the
  :class:`~inqtrix.auth.permissions.AuthorizationService`.

Files are never shareable. Legacy unscoped principals may access only
ownerless legacy rows; scoped principals may access only their own files.
Denied access remains indistinguishable from absence and is audited by the
authorization service.

Uploads stream through a spool file with running SHA-256 and size
accounting — the limit is enforced without ever holding the file in
memory, and the registry row is written only after the blob landed in
the store (an aborted upload leaves nothing behind but a temp file).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator

from inqtrix.auth.permissions import (
    AuthorizationService,
    ResourceNotFound,
    SharePermission,
)
from inqtrix.auth.principal import Principal
from inqtrix.content.ports import FileNotFound, FileRecord, FileRegistry
from inqtrix.knowledge.parsing import DocumentParseError, DocumentParser, extract_file
from inqtrix.storage.object_store import ObjectStore

log = logging.getLogger("inqtrix")

FILE_RESOURCE_TYPE = "file"
"""Authorization label; it is deliberately not a shareable resource type."""


@dataclass(frozen=True)
class ExtractedFileText:
    """The parsed text of one file plus the parser that produced it."""

    file_id: str
    parser_id: str
    text: str
    page_texts: tuple[str, ...] = ()
    parser_revision: str = ""


class FileParserUnavailable(RuntimeError):
    """Raised when text extraction runs without a configured parser."""


class FileTextExtractionError(ValueError):
    """Raised when a file cannot be parsed to text (visible, never empty)."""


class FileRegistryConflict(RuntimeError):
    """A deterministic file id is registered with different immutable facts."""


_SAFE_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
"""Allowed shape for server-generated object-key segments. Tenant ids
and file ids are server-controlled, but the guard keeps the
"keys are shape-safe" invariant explicit instead of assumed."""


class FileTooLarge(ValueError):
    """Raised when an upload exceeds the configured size limit."""

    def __init__(self, *, limit_bytes: int) -> None:
        super().__init__(f"file exceeds the limit of {limit_bytes} bytes")
        self.limit_bytes = limit_bytes


@dataclass(frozen=True)
class SpooledUpload:
    """Result of spooling one upload to disk (pre-registry facts)."""

    path: Path
    size_bytes: int
    sha256: str


class FileService:
    """Upload, list, stream, and delete user files.

    Args:
        registry: Metadata persistence port (memory or Postgres).
        object_store: Blob storage port (local directory or S3).
        permissions: The authorization chokepoint; consulted for every
            non-owner access.
        max_file_bytes: Upload size limit enforced while spooling.
    """

    def __init__(
        self,
        *,
        registry: FileRegistry,
        object_store: ObjectStore,
        permissions: AuthorizationService,
        max_file_bytes: int,
        document_parser: DocumentParser | None = None,
    ) -> None:
        self._registry = registry
        self._object_store = object_store
        self._permissions = permissions
        self._max_file_bytes = max_file_bytes
        self._document_parser = document_parser
        self._object_store_probe_task: asyncio.Task[bool] | None = None

    @property
    def text_extraction_available(self) -> bool:
        """Whether this service can produce canonical server-side text."""

        return self._document_parser is not None

    async def object_store_available(self) -> bool:
        """Return whether the configured blob store is reachable now.

        The check is intentionally read-only and runs off the event loop
        because S3/local filesystem probes are synchronous. It feeds
        operator-facing capability/status surfaces; upload/download paths
        still fail loudly on their own operations.
        """
        loop = asyncio.get_running_loop()
        task = self._object_store_probe_task
        if task is not None and task.get_loop() is not loop:
            if not task.done():
                raise RuntimeError(
                    "object-store probe is still active on another event loop"
                )
            task = None
        if task is None or task.done():
            task = loop.create_task(asyncio.to_thread(self._object_store.is_available))
            self._object_store_probe_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._object_store_probe_task is task:
                self._object_store_probe_task = None

    async def upload(
        self,
        *,
        chunks: AsyncIterator[bytes],
        file_name: str,
        content_type: str,
        principal: Principal,
        workspace_id: str | None,
    ) -> FileRecord:
        """Spool, hash, store, and register one uploaded file.

        Raises:
            FileTooLarge: When the stream exceeds the configured limit
                (spool is discarded, nothing is registered).
        """
        spooled = await self.spool_upload(chunks)
        try:
            record = self.prepare_file_record(
                spooled=spooled,
                file_name=file_name,
                content_type=content_type,
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                workspace_id=workspace_id,
            )
            await self.store_prepared_object(record, spooled)
            try:
                await self.register_prepared_file(record)
            except BaseException:
                # A blob without a registry row is unreachable garbage;
                # clean it up best-effort and keep the failure loud.
                try:
                    await asyncio.to_thread(
                        self._object_store.delete, record.object_key
                    )
                except Exception as cleanup_exc:
                    log.warning(
                        "orphaned blob after failed registry create "
                        "(error_type=%s)",
                        type(cleanup_exc).__name__,
                    )
                raise
            return record
        finally:
            spooled.path.unlink(missing_ok=True)

    async def spool_upload(self, chunks: AsyncIterator[bytes]) -> SpooledUpload:
        """Stream the upload to a temp file with running hash/size."""
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(
            prefix="inqtrix-upload-", delete=False
        ) as spool:
            spool_path = Path(spool.name)
            try:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self._max_file_bytes:
                        raise FileTooLarge(limit_bytes=self._max_file_bytes)
                    digest.update(chunk)
                    # Disk writes leave the event loop — a slow disk
                    # must not stall unrelated requests mid-upload.
                    await asyncio.to_thread(spool.write, chunk)
            except BaseException:
                spool.close()
                spool_path.unlink(missing_ok=True)
                raise
        return SpooledUpload(
            path=spool_path, size_bytes=size, sha256=digest.hexdigest()
        )

    def prepare_file_record(
        self,
        *,
        spooled: SpooledUpload,
        file_name: str,
        content_type: str,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
        workspace_id: str | None,
        file_id: str | None = None,
        created_at: float | None = None,
    ) -> FileRecord:
        """Create immutable server-owned file facts before external writes.

        Durable upload operations persist this record before the object-store
        write.  Its deterministic object key lets recovery distinguish a
        completed atomic put from a request that died before transferring any
        bytes.
        """

        if not _SAFE_KEY_SEGMENT.fullmatch(tenant_id):
            raise ValueError(f"tenant id unsafe for object keys: {tenant_id!r}")
        resolved_file_id = file_id or f"fl_{uuid.uuid4().hex}"
        if not _SAFE_KEY_SEGMENT.fullmatch(resolved_file_id):
            raise ValueError(f"file id unsafe for object keys: {resolved_file_id!r}")
        return FileRecord(
            id=resolved_file_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            file_name=file_name,
            content_type=content_type or "application/octet-stream",
            size_bytes=spooled.size_bytes,
            sha256=spooled.sha256,
            object_key=f"tenants/{tenant_id}/files/{resolved_file_id}",
            created_at=created_at if created_at is not None else time.time(),
        )

    async def store_prepared_object(
        self, record: FileRecord, spooled: SpooledUpload
    ) -> None:
        """Idempotently publish the prepared bytes under their stable key."""

        if spooled.size_bytes != record.size_bytes or spooled.sha256 != record.sha256:
            raise FileRegistryConflict(record.id)
        await asyncio.to_thread(self._object_store.put, record.object_key, spooled.path)

    async def prepared_object_exists(self, record: FileRecord) -> bool:
        """Return whether the operation-bound object has been published."""

        return await asyncio.to_thread(self._object_store.exists, record.object_key)

    async def register_prepared_file(self, record: FileRecord) -> FileRecord:
        """Create one file row, accepting an exact prior create on replay."""

        try:
            existing = await self._registry.get(record.id, tenant_id=record.tenant_id)
        except FileNotFound:
            await self._registry.create(record)
            return record
        if existing != record:
            raise FileRegistryConflict(record.id)
        return existing

    async def prepared_file_record(
        self, file_id: str, *, tenant_id: str
    ) -> FileRecord | None:
        """Read registry state for recovery without performing authorization."""

        try:
            return await self._registry.get(file_id, tenant_id=tenant_id)
        except FileNotFound:
            return None

    async def delete_prepared_object(self, record: FileRecord) -> None:
        """Delete the exact operation-owned blob without touching metadata."""

        await asyncio.to_thread(self._object_store.delete, record.object_key)

    async def discard_prepared_file(self, record: FileRecord) -> None:
        """Compensate an upload that lost its asset lifecycle race.

        The exact immutable registry record is verified before deleting
        anything.  Blob deletion happens first, matching the normal file
        deletion invariant: a transient object-store failure keeps the
        registry row as a visible recovery anchor instead of hiding occupied
        bytes behind absent metadata.  Every step is idempotent.
        """

        registered = await self.prepared_file_record(
            record.id, tenant_id=record.tenant_id
        )
        if registered is not None and registered != record:
            raise FileRegistryConflict(record.id)
        await self.delete_prepared_object(record)
        if registered is not None:
            await self._registry.delete(record.id, tenant_id=record.tenant_id)

    async def discard_file_lifecycle(
        self,
        file_id: str,
        *,
        tenant_id: str,
    ) -> FileRecord | None:
        """Remove an operation-owned file even when no registry row landed.

        Durable upload persists the server-generated file id before its first
        object put.  The deterministic key therefore remains an exact cleanup
        address if deletion wins between object publication and registry
        creation.  A present registry row is retained as the canonical return
        value and removed only after the blob delete succeeds.
        """

        if not _SAFE_KEY_SEGMENT.fullmatch(tenant_id):
            raise ValueError(f"tenant id unsafe for object keys: {tenant_id!r}")
        if not _SAFE_KEY_SEGMENT.fullmatch(file_id):
            raise ValueError(f"file id unsafe for object keys: {file_id!r}")
        registered = await self.prepared_file_record(file_id, tenant_id=tenant_id)
        object_key = (
            registered.object_key
            if registered is not None
            else f"tenants/{tenant_id}/files/{file_id}"
        )
        await asyncio.to_thread(self._object_store.delete, object_key)
        if registered is not None:
            await self._registry.delete(file_id, tenant_id=tenant_id)
        return registered

    async def file_lifecycle_residuals(
        self,
        file_id: str,
        *,
        tenant_id: str,
    ) -> tuple[bool, bool]:
        """Return ``(registry_exists, object_exists)`` for one file lifecycle."""

        if not _SAFE_KEY_SEGMENT.fullmatch(tenant_id):
            raise ValueError(f"tenant id unsafe for object keys: {tenant_id!r}")
        if not _SAFE_KEY_SEGMENT.fullmatch(file_id):
            raise ValueError(f"file id unsafe for object keys: {file_id!r}")
        registered = await self.prepared_file_record(file_id, tenant_id=tenant_id)
        object_key = (
            registered.object_key
            if registered is not None
            else f"tenants/{tenant_id}/files/{file_id}"
        )
        object_exists = await asyncio.to_thread(self._object_store.exists, object_key)
        return registered is not None, object_exists

    async def get(self, file_id: str, *, principal: Principal) -> FileRecord:
        """Return metadata after the read-access check."""
        record = await self._registry.get(file_id, tenant_id=principal.tenant_id)
        await self._require(principal, record, SharePermission.VIEW)
        return record

    async def open_stream(
        self, file_id: str, *, principal: Principal
    ) -> tuple[FileRecord, Iterator[bytes]]:
        """Return metadata plus a byte iterator after the access check.

        The store opens the blob eagerly (missing/unreachable raises
        HERE, before any response byte) — that blocking open runs in a
        thread; the returned iterator stays synchronous because
        Starlette drives sync generators in its threadpool.
        """
        record = await self.get(file_id, principal=principal)
        chunks = await asyncio.to_thread(self._object_store.stream, record.object_key)
        return record, chunks

    async def extract_text(
        self, file_id: str, *, principal: Principal
    ) -> ExtractedFileText:
        """Parse one file's bytes with the server parser.

        Read-access check, blob fetch and parse live here; blocking blob
        read and parse run off the event loop. This is the PRODUCER side:
        readers go through ``file_preparation.read_file_text``, which serves
        a library file's stored preparation and calls this only for a file
        that has none (and for the one-time repair of a legacy asset).

        Raises:
            FileParserUnavailable: No parser configured (route → 501).
            FileNotFound / ObjectStoreError: Propagated from the stream.
            FileTextExtractionError: The parser could not convert the
                file (route → 422); never a silent empty body.
        """
        if self._document_parser is None:
            raise FileParserUnavailable(
                "Server parsing is unavailable (no document parser configured)"
            )
        record, chunks = await self.open_stream(file_id, principal=principal)
        content = await asyncio.to_thread(lambda: b"".join(chunks))
        parser = self._document_parser
        try:
            extraction = await asyncio.to_thread(
                extract_file, parser, file_name=record.file_name, content=content
            )
        except DocumentParseError as exc:
            raise FileTextExtractionError(str(exc)) from exc
        if not extraction.text.strip():
            raise FileTextExtractionError(
                "The document parser produced no indexable text."
            )
        return ExtractedFileText(
            file_id=record.id,
            parser_id=extraction.parser_id,
            text=extraction.text,
            page_texts=extraction.page_texts,
            parser_revision=extraction.parser_revision,
        )

    async def list(
        self,
        *,
        principal: Principal,
        user_context_is_scoped: bool,
        workspace_id: str | None,
    ) -> list[FileRecord]:
        """List visible files (owner-only for scoped principals).

        Share-granted files are not listed here: the file listing is
        deliberately creator-only, unlike the run listing, which adds
        shared-in runs with an ``access`` annotation.
        """
        owner_user_id = principal.user_id if user_context_is_scoped else None
        records = await self._registry.list(
            tenant_id=principal.tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
        )
        if user_context_is_scoped:
            return records
        return [record for record in records if record.owner_user_id is None]

    async def delete(self, file_id: str, *, principal: Principal) -> FileRecord:
        """Delete metadata and blob after the manage-access check.

        The blob is removed first and the registry row second. Object-store
        deletion is idempotent, so a later registry failure can be retried
        safely while the authorization/quota anchor remains. A blob-store
        failure leaves both metadata and quota untouched and surfaces as 503.

        Returns:
            The deleted :class:`FileRecord` — its owner/size let the
            caller free the owner's stored-bytes quota by exactly what
            was held.
        """
        record = await self._registry.get(file_id, tenant_id=principal.tenant_id)
        await self._require(principal, record, SharePermission.EDIT)
        await asyncio.to_thread(self._object_store.delete, record.object_key)
        await self._registry.delete(file_id, tenant_id=principal.tenant_id)
        return record

    async def _require(
        self,
        principal: Principal,
        record: FileRecord,
        permission: SharePermission,
    ) -> None:
        """Require owner/unscoped legacy access; direct file shares do not exist."""
        try:
            await self._permissions.require(
                principal,
                permission,
                owner_user_id=record.owner_user_id,
                resource_tenant_id=record.tenant_id,
                resource_type=FILE_RESOURCE_TYPE,
                resource_id=record.id,
            )
        except ResourceNotFound as exc:
            raise FileNotFound(record.id) from exc
