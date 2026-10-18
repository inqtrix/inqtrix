"""Quota admin surface: gated on the instance-admin axis, not ownership.

Pins the instance-admin gate end to end. Quota administration is tenant-wide
platform administration, so ``/v1/admin/quota*`` is gated on
``instance_role == "admin"`` (the users-mirror single source of truth) via
the shared :func:`inqtrix.server.routers._admin_guard.require_instance_admin`
guard — never on workspace ownership. Two halves:

* Unit tests of the shared guard cover every branch (pat/anonymous,
  missing/expired session, non-admin, disabled admin, admin) without HTTP.
* HTTP tests drive the real cookie-session login (``run_login``), promote
  the session to instance admin, and exercise the four admin endpoints,
  plus the negative cases that pin the fix: an instance admin with NO
  workspace sees quotas (the reported UI symptom), a workspace OWNER who
  is not an instance admin is denied (the escalation), and a demoted or
  disabled admin loses access immediately (the demotion-retention bug).

The metered subjects an admin administers are seeded directly through the
quota service (``record_for_subject`` / the admin mutations) rather than by
driving a second user's runs — the enforcement of *consuming* usage is
covered by ``test_quota_enforcement.py``; here only the *administration* of
it is under test.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from inqtrix.auth.directory import MirroredUser
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService, WorkspaceRole
from inqtrix.auth.principal import ANONYMOUS_PRINCIPAL, Principal
from inqtrix.providers.base import ProviderContext
from inqtrix.quota.models import QuotaDimension, QuotaSubject
from inqtrix.server.container import build_container
from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.server.routers.quota import build_router as build_quota_router
from inqtrix.settings import (
    QuotaSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM
from tests.test_oidc_bff import ISSUER, FakeIdp, make_provider, run_login

ADMIN_SUB = "user-1234"
"""The subject ``run_login`` establishes via FakeIdp's default identity."""

OTHER_SUB = "subject-b"
"""A second metered subject the admin administers (never a session)."""

ADMIN_USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


# --------------------------------------------------------------------------- #
# Unit: the shared instance-admin guard (every branch, no HTTP)
# --------------------------------------------------------------------------- #


class _StubSessions:
    def __init__(self, session) -> None:
        self._session = session

    async def get(self, _session_id: str):
        return self._session


class _StubUsers:
    def __init__(self, mirror) -> None:
        self._mirror = mirror

    async def find_by_user_id(self, *, tenant_id, user_id):
        if self._mirror is not None and self._mirror.user_id == user_id:
            return self._mirror
        return None


class _StubProvider:
    """Minimal provider double for guard unit tests (no real transport)."""

    def __init__(self, principal, *, session=None, mirror=None) -> None:
        self._principal = principal
        self.sessions = _StubSessions(session)
        self.users = _StubUsers(mirror)

    def build_principal_dependency(self):
        async def _dependency(_request: Request) -> Principal:
            return self._principal

        return _dependency


class _StubSession:
    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id


def _mirror(role: str = "admin", *, disabled: bool = False) -> MirroredUser:
    return MirroredUser(
        user_id=ADMIN_USER_ID,
        issuer=ISSUER,
        subject=ADMIN_SUB,
        email="a@example.com",
        email_verified=True,
        display_name="Ada",
        disabled_at=time.time() if disabled else None,
        instance_role=role,
    )


def _dummy_request() -> Request:
    return Request({"type": "http", "method": "GET", "headers": []})


def _resolve(provider) -> tuple:
    return asyncio.run(require_instance_admin(provider, _dummy_request()))


def test_guard_admits_an_instance_admin():
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="oidc_session", session_id="sid"),
        session=_StubSession(ADMIN_USER_ID),
        mirror=_mirror("admin"),
    )
    resolved, error = _resolve(provider)
    assert error is None
    principal, _session, mirror = resolved
    assert principal.user_id == ADMIN_USER_ID
    assert mirror.instance_role == "admin"


def test_guard_rejects_pat_principal():
    """A personal access token can never administer (leaked-token safety)."""
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="pat", pat_id="p1"),
        mirror=_mirror("admin"),
    )
    resolved, error = _resolve(provider)
    assert resolved is None
    assert error.status_code == 404


def test_guard_rejects_anonymous_principal():
    provider = _StubProvider(ANONYMOUS_PRINCIPAL, mirror=_mirror("admin"))
    resolved, error = _resolve(provider)
    assert resolved is None
    assert error.status_code == 404


def test_guard_rejects_session_without_session_id():
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="oidc_session", session_id=None),
        mirror=_mirror("admin"),
    )
    _resolved, error = _resolve(provider)
    assert error.status_code == 404


