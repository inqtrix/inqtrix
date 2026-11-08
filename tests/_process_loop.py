"""Run a coroutine the way a process owner runs its loop."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from inqtrix import aio

T = TypeVar("T")


def run_on_configured_loop(coro: Coroutine[Any, Any, T], *, legacy_segment_workers: int = 16) -> T:
    """``asyncio.run`` with the lanes a worker's or an API's loop has."""

    async def main() -> T:
        loop = asyncio.get_running_loop()
        aio.configure_loop(
            loop,
            blocking_workers=16,
            provider_workers=16,
            legacy_segment_workers=legacy_segment_workers,
            watch=False,
        )
        try:
            return await coro
        finally:
            aio.close_loop(loop)

    return asyncio.run(main())
