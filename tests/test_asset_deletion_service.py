from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.content.ports import FileNotFound
from inqtrix.knowledge.source_cleanup import empty_source_cleanup_plan
from inqtrix.project.asset_records_memory import MemoryAssetStore
from inqtrix.project.asset_records_ports import AssetNotFound
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.vector_index_memory import MemoryVectorIndexStore
from inqtrix.project.vector_index_ports import VectorIndexMember
from inqtrix.quota.models import StockLifecycleState
from inqtrix.runs.deletion_operations import DeletionOperationStore
from inqtrix.services.asset_deletion_service import AssetDeletionService
from inqtrix.services.asset_records_service import AssetRecordsService
from inqtrix.services.vector_index_service import VectorIndexService
from inqtrix.source_authority import (
    MemorySourceLifecycleAuthority,
    SourceLifecycleConflict,
    SourceScope,
)
from inqtrix.sync_bridge import run_coro_sync


@dataclass(frozen=True)
class _DeletedFile:
    tenant_id: str
    owner_user_id: uuid.UUID
    size_bytes: int


class _FileService:
    def __init__(self, owner: uuid.UUID) -> None:
        self.owner = owner
        self.present = {"fl_1"}
        self.fail = False
        self.delete_calls: list[str] = []
        self.bound_assets: AssetRecordsService | None = None

    async def delete(self, file_id: str, *, principal: Principal):
        self.delete_calls.append(file_id)
        if self.fail:
            raise RuntimeError("blob store unavailable")
        if self.bound_assets is not None:
            references = await self.bound_assets.list_assets_by_server_file_id(
                file_id
            )
            if references:
                raise RuntimeError("file registry remains referenced")
        if file_id not in self.present:
            raise FileNotFound(file_id)
        self.present.remove(file_id)
        return _DeletedFile("default", self.owner, 42)

    async def get(self, file_id: str, *, principal: Principal):
        if file_id not in self.present:
            raise FileNotFound(file_id)
        return _DeletedFile("default", self.owner, 42)

    async def discard_file_lifecycle(self, file_id: str, *, tenant_id: str):
        del tenant_id
        self.present.discard(file_id)
        return None

    async def file_lifecycle_residuals(self, file_id: str, *, tenant_id: str):
        del tenant_id
        present = file_id in self.present
        return present, present


class _KnowledgeService:
    def __init__(self) -> None:
        self.active = {"asset:fa_1"}
        self.detached: set[str] = set()

    async def mark_source_deleting(self, source_id: str, **_kwargs) -> None:
        self.detached.add(source_id)

    async def delete_source(self, source_id: str, **_kwargs) -> None:
        self.active.discard(source_id)

    async def prepare_source_cleanup(self, _source_id: str, *, deletion_permit):
        return empty_source_cleanup_plan(deletion_permit)

    async def count_source_residuals(self, source_id: str, **_kwargs) -> int:
        return int(source_id in self.active)


class _QuotaService:
    def __init__(self) -> None:
        self.adjustments: list[tuple[str, int]] = []

        self.states: dict[str, StockLifecycleState] = {}

    def tombstone_stock_blocking(
        self, subject, dimension, *, stock_key: str
    ) -> StockLifecycleState:
        state = self.states.get(stock_key)
        if state is None:
            self.adjustments.append((stock_key, -42))
        state = StockLifecycleState(
            stock_key=stock_key,
            subject=subject,
            dimension=dimension,
            amount=0,
            tombstoned=True,
        )
        self.states[stock_key] = state
        return state

    def stock_state_blocking(self, *, tenant_id: str, stock_key: str):
        del tenant_id
        return self.states.get(stock_key)


class _UploadSettlement:
    def __init__(self) -> None:
        self.ready = False

    def prepared_file_for_deletion(self, *args, **kwargs):
        del args, kwargs
        return None

    def deletion_can_finalize(self, *args, **kwargs) -> bool:
        del args, kwargs
        return self.ready


class _BindBeforeFenceAuthority(MemorySourceLifecycleAuthority):
    """Deterministically lets one active writer win before deletion fences."""

    def __init__(self) -> None:
        super().__init__()
        self.before_fence: Callable[[], None] | None = None

    def begin_delete_many(self, scopes, *, operation_id):
        callback = self.before_fence
        self.before_fence = None
        if callback is not None:
            callback()
        return super().begin_delete_many(scopes, operation_id=operation_id)


