from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest

from inqtrix.auth.permissions import AccessMode, ResourceAccess
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.knowledge.stores.ports import CollectionNotFound, KnowledgeCollection
from inqtrix.project.asset_records_memory import MemoryAssetStore
from inqtrix.project.vector_index_memory import MemoryVectorIndexStore
from inqtrix.project.vector_index_ports import VectorIndexNotFound
from inqtrix.runs.deletion_operations import DeletionOperationStore
from inqtrix.services.asset_deletion_service import AssetDeletionService
from inqtrix.services.asset_records_service import AssetRecordsService
from inqtrix.services.vector_index_service import VectorIndexService


class _Knowledge:
    def __init__(self, owner: uuid.UUID) -> None:
        self.collection = KnowledgeCollection(
            id="kc_1",
            name="Index",
            embedding_model="embed",
            embedding_dim=3,
            created_at=1,
            created_by_user_id=owner,
        )
        self.knowledge = SimpleNamespace(store=self)
        self.fail_after_delete_once = False
        self.forced_residuals: dict[str, int] = {}
        self.delete_calls = 0

    async def get_collection(self, collection_id: str) -> KnowledgeCollection:
        if self.collection is None or self.collection.id != collection_id:
            raise CollectionNotFound(collection_id)
        return self.collection

    async def collection_access(self, collection, visible_to):
        if visible_to.principal.user_id != collection.created_by_user_id:
            raise CollectionNotFound(collection.id)
        return ResourceAccess(AccessMode.OWNER)

    async def delete_collection_for_aggregate(self, collection_id: str, **_kwargs):
        self.delete_calls += 1
        if self.collection is not None and self.collection.id == collection_id:
            self.collection = None
        if self.fail_after_delete_once:
            self.fail_after_delete_once = False
            raise RuntimeError("worker interrupted after collection delete")

    async def collection_residuals(self, _collection_id: str, **_kwargs):
        return dict(self.forced_residuals)


class _IndexingJobs:
    def __init__(self) -> None:
        self.active = True
        self.fence_calls = 0

    def fence_collection_for_deletion(self, collection_id: str, **_kwargs) -> int:
        assert collection_id == "kc_1"
        self.fence_calls += 1
        was_active = self.active
        self.active = False
        return int(was_active)

    def has_active_job(self, collection_id: str) -> bool:
        assert collection_id == "kc_1"
        return self.active


async def _harness(*, server_collection_id: str | None = "kc_1"):
    owner = uuid.uuid4()
    principal = Principal(
        user_id=owner,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    visible = UserContext(principal=principal, workspace_ids=("ws-1",))
    indexes = VectorIndexService(store=MemoryVectorIndexStore())
    await indexes.save_index(
        id="vi_1",
        title="Index",
        handle="index",
        model="embed",
        dims=3,
        status="ready",
        server_collection_id=server_collection_id,
        server_collection_model="embed",
        last_error=None,
        members=(),
        history=(),
        created_at=1,
        updated_at=1,
        caller_user_id=owner,
        workspace_id="ws-1",
        visible_to=visible,
    )
    knowledge = _Knowledge(owner)
    jobs = _IndexingJobs()
    operations = DeletionOperationStore()
    service = AssetDeletionService(
        assets=AssetRecordsService(store=MemoryAssetStore()),
        operation_store=operations,
        files=None,
        knowledge=knowledge,  # type: ignore[arg-type]
        vector_indexes=indexes,
        indexing_jobs=jobs,
    )
    return service, indexes, knowledge, jobs, principal, visible


def _terminal(service, operation_id, principal, expected):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        summary = service.get(
            operation_id, principal=principal, workspace_id="ws-1"
        )
        if summary["status"] == expected:
            return summary
        time.sleep(0.01)
    raise AssertionError(f"operation did not reach {expected}")


@pytest.mark.asyncio
async def test_vector_index_delete_fences_search_job_and_verifies_zero_residue():
    service, indexes, knowledge, jobs, principal, visible = await _harness()
    started = await service.start_vector_index(
        "vi_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    terminal = _terminal(service, started["operation_id"], principal, "deleted")

    assert terminal["completed_items"] == terminal["total_items"] == 4
    assert jobs.fence_calls == 1
    assert knowledge.collection is None
    with pytest.raises(VectorIndexNotFound):
        await indexes.store.get_index("vi_1")


@pytest.mark.asyncio
async def test_retry_after_collection_delete_uses_persisted_recovery_context():
    service, indexes, knowledge, _jobs, principal, visible = await _harness()
    knowledge.fail_after_delete_once = True
    started = await service.start_vector_index(
        "vi_1",
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
    assert record.vector_index_context is not None
    assert record.vector_index_context.index_id == "vi_1"
    assert record.vector_index_context.server_collection_id == "kc_1"
    assert (await indexes.store.get_index("vi_1")).status == "delete_failed"

    retried = await service.retry(
        started["operation_id"], principal=principal, workspace_id="ws-1"
    )
    _terminal(service, retried["operation_id"], principal, "deleted")
    assert knowledge.delete_calls == 2


@pytest.mark.asyncio
async def test_delete_uses_authorized_client_retained_collection_identity():
    service, indexes, knowledge, jobs, principal, visible = await _harness(
        server_collection_id=None
    )
    started = await service.start_vector_index(
        "vi_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
        server_collection_id_hint="kc_1",
    )
    terminal = _terminal(service, started["operation_id"], principal, "deleted")
    record = service.operation_store.get_record(
        started["operation_id"],
        tenant_id="default",
        created_by_user_id=principal.user_id,
        workspace_id="ws-1",
    )

    assert terminal["completed_items"] == terminal["total_items"] == 4
    assert record.vector_index_context is not None
    assert record.vector_index_context.server_collection_id == "kc_1"
    assert jobs.fence_calls == 1
    assert knowledge.collection is None
    with pytest.raises(VectorIndexNotFound):
        await indexes.store.get_index("vi_1")


@pytest.mark.asyncio
async def test_non_owner_cannot_create_vector_index_deletion_receipt():
    service, _indexes, _knowledge, _jobs, _principal, _visible = await _harness()
    attacker = Principal(
        user_id=uuid.uuid4(),
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    with pytest.raises(VectorIndexNotFound):
        await service.start_vector_index(
            "vi_1",
            principal=attacker,
            visible_to=UserContext(principal=attacker, workspace_ids=("ws-1",)),
            workspace_id="ws-1",
        )


@pytest.mark.asyncio
async def test_residual_collection_data_keeps_retry_pointer_visible():
    service, indexes, knowledge, _jobs, principal, visible = await _harness()
    knowledge.forced_residuals = {"vectors": 1}
    started = await service.start_vector_index(
        "vi_1",
        principal=principal,
        visible_to=visible,
        workspace_id="ws-1",
    )
    failed = _terminal(
        service, started["operation_id"], principal, "delete_failed"
    )
    assert failed["retryable"] is True
    assert (await indexes.store.get_index("vi_1")).status == "delete_failed"
