"""Gateway mode: from an asserted identity to an ordinary Inqtrix session.

A session starts only in ``GET /api/auth/session`` and only from a valid
assertion; every later request carries the assertion again and must name the
user its session was started for. Admission, invitations and the admin grant
follow the shared login provisioning. Rejections are counted per outcome and
reported at most once per interval, and no identity value reaches the log.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import sys
import time
import uuid

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet, RSAKey

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.gateway import (
    OUTCOME_STATUS,
    REJECTION_WARNING_INTERVAL_SECONDS,
    CustomResolverGuard,
    GatewayAssertionError,
    GatewayAuthProvider,
    GatewayRejected,
    GatewayRejections,
    IdentitySource,
    JwtAssertionResolver,
    TrustedHeaderResolver,
    build_gateway_provider,
)
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.invitations import (
    CONFIRMED_EMAIL_REQUIRED,
    MemoryInvitationStore,
    RegistrationGate,
)
from inqtrix.auth.jwks import StaticKeys
from inqtrix.auth.lifecycle import (
    ExternalProfile,
    MemoryUserLifecycleTransaction,
    UserLifecycleService,
)
from inqtrix.auth.mapping import ClaimMappingConfig
from inqtrix.auth.pat import MemoryPatStore, PatService, PatVerifier
from inqtrix.auth.permissions import WorkspaceRole
from inqtrix.auth.principal import Principal, resolve_live_principal
from inqtrix.auth.principal_generation import (
    EXPECTED_USER_ID_HEADER,
    bind_principal_generation,
    install_principal_generation_error_handler,
)
from inqtrix.auth.sessions import MemoryFlowStore, MemorySessionStore
from inqtrix.server.routers import auth_config
from inqtrix.server.routers.admin import build_admin_router
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.settings import AuthSettings, Settings

ISSUER = "https://idp.example/realms/corp"
ASSERTION = "X-Inqtrix-Assertion"
KEY = RSAKey.generate_key(2048, {"kid": "gateway-key", "alg": "RS256"})
LOGOUT_URL = "https://gateway.example/oauth2/sign_out"


def _claims(subject: str, **overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": "inqtrix",
        "sub": subject,
        "scope": "openid inqtrix.user",
        "iat": now,
        "exp": now + 300,
        "email": f"{subject}@example.com",
        "email_verified": True,
        "preferred_username": subject.title(),
        "groups": ["staff"],
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def _token(subject: str = "user-a", **overrides: object) -> str:
    return jose_jwt.encode({"alg": "RS256", "kid": KEY.kid}, _claims(subject, **overrides), KEY)


def _as(subject: str = "user-a", **overrides: object) -> dict[str, str]:
    return {ASSERTION: _token(subject, **overrides)}


class World:
    """A gateway provider on memory stores, with an app around it."""

    def __init__(
        self,
        *,
        mapping: ClaimMappingConfig | None = None,
        first_user_owner: bool = False,
        logout_url: str = LOGOUT_URL,
        registration: str = "open",
        identity: MemoryIdentityStore | None = None,
        assertion: str = "jwt",
        resolver: object | None = None,
        issuer_key: str = ISSUER,
        pat_max_ttl_days: int = 0,
        scim_enabled: bool = False,
        peer: str = "testclient",
    ) -> None:
        self.mapping = mapping or ClaimMappingConfig()
        self.users = MemoryUserDirectory()
        self.sessions = MemorySessionStore()
        self.identity = identity
        self.invitations = MemoryInvitationStore(identity) if identity is not None else None
        pat_store = MemoryPatStore()
        self.transaction = MemoryUserLifecycleTransaction(
            users=self.users,
            sessions=self.sessions,
            invitations=self.invitations,
            pat_store=pat_store,
            source_issuer=issuer_key,
        )
        lifecycle = UserLifecycleService(
            users=self.users,
            sessions=self.sessions,
            invitations=self.invitations,
            transaction=self.transaction,
        )
        resolver = resolver or JwtAssertionResolver(
            header_name=ASSERTION,
            issuer=ISSUER,
            audience="inqtrix",
            user_scope="inqtrix.user",
            algorithms=["RS256"],
            leeway_seconds=30,
            subject_claim="sub",
            claim_mapping=self.mapping,
            keys=StaticKeys(KeySet([KEY])),
        )
        self.provider = GatewayAuthProvider(
            source=IdentitySource(issuer_key=issuer_key, resolver=resolver),
            assertion=assertion,
            claim_mapping=self.mapping,
            first_user_owner=first_user_owner,
            logout_url=logout_url,
            sessions=self.sessions,
            flows=MemoryFlowStore(),
            users=self.users,
            session_secret="s" * 32,
            session_max_age_seconds=3600,
            secure_cookies=False,
            pats=PatVerifier(
                store=pat_store,
                pepper="p" * 32,
                user_lookup=self.users,
                max_ttl_seconds=pat_max_ttl_days * 86_400.0,
                source_issuer=issuer_key,
            ),
            pat_service=PatService(store=pat_store, pepper="p" * 32, max_ttl_days=pat_max_ttl_days),
            registration_gate=RegistrationGate(
                invitations=self.invitations,
                users=self.users,
                registration=registration,
            ),
            invitations=self.invitations,
            lifecycle=lifecycle,
            scim_enabled=scim_enabled,
        )
        self.dependency = bind_principal_generation(self.provider.build_principal_dependency())
        app = FastAPI()
        install_principal_generation_error_handler(app)
        app.include_router(build_auth_router(self.provider, self.dependency))
        app.include_router(build_admin_router(self.provider, self.dependency))
        dependency = self.dependency

        @app.get("/v1/protected")
        async def protected_get(principal=Depends(dependency)):
            return {"user_id": str(principal.user_id), "kind": principal.kind}

        @app.post("/v1/protected")
        async def protected_post(principal=Depends(dependency)):
            return {"user_id": str(principal.user_id)}

        self.client = TestClient(app, base_url="http://127.0.0.1:5100", client=(peer, 50_000))

    def start(self, subject: str = "user-a", **overrides: object):
        return self.client.get("/api/auth/session", headers=_as(subject, **overrides))

    def csrf(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get("inqtrix_csrf", "")}

    def all_users(self):
        return asyncio.run(self.users.list_users(tenant_id="default"))


def _error_type(response) -> str:
    body = response.json()
    error = body.get("error") or body.get("detail", {}).get("error", {})
    return error.get("type", "")


# ------------------------------------------------------------------ #
# Session start: none, invalid, resumed, switched or new
# ------------------------------------------------------------------ #


def test_without_an_assertion_no_session_starts() -> None:
    world = World()

    response = world.client.get("/api/auth/session")

    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "reason": "assertion_missing"}
    assert "set-cookie" not in response.headers
    assert world.all_users() == ()


@pytest.mark.parametrize(
    ("make_headers", "status", "outcome"),
    [
        (lambda: {ASSERTION: "not-a-token"}, 400, "malformed"),
        (lambda: {ASSERTION: jose_jwt.encode({"alg": "RS256", "kid": "gateway-key"}, _claims("user-a"), RSAKey.generate_key(2048))}, 401, "signature_invalid"),
        (lambda: {ASSERTION: _token("user-a", scope="openid")}, 401, "claims_invalid"),
        (lambda: {ASSERTION: _token("user-a", exp=int(time.time()) - 3600)}, 401, "expired"),
        (lambda: {ASSERTION: _token("someone@example.com")}, 403, "subject_is_email"),
    ],
    ids=["malformed", "signature", "no-user-scope", "expired", "subject-is-email"],
)
def test_an_invalid_assertion_answers_with_its_outcome(make_headers, status, outcome) -> None:
    world = World()

    # The token is minted here, not when the tests are collected: in a long
    # run it would otherwise expire before the test starts.
    response = world.client.get("/api/auth/session", headers=make_headers())

    assert response.status_code == status
    assert _error_type(response) == outcome
    assert "set-cookie" not in response.headers
    assert world.all_users() == ()


def test_a_valid_assertion_starts_a_session_for_a_new_user() -> None:
    world = World()

    response = world.start("user-a")

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["user"]["display_name"] == "User-A"
    assert body["user"]["email"] == "user-a@example.com"
    assert body["user"]["role"] == "user"
    assert body["csrf_token"] == world.client.cookies.get("inqtrix_csrf")
    assert world.client.cookies.get("inqtrix_session")
    (user,) = world.all_users()
    assert (user.issuer, user.subject) == (ISSUER, "user-a")
    assert user.tenant_id == "default"


def test_a_matching_session_resumes_without_a_new_one() -> None:
    world = World()
    world.start("user-a")
    first = world.client.cookies.get("inqtrix_session")

    again = world.start("user-a")

    assert again.status_code == 200
    assert again.json()["authenticated"] is True
    assert world.client.cookies.get("inqtrix_session") == first
    assert len(world.all_users()) == 1
    # The readable double-submit cookie is renewed from the same value.
    assert f"inqtrix_csrf={again.json()['csrf_token']}" in again.headers["set-cookie"]


def test_a_session_of_another_identity_is_replaced() -> None:
    world = World()
    world.start("user-a")
    session_a = world.client.cookies.get("inqtrix_session")

    response = world.start("user-b")

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "user-b@example.com"
    assert world.client.cookies.get("inqtrix_session") != session_a
    assert asyncio.run(world.sessions.get(session_a)) is None


def test_starting_twice_at_once_creates_one_user() -> None:
    world = World()

    async def both() -> None:
        async def one() -> None:
            await world.provider.start_session(_request_with(_as("user-a")))

        await asyncio.gather(one(), one())

    asyncio.run(both())

    assert len(world.all_users()) == 1


def _request_with(headers: dict[str, str]):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/session",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
            "query_string": b"",
            "client": ("127.0.0.1", 50_000),
        }
    )


# ------------------------------------------------------------------ #
# Every request: assertion and session must name the same user
# ------------------------------------------------------------------ #


def test_a_request_with_cookie_and_matching_assertion_passes() -> None:
    world = World()
    world.start("user-a")
    (user,) = world.all_users()

    response = world.client.get("/v1/protected", headers=_as("user-a"))

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user.user_id), "kind": "oidc_session"}


def test_a_cookie_without_an_assertion_counts_for_nothing() -> None:
    world = World()
    world.start("user-a")

    response = world.client.get("/v1/protected")

    assert response.status_code == 401
    assert _error_type(response) == "assertion_missing"


def test_a_cookie_of_one_user_with_the_assertion_of_another_ends_the_session() -> None:
    world = World()
    world.start("user-a")
    session_a = world.client.cookies.get("inqtrix_session")

    response = world.client.get("/v1/protected", headers=_as("user-b"))

    assert response.status_code == 401
    assert _error_type(response) == "principal_mismatch"
    assert asyncio.run(world.sessions.get(session_a)) is None


def test_after_a_switch_the_old_tab_is_told_to_reload() -> None:
    world = World()
    world.start("user-a")
    (user_a,) = world.all_users()
    world.start("user-b")

    response = world.client.get(
        "/v1/protected",
        headers={**_as("user-b"), EXPECTED_USER_ID_HEADER: str(user_a.user_id)},
    )

    assert response.status_code == 409
    assert _error_type(response) == "principal_changed"


def test_a_disabled_user_with_a_valid_assertion_loses_the_session() -> None:
    world = World()
    world.start("user-a")
    session = world.client.cookies.get("inqtrix_session")
    (user,) = world.all_users()
    asyncio.run(world.users.set_disabled(tenant_id="default", user_id=user.user_id, disabled_at=time.time()))

    protected = world.client.get("/v1/protected", headers=_as("user-a"))
    start = world.start("user-a")

    assert protected.status_code == 401
    assert asyncio.run(world.sessions.get(session)) is None
    assert start.status_code == 403
    assert _error_type(start) == "account_disabled"


def test_unsafe_methods_still_need_the_csrf_token() -> None:
    world = World()
    world.start("user-a")

    without = world.client.post("/v1/protected", headers=_as("user-a"))
    with_token = world.client.post("/v1/protected", headers={**_as("user-a"), **world.csrf()})

    assert without.status_code == 403
    assert _error_type(without) == "csrf_error"
    assert with_token.status_code == 200


def _call_directly(world: World, route: str, headers: dict[str, str]):
    """An unsafe request to a route that calls the principal dependency itself."""
    if route == "tokens":
        return world.client.post("/api/auth/tokens", json={"name": "automation"}, headers=headers)
    if route == "logout":
        return world.client.post("/api/auth/logout", headers=headers)
    target = next(user for user in world.all_users() if user.subject == "user-b")
    return world.client.post(f"/v1/admin/users/{target.user_id}:disable", headers=headers)


@pytest.mark.parametrize("route", ["tokens", "logout", "admin"])
@pytest.mark.parametrize("case", ["no_assertion", "no_csrf", "other_assertion"])
def test_routes_that_call_the_dependency_directly_check_assertion_and_csrf(
    route: str, case: str
) -> None:
    """Token management, logout and the admin guard call the principal
    dependency themselves; that call must check like the injected one."""
    world = World(first_user_owner=True)
    world.start("user-a")
    world.client.cookies.clear()
    world.start("user-b")
    world.client.cookies.clear()
    world.start("user-a")
    headers = {
        "no_assertion": world.csrf(),
        "no_csrf": _as("user-a"),
        "other_assertion": {**_as("user-b"), **world.csrf()},
    }[case]

    response = _call_directly(world, route, headers)

    assert response.status_code == (403 if case == "no_csrf" else 401)
    if route == "tokens":
        world.start("user-a")
        listed = world.client.get("/api/auth/tokens", headers={**_as("user-a"), **world.csrf()})
        assert listed.status_code == 200 and "automation" not in listed.text
    elif route == "logout" and case != "other_assertion":
        # A mismatched assertion ends the session by itself; otherwise it lives on.
        assert world.client.get("/v1/protected", headers=_as("user-a")).status_code == 200
    elif route == "admin":
        assert all(user.disabled_at is None for user in world.all_users())


def test_the_live_resolver_needs_no_assertion() -> None:
    # Streams re-check the session before each frame with the frozen headers
    # of the original request; short-lived gateway tokens must not end them.
    world = World()
    world.start("user-a")
    (user,) = world.all_users()
    session_id = world.client.cookies.get("inqtrix_session")
    request = _request_with({"cookie": f"inqtrix_session={session_id}"})

    principal = asyncio.run(resolve_live_principal(world.dependency, request))

    assert principal.user_id == user.user_id


def test_a_bearer_token_is_a_personal_access_token_never_an_assertion() -> None:
    # A wrong token must fail even beside a valid session and assertion.
    world = World()
    world.start("user-a")

    response = world.client.get(
        "/v1/protected", headers={**_as("user-a"), "Authorization": "Bearer not-a-token"}
    )

    assert response.status_code == 401
    assert world.client.get("/v1/protected", headers=_as("user-a")).status_code == 200


def test_a_session_of_another_issuer_never_matches_the_same_subject() -> None:
    # A local account named like the gateway subject is another person.
    from inqtrix.auth.sessions import AuthSession, new_session_id

    world = World()
    local = asyncio.run(
        world.users.record_login(
            tenant_id="default",
            issuer="local",
            subject="user-a",
            email="user-a@example.com",
            email_verified=True,
            display_name="Local A",
        )
    )
    now = time.time()
    session = AuthSession(
        id=new_session_id(),
        user_id=local.user_id,
        issuer="local",
        subject="user-a",
        email="user-a@example.com",
        display_name="Local A",
        groups=(),
        csrf_random="ab" * 16,
        created_at=now,
        expires_at=now + 3600,
    )
    asyncio.run(world.sessions.create(session))
    world.client.cookies.set("inqtrix_session", session.id)

    response = world.client.get("/v1/protected", headers=_as("user-a"))

    assert response.status_code == 401
    assert _error_type(response) == "principal_mismatch"


# ------------------------------------------------------------------ #
# Logout and the auth surface
# ------------------------------------------------------------------ #


def test_logout_ends_the_session_and_names_the_gateway_logout() -> None:
    world = World()
    world.start("user-a")
    session = world.client.cookies.get("inqtrix_session")

    response = world.client.post("/api/auth/logout", headers={**_as("user-a"), **world.csrf()})

    assert response.status_code == 200
    assert response.json() == {"logged_out": True, "redirect_url": LOGOUT_URL}
    assert asyncio.run(world.sessions.get(session)) is None


def test_without_a_logout_url_the_logout_names_none() -> None:
    world = World(logout_url="")
    world.start("user-a")

    response = world.client.post("/api/auth/logout", headers={**_as("user-a"), **world.csrf()})

    assert response.json() == {"logged_out": True, "redirect_url": None}


def test_the_token_routes_are_mounted_in_the_gateway_branch() -> None:
    world = World()
    world.start("user-a")

    response = world.client.get("/api/auth/tokens", headers=_as("user-a"))

    assert response.status_code == 200


def test_a_token_of_a_local_account_is_refused_at_the_gateway() -> None:
    world = World()
    world.start("user-a")
    gateway_user = asyncio.run(
        world.users.find_user(tenant_id="default", issuer=ISSUER, subject="user-a")
    )
    local = asyncio.run(
        world.users.record_login(
            tenant_id="default",
            issuer="local",
            subject="ci-bot",
            email="ci@example.com",
            email_verified=True,
            display_name="CI",
        )
    )
    own, foreign = (
        asyncio.run(
            world.provider.pat_service.create_token(
                tenant_id="default", owner_user_id=owner, name="automation"
            )
        )
        for owner in (gateway_user.user_id, local.user_id)
    )
    world.client.cookies.clear()

    def bearer(minted) -> int:
        return world.client.get(
            "/v1/protected", headers={"Authorization": f"Bearer {minted.plaintext}"}
        ).status_code

    assert bearer(own) == 200
    assert bearer(foreign) == 401


def test_a_token_beyond_the_cap_is_refused_at_creation() -> None:
    world = World(pat_max_ttl_days=90)
    world.start("user-a")
    headers = {**_as("user-a"), **world.csrf()}

    beyond = world.client.post("/api/auth/tokens", json={"name": "ci", "expires_in_days": 91}, headers=headers)
    capped = world.client.post("/api/auth/tokens", json={"name": "ci"}, headers=headers)

    assert beyond.status_code == 400
    assert _error_type(beyond) == "pat_ttl_exceeds_cap"
    assert capped.status_code == 201
    assert capped.json()["expires_at"] == pytest.approx(capped.json()["created_at"] + 90 * 86_400, abs=5)


def test_the_auth_config_offers_the_gateway_as_login_method() -> None:
    world = World()
    app = FastAPI()
    app.include_router(auth_config.build_router(type("Container", (), {"auth_provider": world.provider})()))

    body = TestClient(app).get("/api/auth/config").json()

    assert body["auth_mode"] == "gateway"
    assert body["login_methods"] == [{"kind": "gateway", "label": "Gateway"}]
    assert body["supports_logout"] is True
    assert body["registration"]["invitations_accepted_by_login"] is True


@pytest.mark.parametrize("assertion", ["jwt", "header", "resolver"])
def test_the_auth_config_tells_the_edge_which_assertion_the_gateway_uses(assertion: str) -> None:
    if assertion == "jwt":
        world = World()
    elif assertion == "header":
        world = World(assertion="header", resolver=_header_resolver(), issuer_key="gateway:corp-sso")
    else:
        world = World(assertion="resolver", resolver=_guarded(), issuer_key="gateway:corp-sso")
    app = FastAPI()
    app.include_router(auth_config.build_router(type("Container", (), {"auth_provider": world.provider})()))

    body = TestClient(app).get("/api/auth/config").json()

    assert body["gateway_assertion"] == assertion
    # The public route names neither the class nor the headers of a resolver.
    assert "StaticResolver" not in str(body) and "x-static-user" not in str(body).lower()


def test_closing_the_provider_releases_the_key_source() -> None:
    class _Keys:
        closed = False

        async def keys_for(self, token: str) -> KeySet:
            return KeySet([KEY])

        async def aclose(self) -> None:
            self.closed = True

    keys = _Keys()
    resolver = JwtAssertionResolver(
        header_name=ASSERTION,
        issuer=ISSUER,
        audience="inqtrix",
        user_scope="inqtrix.user",
        algorithms=["RS256"],
        leeway_seconds=30,
        subject_claim="sub",
        claim_mapping=ClaimMappingConfig(),
        keys=keys,
    )

    asyncio.run(World(resolver=resolver).provider.aclose())

    assert keys.closed is True


@pytest.mark.parametrize("assertion", ["header", "resolver"])
def test_only_a_signed_token_accepts_invitations_by_login(assertion: str) -> None:
    # Neither identity headers nor a resolver of its own prove a confirmed email.
    resolver = _header_resolver() if assertion == "header" else _guarded()
    world = World(assertion=assertion, resolver=resolver, issuer_key="gateway:corp-sso")

    assert world.provider.invitations_accepted_by_login is False


# ------------------------------------------------------------------ #
# Admission, invitations and the admin grant
# ------------------------------------------------------------------ #


def test_the_group_gate_admits_only_members() -> None:
    world = World(mapping=ClaimMappingConfig(allowed_groups=frozenset({"inqtrix-users"})))

    denied = world.start("user-a")
    admitted = world.start("user-b", groups=["inqtrix-users"])

    assert denied.status_code == 403
    assert _error_type(denied) == "admission_denied"
    assert admitted.status_code == 200
    assert [user.subject for user in world.all_users()] == ["user-b"]


@pytest.mark.parametrize(
    ("overrides", "status", "outcome"),
    [
        ({"email_verified": False}, 403, "email_unverified"),
        ({"email": None, "email_verified": None}, 403, "email_unverified"),
        ({"email": "user-a@elsewhere.example"}, 403, "admission_denied"),
        ({}, 200, None),
    ],
    ids=["unconfirmed", "no-email", "foreign-domain", "confirmed"],
)
def test_the_domain_gate_reads_only_a_confirmed_email(overrides, status, outcome) -> None:
    world = World(mapping=ClaimMappingConfig(allowed_domains=frozenset({"example.com"})))

    response = world.start("user-a", **overrides)

    assert response.status_code == status
    if outcome is not None:
        assert _error_type(response) == outcome


def test_the_group_gate_answers_before_the_email_confirmation() -> None:
    world = World(
        mapping=ClaimMappingConfig(
            allowed_groups=frozenset({"inqtrix-users"}), allowed_domains=frozenset({"example.com"})
        )
    )

    response = world.start("user-a", email_verified=False)

    assert _error_type(response) == "admission_denied"


def test_an_existing_user_without_a_confirmed_email_is_stopped_at_the_next_session_start() -> None:
    world = World(mapping=ClaimMappingConfig(allowed_domains=frozenset({"example.com"})))
    assert world.start("user-a").status_code == 200
    world.client.cookies.clear()

    response = world.start("user-a", email_verified=False)

    assert _error_type(response) == "email_unverified"


def test_an_email_from_another_claim_counts_as_unconfirmed() -> None:
    world = World(
        mapping=ClaimMappingConfig(email_claim="upn", allowed_domains=frozenset({"example.com"}))
    )

    response = world.start("user-a", upn="user-a@example.com")

    assert _error_type(response) == "email_unverified"


def test_email_unverified_is_a_403_of_its_own_that_no_resolver_raises() -> None:
    assert OUTCOME_STATUS["email_unverified"] == 403
    assert GatewayRejected("email_unverified").message == CONFIRMED_EMAIL_REQUIRED
    with pytest.raises(ValueError):
        GatewayAssertionError("email_unverified")


def test_invite_only_registration_admits_an_invited_verified_email() -> None:
    identity = MemoryIdentityStore()
    identity.add_workspace("ws-team")
    world = World(registration="invite", identity=identity)
    assert world.invitations is not None
    asyncio.run(
        world.invitations.create(
            tenant_id="default",
            workspace_id="ws-team",
            email="user-a@example.com",
            role=WorkspaceRole.EDITOR,
            invited_by_user_id=uuid.uuid4(),
            expires_at=time.time() + 3600,
        )
    )

    stranger = world.start("user-b")
    invited = world.start("user-a")

    assert stranger.status_code == 403
    assert _error_type(stranger) == "registration_denied"
    assert invited.status_code == 200


@pytest.mark.parametrize("invited", [True, False], ids=["invited", "not-invited"])
def test_invite_mode_refuses_an_unconfirmed_first_login_alike(invited: bool) -> None:
    identity = MemoryIdentityStore()
    identity.add_workspace("ws-team")
    world = World(registration="invite", identity=identity)
    assert world.invitations is not None
    if invited:
        asyncio.run(
            world.invitations.create(
                tenant_id="default",
                workspace_id="ws-team",
                email="user-a@example.com",
                role=WorkspaceRole.EDITOR,
                invited_by_user_id=uuid.uuid4(),
                expires_at=time.time() + 3600,
            )
        )

    response = world.start("user-a", email_verified=False)

    assert response.status_code == 403
    assert _error_type(response) == "email_unverified"
    assert [user.subject for user in world.all_users()] == []


def test_admin_comes_from_token_groups_and_is_never_revoked_by_a_login() -> None:
    world = World(mapping=ClaimMappingConfig(admin_groups=frozenset({"inqtrix-admins"})))

    granted = world.start("user-a", groups=["inqtrix-admins"])
    # A new sign-in, not the running session: the login itself must not revoke.
    world.client.cookies.clear()
    later = world.start("user-a", groups=["staff"])

    assert granted.json()["user"]["role"] == "admin"
    assert later.json()["user"]["role"] == "admin"


def test_the_first_user_is_no_owner_by_default() -> None:
    world = World()

    assert world.start("user-a").json()["user"]["role"] == "user"


def test_with_the_first_user_owner_switch_only_the_first_user_becomes_admin() -> None:
    world = World(first_user_owner=True)

    first = world.start("user-a")
    world.client.cookies.clear()
    second = world.start("user-b")

    assert first.json()["user"]["role"] == "admin"
    assert second.json()["user"]["role"] == "user"


def test_the_same_email_under_another_subject_is_another_user() -> None:
    world = World()

    world.start("user-a", email="shared@example.com")
    world.client.cookies.clear()
    world.start("user-b", email="shared@example.com")

    assert sorted(user.subject for user in world.all_users()) == ["user-a", "user-b"]


def test_a_tenant_in_the_assertion_is_ignored() -> None:
    world = World()

    world.start("user-a", tenant="other-tenant", tid="other-tenant")

    (user,) = world.all_users()
    assert user.tenant_id == "default"


@pytest.mark.parametrize("assertion", ["header", "resolver"])
def test_neither_headers_nor_a_resolver_of_its_own_grant_admin_from_groups(assertion: str) -> None:
    # The proof header proves the hop, not the values of the other headers.
    mapping = ClaimMappingConfig(admin_groups=frozenset({"inqtrix-admins"}))
    if assertion == "header":
        resolver = _header_resolver()
        headers = {"X-Inqtrix-Subject": "user-h", "X-Inqtrix-Groups": "inqtrix-admins", **_PROOF}
    else:
        resolver = _guarded(_StaticResolver(groups=("inqtrix-admins",)))
        headers = {"X-Static-User": "user-h", **_PROOF}
    world = World(
        mapping=mapping,
        assertion=assertion,
        issuer_key="gateway:corp-sso",
        resolver=resolver,
        peer=_PEER,
    )

    response = world.client.get("/api/auth/session", headers=headers)

    assert response.json()["user"]["role"] == "user"


class _StaticResolver:
    """Asserts one fixed identity for every request it is asked about."""

    header_names = ("X-Static-User",)

    def __init__(
        self,
        *,
        subject: str = "user-h",
        groups: tuple[str, ...] = (),
        email_verified: bool = False,
    ) -> None:
        self._subject = subject
        self._groups = groups
        self._email_verified = email_verified

    async def resolve(self, request):
        from inqtrix.auth.gateway import VerifiedIdentity

        return VerifiedIdentity(
            subject=self._subject,
            email=f"{self._subject}@example.com",
            email_verified=self._email_verified,
            display_name="Header User",
            groups=self._groups,
        )


_PEER = "10.1.2.3"
_PROOF = {"X-Inqtrix-Proof": "x" * 32}


def _guarded(resolver: object | None = None) -> CustomResolverGuard:
    """A resolver of its own behind the proof, as the builder puts it."""
    return CustomResolverGuard(
        resolver or _StaticResolver(),
        trusted_networks=[ipaddress.ip_network("10.0.0.0/8")],
        proof_header="X-Inqtrix-Proof",
        shared_secret="x" * 32,
    )


def _header_resolver() -> TrustedHeaderResolver:
    return TrustedHeaderResolver(
        subject_header="X-Inqtrix-Subject",
        email_header="X-Inqtrix-Email",
        name_header="",
        groups_header="X-Inqtrix-Groups",
        proof_header="X-Inqtrix-Proof",
        shared_secret="x" * 32,
        trusted_networks=[ipaddress.ip_network("10.0.0.0/8")],
        header_decoding="none",
        claim_mapping=ClaimMappingConfig(),
    )


# ------------------------------------------------------------------ #
# Construction
# ------------------------------------------------------------------ #


def test_the_lifecycle_must_count_admins_of_the_source() -> None:
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = UserLifecycleService(
        users=users,
        sessions=sessions,
        transaction=MemoryUserLifecycleTransaction(users=users, sessions=sessions),
    )

    with pytest.raises(ValueError, match="source"):
        GatewayAuthProvider(
            source=IdentitySource(issuer_key=ISSUER, resolver=_guarded()),
            assertion="resolver",
            claim_mapping=ClaimMappingConfig(),
            sessions=sessions,
            flows=MemoryFlowStore(),
            users=users,
            session_secret="s" * 32,
            session_max_age_seconds=3600,
            lifecycle=lifecycle,
        )


def test_the_token_verifier_must_accept_only_the_source() -> None:
    users = MemoryUserDirectory()

    with pytest.raises(ValueError, match="token verifier"):
        GatewayAuthProvider(
            source=IdentitySource(issuer_key=ISSUER, resolver=_guarded()),
            assertion="resolver",
            claim_mapping=ClaimMappingConfig(),
            sessions=MemorySessionStore(),
            flows=MemoryFlowStore(),
            users=users,
            session_secret="s" * 32,
            session_max_age_seconds=3600,
            pats=PatVerifier(store=MemoryPatStore(), pepper="p" * 32, user_lookup=users),
        )


class _ReplacedJwtResolver(JwtAssertionResolver):
    """The built-in jwt resolver with a ``resolve`` someone else wrote."""

    async def resolve(self, request):
        from inqtrix.auth.gateway import VerifiedIdentity

        return VerifiedIdentity(subject="user-x", groups=("inqtrix-admins",))


@pytest.mark.parametrize("kind", ["own_resolver", "replaced_jwt_resolver"])
def test_the_jwt_assertion_needs_the_built_in_jwt_resolver(kind: str) -> None:
    """Admin from groups and roles follows the ``jwt`` label, so only the
    resolver that checks the signed token may carry it."""
    mapping = ClaimMappingConfig(admin_groups=frozenset({"inqtrix-admins"}))
    if kind == "own_resolver":
        resolver = _StaticResolver(groups=("inqtrix-admins",))
    else:
        resolver = _ReplacedJwtResolver(
            header_name=ASSERTION,
            issuer=ISSUER,
            audience="inqtrix",
            user_scope="inqtrix.user",
            algorithms=["RS256"],
            leeway_seconds=30,
            subject_claim="sub",
            claim_mapping=mapping,
            keys=StaticKeys(KeySet([KEY])),
        )

    with pytest.raises(ValueError, match="JwtAssertionResolver"):
        World(mapping=mapping, assertion="jwt", resolver=resolver)


class _PrincipalResolver:
    """Returns a principal instead of the identity the gateway asserted."""

    header_names = ("X-Static-User",)

    async def resolve(self, request):
        return Principal(user_id=uuid.uuid4(), kind="oidc_session", role="admin")


class _FailingResolver:
    """Its own code fails on every request with the error it was given."""

    header_names = ("X-Static-User",)

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def resolve(self, request):
        raise self._error


_RESOLVER_CANARY = "resolver-canary-9QX2"


@pytest.mark.parametrize(
    "error",
    [
        RecursionError("maximum recursion depth exceeded while decoding a JSON array"),
        KeyError(_RESOLVER_CANARY),
        RuntimeError(f"the identity backend answered {_RESOLVER_CANARY}"),
    ],
    ids=["recursion", "key", "runtime"],
)
def test_a_failing_resolver_of_its_own_answers_503_resolver_failed(error, caplog) -> None:
    # The code of a resolver of its own is not Inqtrix's: whatever it raises
    # besides GatewayAssertionError ends as one class, never as a server
    # error whose traceback could carry header values into the log.
    world = World(
        assertion="resolver",
        resolver=_guarded(_FailingResolver(error)),
        issuer_key="gateway:corp-sso",
        peer=_PEER,
    )

    with caplog.at_level(logging.WARNING):
        response = world.client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "resolver_failed"
    assert not world.all_users()
    assert "resolver_failed" in caplog.text
    assert type(error).__name__ in caplog.text
    assert _RESOLVER_CANARY not in caplog.text
    assert "Traceback" not in caplog.text


def test_groups_of_the_wrong_type_from_a_resolver_of_its_own_answer_400_malformed() -> None:
    world = World(
        assertion="resolver",
        resolver=_guarded(_StaticResolver(groups=None)),
        issuer_key="gateway:corp-sso",
        peer=_PEER,
    )

    response = world.client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "malformed"
    assert not world.all_users()


def test_a_resolver_must_return_a_verified_identity() -> None:
    world = World(
        assertion="resolver",
        resolver=_guarded(_PrincipalResolver()),
        issuer_key="gateway:corp-sso",
        peer=_PEER,
    )

    with pytest.raises(TypeError, match="VerifiedIdentity"):
        world.client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    assert not world.all_users()


@pytest.mark.parametrize(
    ("assertion", "make"),
    [
        ("header", lambda: _StaticResolver()),
        ("header", lambda: _guarded()),
        ("resolver", lambda: _StaticResolver()),
        ("resolver", lambda: _header_resolver()),
        ("plugin", lambda: _StaticResolver()),
        ("plugin", lambda: _guarded()),
    ],
    ids=[
        "header_with_own",
        "header_with_guard",
        "resolver_unguarded",
        "resolver_with_header",
        "unknown_with_own",
        "unknown_with_guard",
    ],
)
def test_each_assertion_needs_exactly_its_resolver(assertion: str, make) -> None:
    """The label decides admin, invitations and the edge's duties, so it must
    match the resolver that checked the request."""
    with pytest.raises(ValueError, match="needs exactly the resolver"):
        World(assertion=assertion, resolver=make(), issuer_key="gateway:corp-sso")


def test_a_resolver_of_its_own_never_confirms_an_email() -> None:
    world = World(
        assertion="resolver",
        resolver=_guarded(_StaticResolver(email_verified=True)),
        issuer_key="gateway:corp-sso",
        peer=_PEER,
    )

    started = world.client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    (user,) = world.all_users()
    assert started.status_code == 200
    assert user.email_verified is False


def test_an_unknown_email_confirmation_of_a_resolver_counts_as_unconfirmed() -> None:
    # Postgres keeps email_verified NOT NULL; None must never reach the sign-in.
    world = World(
        assertion="resolver",
        resolver=_guarded(_StaticResolver(email_verified=None)),
        issuer_key="gateway:corp-sso",
        peer=_PEER,
    )

    started = world.client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    (user,) = world.all_users()
    assert started.status_code == 200
    assert user.email_verified is False


def test_a_resolver_of_its_own_never_passes_the_domain_gate() -> None:
    world = World(
        mapping=ClaimMappingConfig(allowed_domains=frozenset({"example.com"})),
        assertion="resolver",
        resolver=_guarded(_StaticResolver(email_verified=True)),
        issuer_key="gateway:corp-sso",
        peer=_PEER,
    )

    response = world.client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    assert response.status_code == 403
    assert _error_type(response) == "email_unverified"
    assert not world.all_users()


_GATEWAY_JWT = {
    "mode": "gateway",
    "session_secret": "s" * 32,
    "pat_pepper": "p" * 32,
    "pseudonym_pepper": "q" * 32,
    "gateway_assertion": "jwt",
    "gateway_jwt_header": ASSERTION,
    "gateway_jwt_issuer": ISSUER,
    "gateway_jwt_audience": "inqtrix",
    "gateway_jwt_user_scope": "inqtrix.user",
    "gateway_jwks_url": "https://idp.example/realms/corp/protocol/openid-connect/certs",
    "gateway_pat_max_ttl_days": 90,
    "gateway_logout_url": LOGOUT_URL,
}

_GATEWAY_HEADER = {
    "mode": "gateway",
    "session_secret": "s" * 32,
    "pat_pepper": "p" * 32,
    "pseudonym_pepper": "q" * 32,
    "gateway_assertion": "header",
    "gateway_issuer_key": "gateway:corp-sso",
    "gateway_subject_header": "X-Inqtrix-Subject",
    "gateway_email_header": "X-Inqtrix-Email",
    "gateway_proof_header": "X-Inqtrix-Proof",
    "gateway_shared_secret": "x" * 32,
    "gateway_trusted_proxies": "10.0.0.0/8",
    "gateway_pat_max_ttl_days": 90,
    "gateway_logout_url": LOGOUT_URL,
    "gateway_first_user_owner": True,
}


def _settings(values: dict[str, object], backend: str = "postgres") -> Settings:
    settings = Settings(auth=AuthSettings(**values))
    settings.storage.backend = backend
    # build_engine connects on first use only; nothing here touches a database.
    settings.storage.database_url = "postgresql+asyncpg://inqtrix:unused@127.0.0.1:1/inqtrix"
    return settings


@pytest.mark.parametrize(
    ("values", "issuer_key"),
    [(_GATEWAY_JWT, ISSUER), (_GATEWAY_HEADER, "gateway:corp-sso")],
    ids=["jwt", "header"],
)
def test_the_built_provider_counts_admins_of_its_identity_source(values, issuer_key) -> None:
    provider = build_gateway_provider(_settings(values))

    assert isinstance(provider, GatewayAuthProvider)
    assert provider.mode == "gateway"
    assert provider.source.issuer_key == issuer_key
    assert provider.lifecycle.source_issuer == issuer_key
    assert provider.invitations is not None
    assert provider.pat_service is not None
    assert provider.first_user_owner is bool(values.get("gateway_first_user_owner", False))
    assert provider.logout_url == LOGOUT_URL


@pytest.mark.parametrize(
    ("values", "issuer_key"),
    [(_GATEWAY_JWT, ISSUER), (_GATEWAY_HEADER, "gateway:corp-sso")],
    ids=["jwt", "header"],
)
def test_the_built_provider_accepts_tokens_of_its_identity_source_only(
    values, issuer_key
) -> None:
    provider = build_gateway_provider(_settings(values))

    assert provider.pats.source_issuer == issuer_key


def test_the_builder_sets_the_token_bounds() -> None:
    provider = build_gateway_provider(_settings({**_GATEWAY_JWT, "gateway_pat_inactive_days": 30}))

    assert provider.pats.max_ttl_seconds == 90 * 86_400.0
    assert provider.pats.owner_inactive_seconds == 30 * 86_400.0
    assert provider.pat_service.max_ttl_days == 90


def test_the_builder_refuses_the_memory_backend() -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_STORAGE_BACKEND=postgres"):
        build_gateway_provider(_settings(_GATEWAY_JWT, backend="memory"))


def test_the_builder_warns_without_logout_url_and_first_admin_path(caplog) -> None:
    values = {
        key: value
        for key, value in _GATEWAY_JWT.items()
        if key != "gateway_logout_url"
    }

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.gateway"):
        build_gateway_provider(_settings(values))

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "INQTRIX_GATEWAY_LOGOUT_URL" in messages
    assert "INQTRIX_GATEWAY_FIRST_USER_OWNER" in messages


def test_the_builder_stays_quiet_with_logout_url_and_admin_path(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.gateway"):
        build_gateway_provider(_settings({**_GATEWAY_JWT, "oidc_admin_groups": "inqtrix-admins"}))
        build_gateway_provider(_settings(_GATEWAY_HEADER))

    assert [record.getMessage() for record in caplog.records if record.name == "inqtrix.auth.gateway"] == []


@pytest.mark.parametrize("switch", [True, False], ids=["set", "unset"])
def test_the_email_verified_switch_has_no_effect_in_gateway_mode(caplog, switch: bool) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.gateway"):
        build_gateway_provider(_settings({**_GATEWAY_JWT, "oidc_skip_email_verified": switch}))

    warned = [
        record.getMessage()
        for record in caplog.records
        if "INQTRIX_OIDC_SKIP_EMAIL_VERIFIED has no effect in gateway mode" in record.getMessage()
    ]
    assert len(warned) == (1 if switch else 0)


def test_the_start_line_names_the_assertion_and_the_peer_list(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix.auth.gateway"):
        build_gateway_provider(_settings(_GATEWAY_HEADER))

    lines = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert any("assertion=header" in line and "trusted_proxies=1" in line for line in lines)
    assert all("x" * 32 not in line for line in lines)


_GATEWAY_RESOLVER = {
    "mode": "gateway",
    "session_secret": "s" * 32,
    "pat_pepper": "p" * 32,
    "pseudonym_pepper": "q" * 32,
    "gateway_issuer_key": "gateway:corp-sso",
    "gateway_proof_header": "X-Inqtrix-Proof",
    "gateway_shared_secret": "x" * 32,
    "gateway_trusted_proxies": "10.0.0.0/8",
    "gateway_resolver_headers": "X-Static-User",
    "gateway_pat_max_ttl_days": 90,
    "gateway_logout_url": LOGOUT_URL,
    "gateway_first_user_owner": True,
}


def test_the_builder_puts_a_resolver_of_its_own_behind_the_proof(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix.auth.gateway"):
        provider = build_gateway_provider(_settings(_GATEWAY_RESOLVER), resolver=_StaticResolver())

    assert provider.assertion == "resolver"
    assert type(provider.source.resolver) is CustomResolverGuard
    assert provider.source.issuer_key == "gateway:corp-sso"
    lines = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert any(
        "assertion=resolver" in line and "resolver=tests.test_gateway_session._StaticResolver" in line
        for line in lines
    )
    assert all("x" * 32 not in line for line in lines)


@pytest.mark.parametrize(
    ("change", "variable"),
    [
        ({"gateway_assertion": "jwt"}, r"set are: build_gateway_provider\(resolver=\.\.\.\), INQTRIX_GATEWAY_ASSERTION"),
        ({"gateway_issuer_key": ""}, "INQTRIX_GATEWAY_ISSUER_KEY"),
        ({"gateway_trusted_proxies": ""}, "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
        ({"gateway_proof_header": ""}, "INQTRIX_GATEWAY_PROOF_HEADER"),
        ({"gateway_shared_secret": "short"}, "INQTRIX_GATEWAY_SHARED_SECRET"),
        ({"oidc_admin_groups": "inqtrix-admins"}, "INQTRIX_OIDC_ADMIN_GROUPS"),
        ({"oidc_admin_roles": "admin"}, "INQTRIX_OIDC_ADMIN_ROLES"),
        ({"registration": "invite"}, "INQTRIX_REGISTRATION"),
        ({"oidc_allowed_domains": "example.com"}, "INQTRIX_OIDC_ALLOWED_DOMAINS"),
        ({"gateway_resolver_headers": ""}, "INQTRIX_GATEWAY_RESOLVER_HEADERS"),
        ({"gateway_resolver_headers": "X-Other"}, "INQTRIX_GATEWAY_RESOLVER_HEADERS"),
        ({"gateway_resolver_headers": "X-Static-User,Cookie"}, "INQTRIX_GATEWAY_RESOLVER_HEADERS"),
    ],
    ids=[
        "assertion_too",
        "no_issuer_key",
        "no_peers",
        "no_proof_header",
        "short_secret",
        "admin_groups",
        "admin_roles",
        "invite",
        "domain_gate",
        "no_header_list",
        "header_not_listed",
        "cookie_listed",
    ],
)
def test_the_builder_refuses_unsafe_settings_for_a_resolver_of_its_own(change, variable) -> None:
    with pytest.raises(RuntimeError, match=variable):
        build_gateway_provider(_settings({**_GATEWAY_RESOLVER, **change}), resolver=_StaticResolver())


def _peer_request(headers: list[tuple[str, str]], peer: str):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/session",
            "raw_path": b"/api/auth/session",
            "query_string": b"",
            "headers": [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers],
            "client": (peer, 50_000),
            "server": ("api", 5100),
            "scheme": "http",
            "root_path": "",
        }
    )


@pytest.mark.parametrize(
    ("proof", "peer", "outcome"),
    [("x" * 32, "10.1.2.3", None), ("s" * 32, "10.1.2.3", "untrusted_source"), ("x" * 32, "192.0.2.7", "untrusted_source")],
    ids=["proven", "session_secret_as_proof", "foreign_peer"],
)
def test_the_builder_wires_the_proof_from_the_settings(proof: str, peer: str, outcome) -> None:
    provider = build_gateway_provider(_settings(_GATEWAY_RESOLVER), resolver=_StaticResolver())
    request = _peer_request([("X-Static-User", "user-h"), ("X-Inqtrix-Proof", proof)], peer)

    if outcome is None:
        identity = asyncio.run(provider.source.resolver.resolve(request))
        assert identity is not None and identity.subject == "user-h"
    else:
        with pytest.raises(GatewayAssertionError) as raised:
            asyncio.run(provider.source.resolver.resolve(request))
        assert raised.value.outcome == outcome


def test_the_builder_names_every_problem_of_a_resolver_of_its_own_at_once() -> None:
    class _Synchronous:
        header_names = ("X-Static-User", "X-Static-Mail")

        def resolve(self, request):
            return None

    with pytest.raises(RuntimeError) as raised:
        build_gateway_provider(
            _settings({**_GATEWAY_RESOLVER, "gateway_shared_secret": "short"}), resolver=_Synchronous()
        )

    message = str(raised.value)
    assert "INQTRIX_GATEWAY_SHARED_SECRET" in message
    assert "coroutine" in message
    assert "missing: x-static-mail" in message


def test_the_builder_needs_the_gateway_mode() -> None:
    # Otherwise the API would run the gateway provider while workers resolve another mode.
    with pytest.raises(RuntimeError, match="INQTRIX_AUTH_MODE=gateway"):
        build_gateway_provider(_settings({**_GATEWAY_JWT, "mode": "infer"}))


def test_the_builder_refuses_a_resolver_that_breaks_its_contract() -> None:
    class _Synchronous:
        header_names = ("X-Static-User",)

        def resolve(self, request):
            return None

    with pytest.raises(RuntimeError, match="coroutine"):
        build_gateway_provider(_settings(_GATEWAY_RESOLVER), resolver=_Synchronous())


@pytest.mark.parametrize(
    ("variable", "name"),
    [("INQTRIX_GATEWAY_SUBJECT_HEADER", "Cookie"), ("INQTRIX_GATEWAY_EMAIL_HEADER", "Origin")],
)
def test_the_identity_variables_beside_a_resolver_of_its_own_obey_the_name_rules(
    variable: str, name: str
) -> None:
    # The edge strips them from strangers as well; a stale Cookie would cost guests the session.
    field = variable.removeprefix("INQTRIX_").lower()

    with pytest.raises(RuntimeError, match=variable):
        build_gateway_provider(_settings({**_GATEWAY_RESOLVER, field: name}), resolver=_StaticResolver())


@pytest.mark.parametrize(
    ("values", "variable", "name"),
    [
        (_GATEWAY_JWT, "INQTRIX_GATEWAY_SUBJECT_HEADER", "Cookie"),
        (_GATEWAY_JWT, "INQTRIX_GATEWAY_GROUPS_HEADER", "X-Forwarded-Groups"),
        (_GATEWAY_HEADER, "INQTRIX_GATEWAY_JWT_HEADER", "Origin"),
    ],
    ids=["jwt-cookie", "jwt-forwarded", "header-origin"],
)
def test_identity_variables_the_assertion_does_not_read_obey_the_name_rules(
    values: dict[str, object], variable: str, name: str
) -> None:
    # The edge strips every set identity variable, whichever assertion reads it.
    field = variable.removeprefix("INQTRIX_").lower()

    with pytest.raises(RuntimeError, match=variable):
        build_gateway_provider(_settings({**values, field: name}))


def test_a_hop_count_other_than_one_warns_in_gateway_mode(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.gateway"):
        build_gateway_provider(_settings({**_GATEWAY_JWT, "trusted_proxy_hops": 2}))

    assert "INQTRIX_TRUSTED_PROXY_HOPS=2" in caplog.text


def test_a_key_url_without_tls_warns(caplog) -> None:
    url = "http://idp.example/realms/corp/protocol/openid-connect/certs"

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.gateway"):
        build_gateway_provider(_settings({**_GATEWAY_JWT, "gateway_jwks_url": url}))

    assert "INQTRIX_GATEWAY_JWKS_URL does not use https" in caplog.text


def test_one_hop_and_a_key_url_with_tls_start_without_these_warnings(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.gateway"):
        build_gateway_provider(_settings(_GATEWAY_JWT))

    assert "INQTRIX_TRUSTED_PROXY_HOPS" not in caplog.text
    assert "does not use https" not in caplog.text


def test_a_resolver_whose_header_names_can_be_read_once_starts() -> None:
    class _ReadOnce:
        header_names = iter(("X-Static-User",))

        async def resolve(self, request):
            return None

    provider = build_gateway_provider(_settings(_GATEWAY_RESOLVER), resolver=_ReadOnce())

    assert provider.source.resolver.header_names == ("x-static-user",)


def test_placeholder_secrets_of_the_gateway_mode_are_named_at_start() -> None:
    from inqtrix.server.app import _placeholder_secret_fields

    settings = _settings(
        {
            **_GATEWAY_HEADER,
            "gateway_shared_secret": "CHANGE_ME_" + "x" * 32,
            "scim_enabled": True,
            "scim_token": "CHANGE_ME_" + "y" * 32,
        }
    )

    assert {"INQTRIX_GATEWAY_SHARED_SECRET", "INQTRIX_SCIM_TOKEN"} <= set(
        _placeholder_secret_fields(settings)
    )


# ------------------------------------------------------------------ #
# Rejections in the log and the metrics
# ------------------------------------------------------------------ #


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_rejections_are_reported_once_per_interval_and_outcome(caplog) -> None:
    clock = _Clock()
    rejections = GatewayRejections(clock=clock)

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.gateway"):
        for _ in range(1_000):
            rejections.record("expired")
        rejections.record("malformed")
        clock.now += REJECTION_WARNING_INTERVAL_SECONDS + 1
        rejections.record("expired")

    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 3
    assert "outcome=expired" in lines[0] and "outcome=malformed" in lines[1]
    assert "999" in lines[2]


@pytest.fixture()
def registry():
    from prometheus_client import CollectorRegistry

    from inqtrix.observability.metrics_defs import (
        active_metrics,
        build_call_metrics,
        set_active_metrics,
    )

    registry = CollectorRegistry()
    previous = active_metrics()
    set_active_metrics(build_call_metrics(registry))
    try:
        yield registry
    finally:
        set_active_metrics(previous)


def _rejected(registry, outcome: str) -> float:
    return registry.get_sample_value("inqtrix_gateway_rejections_total", {"outcome": outcome}) or 0.0


def test_an_unconfirmed_email_is_counted_as_its_own_outcome(registry) -> None:
    world = World(mapping=ClaimMappingConfig(allowed_domains=frozenset({"example.com"})))

    world.start("user-a", email_verified=False)

    assert _rejected(registry, "email_unverified") == 1


def test_every_rejection_is_counted_per_outcome(registry) -> None:
    rejections = GatewayRejections(clock=_Clock())

    for _ in range(1_000):
        rejections.record("expired")

    assert _rejected(registry, "expired") == 1_000


def test_rejected_requests_are_counted_by_the_provider(registry) -> None:
    world = World()

    world.client.get("/v1/protected")
    world.client.get("/api/auth/session", headers={ASSERTION: "not-a-token"})
    world.start("user-a")
    world.client.get("/v1/protected", headers=_as("user-b"))

    assert _rejected(registry, "assertion_missing") == 1
    assert _rejected(registry, "malformed") == 1
    assert _rejected(registry, "principal_mismatch") == 1


def test_a_nested_assertion_header_is_a_counted_rejection_not_a_server_error(registry) -> None:
    world = World()
    header = base64.urlsafe_b64encode(b"[" * 10_000).rstrip(b"=").decode()

    response = world.client.get("/api/auth/session", headers={ASSERTION: f"{header}.e30.sig"})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "malformed"
    assert _rejected(registry, "malformed") == 1


def test_no_identity_value_reaches_the_log(caplog) -> None:
    marker = "zz-marker-7f3a"
    world = World()

    with caplog.at_level(logging.DEBUG):
        world.start(
            f"{marker}-sub",
            email=f"{marker}@example.com",
            preferred_username=f"{marker} Name",
            groups=[f"{marker}-group"],
        )
        world.client.get("/v1/protected", headers=_as(f"{marker}-other"))
        world.client.get(
            "/api/auth/session",
            headers=_as(f"{marker}-late", exp=int(time.time()) - 3600),
        )

    assert marker not in caplog.text


def test_no_header_identity_value_reaches_the_log(caplog) -> None:
    marker = "zz-marker-5c2e"
    world = World(
        assertion="header",
        resolver=_header_resolver(),
        issuer_key="gateway:corp-sso",
        peer=_PEER,
    )
    accepted = {
        "X-Inqtrix-Subject": f"{marker}-sub",
        "X-Inqtrix-Email": f"{marker}@example.com",
        "X-Inqtrix-Groups": f"{marker}-group",
        **_PROOF,
    }

    with caplog.at_level(logging.DEBUG):
        world.client.get("/api/auth/session", headers=accepted)
        world.client.get("/v1/protected", headers={**accepted, "X-Inqtrix-Subject": f"{marker}-other"})
        world.client.cookies.clear()
        world.client.get("/api/auth/session", headers={**accepted, "X-Inqtrix-Subject": f"{marker} spaced"})
        world.client.get(
            "/api/auth/session",
            headers={**accepted, "X-Inqtrix-Proof": f"{marker}-forged-proof-" + "z" * 32},
        )

    assert marker not in caplog.text


def test_no_raw_token_reaches_the_log(caplog) -> None:
    world = World()
    tokens = [
        _token("user-a"),
        _token("user-b", exp=int(time.time()) - 3600),
        _token("someone@example.com"),
        _token("user-c", scope="openid"),
    ]

    with caplog.at_level(logging.DEBUG):
        for token in tokens:
            world.client.cookies.clear()
            world.client.get("/api/auth/session", headers={ASSERTION: token})

    for token in tokens:
        assert token not in caplog.text
        assert token.rsplit(".", 1)[-1] not in caplog.text


def test_a_new_account_is_logged_as_provisioned_once(caplog) -> None:
    world = World()

    with caplog.at_level(logging.INFO, logger="inqtrix.auth.gateway"):
        world.start("user-a")
        # A second browser: the login provisions again, the account exists.
        world.client.cookies.clear()
        world.start("user-a")

    provisioned = [record for record in caplog.records if "provisioned" in record.getMessage()]
    assert len(provisioned) == 1
    assert provisioned[0].levelno == logging.INFO


# ------------------------------------------------------------------ #
# SCIM: provisioning is the only way an account is created
# ------------------------------------------------------------------ #


def _provision(world: World, subject: str):
    outcome = asyncio.run(
        world.transaction.provision_external_user(
            tenant_id="default",
            subject=subject,
            profile=ExternalProfile(user_name=f"{subject}@corp.example"),
        )
    )
    assert outcome.user is not None
    return outcome.user.user_id


def test_with_scim_an_unknown_identity_is_not_provisioned(registry) -> None:
    world = World(scim_enabled=True)

    response = world.start("user-a")

    assert response.status_code == 403
    assert _error_type(response) == "not_provisioned"
    assert [user.subject for user in world.all_users()] == []
    assert _rejected(registry, "not_provisioned") == 1


def test_with_scim_a_provisioned_account_signs_in() -> None:
    world = World(scim_enabled=True)
    user_id = _provision(world, "user-a")

    response = world.start("user-a")

    assert response.status_code == 200
    assert [user.user_id for user in world.all_users()] == [user_id]


def test_with_scim_the_admission_gates_answer_first() -> None:
    world = World(
        scim_enabled=True,
        mapping=ClaimMappingConfig(allowed_groups=frozenset({"inqtrix-users"})),
    )

    response = world.start("user-a")

    assert _error_type(response) == "admission_denied"


def test_with_scim_a_tombstone_answers_as_a_disabled_account() -> None:
    world = World(scim_enabled=True)
    user_id = _provision(world, "user-a")
    asyncio.run(world.transaction.deprovision(tenant_id="default", user_id=user_id))

    response = world.start("user-a")

    assert _error_type(response) == "account_disabled"


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_the_builder_passes_the_scim_switch(enabled: bool, caplog) -> None:
    values = {**_GATEWAY_JWT, "scim_enabled": enabled, "scim_token": "t" * 32}

    with caplog.at_level(logging.INFO, logger="inqtrix.auth.gateway"):
        provider = build_gateway_provider(_settings(values))

    assert provider.scim_enabled is enabled
    assert f"scim={'on' if enabled else 'off'}" in caplog.text


def test_the_scim_token_opens_no_api_route() -> None:
    world = World(scim_enabled=True)

    response = world.client.get(
        "/v1/protected", headers={"Authorization": "Bearer scim-token-" + "x" * 32}
    )

    assert response.status_code == 401


# ------------------------------------------------------------------ #
# A resolver of its own from INQTRIX_GATEWAY_RESOLVER
# ------------------------------------------------------------------ #

_PLUGIN = """
from inqtrix.auth.gateway import VerifiedIdentity

