"""The gateway raises its soft open-file limit to the hard limit.

A stream through the gateway holds two descriptors, one to the browser and one
to the backend, so the default ceiling of 4096 upstream connections alone needs
more than 8192 open files; collaboration WebSockets and idle browser
connections come on top and have no ceiling. Container runtimes start the
gateway with different limits: Podman with 524,288, newer Docker runtimes with
a soft limit of 1024 below a high hard limit. Below the need, the gateway ran
out of descriptors long before its ceiling and nothing said why.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from inqtrix_web_gateway import open_files

from .support import _run_stubbed_main

NEED_4096 = 2 * 4096 + open_files._HEADROOM


def _limits(
    monkeypatch: pytest.MonkeyPatch, soft: int, hard: int, *, refuse: bool = False
) -> list[tuple[int, int]]:
    """Fake the process limits; the list records every limit set."""
    calls: list[tuple[int, int]] = []

    def setrlimit(kind: int, value: tuple[int, int]) -> None:
        if refuse:
            raise ValueError("not allowed")
        calls.append(value)

    monkeypatch.setattr(open_files.resource, "getrlimit", lambda kind: (soft, hard))
    monkeypatch.setattr(open_files.resource, "setrlimit", setrlimit)
    return calls


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.parametrize("soft", [1024, NEED_4096 + 1_000], ids=["below-the-need", "above-the-need"])
def test_the_soft_limit_is_raised_to_the_hard_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, soft: int
) -> None:
    """WebSockets and idle connections have no ceiling, so the need is no stopping point."""
    calls = _limits(monkeypatch, soft, 524_288)

    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        open_files.ensure_open_file_limit(4096)

    assert calls == [(524_288, 524_288)]
    assert _warnings(caplog) == []
    assert [record.getMessage() for record in caplog.records] == [
        f"Raised the open-file limit from {soft} to 524288."
    ]


def test_without_a_hard_limit_the_soft_limit_rises_to_the_need(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _limits(monkeypatch, 1024, open_files.resource.RLIM_INFINITY)

    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        open_files.ensure_open_file_limit(4096)

    assert calls == [(NEED_4096, open_files.resource.RLIM_INFINITY)]
    assert _warnings(caplog) == []


def test_a_limit_that_cannot_rise_logs_no_raise_and_names_the_need(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _limits(monkeypatch, 1024, 1024)

    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        open_files.ensure_open_file_limit(4096)

    assert calls == []
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert str(NEED_4096) in record.getMessage()


def test_a_hard_limit_below_the_need_is_taken_and_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _limits(monkeypatch, 1024, 4096)

    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        open_files.ensure_open_file_limit(4096)

    assert calls == [(4096, 4096)]
    [warning] = _warnings(caplog)
    assert "INQTRIX_MAX_UPSTREAM_CONNECTIONS" in warning
    assert str(NEED_4096) in warning


def test_a_limit_that_suffices_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _limits(monkeypatch, 524_288, 524_288)

    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        open_files.ensure_open_file_limit(4096)

    assert calls == []
    assert caplog.records == []


def test_a_refused_raise_is_reported_and_the_gateway_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _limits(monkeypatch, 1024, 524_288, refuse=True)

    with caplog.at_level(logging.INFO, logger="inqtrix.web_gateway"):
        open_files.ensure_open_file_limit(4096)

    [warning] = _warnings(caplog)
    assert "1024" in warning and str(NEED_4096) in warning


def test_the_gateway_sets_the_limit_for_its_ceiling_before_it_serves(
    fake_dist: Path, clean_gateway_env: pytest.MonkeyPatch
) -> None:
    clean_gateway_env.setenv("INQTRIX_MAX_UPSTREAM_CONNECTIONS", "500")

    captured = _run_stubbed_main(clean_gateway_env, fake_dist)

    assert captured["open_file_limit"] == 500
    assert captured["order"].index("open_file_limit") < captured["order"].index("uvicorn.run")
