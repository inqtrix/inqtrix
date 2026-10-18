"""The generalized pairing invariant for sliced embedding submission.

``embed_in_slices`` may split, pace, and resubmit however it likes —
these tests only accept implementations for which vector ``i`` provably
belongs to text ``i`` for every input shape, and for which a failure
never yields a partial result. A deterministic text→vector fake makes
any shift, gap, or swap visible regardless of the internal plan, so the
whole class of silent misalignment bugs stays caught even if the
slicing strategy changes later.
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest

from inqtrix.knowledge import embedding_batches
from inqtrix.knowledge.embedding_batches import (
    embed_in_slices,
    plan_embedding_slices,
)
from inqtrix.providers.embeddings import EmbeddingProviderError


def _vector_for(text: str) -> list[float]:
    """Deterministic per-text vector so any misalignment is visible."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[0] / 255, digest[1] / 255]


def _texts(count: int) -> list[str]:
    """Unique texts with slightly varying lengths to shift boundaries."""
    return [f"text-{i:04d}-" + "x" * (40 + (i % 7)) for i in range(count)]


class _RateLimited(Exception):
    """Duck-typed OpenAI-SDK 429 (classification is attribute-based)."""

    status_code = 429

    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        if retry_after is not None:
            self.response = SimpleNamespace(
                headers={"retry-after": retry_after}
            )


def _rate_limit_error(retry_after: str | None = None) -> EmbeddingProviderError:
    wrapped = EmbeddingProviderError("Embedding call failed: rate limit")
    wrapped.__cause__ = _RateLimited("429", retry_after=retry_after)
    return wrapped


# ---------------------------------------------------------------- #
# Plan properties
# ---------------------------------------------------------------- #


@pytest.mark.parametrize("count", [0, 1, 2, 3, 10, 97, 430])
@pytest.mark.parametrize("max_chars", [50, 120, 1_000, 1_000_000])
def test_plan_covers_every_position_exactly_once_in_order(
    count: int, max_chars: int
) -> None:
    texts = _texts(count)

    plan = plan_embedding_slices(texts, max_chars=max_chars)

    covered = [i for part in plan for i in range(part.start, part.stop)]
    assert covered == list(range(len(texts)))
    for part in plan:
        batch = texts[part]
        assert len(batch) >= 1
        assert len(batch) == 1 or sum(map(len, batch)) <= max_chars


def test_plan_never_splits_a_single_oversized_text() -> None:
    plan = plan_embedding_slices(["y" * 300_000], max_chars=100_000)

    assert plan == [slice(0, 1)]


def test_plan_rejects_a_non_positive_budget() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        plan_embedding_slices(["a"], max_chars=0)


# ---------------------------------------------------------------- #
# The pairing invariant, parametrized over shapes
# ---------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2, 3, 10, 97, 430])
async def test_slicing_preserves_text_vector_pairing(count: int) -> None:
    """vectors[i] must equal f(texts[i]) for all i, whatever the plan."""
    texts = _texts(count)
    requests: list[list[str]] = []

    def fake_embed(batch: list[str]) -> list[list[float]]:
        requests.append(list(batch))
        return [_vector_for(text) for text in batch]

    vectors = await embed_in_slices(texts, embed_fn=fake_embed, max_chars=200)

    assert vectors == [_vector_for(text) for text in texts]
    # every input travelled exactly once, in order, within budget
    assert [text for batch in requests for text in batch] == texts
    for batch in requests:
        assert len(batch) == 1 or sum(map(len, batch)) <= 200


@pytest.mark.asyncio
async def test_empty_input_makes_zero_requests() -> None:
    def fake_embed(batch: list[str]) -> list[list[float]]:
        raise AssertionError("no request expected for empty input")

    assert await embed_in_slices([], embed_fn=fake_embed) == []


@pytest.mark.asyncio
async def test_a_provider_count_drift_is_a_loud_contract_violation() -> None:
    """Defense in depth: a short slice result must never publish quietly."""
    texts = _texts(4)

    def fake_embed(batch: list[str]) -> list[list[float]]:
        return [_vector_for(text) for text in batch][:-1]

    with pytest.raises(EmbeddingProviderError, match="mismatch"):
        await embed_in_slices(texts, embed_fn=fake_embed, max_chars=100)


