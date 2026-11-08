"""The collaboration client keeps one pool of connections for as long as it runs."""

from __future__ import annotations

import asyncio

import httpx

from inqtrix.services.collaboration_client import CollaborationNodeClient


def _client() -> tuple[CollaborationNodeClient, list[str]]:
    requests: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"ready": True})

    return (
        CollaborationNodeClient(
            base_url="http://collaboration:1234",
            secret="s",
            transport=httpx.MockTransport(answer),
        ),
        requests,
    )


def test_requests_share_one_pool_of_connections() -> None:
    client, requests = _client()

    async def scenario() -> None:
        pool = client._open_client()
        await client.available()
        await client.available()
        assert client._open_client() is pool
        await client.aclose()

    asyncio.run(scenario())
    assert requests == ["/health/ready", "/health/ready"]


def test_a_lifespan_that_starts_again_gets_a_new_pool() -> None:
    client, requests = _client()

    async def scenario() -> None:
        await client.available()
        closed = client._open_client()
        await client.aclose()
        await client.available()
        assert client._open_client() is not closed
        await client.aclose()

    asyncio.run(scenario())
    assert requests == ["/health/ready", "/health/ready"]
