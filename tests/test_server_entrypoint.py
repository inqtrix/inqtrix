"""Production ASGI server transport-limit contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _run_main(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Run ``main()`` with its seams stubbed; return the app and uvicorn options."""
    import inqtrix.__main__ as server_main

    app = object()
    order: list[str] = []
    captured: dict[str, object] = {"stub_app": app, "order": order}
    monkeypatch.setattr(
        server_main,
        "configure_logging",
        lambda **kwargs: SimpleNamespace(
            service="api", target="console", level="INFO", format="json", file_path=None
        ),
    )
    monkeypatch.setattr(server_main, "check_retired_settings", lambda **kwargs: [])
    monkeypatch.setattr(server_main, "log_status_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server_main,
        "Settings",
        lambda: SimpleNamespace(
            collaboration=SimpleNamespace(
                max_frame_bytes=3 * 1_048_576,
                max_queued_frames=11,
            )
        ),
    )

    def create_app(settings: object) -> object:
        order.append("create_app")
        return app

    monkeypatch.setattr(server_main, "create_app", create_app)
    monkeypatch.setattr(server_main, "freeze_startup_heap", lambda: order.append("freeze"))

    def run(received_app: object, **kwargs: object) -> None:
        order.append("run")
        captured["app"] = received_app
        captured["options"] = kwargs

    monkeypatch.setattr(server_main.uvicorn, "run", run)

    server_main.main()
    return captured


def test_main_keeps_what_it_built_out_of_later_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spared from every full collection: the composition, not what comes after it."""
    captured = _run_main(monkeypatch)

    assert captured["order"] == ["create_app", "freeze", "run"]


def test_main_propagates_collaboration_limits_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uvicorn bounds frames before FastAPI materializes WebSocket input."""
    captured = _run_main(monkeypatch)

    assert captured["app"] is captured["stub_app"]
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["ws_max_size"] == 3 * 1_048_576
    assert options["ws_max_queue"] == 11


def test_main_runs_uvicorn_without_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inqtrix evaluates X-Forwarded-For itself (trusted proxy hops); uvicorn
    # rewriting the client address and scheme from a loopback peer would let
    # a forwarded value replace the socket peer behind the server's back.
    captured = _run_main(monkeypatch)

    options = captured["options"]
    assert isinstance(options, dict)
    assert options["proxy_headers"] is False


def test_every_example_stack_runs_uvicorn_without_proxy_headers() -> None:
    # The example stacks are a documented way to start the API server; they
    # must not let uvicorn take the client address from forwarded headers.
    root = Path(__file__).resolve().parent.parent / "examples" / "webserver_stacks"
    starts = [
        path for path in sorted(root.glob("*.py")) if "uvicorn.run(" in path.read_text(encoding="utf-8")
    ]

    assert starts
    missing = [
        path.name
        for path in starts
        if 'uvicorn_kwargs["proxy_headers"] = False' not in path.read_text(encoding="utf-8")
    ]
    assert missing == []

