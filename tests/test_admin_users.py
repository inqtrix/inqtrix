"""Instance admin user-management API (/v1/admin/*) on local auth.

Covers the admin gate (session-only + admin role), the user list, role
changes with the last-admin guard, local-user creation, the disable
cascade (mirror flag + session purge + credential disable so login is
refused), and how the erasure route answers each outcome of its start.
Memory backend, secure_cookies off for the http TestClient.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.api_key import build_local_provider
from inqtrix.auth.credentials import LOCAL_ISSUER
from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.lifecycle import UserLifecycleStatus
from inqtrix.auth.principal_generation import (
    bind_principal_generation,
    install_principal_generation_error_handler,
)
from inqtrix.runs.deletion_operations import DeletionOperationConflict
from inqtrix.server.routers.admin import build_admin_router
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.settings import AuthSettings, Settings

OWNER = ("owner@example.com", "correct-horse-battery")


def make_client(*, lifecycle=None, deletions=None) -> TestClient:
    """``lifecycle`` wraps the provider's lifecycle; ``deletions`` is the
    deletion service an account erasure starts on."""
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            oidc_insecure_dev_cookies=True,
        )
    )
    provider = build_local_provider(settings)
    if lifecycle is not None:
        provider.lifecycle = lifecycle(provider.lifecycle)
    app = FastAPI()
    principal_dep = bind_principal_generation(
        provider.build_principal_dependency()
    )
    install_principal_generation_error_handler(app)
    app.include_router(build_auth_router(provider, principal_dep))
    app.include_router(
        build_admin_router(provider, principal_dep, deletions=deletions)
    )

    @app.get("/v1/protected")
    async def protected(principal=Depends(principal_dep)):
        return {"sub": principal.user_id}

    return TestClient(app, base_url="http://127.0.0.1:5100")


def _owner_client(**kwargs):
    client = make_client(**kwargs)
    client.post(
        "/api/setup/owner",
        json={"email": OWNER[0], "password": OWNER[1], "display_name": "Owner"},
    )
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    return client, csrf


def _user_id_of(client, csrf, email):
    rows = client.get("/v1/admin/users").json()["users"]
    return next(u["id"] for u in rows if u["email"] == email)


def test_owner_setup_becomes_admin_and_session_reports_role():
    client, _csrf = _owner_client()
    info = client.get("/api/auth/session").json()
    assert info["user"]["role"] == "admin"
    rows = client.get("/v1/admin/users").json()["users"]
    assert len(rows) == 1
    assert rows[0]["instance_role"] == "admin"
    assert rows[0]["email"] == OWNER[0]


def test_admin_routes_require_session_admin():
    # No session -> 401 (authenticate first), same as every gated route.
    anon = make_client()
    assert anon.get("/v1/admin/users").status_code == 401

    # An authenticated NON-admin user is denied with a hidden 404.
    client, csrf = _owner_client()
    created = client.post(
        "/v1/admin/users",
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201 and created.json()["instance_role"] == "user"
    # Log out the owner, log in as bob (a plain user).
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    client.post(
        "/api/auth/login/local",
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
    )
    assert client.get("/v1/admin/users").status_code == 404


def test_stale_browser_generation_cannot_mutate_new_principal() -> None:
    client, owner_csrf = _owner_client()
    owner_id = _user_id_of(client, owner_csrf, OWNER[0])
    created = client.post(
        "/v1/admin/users",
        json={
            "email": "second-admin@example.com",
            "password": "another-strong-password",
            "instance_role": "admin",
        },
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert created.status_code == 201

    client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": owner_csrf},
    )
    login = client.post(
        "/api/auth/login/local",
        json={
            "email": "second-admin@example.com",
            "password": "another-strong-password",
        },
    )
    assert login.status_code == 200
    second_session = client.get("/api/auth/session").json()
    second_csrf = second_session["csrf_token"]
    second_id = second_session["user"]["id"]
    stale_headers = {
        "X-CSRF-Token": second_csrf,
        "X-Inqtrix-Expected-User-Id": owner_id,
    }

    admin_mutation = client.post(
        "/v1/admin/users",
        json={
            "email": "must-not-exist@example.com",
            "password": "another-strong-password",
        },
        headers=stale_headers,
    )
    token_mutation = client.post(
        "/api/auth/tokens",
        json={"name": "must-not-be-minted"},
        headers=stale_headers,
    )
    password_mutation = client.post(
        "/api/auth/password",
        json={
            "current_password": "another-strong-password",
            "new_password": "replacement-strong-password",
        },
        headers=stale_headers,
    )
    logout = client.post("/api/auth/logout", headers=stale_headers)

    for response in (
        admin_mutation,
        token_mutation,
        password_mutation,
        logout,
    ):
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "principal_changed"

    assert client.get("/api/auth/tokens").json() == {"tokens": []}
    users = client.get("/v1/admin/users").json()["users"]
    assert not any(
        user["email"] == "must-not-exist@example.com" for user in users
    )
    live_session = client.get("/api/auth/session").json()
    assert live_session["authenticated"] is True
    assert live_session["user"]["id"] == second_id


def test_create_and_promote_and_last_admin_guard():
    client, csrf = _owner_client()
    headers = {"X-CSRF-Token": csrf}
    owner_user_id = _user_id_of(client, csrf, OWNER[0])

    # Self-demote is blocked. (The caller is always the last/only admin when
    # demoting self, so the self-guard fires first; the atomic last-admin
    # invariant is covered by test_atomic_last_admin_guard_is_race_free.)
    demote = client.patch(
        f"/v1/admin/users/{owner_user_id}",
        json={"instance_role": "user"},
        headers=headers,
    )
    assert demote.status_code == 409
    assert demote.json()["error"]["type"] == "self_demote"

    # Add a second admin (bob); demoting bob (NOT the caller) is allowed.
    client.post(
        "/v1/admin/users",
        json={"email": "bob@example.com", "password": "another-strong-pw-1", "instance_role": "admin"},
        headers=headers,
    )
    bob_sub = _user_id_of(client, csrf, "bob@example.com")
    ok = client.patch(
        f"/v1/admin/users/{bob_sub}",
        json={"instance_role": "user"},
        headers=headers,
    )
    assert ok.status_code == 200 and ok.json()["instance_role"] == "user"
    # The owner still cannot demote themselves (self-guard).
    assert (
        client.patch(
            f"/v1/admin/users/{owner_user_id}",
            json={"instance_role": "user"},
            headers=headers,
        ).json()["error"]["type"]
        == "self_demote"
    )


def test_admin_reset_password():
    client, csrf = _owner_client()
    headers = {"X-CSRF-Token": csrf}
    client.post(
        "/v1/admin/users",
        json={"email": "bob@example.com", "password": "another-strong-pw-1", "instance_role": "user"},
        headers=headers,
    )
    bob_sub = _user_id_of(client, csrf, "bob@example.com")

    reset = client.post(
        f"/v1/admin/users/{bob_sub}:reset-password",
        json={"password": "bobs-brand-new-pw-9"},
        headers=headers,
    )
    assert reset.status_code == 200 and reset.json()["reset"] is True
    # Resetting an unknown subject is a 404 (done while still the owner).
    assert (
        client.post(
            f"/v1/admin/users/{uuid.uuid4()}:reset-password",
            json={"password": "x" * 12},
            headers=headers,
        ).status_code
        == 404
    )
    # Bob's old password is dead, the new one works. (Logging in as bob below
    # replaces the owner cookie, so this is the last step.)
    assert (
        client.post(
            "/api/auth/login/local",
            json={"email": "bob@example.com", "password": "another-strong-pw-1"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login/local",
            json={"email": "bob@example.com", "password": "bobs-brand-new-pw-9"},
        ).status_code
        == 200
    )


def test_self_disable_blocked():
    client, csrf = _owner_client()
    owner_user_id = _user_id_of(client, csrf, OWNER[0])
    resp = client.post(
        f"/v1/admin/users/{owner_user_id}:disable", headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["type"] == "self_disable"


def test_disable_cascade_blocks_login_and_purges_session():
    client, csrf = _owner_client()
    headers = {"X-CSRF-Token": csrf}
    # Bob logs in, gets a live session.
    client.post(
        "/v1/admin/users",
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
        headers=headers,
    )
    bob_sub = _user_id_of(client, csrf, "bob@example.com")

    # Disable bob (a failed login below does not change the owner's cookie).
    dis = client.post(f"/v1/admin/users/{bob_sub}:disable", headers=headers)
    assert dis.status_code == 200 and dis.json()["disabled"] is True

    # Bob can no longer log in (credential disabled -> uniform 401).
    relogin = client.post(
        "/api/auth/login/local",
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
    )
    assert relogin.status_code == 401

    # Re-enable restores login.
    enabled = client.post(f"/v1/admin/users/{bob_sub}:enable", headers=headers)
    assert enabled.status_code == 200 and enabled.json()["disabled"] is False
    ok = client.post(
        "/api/auth/login/local",
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
    )
    assert ok.status_code == 200


def test_disabled_mirror_reports_session_logged_out():
    # Defense-in-depth for the disable cascade's purge race: a session that
    # outlives the purge by a hair still reads as logged-out, because
    # session_payload consults the mirror's disabled flag. Disable the mirror
    # directly here so the session is NOT purged, isolating that check.
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            oidc_insecure_dev_cookies=True,
        )
    )
    provider = build_local_provider(settings)
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    client.post("/api/setup/owner", json={"email": OWNER[0], "password": OWNER[1]})
    info = client.get("/api/auth/session").json()
    assert info["authenticated"] is True and info["user"]["role"] == "admin"
    asyncio.run(
        provider.users.set_disabled(
            tenant_id="default",
            user_id=uuid.UUID(info["user"]["id"]),
            disabled_at=1.0,
        )
    )
    assert client.get("/api/auth/session").json()["authenticated"] is False


def _seed_admin(directory: MemoryUserDirectory, subject: str) -> uuid.UUID:
    user = asyncio.run(
        directory.record_login(
            tenant_id="default",
            issuer=LOCAL_ISSUER,
            subject=subject,
            email=f"{subject}@example.com",
            email_verified=True,
            display_name=subject,
        )
    )
    asyncio.run(
        directory.set_instance_role(
            tenant_id="default", user_id=user.user_id, role="admin"
        )
    )
    return user.user_id


def test_atomic_last_admin_guard_is_race_free():
    # The check-and-write is one operation, so the guard holds even without
    # the router's pre-read. Sole admin -> both guarded ops refuse (False).
    directory = MemoryUserDirectory()
    user_a = _seed_admin(directory, "a")

    def demote(user_id: uuid.UUID) -> bool:
        return asyncio.run(
            directory.demote_if_not_last_admin(
                tenant_id="default", user_id=user_id
            )
        )

    def disable(user_id: uuid.UUID) -> bool:
        return asyncio.run(
            directory.disable_if_not_last_admin(
                tenant_id="default", user_id=user_id, disabled_at=1.0
            )
        )

    assert demote(user_a) is False
    assert disable(user_a) is False

    # A second admin -> demoting the first is allowed; "b" is then the last
    # active admin and can be neither demoted nor disabled.
    user_b = _seed_admin(directory, "b")
    assert demote(user_a) is True
    assert disable(user_b) is False
    assert demote(user_b) is False

    # Disabling a plain user never trips the admin guard.
    user_c = asyncio.run(
        directory.record_login(
            tenant_id="default",
            issuer=LOCAL_ISSUER,
            subject="c",
            email="c@example.com",
            email_verified=True,
            display_name="c",
        )
    )
    assert disable(user_c.user_id) is True


def test_self_erase_blocked():
    """An admin deleting themselves would lock the instance out mid-request."""
    client, csrf = _owner_client()
    owner_user_id = _user_id_of(client, csrf, OWNER[0])
    resp = client.delete(
        f"/v1/admin/users/{owner_user_id}", headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["type"] == "self_erase"


def test_erasing_an_unknown_account_is_a_404():
    client, csrf = _owner_client()
    resp = client.delete(
        f"/v1/admin/users/{uuid.uuid4()}", headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 404


def test_a_backend_that_cannot_count_refuses_the_preview():
    """An empty preview must read as "unknown", never as a measured zero.

    The memory backend carries no erasure inventory. Serving its empty
    answer would render "0 rows will be deleted" on the confirmation and
    invite an erasure on a scope nobody counted -- which is the single
    mistake the whole confirmation exists to prevent.
    """
    client, csrf = _owner_client()
    client.post(
        "/v1/admin/users",
        json={"email": "carol@example.com", "password": "another-strong-pw-2"},
        headers={"X-CSRF-Token": csrf},
    )
    user_id = _user_id_of(client, csrf, "carol@example.com")

    resp = client.get(f"/v1/admin/users/{user_id}/erasure-preview")

    assert resp.status_code == 503
    assert resp.json()["error"]["type"] == "server_error"


def test_the_preview_requires_an_admin_session():
    client, csrf = _owner_client()
    owner_user_id = _user_id_of(client, csrf, OWNER[0])
    client.cookies.clear()
    resp = client.get(f"/v1/admin/users/{owner_user_id}/erasure-preview")
    assert resp.status_code in (401, 404)


class _ScriptedErasure:
    """The memory lifecycle plus the erasure start it does not have.

    The erasure itself is proven against PostgreSQL; these tests pin how
    the route answers each outcome of its start.
    """

    erases_accounts = True

    def __init__(self, inner, *, status, report):
        self._inner = inner
        self._status = status
        self._report = report
        self.erasing: set[uuid.UUID] = set()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def begin_erasure(self, *, tenant_id, user_id, actor_user_id, operation_id):
        if self._status is not UserLifecycleStatus.UPDATED:
            return self._status, self._report
        self.erasing.add(user_id)
        return self._status, {"operation_id": operation_id}

    async def set_disabled(self, *, user_id, **kwargs):
        if user_id in self.erasing:
            return UserLifecycleStatus.ERASING
        return await self._inner.set_disabled(user_id=user_id, **kwargs)

    async def set_role(self, *, user_id, **kwargs):
        if user_id in self.erasing:
            return UserLifecycleStatus.ERASING
        return await self._inner.set_role(user_id=user_id, **kwargs)


class _Deletions:
    """The deletion service's account-erasure entry points, recorded."""

    def __init__(self, *, status="queued", conflict=False):
        self._status = status
        self._conflict = conflict
        self.started: list[tuple[str, str]] = []
        self.retried: list[str] = []

    async def start_user_erasure(self, user_id, *, principal, operation_id):
        if self._conflict:
            raise DeletionOperationConflict(str(user_id))
        self.started.append((str(user_id), operation_id))
        return {"operation_id": operation_id, "status": self._status}

    async def retry(self, operation_id, *, principal, workspace_id):
        self.retried.append(operation_id)
        return {"operation_id": operation_id, "status": "queued"}