# ---------------------------------------------------------------- #
# Failure semantics: whole result or none, no work after a failure
# ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failure_mid_plan_yields_no_partial_result_and_stops() -> None:
    texts = _texts(10)
    requests: list[list[str]] = []

    def fake_embed(batch: list[str]) -> list[list[float]]:
        requests.append(list(batch))
        if len(requests) == 3:
            raise EmbeddingProviderError("deterministic validation failure")
        return [_vector_for(text) for text in batch]

    with pytest.raises(EmbeddingProviderError):
        await embed_in_slices(texts, embed_fn=fake_embed, max_chars=100)

    assert len(requests) == 3


@pytest.mark.asyncio
async def test_non_rate_limit_errors_propagate_without_any_wait() -> None:
    """5xx/timeout keep today's immediate dependency-pause path."""

    class _Unavailable(Exception):
        status_code = 503

    waits: list[tuple[int, int]] = []

    def fake_embed(batch: list[str]) -> list[list[float]]:
        wrapped = EmbeddingProviderError("Embedding call failed: upstream")
        wrapped.__cause__ = _Unavailable("503")
        raise wrapped

    started = time.monotonic()
    with pytest.raises(EmbeddingProviderError):
        await embed_in_slices(
            _texts(3),
            embed_fn=fake_embed,
            max_chars=100,
            on_wait=lambda current, total: waits.append((current, total)),
        )

    assert waits == []
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------- #
# Rate-limit pacing: visible, bounded, resubmits the same slice
# ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_wait_actually_elapses_before_resubmission() -> None:
    """A nullified wait must fail: pacing is the module's entire point."""
    rejected = False

    def fake_embed(batch: list[str]) -> list[list[float]]:
        nonlocal rejected
        if not rejected:
            rejected = True
            raise _rate_limit_error(retry_after="0.2")
        return [_vector_for(text) for text in batch]

    started = time.monotonic()
    vectors = await embed_in_slices(
        _texts(2), embed_fn=fake_embed, max_chars=1_000
    )
    elapsed = time.monotonic() - started

    assert vectors == [_vector_for(text) for text in _texts(2)]
    # Retry-After is a floor on the real clock, not just a computed number.
    assert elapsed >= 0.2
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_wait_budget_is_per_document_across_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waits accumulate over the whole document; a per-slice reset must fail.

    Slice 1 is rejected twice and slice 3 twice: the fourth total wait
    exceeds ``max_waits=3``, so the error propagates even though no single
    slice was rejected more than twice.
    """
    monkeypatch.setattr(embedding_batches, "_WAIT_DEFAULT_SECONDS", 0.0)
    texts = _texts(6)
    rejections: dict[str, int] = {}
    waits: list[tuple[int, int]] = []

    def fake_embed(batch: list[str]) -> list[list[float]]:
        marker = batch[0]
        target = {texts[0], texts[4]}  # first text of slice 1 and slice 3
        if marker in target and rejections.get(marker, 0) < 2:
            rejections[marker] = rejections.get(marker, 0) + 1
            raise _rate_limit_error()
        return [_vector_for(text) for text in batch]

    with pytest.raises(EmbeddingProviderError):
        await embed_in_slices(
            texts,
            embed_fn=fake_embed,
            max_chars=120,
            max_waits=3,
            on_wait=lambda current, total: waits.append((current, total)),
        )

    # two bridged waits on slice 1, one on slice 3, then refusal
    assert waits == [(1, 3), (1, 3), (3, 3)]


@pytest.mark.asyncio
async def test_batch_callback_reports_one_based_progress_and_resumes() -> None:
    """The badge channel: exact (current, total) values, re-emitted after
    a wait so the display flips back from waiting to embedding."""
    texts = _texts(6)  # -> 3 slices at max_chars=120
    batches: list[tuple[int, int]] = []
    waits: list[tuple[int, int]] = []
    rejected = False

    def fake_embed(batch: list[str]) -> list[list[float]]:
        nonlocal rejected
        if not rejected and batch[0] == texts[2]:  # first attempt of slice 2
            rejected = True
            raise _rate_limit_error(retry_after="0.01")
        return [_vector_for(text) for text in batch]

    await embed_in_slices(
        texts,
        embed_fn=fake_embed,
        max_chars=120,
        on_batch=lambda current, total: batches.append((current, total)),
        on_wait=lambda current, total: waits.append((current, total)),
    )

    assert batches == [(1, 3), (2, 3), (2, 3), (3, 3)]
    assert waits == [(2, 3)]


@pytest.mark.asyncio
async def test_rate_limited_slice_waits_visibly_then_resubmits() -> None:
    texts = _texts(6)
    requests: list[list[str]] = []
    waits: list[tuple[int, int]] = []
    rejected = False

    def fake_embed(batch: list[str]) -> list[list[float]]:
        nonlocal rejected
        requests.append(list(batch))
        if not rejected and len(requests) == 2:
            rejected = True
            raise _rate_limit_error(retry_after="0.01")
        return [_vector_for(text) for text in batch]

    vectors = await embed_in_slices(
        texts,
        embed_fn=fake_embed,
        max_chars=120,
        on_wait=lambda current, total: waits.append((current, total)),
    )

    assert vectors == [_vector_for(text) for text in texts]
    assert len(waits) == 1
    # the rejected slice was resubmitted byte-identically, then the plan
    # continued — nothing was skipped and nothing was embedded twice
    assert requests[1] == requests[2]
    flattened: list[str] = []
    for number, batch in enumerate(requests):
        if number == 1:
            continue  # the rejected attempt produced no vectors
        flattened.extend(batch)
    assert flattened == texts


@pytest.mark.asyncio
async def test_wait_budget_exhaustion_propagates_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_batches, "_WAIT_DEFAULT_SECONDS", 0.0)
    requests: list[list[str]] = []
    waits: list[tuple[int, int]] = []

    def fake_embed(batch: list[str]) -> list[list[float]]:
        requests.append(list(batch))
        raise _rate_limit_error()

    with pytest.raises(EmbeddingProviderError):
        await embed_in_slices(
            _texts(4),
            embed_fn=fake_embed,
            max_chars=100,
            max_waits=3,
            on_wait=lambda current, total: waits.append((current, total)),
        )

    # one initial submission plus exactly max_waits resubmissions
    assert len(requests) == 4
    assert len(waits) == 3


@pytest.mark.asyncio
async def test_cancel_during_a_wait_raises_the_callers_exception() -> None:
    """The wait is interruptible with the caller's own cancellation type."""

    class _Cancelled(Exception):
        pass

    checks = 0

    def cancel_check() -> None:
        nonlocal checks
        checks += 1
        if checks > 2:
            raise _Cancelled()

    def fake_embed(batch: list[str]) -> list[list[float]]:
        raise _rate_limit_error(retry_after="30")

    started = time.monotonic()
    with pytest.raises(_Cancelled):
        await embed_in_slices(
            _texts(3),
            embed_fn=fake_embed,
            max_chars=100,
            cancel_check=cancel_check,
        )

    # interrupted within the first poll slices, far below the 30s window
    assert time.monotonic() - started < 5.0


