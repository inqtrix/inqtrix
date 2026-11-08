"""The engine replay notices a changed model input and sees every transport."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.request import Request, urlopen

import httpx
import httpx2
import pytest
import requests
from vcr.errors import CannotOverwriteExistingCassetteException

from inqtrix.providers.litellm import LiteLLM
from tests.replay.engine.body_matcher import normalized_body

pytestmark = pytest.mark.replay

_URL = "http://localhost:4000/canary"
_PROMPT = "Was ist Inqtrix?"


def _provider() -> LiteLLM:
    # Its SDK clients never retry a miss as a connection error (max_retries=0).
    return LiteLLM(
        api_key="test-key",
        base_url="http://localhost:4000/v1",
        default_model="gpt-4o",
    )


def _caused_by_miss(error: BaseException | None) -> bool:
    while error is not None:
        if isinstance(error, CannotOverwriteExistingCassetteException):
            return True
        error = error.__cause__ or error.__context__
    return False


@pytest.mark.vcr
@pytest.mark.default_cassette("body_match.yaml")
def test_the_same_model_input_replays() -> None:
    assert "recherchiert" in _provider().complete(_PROMPT)


@pytest.mark.vcr
@pytest.mark.default_cassette("body_match.yaml")
def test_a_changed_model_input_is_a_loud_miss() -> None:
    with pytest.raises(Exception) as caught:
        _provider().complete(_PROMPT + " Bitte kurz.")

    assert _caused_by_miss(caught.value)


def test_the_body_match_ignores_key_order_and_whitespace() -> None:
    first = SimpleNamespace(body=b'{"a": 1, "b": [2]}')
    second = SimpleNamespace(body='{"b":[2],"a":1}')
    other = SimpleNamespace(body='{"b":[3],"a":1}')

    assert normalized_body(first) == normalized_body(second)
    assert normalized_body(first) != normalized_body(other)


def _httpx() -> None:
    httpx.post(_URL, json={"canary": True}, timeout=1)


def _httpx_async() -> None:
    async def call() -> None:
        async with httpx.AsyncClient(timeout=1) as client:
            await client.post(_URL, json={"canary": True})

    asyncio.run(call())


def _httpx2() -> None:
    httpx2.post(_URL, json={"canary": True}, timeout=1)


def _httpx2_async() -> None:
    async def call() -> None:
        async with httpx2.AsyncClient(timeout=1) as client:
            await client.post(_URL, json={"canary": True})

    asyncio.run(call())


def _urllib() -> None:
    urlopen(Request(_URL, data=b'{"canary":true}', method="POST"), timeout=1)


def _requests() -> None:
    requests.post(_URL, json={"canary": True}, timeout=1)


@pytest.mark.vcr
@pytest.mark.default_cassette("no_recording.yaml")
@pytest.mark.parametrize(
    "send",
    [_httpx, _httpx_async, _httpx2, _httpx2_async, _urllib, _requests],
    ids=["httpx", "httpx_async", "httpx2", "httpx2_async", "urllib", "requests"],
)
def test_no_transport_can_leave_the_replay(send) -> None:
    """A transport VCR does not see would turn a miss into a live call."""
    with pytest.raises(CannotOverwriteExistingCassetteException):
        send()
