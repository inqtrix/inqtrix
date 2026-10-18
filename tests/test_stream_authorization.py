"""The generation-gated frame authorization shared by both SSE twins.

The gate's contract: the FULL authoritative chain runs on the first
frame, whenever the commit-ordered generation moved, whenever the time
ceiling elapsed, and on every frame for principals without a generation
— never less. The generation is a hint; the chain decides.
"""

from __future__ import annotations

import pytest

from inqtrix.server.stream_authorization import (
    GenerationGatedFrameAuthorization,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Chain:
    def __init__(self, result: bool = True) -> None:
        self.calls = 0
        self.result = result

    async def __call__(self) -> bool:
        self.calls += 1
        return self.result


def _gate(chain, generations, clock, ceiling=30.0):
    values = iter(generations)

    async def read() -> int | None:
        return next(values)

    return GenerationGatedFrameAuthorization(
        full_check=chain,
        read_generation=read,
        ceiling_seconds=ceiling,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_quiet_generation_skips_the_chain_until_the_ceiling() -> None:
    chain = _Chain()
    clock = _Clock()
    gate = _gate(chain, [5, 5, 5, 5], clock)

    assert await gate.allowed()
    assert chain.calls == 1, "first frame always runs the full chain"
    clock.now += 1.0
    assert await gate.allowed()
    clock.now += 1.0
    assert await gate.allowed()
    assert chain.calls == 1, "unchanged generation within the ceiling: hint only"
    clock.now += 31.0
    assert await gate.allowed()
    assert chain.calls == 2, (
        "the ceiling is part of the security contract: session expiry "
        "writes no bump, so the chain must re-run on time alone"
    )


@pytest.mark.asyncio
async def test_moved_generation_reruns_the_chain_immediately() -> None:
    chain = _Chain()
    clock = _Clock()
    gate = _gate(chain, [5, 6], clock)

    assert await gate.allowed()
    clock.now += 1.0
    assert await gate.allowed()
    assert chain.calls == 2, "a bump within the ceiling must re-check NOW"


@pytest.mark.asyncio
async def test_failed_chain_ends_the_stream_even_with_a_quiet_generation() -> None:
    chain = _Chain(result=False)
    clock = _Clock()
    gate = _gate(chain, [5], clock)
    assert not await gate.allowed()


@pytest.mark.asyncio
async def test_missing_generation_runs_the_chain_every_frame() -> None:
    # Api-key principals and the memory identity backend have no
    # generation: the gate must keep the pre-gate per-frame behavior,
    # never a silently weaker check.
    chain = _Chain()
    clock = _Clock()
    gate = _gate(chain, [None, None, None], clock)
    for _ in range(3):
        assert await gate.allowed()
    assert chain.calls == 3


@pytest.mark.asyncio
async def test_missing_reader_runs_the_chain_every_frame() -> None:
    chain = _Chain()
    gate = GenerationGatedFrameAuthorization(
        full_check=chain, read_generation=None
    )
    for _ in range(3):
        assert await gate.allowed()
    assert chain.calls == 3


@pytest.mark.asyncio
async def test_failing_reader_degrades_to_the_chain_loudly(caplog) -> None:
    chain = _Chain()

    async def broken() -> int | None:
        raise RuntimeError("hint store down")

    gate = GenerationGatedFrameAuthorization(
        full_check=chain, read_generation=broken
    )
    with caplog.at_level("WARNING", logger="inqtrix"):
        assert await gate.allowed()
        assert await gate.allowed()
    assert chain.calls == 2, "a broken hint must not weaken the check"
    warnings = [
        r for r in caplog.records if "Generation read" in r.message
    ]
    assert len(warnings) == 1, "warn once per stream, not per frame"


@pytest.mark.asyncio
async def test_bump_between_read_and_chain_is_caught_next_frame() -> None:
    # The generation is read BEFORE the chain runs. A mutation committing
    # in between bumps the stored value again, so the NEXT frame compares
    # against a stale last_generation and re-checks -- the ordering loses
    # nothing.
    chain = _Chain()
    clock = _Clock()
    gate = _gate(chain, [5, 6], clock)
    assert await gate.allowed()  # stores 5
    clock.now += 1.0
    chain.result = False  # the chain now reflects the revocation
    assert not await gate.allowed(), (
        "the moved generation forces the chain, which ends the stream"
    )


def test_both_routers_wire_the_gate_and_the_indexing_keepalive() -> None:
    """Source pin for the route glue no request-level test can reach.

    No request-level test notices a router that falls back to a
    per-frame authorization closure, or an indexing stream without its
    keepalive. The gate unit tests cover the class; THIS pins that both
    twins construct it, feed it the permission service's generation
    reader, and that the indexing stream keeps its heartbeat.
    """
    from pathlib import Path

    import inqtrix.server.routers.indexing as indexing_module
    import inqtrix.server.routers.runs as runs_module

    for module in (runs_module, indexing_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "GenerationGatedFrameAuthorization(" in source, module.__name__
        assert ".authorization_generation(" in source, (
            f"{module.__name__} must feed the permission service's "
            "generation reader into the gate"
        )
    indexing_source = Path(indexing_module.__file__).read_text(
        encoding="utf-8"
    )
    assert ": keepalive" in indexing_source, (
        "the indexing stream's quiet-period keepalive (and its gate "
        "recheck) must not silently disappear again"
    )
    assert "next_heartbeat" in indexing_source


def test_ceiling_constant_stays_a_security_bound() -> None:
    """The ceiling is the ONLY revocation bound for session/PAT expiry
    (which writes no bump); the module promises well below a minute."""
    from inqtrix.server.stream_authorization import (
        FULL_CHECK_CEILING_SECONDS,
    )

    assert 0 < FULL_CHECK_CEILING_SECONDS <= 60.0
    gate = GenerationGatedFrameAuthorization(
        full_check=_Chain(), read_generation=None
    )
    assert gate._ceiling_seconds == FULL_CHECK_CEILING_SECONDS


@pytest.mark.asyncio
async def test_the_stored_generation_is_the_pre_chain_read() -> None:
    """Killing pin for the read-before-chain ordering.

    A mutant that stores a post-chain RE-READ swallows a bump committing
    between the read and the chain: with values [5, 6, 6] it would cache
    6 on the first frame and skip the second frame's re-check entirely.
    """
    chain = _Chain()
    clock = _Clock()
    reads = {"count": 0}
    values = iter([5, 6, 6])

    async def read() -> int | None:
        reads["count"] += 1
        return next(values)

    gate = GenerationGatedFrameAuthorization(
        full_check=chain, read_generation=read, clock=clock
    )
    assert await gate.allowed()
    assert reads["count"] == 1, "exactly one read per frame"
    clock.now += 1.0
    assert await gate.allowed()
    assert chain.calls == 2, (
        "the bump that landed between read and chain must force a "
        "re-check on the NEXT frame"
    )
