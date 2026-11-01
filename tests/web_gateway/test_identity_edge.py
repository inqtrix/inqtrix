"""The identity edge: which identity headers reach the API server, and from whom.

In the gateway mode the API gateway in front of Inqtrix asserts the user. The
web gateway passes duplicate headers through unchanged (the API server rejects
them), removes every configured identity, proof and assertion header that a
peer outside its list sends, and keeps a forwarded client chain only from a
peer in that list. It learns the mode from the API server and stays unready
when its own configuration cannot protect that mode.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import settings as gateway_settings
from inqtrix_web_gateway.headers import _proxy_websocket_headers
from starlette.websockets import WebSocket

from .support import _json_response

NAMES = ("X-Inqtrix-Assertion", "X-Inqtrix-Subject", "X-Inqtrix-Proof")
TRUSTED_PEER = ("10.1.2.3", 50_000)
STRANGER = ("192.0.2.10", 50_000)


def _policy(
    trusted: str = "10.1.0.0/16",
    names: tuple[str, ...] = NAMES,
    *,
    subject: str = "X-Inqtrix-Subject",
    proof: str = "X-Inqtrix-Proof",
    resolver: str = "",
):
    return gateway_settings.IdentityEdgePolicy(
        header_names=names,
        trusted_proxies=trusted,
        subject_header=subject,
        proof_header=proof,
        resolver_headers=resolver,
    )


_UNSET = object()
_OMIT = object()


class Backend:
    """A fake API server that records what reaches it."""

    def __init__(self, mode: str = "gateway", assertion: object = _UNSET) -> None:
        self.mode = mode
        if assertion is _UNSET:
            assertion = "jwt" if mode == "gateway" else None
        self.assertion = assertion
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/config":
            config = {"auth_mode": self.mode}
            if self.assertion is not _OMIT:
                config["gateway_assertion"] = self.assertion
            return _json_response(200, config)
        self.requests.append(request)
        return _json_response(200, {"status": "ok"})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def _client(
    fake_dist: Path,
    backend: Backend,
    *,
    policy=None,
    peer: tuple[str, int] = STRANGER,
    forwarded_allow_ips_set: bool = False,
) -> TestClient:
    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(backend),
        identity_policy=policy if policy is not None else _policy(),
        forwarded_allow_ips_set=forwarded_allow_ips_set,
    )
    return TestClient(app, client=peer)


# ------------------------------------------------------------------ #
# Duplicate and untrusted identity headers
# ------------------------------------------------------------------ #


def test_duplicate_identity_headers_reach_the_api_server_unfolded(fake_dist: Path) -> None:
    backend = Backend()

    with _client(fake_dist, backend, peer=TRUSTED_PEER) as client:
        client.get(
            "/v1/runs",
            headers=[("X-Inqtrix-Subject", "user-a"), ("X-Inqtrix-Subject", "user-b")],
        )

    assert backend.last.headers.get_list("x-inqtrix-subject") == ["user-a", "user-b"]


def test_identity_headers_from_an_untrusted_peer_never_reach_the_api_server(
    fake_dist: Path,
) -> None:
    backend = Backend()

    with _client(fake_dist, backend, peer=STRANGER) as client:
        client.get(
            "/v1/runs",
            headers={
                "X-Inqtrix-Assertion": "forged",
                "x-inqtrix-subject": "forged",
                "X-Inqtrix-Proof": "forged",
                "X-CSRF-Token": "kept",
            },
        )

    sent = {name.lower() for name, _value in backend.last.headers.multi_items()}
    assert not sent & {name.lower() for name in NAMES}
    assert backend.last.headers["x-csrf-token"] == "kept"


def test_identity_headers_from_a_trusted_peer_pass(fake_dist: Path) -> None:
    backend = Backend()

    with _client(fake_dist, backend, peer=TRUSTED_PEER) as client:
        client.get("/api/auth/session", headers={"X-Inqtrix-Assertion": "token"})

    assert backend.last.headers["x-inqtrix-assertion"] == "token"


def test_the_socket_peer_decides_not_the_forwarded_chain(fake_dist: Path) -> None:
    backend = Backend()

    with _client(fake_dist, backend, peer=STRANGER) as client:
        client.get(
            "/v1/runs",
            headers={"X-Inqtrix-Subject": "forged", "X-Forwarded-For": "10.1.2.3"},
        )

    assert "x-inqtrix-subject" not in backend.last.headers


def test_without_a_peer_list_nothing_is_removed(fake_dist: Path) -> None:
    # A token assertion carries its own proof; with no list the edge keeps it.
    backend = Backend()

    with _client(fake_dist, backend, policy=_policy(trusted=""), peer=STRANGER) as client:
        client.get("/api/auth/session", headers={"X-Inqtrix-Assertion": "token"})

    assert backend.last.headers["x-inqtrix-assertion"] == "token"


def test_removing_headers_is_reported_by_name_once_per_interval(
    fake_dist: Path, caplog: pytest.LogCaptureFixture
) -> None:
    backend = Backend()

    with caplog.at_level(logging.WARNING, logger="inqtrix.web_gateway"):
        with _client(fake_dist, backend, peer=STRANGER) as client:
            for _ in range(3):
                client.get("/v1/runs", headers={"X-Inqtrix-Subject": "zz-marker-value"})

    lines = [record.getMessage() for record in caplog.records if "untrusted_source" in record.getMessage()]
    assert len(lines) == 1
    assert "x-inqtrix-subject" in lines[0]
    assert "zz-marker-value" not in caplog.text


# ------------------------------------------------------------------ #
# The client chain in X-Forwarded-For
# ------------------------------------------------------------------ #


def test_in_gateway_mode_an_untrusted_peer_cannot_forward_a_chain(fake_dist: Path) -> None:
    backend = Backend(mode="gateway")

    with _client(fake_dist, backend, peer=STRANGER) as client:
        client.get("/v1/runs", headers={"X-Forwarded-For": "198.51.100.1"})

    assert backend.last.headers.get_list("x-forwarded-for") == ["192.0.2.10"]


def test_in_gateway_mode_a_trusted_peer_keeps_its_chain_unextended(fake_dist: Path) -> None:
    # The API gateway wrote the client on the right; the API server reads it
    # with one trusted hop, so nothing may be appended after it.
    backend = Backend(mode="gateway")

    with _client(fake_dist, backend, peer=TRUSTED_PEER) as client:
        client.get("/v1/runs", headers={"X-Forwarded-For": "203.0.113.7"})

    assert backend.last.headers.get_list("x-forwarded-for") == ["203.0.113.7"]


def test_in_gateway_mode_a_trusted_peer_without_a_chain_is_the_client(fake_dist: Path) -> None:
    backend = Backend(mode="gateway")

    with _client(fake_dist, backend, peer=TRUSTED_PEER) as client:
        client.get("/v1/runs")

    assert backend.last.headers.get_list("x-forwarded-for") == ["10.1.2.3"]


def test_outside_gateway_mode_the_chain_is_extended_as_before(fake_dist: Path) -> None:
    backend = Backend(mode="local")

    with _client(fake_dist, backend, peer=STRANGER) as client:
        client.get("/v1/runs", headers={"X-Forwarded-For": "198.51.100.1"})

    assert backend.last.headers.get_list("x-forwarded-for") == ["198.51.100.1, 192.0.2.10"]


# ------------------------------------------------------------------ #
# Readiness and the start line
# ------------------------------------------------------------------ #


def test_gateway_mode_without_identity_header_names_is_not_ready(fake_dist: Path) -> None:
    backend = Backend(mode="gateway")

    with _client(fake_dist, backend, policy=_policy(names=(), subject="", proof="")) as client:
        health = client.get("/health")
        ready = client.get("/readyz")

    assert health.status_code == 503 and ready.status_code == 503
    assert "INQTRIX_GATEWAY_" in health.json()["detail"]
    assert backend.requests == []


def test_gateway_mode_with_forwarded_allow_ips_is_not_ready(fake_dist: Path) -> None:
    backend = Backend(mode="gateway")

    with _client(fake_dist, backend, forwarded_allow_ips_set=True) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert "FORWARDED_ALLOW_IPS" in response.json()["detail"]


def test_gateway_mode_with_identity_header_names_is_ready(fake_dist: Path) -> None:
    backend = Backend(mode="gateway")

    with _client(fake_dist, backend) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert [request.url.path for request in backend.requests] == ["/health"]


def test_other_modes_stay_ready_without_identity_header_names(fake_dist: Path) -> None:
    backend = Backend(mode="local")

    with _client(fake_dist, backend, policy=_policy(names=(), trusted="", subject="", proof="")) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_unready_is_reported_once(fake_dist: Path, caplog: pytest.LogCaptureFixture) -> None:
    backend = Backend(mode="gateway")

    with caplog.at_level(logging.ERROR, logger="inqtrix.web_gateway"):
        with _client(fake_dist, backend, policy=_policy(names=(), subject="", proof="")) as client:
            client.get("/health")
            client.get("/health")

    assert len([record for record in caplog.records if record.levelno == logging.ERROR]) == 1


def test_the_header_assertion_needs_a_peer_list_at_the_edge(fake_dist: Path) -> None:
    backend = Backend(mode="gateway", assertion="header")

    with _client(fake_dist, backend, policy=_policy(trusted="")) as client:
        health = client.get("/health")
        ready = client.get("/readyz")

    assert health.status_code == 503 and ready.status_code == 503
    assert "INQTRIX_GATEWAY_TRUSTED_PROXIES" in health.json()["detail"]
    assert backend.requests == []


@pytest.mark.parametrize("missing", ["subject", "proof"])
def test_the_header_assertion_needs_the_subject_and_proof_names(
    fake_dist: Path, missing: str
) -> None:
    backend = Backend(mode="gateway", assertion="header")
    policy = _policy(**{missing: ""})

    with _client(fake_dist, backend, policy=policy) as client:
        response = client.get("/health")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "INQTRIX_GATEWAY_SUBJECT_HEADER" in detail and "INQTRIX_GATEWAY_PROOF_HEADER" in detail


def test_the_header_assertion_with_its_list_and_names_is_ready(fake_dist: Path) -> None:
    backend = Backend(mode="gateway", assertion="header")

    with _client(fake_dist, backend) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert [request.url.path for request in backend.requests] == ["/health"]


def test_the_token_assertion_stays_ready_without_a_peer_list(fake_dist: Path) -> None:
    backend = Backend(mode="gateway", assertion="jwt")

    with _client(fake_dist, backend, policy=_policy(trusted="")) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_the_edge_takes_the_assertion_from_the_api_server(fake_dist: Path) -> None:
    # A proof header name in the edge's own settings does not make it a header edge.
    backend = Backend(mode="gateway", assertion="jwt")
    policy = _policy(trusted="", proof="X-Inqtrix-Proof")

    with _client(fake_dist, backend, policy=policy) as client:
        response = client.get("/health")

    assert response.status_code == 200


@pytest.mark.parametrize("assertion", [_OMIT, None, "plugin"])
def test_a_gateway_mode_without_a_known_assertion_is_not_ready(
    fake_dist: Path, assertion: object
) -> None:
    backend = Backend(mode="gateway", assertion=assertion)

    with _client(fake_dist, backend) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert "gateway_assertion" in response.json()["detail"]


def test_the_start_line_names_keep_all_without_a_peer_list(
    fake_dist: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        with _client(fake_dist, Backend(), policy=_policy(trusted="")):
            pass

    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "identity_header_policy=keep_all" in line and "trusted_proxies=0" in line for line in lines
    )


@pytest.mark.parametrize("with_policy", [False, True], ids=["without", "with"])
def test_an_edge_built_without_a_policy_says_so(
    fake_dist: Path, caplog: pytest.LogCaptureFixture, with_policy: bool
) -> None:
    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=httpx.MockTransport(Backend(mode="local")),
        identity_policy=_policy() if with_policy else None,
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix.web_gateway"):
        with TestClient(app):
            pass

    warned = [record for record in caplog.records if "Identity edge without a policy" in record.getMessage()]
    assert len(warned) == (0 if with_policy else 1)


def test_the_start_line_names_the_identity_header_policy(
    fake_dist: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        with _client(fake_dist, Backend()):
            pass

    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "identity_header_policy=strip_untrusted" in line
        and "x-inqtrix-subject" in line
        and "trusted_proxies=1" in line
        for line in lines
    )


# ------------------------------------------------------------------ #
# The WebSocket handshake
# ------------------------------------------------------------------ #


def _websocket(headers: list[tuple[str, str]], peer: tuple[str, int] = STRANGER) -> WebSocket:
    scope = {
        "type": "websocket",
        "path": "/collaboration",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "query_string": b"",
        "client": peer,
        "scheme": "ws",
        "server": ("127.0.0.1", 8080),
    }

    async def receive():  # pragma: no cover - never awaited
        return {"type": "websocket.connect"}

    async def send(message):  # pragma: no cover - never awaited
        return None

    return WebSocket(scope, receive, send)


def test_identity_headers_never_reach_the_websocket_handshake() -> None:
    socket = _websocket(
        [("Host", "desk.example"), ("X-Inqtrix-Assertion", "token"), ("X-Inqtrix-Subject", "user-a")],
        peer=TRUSTED_PEER,
    )

    headers = dict(_proxy_websocket_headers(socket, None, None, gateway_mode=False, policy=_policy()))

    assert not {name.lower() for name in headers} & {name.lower() for name in NAMES}


@pytest.mark.parametrize(
    ("peer", "expected"),
    [(STRANGER, "192.0.2.10"), (TRUSTED_PEER, "203.0.113.7")],
    ids=["untrusted", "trusted"],
)
def test_in_gateway_mode_the_websocket_chain_follows_the_same_rule(peer, expected) -> None:
    socket = _websocket([("Host", "desk.example"), ("X-Forwarded-For", "203.0.113.7")], peer=peer)

    headers = dict(_proxy_websocket_headers(socket, None, None, gateway_mode=True, policy=_policy()))

    assert headers["x-forwarded-for"] == expected


# ------------------------------------------------------------------ #
# The policy
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("trusted", ["0.0.0.0/0", "::/0", "10.0.0.0/8, ::/0", "not-a-network"])
def test_the_peer_list_rejects_all_networks_and_garbage(trusted: str) -> None:
    with pytest.raises(ValueError, match="INQTRIX_GATEWAY_TRUSTED_PROXIES"):
        gateway_settings.IdentityEdgePolicy(header_names=NAMES, trusted_proxies=trusted)


def test_the_policy_reads_the_names_and_the_list_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INQTRIX_GATEWAY_JWT_HEADER", "X-Inqtrix-Assertion")
    monkeypatch.setenv("INQTRIX_GATEWAY_SUBJECT_HEADER", "X-Inqtrix-Subject")
    monkeypatch.setenv("INQTRIX_GATEWAY_PROOF_HEADER", "X-Inqtrix-Proof")
    monkeypatch.setenv("INQTRIX_GATEWAY_TRUSTED_PROXIES", "10.1.0.0/16")
    monkeypatch.setenv("INQTRIX_GATEWAY_SHARED_SECRET", "never-read-by-the-edge")

    policy = gateway_settings.IdentityEdgePolicy.from_env()

    assert policy.header_names == frozenset(
        {"x-inqtrix-assertion", "x-inqtrix-subject", "x-inqtrix-proof"}
    )
    assert (policy.subject_header, policy.proof_header) == ("x-inqtrix-subject", "x-inqtrix-proof")
    assert policy.trusts("10.1.2.3") and not policy.trusts("192.0.2.10")


def test_the_subject_and_proof_headers_are_always_removed_from_strangers() -> None:
    policy = gateway_settings.IdentityEdgePolicy(
        subject_header="X-Inqtrix-Subject", proof_header="X-Inqtrix-Proof", trusted_proxies="10.1.0.0/16"
    )

    assert policy.header_names == frozenset({"x-inqtrix-subject", "x-inqtrix-proof"})


def test_the_edge_never_reads_the_shared_secret() -> None:
    # The proof header is checked by the API server; the edge only knows names.
    package = Path(gateway_settings.__file__).parent
    sources = "".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))

    assert "SHARED_SECRET" not in sources


def test_the_retired_proxy_allow_list_reaches_the_edge(
    fake_dist: Path, clean_gateway_env: pytest.MonkeyPatch
) -> None:
    monkeypatch = clean_gateway_env
    captured: dict[str, object] = {}

    def build_app(*_args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(gateway_settings, "_resolve_dist_dir", lambda: fake_dist)
    monkeypatch.setattr(gateway_app, "build_app", build_app)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")

    gateway_app.create_app_from_env()

    assert captured["forwarded_allow_ips_set"] is True


# ------------------------------------------------------------------ #
# A resolver of its own
# ------------------------------------------------------------------ #

RESOLVER_HEADERS = "X-Firma-User,X-Firma-Groups"


def test_the_headers_of_a_resolver_of_its_own_never_reach_the_api_server_from_strangers(
    fake_dist: Path,
) -> None:
    backend = Backend(mode="gateway", assertion="resolver")

    with _client(fake_dist, backend, policy=_policy(resolver=RESOLVER_HEADERS), peer=STRANGER) as client:
        client.get(
            "/v1/runs",
            headers={"X-Firma-User": "forged", "X-Firma-Groups": "admins", "X-CSRF-Token": "kept"},
        )

    assert "x-firma-user" not in backend.last.headers
    assert "x-firma-groups" not in backend.last.headers
    assert backend.last.headers["x-csrf-token"] == "kept"


@pytest.mark.parametrize(
    ("policy", "variable"),
    [
        (lambda: _policy(trusted="", resolver=RESOLVER_HEADERS), "INQTRIX_GATEWAY_TRUSTED_PROXIES"),
        (lambda: _policy(proof="", resolver=RESOLVER_HEADERS), "INQTRIX_GATEWAY_PROOF_HEADER"),
        (lambda: _policy(), "INQTRIX_GATEWAY_RESOLVER_HEADERS"),
        (lambda: _policy(resolver="X-Firma-User,Cookie"), "INQTRIX_GATEWAY_RESOLVER_HEADERS"),
    ],
    ids=["no_peers", "no_proof_name", "no_resolver_list", "cookie_listed"],
)
def test_a_resolver_of_its_own_needs_its_duties_at_the_edge(
    fake_dist: Path, policy, variable: str
) -> None:
    backend = Backend(mode="gateway", assertion="resolver")

    with _client(fake_dist, backend, policy=policy()) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert variable in response.json()["detail"]
    assert backend.requests == []


def test_a_resolver_of_its_own_with_its_duties_is_ready(fake_dist: Path) -> None:
    backend = Backend(mode="gateway", assertion="resolver")

    with _client(fake_dist, backend, policy=_policy(resolver=RESOLVER_HEADERS)) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_a_listed_header_the_edge_must_pass_on_is_never_stripped() -> None:
    policy = gateway_settings.IdentityEdgePolicy(
        trusted_proxies="10.1.0.0/16",
        proof_header="X-Inqtrix-Proof",
        resolver_headers="X-Firma-User,Cookie,Origin",
    )

    assert "x-firma-user" in policy.header_names
    assert not {"cookie", "origin"} & policy.header_names
    assert "INQTRIX_GATEWAY_RESOLVER_HEADERS" in (policy.resolver_header_problem or "")


def test_the_edge_forbids_the_same_resolver_headers_as_the_api_server() -> None:
    from inqtrix.auth import gateway as server_gateway

    assert gateway_settings._RESOLVER_FORBIDDEN_HEADERS == (
        server_gateway._RESERVED_HEADER_NAMES | server_gateway._EDGE_PASSED_HEADER_NAMES
    )


def test_the_policy_reads_the_resolver_headers_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INQTRIX_GATEWAY_RESOLVER_HEADERS", "X-Firma-User, X-Firma-Groups")
    monkeypatch.setenv("INQTRIX_GATEWAY_TRUSTED_PROXIES", "10.1.0.0/16")

    policy = gateway_settings.IdentityEdgePolicy.from_env()

    assert policy.resolver_header_names == frozenset({"x-firma-user", "x-firma-groups"})
    assert policy.resolver_header_names <= policy.header_names


def test_the_edge_refuses_a_listed_name_that_is_no_token_as_written() -> None:
    # Lowered, a Kelvin sign would pass as an ASCII k; the rule applies to the name as written.
    policy = gateway_settings.IdentityEdgePolicy(
        trusted_proxies="10.1.0.0/16", proof_header="X-Inqtrix-Proof", resolver_headers="X-Firma-\u212a"
    )

    assert policy.resolver_header_names == frozenset()
    assert "INQTRIX_GATEWAY_RESOLVER_HEADERS" in (policy.resolver_header_problem or "")



def test_the_refusal_names_each_refused_entry_and_tells_no_header_name_apart() -> None:
    policy = _policy(resolver="X-Firma-User,Cookie,Origin,X-Firma-K")

    problem = policy.resolver_header_problem or ""
    assert "a header the edge passes on unchanged): Cookie, Origin" in problem
    assert "are no header name: 'X-Firma-\\u212a'" in problem
    assert "X-Firma-User" not in problem


# ------------------------------------------------------------------ #
# Identity variables naming a header the edge must leave alone
# ------------------------------------------------------------------ #


def test_an_identity_variable_naming_a_header_the_edge_passes_on_is_never_stripped(
    fake_dist: Path,
) -> None:
    # A stale or mistyped variable must not cost a guest the session cookie.
    backend = Backend(mode="gateway", assertion="header")
    policy = _policy(names=("X-Inqtrix-Assertion", "Cookie", "X-Inqtrix-Proof"), subject="Cookie")

    with _client(fake_dist, backend, policy=policy, peer=STRANGER) as client:
        client.get("/v1/runs", headers={"Cookie": "inqtrix_session=kept", "X-Inqtrix-Proof": "forged"})
        forwarded = backend.last
        health = client.get("/health")

    assert forwarded.headers["cookie"] == "inqtrix_session=kept"
    assert "x-inqtrix-proof" not in forwarded.headers
    assert health.status_code == 503
    detail = health.json()["detail"]
    assert "INQTRIX_GATEWAY_*_HEADER" in detail
    assert "never strips: Cookie" in detail


_SAMPLE_NAMES = (
    "X-Firma-User",
    "x-firma-groups",
    "Cookie",
    "AUTHORIZATION",
    "Host",
    "Proxy-Connection",
    "TE",
    "Origin",
    "X-CSRF-Token",
    "x-inqtrix-expected-user-id",
    "X-Inqtrix-Workspace-Id",
    "X-Inqtrix-Guest-CSRF",
    "X-Forwarded-For",
    "x-forwarded-user",
    "X-Inqtrix-Proof",
    "X-Firma-K",
    "X Firma",
    "X-Firma:User",
)


@pytest.mark.parametrize("name", _SAMPLE_NAMES)
def test_the_edge_refuses_a_name_exactly_when_the_api_server_does(name: str) -> None:
    from inqtrix.auth import gateway as server_gateway

    proof = "X-Inqtrix-Proof"
    kept, refused, invalid = gateway_settings._strippable([name], proof_header=proof)
    server = server_gateway._resolver_header_problems([name], source="LIST", proof_header=proof)
    assert bool(refused or invalid) == bool(server)
    assert bool(kept) != bool(server)

    kept, refused, invalid = gateway_settings._strippable([name])
    server = server_gateway._header_name_problems({"VARIABLE": name})
    assert bool(refused or invalid) == bool(server)
    assert bool(kept) != bool(server)


def test_no_header_the_edge_may_strip_passes_the_websocket_handshake() -> None:
    # The handshake passes only its allowlist, so every name on it must be one
    # no identity variable may name.
    from inqtrix_web_gateway.headers import _WEBSOCKET_FORWARD_HEADERS

    assert set(_WEBSOCKET_FORWARD_HEADERS) <= gateway_settings._RESOLVER_FORBIDDEN_HEADERS
