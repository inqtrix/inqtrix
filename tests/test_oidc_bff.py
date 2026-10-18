"""OIDC BFF tests: full login roundtrip, hardening, CSRF — offline.

A fake IdP lives entirely in an ``httpx.MockTransport``: discovery,
JWKS (a real in-test RSA key), token endpoint (id_tokens signed with
that key). The flows therefore exercise the REAL validation path —
signature, issuer, audience, expiry, nonce — not mocks of it.

The hardening cases pin the security review checklist: ``alg`` none and
HS256 rejected (alg confusion), wrong nonce/issuer/audience rejected,
replayed state rejected, login CSRF (callback without the flow cookie)
rejected, API CSRF enforced on unsafe methods, group allowlist enforced.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet, RSAKey
from joserfc.jwk import OctKey

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.lifecycle import (
    MemoryUserLifecycleTransaction,
    UserLifecycleService,
)
from inqtrix.auth.oidc import (
    OidcAuthProvider,
    OidcClient,
    OidcExchangeError,
    make_csrf_token,
)
from inqtrix.auth.sessions import MemoryFlowStore, MemorySessionStore
from inqtrix.server.routers.auth import build_auth_router

ISSUER = "https://idp.example/dex"
CLIENT_ID = "inqtrix-test"
REDIRECT = "http://127.0.0.1:5100/api/auth/callback"

RSA_KEY = RSAKey.generate_key(2048, {"kid": "test-key", "alg": "RS256"})
JWKS = KeySet([RSA_KEY]).as_dict(private=False)


class FakeIdp:
    """Scripted IdP behind an httpx MockTransport."""

    def __init__(self) -> None:
        self.claims_override: dict[str, Any] = {}
        self.omit_claims: set[str] = set()
        self.signer = RSA_KEY
        self.algorithm = "RS256"
        self.token_status = 200
        self.userinfo_calls = 0

    def id_token(self, *, nonce: str) -> str:
        claims = {
            "iss": ISSUER,
            "sub": "user-1234",
            "aud": CLIENT_ID,
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "nonce": nonce,
            "email": "alice@example.com",
            "email_verified": True,
            "preferred_username": "alice",
            "groups": ["team-a"],
        }
        claims.update(self.claims_override)
        for claim in self.omit_claims:
            claims.pop(claim, None)
        header = {"alg": self.algorithm, "kid": "test-key"}
        if self.algorithm == "none":
            import base64

            segment = lambda data: (  # noqa: E731 — local helper
                base64.urlsafe_b64encode(
                    json.dumps(data).encode()
                ).rstrip(b"=").decode()
            )
            return f"{segment(header)}.{segment(claims)}."
        key = (
            self.signer
            if self.algorithm == "RS256"
            else OctKey.import_key(b"x" * 32)
        )
        return jose_jwt.encode(header, claims, key)

    def transport(self) -> httpx.MockTransport:
        captured_nonce: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/.well-known/openid-configuration"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "authorization_endpoint": f"{ISSUER}/auth",
                        "token_endpoint": f"{ISSUER}/token",
                        "jwks_uri": f"{ISSUER}/keys",
                        "userinfo_endpoint": f"{ISSUER}/userinfo",
                    },
                )
            if path.endswith("/keys"):
                return httpx.Response(200, json=JWKS)
            if path.endswith("/token"):
                body = urllib.parse.parse_qs(request.content.decode())
                nonce = captured_nonce.get("value", "")
                if self.token_status != 200:
                    return httpx.Response(self.token_status, json={})
                return httpx.Response(
                    200,
                    json={
                        "access_token": "at-1",
                        "id_token": self.id_token(nonce=nonce),
                        "token_type": "bearer",
                        "_code": body.get("code", [""])[0],
                    },
                )
            if path.endswith("/userinfo"):
                self.userinfo_calls += 1
                return httpx.Response(
                    200, json={"sub": "user-1234", "groups": ["team-a"]}
                )
            return httpx.Response(404)

        self._captured_nonce = captured_nonce
        return httpx.MockTransport(handler)

    def remember_nonce(self, authorize_url: str) -> None:
        """Record the nonce the relying party sent (the IdP echoes it)."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)
        self._captured_nonce["value"] = query["nonce"][0]


