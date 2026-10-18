"""Crash, replay and source-fence contracts for durable bound uploads."""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.content.memory import MemoryFileRegistry
from inqtrix.knowledge.parsing import DocumentParseError
from inqtrix.project.asset_records_memory import MemoryAssetStore
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.quota.models import QuotaDimension, StockLifecycleState
from inqtrix.server.uploads import (
    MemoryUploadOperationStore,
    UploadAttemptSuperseded,
    UploadBinding,
    UploadOperationConflict,
    UploadOperationStatus,
)
from inqtrix.services.asset_records_service import AssetRecordsService
from inqtrix.services.file_service import FileService, SpooledUpload
from inqtrix.services.upload_operation_service import (
    UploadBytesRequired,
    UploadExecutionDeferred,
    UploadOperationService,
    UploadReconciler,
)
from inqtrix.source_authority import MemorySourceLifecycleAuthority, SourceScope
from inqtrix.storage.object_store import LocalFSObjectStore

USER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PAYLOAD = b"durable upload evidence\n" * 32


class _ProcessCrash(BaseException):
    """Fault that deliberately bypasses request-level exception handling."""


class _Parser:
    parser_id = "canonical-test-parser"
    parser_revision = "7.1"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def parse(self, *, file_name: str, content: bytes) -> str:
        del file_name
        self.calls += 1
        if self.fail:
            raise DocumentParseError("canonical parse failed")
        return content.decode("utf-8")


class _QuotaSpy:
    def __init__(self) -> None:
        self.adjustments: set[str] = set()
        self.total = 0
        self.calls = 0

    async def set_stock_for_subject(
        self,
        subject,
        dimension,
        *,
        stock_key: str,
        amount: int,
    ) -> StockLifecycleState:
        assert subject.user_id == USER
        assert dimension is QuotaDimension.STORED_BYTES
        self.calls += 1
        if stock_key not in self.adjustments:
            self.adjustments.add(stock_key)
            self.total += amount
        return StockLifecycleState(
            stock_key=stock_key,
            subject=subject,
            dimension=dimension,
            amount=amount,
            tombstoned=False,
        )


@dataclass
class _Harness:
    files: FileService
    registry: MemoryFileRegistry
    asset_store: MemoryAssetStore
    assets: AssetRecordsService
    operations: MemoryUploadOperationStore
    upload: UploadOperationService
    quota: _QuotaSpy
    visible_to: UserContext
    authority: MemorySourceLifecycleAuthority

    async def spool(self) -> SpooledUpload:
        async def chunks():
            yield PAYLOAD[:97]
            yield PAYLOAD[97:]

        return await self.files.spool_upload(chunks())

    async def start(self, spooled: SpooledUpload, *, created_at: float = 1.0):
        return await self.upload.start_from_spool(
            asset_id="asset-upload",
            spooled=spooled,
            file_name="evidence.txt",
            content_type="text/plain",
            binding=UploadBinding(
                section_id="section-upload",
                group_id=None,
                title="Evidence",
                label="evidence",
                origin="library",
                page_count=None,
                parse_status="parsed",
                parse_warning=None,
                text_truncated=False,
                parser_id=None,
                created_at=created_at,
            ),
            visible_to=self.visible_to,
        )


