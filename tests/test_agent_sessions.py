"""Ownership and atomic-claim contracts for Agent Desk sessions."""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.project.agent_sessions_memory import MemoryAgentSessionStore
from inqtrix.project.agent_sessions_ports import (
    AgentSessionGroupNotFound,
    AgentSessionNotFound,
)
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.services.agent_sessions_service import AgentSessionsService

ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOB = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class _HistoricalRuns:
    def __init__(
        self,
        owners: set[tuple[str | None, uuid.UUID | None]] | None = None,
    ) -> None:
        self.owners = owners or set()

    def session_owners(
        self, session_id: str
    ) -> set[tuple[str | None, uuid.UUID | None]]:
        del session_id
        return set(self.owners)


def _user(user_id: uuid.UUID, tenant_id: str = "default") -> UserContext:
    return UserContext(
        principal=Principal(
            user_id=user_id,
            kind="oidc_session",
            tenant_id=tenant_id,
            role="member",
        )
    )


@pytest.mark.asyncio
async def test_claim_does_not_overwrite_an_existing_session() -> None:
    store = MemoryAgentSessionStore()
    service = AgentSessionsService(store=store, run_store=_HistoricalRuns())
    first = await service.claim_session(
        "as_one",
        title="Original",
        caller_user_id=ALICE,
        workspace_id="ws-a",
        visible_to=_user(ALICE),
    )

    with pytest.raises(AgentSessionNotFound):
        await service.claim_session(
            "as_one",
            title="Hijack",
            caller_user_id=BOB,
            workspace_id="ws-b",
            visible_to=_user(BOB),
        )

    stored = await store.get_session("as_one")
    assert stored == first
    assert stored.title == "Original"
    assert stored.created_by_user_id == ALICE
    assert stored.workspace_id == "ws-a"


@pytest.mark.asyncio
async def test_deleted_session_can_only_be_reclaimed_by_historical_owner() -> None:
    runs = _HistoricalRuns({("default", ALICE)})
    service = AgentSessionsService(
        store=MemoryAgentSessionStore(), run_store=runs
    )

    with pytest.raises(AgentSessionNotFound):
        await service.claim_session(
            "as_deleted",
            title="Foreign",
            caller_user_id=BOB,
            workspace_id=None,
            visible_to=_user(BOB),
        )

    claimed = await service.claim_session(
        "as_deleted",
        title="Recovered",
        caller_user_id=ALICE,
        workspace_id=None,
        visible_to=_user(ALICE),
    )
    assert claimed.created_by_user_id == ALICE


@pytest.mark.asyncio
async def test_save_cannot_claim_a_deleted_historical_session() -> None:
    store = MemoryAgentSessionStore()
    service = AgentSessionsService(
        store=store,
        run_store=_HistoricalRuns({("default", ALICE)}),
    )

    with pytest.raises(AgentSessionNotFound):
        await service.save_session(
            id="as_deleted_save",
            title="Foreign",
            items_json="[]",
            group_id=None,
            created_at=1.0,
            updated_at=2.0,
            caller_user_id=BOB,
            workspace_id=None,
            visible_to=_user(BOB),
        )

    with pytest.raises(AgentSessionNotFound):
        await store.get_session("as_deleted_save")


@pytest.mark.asyncio
async def test_mixed_historical_owners_make_session_unclaimable() -> None:
    service = AgentSessionsService(
        store=MemoryAgentSessionStore(),
        run_store=_HistoricalRuns(
            {("default", ALICE), ("default", BOB)}
        ),
    )

    with pytest.raises(AgentSessionNotFound):
        await service.claim_session(
            "as_mixed",
            title="Ambiguous",
            caller_user_id=ALICE,
            workspace_id=None,
            visible_to=_user(ALICE),
        )