def make_provider(
    idp: FakeIdp, **overrides: Any
) -> tuple[OidcAuthProvider, MemoryUserDirectory]:
    users = MemoryUserDirectory()
    client = OidcClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="secret",
        redirect_url=REDIRECT,
        transport=idp.transport(),
    )
    sessions = MemorySessionStore()
    arguments: dict[str, Any] = dict(
        client=client,
        sessions=sessions,
        flows=MemoryFlowStore(),
        users=users,
        session_secret="test-session-secret",
        session_max_age_seconds=3600,
        secure_cookies=False,
    )
    arguments.update(overrides)
    if "lifecycle" not in arguments:
        pat_service = arguments.get("pat_service")
        pat_store = getattr(pat_service, "_store", None)
        transaction = MemoryUserLifecycleTransaction(
            users=arguments["users"],
            sessions=arguments["sessions"],
            invitations=arguments.get("invitations"),
            pat_store=pat_store,
        )
        arguments["lifecycle"] = UserLifecycleService(
            users=arguments["users"],
            sessions=arguments["sessions"],
            invitations=arguments.get("invitations"),
            pat_service=pat_service,
            transaction=transaction,
        )
    return OidcAuthProvider(**arguments), users


def make_app(provider: OidcAuthProvider, *, audit: Any = None) -> TestClient:
    from fastapi import Depends

    app = FastAPI()
    app.include_router(build_auth_router(provider, audit=audit))
    principal_dep = provider.build_principal_dependency()

    @app.get("/v1/protected")
    async def protected_get(principal=Depends(principal_dep)):
        return {"sub": principal.user_id, "kind": principal.kind}

    @app.post("/v1/protected")
    async def protected_post(principal=Depends(principal_dep)):
        return {"sub": principal.user_id}

    return TestClient(app, base_url="http://127.0.0.1:5100")


def run_login(client: TestClient, idp: FakeIdp) -> httpx.Response:
    """Drive login -> IdP redirect -> callback; returns the callback."""
    start = client.get("/api/auth/login", follow_redirects=False)
    assert start.status_code == 302
    authorize_url = start.headers["location"]
    idp.remember_nonce(authorize_url)
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(authorize_url).query
    )
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    state = query["state"][0]
    return client.get(
        f"/api/auth/callback?code=fake-code&state={state}",
        follow_redirects=False,
    )


# ------------------------------------------------------------------ #
# Happy path
# ------------------------------------------------------------------ #


def test_full_login_roundtrip_establishes_a_session():
    idp = FakeIdp()
    provider, users = make_provider(idp)
    client = make_app(provider)

    callback = run_login(client, idp)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    assert "inqtrix_session" in callback.cookies

    session_response = client.get("/api/auth/session")
    info = session_response.json()
    assert info["authenticated"] is True
    assert info["user"]["display_name"] == "alice"
    assert info["csrf_token"]
    assert session_response.cookies.get("inqtrix_csrf") == info["csrf_token"]

    protected = client.get("/v1/protected")
    assert protected.status_code == 200
    assert protected.json() == {
        "sub": info["user"]["id"],
        "kind": "oidc_session",
    }

    # JIT mirror recorded the (issuer, subject) anchor.
    assert ("default", ISSUER, "user-1234") in users.users


def test_unsafe_method_requires_the_csrf_header():
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)
    run_login(client, idp)

    without = client.post("/v1/protected")
    assert without.status_code == 403
    # HTTPException details arrive under FastAPI's default wrapper —
    # the same {"detail": {"error": ...}} shape the apikey gate pins.
    assert without.json()["detail"]["error"]["type"] == "csrf_error"

    token = client.get("/api/auth/session").json()["csrf_token"]
    with_token = client.post(
        "/v1/protected", headers={"X-CSRF-Token": token}
    )
    assert with_token.status_code == 200

    forged = make_csrf_token("wrong-secret", "sid", "00" * 16)
    assert (
        client.post(
            "/v1/protected", headers={"X-CSRF-Token": forged}
        ).status_code
        == 403
    )


def test_logout_destroys_the_session():
    class _RecordingSink:
        def __init__(self) -> None:
            self.entries = []

        async def record(self, entry) -> None:
            self.entries.append(entry)

    idp = FakeIdp()
    provider, _users = make_provider(idp)
    audit = _RecordingSink()
    client = make_app(provider, audit=audit)
    run_login(client, idp)
    raw_session_id = client.cookies.get("inqtrix_session")
    assert raw_session_id is not None and len(raw_session_id) == 43
    token = client.get("/api/auth/session").json()["csrf_token"]

    response = client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 200
    assert client.get("/api/auth/session").json() == {
        "authenticated": False
    }
    assert client.get("/v1/protected").status_code == 401
    logouts = [entry for entry in audit.entries if entry.action == "auth.logout"]
    assert len(logouts) == 1
    resource_id = logouts[0].resource_id
    assert re.fullmatch(r"ses_[0-9a-f]{16}", resource_id)
    assert raw_session_id not in resource_id
    assert raw_session_id[:12] not in resource_id
    assert raw_session_id[-12:] not in resource_id


