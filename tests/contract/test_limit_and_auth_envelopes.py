"""Contract tests for the 429 limit envelopes and the 401 wire shape.

Closes the review-identified safety-net gaps: the semaphore-busy and
queue-full envelopes (five hand-moved sites in the router split) and
the 401-before-400 precedence plus the exact FastAPI ``detail``-wrapped
401 body the React client and SDKs observe.
"""

from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

import inqtrix.research.web_research as web_research_module
from inqtrix.providers.base import ProviderContext
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import ServerSettings, Settings

from tests.contract._app import (
    StubLLM,
    StubSearch,
    make_contract_client,
    minimal_agent_result,
    wait_for_run_status,
)


# ------------------------------------------------------------------ #
# 429 — semaphore busy (chat) and run queue full
# ------------------------------------------------------------------ #


class _LockedSemaphore:
    """Semaphore stand-in that always reports busy.

    The routers consult ``sem.locked()`` BEFORE acquiring; this pins
    that ordering and the exact envelope without real concurrency.
    """

    def locked(self) -> bool:
        return True


def test_chat_returns_429_envelope_when_semaphore_busy():
    app = FastAPI()
    router = create_router()
    register_routes(
        router,
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(),
        semaphore_factory=lambda: _LockedSemaphore(),
    )
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": False,
            },
        )

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "message": "Too many concurrent requests. Please wait.",
            "type": "rate_limit_error",
        }
    }


def test_runs_return_429_envelope_when_queue_full(monkeypatch):
    release = threading.Event()

    def blocking_run(*args, **kwargs):
        release.wait(timeout=5)
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "run_web_graph", blocking_run)

    with make_contract_client(
        server_settings=ServerSettings(
            run_max_concurrent=1, run_queue_max_size=0
        ),
    ) as client:
        first = client.post("/v1/runs", json={"question": "blockiert"})
        assert first.status_code == 202
        wait_for_run_status(client, first.json()["run_id"], "running")

        overflow = client.post("/v1/runs", json={"question": "zu viel"})
        release.set()
        wait_for_run_status(client, first.json()["run_id"], "completed")

    assert overflow.status_code == 429
    assert overflow.json() == {
        "error": {
            "message": "Too many queued research jobs. Please wait.",
            "type": "rate_limit_error",
            # Additive discriminator: the global queue is full, as opposed
            # to the per-user fairness cap (reason=per_user_limit).
            "reason": "queue_full",
        }
    }


# ------------------------------------------------------------------ #
# 401 — wire shape and precedence over body validation
# ------------------------------------------------------------------ #


def _gated_client():
    return make_contract_client(
        server_settings=ServerSettings(api_key="secret-token-123"),
    )


def test_unauthenticated_invalid_json_returns_401_before_400():
    """Auth runs before the body is read: 401 wins over the JSON 400."""
    with _gated_client() as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "detail": {
            "error": {
                "message": "Missing or malformed Authorization header",
                "type": "unauthorized",
            }
        }
    }


def test_wrong_key_401_body_is_byte_stable():
    with _gated_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}]},
            headers={"Authorization": "Bearer wrong-token"},
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "error": {
                "message": "Invalid API key",
                "type": "unauthorized",
            }
        }
    }


def test_authenticated_invalid_json_returns_400_envelope():
    with _gated_client() as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer secret-token-123",
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Invalid JSON body",
            "type": "invalid_request_error",
        }
    }
