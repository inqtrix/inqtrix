from __future__ import annotations

import time
import uuid

import pytest

from inqtrix.runs.deletion_operations import (
    DeletionManifestItem,
    DeletionOperationConflict,
    DeletionOperationNotFound,
    DeletionOperationStatus,
    DeletionOperationStore,
    DeletionStage,
    DeletionTargetKind,
)
from inqtrix.source_authority import SourceLifecycleConflict, SourceScope


def _wait_for(
    store: DeletionOperationStore,
    operation_id: str,
    user_id: uuid.UUID,
    expected: str,
) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = store.get(
            operation_id,
            tenant_id="default",
            created_by_user_id=user_id,
            workspace_id="ws-1",
        )
        if result["status"] == expected:
            return result
        time.sleep(0.01)
    raise AssertionError(f"operation did not reach {expected}")


def _manifest() -> tuple[DeletionManifestItem, ...]:
    return (
        DeletionManifestItem(
            asset_id="fa_1",
            source_id="asset:fa_1",
            server_file_id="fl_1",
            size_bytes=42,
        ),
    )


def test_in_memory_deletion_reports_real_stages_and_terminal_state() -> None:
    store = DeletionOperationStore()
    user_id = uuid.uuid4()

    def work(handle) -> None:
        handle.progress(
            DeletionStage.SEARCH_DETACHED,
            completed_items=0,
            total_items=1,
        )
        handle.progress(
            DeletionStage.RESIDUALS_VERIFIED,
            completed_items=1,
            total_items=1,
        )
        handle.complete()

    created = store.submit(
        target_kind=DeletionTargetKind.ASSET,
        target_id="fa_1",
        manifest=_manifest(),
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
        work=work,
    )

    result = _wait_for(
        store,
        created["operation_id"],
        user_id,
        DeletionOperationStatus.DELETED.value,
    )
    assert result["stage"] == DeletionStage.DELETED.value
    assert result["completed_items"] == result["total_items"] == 1
    assert result["error"] is None
    assert result["retryable"] is False


def test_active_submission_is_idempotent_for_the_same_target() -> None:
    store = DeletionOperationStore()
    user_id = uuid.uuid4()
    release = False

    def work(handle) -> None:
        nonlocal release
        deadline = time.monotonic() + 2
        while not release and time.monotonic() < deadline:
            time.sleep(0.01)
        handle.complete()

    first = store.submit(
        target_kind=DeletionTargetKind.ASSET,
        target_id="fa_1",
        manifest=_manifest(),
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
        work=work,
    )
    second = store.submit(
        target_kind=DeletionTargetKind.ASSET,
        target_id="fa_1",
        manifest=_manifest(),
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
        work=work,
    )
    release = True

    assert second["operation_id"] == first["operation_id"]
    _wait_for(store, first["operation_id"], user_id, "deleted")


def test_overlapping_aggregate_cannot_start_a_second_asset_operation() -> None:
    store = DeletionOperationStore()
    user_id = uuid.uuid4()
    release = False

    def work(handle) -> None:
        nonlocal release
        deadline = time.monotonic() + 2
        while not release and time.monotonic() < deadline:
            time.sleep(0.01)
        handle.complete()

    first = store.submit(
        target_kind=DeletionTargetKind.BULK,
        target_id="bulk:one",
        manifest=_manifest(),
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
        work=work,
    )

    with pytest.raises(DeletionOperationConflict) as exc_info:
        store.submit(
            target_kind=DeletionTargetKind.ASSET,
            target_id="fa_1",
            manifest=_manifest(),
            tenant_id="default",
            created_by_user_id=user_id,
            workspace_id="ws-1",
            work=work,
        )

    assert str(exc_info.value) == first["operation_id"]
    release = True
    _wait_for(store, first["operation_id"], user_id, "deleted")


def test_failed_deletion_keeps_manifest_and_retries_same_operation() -> None:
    store = DeletionOperationStore()
    user_id = uuid.uuid4()
    attempts = 0

    def work(handle) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("object store unavailable")
        handle.progress(
            DeletionStage.RESIDUALS_VERIFIED,
            completed_items=1,
            total_items=1,
        )
        handle.complete()

    created = store.submit(
        target_kind=DeletionTargetKind.ASSET,
        target_id="fa_1",
        manifest=_manifest(),
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
        work=work,
    )
    failed = _wait_for(store, created["operation_id"], user_id, "delete_failed")
    assert failed["asset_ids"] == ["fa_1"]
    assert failed["retryable"] is True

    retried = store.retry(
        created["operation_id"],
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
    )
    assert retried["operation_id"] == created["operation_id"]
    completed = _wait_for(store, created["operation_id"], user_id, "deleted")
    assert completed["attempt"] == 2

    with pytest.raises(DeletionOperationConflict):
        store.retry(
            created["operation_id"],
            tenant_id="default",
            created_by_user_id=user_id,
            workspace_id="ws-1",
        )