# ------------------------------------------------------------------ #
# Hardening
# ------------------------------------------------------------------ #


def test_unauthenticated_request_is_rejected():
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)
    response = client.get("/v1/protected")
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "unauthorized"


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_alg_confusion_is_rejected(algorithm):
    idp = FakeIdp()
    idp.algorithm = algorithm
    provider, _users = make_provider(idp)
    client = make_app(provider)

    callback = run_login(client, idp)
    assert callback.status_code == 403
    assert callback.json()["error"]["type"] == "oidc_error"


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"iss": "https://evil.example"}, "id_token issuer mismatch"),
        ({"aud": "other-client"}, "id_token audience does not include the client id"),
        ({"exp": int(time.time()) - 600}, "id_token has expired"),
        ({"nonce": "stolen-nonce"}, "id_token nonce mismatch"),
        ({"email_verified": False}, "Email address is not verified"),
    ],
)
def test_claim_violations_are_rejected(override, fragment):
    idp = FakeIdp()
    idp.claims_override = override
    provider, _users = make_provider(idp)
    client = make_app(provider)

    callback = run_login(client, idp)
    assert callback.status_code == 403
    assert fragment in callback.json()["error"]["message"]


def test_missing_email_verified_claim_is_rejected():
    idp = FakeIdp()
    idp.omit_claims.add("email_verified")
    provider, _users = make_provider(idp)
    client = make_app(provider)

    callback = run_login(client, idp)
    assert callback.status_code == 403
    assert "Email address is not verified" in callback.json()["error"]["message"]


@pytest.mark.parametrize("claim_state", ["missing", "false"])
def test_explicit_email_verified_skip_accepts_unverified_claim(
    claim_state, caplog
):
    idp = FakeIdp()
    if claim_state == "missing":
        idp.omit_claims.add("email_verified")
    else:
        idp.claims_override = {"email_verified": False}
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        provider, _users = make_provider(idp, skip_email_verified=True)
    client = make_app(provider)

    callback = run_login(client, idp)

    assert callback.status_code == 303
    assert "email_verified check is explicitly disabled" in caplog.text


def test_replayed_state_is_rejected():
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)

    start = client.get("/api/auth/login", follow_redirects=False)
    idp.remember_nonce(start.headers["location"])
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(start.headers["location"]).query
    )
    state = query["state"][0]
    first = client.get(
        f"/api/auth/callback?code=c&state={state}", follow_redirects=False
    )
    assert first.status_code == 303
    replay = client.get(
        f"/api/auth/callback?code=c&state={state}", follow_redirects=False
    )
    # The first callback clears the flow cookie, so the replay is
    # rejected at the login-CSRF check; a replay that somehow carries
    # the cookie again fails on the consumed flow record — both are
    # 400 oidc_error.
    assert replay.status_code == 400
    assert replay.json()["error"]["type"] == "oidc_error"


def test_callback_without_flow_cookie_is_login_csrf():
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)
    # Attacker-supplied state without the browser having started a flow.
    response = client.get(
        "/api/auth/callback?code=c&state=attacker-state",
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Invalid login transaction" in response.json()["error"]["message"]


def test_group_allowlist_gates_logins():
    idp = FakeIdp()
    provider, _users = make_provider(
        idp, allowed_groups=frozenset({"platform-admins"})
    )
    client = make_app(provider)
    callback = run_login(client, idp)
    assert callback.status_code == 403
    assert "ALLOWED_GROUPS" in callback.json()["error"]["message"]


def test_email_domain_allowlist_gates_logins():
    idp = FakeIdp()
    provider, _users = make_provider(
        idp, allowed_domains=frozenset({"corp.example"})
    )
    client = make_app(provider)
    # The default token email is alice@example.com, not in the allowlist.
    callback = run_login(client, idp)
    assert callback.status_code == 403
    assert "ALLOWED_DOMAINS" in callback.json()["error"]["message"]


def test_admin_role_claim_grants_instance_admin():
    idp = FakeIdp()
    provider, users = make_provider(
        idp,
        admin_roles=frozenset({"inqtrix-admin"}),
        userinfo_fallback=False,
    )
    client = make_app(provider)

    # First login (no roles) bootstraps the owner, so the next users are
    # NOT auto-promoted by the first-login-owner rule.
    run_login(client, idp)

    # A different user carrying the admin role is promoted via the claim
    # alone (grant-only, parity with the LDAP admin-group path).
    idp.claims_override = {"sub": "bob", "roles": ["inqtrix-admin"]}
    run_login(client, idp)
    assert users.users[("default", ISSUER, "bob")].instance_role == "admin"

    # A third user without the admin role stays a regular user.
    idp.claims_override = {"sub": "carol", "roles": ["staff"]}
    run_login(client, idp)
    assert users.users[("default", ISSUER, "carol")].instance_role == "user"


def test_roles_claim_does_not_fetch_userinfo_when_elevation_unconfigured():
    # No admin_roles/admin_groups -> the roles claim changes nothing, so
    # the default token (username/email/groups present) must NOT trigger a
    # userinfo round-trip just because it lacks a roles claim.
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)
    run_login(client, idp)
    assert idp.userinfo_calls == 0