def test_guard_rejects_expired_session_with_401():
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="oidc_session", session_id="sid"),
        session=None,  # sessions.get -> None: the session is gone
        mirror=_mirror("admin"),
    )
    _resolved, error = _resolve(provider)
    assert error.status_code == 401


def test_guard_rejects_unmirrored_user():
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="oidc_session", session_id="sid"),
        session=_StubSession(ADMIN_USER_ID),
        mirror=None,
    )
    _resolved, error = _resolve(provider)
    assert error.status_code == 404


def test_guard_rejects_non_admin_role():
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="oidc_session", session_id="sid"),
        session=_StubSession(ADMIN_USER_ID),
        mirror=_mirror("user"),
    )
    _resolved, error = _resolve(provider)
    assert error.status_code == 404


def test_guard_rejects_disabled_admin():
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="oidc_session", session_id="sid"),
        session=_StubSession(ADMIN_USER_ID),
        mirror=_mirror("admin", disabled=True),
    )
    _resolved, error = _resolve(provider)
    assert error.status_code == 404


def test_guard_logs_authenticated_non_admin_denial(caplog):
    """An authenticated non-admin denial is operator-visible, never silent.

    The pre-auth branches stay quiet; only an authenticated session that is not
    an active admin produces the loud ``authz``-style warning, restoring the
    visibility the old workspace-OWNER quota gate had via ``AuthorizationService._deny``.
    """
    provider = _StubProvider(
        Principal(user_id=ADMIN_USER_ID, kind="oidc_session", session_id="sid"),
        session=_StubSession(ADMIN_USER_ID),
        mirror=_mirror("user"),
    )
    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            _resolved, error = _resolve(provider)
    finally:
        logger.removeHandler(caplog.handler)
    assert error.status_code == 404
    messages = [
        record.getMessage()
        for record in caplog.records
        if "authz denied" in record.getMessage()
    ]
    assert any(
        "action=instance_admin" in message and "actor_ref=usr_" in message
        for message in messages
    )
    assert all(str(ADMIN_USER_ID) not in message for message in messages)
    assert all("reason=" not in message for message in messages)


# --------------------------------------------------------------------------- #
# HTTP: the admin surface over a real cookie session
# --------------------------------------------------------------------------- #


def make_world(quota: QuotaSettings):
    """An oidc app with quotas on; the auth + quota routers mounted.

    Returns ``(client, container, idp, identity, users)``. ``identity`` is
    the membership/audit store; ``users`` is the JIT mirror the login
    populates and the guard reads ``instance_role`` from.
    """
    identity = MemoryIdentityStore()
    idp = FakeIdp()
    provider, users = make_provider(idp)
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
            quota=quota,
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=provider,
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
    )
    assert container.quota_service is not None
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    app.include_router(build_quota_router(container))
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    return client, container, idp, identity, users


def login(client, idp) -> str:
    """Log in the default identity; return its CSRF token (no promotion)."""
    run_login(client, idp)
    return client.get("/api/auth/session").json()["csrf_token"]


def user_id_for(users, subject: str = ADMIN_SUB) -> uuid.UUID:
    mirrored = asyncio.run(
        users.find_user(
            tenant_id="default", issuer=ISSUER, subject=subject
        )
    )
    assert mirrored is not None
    return mirrored.user_id


def promote(users, subject: str = ADMIN_SUB) -> None:
    asyncio.run(
        users.set_instance_role(
            tenant_id="default", user_id=user_id_for(users, subject), role="admin"
        )
    )


def demote(users, subject: str = ADMIN_SUB) -> None:
    """Force a session to the non-admin role.

    The first login bootstraps the instance owner (auto-promoted to admin),
    so a genuine non-admin session must be demoted explicitly. ``set_instance_role``
    is used directly (not the last-admin-guarded path) to model the resulting
    mirror state.
    """
    asyncio.run(
        users.set_instance_role(
            tenant_id="default", user_id=user_id_for(users, subject), role="user"
        )
    )


def seed_usage(
    container, user_id: uuid.UUID, dimension: QuotaDimension, amount: int
) -> None:
    asyncio.run(
        container.quota_service.record_for_subject(
            QuotaSubject("default", user_id), dimension, amount
        )
    )


def usage_limit(container, user_id: uuid.UUID, dimension: QuotaDimension):
    rows = asyncio.run(
        container.quota_service.usage_for(QuotaSubject("default", user_id))
    )
    return next(r for r in rows if r.dimension == dimension).limit