async def _build_harness(
    tmp_path: Path,
    *,
    max_attempts: int = 3,
    heartbeat_seconds: float = 15.0,
    fault_hook=None,
    parser=None,
) -> _Harness:
    visible_to = UserContext(
        principal=Principal(
            user_id=USER,
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )
    )
    authority = MemorySourceLifecycleAuthority()
    asset_store = MemoryAssetStore()
    asset_store.bind_source_lifecycle_authority(authority)
    assets = AssetRecordsService(store=asset_store)
    await assets.save_section(
        id="section-upload",
        kind="custom",
        title="Uploads",
        created_at=1.0,
        updated_at=1.0,
        caller_user_id=USER,
        workspace_id=None,
        visible_to=visible_to,
    )
    await assets.reserve_upload(
        id="asset-upload",
        section_id="section-upload",
        group_id=None,
        title="Evidence",
        label="evidence",
        file_name="evidence.txt",
        mime_type="text/plain",
        origin="library",
        page_count=None,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=len(PAYLOAD),
        parser_id=None,
        created_at=1.0,
        updated_at=1.0,
        caller_user_id=USER,
        workspace_id=None,
        visible_to=visible_to,
    )
    registry = MemoryFileRegistry()
    permissions = Mock()
    permissions.require = AsyncMock(return_value=None)
    files = FileService(
        registry=registry,
        object_store=LocalFSObjectStore(root=tmp_path / "objects"),
        permissions=permissions,
        max_file_bytes=10_000,
        document_parser=parser,
    )
    operations = MemoryUploadOperationStore(assets=asset_store)
    quota = _QuotaSpy()
    upload = UploadOperationService(
        operations=operations,
        files=files,
        assets=assets,
        quota=quota,
        max_attempts=max_attempts,
        heartbeat_seconds=heartbeat_seconds,
        fault_hook=fault_hook,
    )
    return _Harness(
        files=files,
        registry=registry,
        asset_store=asset_store,
        assets=assets,
        operations=operations,
        upload=upload,
        quota=quota,
        visible_to=visible_to,
        authority=authority,
    )


