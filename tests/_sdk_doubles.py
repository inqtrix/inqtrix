"""Serve an async-native adapter's SDK calls from a test's sync client double."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any


def async_sdk(create: Any) -> Any:
    """An ``AsyncOpenAI`` double whose async ``chat.completions.create`` calls *create*.

    *create* is a test's sync double (a ``MagicMock`` or a function); its
    return values, side effects and call records stay where the test set them.
    A side effect that returns an awaitable is awaited on the loop, for tests
    whose concurrent calls have to meet there.
    """

    async def acreate(**kwargs: Any) -> Any:
        result = create(**kwargs)
        return await result if inspect.isawaitable(result) else result

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=acreate)))


def serve_sdk_calls(provider: Any, client: Any) -> Any:
    """Make *client* the SDK client of an adapter that calls ``AsyncOpenAI``.

    Azure and LiteLLM build one async SDK client per loop. These tests script
    a sync ``client.chat.completions.create`` and read its call records from
    ``provider._client``; every SDK call of the adapter reaches whatever
    ``provider._client`` holds at that moment.
    """
    provider._client = client
    sdk = async_sdk(lambda **kwargs: provider._client.chat.completions.create(**kwargs))
    provider._async_client = lambda: sdk
    return client


def async_responses_sdk(client: Any) -> Any:
    """An async SDK double whose ``responses.create`` calls *client*'s sync one.

    For the search adapters, which call the Responses API; an awaitable side
    effect is awaited on the loop, as in :func:`async_sdk`.
    """

    async def acreate(**kwargs: Any) -> Any:
        result = client.responses.create(**kwargs)
        return await result if inspect.isawaitable(result) else result

    return SimpleNamespace(responses=SimpleNamespace(create=acreate))



def async_double(fake: Any) -> Any:
    """An async stand-in for a provider's request coroutine that calls *fake*.

    *fake* is a test's sync double (a function, lambda or ``MagicMock``) for a
    method such as ``AnthropicLLM._arequest_json``; its return values, side
    effects and call records stay where the test set them. An awaitable result
    is awaited on the loop.
    """

    async def call(*args: Any, **kwargs: Any) -> Any:
        result = fake(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return call
