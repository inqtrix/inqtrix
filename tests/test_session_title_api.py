"""Request-level title admission, scope and late-result behavior."""

import asyncio
import threading
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.project.agent_sessions_memory import MemoryAgentSessionStore
from inqtrix.project.chat_memory import MemoryChatStore
from inqtrix.server.execution import ExecutionLanes
from inqtrix.server.routers import agent_sessions, chat_history, session_titles
from inqtrix.services.agent_sessions_service import AgentSessionsService
from inqtrix.services.chat_history_service import ChatHistoryService
from tests.test_session_titler import QuotaRecorder, TitleLLM

OWNER = UUID(int=101)


class Quota(QuotaRecorder):
    async def check(self, *args):
        return None

    def subject_for(self, principal):
        return principal.user_id


@pytest_asyncio.fixture(params=["chat", "agent"])
async def api(request):
    kind = request.param
    llm, quota = TitleLLM(), Quota()
    chat = ChatHistoryService(store=MemoryChatStore())
    agent = AgentSessionsService(store=MemoryAgentSessionStore())
    store = chat.store if kind == "chat" else agent.store
    common = dict(
        id="subject",
        title="Initial",
        created_at=1.0,
        updated_at=1.0,
        created_by_user_id=OWNER,
        workspace_id="workspace-one",
        group_id=None,
        title_revision=0,
        title_source="initial",
    )

    async def save(title, source, revision=0):
        values = {
            **common,
            "title": title,
            "title_source": source,
            "title_revision": revision,
        }
        if kind == "chat":
            return await store.upsert_thread(**values, preview="", source="api")
        return await store.upsert_session(**values, items_json="[]")

    await save("Initial", "initial")

    async def principal(req: Request):
        return Principal(
            user_id=UUID(int=int(req.headers.get("x-test-user", "101"))),
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )

    async def context(req: Request):
        return UserContext(principal=await principal(req))

    lanes = ExecutionLanes(ai_workers=1, stream_workers=1)
    container = SimpleNamespace(
        principal_dependency=principal,
        user_context_dependency=context,
        resolver=SimpleNamespace(
            resolve=lambda _: SimpleNamespace(
                providers=SimpleNamespace(llm=llm),
                agent_settings=SimpleNamespace(max_question_length=1000),
            )
        ),
        settings=SimpleNamespace(server=SimpleNamespace(session_titles_enabled=True)),
        chat_history_service=chat,
        agent_sessions_service=agent,
        quota_service=quota,
        execution_lanes=lanes,
        asset_deletion_service=None,
        agent_control_service=None,
    )
    app = FastAPI()
    app.include_router(session_titles.build_router(container))
    app.include_router(chat_history.build_router(container))
    app.include_router(agent_sessions.build_router(container))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client,
            kind=kind,
            llm=llm,
            quota=quota,
            save=save,
            container=container,
        )
    lanes.close()


def body(api):
    return {
        "question": "Wie funktioniert ein digitaler Zwilling?",
        "language": "de",
        "target": {"kind": api.kind, "id": "subject", "revision": 0},
    }


def timeout_after_provider_starts(monkeypatch, started):
    """Exercise HTTP cancellation only after the real worker owns capacity.

    Thread startup is not the behavior under test. A tiny wall-clock deadline
    before startup can skip the provider entirely on a busy test machine.
    """
    async def wait_for_started(awaitable, *, timeout):
        assert await asyncio.to_thread(started.wait, 2), "Provider did not start"
        return await asyncio.wait_for(awaitable, timeout=0.01)

    monkeypatch.setattr(
        session_titles,
        "asyncio",
        SimpleNamespace(
            get_running_loop=asyncio.get_running_loop,
            shield=asyncio.shield,
            wait_for=wait_for_started,
            TimeoutError=asyncio.TimeoutError,
        ),
    )


