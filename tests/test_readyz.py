"""/readyz readiness contract (2.3).

Liveness (``/health``) is provider-only; readiness keys traffic on the
STATEFUL dependencies: database or queue down => 503 (drain the pod),
vector store down => degraded but ready (knowledge fails loudly
per-request, everything else serves). Memory backends are trivially
ready so the zero-infrastructure default stays green.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.services.system_runtime import readiness_payload
from inqtrix.settings import Settings


def _container(
    *,
    storage_backend: str = "memory",
    queue_backend: str = "memory",
    session_factory=None,
    knowledge_service=None,
    file_service=None,
) -> SimpleNamespace:
    settings = Settings()
    settings.storage.backend = storage_backend  # type: ignore[assignment]
    settings.queue.backend = queue_backend  # type: ignore[assignment]
    return SimpleNamespace(
        settings=settings,
        session_factory=session_factory,
        knowledge_service=knowledge_service,
        file_service=file_service,
    )


@pytest.mark.asyncio
async def test_memory_backends_are_trivially_ready() -> None:
    status_code, payload = await readiness_payload(_container())
    assert status_code == 200
    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "database": "skipped",
        "queue": "skipped",
        "vector_store": "skipped",
        "object_store": "skipped",
    }


@pytest.mark.asyncio
async def test_postgres_without_reachable_database_is_not_ready() -> None:
    class _FailingSession:
        async def __aenter__(self):
            raise ConnectionError("db down")

        async def __aexit__(self, *exc):
            return False

    status_code, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=_FailingSession)
    )
    assert status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["database"] == "unavailable"


@pytest.mark.asyncio
async def test_postgres_declared_without_factory_is_a_visible_miswire() -> None:
    status_code, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=None)
    )
    assert status_code == 503
    assert payload["checks"]["database"] == "unavailable"


@pytest.mark.asyncio
async def test_postgres_readiness_uses_schema_and_role_contract(monkeypatch) -> None:
    observed: dict[str, object] = {}

    async def verify(
        session_factory,
        *,
        app_role: str,
        login_policy: str,
    ) -> None:
        observed["session_factory"] = session_factory
        observed["app_role"] = app_role
        observed["login_policy"] = login_policy

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        verify,
    )
    factory = object()
    status_code, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=factory)
    )

    assert status_code == 200
    assert payload["checks"]["database"] == "ok"
    assert observed == {
        "session_factory": factory,
        "app_role": "inqtrix_app",
        "login_policy": "restricted",
    }


@pytest.mark.asyncio
async def test_dead_vector_store_degrades_but_stays_ready() -> None:
    knowledge = SimpleNamespace(
        knowledge=SimpleNamespace(
            store=SimpleNamespace(is_available=lambda: False)
        )
    )
    status_code, payload = await readiness_payload(
        _container(knowledge_service=knowledge)
    )
    assert status_code == 200
    assert payload["status"] == "degraded"
    assert payload["checks"]["vector_store"] == "unavailable"


@pytest.mark.asyncio
async def test_dead_object_store_degrades_but_stays_ready() -> None:
    class _UnavailableFiles:
        async def object_store_available(self) -> bool:
            return False

    status_code, payload = await readiness_payload(
        _container(file_service=_UnavailableFiles())
    )

    assert status_code == 200
    assert payload["status"] == "degraded"
    assert payload["checks"]["object_store"] == "unavailable"


@pytest.mark.asyncio
async def test_valkey_queue_down_is_not_ready(monkeypatch) -> None:
    """A dead dispatch queue must take the pod out of rotation (503)."""
    import inqtrix.services.system_runtime as runtime_module

    monkeypatch.setattr(
        runtime_module, "_ping_valkey", lambda _url: False
    )
    status_code, payload = await readiness_payload(
        _container(queue_backend="valkey")
    )
    assert status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["queue"] == "unavailable"


@pytest.mark.asyncio
async def test_valkey_queue_up_is_ready(monkeypatch) -> None:
    import inqtrix.services.system_runtime as runtime_module

    monkeypatch.setattr(
        runtime_module, "_ping_valkey", lambda _url: True
    )
    status_code, payload = await readiness_payload(
        _container(queue_backend="valkey")
    )
    assert status_code == 200
    assert payload["checks"]["queue"] == "ok"


@pytest.mark.asyncio
async def test_payload_distinguishes_violation_from_unreachability(
    monkeypatch,
) -> None:
    """The bool once conflated both; the product gate needs the split.

    A probe that cannot REACH the database proves nothing about the
    schema/role contract — a probe that connected and found a wrong
    contract is a confirmed break. checks.database keeps its coarse
    orchestrator labels; database_contract carries the verdict.
    """
    from inqtrix.storage.runtime_contract import (
        DatabaseRuntimeContractError,
        DatabaseRuntimeUnavailableError,
    )

    async def _violates(session_factory, *, app_role, login_policy):
        raise DatabaseRuntimeContractError("Schema-Kopf falsch")

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        _violates,
    )
    _, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=object())
    )
    assert payload["checks"]["database"] == "unavailable"
    assert payload["database_contract"] == "violation"

    async def _unreachable(session_factory, *, app_role, login_policy):
        raise DatabaseRuntimeUnavailableError("Pool erschoepft")

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        _unreachable,
    )
    _, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=object())
    )
    assert payload["checks"]["database"] == "unavailable"
    assert payload["database_contract"] == "unavailable"

    async def _ok(session_factory, *, app_role, login_policy):
        return None

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        _ok,
    )
    _, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=object())
    )
    assert payload["database_contract"] == "ok"


def _gate_app(monkeypatch, *, database_contract: str, gate_open: bool):
    """A minimal app with the real readyz route and the real gate state."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from inqtrix.server.routers import health as health_module

    async def _payload(container):
        ready = database_contract in ("ok", "skipped")
        return (200 if ready else 503), {
            "status": "ready" if ready else "not_ready",
            "checks": {
                "database": "ok" if ready else "unavailable",
                "queue": "skipped",
                "vector_store": "skipped",
                "object_store": "skipped",
            },
            "database_contract": database_contract,
        }

    monkeypatch.setattr(
        "inqtrix.services.system_runtime.readiness_payload", _payload
    )
    container = _container()
    container.settings.sharing.restrict_to_workspace_members = False
    container.health_service = SimpleNamespace(
        health_payload=lambda: (200, {"status": "ok"})
    )
    app = FastAPI()
    app.state.database_contract_ready = gate_open
    app.state.workspace_share_reconciliation_ready = True
    app.include_router(health_module.build_router(container))
    return app, TestClient(app)