def test_source_cleanup_plan_is_checkpointed_before_destructive_retry() -> None:
    store = DeletionOperationStore()
    user_id = uuid.uuid4()
    operation_id: str | None = None
    attempts = 0
    plan = {
        "version": 1,
        "scope": {
            "tenant_id": "default",
            "source_id": "asset:fa_1",
            "owner_user_id": str(user_id),
            "workspace_id": "ws-1",
        },
        "authority_epoch": 1,
        "operation_id": "placeholder",
        "targets": [],
    }

    def work(handle) -> None:
        nonlocal attempts, operation_id
        attempts += 1
        operation_id = handle.operation_id
        if attempts == 1:
            persisted = {**plan, "operation_id": handle.operation_id}
            handle.checkpoint_source_cleanup("fa_1", persisted)
            raise RuntimeError("worker stopped after checkpoint")
        record = store.get_record(
            handle.operation_id,
            tenant_id="default",
            created_by_user_id=user_id,
            workspace_id="ws-1",
        )
        assert record.manifest[0].source_cleanup_plan is not None
        assert (
            record.manifest[0].source_cleanup_plan["operation_id"]
            == handle.operation_id
        )
        handle.complete()

    created = store.submit(
        target_kind=DeletionTargetKind.ASSET,
        target_id="fa_1",
        manifest=_manifest(),
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
        work=work,
    )
    _wait_for(store, created["operation_id"], user_id, "delete_failed")
    assert operation_id == created["operation_id"]

    store.retry(
        created["operation_id"],
        tenant_id="default",
        created_by_user_id=user_id,
        workspace_id="ws-1",
    )
    _wait_for(store, created["operation_id"], user_id, "deleted")


def test_deletion_operation_visibility_is_owner_and_workspace_scoped() -> None:
    store = DeletionOperationStore()
    owner = uuid.uuid4()
    created = store.submit(
        target_kind=DeletionTargetKind.ASSET,
        target_id="fa_1",
        manifest=_manifest(),
        tenant_id="default",
        created_by_user_id=owner,
        workspace_id="ws-1",
        work=lambda handle: handle.complete(),
    )
    _wait_for(store, created["operation_id"], owner, "deleted")

    with pytest.raises(DeletionOperationNotFound):
        store.get(
            created["operation_id"],
            tenant_id="default",
            created_by_user_id=uuid.uuid4(),
            workspace_id="ws-1",
        )
    with pytest.raises(DeletionOperationNotFound):
        store.get(
            created["operation_id"],
            tenant_id="default",
            created_by_user_id=owner,
            workspace_id="ws-other",
        )


def test_deletion_feed_retains_empty_section_operations() -> None:
    store = DeletionOperationStore()
    owner = uuid.uuid4()
    created = store.submit(
        target_kind=DeletionTargetKind.SECTION,
        target_id="sec_empty",
        manifest=(),
        tenant_id="default",
        created_by_user_id=owner,
        workspace_id="ws-1",
        work=lambda handle: handle.complete(),
    )
    _wait_for(store, created["operation_id"], owner, "deleted")

    data, next_cursor = store.list_operations(
        tenant_id="default",
        created_by_user_id=owner,
        workspace_id="ws-1",
        limit=10,
        after=None,
    )

    assert next_cursor is None
    assert data[0]["operation_id"] == created["operation_id"]
    assert data[0]["target_kind"] == "section"
    assert data[0]["asset_ids"] == []


def test_section_tombstone_outlives_the_operation_receipt() -> None:
    store = DeletionOperationStore()
    owner = uuid.uuid4()
    created = store.submit(
        target_kind=DeletionTargetKind.SECTION,
        target_id="sec_permanent",
        manifest=(),
        tenant_id="default",
        created_by_user_id=owner,
        workspace_id="ws-1",
        work=lambda handle: handle.complete(),
    )
    _wait_for(store, created["operation_id"], owner, "deleted")

    # Receipt retention and source identity retention are intentionally
    # different contracts. Simulate receipt expiry without weakening the
    # permanent no-resurrection authority.
    store._records.clear()  # type: ignore[attr-defined]
    with pytest.raises(SourceLifecycleConflict):
        store._source_authority.register_active(  # type: ignore[attr-defined]
            SourceScope(
                tenant_id="default",
                source_id="section:sec_permanent",
                owner_user_id=owner,
                workspace_id="ws-1",
            )
        )
