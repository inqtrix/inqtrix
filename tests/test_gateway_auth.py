"""Gateway mode: an API gateway in front of Inqtrix asserts who the user is.

These tests pin the mode itself and its start checks: ``gateway`` is a
cookie-session mode, a gateway deployment refuses to start with an
incomplete or unsafe configuration, and the settings bridge never serves the
mode with a provider that ignores the gateway.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import types
from ipaddress import ip_network
from pathlib import Path
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet, RSAKey
from pydantic import ValidationError
from starlette.requests import Request

from inqtrix.auth import api_key as api_key_module
from inqtrix.auth import gateway as gateway_module
from inqtrix.auth.api_key import build_auth_provider
from inqtrix.auth.credentials import LOCAL_ISSUER
from inqtrix.auth.directory import DELETED_ACCOUNT_ISSUER, RESERVED_ISSUERS
from inqtrix.auth.gateway import (
    ASSERTION_FAILURES,
    OUTCOME_STATUS,
    CustomResolverGuard,
    GatewayAssertionError,
    GatewayIdentityResolver,
    IdentitySource,
    JwtAssertionResolver,
    TrustedHeaderResolver,
    VerifiedIdentity,
    build_gateway_provider,
    gateway_identity_source,
)
from inqtrix.auth.jwks import KeysUnavailable, StaticKeys
from inqtrix.auth.mapping import ClaimMappingConfig
from inqtrix.auth.principal import COOKIE_SESSION_MODES, AuthMode, resolve_auth_mode
from inqtrix.server.routers import auth_config
from inqtrix.settings import AuthSettings, ServerSettings, Settings

_SECRETS = {
    "session_secret": "s" * 32,
    "pat_pepper": "p" * 32,
    "pseudonym_pepper": "q" * 32,
}

_JWT = {
    "mode": "gateway",
    **_SECRETS,
    "gateway_assertion": "jwt",
    "gateway_jwt_header": "X-Inqtrix-Assertion",
    "gateway_jwt_issuer": "https://idp.example/realms/corp",
    "gateway_jwt_audience": "inqtrix",
    "gateway_jwt_user_scope": "inqtrix.user",
    "gateway_jwks_url": "https://idp.example/realms/corp/protocol/openid-connect/certs",
}

_HEADER = {
    "mode": "gateway",
    **_SECRETS,
    "gateway_assertion": "header",
    "gateway_issuer_key": "gateway:corp-sso",
    "gateway_subject_header": "X-Inqtrix-Subject",
    "gateway_email_header": "X-Inqtrix-Email",
    "gateway_proof_header": "X-Inqtrix-Proof",
    "gateway_shared_secret": "x" * 32,
    "gateway_trusted_proxies": "10.0.0.0/8",
}


def _auth(base: dict[str, object], **overrides: object) -> AuthSettings:
    return AuthSettings(**{**base, **overrides})


KEY_ONE = RSAKey.generate_key(2048, {"kid": "key-one", "alg": "RS256"})
KEY_TWO = RSAKey.generate_key(2048, {"kid": "key-two", "alg": "RS256"})
KEY_OTHER = RSAKey.generate_key(2048, {"kid": "key-one", "alg": "RS256"})


class _NoAssertion:
    """A resolver that never finds an assertion (for source checks)."""

    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        return None


# ------------------------------------------------------------------ #
# The mode
# ------------------------------------------------------------------ #


def test_gateway_is_an_auth_mode_with_a_cookie_session() -> None:
    # Sharing, collaboration, quota and admin all gate on this one set; a
    # mode missing from it loses those surfaces without any error.
    assert "gateway" in get_args(AuthMode)
    assert "gateway" in COOKIE_SESSION_MODES


def test_the_settings_accept_gateway_as_mode() -> None:
    assert AuthSettings(mode="gateway").mode == "gateway"


def test_settings_bridge_builds_the_gateway_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # Before this mode existed, an unknown mode fell through to the open
    # provider; the bridge must hand the gateway mode to its own builder.
    sentinel = object()
    seen: list[Settings] = []

    def fake_build(settings: Settings) -> object:
        seen.append(settings)
        return sentinel

    monkeypatch.setattr(gateway_module, "build_gateway_provider", fake_build)
    settings = Settings(auth=_auth(_JWT))
    settings.storage.backend = "postgres"

    assert build_auth_provider(settings) is sentinel
    assert seen == [settings]


def test_gateway_mode_gets_the_cookie_session_contract() -> None:
    # The surfaces read the mode set; the auth config shows its effect: a
    # gateway session needs CSRF and offers a logout like every cookie mode.
    container = types.SimpleNamespace(auth_provider=types.SimpleNamespace(mode="gateway"))
    app = FastAPI()
    app.include_router(auth_config.build_router(container))

    body = TestClient(app).get("/api/auth/config").json()

    assert body["auth_mode"] == "gateway"
    assert body["csrf_required"] is True
    assert body["supports_logout"] is True


def test_an_unknown_mode_never_falls_back_to_the_open_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_key_module, "resolve_auth_mode", lambda *args, **kwargs: "unexpected")

    with pytest.raises(RuntimeError, match="unknown mode"):
        build_auth_provider(Settings())


def test_an_unknown_assertion_kind_is_rejected_by_the_settings() -> None:
    # A custom resolver is not an assertion kind; there is no ``plugin`` value.
    with pytest.raises(ValidationError):
        AuthSettings(gateway_assertion="plugin")


# ------------------------------------------------------------------ #
# Start checks in resolve_auth_mode (No. 1, 7, 8): every process
# ------------------------------------------------------------------ #


def test_complete_gateway_configuration_resolves() -> None:
    assert resolve_auth_mode(_auth(_JWT), ServerSettings(), storage_backend="postgres") == "gateway"


def test_a_worker_resolves_gateway_mode_without_an_assertion_kind() -> None:
    # A process without an HTTP provider (the worker) checks only what every
    # process of the deployment needs; the identity source is not its concern.
    auth = AuthSettings(mode="gateway", **_SECRETS)

    assert resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres") == "gateway"


@pytest.mark.parametrize(
    ("field", "name"),
    [
        ("session_secret", "INQTRIX_SESSION_SECRET"),
        ("pat_pepper", "INQTRIX_PAT_PEPPER"),
        ("pseudonym_pepper", "INQTRIX_PSEUDONYM_PEPPER"),
    ],
)
def test_gateway_mode_requires_the_session_secrets(field: str, name: str) -> None:
    with pytest.raises(RuntimeError, match=name):
        resolve_auth_mode(_auth(_JWT, **{field: ""}), ServerSettings(), storage_backend="postgres")


def test_gateway_mode_requires_postgres() -> None:
    # Tombstones, the inactivity bound and SCIM need durable state.
    with pytest.raises(RuntimeError, match="INQTRIX_STORAGE_BACKEND=postgres"):
        resolve_auth_mode(_auth(_JWT), ServerSettings(), storage_backend="memory")


def test_without_scim_a_personal_access_token_needs_a_bound() -> None:
    auth = _auth(_JWT, gateway_pat_max_ttl_days=0, gateway_pat_inactive_days=0)

    with pytest.raises(RuntimeError) as stop:
        resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres")

    assert "INQTRIX_GATEWAY_PAT_MAX_TTL_DAYS" in str(stop.value)
    assert "INQTRIX_GATEWAY_PAT_INACTIVE_DAYS" in str(stop.value)


@pytest.mark.parametrize(
    ("max_ttl", "inactive"),
    [(90, 0), (0, 30)],
)
def test_one_personal_access_token_bound_is_enough(max_ttl: int, inactive: int) -> None:
    auth = _auth(_JWT, gateway_pat_max_ttl_days=max_ttl, gateway_pat_inactive_days=inactive)

    assert resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres") == "gateway"


def test_with_scim_both_personal_access_token_bounds_may_be_off() -> None:
    auth = _auth(
        _JWT,
        gateway_pat_max_ttl_days=0,
        gateway_pat_inactive_days=0,
        scim_enabled=True,
        scim_token="t" * 32,
    )

    assert resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres") == "gateway"


def test_scim_token_needs_32_characters() -> None:
    auth = _auth(_JWT, scim_enabled=True, scim_token="t" * 31)

    with pytest.raises(RuntimeError, match="INQTRIX_SCIM_TOKEN"):
        resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres")


def test_scim_is_the_only_way_in_so_invitations_are_rejected() -> None:
    auth = _auth(_JWT, scim_enabled=True, scim_token="t" * 32, registration="invite")

    with pytest.raises(RuntimeError, match="INQTRIX_REGISTRATION=invite"):
        resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres")


@pytest.mark.parametrize(
    "auth",
    [
        AuthSettings(scim_enabled=True, scim_token="t" * 32),
        AuthSettings(
            mode="oidc",
            oidc_issuer="https://idp.example",
            oidc_client_id="inqtrix",
            oidc_client_secret="c" * 32,
            scim_enabled=True,
            scim_token="t" * 32,
            **_SECRETS,
        ),
        AuthSettings(mode="local", scim_enabled=True, scim_token="t" * 32, **_SECRETS),
    ],
    ids=["inferred", "oidc", "local"],
)
def test_scim_runs_only_in_gateway_mode(auth: AuthSettings) -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_SCIM_ENABLED"):
        resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres")


@pytest.mark.parametrize(
    "auth",
    [
        AuthSettings(gateway_resolver="firma_auth:Resolver"),
        AuthSettings(mode="local", gateway_resolver="firma_auth:Resolver", **_SECRETS),
    ],
    ids=["inferred", "local"],
)
def test_a_resolver_of_its_own_runs_only_in_gateway_mode(auth: AuthSettings) -> None:
    """Outside the gateway mode the setting would be ignored without a word."""
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_RESOLVER"):
        resolve_auth_mode(auth, ServerSettings(), storage_backend="postgres")


# ------------------------------------------------------------------ #
# Start checks of the identity source (No. 2, 3, 5, 6): API server
# ------------------------------------------------------------------ #


def test_complete_jwt_configuration_yields_the_issuer_as_identity_source() -> None:
    source = gateway_identity_source(_auth(_JWT))

    assert source.issuer_key == "https://idp.example/realms/corp"
    assert isinstance(source.resolver, JwtAssertionResolver)


def test_complete_header_configuration_yields_the_issuer_key() -> None:
    source = gateway_identity_source(_auth(_HEADER))

    assert source.issuer_key == "gateway:corp-sso"
    assert isinstance(source.resolver, TrustedHeaderResolver)


def test_assertion_kind_is_required() -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_ASSERTION"):
        gateway_identity_source(_auth(_JWT, gateway_assertion=""))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"gateway_jwt_header": ""}, "INQTRIX_GATEWAY_JWT_HEADER"),
        ({"gateway_jwt_issuer": ""}, "INQTRIX_GATEWAY_JWT_ISSUER"),
        ({"gateway_jwt_audience": ""}, "INQTRIX_GATEWAY_JWT_AUDIENCE"),
        ({"gateway_jwt_user_scope": ""}, "INQTRIX_GATEWAY_JWT_USER_SCOPE"),
        ({"gateway_jwt_user_scope": "inqtrix.user openid"}, "INQTRIX_GATEWAY_JWT_USER_SCOPE"),
        ({"gateway_jwks_url": ""}, "INQTRIX_GATEWAY_JWKS_URL"),
        ({"gateway_jwt_public_key_file": "/run/keys/gateway.jwks"}, "INQTRIX_GATEWAY_JWT_PUBLIC_KEY_FILE"),
        ({"gateway_jwt_algorithms": "RS256,HS256"}, "INQTRIX_GATEWAY_JWT_ALGORITHMS"),
        ({"gateway_jwt_algorithms": "none"}, "INQTRIX_GATEWAY_JWT_ALGORITHMS"),
        ({"gateway_jwt_algorithms": "RS256,XY999"}, "INQTRIX_GATEWAY_JWT_ALGORITHMS"),
        ({"gateway_jwt_algorithms": " , "}, "INQTRIX_GATEWAY_JWT_ALGORITHMS"),
        ({"gateway_subject_claim": "email"}, "INQTRIX_GATEWAY_SUBJECT_CLAIM"),
        ({"gateway_subject_claim": "upn"}, "INQTRIX_GATEWAY_SUBJECT_CLAIM"),
        ({"gateway_subject_claim": "preferred_username"}, "INQTRIX_GATEWAY_SUBJECT_CLAIM"),
        ({"gateway_subject_claim": "unique_name"}, "INQTRIX_GATEWAY_SUBJECT_CLAIM"),
        (
            {"gateway_subject_claim": "mail", "oidc_email_claim": "mail"},
            "INQTRIX_GATEWAY_SUBJECT_CLAIM",
        ),
        ({"gateway_trusted_proxies": "0.0.0.0/0"}, "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
        ({"gateway_trusted_proxies": "10.0.0.0/8,not-a-network"}, "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
    ],
    ids=lambda value: str(value),
)
def test_incomplete_or_unsafe_jwt_configuration_stops_the_start(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(RuntimeError, match=expected):
        gateway_identity_source(_auth(_JWT, **overrides))


def test_a_key_file_alone_is_a_key_source(tmp_path: Path) -> None:
    path = tmp_path / "gateway.jwks"
    path.write_text(json.dumps(KeySet([KEY_ONE, KEY_TWO]).as_dict(private=False)), encoding="utf-8")
    auth = _auth(_JWT, gateway_jwks_url="", gateway_jwt_public_key_file=str(path))

    assert gateway_identity_source(auth).issuer_key == "https://idp.example/realms/corp"


@pytest.mark.parametrize(
    "content",
    [json.dumps({"kty": "RSA"}), json.dumps({"keys": []}), "not json"],
    ids=["no-keys-member", "empty", "not-json"],
)
def test_an_unusable_key_file_stops_the_start(tmp_path: Path, content: str) -> None:
    path = tmp_path / "gateway.jwks"
    path.write_text(content, encoding="utf-8")
    auth = _auth(_JWT, gateway_jwks_url="", gateway_jwt_public_key_file=str(path))

    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_JWT_PUBLIC_KEY_FILE"):
        gateway_identity_source(auth)


def test_a_missing_key_file_stops_the_start(tmp_path: Path) -> None:
    auth = _auth(_JWT, gateway_jwks_url="", gateway_jwt_public_key_file=str(tmp_path / "absent.jwks"))

    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_JWT_PUBLIC_KEY_FILE"):
        gateway_identity_source(auth)


@pytest.mark.parametrize("leeway", [-1, 121])
def test_clock_leeway_stays_within_two_minutes(leeway: int) -> None:
    with pytest.raises(ValidationError):
        AuthSettings(gateway_jwt_leeway_seconds=leeway)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"gateway_subject_header": ""}, "INQTRIX_GATEWAY_SUBJECT_HEADER"),
        ({"gateway_proof_header": ""}, "INQTRIX_GATEWAY_PROOF_HEADER"),
        ({"gateway_shared_secret": ""}, "INQTRIX_GATEWAY_SHARED_SECRET"),
        ({"gateway_shared_secret": "x" * 31}, "INQTRIX_GATEWAY_SHARED_SECRET"),
        ({"gateway_trusted_proxies": ""}, "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
        ({"gateway_trusted_proxies": "0.0.0.0/0"}, "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
        ({"gateway_trusted_proxies": "10.0.0.0/8,::/0"}, "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
        ({"gateway_trusted_proxies": "10.0.0.0/33"}, "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
        ({"gateway_email_header": "", "gateway_name_header": ""}, "INQTRIX_GATEWAY_EMAIL_HEADER"),
        ({"gateway_issuer_key": ""}, "INQTRIX_GATEWAY_ISSUER_KEY"),
        ({"registration": "invite"}, "INQTRIX_REGISTRATION=invite"),
        ({"oidc_allowed_domains": "example.com"}, "INQTRIX_OIDC_ALLOWED_DOMAINS"),
        ({"oidc_admin_groups": "inqtrix-admins"}, "INQTRIX_OIDC_ADMIN_GROUPS"),
        ({"oidc_admin_roles": "admin"}, "INQTRIX_OIDC_ADMIN_ROLES"),
    ],
    ids=lambda value: str(value),
)
def test_incomplete_or_unsafe_header_configuration_stops_the_start(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(RuntimeError, match=expected):
        gateway_identity_source(_auth(_HEADER, **overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"gateway_email_header": "", "gateway_name_header": "X-Inqtrix-Name"},
        {"gateway_email_header": "X-Inqtrix-Email", "gateway_name_header": ""},
    ],
)
def test_a_name_or_an_email_header_is_enough(overrides: dict[str, object]) -> None:
    assert gateway_identity_source(_auth(_HEADER, **overrides)).issuer_key == "gateway:corp-sso"


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "cookie",
        "Host",
        "Connection",
        "Transfer-Encoding",
        "X-Forwarded-For",
        "x-forwarded-user",
    ],
)
def test_identity_headers_never_use_reserved_names(name: str) -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_SUBJECT_HEADER"):
        gateway_identity_source(_auth(_HEADER, gateway_subject_header=name))
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_JWT_HEADER"):
        gateway_identity_source(_auth(_JWT, gateway_jwt_header=name))


@pytest.mark.parametrize(
    "name",
    ["Origin", "X-CSRF-Token", "x-inqtrix-expected-user-id", "X-Inqtrix-Workspace-Id", "X-Inqtrix-Guest-CSRF"],
)
def test_identity_headers_never_name_what_the_edge_passes_on(name: str) -> None:
    # The edge strips every identity header from strangers; these the browser needs.
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_EMAIL_HEADER must not name"):
        gateway_identity_source(_auth(_HEADER, gateway_email_header=name))
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_JWT_HEADER must not name"):
        gateway_identity_source(_auth(_JWT, gateway_jwt_header=name))


@pytest.mark.parametrize("name", ["X-Inqtrix-Subject\u212a", "X Inqtrix Subject", "X-Inqtrix-Subject,X-Other"])
def test_identity_headers_are_header_names(name: str) -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_SUBJECT_HEADER is not a header name"):
        gateway_identity_source(_auth(_HEADER, gateway_subject_header=name))


@pytest.mark.parametrize(
    "overrides",
    [
        {"gateway_email_header": "x-inqtrix-subject"},
        {"gateway_proof_header": "X-INQTRIX-SUBJECT"},
        {"gateway_groups_header": "X-Inqtrix-Proof"},
        {"gateway_name_header": "X-Inqtrix-Email"},
    ],
    ids=lambda value: str(value),
)
def test_identity_headers_are_pairwise_distinct(overrides: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="distinct"):
        gateway_identity_source(_auth(_HEADER, **overrides))


def test_every_problem_is_named_at_once() -> None:
    auth = _auth(_HEADER, gateway_proof_header="", gateway_shared_secret="short", gateway_trusted_proxies="")

    with pytest.raises(RuntimeError) as stop:
        gateway_identity_source(auth)

    for name in (
        "INQTRIX_GATEWAY_PROOF_HEADER",
        "INQTRIX_GATEWAY_SHARED_SECRET",
        "INQTRIX_GATEWAY_TRUSTED_PROXIES",
    ):
        assert name in str(stop.value)
    assert "short" not in str(stop.value)


def test_secret_values_never_appear_in_start_errors() -> None:
    secret = "MARKER-" + "z" * 20
    auth = _auth(_HEADER, gateway_shared_secret=secret)

    with pytest.raises(RuntimeError) as stop:
        gateway_identity_source(auth)

    assert secret not in str(stop.value)


@pytest.mark.parametrize(
    "issuer_key",
    [LOCAL_ISSUER, DELETED_ACCOUNT_ISSUER, "", "   ", "g" * 256],
    ids=["local", "system", "empty", "blank", "too-long"],
)
def test_identity_source_rejects_reserved_empty_and_overlong_issuers(issuer_key: str) -> None:
    with pytest.raises(ValueError):
        IdentitySource(issuer_key=issuer_key, resolver=_NoAssertion())


def test_reserved_issuers_are_the_local_and_the_system_issuer() -> None:
    assert RESERVED_ISSUERS == frozenset({LOCAL_ISSUER, DELETED_ACCOUNT_ISSUER})


@pytest.mark.parametrize("issuer", [LOCAL_ISSUER, DELETED_ACCOUNT_ISSUER])
def test_a_reserved_issuer_cannot_become_the_jwt_issuer(issuer: str) -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_JWT_ISSUER"):
        gateway_identity_source(_auth(_JWT, gateway_jwt_issuer=issuer))


@pytest.mark.parametrize("issuer_key", [LOCAL_ISSUER, DELETED_ACCOUNT_ISSUER])
def test_a_reserved_issuer_cannot_become_the_header_issuer_key(issuer_key: str) -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_ISSUER_KEY"):
        gateway_identity_source(_auth(_HEADER, gateway_issuer_key=issuer_key))


def test_the_provider_builder_runs_the_identity_source_checks() -> None:
    settings = Settings(auth=_auth(_HEADER, gateway_shared_secret="short"))
    settings.storage.backend = "postgres"

    with pytest.raises(RuntimeError, match="INQTRIX_GATEWAY_SHARED_SECRET"):
        build_gateway_provider(settings)


# ------------------------------------------------------------------ #
# Resolver contract
# ------------------------------------------------------------------ #


def test_a_resolver_may_raise_only_assertion_failures() -> None:
    with pytest.raises(ValueError):
        GatewayAssertionError("not_provisioned")
    for outcome in ASSERTION_FAILURES:
        assert GatewayAssertionError(outcome).status == OUTCOME_STATUS[outcome]
    assert GatewayAssertionError("keys_unavailable").status == 503
    assert GatewayAssertionError("subject_is_email").status == 403


def test_the_built_in_resolvers_satisfy_the_protocol() -> None:
    assert isinstance(_jwt_resolver(), GatewayIdentityResolver)
    assert isinstance(_header_resolver(), GatewayIdentityResolver)


def test_an_identity_source_needs_a_resolver() -> None:
    with pytest.raises(TypeError):
        IdentitySource(issuer_key="gateway:corp-sso", resolver=object())


def _request(
    headers: list[tuple[str, str | bytes]], client: str | None = "10.1.2.3", query: bytes = b""
) -> Request:
    raw = [
        (name.lower().encode("latin-1"), value if isinstance(value, bytes) else value.encode("latin-1"))
        for name, value in headers
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/runs",
            "raw_path": b"/v1/runs",
            "query_string": query,
            "headers": raw,
            "client": (client, 50_000) if client else None,
            "server": ("api", 5100),
            "scheme": "http",
            "root_path": "",
        }
    )


def _resolve(resolver: object, request: Request) -> VerifiedIdentity | None:
    return asyncio.run(resolver.resolve(request))


def _outcome(resolver: object, request: Request) -> str:
    with pytest.raises(GatewayAssertionError) as rejected:
        _resolve(resolver, request)
    return rejected.value.outcome


# ------------------------------------------------------------------ #
# JWT assertions
# ------------------------------------------------------------------ #

ISSUER = "https://idp.example/realms/corp"
AUDIENCE = "inqtrix"
USER_SCOPE = "inqtrix.user"
NOW = 1_800_000_000
JWT_HEADER = "X-Inqtrix-Assertion"


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "f3a1c9e0-7b2d-4c1e-9a55-user",
        "scp": USER_SCOPE,
        "exp": NOW + 300,
        "iat": NOW - 5,
        "email": "alice@example.com",
        "email_verified": True,
        "preferred_username": "alice",
        "groups": ["team-a"],
        "roles": ["reader"],
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def _sign(claims: dict[str, object], key: RSAKey = KEY_ONE, **header: object) -> str:
    return jose_jwt.encode({"alg": "RS256", "kid": key.kid, **header}, claims, key)


def _jwt_resolver(
    *, keys: object | None = None, mapping: ClaimMappingConfig | None = None
) -> JwtAssertionResolver:
    return JwtAssertionResolver(
        header_name=JWT_HEADER,
        issuer=ISSUER,
        audience=AUDIENCE,
        user_scope=USER_SCOPE,
        algorithms=("RS256", "ES256"),
        leeway_seconds=30,
        subject_claim="sub",
        claim_mapping=mapping or ClaimMappingConfig(),
        keys=keys or StaticKeys(KeySet([KEY_ONE, KEY_TWO])),
        clock=lambda: NOW,
    )


def _jwt_request(token: str) -> Request:
    return _request([(JWT_HEADER, token)])


def test_a_valid_user_token_yields_the_verified_identity() -> None:
    identity = _resolve(_jwt_resolver(), _jwt_request(_sign(_claims())))

    assert identity == VerifiedIdentity(
        subject="f3a1c9e0-7b2d-4c1e-9a55-user",
        email="alice@example.com",
        email_verified=True,
        display_name="alice",
        groups=("team-a",),
        roles=("reader",),
    )


def test_without_the_assertion_header_there_is_no_assertion() -> None:
    assert _resolve(_jwt_resolver(), _request([])) is None


def test_two_assertion_headers_are_rejected() -> None:
    token = _sign(_claims())
    request = _request([(JWT_HEADER, token), (JWT_HEADER, token)])

    assert _outcome(_jwt_resolver(), request) == "duplicate_header"


@pytest.mark.parametrize("token", ["not-a-token", "a.b"], ids=["one-part", "two-parts"])
def test_an_unreadable_assertion_is_malformed(token: str) -> None:
    assert _outcome(_jwt_resolver(), _jwt_request(token)) == "malformed"


def test_an_oversized_assertion_is_malformed_even_when_validly_signed() -> None:
    token = _sign(_claims(padding="x" * (16 * 1024)))

    assert _outcome(_jwt_resolver(), _jwt_request(token)) == "malformed"


def test_an_assertion_header_nested_beyond_the_parser_is_malformed() -> None:
    # Deeper than the JSON parser recurses, yet within the size bound: it is
    # an unreadable assertion, not a server error.
    import base64

    header = base64.urlsafe_b64encode(b"[" * 10_000).rstrip(b"=").decode()
    token = f"{header}.e30.sig"
    assert len(token.encode()) < 16 * 1024

    assert _outcome(_jwt_resolver(), _jwt_request(token)) == "malformed"


def _unsigned(header: dict[str, object], claims: dict[str, object]) -> str:
    def segment(data: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{segment(header)}.{segment(claims)}."


def test_alg_none_is_rejected() -> None:
    token = _unsigned({"alg": "none", "kid": "key-one"}, _claims())

    assert _outcome(_jwt_resolver(), _jwt_request(token)) == "signature_invalid"


def test_hmac_with_the_public_key_as_secret_is_rejected() -> None:
    # The library refuses to build this key, so the attack token is signed by hand.
    secret = KEY_ONE.as_pem(private=False)
    unsigned = _unsigned({"alg": "HS256", "kid": "key-one"}, _claims()).rstrip(".")
    signature = hmac.new(secret, unsigned.encode(), hashlib.sha256).digest()
    token = f"{unsigned}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    assert _outcome(_jwt_resolver(), _jwt_request(token)) == "signature_invalid"


def test_an_algorithm_outside_the_configured_list_is_rejected() -> None:
    # The key is known and the signature is valid; only the algorithm list
    # (RS256 and ES256 here) may reject the token.
    key = RSAKey.generate_key(2048, {"kid": "key-any-alg"})
    resolver = _jwt_resolver(keys=StaticKeys(KeySet([KEY_ONE, key])))
    token = jose_jwt.encode({"alg": "RS384", "kid": "key-any-alg"}, _claims(), key, algorithms=["RS384"])

    assert _outcome(resolver, _jwt_request(token)) == "signature_invalid"


@pytest.mark.parametrize("parameter", ["jwk", "jku", "x5u", "x5c"])
def test_keys_offered_by_the_token_are_never_used(parameter: str) -> None:
    value: object = KEY_ONE.as_dict(private=False) if parameter == "jwk" else "https://attacker.example/keys"
    if parameter == "x5c":
        value = ["MIIB"]
    token = _sign(_claims(), **{parameter: value})

    assert _outcome(_jwt_resolver(), _jwt_request(token)) == "signature_invalid"


def test_a_signature_by_another_key_is_rejected() -> None:
    token = _sign(_claims(), key=KEY_OTHER)

    assert _outcome(_jwt_resolver(), _jwt_request(token)) == "signature_invalid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://idp.example/realms/other"},
        {"iss": ISSUER + "/"},
        {"aud": "another-api"},
        {"aud": ["another-api", "third"]},
        {"scp": None},
        {"scp": "openid profile"},
        {"scp": None, "roles": [USER_SCOPE]},
        {"scp": None, "oid": "f3a1c9e0-7b2d-4c1e-9a55-user", "roles": ["Inqtrix.Admin"], "idtyp": "app"},
        {"sub": 12345},
        {"sub": ""},
        {"sub": "s" * 256},
        {"sub": "user\n1"},
    ],
    ids=[
        "other-issuer",
        "issuer-with-slash",
        "other-audience",
        "audience-list-without-ours",
        "no-scope",
        "scope-without-user-scope",
        "user-scope-only-in-roles",
        "client-credentials-token",
        "subject-not-a-string",
        "empty-subject",
        "overlong-subject",
        "subject-with-control-character",
    ],
)
def test_claims_that_do_not_prove_a_signed_in_user_are_invalid(overrides: dict[str, object]) -> None:
    assert _outcome(_jwt_resolver(), _jwt_request(_sign(_claims(**overrides)))) == "claims_invalid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": [AUDIENCE, "another-api"]},
        {"scp": None, "scope": "openid inqtrix.user profile"},
        {"scp": [USER_SCOPE, "other"]},
        {"exp": NOW - 20},
    ],
    ids=["audience-list", "scope-string", "scp-list", "exp-within-leeway"],
)
def test_accepted_claim_forms(overrides: dict[str, object]) -> None:
    identity = _resolve(_jwt_resolver(), _jwt_request(_sign(_claims(**overrides))))

    assert identity is not None


@pytest.mark.parametrize(
    "overrides",
    [{"exp": NOW - 31}, {"exp": None}, {"nbf": NOW + 31}, {"iat": NOW + 31}],
    ids=["expired", "no-exp", "not-yet-valid", "issued-in-the-future"],
)
def test_tokens_outside_their_lifetime_are_expired(overrides: dict[str, object]) -> None:
    assert _outcome(_jwt_resolver(), _jwt_request(_sign(_claims(**overrides)))) == "expired"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": "alice@example.com", "email": None},
        {"sub": "ALICE@EXAMPLE.COM"},
        {"sub": "bob@corp.example"},
    ],
    ids=["email-form-without-email-claim", "equal-to-email", "email-form"],
)
def test_an_email_never_becomes_the_subject(overrides: dict[str, object]) -> None:
    resolver = _jwt_resolver()
    request = _jwt_request(_sign(_claims(**overrides)))

    assert _outcome(resolver, request) == "subject_is_email"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_email_verified_counts_only_when_true(value: object) -> None:
    identity = _resolve(_jwt_resolver(), _jwt_request(_sign(_claims(email_verified=value))))

    assert identity is not None and identity.email_verified is False


class _UnavailableKeys:
    async def keys_for(self, token: str) -> KeySet:
        raise KeysUnavailable("no key set")


def test_unavailable_keys_answer_with_503() -> None:
    resolver = _jwt_resolver(keys=_UnavailableKeys())

    with pytest.raises(GatewayAssertionError) as rejected:
        _resolve(resolver, _jwt_request(_sign(_claims())))

    assert rejected.value.outcome == "keys_unavailable"
    assert rejected.value.status == 503


def test_both_keys_of_a_key_file_verify() -> None:
    resolver = _jwt_resolver()

    assert _resolve(resolver, _jwt_request(_sign(_claims(), key=KEY_ONE))) is not None
    assert _resolve(resolver, _jwt_request(_sign(_claims(), key=KEY_TWO))) is not None


def test_a_kid_outside_the_key_file_is_a_signature_failure_not_an_outage() -> None:
    resolver = _jwt_resolver(keys=StaticKeys(KeySet([KEY_ONE])))

    assert _outcome(resolver, _jwt_request(_sign(_claims(), key=KEY_TWO))) == "signature_invalid"


def _overage(**overrides: object) -> dict[str, object]:
    return _claims(
        groups=None,
        _claim_names={"groups": "src1"},
        _claim_sources={"src1": {"endpoint": "https://graph.example/v1.0/users/x/getMemberObjects"}},
        **overrides,
    )


def test_overage_with_a_group_gate_is_rejected() -> None:
    resolver = _jwt_resolver(mapping=ClaimMappingConfig(allowed_groups=frozenset({"team-a"})))

    assert _outcome(resolver, _jwt_request(_sign(_overage()))) == "claims_invalid"


def test_overage_with_admin_groups_only_leaves_groups_empty_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _jwt_resolver(mapping=ClaimMappingConfig(admin_groups=frozenset({"admins"})))
    request = _jwt_request(_sign(_overage()))

    with caplog.at_level(logging.WARNING, logger="inqtrix.auth.mapping"):
        first = _resolve(resolver, request)
        second = _resolve(resolver, request)

    assert first is not None and first.groups == ()
    assert second is not None and second.groups == ()
    warnings = [record for record in caplog.records if "distributed claim" in record.getMessage()]
    assert len(warnings) == 1


# ------------------------------------------------------------------ #
# Header assertions
# ------------------------------------------------------------------ #

SHARED_SECRET = "proof-" + "s" * 40


def _header_resolver(**overrides: object) -> TrustedHeaderResolver:
    options: dict[str, object] = {
        "subject_header": "X-Inqtrix-Subject",
        "email_header": "X-Inqtrix-Email",
        "name_header": "X-Inqtrix-Name",
        "groups_header": "X-Inqtrix-Groups",
        "proof_header": "X-Inqtrix-Proof",
        "shared_secret": SHARED_SECRET,
        "trusted_networks": (ip_network("10.0.0.0/8"),),
        "header_decoding": "none",
        "claim_mapping": ClaimMappingConfig(),
    }
    options.update(overrides)
    return TrustedHeaderResolver(**options)


def _headers(**values: str | bytes | None) -> list[tuple[str, str | bytes]]:
    defaults: dict[str, str | bytes | None] = {
        "X-Inqtrix-Subject": "e7b1c2d3-user",
        "X-Inqtrix-Email": "alice@example.com",
        "X-Inqtrix-Name": "Alice",
        "X-Inqtrix-Groups": "team-a,team-b",
        "X-Inqtrix-Proof": SHARED_SECRET,
    }
    renamed = {name.replace("_", "-"): value for name, value in values.items()}
    defaults.update({key: renamed[key] for key in renamed})
    return [(name, value) for name, value in defaults.items() if value is not None]


def test_no_header_value_reaches_the_log(caplog) -> None:
    marker = "zz-marker-5c1e"
    resolver = _header_resolver()
    cases = [
        _request(
            _headers(
                X_Inqtrix_Subject=f"{marker}-sub",
                X_Inqtrix_Email=f"{marker}@example.com",
                X_Inqtrix_Name=f"{marker} Name",
                X_Inqtrix_Groups=f"{marker}-group",
            )
        ),
        _request(_headers(X_Inqtrix_Subject=f"{marker}-sub", X_Inqtrix_Proof=f"{marker}-wrong-secret")),
        _request(_headers(X_Inqtrix_Subject=f"{marker}-sub"), client="192.0.2.10"),
        _request(
            [*_headers(X_Inqtrix_Subject=f"{marker}-sub"), ("X-Inqtrix-Subject", f"{marker}-again")]
        ),
    ]

    with caplog.at_level(logging.DEBUG):
        for request in cases:
            try:
                _resolve(resolver, request)
            except GatewayAssertionError:
                pass

    assert marker not in caplog.text
    assert SHARED_SECRET not in caplog.text


def test_a_valid_header_assertion_yields_the_identity() -> None:
    identity = _resolve(_header_resolver(), _request(_headers()))

    assert identity == VerifiedIdentity(
        subject="e7b1c2d3-user",
        email="alice@example.com",
        email_verified=False,
        display_name="Alice",
        groups=("team-a", "team-b"),
        roles=(),
    )


def test_without_a_subject_header_there_is_no_assertion() -> None:
    request = _request(_headers(**{"X_Inqtrix_Subject": None}))

    assert _resolve(_header_resolver(), request) is None


@pytest.mark.parametrize(
    "headers",
    [
        _headers(**{"X_Inqtrix_Proof": None}),
        _headers(**{"X_Inqtrix_Proof": "wrong-" + "s" * 40}),
        _headers() + [("X-Inqtrix-Proof", SHARED_SECRET)],
    ],
    ids=["no-proof", "wrong-proof", "proof-twice"],
)
def test_without_the_proof_the_headers_count_for_nothing(headers: list[tuple[str, str | bytes]]) -> None:
    assert _outcome(_header_resolver(), _request(headers)) == "untrusted_source"


@pytest.mark.parametrize(
    ("client", "extra"),
    [
        ("192.168.1.5", []),
        (None, []),
        ("192.168.1.5", [("X-Forwarded-For", "10.1.2.3")]),
        ("127.0.0.1", [("X-Forwarded-For", "10.1.2.3")]),
    ],
    ids=["foreign-peer", "no-peer", "peer-from-forwarded-header", "loopback-with-trusted-forwarded-header"],
)
def test_only_the_socket_peer_decides_the_source(client: str | None, extra: list[tuple[str, str]]) -> None:
    request = _request(_headers() + extra, client=client)

    assert _outcome(_header_resolver(), request) == "untrusted_source"


@pytest.mark.parametrize("name", ["X-Inqtrix-Subject", "X-Inqtrix-Email", "X-Inqtrix-Groups", "X-Inqtrix-Name"])
def test_a_repeated_identity_header_is_rejected(name: str) -> None:
    request = _request(_headers() + [(name, "second-value")])

    assert _outcome(_header_resolver(), request) == "duplicate_header"


@pytest.mark.parametrize(
    "values",
    [
        {"X_Inqtrix_Subject": b"e7b1\rc2"},
        {"X_Inqtrix_Email": b"alice@example.com\n"},
        {"X_Inqtrix_Name": b"Ali\x00ce"},
        {"X_Inqtrix_Groups": b"team-a,\x01team-b"},
        {"X_Inqtrix_Subject": "e7b1 c2d3"},
        {"X_Inqtrix_Subject": "e7b1,c2d3"},
        {"X_Inqtrix_Subject": "j\xfcrgen"},
        {"X_Inqtrix_Subject": "s" * 256},
        {"X_Inqtrix_Email": "a" * 310 + "@example.com"},
        {"X_Inqtrix_Name": "n" * 257},
        {"X_Inqtrix_Groups": ",".join(f"g{index}" for index in range(257))},
        {"X_Inqtrix_Groups": "g" * (8 * 1024 + 1)},
    ],
    ids=[
        "subject-cr",
        "email-lf",
        "name-nul",
        "groups-control",
        "subject-space",
        "subject-comma",
        "subject-non-ascii",
        "subject-too-long",
        "email-too-long",
        "name-too-long",
        "too-many-groups",
        "groups-too-long",
    ],
)
def test_malformed_header_values_are_rejected(values: dict[str, str | bytes]) -> None:
    assert _outcome(_header_resolver(), _request(_headers(**values))) == "malformed"


@pytest.mark.parametrize(
    ("values", "resolver_options"),
    [
        ({"X_Inqtrix_Subject": "alice@example.com"}, {}),
        ({"X_Inqtrix_Subject": "ALICE@example.com"}, {}),
        ({"X_Inqtrix_Subject": "bob@corp.example", "X_Inqtrix_Email": None}, {"email_header": ""}),
    ],
    ids=["equal-to-email", "equal-ignoring-case", "email-form-without-email-header"],
)
def test_an_email_never_becomes_the_header_subject(
    values: dict[str, str | None], resolver_options: dict[str, object]
) -> None:
    assert _outcome(_header_resolver(**resolver_options), _request(_headers(**values))) == "subject_is_email"


def test_percent_decoding_applies_to_name_and_groups_when_configured() -> None:
    request = _request(_headers(**{"X_Inqtrix_Name": "J%C3%BCrgen", "X_Inqtrix_Groups": "team%20a,team-b"}))

    decoded = _resolve(_header_resolver(header_decoding="percent"), request)
    raw = _resolve(_header_resolver(), request)

    assert decoded is not None and decoded.display_name == "Jürgen"
    assert decoded.groups == ("team a", "team-b")
    assert raw is not None and raw.display_name == "J%C3%BCrgen"


def test_a_decoded_control_character_is_still_malformed() -> None:
    request = _request(_headers(**{"X_Inqtrix_Name": "Ali%0Ace"}))

    assert _outcome(_header_resolver(header_decoding="percent"), request) == "malformed"


def test_the_shared_secret_is_compared_in_constant_time() -> None:
    """The header assertion and a resolver of its own check the proof in one
    place, and that place compares digests in constant time."""
    tree = ast.parse(inspect.getsource(gateway_module._require_proof))
    compared = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and "digest" in ast.unparse(node)
    ]
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "hmac.compare_digest"
    ]
    checked = [
        resolver.__name__
        for resolver in (TrustedHeaderResolver, CustomResolverGuard)
        if "_require_proof(" in inspect.getsource(resolver.resolve)
    ]

    assert compared == []
    assert calls
    assert checked == ["TrustedHeaderResolver", "CustomResolverGuard"]


# ------------------------------------------------------------------ #
# A resolver of its own behind the proof of the header assertion
# ------------------------------------------------------------------ #

_FIRMA_USER = ("X-Firma-User", "firma-1")
_FIRMA_PROOF = ("X-Inqtrix-Proof", "s" * 32)


class _FirmaResolver:
    """Reads one header of its own and counts how often the core let it read."""

    header_names = ("X-Firma-User",)

    def __init__(self, identity: VerifiedIdentity | None = None) -> None:
        self.identity = identity or VerifiedIdentity(subject="firma-1")
        self.calls = 0

    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        self.calls += 1
        return self.identity


class _Undeclared:
    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        return None


class _Synchronous:
    header_names = ("X-Firma-User",)

    def resolve(self, request: Request) -> VerifiedIdentity | None:
        return None


def _guard(resolver: object | None = None) -> CustomResolverGuard:
    return CustomResolverGuard(
        resolver or _FirmaResolver(),
        trusted_networks=[ip_network("10.0.0.0/8")],
        proof_header="X-Inqtrix-Proof",
        shared_secret="s" * 32,
    )


def test_a_resolver_of_its_own_reads_only_proven_requests() -> None:
    inner = _FirmaResolver()
    guard = _guard(inner)

    without = asyncio.run(guard.resolve(_request([("X-Other", "1")])))
    proven = asyncio.run(guard.resolve(_request([_FIRMA_USER, _FIRMA_PROOF])))

    assert without is None
    assert proven == inner.identity
    assert inner.calls == 1


@pytest.mark.parametrize(
    ("headers", "client", "outcome"),
    [
        ([_FIRMA_USER, _FIRMA_PROOF], "192.0.2.7", "untrusted_source"),
        ([_FIRMA_USER], "10.1.2.3", "untrusted_source"),
        ([_FIRMA_USER, ("X-Inqtrix-Proof", "t" * 32)], "10.1.2.3", "untrusted_source"),
        ([_FIRMA_USER, _FIRMA_PROOF, _FIRMA_PROOF], "10.1.2.3", "untrusted_source"),
        ([_FIRMA_USER, _FIRMA_USER, _FIRMA_PROOF], "10.1.2.3", "duplicate_header"),
        ([_FIRMA_USER, _FIRMA_PROOF, ("X-Forwarded-For", "10.9.9.9")], "127.0.0.1", "untrusted_source"),
    ],
    ids=["foreign_peer", "no_proof", "wrong_proof", "proof_twice", "header_twice", "forwarded_peer"],
)
def test_a_resolver_of_its_own_never_reads_an_unproven_request(headers, client, outcome) -> None:
    inner = _FirmaResolver()

    with pytest.raises(GatewayAssertionError) as raised:
        asyncio.run(_guard(inner).resolve(_request(headers, client)))

    assert raised.value.outcome == outcome
    assert inner.calls == 0


@pytest.mark.parametrize(
    ("identity", "outcome"),
    [
        (VerifiedIdentity(subject=""), "malformed"),
        (VerifiedIdentity(subject="firma\x001"), "malformed"),
        (VerifiedIdentity(subject="f" * 256), "malformed"),
        (VerifiedIdentity(subject="alice@example.com"), "subject_is_email"),
        (VerifiedIdentity(subject="firma-1", email="a" * 321), "malformed"),
        (VerifiedIdentity(subject="firma-1", display_name="n" * 257), "malformed"),
        (VerifiedIdentity(subject="firma-1", groups=tuple(f"g{index}" for index in range(257))), "malformed"),
        (VerifiedIdentity(subject="firma-1", roles=("admin\n",)), "malformed"),
        (VerifiedIdentity(subject="firma-1", email="a\r\n@example.com"), "malformed"),
        (VerifiedIdentity(subject="firma-1", display_name="A\x00"), "malformed"),
        (VerifiedIdentity(subject="firma-1", groups=("g\n",)), "malformed"),
        (VerifiedIdentity(subject="firma-1", email=123), "malformed"),
        (VerifiedIdentity(subject="firma-1", groups=("g" * 5000, "h" * 5000)), "malformed"),
        (VerifiedIdentity(subject="firma-1", groups=None), "malformed"),
        (VerifiedIdentity(subject="firma-1", roles=None), "malformed"),
        (VerifiedIdentity(subject="firma-1", groups="ab"), "malformed"),
        (VerifiedIdentity(subject="firma-1", roles="ab"), "malformed"),
    ],
    ids=[
        "empty",
        "control",
        "long_subject",
        "email_subject",
        "long_email",
        "long_name",
        "groups",
        "role_control",
        "email_control",
        "name_control",
        "group_control",
        "email_not_text",
        "group_bytes",
        "groups_none",
        "roles_none",
        "groups_text",
        "roles_text",
    ],
)
def test_the_values_of_a_resolver_of_its_own_are_checked(identity, outcome) -> None:
    with pytest.raises(GatewayAssertionError) as raised:
        asyncio.run(_guard(_FirmaResolver(identity)).resolve(_request([_FIRMA_USER, _FIRMA_PROOF])))

    assert raised.value.outcome == outcome


def test_groups_of_a_resolver_of_its_own_may_be_a_list() -> None:
    # Claims a resolver decodes from JSON arrive as lists; everything after
    # the check reads them like the tuple the dataclass declares.
    identity = VerifiedIdentity(subject="firma-1", groups=["staff"], roles=["editor"])

    resolved = asyncio.run(
        _guard(_FirmaResolver(identity)).resolve(_request([_FIRMA_USER, _FIRMA_PROOF]))
    )

    assert resolved is identity


@pytest.mark.parametrize(
    "header_names",
    [
        None,
        (),
        "X-Firma-User",
        ("X Firma",),
        ("X-Firma-User", "x-firma-user"),
        ("Authorization",),
        ("Cookie",),
        ("Origin",),
        ("X-CSRF-Token",),
        ("X-Inqtrix-Workspace-Id",),
        ("X-Inqtrix-Expected-User-Id",),
        ("X-Inqtrix-Guest-CSRF",),
        ("X-Inqtrix-Proof",),
        ("X-Forwarded-User",),
        ("X-Firma-\u212a",),
    ],
    ids=[
        "missing",
        "empty",
        "string",
        "not_a_token",
        "twice",
        "authorization",
        "cookie",
        "origin",
        "csrf",
        "workspace",
        "expected_user",
        "guest_csrf",
        "proof",
        "forwarded",
        "kelvin_sign",
    ],
)
def test_a_resolver_of_its_own_declares_only_headers_the_edge_may_strip(header_names) -> None:
    resolver = _Undeclared() if header_names is None else _FirmaResolver()
    if header_names is not None:
        resolver.header_names = header_names

    with pytest.raises(ValueError, match="header_names"):
        _guard(resolver)


def test_a_resolver_of_its_own_must_resolve_asynchronously() -> None:
    with pytest.raises(TypeError, match="coroutine"):
        _guard(_Synchronous())


class _CuriousResolver:
    """Records what the core lets it see."""

    header_names = ("X-Firma-User", "X-Firma-Mail")

    def __init__(self) -> None:
        self.calls = 0
        self.seen: tuple = ()

    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        self.calls += 1
        self.seen = (
            {name for name, _value in request.headers.items()},
            dict(request.cookies),
            dict(request.query_params),
            await request.body(),
        )
        return VerifiedIdentity(subject="firma-1")


def test_a_resolver_of_its_own_sees_only_its_declared_headers() -> None:
    inner = _CuriousResolver()
    request = _request(
        [_FIRMA_USER, _FIRMA_PROOF, ("Cookie", "inqtrix_session=s"), ("X-Firma-Groups", "admins")],
        query=b"user=other",
    )

    asyncio.run(_guard(inner).resolve(request))

    headers, cookies, query, body = inner.seen
    assert headers == {"x-firma-user"}
    assert (cookies, query, body) == ({}, {}, b"")


def test_a_doubled_second_declared_header_never_reaches_the_resolver() -> None:
    inner = _CuriousResolver()
    request = _request(
        [_FIRMA_USER, ("X-Firma-Mail", "a@example.com"), ("X-Firma-Mail", "b@example.com"), _FIRMA_PROOF]
    )

    with pytest.raises(GatewayAssertionError) as raised:
        asyncio.run(_guard(inner).resolve(request))

    assert raised.value.outcome == "duplicate_header"
    assert inner.calls == 0


@pytest.mark.parametrize("everyone", ["0.0.0.0/0", "::/0"])
def test_a_guard_never_trusts_every_address(everyone: str) -> None:
    with pytest.raises(ValueError, match="every address"):
        CustomResolverGuard(
            _FirmaResolver(),
            trusted_networks=[ip_network("10.0.0.0/8"), ip_network(everyone)],
            proof_header="X-Inqtrix-Proof",
            shared_secret="s" * 32,
        )

