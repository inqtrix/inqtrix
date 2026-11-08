"""What a test double of a store needs to stand in on both sides of the bridge."""

from __future__ import annotations

from typing import Any


class BothNames:
    """A double names each operation once; code on the loop finds it as ``aname`` too."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("a"):
            operation = getattr(self, name[1:], None)
            if callable(operation):

                async def on_the_loop(*args: Any, **kwargs: Any) -> Any:
                    return operation(*args, **kwargs)

                return on_the_loop
        raise AttributeError(name)