def test_gate_keeps_its_state_when_the_probe_cannot_reach_the_database(
    monkeypatch,
) -> None:
    """The metastability guard: one slow probe must not close a healthy gate.

    Otherwise a single 2s probe timeout under a request burst closes the
    gate for a full healthcheck interval — most stream connects get 503
    while every run underneath is healthy.
    """
    app, client = _gate_app(
        monkeypatch, database_contract="unavailable", gate_open=True
    )
    response = client.get("/readyz")
    assert response.status_code == 503, "orchestrator still drains the pod"
    assert app.state.database_contract_ready is True, (
        "unreachability must not close the product gate"
    )
    # And the closed gate stays closed — kept state, not forced open.
    app2, client2 = _gate_app(
        monkeypatch, database_contract="unavailable", gate_open=False
    )
    client2.get("/readyz")
    assert app2.state.database_contract_ready is False


def test_gate_closes_on_a_confirmed_contract_violation(monkeypatch) -> None:
    app, client = _gate_app(
        monkeypatch, database_contract="violation", gate_open=True
    )
    response = client.get("/readyz")
    assert response.status_code == 503
    assert app.state.database_contract_ready is False, (
        "a confirmed schema/role break must close the gate"
    )


def test_gate_reopens_when_the_contract_verifies_again(monkeypatch) -> None:
    app, client = _gate_app(
        monkeypatch, database_contract="ok", gate_open=False
    )
    response = client.get("/readyz")
    assert response.status_code == 200
    assert app.state.database_contract_ready is True


@pytest.mark.asyncio
async def test_probe_timeout_reads_unavailable_not_violation(
    monkeypatch,
) -> None:
    """The named regression, driven through the REAL wait_for expiry.

    A probe that times out has learned nothing: under a load spike the
    database is slow, not misconfigured. Such a reading must classify as
    unreachability, never as a contract break. Latching it into a
    permanent verdict takes the whole instance out of service for a
    condition that resolves on its own.
    """
    import asyncio

    monkeypatch.setattr(
        "inqtrix.services.system_runtime._RUNTIME_PROBE_TIMEOUT_SECONDS",
        0.05,
    )

    async def _slow(session_factory, *, app_role, login_policy):
        await asyncio.sleep(0.5)

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        _slow,
    )
    _, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=object())
    )
    assert payload["database_contract"] == "unavailable"
    assert payload["checks"]["database"] == "unavailable"


@pytest.mark.asyncio
async def test_unclassifiable_probe_failure_latches_as_violation(
    monkeypatch,
) -> None:
    """Integrity over availability when the probe cannot say why it died."""

    async def _dies(session_factory, *, app_role, login_policy):
        raise RuntimeError("verifier defect, not a reachability failure")

    monkeypatch.setattr(
        "inqtrix.storage.runtime_contract.verify_database_runtime_contract",
        _dies,
    )
    _, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=object())
    )
    assert payload["database_contract"] == "violation"


@pytest.mark.asyncio
async def test_missing_session_factory_is_a_violation_not_a_transient() -> None:
    """A composition bug is permanent for this process — it must latch."""
    _, payload = await readiness_payload(
        _container(storage_backend="postgres", session_factory=None)
    )
    assert payload["database_contract"] == "violation"
    assert payload["checks"]["database"] == "unavailable"


def test_gate_fails_closed_after_sustained_unreachability(monkeypatch) -> None:
    """Keep-state is BOUNDED: only Kubernetes drains an unready pod, so a
    wrong-schema database hiding behind persistent probe timeouts must
    not hold the gate open forever."""
    import time as time_module

    app, client = _gate_app(
        monkeypatch, database_contract="unavailable", gate_open=True
    )
    app.state.database_contract_unavailable_since = (
        time_module.monotonic() - 121.0
    )
    client.get("/readyz")
    assert app.state.database_contract_ready is False, (
        "sustained unreachability past the bound must fail closed"
    )
    # Within the bound the gate still holds (the metastability fix).
    app2, client2 = _gate_app(
        monkeypatch, database_contract="unavailable", gate_open=True
    )
    app2.state.database_contract_unavailable_since = (
        time_module.monotonic() - 30.0
    )
    client2.get("/readyz")
    assert app2.state.database_contract_ready is True


def test_recovered_probe_resets_the_unavailability_clock(monkeypatch) -> None:
    """ok clears the streak: two separate blips never sum past the bound."""
    import time as time_module

    app, client = _gate_app(
        monkeypatch, database_contract="ok", gate_open=True
    )
    app.state.database_contract_unavailable_since = (
        time_module.monotonic() - 121.0
    )
    client.get("/readyz")
    assert app.state.database_contract_unavailable_since is None
    assert app.state.database_contract_ready is True
