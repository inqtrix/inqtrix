"""User invalidations remain bounded, scoped, and content-free."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from inqtrix.auth.principal import Principal
from inqtrix.server.routers.user_events import (
    _invalidation_frame,
    _sse_frame,
    build_router,
)
from inqtrix.user_events import (
    MemoryUserEventStore,
    UserEventPage,
    UserInvalidation,
)


@pytest.mark.asyncio
async def test_memory_user_events_replay_only_the_target_users_frames() -> None:
    store = MemoryUserEventStore()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    await store.append(
        tenant_id="default",
        target_user_id=user_a,
        scope="runs",
        resource_type="run",
        resource_id="run_a",
    )
    await store.append(
        tenant_id="default",
        target_user_id=user_b,
        scope="prompts",
        resource_type="prompt_template",
        resource_id="pt_b",
    )

    page = await store.page_after(
        tenant_id="default", target_user_id=user_a, cursor=0
    )

    assert page.reset_required is False
    assert [(event.scope, event.resource_id) for event in page.events] == [
        ("runs", "run_a")
    ]
    assert page.current_cursor == 2


@pytest.mark.asyncio
async def test_memory_user_events_request_reset_after_bounded_eviction() -> None:
    store = MemoryUserEventStore(max_events=1)
    user_id = uuid.uuid4()
    await store.append(
        tenant_id="default", target_user_id=user_id, scope="runs"
    )
    await store.append(
        tenant_id="default", target_user_id=user_id, scope="skills"
    )

    page = await store.page_after(
        tenant_id="default", target_user_id=user_id, cursor=1
    )

    assert page.reset_required is True
    assert page.events == ()
    assert page.current_cursor == 2


@pytest.mark.asyncio
async def test_memory_user_events_reset_a_cursor_from_before_restart() -> None:
    store = MemoryUserEventStore()
    user_id = uuid.uuid4()
    await store.append(
        tenant_id="default", target_user_id=user_id, scope="runs"
    )

    page = await store.page_after(
        tenant_id="default", target_user_id=user_id, cursor=500
    )

    assert page.reset_required is True
    assert page.events == ()
    assert page.current_cursor == 1


def test_ready_and_reset_sse_frames_have_the_public_contract() -> None:
    user_id = uuid.uuid4()

    ready = _sse_frame(
        "ready", {"user_id": str(user_id), "cursor": "17"}
    )
    reset = _sse_frame("reset", {})

    assert "event: ready" in ready
    assert json.loads(ready.split("data: ", 1)[1]) == {
        "user_id": str(user_id),
        "cursor": "17",
    }
    assert "data: {}" in reset


@pytest.mark.asyncio
async def test_invalidation_frame_never_serializes_an_unrelated_payload() -> None:
    store = MemoryUserEventStore()
    event = await store.append(
        tenant_id="default",
        target_user_id=uuid.uuid4(),
        scope="skills",
        resource_type="skill_template",
        resource_id="skill_1",
    )

    frame = _invalidation_frame(event)

    assert "id: 1" in frame
    assert '"scope":"skills"' in frame
    assert '"resource_id":"skill_1"' in frame
    assert "title" not in frame
    assert "content" not in frame
    assert "permission" not in frame


class _ConnectedRequest:
    def __init__(self, last_event_id: str = "0") -> None:
        self.headers = {"last-event-id": last_event_id}

    async def is_disconnected(self) -> bool:
        return False


class _ReplayStore:
    def __init__(self, event: UserInvalidation) -> None:
        self.event = event

    async def current_cursor(self, *, tenant_id: str) -> int:
        del tenant_id
        return self.event.id

    async def page_after(self, **kwargs) -> UserEventPage:
        cursor = int(kwargs["cursor"])
        return UserEventPage(
            (self.event,) if cursor < self.event.id else (),
            self.event.id,
        )

    async def wait_for_change(self, **kwargs) -> None:
        del kwargs


def _event_endpoint(container) -> object:
    router = build_router(container)
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/v1/user/events"
    )


@pytest.mark.asyncio
async def test_user_stream_sends_ready_before_replayed_invalidation() -> None:
    user_id = uuid.uuid4()
    principal = Principal(user_id=user_id, kind="oidc_session")
    event = UserInvalidation(
        id=4,
        tenant_id="default",
        target_user_id=user_id,
        scope="runs",
        resource_type="run",
        resource_id="run_4",
    )

    async def resolve(_request):
        return principal

    endpoint = _event_endpoint(
        SimpleNamespace(
            user_event_store=_ReplayStore(event),
            principal_dependency=resolve,
        )
    )
    response = await endpoint(_ConnectedRequest("3"), principal)
    iterator = response.body_iterator

    ready = await anext(iterator)
    invalidation = await anext(iterator)
    await iterator.aclose()

    assert "event: ready" in ready
    assert f'"user_id":"{user_id}"' in ready
    assert "id: 4" in invalidation
    assert "event: invalidate" in invalidation


@pytest.mark.asyncio
async def test_user_stream_rechecks_identity_before_each_data_frame() -> None:
    user_id = uuid.uuid4()
    principal = Principal(user_id=user_id, kind="oidc_session")
    event = UserInvalidation(
        id=2,
        tenant_id="default",
        target_user_id=user_id,
        scope="skills",
    )
    calls = 0

    async def resolve(_request):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise HTTPException(status_code=401)
        return principal

    endpoint = _event_endpoint(
        SimpleNamespace(
            user_event_store=_ReplayStore(event),
            principal_dependency=resolve,
        )
    )
    response = await endpoint(_ConnectedRequest("1"), principal)
    iterator = response.body_iterator

    ready = await anext(iterator)

    assert "event: ready" in ready
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


@pytest.mark.asyncio
async def test_user_stream_rechecks_identity_before_quiet_keepalive() -> None:
    user_id = uuid.uuid4()
    principal = Principal(user_id=user_id, kind="oidc_session")
    event = UserInvalidation(
        id=2,
        tenant_id="default",
        target_user_id=user_id,
        scope="skills",
    )
    calls = 0

    async def resolve(_request):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise HTTPException(status_code=401)
        return principal

    endpoint = _event_endpoint(
        SimpleNamespace(
            user_event_store=_ReplayStore(event),
            principal_dependency=resolve,
        )
    )
    response = await endpoint(_ConnectedRequest("2"), principal)
    iterator = response.body_iterator

    ready = await anext(iterator)

    assert "event: ready" in ready
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


def test_postgres_store_constructor_rejects_nonpositive_knobs() -> None:
    """All three sizing guards, offline: importing the store needs no DB.

    Without this, only gated integration runs would exercise these, and
    the default lane would have no coverage for a plain ValueError.
    """
    from inqtrix.storage.user_events_postgres import PostgresUserEventStore

    for kwargs in (
        {"retention_seconds": 0},
        {"poll_seconds": 0},
        {"cleanup_interval_seconds": 0},
    ):
        (name,) = kwargs
        with pytest.raises(ValueError, match=name):
            PostgresUserEventStore(
                session_factory=None, app_role="inqtrix_app", **kwargs
            )


# -- shared change poll of the Postgres store, offline ---------------------
#
# Every waiting stream of one tenant in one event loop shares one poll per
# interval. The poll's database read is replaced here, so these tests pin the
# registration, wake and failure rules without a database.

POLL_TENANT = "poll-tenant"
POLL_USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_POLL_USER = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _polled_store(poll_seconds: float, answers):
    """A Postgres user-event store whose poll answers from *answers*: a list
    consumed one entry per poll (the last entry repeats); an exception entry
    is raised by that poll."""
    from inqtrix.storage.user_events_postgres import PostgresUserEventStore

    store = PostgresUserEventStore(
        session_factory=None, app_role="", poll_seconds=poll_seconds
    )
    polls: list[tuple[str, tuple[uuid.UUID, ...]]] = []

    async def newest_event_ids(tenant_id, user_ids):
        polls.append((tenant_id, tuple(user_ids)))
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    store._newest_event_ids = newest_event_ids
    return store, polls


def test_shared_poll_wakes_only_streams_whose_user_has_a_newer_event() -> None:
    import asyncio
    import time

    store, polls = _polled_store(0.02, [{}, {POLL_USER: 9}])

    async def scenario() -> dict[str, float | None]:
        woken: dict[str, float | None] = {}
        started = time.monotonic()

        async def wait(name: str, user: uuid.UUID, cursor: int) -> None:
            await store.wait_for_change(
                tenant_id=POLL_TENANT,
                target_user_id=user,
                cursor=cursor,
                timeout=0.5,
            )
            elapsed = time.monotonic() - started
            woken[name] = elapsed if elapsed < 0.4 else None

        await asyncio.gather(
            wait("behind", POLL_USER, 5),
            wait("current", POLL_USER, 9),
            wait("other user", OTHER_POLL_USER, 0),
        )
        return woken

    woken = asyncio.run(scenario())

    assert woken["behind"] is not None
    assert woken["current"] is None
    assert woken["other user"] is None
    # One read per interval for all three streams, each naming both users.
    assert all(
        tenant == POLL_TENANT and set(users) == {POLL_USER, OTHER_POLL_USER}
        for tenant, users in polls[:2]
    )
    assert len(polls) <= 0.5 / 0.02 + 3


def test_shared_poll_wakes_a_stream_within_one_interval() -> None:
    import asyncio
    import time

    store, _polls = _polled_store(0.05, [{}, {}, {POLL_USER: 9}])

    async def scenario() -> float:
        started = time.monotonic()
        await store.wait_for_change(
            tenant_id=POLL_TENANT, target_user_id=POLL_USER, cursor=5, timeout=2.0
        )
        return time.monotonic() - started

    # Two empty polls, then the event: woken at the third poll.
    assert asyncio.run(scenario()) < 0.05 * 2 + 0.08


def test_a_stream_that_ends_leaves_the_shared_poll_and_stops_it() -> None:
    import asyncio

    store, polls = _polled_store(0.02, [{}])

    async def scenario() -> None:
        waiting = asyncio.create_task(
            store.wait_for_change(
                tenant_id=POLL_TENANT,
                target_user_id=POLL_USER,
                cursor=5,
                timeout=30.0,
            )
        )
        await asyncio.sleep(0.1)
        assert polls
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0)
        polled = len(polls)
        await asyncio.sleep(0.1)
        assert len(polls) == polled
        assert store._change_polls == {}

    asyncio.run(scenario())


def test_the_last_stream_leaving_cancels_the_sleeping_poll_at_once() -> None:
    import asyncio

    store, _polls = _polled_store(30.0, [{}])

    async def scenario() -> list[asyncio.Task]:
        await store.wait_for_change(
            tenant_id=POLL_TENANT, target_user_id=POLL_USER, cursor=5, timeout=0.05
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    assert asyncio.run(scenario()) == []


def test_a_failed_poll_warns_once_keeps_polling_and_reports_recovery(caplog) -> None:
    import asyncio
    import logging
    import time

    store, _polls = _polled_store(
        0.02,
        [RuntimeError("database unavailable"), RuntimeError("database unavailable"), {POLL_USER: 9}],
    )

    async def scenario() -> float:
        started = time.monotonic()
        await store.wait_for_change(
            tenant_id=POLL_TENANT, target_user_id=POLL_USER, cursor=5, timeout=5.0
        )
        return time.monotonic() - started

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        elapsed = asyncio.run(scenario())

    assert elapsed < 1.0
    # The texts are the store's own; a rewording must be caught here.
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("User event change poll failed")
    ]
    assert len(warnings) == 1
    assert any(
        record.levelno == logging.INFO
        and record.getMessage()
        == "User event change poll recovered after 2 failed attempts."
        for record in caplog.records
    )


def test_shared_poll_of_one_tenant_never_names_or_wakes_another_tenants_streams() -> None:
    """Each tenant has its own poll: a user id that exists in two tenants
    is probed per tenant, and an event in one tenant wakes no stream of the
    other."""
    import asyncio
    import time

    from inqtrix.storage.user_events_postgres import PostgresUserEventStore

    store = PostgresUserEventStore(
        session_factory=None, app_role="", poll_seconds=0.02
    )
    events_by_tenant = {"tenant-a": {POLL_USER: 9}, "tenant-b": {}}
    polls: list[tuple[str, frozenset[uuid.UUID]]] = []

    async def newest_event_ids(tenant_id, user_ids):
        polls.append((tenant_id, frozenset(user_ids)))
        return dict(events_by_tenant[tenant_id])

    store._newest_event_ids = newest_event_ids

    async def scenario() -> dict[str, float | None]:
        woken: dict[str, float | None] = {}
        started = time.monotonic()

        async def wait(name: str, tenant_id: str, user: uuid.UUID) -> None:
            await store.wait_for_change(
                tenant_id=tenant_id, target_user_id=user, cursor=5, timeout=0.4
            )
            elapsed = time.monotonic() - started
            woken[name] = elapsed if elapsed < 0.3 else None

        await asyncio.gather(
            wait("tenant b, same user id", "tenant-b", POLL_USER),
            wait("tenant b, other user", "tenant-b", OTHER_POLL_USER),
            wait("tenant a", "tenant-a", POLL_USER),
        )
        return woken

    woken = asyncio.run(scenario())

    assert woken["tenant a"] is not None
    assert woken["tenant b, same user id"] is None
    assert woken["tenant b, other user"] is None
    assert {tenant for tenant, _users in polls} == {"tenant-a", "tenant-b"}
    for tenant, users in polls:
        expected = (
            {POLL_USER} if tenant == "tenant-a" else {POLL_USER, OTHER_POLL_USER}
        )
        assert users == expected, (tenant, users)
    assert store._change_polls == {}