BUILT = []


class Resolver:
    header_names = ("X-Firma-User",)

    def __init__(self):
        BUILT.append(self)

    async def resolve(self, request):
        return VerifiedIdentity(subject=request.headers["x-firma-user"])
"""


def _plugin(tmp_path, monkeypatch, body: str) -> str:
    """Write a module into a folder on the path, the way an operator mounts one."""
    name = f"firma_resolver_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


def _setting(path: str, **extra: object) -> dict[str, object]:
    return {
        **_GATEWAY_RESOLVER,
        "gateway_resolver": path,
        "gateway_resolver_headers": "X-Firma-User",
        **extra,
    }


def test_the_setting_loads_a_resolver_of_its_own_once(tmp_path, monkeypatch, caplog) -> None:
    module = _plugin(tmp_path, monkeypatch, _PLUGIN)

    with caplog.at_level(logging.INFO, logger="inqtrix.auth.gateway"):
        provider = build_gateway_provider(_settings(_setting(f"{module}:Resolver")))

    assert provider.assertion == "resolver"
    assert type(provider.source.resolver) is CustomResolverGuard
    assert sys.modules[module].BUILT == [provider.source.resolver.resolver]
    lines = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert any("assertion=resolver" in line and f"resolver={module}:Resolver" in line for line in lines)


@pytest.mark.parametrize(
    ("body", "path", "shown", "hidden"),
    [
        (None, "firma_resolver", "module:Class", None),
        (None, "firma resolver:Resolver", "module:Class", None),
        (None, "firma_missing_plugin_xyz:Resolver", "firma_missing_plugin_xyz", None),
        ("import firma_missing_dependency_xyz\n", "{module}:Resolver", "firma_missing_dependency_xyz", None),
        ("raise ValueError('secret-at-import')\n", "{module}:Resolver", "ValueError", "secret-at-import"),
        ("X = 1\n", "{module}:Resolver", "no attribute", None),
        ("def Resolver():\n    return None\n", "{module}:Resolver", "not a class", None),
        (
            "class Resolver:\n    def __init__(self):\n        raise RuntimeError('secret-at-build')\n",
            "{module}:Resolver",
            "RuntimeError",
            "secret-at-build",
        ),
    ],
    ids=[
        "no_colon",
        "not_an_identifier",
        "module_missing",
        "dependency_missing",
        "import_fails",
        "attribute_missing",
        "not_a_class",
        "constructor_fails",
    ],
)
def test_the_setting_refuses_what_it_cannot_load(tmp_path, monkeypatch, body, path, shown, hidden) -> None:
    module = _plugin(tmp_path, monkeypatch, body) if body is not None else ""

    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_RESOLVER") as raised:
        build_gateway_provider(_settings(_setting(path.format(module=module))))

    assert shown in str(raised.value)
    if hidden is not None:
        # Neither the message nor a chained exception carries the original text.
        assert hidden not in str(raised.value)
        assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_the_setting_and_a_passed_resolver_exclude_each_other(tmp_path, monkeypatch) -> None:
    module = _plugin(tmp_path, monkeypatch, _PLUGIN)
    values = _setting(f"{module}:Resolver", gateway_resolver_headers="X-Firma-User,X-Static-User")

    with pytest.raises(RuntimeError, match="exactly one of") as raised:
        build_gateway_provider(_settings(values), resolver=_StaticResolver())

    assert "set are: build_gateway_provider(resolver=...), INQTRIX_GATEWAY_RESOLVER" in str(raised.value)
    assert sys.modules.get(module) is None


def test_the_setting_and_a_built_in_assertion_exclude_each_other(tmp_path, monkeypatch) -> None:
    module = _plugin(tmp_path, monkeypatch, _PLUGIN)

    with pytest.raises(RuntimeError, match="exactly one of") as raised:
        build_gateway_provider(_settings(_setting(f"{module}:Resolver", gateway_assertion="header")))

    assert "set are: INQTRIX_GATEWAY_RESOLVER, INQTRIX_GATEWAY_ASSERTION" in str(raised.value)
    # The conflict stops the start before the plugin's code runs.
    assert sys.modules.get(module) is None


def test_all_three_sources_are_named_at_once(tmp_path, monkeypatch) -> None:
    module = _plugin(tmp_path, monkeypatch, _PLUGIN)
    values = _setting(f"{module}:Resolver", gateway_assertion="jwt")

    with pytest.raises(RuntimeError) as raised:
        build_gateway_provider(_settings(values), resolver=_StaticResolver())

    assert (
        "set are: build_gateway_provider(resolver=...), INQTRIX_GATEWAY_RESOLVER, "
        "INQTRIX_GATEWAY_ASSERTION"
    ) in str(raised.value)


def test_settings_of_a_resolver_of_its_own_are_checked_before_it_loads(tmp_path, monkeypatch) -> None:
    module = _plugin(tmp_path, monkeypatch, _PLUGIN)

    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_SHARED_SECRET"):
        build_gateway_provider(_settings(_setting(f"{module}:Resolver", gateway_shared_secret="short")))

    assert sys.modules.get(module) is None


@pytest.mark.parametrize(
    "body",
    [
        "class Resolver:\n    @property\n    def header_names(self):\n        raise ValueError('secret-in-names')\n"
        "    async def resolve(self, request):\n        return None\n",
        "import sys\nsys.exit('secret-at-exit')\n",
        "import sys\nclass Resolver:\n    def __init__(self):\n        sys.exit('secret-at-exit')\n",
    ],
    ids=["header_names_raises", "module_exits", "constructor_exits"],
)
def test_plugin_code_failing_at_start_names_the_setting_but_not_its_text(tmp_path, monkeypatch, body) -> None:
    module = _plugin(tmp_path, monkeypatch, body)

    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_RESOLVER") as raised:
        build_gateway_provider(_settings(_setting(f"{module}:Resolver")))

    assert "secret-" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


# ------------------------------------------------------------------ #
# A resolver of its own behind the web gateway, in one process
# ------------------------------------------------------------------ #


def _edge_over(world: World, tmp_path) -> FastAPI:
    """The web gateway in front of *world*'s app; it trusts the API gateway at
    10.200.0.0/16 and reaches the API server from an address the API trusts."""
    from inqtrix_web_gateway import app as gateway_app
    from inqtrix_web_gateway import settings as gateway_settings

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>app</body></html>", encoding="utf-8")
    return gateway_app.build_app(
        dist,
        "http://api.internal",
        transport=httpx.ASGITransport(app=world.client.app, client=(_PEER, 50_000)),
        identity_policy=gateway_settings.IdentityEdgePolicy(
            trusted_proxies="10.200.0.0/16",
            proof_header="X-Inqtrix-Proof",
            resolver_headers="X-Static-User",
        ),
    )


@pytest.mark.parametrize(
    ("peer", "authenticated"),
    [("192.0.2.7", False), ("10.200.0.9", True)],
    ids=["stranger", "api_gateway"],
)
def test_a_resolver_of_its_own_behind_the_edge(tmp_path, peer: str, authenticated: bool) -> None:
    """The edge strips the resolver's header and the proof from a stranger, so
    only the request of the API gateway signs in."""
    world = World(assertion="resolver", resolver=_guarded(), issuer_key="gateway:corp-sso")

    with TestClient(_edge_over(world, tmp_path), client=(peer, 50_000)) as client:
        response = client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    assert response.status_code == 200
    assert response.json()["authenticated"] is authenticated
    assert bool(world.all_users()) is authenticated


def test_a_resolver_of_its_own_refuses_a_stranger_at_the_api_port() -> None:
    world = World(
        assertion="resolver", resolver=_guarded(), issuer_key="gateway:corp-sso", peer="192.0.2.7"
    )

    response = world.client.get("/api/auth/session", headers={"X-Static-User": "user-h", **_PROOF})

    assert response.status_code == 401
    assert _error_type(response) == "untrusted_source"
    assert not world.all_users()

