"""The operator list of run bindings, before raising a contract and on the way back.

Whoever raises a workflow contract reads which open runs are bound below the
new version; whoever rolls a deploy back reads which are bound above the old
one, because such a run stops every worker that claims it, and cancels those.
Only an instance admin reads the list or cancels.
"""

from __future__ import annotations

import asyncio
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.api_key import build_local_provider
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.admin import build_admin_router
from inqtrix.server.routers.admin_system import build_router
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.settings import AuthSettings, ServerSettings, Settings, StorageSettings
from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM

OWNER = ("owner@example.com", "correct-horse-battery")
ROUTE = "/v1/admin/system/run-bindings"
CANCEL = "/v1/admin/system/run-bindings/cancel"


def _world():
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            oidc_insecure_dev_cookies=True,
            pat_pepper="p" * 32,
            session_secret="s" * 32,
        ),
        server=ServerSettings(public_base_url="", RUN_MAX_CONCURRENT=1),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    provider = build_local_provider(settings)
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=provider,
    )
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    app.include_router(build_admin_router(provider))
    app.include_router(build_router(container))
    return TestClient(app, base_url="http://127.0.0.1:5100"), container


def _as_owner(client: TestClient) -> str:
    client.post(
        "/api/setup/owner",
        json={"display_name": "Owner", "email": OWNER[0], "password": OWNER[1]},
    )
    return client.get("/api/auth/session").json()["csrf_token"]


def test_only_an_instance_admin_reads_the_run_bindings() -> None:
    client, _container = _world()
    assert client.get(ROUTE).status_code == 401

    csrf = _as_owner(client)
    assert client.get(ROUTE).status_code == 200
    created = client.post(
        "/v1/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
    )
    assert created.status_code == 201
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    client.post(
        "/api/auth/login/local",
        json={"identifier": "bob@example.com", "password": "another-strong-pw-1"},
    )

    assert client.get(ROUTE).status_code == 404


def test_the_list_names_the_contracts_and_the_runs_outside_a_version() -> None:
    client, container = _world()
    _as_owner(client)
    started, release = threading.Event(), threading.Event()

    def blocking(handle) -> None:
        started.set()
        release.wait(timeout=5)
        handle.complete({"answer": "done"})

    store = container.run_service.run_store
    running = store.submit(
        question="q", stack_name="default", work=blocking,
        mode="research", workflow_contract=1,
    )["run_id"]
    assert started.wait(timeout=5)
    waiting = store.submit(
        question="q", stack_name="default", work=lambda handle: handle.complete({}),
        mode="research", workflow_contract=2,
    )["run_id"]
    try:
        below = client.get(ROUTE, params={"mode": "research", "below": 2})
        above = client.get(ROUTE, params={"above": 1})
    finally:
        release.set()

    assert below.status_code == 200
    body = below.json()
    assert body["contracts"] == container.registry.workflow_contracts()
    assert body["counts"] == [
        {"mode": "research", "status": "queued", "workflow_contract": 2, "runs": 1},
        {"mode": "research", "status": "running", "workflow_contract": 1, "runs": 1},
    ]
    assert [run["run_id"] for run in body["runs"]] == [running]
    assert (body["runs_total"], body["truncated"]) == (1, False)
    assert [run["run_id"] for run in above.json()["runs"]] == [waiting]


def test_a_bound_that_is_no_contract_version_is_refused() -> None:
    client, _container = _world()
    _as_owner(client)

    for params in ({"below": "0"}, {"above": "zwei"}, {"below": "-1"}, {"above": "2147483648"}):
        response = client.get(ROUTE, params=params)
        assert response.status_code == 400, params
        assert response.json()["error"]["type"] == "invalid_request_error"


def _events(store, run_id: str) -> list[tuple[str, dict]]:
    return [(event["type"], event["data"]) for event in store.events_snapshot(run_id)]


def test_only_an_instance_admin_cancels_with_the_token_of_the_session() -> None:
    client, _container = _world()
    assert client.post(CANCEL, json={"mode": "research"}).status_code == 401

    csrf = _as_owner(client)
    assert client.post(CANCEL, json={"mode": "research"}).status_code == 403
    client.post(
        "/v1/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
    )
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    client.post(
        "/api/auth/login/local",
        json={"identifier": "bob@example.com", "password": "another-strong-pw-1"},
    )
    bob_csrf = client.get("/api/auth/session").json()["csrf_token"]

    assert (
        client.post(
            CANCEL, headers={"X-CSRF-Token": bob_csrf}, json={"mode": "research"}
        ).status_code
        == 404
    )


def test_the_cancel_ends_only_the_runs_this_server_cannot_run() -> None:
    client, container = _world()
    csrf = _as_owner(client)
    contract = container.registry.workflow_contracts()["research"]
    started, release = threading.Event(), threading.Event()

    def blocking(handle) -> None:
        started.set()
        release.wait(timeout=5)
        handle.complete({"answer": "done"})

    store = container.run_service.run_store
    running = store.submit(
        question="q", stack_name="default", work=blocking,
        mode="research", workflow_contract=contract + 1,
    )["run_id"]
    assert started.wait(timeout=5)
    queued_above = store.submit(
        question="q", stack_name="default", work=lambda handle: handle.complete({}),
        mode="research", workflow_contract=contract + 1,
    )["run_id"]
    queued_current = store.submit(
        question="q", stack_name="default", work=lambda handle: handle.complete({}),
        mode="research", workflow_contract=contract,
    )["run_id"]
    unregistered = store.submit(
        question="q", stack_name="default", work=lambda handle: handle.complete({}),
        mode="retired_probe", workflow_contract=1,
    )["run_id"]
    try:
        response = client.post(
            CANCEL, headers={"X-CSRF-Token": csrf}, json={"mode": "research"}
        )
        current_status = store.get(queued_current)["status"]
        unregistered_status = store.get(unregistered)["status"]
        other_mode = client.post(
            CANCEL, headers={"X-CSRF-Token": csrf}, json={"mode": "retired_probe"}
        )
    finally:
        release.set()

    assert response.status_code == 200
    assert response.json() == {
        "mode": "research",
        "server_contract": contract,
        "cancelled": 1,
        "cancel_requested": 1,
    }
    assert _events(store, queued_above)[-1] == (
        "inqtrix.run.cancelled",
        {"status": "cancelled", "reason": "cancelled_by_admin"},
    )
    assert (
        "inqtrix.run.cancel_requested",
        {"status": "running", "reason": "cancelled_by_admin"},
    ) in _events(store, running)
    assert (current_status, unregistered_status) == ("queued", "queued")
    assert other_mode.json() == {
        "mode": "retired_probe",
        "server_contract": None,
        "cancelled": 1,
        "cancel_requested": 0,
    }


def test_a_cancel_that_names_no_mode_is_refused() -> None:
    client, _container = _world()
    csrf = _as_owner(client)

    for body in ({}, {"mode": ""}, {"mode": "Agent Kernel"}, {"mode": 1}, {"mode": "a" * 65}):
        response = client.post(CANCEL, headers={"X-CSRF-Token": csrf}, json=body)
        assert response.status_code == 400, body
        assert response.json()["error"]["type"] == "invalid_request_error"
    raw = client.post(
        CANCEL,
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=b"not json",
    )
    assert raw.status_code == 400
