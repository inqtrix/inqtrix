"""A job queue keeps one client, and a closed queue opens a new one when used again."""

from __future__ import annotations

import asyncio
import sys
import types

from inqtrix.runs.valkey_queue import ValkeyRunQueue


class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_one_client_until_it_is_closed(monkeypatch) -> None:
    built: list[_Client] = []

    def from_url(_url: str, **_settings: object) -> _Client:
        built.append(_Client())
        return built[-1]

    monkeypatch.setattr("valkey.asyncio.Valkey.from_url", from_url)
    queue = ValkeyRunQueue(url="redis://valkey:6379/0")

    async def scenario() -> None:
        first = queue._client
        assert queue._client is first
        await queue.aclose()
        assert first.closed
        assert queue._client is not first
        await queue.aclose()

    asyncio.run(scenario())
    assert len(built) == 2


def test_an_injected_client_stays_with_whoever_injected_it() -> None:
    injected = _Client()
    queue = ValkeyRunQueue(url="", client=injected)

    asyncio.run(queue.aclose())

    assert queue._client is injected
    assert not injected.closed


def test_a_queue_imports_its_client_when_built_not_on_first_use(monkeypatch) -> None:
    """The worker builds its queue before its loop runs; a first use on the loop imports nothing."""
    built: list[str] = []

    class _Valkey:
        @classmethod
        def from_url(cls, url: str, **_settings: object) -> _Client:
            built.append(url)
            return _Client()

    monkeypatch.setitem(sys.modules, "valkey.asyncio", types.SimpleNamespace(Valkey=_Valkey))
    queue = ValkeyRunQueue(url="redis://valkey:6379/0")
    # From here an import of the client package would fail.
    monkeypatch.setitem(sys.modules, "valkey.asyncio", None)

    assert isinstance(queue._client, _Client)
    assert built == ["redis://valkey:6379/0"]