@pytest.mark.asyncio
async def test_metadata_api_reports_preserved_titles_and_refuses_legacy_overwrites(api):
    url = (
        "/v1/chat/threads/subject"
        if api.kind == "chat"
        else "/v1/agent-sessions/subject"
    )
    data = {
        "title": "My project",
        "title_revision": 0,
        "title_source": "manual",
        "created_at": 1.0,
        "updated_at": 2.0,
        "group_id": None,
        **(
            {"preview": "latest", "source": "api", "model_selection": ""}
            if api.kind == "chat"
            else {"items_json": "[]"}
        ),
    }
    headers = {"x-inqtrix-workspace-id": "workspace-one"}
    saved = await api.client.put(url, json=data, headers=headers)
    assert saved.status_code == 200, saved.text
    assert (
        saved.json()["title"],
        saved.json()["title_revision"],
        saved.json()["title_source"],
    ) == ("My project", 1, "manual")
    echoed = await api.client.put(
        url,
        json={**data, "title": "Initial", "title_source": "initial", "updated_at": 3.0},
        headers=headers,
    )
    assert echoed.json()["title"] == "My project"
    assert echoed.json()["title_preserved"] is True
    legacy = {
        key: value
        for key, value in data.items()
        if key not in {"title_revision", "title_source"}
    }
    refused = await api.client.put(
        url, json={**legacy, "title": "Old echo", "updated_at": 99.0}, headers=headers
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "session_title_conflict"
    service = (
        api.container.chat_history_service
        if api.kind == "chat"
        else api.container.agent_sessions_service
    )
    row = await (
        service.store.get_thread("subject")
        if api.kind == "chat"
        else service.store.get_session("subject")
    )
    assert (row.title, row.updated_at) == ("My project", 3.0)


@pytest.mark.asyncio
async def test_title_request_commits_to_the_owned_initial_conversation(api):
    response = await api.client.post(
        "/v1/session-titles",
        json=body(api),
        headers={"x-inqtrix-workspace-id": "workspace-one"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["current"] == {
        "title": "Digitaler Zwilling für Batteriespeicher",
        "title_revision": 1,
        "title_source": "generated",
    }
    assert api.quota.bookings == [(OWNER, "llm_tokens", 20)]
    assert 0 < api.llm.calls[0][1]["timeout"] <= 8


@pytest.mark.asyncio
async def test_renamed_conversation_is_skipped_before_any_model_cost(api):
    await api.save("My project", "manual")
    for revision in (0, 1):
        payload = body(api)
        payload["target"]["revision"] = revision
        response = await api.client.post(
            "/v1/session-titles",
            json=payload,
            headers={"x-inqtrix-workspace-id": "workspace-one"},
        )
        assert response.json()["reason"] == "title_changed"
    assert api.llm.calls == []


@pytest.mark.asyncio
async def test_generated_title_is_not_generated_again_at_its_revision(api):
    # A later question in the same conversation must not replace a title
    # the model already generated, nor pay for a second generation. The
    # store's commit guard answers 'title_changed' as well, so the model
    # call count and the quota bookings are what pin the check before the
    # model call; the different second title makes a replacement visible.
    headers = {"x-inqtrix-workspace-id": "workspace-one"}
    first = await api.client.post("/v1/session-titles", json=body(api), headers=headers)
    assert first.json()["current"]["title_source"] == "generated"
    api.llm.title = "Nur Deutschland"
    payload = body(api)
    payload["question"] = "Ja, nur Deutschland"
    payload["target"]["revision"] = first.json()["current"]["title_revision"]
    second = await api.client.post("/v1/session-titles", json=payload, headers=headers)
    data = second.json()
    assert (data["title"], data["reason"], data["current"]["title"]) == (
        None,
        "title_changed",
        "Digitaler Zwilling für Batteriespeicher",
    )
    assert len(api.llm.calls) == 1
    assert api.quota.bookings == [(OWNER, "llm_tokens", 20)]


@pytest.mark.asyncio
async def test_manual_rename_during_model_call_wins_at_commit(api):
    api.llm.after_call = lambda: asyncio.run(api.save("My project", "manual"))
    response = await api.client.post(
        "/v1/session-titles",
        json=body(api),
        headers={"x-inqtrix-workspace-id": "workspace-one"},
    )
    data = response.json()
    assert (data["title"], data["reason"], data["current"]["title"]) == (
        None,
        "title_changed",
        "My project",
    )
    assert len(api.llm.calls) == 1
    assert api.quota.bookings == [(OWNER, "llm_tokens", 20)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"x-test-user": "202", "x-inqtrix-workspace-id": "workspace-one"},
        {"x-inqtrix-workspace-id": "workspace-two"},
    ],
)
async def test_foreign_owner_or_workspace_is_denied_before_model_call(api, headers):
    response = await api.client.post(
        "/v1/session-titles", json=body(api), headers=headers
    )
    assert response.status_code == 404
    assert api.llm.calls == []


@pytest.mark.asyncio
async def test_disabled_or_missing_fast_model_does_not_use_the_main_model(api):
    api.container.settings.server.session_titles_enabled = False
    response = await api.client.post(
        "/v1/session-titles",
        json=body(api),
        headers={"x-inqtrix-workspace-id": "workspace-one"},
    )
    assert response.json()["reason"] == "disabled"
    api.container.settings.server.session_titles_enabled = True
    api.llm.models.tier_fast_model = ""
    response = await api.client.post(
        "/v1/session-titles",
        json=body(api),
        headers={"x-inqtrix-workspace-id": "workspace-one"},
    )
    assert response.json()["reason"] == "no_fast_model"
    assert api.llm.calls == []


@pytest.mark.asyncio
async def test_http_timeout_keeps_usage_booking_and_does_not_publish_late_result(
    api, monkeypatch
):
    started, release, ended = threading.Event(), threading.Event(), threading.Event()
    original = api.llm._call

    def blocked(prompt, kwargs):
        started.set()
        release.wait(timeout=2)
        original(prompt, kwargs)
        ended.set()

    api.llm._call = blocked
    timeout_after_provider_starts(monkeypatch, started)
    try:
        response = await api.client.post(
            "/v1/session-titles",
            json=body(api),
            headers={"x-inqtrix-workspace-id": "workspace-one"},
        )
        assert started.is_set()
        assert response.json()["reason"] == "timeout"
        assert api.quota.bookings == []
    finally:
        release.set()
        await asyncio.to_thread(ended.wait, 2)
    for _ in range(50):
        if api.quota.bookings:
            break
        await asyncio.sleep(0.01)
    assert api.quota.bookings == [(OWNER, "llm_tokens", 20)]
    service = (
        api.container.chat_history_service
        if api.kind == "chat"
        else api.container.agent_sessions_service
    )
    row = await (
        service.store.get_thread("subject")
        if api.kind == "chat"
        else service.store.get_session("subject")
    )
    assert (row.title, row.title_revision) == ("Initial", 0)


@pytest.mark.asyncio
async def test_timed_out_waiters_do_not_release_busy_provider_capacity(
    api, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    original = api.llm._call

    def blocked(prompt, kwargs):
        started.set()
        release.wait(timeout=5)
        original(prompt, kwargs)

    api.llm._call = blocked
    timeout_after_provider_starts(monkeypatch, started)
    try:
        for _ in range(2):
            started.clear()
            response = await api.client.post(
                "/v1/session-titles",
                json=body(api),
                headers={"x-inqtrix-workspace-id": "workspace-one"},
            )
            assert response.json()["reason"] == "timeout"
        refused = await api.client.post(
            "/v1/session-titles",
            json=body(api),
            headers={"x-inqtrix-workspace-id": "workspace-one"},
        )
        assert refused.json()["reason"] == "rate_limited"
    finally:
        release.set()
        for _ in range(100):
            if len(api.quota.bookings) == 2:
                break
            await asyncio.sleep(0.01)
    assert len(api.quota.bookings) == 2
