"""Named thread lanes for the blocking work the HTTP server offloads.

Several kinds of blocking work share the API process, and they behave nothing
alike. An AI call occupies its thread for the whole request — minutes, for a
research answer. A stream reader blocks on a queue, wakes, and re-arms for as
long as a viewer watches. Everything else is a short read that wants its thread
back immediately.

Left in one pool they starve each other: enough concurrent AI calls and no
thread remains for a stream reader to deliver an event or for a metrics scrape
to answer, even though the event loop is idle the whole time. Worse, the pool
that would absorb them is sized ``min(32, os.cpu_count() + 4)`` — a number
derived from the machine's cores, which in a container is not the CPU budget
the process actually has.

So the two long-lived kinds get their own named lane, which leaves the default
pool to the short reads — with one exception worth knowing: the per-user
invalidation stream still parks a default-pool thread per connected client when
the in-memory user-event store is in use (the Postgres store waits on the event
loop instead and costs nothing here).

``ai``
    Slots for concurrent AI calls. Admission bounds how many may START
    (``MAX_CONCURRENT``), but not how many are still RUNNING: a client that
    navigates away closes the streaming generator, which releases its
    admission slot while the thread it started keeps working until the
    algorithm reaches its next cancellation checkpoint. Sizing the lane to
    the admission ceiling exactly would let those orphans crowd out requests
    that admission has just accepted, so the lane carries a full extra
    generation of headroom.

``streams``
    One slot per open event stream while it waits for its next event. Sized
    separately because the number of viewers is not bounded by the number of
    runs — several people may watch the same run, and indexing streams are
    counted here too.

``titles``
    Two slots for optional short conversation labels. They neither consume
    answer admission permits nor occupy the answer pool. Threads start lazily.

A blocked thread is cheap: parked on a lock it holds a few kilobytes and does
not take the interpreter lock. Sizing a lane generously costs far less than
letting one class of work displace another.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from inqtrix.constants import SESSION_TITLE_CONCURRENCY


class ExecutionLanes:
    """Owns the named thread pools and disposes them on shutdown."""

    def __init__(self, *, ai_workers: int, stream_workers: int) -> None:
        """Create answer, stream and optional-title lanes.

        Args:
            ai_workers: Slots for concurrent AI calls. Pass more than the
                admission ceiling: a call whose admission slot was released
                on client disconnect keeps its thread until it reaches a
                cancellation checkpoint.
            stream_workers: Slots for event-stream readers. Each open stream
                holds one while waiting; beyond this count readers are served
                in rotation, which delays events rather than dropping them.

        Raises:
            ValueError: Either size is below one.
        """
        if ai_workers < 1 or stream_workers < 1:
            raise ValueError(
                "Execution lanes need at least one worker each "
                f"(ai_workers={ai_workers}, stream_workers={stream_workers})."
            )
        self.ai = ThreadPoolExecutor(
            max_workers=ai_workers, thread_name_prefix="inqtrix-ai"
        )
        self.streams = ThreadPoolExecutor(
            max_workers=stream_workers, thread_name_prefix="inqtrix-stream"
        )
        # Optional labels must not queue ahead of the user's actual answer,
        # including installations with a single admitted AI request.
        self.titles = ThreadPoolExecutor(
            max_workers=SESSION_TITLE_CONCURRENCY, thread_name_prefix="inqtrix-title"
        )

    def close(self) -> None:
        """Release all lanes without waiting for in-flight work.

        Shutdown runs on the event loop, so waiting here would block every
        other teardown step behind whatever AI call happens to be running.
        Queued work is cancelled; work already running is not interrupted,
        and the interpreter joins those workers at exit — so a long AI call
        still delays process exit, just not the teardown sequence.
        """
        self.ai.shutdown(wait=False, cancel_futures=True)
        self.streams.shutdown(wait=False, cancel_futures=True)
        self.titles.shutdown(wait=False, cancel_futures=True)