def test_instance_admin_without_workspace_sees_quotas(tmp_path):
    """The reported symptom: an admin owning NO workspace can administer."""
    client, container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True, runs_max=500)
    )
    with client:
        login(client, idp)
        promote(users)
        # The admin holds zero workspace memberships — availability tracks
        # the instance role, not ownership.
        ok = client.get("/v1/admin/quota")
        assert ok.status_code == 200
        body = ok.json()
        assert "runs" in body["dimensions"]
        assert body["ceilings"]["runs"] == 500
        assert body["subjects"] == []


def test_non_admin_user_is_denied(tmp_path):
    client, _container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        csrf = login(client, idp)
        demote(users)  # a genuine non-admin (first login auto-promotes)
        assert client.get("/v1/admin/quota").status_code == 404
        put = client.put(
            "/v1/admin/quota/limits",
            json={"user_id": str(OTHER_USER_ID), "dimension": "runs", "value": 1},
            headers={"X-CSRF-Token": csrf},
        )
        assert put.status_code == 404


def test_workspace_owner_without_admin_role_is_denied(tmp_path):
    """Ownership confers nothing tenant-wide — the escalation is closed."""
    client, _container, idp, identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        login(client, idp)
        demote(users)  # a non-admin who nonetheless owns a workspace
        # Make the logged-in user a workspace OWNER directly.
        workspace_id, _ = asyncio.run(
            identity.create_workspace(
                tenant_id="default",
                name="Team",
                created_by_user_id=user_id_for(users),
            )
        )
        role = asyncio.run(
            identity.role_in_workspace(
                tenant_id="default",
                user_id=user_id_for(users),
                workspace_id=workspace_id,
            )
        )
        assert role == WorkspaceRole.OWNER  # genuinely an OWNER...
        # ...yet quota admin is denied (the axis is instance_role).
        assert client.get("/v1/admin/quota").status_code == 404


def test_unauthenticated_request_is_401(tmp_path):
    client, _container, _idp, _identity, _users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        assert client.get("/v1/admin/quota").status_code == 401


def test_demoted_admin_loses_access(tmp_path):
    """The demotion-retention bug: clearing instance_role revokes at once."""
    client, _container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        login(client, idp)
        promote(users)
        assert client.get("/v1/admin/quota").status_code == 200
        # Demote (single admin -> set_instance_role directly, not the
        # last-admin-guarded path, to model the post-demotion mirror state).
        asyncio.run(
            users.set_instance_role(
                tenant_id="default",
                user_id=user_id_for(users),
                role="user",
            )
        )
        assert client.get("/v1/admin/quota").status_code == 404


def test_disabled_admin_loses_access(tmp_path):
    client, _container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        login(client, idp)
        promote(users)
        assert client.get("/v1/admin/quota").status_code == 200
        asyncio.run(
            users.set_disabled(
                tenant_id="default",
                user_id=user_id_for(users),
                disabled_at=time.time(),
            )
        )
        assert client.get("/v1/admin/quota").status_code == 401


def test_admin_set_and_clear_override(tmp_path):
    client, container, idp, identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        csrf = login(client, idp)
        promote(users)
        put = client.put(
            "/v1/admin/quota/limits",
            json={"user_id": str(OTHER_USER_ID), "dimension": "runs", "value": 3},
            headers={"X-CSRF-Token": csrf},
        )
        assert put.status_code == 200
        assert usage_limit(container, OTHER_USER_ID, QuotaDimension.RUNS) == 3

        last = identity.audit_entries[-1]
        assert last.action == "quota.override"
        assert last.resource_id == f"{OTHER_USER_ID}:runs"
        assert last.actor_user_id == user_id_for(users)
        assert last.detail == {"value": "3"}

        overview = client.get("/v1/admin/quota").json()
        subj = next(
            s for s in overview["subjects"]
            if s["user_id"] == str(OTHER_USER_ID)
        )
        assert subj["dimensions"]["runs"]["override"] == 3

        cleared = client.delete(
            "/v1/admin/quota/limits",
            params={"user_id": str(OTHER_USER_ID), "dimension": "runs"},
            headers={"X-CSRF-Token": csrf},
        )
        assert cleared.status_code == 204
        assert identity.audit_entries[-1].action == "quota.override_cleared"
        assert usage_limit(container, OTHER_USER_ID, QuotaDimension.RUNS) is None