def test_bound_upload_queues_canonical_parse_and_publishes_one_source(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        parser = _Parser()
        harness = await _build_harness(tmp_path, parser=parser)
        spooled = await harness.spool()
        try:
            attempt = await harness.start(spooled)
            with pytest.raises(UploadExecutionDeferred) as deferred:
                await harness.upload.execute(
                    attempt,
                    visible_to=harness.visible_to,
                    spooled=spooled,
                )

            assert deferred.value.operation["status"] == "queued"
            assert deferred.value.operation["stage"] == "parsing"
            assert parser.calls == 0
            pending = await harness.assets.get_asset(
                "asset-upload", visible_to=harness.visible_to
            )
            assert pending.upload_status == "parsing"
            assert pending.prepared_text == ""

            claimed = harness.operations.claim_for_execution(
                attempt.record.operation_id,
                "default",
                allow_takeover=False,
            )
            assert claimed is not None
            _, asset, summary = await harness.upload.execute(
                claimed,
                visible_to=harness.visible_to,
                spooled=None,
            )

            expected = PAYLOAD.decode("utf-8").strip()
            assert summary["status"] == "ready"
            assert asset.upload_status == "ready"
            assert asset.extracted_text == expected
            assert asset.prepared_text == expected
            assert asset.prepared_parser_id == parser.parser_id
            assert asset.prepared_parser_revision == parser.parser_revision
            assert asset.prepared_file_sha256 == attempt.record.file.sha256
            assert asset.prepared_content_hash is not None
            assert asset.prepared_at is not None
            assert parser.calls == 1
            assert harness.quota.total == len(PAYLOAD)
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_restart_after_prepared_publish_reuses_persisted_parse_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        parser = _Parser()

        def crash(at: str, _record) -> None:
            if at == "after_parse_result":
                raise _ProcessCrash(at)

        harness = await _build_harness(
            tmp_path,
            parser=parser,
            fault_hook=crash,
        )
        spooled = await harness.spool()
        try:
            attempt = await harness.start(spooled)
            with pytest.raises(UploadExecutionDeferred):
                await harness.upload.execute(
                    attempt,
                    visible_to=harness.visible_to,
                    spooled=spooled,
                )
            claimed = harness.operations.claim_for_execution(
                attempt.record.operation_id,
                "default",
                allow_takeover=False,
            )
            assert claimed is not None
            with pytest.raises(_ProcessCrash, match="after_parse_result"):
                await harness.upload.execute(
                    claimed,
                    visible_to=harness.visible_to,
                    spooled=None,
                )
            published = await harness.assets.get_asset(
                "asset-upload", visible_to=harness.visible_to
            )
            assert published.prepared_text
            assert parser.calls == 1

            assert harness.operations.stale_dispatches(older_than_seconds=-1) == [
                (attempt.record.operation_id, "default")
            ]
            resumed = harness.operations.claim_for_execution(
                attempt.record.operation_id,
                "default",
                allow_takeover=False,
            )
            assert resumed is not None
            harness.upload._fault_hook = None
            _, asset, ready = await harness.upload.execute(
                resumed,
                visible_to=harness.visible_to,
                spooled=None,
            )
            assert ready["status"] == "ready"
            assert asset.prepared_text == published.prepared_text
            assert parser.calls == 1
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_deterministic_parse_failure_keeps_original_but_not_index_source(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        parser = _Parser(fail=True)
        harness = await _build_harness(tmp_path, parser=parser)
        spooled = await harness.spool()
        try:
            attempt = await harness.start(spooled)
            with pytest.raises(UploadExecutionDeferred):
                await harness.upload.execute(
                    attempt,
                    visible_to=harness.visible_to,
                    spooled=spooled,
                )
            claimed = harness.operations.claim_for_execution(
                attempt.record.operation_id,
                "default",
                allow_takeover=False,
            )
            assert claimed is not None
            file_record, asset, summary = await harness.upload.execute(
                claimed,
                visible_to=harness.visible_to,
                spooled=None,
            )
            assert summary["status"] == "ready"
            assert file_record.id == asset.server_file_id
            assert asset.parse_status == "error"
            assert asset.parse_warning == "canonical parse failed"
            assert asset.prepared_text == ""
            assert asset.prepared_content_hash is None
            assert await harness.files.prepared_object_exists(file_record)
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "boundary",
    [
        "after_object_store",
        "after_file_registry",
        "after_asset_bind",
        "after_quota_receipt",
    ],
)
def test_restart_reconciles_every_external_write_boundary(
    tmp_path: Path, boundary: str
) -> None:
    async def scenario() -> None:
        def crash(at: str, _record) -> None:
            if at == boundary:
                raise _ProcessCrash(at)

        harness = await _build_harness(tmp_path, fault_hook=crash)
        spooled = await harness.spool()
        try:
            attempt = await harness.start(spooled)
            with pytest.raises(_ProcessCrash, match=boundary):
                await harness.upload.execute(
                    attempt,
                    visible_to=harness.visible_to,
                    spooled=spooled,
                )

            # Simulate a new process claiming the persisted, stale operation.
            assert harness.operations.stale_dispatches(older_than_seconds=-1) == [
                (attempt.record.operation_id, "default")
            ]
            claimed = harness.operations.claim_for_execution(
                attempt.record.operation_id,
                "default",
                allow_takeover=False,
            )
            assert claimed is not None
            harness.upload._fault_hook = None
            _, asset, summary = await harness.upload.execute(
                claimed,
                visible_to=harness.visible_to,
                spooled=None,
            )

            assert summary["status"] == "ready"
            assert summary["stage"] == "ready"
            assert asset.upload_status == "ready"
            assert asset.server_file_id == attempt.record.file.id
            registered = await harness.registry.list(
                tenant_id="default",
                owner_user_id=USER,
                workspace_id=None,
            )
            assert [item.id for item in registered] == [attempt.record.file.id]
            assert harness.quota.total == len(PAYLOAD)
            assert len(harness.quota.adjustments) == 1
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_lost_response_after_ready_replays_same_operation_and_file(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        def crash(at: str, _record) -> None:
            if at == "after_ready_commit":
                raise _ProcessCrash(at)

        harness = await _build_harness(tmp_path, fault_hook=crash)
        first_spool = await harness.spool()
        retry_spool = await harness.spool()
        try:
            first = await harness.start(first_spool)
            with pytest.raises(_ProcessCrash):
                await harness.upload.execute(
                    first,
                    visible_to=harness.visible_to,
                    spooled=first_spool,
                )
            assert first.record.status is UploadOperationStatus.READY

            # A reconstructed browser request need not reproduce the original
            # client-side creation timestamp.
            replay = await harness.start(retry_spool, created_at=999.0)
            assert replay.already_ready is True
            file_record, asset, summary = await harness.upload.execute(
                replay,
                visible_to=harness.visible_to,
                spooled=retry_spool,
            )
            assert file_record.id == first.record.file.id
            assert asset.server_file_id == file_record.id
            assert summary["operation_id"] == first.record.operation_id
            assert harness.quota.total == len(PAYLOAD)
        finally:
            first_spool.path.unlink(missing_ok=True)
            retry_spool.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_existing_exact_asset_binding_is_reconciled_and_quota_booked_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        spooled = await harness.spool()
        try:
            prepared = harness.files.prepare_file_record(
                spooled=spooled,
                file_name="evidence.txt",
                content_type="text/plain",
                tenant_id="default",
                owner_user_id=USER,
                workspace_id=None,
            )
            await harness.files.store_prepared_object(prepared, spooled)
            await harness.files.register_prepared_file(prepared)
            await harness.assets.bind_uploaded_file(
                id="asset-upload",
                section_id="section-upload",
                group_id=None,
                title="Evidence",
                label="evidence",
                file_name=prepared.file_name,
                mime_type=prepared.content_type,
                origin="library",
                page_count=None,
                parse_status="parsed",
                parse_warning=None,
                text_truncated=False,
                size_bytes=prepared.size_bytes,
                server_file_id=prepared.id,
                parser_id=None,
                created_at=1.0,
                updated_at=prepared.created_at,
                caller_user_id=USER,
                workspace_id=None,
                visible_to=harness.visible_to,
            )

            attempt = await harness.start(spooled, created_at=42.0)
            assert attempt.record.file == prepared
            _, asset, ready = await harness.upload.execute(
                attempt,
                visible_to=harness.visible_to,
                spooled=spooled,
            )
            assert ready["status"] == "ready"
            assert asset.server_file_id == prepared.id
            assert harness.quota.total == len(PAYLOAD)

            replay = await harness.start(spooled, created_at=77.0)
            assert replay.already_ready is True
            await harness.upload.execute(
                replay,
                visible_to=harness.visible_to,
                spooled=spooled,
            )
            assert harness.quota.total == len(PAYLOAD)
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_missing_request_spool_is_explicit_and_same_file_can_resume(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        first_spool = await harness.spool()
        retry_spool = await harness.spool()
        try:
            attempt = await harness.start(first_spool)
            with pytest.raises(UploadBytesRequired):
                await harness.upload.execute(
                    attempt,
                    visible_to=harness.visible_to,
                    spooled=None,
                )
            summary = harness.operations.get(
                attempt.record.operation_id,
                tenant_id="default",
                created_by_user_id=USER,
                workspace_id=None,
            )
            assert summary["status"] == "awaiting_bytes"
            assert summary["requires_bytes"] is True
            with pytest.raises(UploadOperationConflict, match="bytes are required"):
                harness.operations.retry(
                    attempt.record.operation_id,
                    tenant_id="default",
                    created_by_user_id=USER,
                    workspace_id=None,
                )

            resumed = await harness.start(retry_spool, created_at=2.0)
            _, asset, ready = await harness.upload.execute(
                resumed,
                visible_to=harness.visible_to,
                spooled=retry_spool,
            )
            assert ready["status"] == "ready"
            assert asset.upload_status == "ready"
        finally:
            first_spool.path.unlink(missing_ok=True)
            retry_spool.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_newer_attempt_fences_an_older_concurrent_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        first_spool = await harness.spool()
        second_spool = await harness.spool()
        try:
            first = await harness.start(first_spool)
            second = await harness.start(second_spool, created_at=7.0)
            assert second.attempt == first.attempt + 1
            with pytest.raises(UploadAttemptSuperseded):
                await harness.upload.execute(
                    first,
                    visible_to=harness.visible_to,
                    spooled=first_spool,
                )
            assert not await harness.files.prepared_object_exists(first.record.file)

            _, _, ready = await harness.upload.execute(
                second,
                visible_to=harness.visible_to,
                spooled=second_spool,
            )
            assert ready["status"] == "ready"
        finally:
            first_spool.path.unlink(missing_ok=True)
            second_spool.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_slow_active_write_is_heartbeated_not_reclaimed(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path, heartbeat_seconds=0.01)
        spooled = await harness.spool()
        original_store = harness.files.store_prepared_object

        async def slow_store(record, upload_spool) -> None:
            await asyncio.sleep(0.12)
            await original_store(record, upload_spool)

        harness.files.store_prepared_object = slow_store  # type: ignore[method-assign]
        try:
            attempt = await harness.start(spooled)
            execution = asyncio.create_task(
                harness.upload.execute(
                    attempt,
                    visible_to=harness.visible_to,
                    spooled=spooled,
                )
            )
            await asyncio.sleep(0.07)
            assert harness.operations.stale_dispatches(older_than_seconds=0.03) == []
            _, _, ready = await execution
            assert ready["status"] == "ready"
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_automatic_retry_budget_is_bounded_and_manual_retry_is_visible(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        def fail_after_quota(at: str, _record) -> None:
            if at == "after_quota_receipt":
                raise RuntimeError("quota dependency interrupted")

        harness = await _build_harness(
            tmp_path,
            max_attempts=2,
            fault_hook=fail_after_quota,
        )
        spooled = await harness.spool()
        try:
            first = await harness.start(spooled)
            with pytest.raises(UploadExecutionDeferred) as deferred:
                await harness.upload.execute(
                    first,
                    visible_to=harness.visible_to,
                    spooled=spooled,
                )
            assert deferred.value.operation["status"] == "queued"

            second = harness.operations.claim_for_execution(
                first.record.operation_id,
                "default",
                allow_takeover=False,
            )
            assert second is not None and second.attempt == 2
            with pytest.raises(RuntimeError, match="quota dependency interrupted"):
                await harness.upload.execute(
                    second,
                    visible_to=harness.visible_to,
                    spooled=None,
                )
            failed = harness.operations.get_record(first.record.operation_id)
            assert failed.status is UploadOperationStatus.UPLOAD_FAILED
            assert failed.error == {
                "message": "quota dependency interrupted",
                "type": "retry_budget_exhausted",
            }

            manual = harness.operations.retry(
                first.record.operation_id,
                tenant_id="default",
                created_by_user_id=USER,
                workspace_id=None,
            )
            assert manual["status"] == "queued"
            third = harness.operations.claim_for_execution(
                first.record.operation_id,
                "default",
                allow_takeover=False,
            )
            assert third is not None
            harness.upload._fault_hook = None
            _, _, ready = await harness.upload.execute(
                third,
                visible_to=harness.visible_to,
                spooled=None,
            )
            assert ready["status"] == "ready"
            # Three calls crossed the receipt boundary, but one stable
            # adjustment id changed the stock exactly once.
            assert harness.quota.calls == 3
            assert harness.quota.total == len(PAYLOAD)
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_deletion_fence_cleans_file_that_never_became_asset_evidence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        spooled = await harness.spool()
        try:
            attempt = await harness.start(spooled)
            harness.authority.begin_delete(
                SourceScope(
                    tenant_id="default",
                    source_id="asset:asset-upload",
                    owner_user_id=USER,
                    workspace_id=None,
                ),
                operation_id="del-upload-race",
            )
            with pytest.raises(Exception) as failed:
                await harness.upload.execute(
                    attempt,
                    visible_to=harness.visible_to,
                    spooled=spooled,
                )
            assert failed.value.__class__.__name__ == "AssetDeletionInProgress"

            operation = harness.operations.get_record(attempt.record.operation_id)
            assert operation.status is UploadOperationStatus.UPLOAD_FAILED
            assert operation.error is not None
            assert operation.error["type"] == "upload_source_unavailable"
            assert (
                await harness.registry.list(
                    tenant_id="default",
                    owner_user_id=USER,
                    workspace_id=None,
                )
                == []
            )
            assert list((tmp_path / "objects").rglob("fl_*")) == []
            assert harness.quota.total == 0
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_file_lifecycle_cleanup_removes_blob_without_registry_row(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        spooled = await harness.spool()
        try:
            attempt = await harness.start(spooled)
            record = attempt.record.file
            await harness.files.store_prepared_object(record, spooled)
            assert await harness.files.prepared_object_exists(record)
            assert (
                await harness.files.prepared_file_record(record.id, tenant_id="default")
                is None
            )

            deleted = await harness.files.discard_file_lifecycle(
                record.id, tenant_id="default"
            )

            assert deleted is None
            assert not await harness.files.prepared_object_exists(record)
            assert await harness.files.file_lifecycle_residuals(
                record.id, tenant_id="default"
            ) == (False, False)
        finally:
            spooled.path.unlink(missing_ok=True)

    asyncio.run(scenario())


def test_reconciler_stops_on_schema_head_mismatch_instead_of_retrying():
    """A fence hit is fatal for THIS process's reconciler, not a warning.

    The expected schema head is a code constant, so a mismatch can never
    heal for the running process; retrying every pass would downgrade a
    fatal state to warning spam (the no-silent-fallback rule). ``_run``
    must return on the first fence hit -- with ``stop`` never set, a
    swallowed mismatch would keep this call looping forever.
    """
    import threading

    from inqtrix.services.upload_operation_service import UploadReconciler
    from inqtrix.storage.migration_contract import SchemaHeadMismatch

    calls: list[float] = []
    stop = threading.Event()

    class FencedOperations:
        def stale_dispatches(self, *, older_than_seconds):
            calls.append(older_than_seconds)
            if len(calls) >= 3:
                # Regression guard: a swallowed mismatch would loop forever
                # (no timeout plugin runs in this suite). Bound it so the
                # test FAILS on the call count instead of hanging.
                stop.set()
            raise SchemaHeadMismatch("Schema-Kopf bewegt: 0079 statt 0078")

    service = Mock()
    service.operations = FencedOperations()
    reconciler = UploadReconciler(service=service, interval_seconds=1.0)

    # Direct call on this thread: returns promptly on the fence hit.
    reconciler._run(stop)

    assert calls == [30.0], "exactly one pass, then a hard stop"


def _with_parser(harness: _Harness, parser, tmp_path: Path) -> UploadOperationService:
    """The same stores, served by a process that has a parser now."""
    permissions = Mock()
    permissions.require = AsyncMock(return_value=None)
    files = FileService(
        registry=harness.registry,
        object_store=LocalFSObjectStore(root=tmp_path / "objects"),
        permissions=permissions,
        max_file_bytes=10_000,
        document_parser=parser,
    )
    return UploadOperationService(
        operations=harness.operations,
        files=files,
        assets=harness.assets,
        quota=harness.quota,
        max_attempts=3,
    )


async def _upload_without_parser(harness: _Harness) -> str:
    """A bound upload completed by a process without a parser: the
    preparation is skipped and the asset is ready without a text."""
    spooled = await harness.spool()
    try:
        attempt = await harness.start(spooled)
        _, asset, summary = await harness.upload.execute(
            attempt, visible_to=harness.visible_to, spooled=spooled
        )
    finally:
        spooled.path.unlink(missing_ok=True)
    assert summary["status"] == "ready"
    assert asset.prepared_parser_id is None
    return attempt.record.operation_id


def test_a_preparation_skipped_without_a_parser_is_made_once_a_parser_exists(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        operation_id = await _upload_without_parser(harness)
        assert harness.upload.queue_missing_preparations() == []

        parser = _Parser()
        later = _with_parser(harness, parser, tmp_path)
        later._audit_uploaded = AsyncMock()
        assert later.queue_missing_preparations() == [(operation_id, "default")]
        assert later.queue_missing_preparations() == []
        pending = await harness.assets.get_asset(
            "asset-upload", visible_to=harness.visible_to
        )
        assert pending.upload_status == "parsing"

        claimed = harness.operations.claim_for_execution(
            operation_id, "default", allow_takeover=False
        )
        assert claimed is not None
        _, asset, summary = await later.execute(
            claimed, visible_to=harness.visible_to, spooled=None
        )

        assert summary["status"] == "ready"
        assert asset.upload_status == "ready"
        assert asset.prepared_text == PAYLOAD.decode("utf-8").strip()
        assert asset.prepared_parser_id == parser.parser_id
        assert parser.calls == 1
        # Preparing a stored file again is not another upload.
        later._audit_uploaded.assert_not_awaited()
        assert later.queue_missing_preparations() == []

    asyncio.run(scenario())


def test_a_file_the_parser_cannot_read_is_not_queued_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        operation_id = await _upload_without_parser(harness)
        later = _with_parser(harness, _Parser(fail=True), tmp_path)
        assert later.queue_missing_preparations() == [(operation_id, "default")]
        claimed = harness.operations.claim_for_execution(
            operation_id, "default", allow_takeover=False
        )
        assert claimed is not None
        _, asset, summary = await later.execute(
            claimed, visible_to=harness.visible_to, spooled=None
        )

        assert summary["status"] == "ready"
        assert asset.parse_status == "error"
        assert later.queue_missing_preparations() == []

    asyncio.run(scenario())


def test_a_file_being_deleted_is_never_queued_for_preparation(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        await _upload_without_parser(harness)
        asset = await harness.asset_store.get_asset("asset-upload")
        await harness.asset_store.set_asset_deletion_state(
            asset.id,
            scope=ResourceScope.from_record(asset),
            lifecycle_status="deleting",
            deletion_operation_id="del_1",
            deletion_stage="requested",
            deletion_error=None,
        )

        later = _with_parser(harness, _Parser(), tmp_path)
        assert later.queue_missing_preparations() == []

    asyncio.run(scenario())


def test_the_reconciler_prepares_a_skipped_upload_in_its_next_pass(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        harness = await _build_harness(tmp_path)
        await _upload_without_parser(harness)
        later = _with_parser(harness, _Parser(), tmp_path)
        stop = threading.Event()
        stale = harness.operations.stale_dispatches

        def one_pass(*, older_than_seconds):
            stop.set()
            return stale(older_than_seconds=older_than_seconds)

        harness.operations.stale_dispatches = one_pass
        reconciler = UploadReconciler(service=later, interval_seconds=1.0)
        await asyncio.to_thread(reconciler._run, stop)

        asset = await harness.assets.get_asset(
            "asset-upload", visible_to=harness.visible_to
        )
        assert asset.upload_status == "ready"
        assert asset.prepared_text == PAYLOAD.decode("utf-8").strip()

    asyncio.run(scenario())


def test_a_failing_preparation_pass_never_blocks_the_reconcilers_stale_pass(
    caplog,
) -> None:
    stop = threading.Event()
    passes: list[float] = []

    class Operations:
        def stale_dispatches(self, *, older_than_seconds):
            passes.append(older_than_seconds)
            stop.set()
            return []

    service = Mock()
    service.operations = Operations()
    service.queue_missing_preparations.side_effect = RuntimeError("database unavailable")
    reconciler = UploadReconciler(service=service, interval_seconds=1.0)

    with caplog.at_level("WARNING"):
        # Bounded: a pass that never reaches the stale dispatches would loop
        # until stopped here, and must fail rather than hang.
        worker = threading.Thread(target=reconciler._run, args=(stop,))
        worker.start()
        worker.join(timeout=5)
        stop.set()
        worker.join(timeout=5)

    assert passes == [30.0]
    assert "Missing upload preparations could not be queued" in caplog.text