async def _fixture():
    owner = uuid.uuid4()
    principal = Principal(
        user_id=owner,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    visible = UserContext(principal=principal, workspace_ids=("ws-1",))
    asset_store = MemoryAssetStore()
    assets = AssetRecordsService(store=asset_store)
    await assets.save_section(
        id="sec_1",
        kind="custom",
        title="Files",
        created_at=1,
        updated_at=1,
        caller_user_id=owner,
        workspace_id="ws-1",
        visible_to=visible,
    )
    await assets.reserve_upload(
        id="fa_1",
        section_id="sec_1",
        group_id=None,
        title="Policy",
        label="policy",
        file_name="policy.pdf",
        mime_type="application/pdf",
        origin="library",
        page_count=1,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=42,
        parser_id="markitdown",
        created_at=1,
        updated_at=1,
        caller_user_id=owner,
        workspace_id="ws-1",
        visible_to=visible,
    )
    await assets.bind_uploaded_file(
        id="fa_1",
        section_id="sec_1",
        group_id=None,
        title="Policy",
        label="policy",
        file_name="policy.pdf",
        mime_type="application/pdf",
        origin="library",
        page_count=1,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=42,
        server_file_id="fl_1",
        parser_id="markitdown",
        created_at=1,
        updated_at=1,
        caller_user_id=owner,
        workspace_id="ws-1",
        visible_to=visible,
    )
    await assets.save_asset(
        id="fa_1",
        section_id="sec_1",
        group_id=None,
        title="Policy",
        label="policy",
        file_name="policy.pdf",
        mime_type="application/pdf",
        origin="library",
        page_count=1,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=42,
        server_file_id="fl_1",
        parser_id="markitdown",
        extracted_text="source",
        created_at=1,
        updated_at=1,
        caller_user_id=owner,
        workspace_id="ws-1",
        visible_to=visible,
    )
    index_store = MemoryVectorIndexStore()
    indexes = VectorIndexService(store=index_store)
    await indexes.save_index(
        id="vi_1",
        title="Index",
        handle="index",
        model="embed",
        dims=3,
        status="ready",
        server_collection_id="kc_1",
        server_collection_model="embed",
        last_error=None,
        members=(VectorIndexMember("fa_1", "embedded", "kd_1"),),
        history=(),
        created_at=1,
        updated_at=1,
        caller_user_id=owner,
        workspace_id="ws-1",
        visible_to=visible,
    )
    files = _FileService(owner)
    knowledge = _KnowledgeService()
    operations = DeletionOperationStore()
    service = AssetDeletionService(
        assets=assets,
        operation_store=operations,
        files=files,  # type: ignore[arg-type]
        knowledge=knowledge,  # type: ignore[arg-type]
        vector_indexes=indexes,
    )
    return service, assets, indexes, files, knowledge, principal, visible


def _terminal(
    service: AssetDeletionService,
    operation_id: str,
    principal: Principal,
    expected: str,
) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = service.get(operation_id, principal=principal, workspace_id="ws-1")
        if result["status"] == expected:
            return result
        time.sleep(0.01)
    raise AssertionError(f"operation did not reach {expected}")


@pytest.mark.asyncio
async def test_asset_delete_converges_all_resources_before_success() -> None:
    service, assets, indexes, files, knowledge, principal, visible = await _fixture()
    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    result = _terminal(service, started["operation_id"], principal, "deleted")

    assert result["completed_items"] == 1
    assert "fl_1" not in files.present
    assert "asset:fa_1" in knowledge.detached
    assert "asset:fa_1" not in knowledge.active
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")
    assert (
        await indexes.count_asset_memberships(
            "fa_1",
            scope=ResourceScope(principal.user_id, "ws-1"),
        )
        == 0
    )


@pytest.mark.asyncio
async def test_asset_delete_releases_file_binding_before_registry_delete() -> None:
    service, assets, _indexes, files, _knowledge, principal, visible = (
        await _fixture()
    )
    files.bound_assets = assets

    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    result = _terminal(service, started["operation_id"], principal, "deleted")

    assert result["completed_items"] == 1
    assert files.delete_calls == ["fl_1"]
    assert "fl_1" not in files.present
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")


@pytest.mark.asyncio
async def test_group_delete_keeps_child_sources_and_only_removes_membership() -> None:
    service, assets, indexes, files, knowledge, principal, visible = await _fixture()
    await assets.save_group(
        id="fg_1",
        section_id="sec_1",
        title="Dossier",
        created_at=1,
        updated_at=1,
        caller_user_id=principal.user_id,
        workspace_id="ws-1",
        visible_to=visible,
    )
    await assets.save_asset(
        id="fa_1",
        section_id="sec_1",
        group_id="fg_1",
        title="Policy",
        label="policy",
        file_name="policy.pdf",
        mime_type="application/pdf",
        origin="library",
        page_count=1,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=42,
        server_file_id="fl_1",
        parser_id="markitdown",
        extracted_text="source",
        created_at=1,
        updated_at=2,
        caller_user_id=principal.user_id,
        workspace_id="ws-1",
        visible_to=visible,
    )

    started = await service.start_group(
        "fg_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    completed = _terminal(
        service, started["operation_id"], principal, "deleted"
    )

    assert completed["asset_ids"] == []
    assert completed["target_kind"] == "group"
    assert not any(
        group.id == "fg_1"
        for group in await assets.list_groups(
            caller_user_id=principal.user_id,
            workspace_id="ws-1",
        )
    )
    assert (await assets.get_asset("fa_1", visible_to=visible)).group_id is None
    assert files.present == {"fl_1"}
    assert knowledge.active == {"asset:fa_1"}
    assert (
        await indexes.count_asset_memberships(
            "fa_1",
            scope=ResourceScope(principal.user_id, "ws-1"),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_group_delete_failure_retains_identity_and_retry_converges() -> None:
    service, assets, _indexes, _files, _knowledge, principal, visible = await _fixture()
    await assets.save_group(
        id="fg_retry",
        section_id="sec_1",
        title="Retry",
        created_at=1,
        updated_at=1,
        caller_user_id=principal.user_id,
        workspace_id="ws-1",
        visible_to=visible,
    )
    original_delete = assets.store.delete_group
    fail_once = True

    async def _fail_once(group_id: str, *, scope: ResourceScope) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("database unavailable")
        await original_delete(group_id, scope=scope)

    assets.store.delete_group = _fail_once  # type: ignore[method-assign]
    started = await service.start_group(
        "fg_retry",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    failed = _terminal(
        service, started["operation_id"], principal, "delete_failed"
    )
    assert failed["error"]["message"] == "database unavailable"
    assert any(
        group.id == "fg_retry"
        for group in await assets.list_groups(
            caller_user_id=principal.user_id,
            workspace_id="ws-1",
        )
    )

    await service.retry(
        started["operation_id"],
        principal=principal,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "deleted")
    assert not any(
        group.id == "fg_retry"
        for group in await assets.list_groups(
            caller_user_id=principal.user_id,
            workspace_id="ws-1",
        )
    )


@pytest.mark.asyncio
async def test_invisible_asset_id_cannot_mint_a_deletion_operation() -> None:
    service, assets, _indexes, _files, knowledge, owner, _visible = await _fixture()
    stranger = Principal(
        user_id=uuid.uuid4(),
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    stranger_visible = UserContext(
        principal=stranger,
        workspace_ids=("ws-attacker",),
    )

    with pytest.raises(AssetNotFound):
        await service.start_asset(
            "fa_1",
            principal=stranger,
            visible_to=stranger_visible,
            workspace_id="ws-attacker",
        )

    retained = await assets.get_asset(
        "fa_1",
        visible_to=UserContext(
            principal=owner,
            workspace_ids=("ws-1",),
        ),
    )
    assert retained.lifecycle_status == "active"
    assert "asset:fa_1" in knowledge.active
    assert service.list_operations(
        principal=stranger,
        workspace_id="ws-attacker",
        limit=10,
        after=None,
    ) == ([], None)


@pytest.mark.asyncio
async def test_bulk_delete_rejects_the_whole_request_when_one_id_is_unknown() -> None:
    service, assets, _indexes, _files, knowledge, principal, visible = (
        await _fixture()
    )

    with pytest.raises(AssetNotFound):
        await service.start_bulk(
            ("fa_1", "fa_foreign"),
            principal=principal,
            visible_to=visible,
            workspace_id="ws-1",
        )

    retained = await assets.get_asset("fa_1", visible_to=visible)
    assert retained.lifecycle_status == "active"
    assert "asset:fa_1" in knowledge.active
    assert service.list_operations(
        principal=principal,
        workspace_id="ws-1",
        limit=10,
        after=None,
    ) == ([], None)


@pytest.mark.asyncio
async def test_delete_refreshes_blob_manifest_after_winning_upload_binding() -> None:
    service, assets, _indexes, files, _knowledge, principal, visible = await _fixture()
    authority = _BindBeforeFenceAuthority()
    assets.store.bind_source_lifecycle_authority(authority)
    service.operation_store.bind_source_lifecycle_authority(authority)
    authority.register_active(
        SourceScope(
            tenant_id="default",
            source_id="asset:fa_1",
            owner_user_id=principal.user_id,
            workspace_id="ws-1",
        )
    )

    existing = await assets.store.get_asset("fa_1")
    assets.store._assets["fa_1"] = replace(  # type: ignore[attr-defined]
        existing,
        server_file_id=None,
        size_bytes=0,
        upload_status="awaiting_upload",
        upload_operation_id=None,
    )
    files.present = {"fl_late"}

    def _complete_upload_before_fence() -> None:
        run_coro_sync(
            assets.bind_uploaded_file(
                id="fa_1",
                section_id="sec_1",
                group_id=None,
                title="Policy",
                label="policy",
                file_name="policy.pdf",
                mime_type="application/pdf",
                origin="library",
                page_count=1,
                parse_status="parsed",
                parse_warning=None,
                text_truncated=False,
                size_bytes=42,
                server_file_id="fl_late",
                parser_id="markitdown",
                created_at=1,
                updated_at=2,
                caller_user_id=principal.user_id,
                workspace_id="ws-1",
                visible_to=visible,
            )
        )

    authority.before_fence = _complete_upload_before_fence
    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "deleted")

    record = service.operation_store.get_record(
        started["operation_id"],
        tenant_id="default",
        created_by_user_id=principal.user_id,
        workspace_id="ws-1",
    )
    assert record.manifest[0].server_file_id == "fl_late"
    assert record.manifest[0].size_bytes == 42
    assert files.delete_calls == ["fl_late"]
    assert files.present == set()


@pytest.mark.asyncio
async def test_failed_blob_delete_keeps_asset_and_retry_identity() -> None:
    service, assets, _indexes, files, _knowledge, principal, visible = await _fixture()
    files.fail = True
    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    failed = _terminal(service, started["operation_id"], principal, "delete_failed")
    retained = await assets.store.get_asset("fa_1")
    assert retained.lifecycle_status == "delete_failed"
    assert retained.deletion_operation_id == started["operation_id"]
    assert retained.server_file_id is None
    assert failed["retryable"] is True

    files.fail = False
    retried = await service.retry(
        started["operation_id"], principal=principal, workspace_id="ws-1"
    )
    assert retried["operation_id"] == started["operation_id"]
    _terminal(service, started["operation_id"], principal, "deleted")
    assert files.delete_calls == ["fl_1", "fl_1"]
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")


@pytest.mark.asyncio
async def test_missing_knowledge_dependency_never_confirms_linked_asset_deleted() -> (
    None
):
    service, assets, indexes, files, knowledge, principal, visible = await _fixture()
    service._knowledge = None  # type: ignore[assignment]

    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    failed = _terminal(service, started["operation_id"], principal, "delete_failed")

    assert failed["retryable"] is True
    assert failed["error"]["type"] == "dependency_unavailable"
    assert "knowledge deletion component" in failed["error"]["message"]
    retained = await assets.store.get_asset("fa_1")
    assert retained.lifecycle_status == "delete_failed"
    assert retained.deletion_operation_id == started["operation_id"]
    assert "fl_1" in files.present
    assert "asset:fa_1" in knowledge.active
    assert (
        await indexes.count_asset_memberships(
            "fa_1",
            scope=ResourceScope(principal.user_id, "ws-1"),
        )
        == 1
    )

    service._knowledge = knowledge  # type: ignore[assignment]
    await service.retry(
        started["operation_id"],
        principal=principal,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "deleted")
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")


@pytest.mark.asyncio
async def test_missing_cleanup_registries_are_not_interpreted_as_zero() -> None:
    service, assets, _indexes, files, _knowledge, principal, visible = await _fixture()
    service._knowledge = None  # type: ignore[assignment]
    service._vector_indexes = None  # type: ignore[assignment]

    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    failed = _terminal(service, started["operation_id"], principal, "delete_failed")

    assert failed["error"]["type"] == "dependency_unavailable"
    assert "fl_1" in files.present
    retained = await assets.store.get_asset("fa_1")
    assert retained.lifecycle_status == "delete_failed"


@pytest.mark.asyncio
async def test_missing_knowledge_dependency_accepts_proven_volatile_absence() -> None:
    service, assets, indexes, files, knowledge, principal, visible = await _fixture()
    knowledge.active.clear()
    await indexes.remove_asset_memberships(
        "fa_1",
        scope=ResourceScope(principal.user_id, "ws-1"),
    )
    service._knowledge = None  # type: ignore[assignment]

    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "deleted")

    assert "fl_1" not in files.present
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")


@pytest.mark.asyncio
async def test_empty_fenced_cleanup_plan_allows_retry_without_knowledge_service() -> (
    None
):
    service, assets, _indexes, files, knowledge, principal, visible = await _fixture()
    knowledge.active.clear()
    files.fail = True

    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "delete_failed")
    record = service.operation_store.get_record(
        started["operation_id"],
        tenant_id="default",
        created_by_user_id=principal.user_id,
        workspace_id="ws-1",
    )
    cleanup = record.manifest[0].source_cleanup_plan
    assert cleanup is not None
    assert cleanup["targets"] == []

    service._knowledge = None  # type: ignore[assignment]
    files.fail = False
    await service.retry(
        started["operation_id"],
        principal=principal,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "deleted")
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")


@pytest.mark.asyncio
async def test_delete_waits_for_running_upload_cleanup_before_terminal_success() -> (
    None
):
    service, assets, _indexes, files, _knowledge, principal, visible = await _fixture()
    upload = _UploadSettlement()
    service._uploads = upload  # type: ignore[assignment]
    existing = await assets.store.get_asset("fa_1")
    assets.store._assets["fa_1"] = replace(  # type: ignore[attr-defined]
        existing,
        upload_operation_id="upl_in_flight",
    )

    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    failed = _terminal(service, started["operation_id"], principal, "delete_failed")
    assert failed["retryable"] is True
    retained = await assets.store.get_asset("fa_1")
    assert retained.lifecycle_status == "delete_failed"
    assert files.present == set()

    upload.ready = True
    await service.retry(started["operation_id"], principal=principal, workspace_id="ws-1")
    _terminal(service, started["operation_id"], principal, "deleted")
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")
    _terminal(service, started["operation_id"], principal, "deleted")
    with pytest.raises(AssetNotFound):
        await assets.store.get_asset("fa_1")


@pytest.mark.asyncio
async def test_legacy_shared_blob_is_deleted_and_released_once_in_same_bulk() -> None:
    service, assets, _indexes, files, knowledge, principal, visible = await _fixture()
    original = await assets.store.get_asset("fa_1")
    # Deliberately bypass the current one-file/one-asset invariant to model a
    # historical duplicate that predates the constraint.
    assets.store._assets["fa_2"] = replace(  # type: ignore[attr-defined]
        original,
        id="fa_2",
        title="Historical duplicate",
    )
    knowledge.active.add("asset:fa_2")
    quota = _QuotaService()
    service.bind_quota_service(quota)  # type: ignore[arg-type]

    started = await service.start_bulk(
        ("fa_1", "fa_2"),
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "deleted")

    assert files.delete_calls == ["fl_1"]
    assert len(quota.adjustments) == 1
    assert quota.adjustments[0][1] == -42


@pytest.mark.asyncio
async def test_legacy_shared_blob_outside_manifest_fails_before_blob_delete() -> None:
    service, assets, _indexes, files, _knowledge, principal, visible = await _fixture()
    original = await assets.store.get_asset("fa_1")
    assets.store._assets["fa_2"] = replace(  # type: ignore[attr-defined]
        original,
        id="fa_2",
        title="Other asset",
    )

    started = await service.start_asset(
        "fa_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    _terminal(service, started["operation_id"], principal, "delete_failed")

    assert files.delete_calls == []
    assert "fl_1" in files.present
    assert (await assets.store.get_asset("fa_1")).lifecycle_status == "delete_failed"
    assert (await assets.store.get_asset("fa_2")).lifecycle_status == "active"


@pytest.mark.asyncio
async def test_ad_hoc_bulk_delete_has_a_bounded_manifest() -> None:
    service, _assets, _indexes, _files, _knowledge, principal, visible = (
        await _fixture()
    )

    with pytest.raises(ValueError, match="at most 200"):
        await service.start_bulk(
            tuple(f"fa_{index}" for index in range(201)),
            principal=principal,
            visible_to=visible,
            workspace_id="ws-1",
        )


@pytest.mark.asyncio
async def test_an_account_erasure_runs_as_one_operation_of_the_administrator() -> (
    None
):
    """The operation is registered under the id the account was marked with
    and runs the bound erasure as the administrator who started it."""
    service, *_unused, principal, _visible = await _fixture()
    erased: list[tuple[str, uuid.UUID, uuid.UUID | None, str]] = []

    class _Erasure:
        def execute(self, handle, *, tenant_id, user_id, actor_user_id):
            erased.append((tenant_id, user_id, actor_user_id, handle.operation_id))
            handle.complete()

    service.bind_user_erasure(_Erasure())
    account = uuid.uuid4()

    started = await service.start_user_erasure(
        account, principal=principal, operation_id="del_marked"
    )
    deadline = time.monotonic() + 2
    while (
        service.get("del_marked", principal=principal, workspace_id=None)["status"]
        != "deleted"
    ):
        assert time.monotonic() < deadline, "the erasure operation never finished"
        time.sleep(0.01)

    assert started["operation_id"] == "del_marked"
    assert erased == [("default", account, principal.user_id, "del_marked")]


def _orphaned_source(*, knowledge: "_KnowledgeService | None"):
    """A live source of the principal whose asset or document is gone."""
    authority = MemorySourceLifecycleAuthority()
    operations = DeletionOperationStore()
    operations.bind_source_lifecycle_authority(authority)
    service = AssetDeletionService(
        assets=None,  # type: ignore[arg-type]
        operation_store=operations,
        files=None,
        knowledge=knowledge,  # type: ignore[arg-type]
        vector_indexes=None,
    )
    principal = Principal(
        user_id=uuid.uuid4(), kind="oidc_session", tenant_id="default", role="member"
    )
    scope = SourceScope(
        tenant_id="default",
        source_id="document:orphan",
        owner_user_id=principal.user_id,
        workspace_id="ws-1",
    )
    authority.register_active(scope)
    if knowledge is not None:
        knowledge.active.add(scope.source_id)
    return service, authority, principal, scope


@pytest.mark.asyncio
async def test_an_orphaned_source_is_cleaned_up_before_its_tombstone() -> None:
    knowledge = _KnowledgeService()
    service, authority, principal, scope = _orphaned_source(knowledge=knowledge)

    started = await service.start_orphaned_source(
        scope.source_id, principal=principal, workspace_id="ws-1"
    )
    _terminal(service, started["operation_id"], principal, "deleted")

    assert scope.source_id in knowledge.detached
    assert scope.source_id not in knowledge.active
    # The tombstone: the source can never become live again.
    with pytest.raises(SourceLifecycleConflict):
        authority.register_active(scope)


@pytest.mark.asyncio
async def test_an_orphaned_source_with_knowledge_left_fails_visibly() -> None:
    knowledge = _KnowledgeService()

    async def remove_nothing(source_id: str, **_kwargs) -> None:
        del source_id

    knowledge.delete_source = remove_nothing  # type: ignore[method-assign]
    service, _authority, principal, scope = _orphaned_source(knowledge=knowledge)

    started = await service.start_orphaned_source(
        scope.source_id, principal=principal, workspace_id="ws-1"
    )
    _terminal(service, started["operation_id"], principal, "delete_failed")

    assert scope.source_id in knowledge.active


@pytest.mark.asyncio
async def test_without_the_knowledge_service_an_orphaned_source_is_not_assumed_empty() -> None:
    service, _authority, principal, scope = _orphaned_source(knowledge=None)

    started = await service.start_orphaned_source(
        scope.source_id, principal=principal, workspace_id="ws-1"
    )

    _terminal(service, started["operation_id"], principal, "delete_failed")