def test_admin_default_for_all_and_ceiling_clamp(tmp_path):
    client, container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True, runs_max=10)
    )
    with client:
        csrf = login(client, idp)
        promote(users)
        put = client.put(
            "/v1/admin/quota/limits",
            json={
                "user_id": "default",
                "dimension": "runs",
                "value": 50,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert put.status_code == 200
        # The effective limit is clamped to the operator ceiling (10)...
        assert usage_limit(container, OTHER_USER_ID, QuotaDimension.RUNS) == 10
        # ...while the raw tenant default (pre-clamp) is surfaced.
        overview = client.get("/v1/admin/quota").json()
        assert overview["tenant_default"]["runs"] == 50

        cleared = client.delete(
            "/v1/admin/quota/limits",
            params={"user_id": "default", "dimension": "runs"},
            headers={"X-CSRF-Token": csrf},
        )
        assert cleared.status_code == 204
        assert usage_limit(container, OTHER_USER_ID, QuotaDimension.RUNS) == 10


def test_admin_reset_zeroes_flow_usage(tmp_path):
    client, container, idp, identity, users = make_world(
        QuotaSettings(enabled=True, runs_default=1)
    )
    with client:
        csrf = login(client, idp)
        promote(users)
        seed_usage(container, OTHER_USER_ID, QuotaDimension.RUNS, 1)
        reset = client.post(
            "/v1/admin/quota/reset",
            json={"user_id": str(OTHER_USER_ID), "dimension": "runs"},
            headers={"X-CSRF-Token": csrf},
        )
        assert reset.status_code == 200
        used = next(
            r.used
            for r in asyncio.run(
                container.quota_service.usage_for(
                    QuotaSubject("default", OTHER_USER_ID)
                )
            )
            if r.dimension == QuotaDimension.RUNS
        )
        assert used == 0
        last = identity.audit_entries[-1]
        assert last.action == "quota.reset"
        assert last.resource_id == f"{OTHER_USER_ID}:runs"


def test_admin_reset_rejects_stock_dimension(tmp_path):
    client, _container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        csrf = login(client, idp)
        promote(users)
        resp = client.post(
            "/v1/admin/quota/reset",
            json={"user_id": str(OTHER_USER_ID), "dimension": "stored_bytes"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400


def test_admin_invalid_dimension_is_400(tmp_path):
    client, _container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        csrf = login(client, idp)
        promote(users)
        resp = client.put(
            "/v1/admin/quota/limits",
            json={"user_id": str(OTHER_USER_ID), "dimension": "bogus", "value": 1},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400


def test_admin_overview_enriches_metered_subject(tmp_path):
    client, container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True, runs_default=5)
    )
    with client:
        login(client, idp)
        promote(users)
        # A second user, mirrored for enrichment + metered for usage.
        asyncio.run(
            users.record_login(
                tenant_id="default",
                issuer=ISSUER,
                subject=OTHER_SUB,
                email="b@example.com",
                email_verified=True,
                display_name="Bea B",
                canonical_user_id=OTHER_USER_ID,
            )
        )
        seed_usage(container, OTHER_USER_ID, QuotaDimension.RUNS, 1)
        overview = client.get("/v1/admin/quota").json()
        assert "stored_bytes" in overview["stock_dimensions"]
        subj = next(
            s for s in overview["subjects"]
            if s["user_id"] == str(OTHER_USER_ID)
        )
        assert subj["display_name"] == "Bea B"
        assert subj["email"] == "b@example.com"
        assert subj["dimensions"]["runs"]["used"] == 1
        assert subj["dimensions"]["runs"]["limit"] == 5
        assert subj["dimensions"]["runs"]["period_start"] > 0


def test_admin_overview_subject_without_profile(tmp_path):
    client, container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        login(client, idp)
        promote(users)
        # OTHER_SUB is metered but carries no mirror profile -> None fallback.
        seed_usage(container, OTHER_USER_ID, QuotaDimension.RUNS, 1)
        overview = client.get("/v1/admin/quota").json()
        subj = next(
            s for s in overview["subjects"]
            if s["user_id"] == str(OTHER_USER_ID)
        )
        assert subj["display_name"] is None
        assert subj["email"] is None


def test_admin_mutations_require_csrf(tmp_path):
    """Unsafe admin methods go through the session CSRF gate (cookie BFF)."""
    client, _container, idp, _identity, users = make_world(
        QuotaSettings(enabled=True)
    )
    with client:
        login(client, idp)
        promote(users)
        # No X-CSRF-Token header on a state-changing PUT.
        resp = client.put(
            "/v1/admin/quota/limits",
            json={"user_id": str(OTHER_USER_ID), "dimension": "runs", "value": 1},
        )
        assert resp.status_code == 403
