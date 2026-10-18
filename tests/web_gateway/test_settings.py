"""Regression tests for gateway settings responsibility."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import settings as gateway_settings

from .support import _json_response

@pytest.mark.parametrize(
    "public_base_url",
    [
        "desk.example",
        "ftp://desk.example",
        "https://user@desk.example",
        "https://desk.example/path",
        "https://desk.example?tenant=a",
        "https://bad host.example",
    ],
)
def test_public_base_url_rejects_non_origin_values(
    fake_dist: Path,
    public_base_url: str,
) -> None:
    """TLS forwarding cannot be configured from an ambiguous URL."""
    with pytest.raises(ValueError, match="INQTRIX_PUBLIC_BASE_URL"):
        gateway_app.build_app(
            fake_dist,
            "http://backend.invalid",
            public_base_url=public_base_url,
        )

def test_resolve_dist_dir_missing_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing dist/ directory triggers a loud RuntimeError."""
    monkeypatch.setenv("INQTRIX_DIST_DIR", str(tmp_path / "does-not-exist"))
    with pytest.raises(RuntimeError, match="dist/ not found"):
        gateway_settings._resolve_dist_dir()

def test_resolve_dist_dir_explicit_path(
    fake_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit INQTRIX_DIST_DIR overrides the repository default."""
    monkeypatch.setenv("INQTRIX_DIST_DIR", str(fake_dist))
    resolved = gateway_settings._resolve_dist_dir()
    assert resolved == fake_dist.resolve()


def test_resolve_dist_dir_requires_a_readable_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty directory is not a deployable SPA build."""
    empty_dist = tmp_path / "dist"
    empty_dist.mkdir()
    monkeypatch.setenv("INQTRIX_DIST_DIR", str(empty_dist))

    with pytest.raises(RuntimeError, match="dist/index.html"):
        gateway_settings._resolve_dist_dir()


@pytest.mark.parametrize("external_scheme", ["ftp", "HTTPS://x", "ws", "wss"])
def test_external_scheme_rejects_invalid_values(
    fake_dist: Path,
    external_scheme: str,
) -> None:
    """Only http and https are valid forwarded-scheme overrides."""
    with pytest.raises(ValueError, match="INQTRIX_EXTERNAL_SCHEME"):
        gateway_app.build_app(
            fake_dist,
            "http://backend.invalid",
            external_scheme=external_scheme,
        )

def test_public_base_url_and_matching_external_scheme_share_one_contract(
    fake_dist: Path,
) -> None:
    """A redundant but consistent scheme never creates a warning path."""
    received: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["proto"] = request.headers.get("x-forwarded-proto")
        received["forwarded_host"] = request.headers.get("x-forwarded-host")
        return _json_response(200, {})

    transport = httpx.MockTransport(handler)
    app = gateway_app.build_app(
        fake_dist,
        "http://backend.invalid",
        transport=transport,
        public_base_url="https://desk.example",
        external_scheme="https",
    )

    with TestClient(app) as client:
        client.get("/v1/runs", headers={"Host": "gateway.internal:8080"})

    assert received["proto"] == "https"
    assert received["forwarded_host"] == "desk.example"


def test_public_base_url_rejects_a_conflicting_external_scheme(
    fake_dist: Path,
) -> None:
    """Two public-boundary values may not describe different origins."""
    with pytest.raises(
        ValueError,
        match="must match the scheme in INQTRIX_PUBLIC_BASE_URL",
    ):
        gateway_app.build_app(
            fake_dist,
            "http://backend.invalid",
            public_base_url="https://desk.example",
            external_scheme="http",
        )


def test_request_body_limit_tracks_max_file_bytes(
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """The derived cap follows INQTRIX_MAX_FILE_BYTES plus fixed headroom.

    Uses a value distinct from the packaged default so the test cannot stay
    green through the default-equals-default trap after a revert.
    """
    clean_gateway_env.setenv("INQTRIX_MAX_FILE_BYTES", "5000000")
    resolved = gateway_settings._resolve_proxy_max_body_bytes()
    assert resolved == 5_000_000 + 10 * 1024 * 1024

def test_missing_max_file_bytes_warns_visibly(
    clean_gateway_env: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without the backend variable, the guessed cap is announced loudly."""
    del clean_gateway_env
    with caplog.at_level("WARNING", logger="inqtrix.web_gateway"):
        resolved = gateway_settings._resolve_proxy_max_body_bytes()
    assert resolved is None
    assert "INQTRIX_MAX_FILE_BYTES" in caplog.text

def test_proxy_max_body_bytes_override_wins(
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """The explicit proxy cap beats the derivation from the backend limit."""
    clean_gateway_env.setenv("INQTRIX_PROXY_MAX_BODY_BYTES", "123456")
    clean_gateway_env.setenv("INQTRIX_MAX_FILE_BYTES", "5000000")
    assert gateway_settings._resolve_proxy_max_body_bytes() == 123_456

def test_removed_nginx_body_size_alias_is_ignored(
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """The gateway has one byte-valued request-size configuration contract."""
    clean_gateway_env.setenv("INQTRIX_CLIENT_MAX_BODY_SIZE", "110m")
    clean_gateway_env.setenv("INQTRIX_MAX_FILE_BYTES", "5000000")
    assert (
        gateway_settings._resolve_proxy_max_body_bytes()
        == 5_000_000 + 10 * 1024 * 1024
    )

def test_backend_url_uses_only_the_canonical_contract(
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """Removed aliases cannot silently create a second configuration path."""
    clean_gateway_env.setenv("INQTRIX_BACKEND_URL", "http://canonical:5100/")
    assert gateway_settings._resolve_backend_url() == "http://canonical:5100"


@pytest.mark.parametrize(
    "backend_url",
    [
        "backend.internal:5100",
        "ftp://backend.internal",
        "http://user:password@backend.internal:5100",
        "http://backend.internal:5100/path",
        "http://backend.internal:5100?tenant=a",
        "http://backend.internal:5100#fragment",
        "http://bad host:5100",
    ],
)
def test_backend_url_rejects_non_origin_and_credential_values(
    fake_dist: Path,
    backend_url: str,
) -> None:
    """Every adapter receives one unambiguous credential-free origin."""
    with pytest.raises(ValueError, match="INQTRIX_BACKEND_URL"):
        gateway_app.build_app(fake_dist, backend_url)


def test_backend_url_normalizes_default_port_and_ipv6_origin(
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """Canonicalization is deterministic without losing IPv6 brackets."""
    clean_gateway_env.setenv(
        "INQTRIX_BACKEND_URL",
        "HTTPS://[2001:db8::1]:443/",
    )
    assert (
        gateway_settings._resolve_backend_url()
        == "https://[2001:db8::1]"
    )


def test_backend_validation_never_echoes_credential_input(
    fake_dist: Path,
) -> None:
    """Startup failures cannot disclose rejected backend userinfo."""
    credential = "SYNTHETIC-BACKEND-CREDENTIAL"
    with pytest.raises(ValueError) as captured:
        gateway_app.build_app(
            fake_dist,
            f"http://runtime:{credential}@backend.internal:5100",
        )
    assert credential not in str(captured.value)


def test_removed_api_upstream_alias_is_ignored(
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """The nginx-era host/port alias is not a compatibility interface."""
    clean_gateway_env.setenv("INQTRIX_API_UPSTREAM", "api:5100")
    assert gateway_settings._resolve_backend_url() == "http://localhost:5100"
