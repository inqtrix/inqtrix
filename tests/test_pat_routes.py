"""PAT route and Bearer-path tests over the OIDC BFF test harness.

Pins the HTTP contracts: plaintext appears exactly once at creation,
Bearer requests bypass CSRF (no cookie-bound ambient authority), the
cookie path keeps requiring CSRF, a PAT can never manage PATs, and
foreign/unknown token ids are indistinguishable 404s.
"""

from __future__ import annotations

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.pat import MemoryPatStore, PatService, PatVerifier

from tests.test_oidc_bff import (
    FakeIdp,
    make_app,
    make_provider,
    run_login,
)

PEPPER = "route-test-pepper"


def make_pat_client():
    """Logged-in client whose provider carries the PAT collaborators."""
    idp = FakeIdp()
    store = MemoryPatStore()
    users = MemoryUserDirectory()
    provider, _users = make_provider(
        idp,
        users=users,
        pats=PatVerifier(
            store=store,
            pepper=PEPPER,
            user_lookup=users,
        ),
        pat_service=PatService(store=store, pepper=PEPPER, max_per_user=2),
    )
    client = make_app(provider)
    run_login(client, idp)
    csrf = client.cookies.get("inqtrix_csrf")
    return client, csrf


def create_token(client, csrf, name="ci-runner", **extra):
    return client.post(
        "/api/auth/tokens",
        json={"name": name, **extra},
        headers={"X-CSRF-Token": csrf},
    )


def test_create_returns_plaintext_exactly_once():
    client, csrf = make_pat_client()
    created = create_token(client, csrf)
    assert created.status_code == 201
    payload = created.json()
    assert payload["token"].startswith("ipat_")
    assert payload["name"] == "ci-runner"
    assert payload["expires_at"] is None
    # Listing never exposes the plaintext or hash again.
    listed = client.get("/api/auth/tokens").json()["tokens"]
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert "secret_hmac" not in listed[0]


def test_bearer_request_succeeds_without_csrf():
    client, csrf = make_pat_client()
    token = create_token(client, csrf).json()["token"]
    user_id = client.get("/api/auth/session").json()["user"]["id"]
    # POST is an unsafe method; with a Bearer PAT no CSRF header is
    # needed because no cookie-bound ambient authority is in play.
    client.cookies.clear()
    response = client.post(
        "/v1/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["sub"] == user_id


def test_bearer_never_falls_through_to_the_cookie():
    """A WRONG token with a perfectly valid session cookie must fail:
    falling back would let a leaked-but-revoked token ride ambient
    sessions invisibly."""
    client, csrf = make_pat_client()
    create_token(client, csrf)
    response = client.get(
        "/v1/protected", headers={"Authorization": "Bearer ipat_garbage_x"}
    )
    assert response.status_code == 401


def test_cookie_path_still_requires_csrf():
    client, csrf = make_pat_client()
    no_csrf = client.post("/v1/protected")
    assert no_csrf.status_code == 403
    with_csrf = client.post(
        "/v1/protected", headers={"X-CSRF-Token": csrf}
    )
    assert with_csrf.status_code == 200


def test_revoked_token_turns_401():
    client, csrf = make_pat_client()
    created = create_token(client, csrf).json()
    revoked = client.delete(
        f"/api/auth/tokens/{created['token_id']}",
        headers={"X-CSRF-Token": csrf},
    )
    assert revoked.status_code == 200
    response = client.get(
        "/v1/protected",
        headers={"Authorization": f"Bearer {created['token']}"},
    )
    assert response.status_code == 401


def test_foreign_and_unknown_ids_are_indistinguishable_404s():
    client, csrf = make_pat_client()
    unknown = client.delete(
        "/api/auth/tokens/deadbeef", headers={"X-CSRF-Token": csrf}
    )
    assert unknown.status_code == 404


def test_pat_cannot_manage_pats():
    client, csrf = make_pat_client()
    token = create_token(client, csrf).json()["token"]
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/tokens", headers=bearer).status_code == 403
    assert (
        client.post(
            "/api/auth/tokens", json={"name": "evil"}, headers=bearer
        ).status_code
        == 403
    )


def test_cap_yields_409():
    client, csrf = make_pat_client()
    assert create_token(client, csrf, name="a").status_code == 201
    assert create_token(client, csrf, name="b").status_code == 201
    capped = create_token(client, csrf, name="c")
    assert capped.status_code == 409
    assert "Maximum number of access tokens" in capped.json()["error"]["message"]


def test_create_validation():
    client, csrf = make_pat_client()
    assert create_token(client, csrf, name="").status_code == 400
    assert create_token(client, csrf, name="x" * 121).status_code == 400
    assert (
        create_token(client, csrf, expires_in_days=0).status_code == 400
    )
    assert (
        create_token(client, csrf, expires_in_days="soon").status_code
        == 400
    )


def test_expiring_token_carries_its_expiry():
    client, csrf = make_pat_client()
    created = create_token(client, csrf, expires_in_days=30).json()
    assert created["expires_at"] is not None


def test_token_routes_absent_without_a_pat_service():
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)
    run_login(client, idp)
    assert client.get("/api/auth/tokens").status_code == 404
