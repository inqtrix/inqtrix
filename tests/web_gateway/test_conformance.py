"""Regression tests for gateway conformance responsibility."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from inqtrix.settings import CollaborationSettings, StorageSettings
from inqtrix_web_gateway import app as gateway_app
from inqtrix_web_gateway import cli as gateway_cli
from inqtrix_web_gateway import settings as gateway_settings

from .support import _run_stubbed_main

def test_main_propagates_non_default_collaboration_transport_limits(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """One environment value configures both relay and browser parser limits.

    The stub replicates the exact keyword-only ``build_app`` signature so
    this test breaks loudly whenever the ``main()`` call site gains or
    loses a parameter without the propagation being asserted here.
    """
    monkeypatch = clean_gateway_env
    configured_frame_bytes = 3 * 1_048_576
    configured_queue = 9
    app = object()
    captured: dict[str, object] = {}

    def build_app(
        dist_dir: Path,
        backend_url: str,
        *,
        collaboration_settings: Any,
        public_base_url: str | None,
        external_scheme: str | None,
        max_request_bytes: int | None,
        max_upstream_connections: int | None,
    ) -> object:
        captured["dist_dir"] = dist_dir
        captured["backend_url"] = backend_url
        captured["app_frame_limit"] = collaboration_settings.max_frame_bytes
        captured["app_queue_limit"] = collaboration_settings.max_queued_frames
        captured["public_base_url"] = public_base_url
        captured["external_scheme"] = external_scheme
        captured["max_request_bytes"] = max_request_bytes
        captured["max_upstream_connections"] = max_upstream_connections
        return app

    def run(received_app: object, **kwargs: object) -> None:
        captured["uvicorn_app"] = received_app
        captured["uvicorn_options"] = kwargs

    monkeypatch.setenv(
        "INQTRIX_COLLABORATION_MAX_FRAME_BYTES",
        str(configured_frame_bytes),
    )
    monkeypatch.setenv(
        "INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES",
        str(configured_queue),
    )
    monkeypatch.setenv("INQTRIX_COLLABORATION_ENABLED", "false")
    monkeypatch.setenv("INQTRIX_PUBLIC_BASE_URL", "https://desk.example")
    monkeypatch.setenv("INQTRIX_EXTERNAL_SCHEME", "https")
    monkeypatch.setenv("INQTRIX_MAX_UPSTREAM_CONNECTIONS", "321")
    monkeypatch.setenv("INQTRIX_MAX_FILE_BYTES", "5000000")
    monkeypatch.setattr(gateway_settings, "_resolve_dist_dir", lambda: fake_dist)
    monkeypatch.setattr(gateway_app, "build_app", build_app)
    monkeypatch.setattr(gateway_cli.uvicorn, "run", run)

    gateway_cli.main([])

    assert captured["app_frame_limit"] == configured_frame_bytes
    assert captured["app_queue_limit"] == configured_queue
    assert captured["public_base_url"] == "https://desk.example"
    assert captured["external_scheme"] == "https"
    assert captured["max_request_bytes"] == 5_000_000 + 10 * 1024 * 1024
    assert captured["max_upstream_connections"] == 321
    assert captured["uvicorn_app"] is app
    assert (
        captured["uvicorn_options"]["ws_max_size"]
        == configured_frame_bytes
    )
    assert captured["uvicorn_options"]["ws_max_queue"] == configured_queue
    assert captured["uvicorn_options"]["workers"] == 1

def test_gateway_transport_defaults_match_canonical_settings() -> None:
    """The dependency-light gateway cannot silently drift from API bounds."""
    canonical = CollaborationSettings.model_fields
    frame_metadata = canonical["max_frame_bytes"].metadata
    queue_metadata = canonical["max_queued_frames"].metadata
    assert gateway_settings._DEFAULT_MAX_FILE_BYTES == int(
        StorageSettings.model_fields["max_file_bytes"].default
    )
    assert gateway_settings._COLLABORATION_DEFAULT_FRAME_BYTES == int(
        canonical["max_frame_bytes"].default
    )
    assert gateway_settings._COLLABORATION_DEFAULT_QUEUED_FRAMES == int(
        canonical["max_queued_frames"].default
    )
    assert gateway_settings._COLLABORATION_MIN_FRAME_BYTES == int(
        next(item.ge for item in frame_metadata if hasattr(item, "ge"))
    )
    assert gateway_settings._COLLABORATION_MAX_FRAME_BYTES == int(
        next(item.le for item in frame_metadata if hasattr(item, "le"))
    )
    assert gateway_settings._COLLABORATION_MIN_QUEUED_FRAMES == int(
        next(item.ge for item in queue_metadata if hasattr(item, "ge"))
    )
    assert gateway_settings._COLLABORATION_MAX_QUEUED_FRAMES == int(
        next(item.le for item in queue_metadata if hasattr(item, "le"))
    )

def test_main_passes_workers_explicitly_default_one(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WEB_CONCURRENCY cannot flip uvicorn into the multiprocess path.

    uvicorn derives ``workers`` from ``WEB_CONCURRENCY`` only when the
    argument is omitted, and then exits at startup because the gateway
    passes an app instance. Passing ``workers=1`` explicitly makes
    ``RESEARCH_DESK_WORKERS`` the only concurrency knob — and ignoring the
    platform variable must be visible, not silent.
    """
    clean_gateway_env.setenv("WEB_CONCURRENCY", "4")
    with caplog.at_level("WARNING", logger="inqtrix.web_gateway"):
        captured = _run_stubbed_main(clean_gateway_env, fake_dist)

    assert captured["uvicorn_options"]["workers"] == 1
    assert captured["uvicorn_app"] is captured["stub_app"]
    assert "WEB_CONCURRENCY=4 is ignored" in caplog.text

