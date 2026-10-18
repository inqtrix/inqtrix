"""Token-budgeted slicing and paced submission of embedding requests.

A document sent to the embedding provider as a single request carrying
every chunk fails when it is large: hundreds of chunks (~200k tokens)
exceed a per-minute provider quota in one shot and can therefore never
succeed, no matter how often the job resumes. This
module splits the input into order-preserving slices under a character
budget and, when the provider rejects a slice with a rate limit, waits
visibly (honouring ``Retry-After``) before submitting that same slice
again — bounded, cancellable, never silent. A 429 rejects the request
at the gate without processing it, so resubmission cannot double-embed
anything.

Ordering is the load-bearing invariant: the caller pairs vector ``i``
with chunk ``i`` positionally. Slices are submitted strictly
sequentially and concatenated in plan order, so no completion-order
race can reorder results; within one slice the provider's own
index-sort and count check apply (``_OpenAISDKEmbeddings._embed``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from inqtrix.provider_failure_contract import (
    ProviderFailureKind,
    classify_provider_failure,
    exception_chain,
)
from inqtrix.providers.base import sdk_retry_after
from inqtrix.providers.embeddings import EmbeddingProviderError

log = logging.getLogger("inqtrix")

# ~25k tokens at the 4-chars/token heuristic used by quota metering
# (inqtrix.quota.models.estimate_tokens). Below the smallest observed
# working request window and small enough that several slices fit into
# one quota minute; documents at or below ~50 default chunks go out as
# one request.
_MAX_SLICE_CHARS = 100_000
# One document bridges at most this many provider wait phases before the
# original rate-limit error propagates and the job parks as a resumable
# dependency pause.
_MAX_PROVIDER_WAITS = 3
# Azure's embedding 429 says "retry after 60 seconds"; used when the
# response carries no numeric Retry-After header.
_WAIT_DEFAULT_SECONDS = 60.0
# A hostile or absurd Retry-After must not stall a worker for hours.
_WAIT_CAP_SECONDS = 120.0
# Cancellation re-check interval while waiting out a provider window.
_WAIT_POLL_SECONDS = 0.5


def plan_embedding_slices(
    texts: list[str], *, max_chars: int = _MAX_SLICE_CHARS
) -> list[slice]:
    """Greedily pack ``texts`` into contiguous, order-preserving slices.

    Every position lands in exactly one slice, slices cover the list in
    input order, and no slice exceeds ``max_chars`` unless a single text
    alone does — texts are never split, so an oversized text travels as
    its own slice of one.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    plan: list[slice] = []
    start = 0
    used = 0
    for position, text in enumerate(texts):
        length = len(text)
        if position > start and used + length > max_chars:
            plan.append(slice(start, position))
            start = position
            used = 0
        used += length
    if start < len(texts):
        plan.append(slice(start, len(texts)))
    return plan


def _rate_limit_wait_seconds(exc: BaseException) -> float:
    """Resolve the visible wait before resubmitting a rejected slice.

    Walks the wrapped provider failure for a numeric ``Retry-After``
    (the ``raise … from`` chain keeps the SDK error reachable), capped so
    a hostile header cannot stall a worker, with the provider-documented
    default when the header is absent or non-numeric.
    """
    for err in exception_chain(exc):
        header = sdk_retry_after(err)
        if header is None:
            continue
        try:
            parsed = float(header)
        except ValueError:
            continue
        if parsed > 0:
            return min(parsed, _WAIT_CAP_SECONDS)
    return _WAIT_DEFAULT_SECONDS


async def _cancellable_wait(
    seconds: float, cancel_check: Callable[[], None] | None
) -> None:
    """Sleep in short poll slices so cancellation interrupts promptly."""
    deadline = time.monotonic() + seconds
    while True:
        if cancel_check is not None:
            cancel_check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_WAIT_POLL_SECONDS, remaining))


async def embed_in_slices(
    texts: list[str],
    *,
    embed_fn: Callable[[list[str]], list[list[float]]],
    cancel_check: Callable[[], None] | None = None,
    on_batch: Callable[[int, int], None] | None = None,
    on_wait: Callable[[int, int], None] | None = None,
    max_chars: int = _MAX_SLICE_CHARS,
    max_waits: int = _MAX_PROVIDER_WAITS,
) -> list[list[float]]:
    """Embed ``texts`` slice by slice, pacing around provider rate limits.

    ``embed_fn`` is the provider's synchronous ``embed_documents`` (model
    already bound) and runs off the event loop per slice. Results are
    concatenated strictly in plan order; a failure never yields a partial
    result. Only a rate-limited rejection triggers a bounded, visible
    wait followed by resubmission of the same slice; every other failure
    propagates immediately, preserving today's dependency-pause path.
    """
    if not texts:
        return []
    plan = plan_embedding_slices(texts, max_chars=max_chars)
    total = len(plan)
    vectors: list[list[float]] = []
    waits_used = 0
    for number, part in enumerate(plan, start=1):
        slice_texts = texts[part]
        while True:
            if cancel_check is not None:
                cancel_check()
            if on_batch is not None:
                on_batch(number, total)
            try:
                result = await asyncio.to_thread(embed_fn, slice_texts)
            except Exception as exc:
                classification = classify_provider_failure(exc)
                if (
                    classification.kind is not ProviderFailureKind.RATE_LIMITED
                    or waits_used >= max_waits
                ):
                    raise
                waits_used += 1
                delay = _rate_limit_wait_seconds(exc)
                # No Silent Fallbacks: every bridged rejection is visible
                # in the log and, via on_wait, in the document's badge.
                log.warning(
                    "Embedding subrequest %d/%d rejected by the provider (rate limit); "
                    "visible wait %d/%d for %.0fs before resubmission",
                    number,
                    total,
                    waits_used,
                    max_waits,
                    delay,
                )
                if on_wait is not None:
                    on_wait(number, total)
                await _cancellable_wait(delay, cancel_check)
                continue
            vectors.extend(result)
            break
    if len(vectors) != len(texts):
        # Defense in depth for the pairing invariant. Deliberately not a
        # transient failure: a count drift is a contract violation, never
        # something a resume could fix.
        raise EmbeddingProviderError(
            f"Embedding slice concatenation mismatch: planned {len(texts)} "
            f"inputs, collected {len(vectors)} vectors"
        )
    return vectors