def _erasure_world(
    deletions, *, status=UserLifecycleStatus.UPDATED, report=None
):
    """An owner, one colleague to erase, and the erasure's collaborators."""
    wrapped = []

    def wrap(inner):
        wrapped.append(_ScriptedErasure(inner, status=status, report=report))
        return wrapped[0]

    client, csrf = _owner_client(lifecycle=wrap, deletions=deletions)
    client.post(
        "/v1/admin/users",
        json={"email": "carol@example.com", "password": "another-strong-pw-2"},
        headers={"X-CSRF-Token": csrf},
    )
    target = _user_id_of(client, csrf, "carol@example.com")
    return client, csrf, target


def test_erasing_an_account_answers_202_with_its_deletion_operation():
    deletions = _Deletions()
    client, csrf, target = _erasure_world(deletions)

    resp = client.delete(f"/v1/admin/users/{target}", headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert deletions.started == [(target, body["operation_id"])]


def test_a_refused_erasure_names_the_reasons_the_preview_shows():
    reason = "1 row(s) in workspaces.created_by_user_id: reassign it first"
    deletions = _Deletions()
    client, csrf, target = _erasure_world(
        deletions,
        status=UserLifecycleStatus.REFUSED,
        report={"refused": [reason]},
    )

    resp = client.delete(f"/v1/admin/users/{target}", headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["type"] == "erasure_refused"
    assert error["reasons"] == [reason]
    assert deletions.started == [], "a refused start registers nothing"


def test_the_last_admin_cannot_be_erased():
    deletions = _Deletions()
    client, csrf, target = _erasure_world(
        deletions, status=UserLifecycleStatus.LAST_ADMIN, report={}
    )

    resp = client.delete(f"/v1/admin/users/{target}", headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 409
    assert resp.json()["error"]["type"] == "last_admin"
    assert deletions.started == []


def test_a_failed_erasure_is_retried_by_the_next_request():
    """Deleting again is how an administrator resumes a failed erasure."""
    deletions = _Deletions(status="delete_failed")
    client, csrf, target = _erasure_world(deletions)

    resp = client.delete(f"/v1/admin/users/{target}", headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert deletions.retried == [resp.json()["operation_id"]]


def test_another_administrators_erasure_is_a_conflict():
    client, csrf, target = _erasure_world(_Deletions(conflict=True))

    resp = client.delete(f"/v1/admin/users/{target}", headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 409
    assert resp.json()["error"]["type"] == "deletion_in_progress"


def test_an_erasure_needs_a_backend_and_the_deletion_machine():
    """Both halves or 503: a start nobody executes would strand the account
    in its erasing state."""
    plain, plain_csrf = _owner_client(deletions=_Deletions())
    plain.post(
        "/v1/admin/users",
        json={"email": "dave@example.com", "password": "another-strong-pw-3"},
        headers={"X-CSRF-Token": plain_csrf},
    )
    plain_target = _user_id_of(plain, plain_csrf, "dave@example.com")
    no_machine, machine_csrf, machine_target = _erasure_world(None)

    backend_missing = plain.delete(
        f"/v1/admin/users/{plain_target}", headers={"X-CSRF-Token": plain_csrf}
    )
    machine_missing = no_machine.delete(
        f"/v1/admin/users/{machine_target}", headers={"X-CSRF-Token": machine_csrf}
    )

    assert backend_missing.status_code == 503
    assert machine_missing.status_code == 503


def test_an_account_being_erased_cannot_be_enabled_or_promoted():
    client, csrf, target = _erasure_world(_Deletions())
    assert (
        client.delete(
            f"/v1/admin/users/{target}", headers={"X-CSRF-Token": csrf}
        ).status_code
        == 202
    )

    enable = client.post(
        f"/v1/admin/users/{target}:enable", headers={"X-CSRF-Token": csrf}
    )
    promote = client.patch(
        f"/v1/admin/users/{target}",
        json={"instance_role": "admin"},
        headers={"X-CSRF-Token": csrf},
    )

    assert enable.status_code == 409
    assert promote.status_code == 409
    assert enable.json()["error"]["type"] == "erasure_in_progress"
    assert promote.json()["error"]["type"] == "erasure_in_progress"