def test_cli_options_override_environment(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """Explicit command-line values win over environment configuration."""
    clean_gateway_env.setenv("INQTRIX_DIST_DIR", "/environment/dist")
    clean_gateway_env.setenv("INQTRIX_BACKEND_URL", "http://environment:5100")
    clean_gateway_env.setenv("RESEARCH_DESK_HOST", "127.0.0.2")
    clean_gateway_env.setenv("RESEARCH_DESK_PORT", "8081")

    captured = _run_stubbed_main(
        clean_gateway_env,
        fake_dist,
        (
            "--dist-dir",
            str(fake_dist),
            "--backend-url",
            "http://command-line:5100/",
            "--host",
            "0.0.0.0",
            "--port",
            "9090",
        ),
    )

    assert captured["build_app_args"][1] == "http://command-line:5100"
    assert captured["uvicorn_options"]["host"] == "0.0.0.0"
    assert captured["uvicorn_options"]["port"] == 9090
    assert gateway_cli.os.environ["INQTRIX_DIST_DIR"] == str(fake_dist)

def test_environment_options_override_packaged_defaults(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """Environment values remain effective when the CLI omits an option."""
    clean_gateway_env.setenv("INQTRIX_BACKEND_URL", "http://environment:5200")
    clean_gateway_env.setenv("RESEARCH_DESK_HOST", "127.0.0.2")
    clean_gateway_env.setenv("RESEARCH_DESK_PORT", "8181")

    captured = _run_stubbed_main(clean_gateway_env, fake_dist)

    assert captured["build_app_args"][1] == "http://environment:5200"
    assert captured["uvicorn_options"]["host"] == "127.0.0.2"
    assert captured["uvicorn_options"]["port"] == 8181

@pytest.mark.parametrize("port", ("0", "65536"))
def test_cli_port_rejects_out_of_range_values(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
    port: str,
) -> None:
    """The CLI and environment share one validated TCP-port contract."""
    clean_gateway_env.setattr(gateway_settings, "_resolve_dist_dir", lambda: fake_dist)
    run_calls: list[object] = []
    clean_gateway_env.setattr(
        gateway_cli.uvicorn, "run", lambda *a, **k: run_calls.append(a)
    )

    with pytest.raises(ValueError, match="between 1 and 65535"):
        gateway_cli.main(["--port", port])

    assert run_calls == []

def test_main_multi_worker_uses_import_string_factory(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """Multi-worker mode validates in the parent, then spawns via factory.

    The parent must build (and discard) the app before ``uvicorn.run`` so a
    static misconfiguration fails loud and terminal instead of crash-looping
    the multiprocess supervisor, and the run call must reference the factory
    as an import string because uvicorn refuses ``workers>1`` with an app
    instance.
    """
    clean_gateway_env.setenv("RESEARCH_DESK_WORKERS", "3")
    captured = _run_stubbed_main(clean_gateway_env, fake_dist)

    assert captured["order"] == ["build_app", "uvicorn.run"]
    assert (
        captured["uvicorn_app"]
        == "inqtrix_web_gateway.app:create_app_from_env"
    )
    assert captured["uvicorn_options"]["factory"] is True
    assert captured["uvicorn_options"]["workers"] == 3
    assert "app_dir" not in captured["uvicorn_options"]

def test_multi_worker_parent_validates_before_spawn(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing dist/ aborts the parent; the supervisor never starts."""
    del fake_dist
    clean_gateway_env.setenv("RESEARCH_DESK_WORKERS", "2")
    clean_gateway_env.setenv(
        "INQTRIX_DIST_DIR", str(tmp_path / "does-not-exist")
    )
    run_calls: list[object] = []
    clean_gateway_env.setattr(
        gateway_cli.uvicorn, "run", lambda *a, **k: run_calls.append(a)
    )

    with pytest.raises(RuntimeError, match="dist/ not found"):
        gateway_cli.main([])

    assert run_calls == []

def test_create_app_from_env_builds_app(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """The factory resolves the environment into a serving application."""
    clean_gateway_env.setenv("INQTRIX_DIST_DIR", str(fake_dist))
    app = gateway_app.create_app_from_env()

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "fake-react-app" in response.text


def test_python_image_rejects_nginx_adapter_sentinel(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """A mismatched Compose override fails before binding a public socket."""
    clean_gateway_env.setenv("INQTRIX_DIST_DIR", str(fake_dist))
    clean_gateway_env.setenv("INQTRIX_WEB_ADAPTER", "nginx")

    with pytest.raises(
        ValueError,
        match="INQTRIX_WEB_ADAPTER=python",
    ):
        gateway_app.create_app_from_env()


@pytest.mark.parametrize(
    ("raw_value", "match"),
    [
        ("0", "RESEARCH_DESK_WORKERS must be a positive integer"),
        ("-2", "RESEARCH_DESK_WORKERS must be a positive integer"),
        ("abc", "RESEARCH_DESK_WORKERS must be an integer"),
    ],
)
def test_workers_rejects_invalid_configuration(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
    raw_value: str,
    match: str,
) -> None:
    """Invalid worker counts fail loudly with the variable name.

    ``uvicorn.run`` is stubbed so a validation regression fails red instead
    of actually starting a server and hanging the suite.
    """
    clean_gateway_env.setenv("RESEARCH_DESK_WORKERS", raw_value)
    clean_gateway_env.setattr(gateway_settings, "_resolve_dist_dir", lambda: fake_dist)
    run_calls: list[object] = []
    clean_gateway_env.setattr(
        gateway_cli.uvicorn, "run", lambda *a, **k: run_calls.append(a)
    )

    with pytest.raises(ValueError, match=match):
        gateway_cli.main([])

    assert run_calls == []

def test_main_passes_ssl_kwargs(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """Configured TLS material reaches uvicorn unchanged."""
    clean_gateway_env.setenv("RESEARCH_DESK_SSL_CERTFILE", "/etc/tls/tls.crt")
    clean_gateway_env.setenv("RESEARCH_DESK_SSL_KEYFILE", "/etc/tls/tls.key")
    clean_gateway_env.setenv("RESEARCH_DESK_SSL_KEYFILE_PASSWORD", "pw")
    captured = _run_stubbed_main(clean_gateway_env, fake_dist)

    options = captured["uvicorn_options"]
    assert options["ssl_certfile"] == "/etc/tls/tls.crt"
    assert options["ssl_keyfile"] == "/etc/tls/tls.key"
    assert options["ssl_keyfile_password"] == "pw"

@pytest.mark.parametrize(
    "present", ["RESEARCH_DESK_SSL_CERTFILE", "RESEARCH_DESK_SSL_KEYFILE"]
)
def test_ssl_certfile_without_keyfile_raises(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
    present: str,
) -> None:
    """A half-configured TLS pair fails loudly instead of serving HTTP."""
    clean_gateway_env.setenv(present, "/etc/tls/only-one")
    clean_gateway_env.setattr(gateway_settings, "_resolve_dist_dir", lambda: fake_dist)

    with pytest.raises(ValueError, match="must be set together"):
        gateway_cli.main([])

def test_no_ssl_env_passes_no_ssl_kwargs(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
) -> None:
    """Without TLS env the uvicorn call carries no ssl arguments."""
    captured = _run_stubbed_main(clean_gateway_env, fake_dist)

    assert "ssl_certfile" not in captured["uvicorn_options"]
    assert "ssl_keyfile" not in captured["uvicorn_options"]

def test_multi_worker_validates_tls_material_in_parent(
    fake_dist: Path,
    clean_gateway_env: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Broken TLS material aborts the parent; the supervisor never starts.

    uvicorn loads the SSL context only inside the worker children, so
    without the parent probe a missing certificate file would crash-loop
    the multiprocess supervisor indefinitely instead of failing terminal.
    """
    clean_gateway_env.setenv("RESEARCH_DESK_WORKERS", "2")
    clean_gateway_env.setenv(
        "RESEARCH_DESK_SSL_CERTFILE", str(tmp_path / "missing.crt")
    )
    clean_gateway_env.setenv(
        "RESEARCH_DESK_SSL_KEYFILE", str(tmp_path / "missing.key")
    )
    clean_gateway_env.setattr(gateway_settings, "_resolve_dist_dir", lambda: fake_dist)
    run_calls: list[object] = []
    clean_gateway_env.setattr(
        gateway_cli.uvicorn, "run", lambda *a, **k: run_calls.append(a)
    )

    with pytest.raises(OSError):
        gateway_cli.main([])

    assert run_calls == []