def test_roles_claim_fetches_userinfo_when_elevation_configured():
    # With admin-from-claims configured, a token lacking the roles claim
    # DOES pull userinfo so the elevation decision sees the roles.
    idp = FakeIdp()
    provider, _users = make_provider(
        idp, admin_roles=frozenset({"inqtrix-admin"})
    )
    client = make_app(provider)
    run_login(client, idp)
    assert idp.userinfo_calls == 1


def test_open_redirect_targets_are_normalized():
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)
    start = client.get(
        "/api/auth/login?next=//evil.example/phish",
        follow_redirects=False,
    )
    idp.remember_nonce(start.headers["location"])
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(start.headers["location"]).query
    )
    callback = client.get(
        f"/api/auth/callback?code=c&state={query['state'][0]}",
        follow_redirects=False,
    )
    assert callback.headers["location"] == "/"


def test_expired_session_resolves_unauthenticated():
    idp = FakeIdp()
    provider, _users = make_provider(idp, session_max_age_seconds=300)
    client = make_app(provider)
    run_login(client, idp)
    # Backdate the stored session by mutating the memory store.
    store = provider.sessions
    session = next(iter(store._sessions.values()))
    store._sessions[session.id] = type(session)(
        **{**session.__dict__, "expires_at": time.time() - 1}
    )
    assert client.get("/v1/protected").status_code == 401


def test_token_endpoint_failure_is_a_visible_error():
    idp = FakeIdp()
    idp.token_status = 500
    provider, _users = make_provider(idp)
    client = make_app(provider)
    callback = run_login(client, idp)
    assert callback.status_code == 403
    assert "Token endpoint returned status 500" in callback.json()["error"]["message"]


def test_resolve_identity_requires_a_subject():
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    with pytest.raises(OidcExchangeError, match="Subject"):
        provider.resolve_identity({"iss": ISSUER})


@pytest.mark.asyncio
async def test_flow_store_consumption_is_one_time():
    from inqtrix.auth.sessions import LoginFlow, MemoryFlowStore

    store = MemoryFlowStore()
    flow = LoginFlow(
        state="s1",
        code_verifier="v",
        nonce="n",
        next_path="/",
        expires_at=time.time() + 60,
    )
    await store.put(flow)
    first = await store.consume("s1")
    second = await store.consume("s1")
    assert first is not None and first.code_verifier == "v"
    assert second is None


def test_auth_responses_declare_no_store():
    """Identity facts and the CSRF token must never be cacheable."""
    idp = FakeIdp()
    provider, _users = make_provider(idp)
    client = make_app(provider)
    anonymous = client.get("/api/auth/session")
    assert anonymous.headers["cache-control"] == "no-store"
    run_login(client, idp)
    authenticated = client.get("/api/auth/session")
    assert authenticated.headers["cache-control"] == "no-store"


def test_userinfo_fallback_merges_thin_tokens_but_rejects_foreign_sub():
    """Thin ID tokens without a groups claim get their claims from userinfo; a
    userinfo answer for a DIFFERENT subject is discarded entirely."""
    idp = FakeIdp()
    # Thin token: no groups claim in the id_token.
    idp.claims_override = {"groups": None}
    provider, _users = make_provider(
        idp, allowed_groups=frozenset({"team-a"})
    )
    client = make_app(provider)
    # userinfo (FakeIdp) returns groups=["team-a"] for the same sub,
    # so the allowlist gate passes only via the merge.
    callback = run_login(client, idp)
    assert callback.status_code == 303

    # Foreign sub in userinfo: the merge must be discarded, leaving
    # the thin token without groups -> allowlist rejects the login.
    idp2 = FakeIdp()
    idp2.claims_override = {"groups": None, "sub": "user-9999"}
    provider2, _ = make_provider(
        idp2, allowed_groups=frozenset({"team-a"})
    )
    client2 = make_app(provider2)
    rejected = run_login(client2, idp2)
    assert rejected.status_code == 403