def test_memory_claim_is_atomic_across_threads() -> None:
    store = MemoryAgentSessionStore()

    def claim(user_id: uuid.UUID):
        return asyncio.run(
            store.claim_session(
                id="as_race",
                title=str(user_id),
                created_at=1.0,
                created_by_user_id=user_id,
                workspace_id=f"ws-{user_id}",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        landed = list(executor.map(claim, (ALICE, BOB)))

    assert landed[0] == landed[1]
    stored = asyncio.run(store.get_session("as_race"))
    assert stored == landed[0]
    assert stored.created_by_user_id in {ALICE, BOB}


@pytest.mark.asyncio
async def test_concurrent_session_saves_preserve_the_claim_winner() -> None:
    store = MemoryAgentSessionStore()
    services = [
        AgentSessionsService(store=store, run_store=_HistoricalRuns()),
        AgentSessionsService(store=store, run_store=_HistoricalRuns()),
    ]

    async def save(index: int):
        user_id = (ALICE, BOB)[index]
        return await services[index].save_session(
            id="as_save_race",
            title=str(user_id),
            items_json=f'["{user_id}"]',
            group_id=None,
            created_at=1.0,
            updated_at=2.0,
            caller_user_id=user_id,
            workspace_id=f"ws-{user_id}",
            visible_to=_user(user_id),
        )

    results = await asyncio.gather(
        save(0), save(1), return_exceptions=True
    )
    winners = [item for item in results if not isinstance(item, Exception)]
    losers = [item for item in results if isinstance(item, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], AgentSessionNotFound)
    assert await store.get_session("as_save_race") == winners[0]


@pytest.mark.asyncio
async def test_concurrent_group_saves_preserve_the_claim_winner() -> None:
    store = MemoryAgentSessionStore()
    service = AgentSessionsService(store=store)

    async def save(user_id: uuid.UUID):
        return await service.save_group(
            id="asg_race",
            title=str(user_id),
            created_at=1.0,
            updated_at=2.0,
            caller_user_id=user_id,
            workspace_id=f"ws-{user_id}",
            visible_to=_user(user_id),
        )

    results = await asyncio.gather(
        save(ALICE), save(BOB), return_exceptions=True
    )
    winners = [item for item in results if not isinstance(item, Exception)]
    losers = [item for item in results if isinstance(item, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], AgentSessionGroupNotFound)
    stored = await store.list_groups(
        created_by_user_id=None, workspace_id=None
    )
    assert stored == winners


@pytest.mark.asyncio
async def test_stale_owner_cannot_overwrite_a_reclaimed_session_or_group() -> None:
    store = MemoryAgentSessionStore()
    await store.claim_session(
        id="as_reclaim", title="Alice", created_at=1.0,
        created_by_user_id=ALICE, workspace_id="ws-a",
    )
    stale_session = await store.get_session("as_reclaim")
    await store.delete_session(
        "as_reclaim", scope=ResourceScope.from_record(stale_session)
    )
    attacker = await store.claim_session(
        id="as_reclaim", title="Bob", created_at=2.0,
        created_by_user_id=BOB, workspace_id="ws-b",
    )
    with pytest.raises(AgentSessionNotFound):
        await store.upsert_session(
            id="as_reclaim", title="Stale", items_json="[1]",
            group_id=None, created_at=1.0, updated_at=3.0,
            created_by_user_id=ALICE, workspace_id="ws-a",
        )
    assert await store.get_session("as_reclaim") == attacker

    await store.claim_group(
        id="asg_reclaim", title="Alice", created_at=1.0,
        created_by_user_id=ALICE, workspace_id="ws-a",
    )
    stale_group = next(
        group
        for group in await store.list_groups(
            created_by_user_id=ALICE, workspace_id="ws-a"
        )
        if group.id == "asg_reclaim"
    )
    await store.delete_group(
        "asg_reclaim", scope=ResourceScope.from_record(stale_group)
    )
    attacker_group = await store.claim_group(
        id="asg_reclaim", title="Bob", created_at=2.0,
        created_by_user_id=BOB, workspace_id="ws-b",
    )
    with pytest.raises(AgentSessionGroupNotFound):
        await store.upsert_group(
            id="asg_reclaim", title="Stale", created_at=1.0,
            updated_at=3.0, created_by_user_id=ALICE, workspace_id="ws-a",
        )
    groups = await store.list_groups(created_by_user_id=None, workspace_id=None)
    assert groups == [attacker_group]


@pytest.mark.asyncio
async def test_the_session_list_carries_each_settings_body() -> None:
    """An agent session's ``items_json`` is a small settings body (source
    policy, knowledge switch, model pick). A client that compares its
    sessions against list rows without it reads default settings and saves
    every session with a pick of its own again."""
    service = AgentSessionsService(
        store=MemoryAgentSessionStore(), run_store=_HistoricalRuns()
    )
    body = '{"source_policy": {"web": "disabled", "knowledge": "available"}}'
    await service.save_session(
        id="as_listed", title="Liste", items_json=body, group_id=None,
        created_at=1.0, updated_at=2.0, caller_user_id=ALICE,
        workspace_id=None, visible_to=_user(ALICE),
    )

    listed = await service.list_sessions(caller_user_id=ALICE, workspace_id=None)

    assert [(session.id, session.items_json) for session in listed] == [("as_listed", body)]
