"""Generation-gated per-frame authorization for long-lived SSE streams.

One shared gate for the run and indexing event streams, so the two
cannot drift apart the way hand-copied ``_authorized_frame`` closures do.
Per frame the gate reads the user's commit-ordered authorization
generation — one indexed SELECT inside one short tenant transaction
(checkout, role/tenant setup, SELECT, commit) — and re-runs the FULL
authoritative chain (credential resolver, user context, resource
visibility: three such transactions plus a run-store read) only when the
generation moved or a bounded time ceiling elapsed. The generation
is a HINT, never a decision: over-invalidation merely re-runs the chain.

The time ceiling is part of the security contract, not a fallback:
session EXPIRY writes no mutation and therefore bumps nothing, so the
ceiling is the guaranteed upper bound for it (and for any future
under-invalidation). Principals without a generation (api-key mode,
memory identity backend) run the full chain on every frame — exactly the
pre-gate behavior, never a silently weaker check.

A mutation may still commit between the generation read and the frame's
``yield``; that one-frame window existed with the per-frame full chain
too and is unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("inqtrix")

# Guaranteed upper bound between FULL authorization re-checks on a quiet
# generation. Sized for the un-bumped paths (session expiry): well below
# a minute, well above the per-frame cadence the generation replaces.
FULL_CHECK_CEILING_SECONDS = 30.0


class GenerationGatedFrameAuthorization:
    """Per-stream gate: cheap generation hint, bounded full re-checks."""

    def __init__(
        self,
        *,
        full_check: Callable[[], Awaitable[bool]],
        read_generation: Callable[[], Awaitable[int | None]] | None,
        ceiling_seconds: float = FULL_CHECK_CEILING_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._full_check = full_check
        self._read_generation = read_generation
        self._ceiling_seconds = float(ceiling_seconds)
        self._clock = clock
        self._last_generation: int | None = None
        self._next_full_check = 0.0
        self._warned_read_failure = False

    async def allowed(self) -> bool:
        """Authorize one frame; False ends the stream."""
        if self._read_generation is None:
            return await self._full_check()
        try:
            generation = await self._read_generation()
        except Exception:  # noqa: BLE001 — degrade to the FULL check, loudly
            # A failing hint must never fail the stream on its own, and
            # never silently weaken the check either: the authoritative
            # chain decides. Warn once per stream, not per frame.
            if not self._warned_read_failure:
                self._warned_read_failure = True
                log.warning(
                    "Generation read for frame authorization failed; using the full "
                    "authorization path for every frame.",
                    exc_info=True,
                )
            return await self._full_check()
        if generation is None:
            # No generation for this principal (api-key, memory backend):
            # full chain every frame, the pre-gate behavior.
            return await self._full_check()
        now = self._clock()
        if (
            generation == self._last_generation
            and now < self._next_full_check
        ):
            return True
        # The generation is read BEFORE the full chain runs: a mutation
        # committing in between bumps the value again and the NEXT frame
        # re-checks — nothing is lost to the ordering.
        if not await self._full_check():
            return False
        self._last_generation = generation
        self._next_full_check = now + self._ceiling_seconds
        return True
