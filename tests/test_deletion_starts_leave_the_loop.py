"""A deletion start never makes the event loop wait for the deletion store.

Registering an operation can wait for row and tenant locks that other
requests hold. Those requests' transactions run on the same event loop and
can only commit while it keeps running, so a start that blocked the loop
would wait for itself: every request of the process stops until a timeout
breaks the cycle. Every start awaits the store's async name.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from inqtrix.auth.lifecycle import AdminActor
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.runs.deletion_operations import (
    DeletionTargetKind,
)
from inqtrix.services.asset_deletion_service import AssetDeletionService

OWNER = uuid.uuid4()
PRINCIPAL = Principal(user_id=OWNER, kind="oidc_session", tenant_id="default", role="member")
VISIBLE = UserContext(principal=PRINCIPAL, workspace_ids=())


class _Store:
    """The deletion store's write path; its sync name would block the loop."""

    def __init__(self) -> None:
        self.registered: list[DeletionTargetKind] = []

    async def afind_for_target(self, *args, **kwargs):
        return None

    async def asubmit(self, **kwargs):
        self.registered.append(kwargs["target_kind"])
        return {"operation_id": "del_probe", "status": "queued"}

    def submit(self, **kwargs):
        raise AssertionError("the sync name would block the loop")


class _Assets:
    async def assets_for_section(self, section_id, *, visible_to):
        return ()


class _Knowledge:
    """Owner access to one collection and one document in it."""

    def __init__(self) -> None:
        collection = SimpleNamespace(id="kc_1", embedding_model="embed")
        self._collection = collection
        self.knowledge = SimpleNamespace(store=self)

    async def get_collection(self, collection_id):
        return self._collection

    async def collection_access(self, collection, visible_to):
        return SimpleNamespace(mode=SimpleNamespace(value="owner"))

    async def prepare_document_deletion(self, document_id, *, visible_to):
        return SimpleNamespace(id=document_id), self._collection


class _Sessions:
    async def prepare_deletion(self, session_id, *, visible_to, request_workspace_id):
        return SimpleNamespace(
            id=session_id,
            created_by_user_id=OWNER,
            workspace_id=request_workspace_id,
        )


def _service(store) -> AssetDeletionService:
    service = AssetDeletionService(
        assets=_Assets(),  # type: ignore[arg-type]
        operation_store=store,
        files=None,
        knowledge=_Knowledge(),  # type: ignore[arg-type]
        vector_indexes=None,
    )
    service.bind_session_deletion(
        agent_sessions=_Sessions(),  # type: ignore[arg-type]
        knowledge_sessions=_Sessions(),  # type: ignore[arg-type]
    )
    service.bind_user_erasure(object())
    return service


STARTS = {
    DeletionTargetKind.SECTION: lambda s: s.start_section(
        "fsec_1", principal=PRINCIPAL, visible_to=VISIBLE, workspace_id=None
    ),
    DeletionTargetKind.KNOWLEDGE_COLLECTION: lambda s: s.start_knowledge_collection(
        "kc_1", principal=PRINCIPAL, visible_to=VISIBLE
    ),
    DeletionTargetKind.KNOWLEDGE_DOCUMENT: lambda s: s.start_knowledge_document(
        "kd_1", principal=PRINCIPAL, visible_to=VISIBLE
    ),
    DeletionTargetKind.AGENT_SESSION: lambda s: s.start_agent_session(
        "as_1", principal=PRINCIPAL, visible_to=VISIBLE, workspace_id="ws-1"
    ),
    DeletionTargetKind.KNOWLEDGE_SESSION: lambda s: s.start_knowledge_session(
        "ks_1", principal=PRINCIPAL, visible_to=VISIBLE, workspace_id="ws-1"
    ),
    DeletionTargetKind.USER: lambda s: s.start_user_erasure(
        uuid.uuid4(),
        actor=AdminActor(PRINCIPAL.user_id),
        tenant_id=PRINCIPAL.tenant_id,
        operation_id="del_probe",
    ),
    DeletionTargetKind.SOURCE: lambda s: s.start_orphaned_source(
        "document:orphan", principal=PRINCIPAL, workspace_id=None
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(STARTS), ids=lambda kind: kind.value)
async def test_every_start_awaits_the_store(kind) -> None:
    store = _Store()

    summary = await STARTS[kind](_service(store))

    assert summary["operation_id"] == "del_probe"
    assert store.registered == [kind]


@pytest.mark.asyncio
async def test_a_registration_waiting_for_another_request_lets_it_finish() -> None:
    """The registration waits for a lock another request on this loop holds.

    That request releases it only if the loop keeps running; with a
    registration that blocks the loop, both would wait until a timeout.
    """
    released = asyncio.Event()

    class _LockedStore(_Store):
        async def asubmit(self, **kwargs):
            await asyncio.wait_for(released.wait(), timeout=5)
            return await super().asubmit(**kwargs)

    async def other_request() -> None:
        released.set()

    other = asyncio.create_task(other_request())
    summary = await STARTS[DeletionTargetKind.USER](_service(_LockedStore()))
    await other

    assert summary["operation_id"] == "del_probe"
