"""A task holds at most one database connection.

All stores of a process share one pool. A task that opens a second session while
it holds a first one can wait for itself: with the pool full, several such tasks
each hold a connection and wait for another until the pool's timeout fails them.
Stores on engines of their own would hide this, so the guard watches every pool.

It sees one task. A second connection opened by a child task that the holder awaits
stays unseen, and so does the way through a transition lane and back onto the loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import greenlet
from sqlalchemy import event
from sqlalchemy.pool import Pool

_ROOT = f"{Path(__file__).resolve().parent.parent}/"

_held: dict[Any, tuple[asyncio.Task[Any], str]] = {}
_found: list[str] = []


def _where() -> str:
    """Where the task stands. SQLAlchemy runs this in a greenlet below its frames."""
    parent = greenlet.getcurrent().parent
    frame = parent.gr_frame if parent is not None else None
    while frame is not None:
        name = frame.f_code.co_filename
        if name.startswith(_ROOT) and "/.venv/" not in name and "storage/db.py" not in name:
            return f"{name[len(_ROOT):]}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back
    return "unknown"


def _on_checkout(dbapi_connection: Any, record: Any, proxy: Any) -> None:
    del dbapi_connection, proxy
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return
    if task is None:
        return
    where = _where()
    # The same record again is one connection: a listener that replaced a dead
    # connection restarts the listeners of that checkout.
    for held, (owner, first) in list(_held.items()):
        if owner is task and held is not record:
            _found.append(f"second connection at {where}, first at {first}")
            break
    _held[record] = (task, where)


def _on_checkin(dbapi_connection: Any, record: Any) -> None:
    del dbapi_connection
    _held.pop(record, None)


def install() -> None:
    event.listen(Pool, "checkout", _on_checkout)
    event.listen(Pool, "checkin", _on_checkin)


def reset() -> None:
    _held.clear()
    _found.clear()


def found() -> list[str]:
    return list(_found)
