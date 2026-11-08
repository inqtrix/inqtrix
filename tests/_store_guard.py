"""Show a sync store name on the loop while tests run on the in-memory stores.

A memory store has no I/O, so its sync name would simply work where a durable store
refuses. Most of the suite runs on memory stores, and each of those tests should
reveal what only a database otherwise would.
"""

from __future__ import annotations

import functools
import inspect
import sys
import threading
from collections.abc import Callable, Iterable
from typing import Any

from inqtrix import aio

_inline = threading.local()


def _guarded(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if (
            not getattr(_inline, "depth", 0)
            and caller.startswith("inqtrix.")
            and caller != method.__module__
        ):
            aio._assert_may_wait_for_loop()
        return method(self, *args, **kwargs)

    return guarded


def _marked(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def marked(self: Any, *args: Any, **kwargs: Any) -> Any:
        _inline.depth = getattr(_inline, "depth", 0) + 1
        try:
            return await method(self, *args, **kwargs)
        finally:
            _inline.depth -= 1

    return marked


def install(stores: Iterable[type]) -> None:
    """Guard every operation that has both names; called once per test session."""
    for store in stores:
        for name, member in list(vars(store).items()):
            if name.startswith("_") or not inspect.iscoroutinefunction(member):
                continue
            sync = vars(store).get(name[1:])
            if callable(sync):
                setattr(store, name, _marked(member))
                setattr(store, name[1:], _guarded(sync))