# ---------------------------------------------------------------- #
# Retry-After resolution
# ---------------------------------------------------------------- #


def test_wait_seconds_honour_header_cap_and_default() -> None:
    assert (
        embedding_batches._rate_limit_wait_seconds(
            _rate_limit_error(retry_after="7")
        )
        == 7.0
    )
    assert (
        embedding_batches._rate_limit_wait_seconds(
            _rate_limit_error(retry_after="999999")
        )
        == embedding_batches._WAIT_CAP_SECONDS
    )
    assert (
        embedding_batches._rate_limit_wait_seconds(_rate_limit_error())
        == embedding_batches._WAIT_DEFAULT_SECONDS
    )
    assert (
        embedding_batches._rate_limit_wait_seconds(
            _rate_limit_error(retry_after="soon")
        )
        == embedding_batches._WAIT_DEFAULT_SECONDS
    )
    # zero and negative headers carry no usable pacing hint
    assert (
        embedding_batches._rate_limit_wait_seconds(
            _rate_limit_error(retry_after="0")
        )
        == embedding_batches._WAIT_DEFAULT_SECONDS
    )
    assert (
        embedding_batches._rate_limit_wait_seconds(
            _rate_limit_error(retry_after="-5")
        )
        == embedding_batches._WAIT_DEFAULT_SECONDS
    )
