"""Production ASGI server transport-limit contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_main_propagates_collaboration_limits_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uvicorn bounds frames before FastAPI materializes WebSocket input."""
    import inqtrix.__main__ as server_main

    app = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        server_main,
        "_SETTINGS",
        SimpleNamespace(
            collaboration=SimpleNamespace(
                max_frame_bytes=3 * 1_048_576,
                max_queued_frames=11,
            )
        ),
    )
    monkeypatch.setattr(server_main, "app", app)

    def run(received_app: object, **kwargs: object) -> None:
        captured["app"] = received_app
        captured["options"] = kwargs

    monkeypatch.setattr(server_main.uvicorn, "run", run)

    server_main.main()

    assert captured["app"] is app
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["ws_max_size"] == 3 * 1_048_576
    assert options["ws_max_queue"] == 11
