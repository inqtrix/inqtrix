"""Chunk contextualization (Anthropic-style contextual retrieval).

At ingestion time an LLM situates every chunk within its document. The
generated context stays separate from source evidence and is combined with it
only for embedding and sparse indexing.

Calls are grouped dynamically by the resolved model's context budget. A
provider, timeout, or validation failure aborts the contextualized revision;
it never publishes a mixture of contextualized and raw chunks. A raw-text
revision is a separate, explicit user choice.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import logging
import math
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import AgentCancelled
from inqtrix.contextualization_circuit import (
    ContextualizationCircuitBreaker,
    ContextualizationCircuitPermit,
    ContextualizationCircuitView,
)
from inqtrix.knowledge.chunking import ChunkSlice
from inqtrix.model_cards import resolve_model_card
from inqtrix.model_routing import resolve_model
from inqtrix.provider_failure_contract import (
    ProviderFailureKind,
    classify_provider_failure,
)
from inqtrix.providers.base import (
    INCOMPLETE_FINISH_REASONS,
    get_llm_provider_capabilities,
    provider_cancel_scope,
)
from inqtrix.observability.context import bind_purpose
from inqtrix.prompts import (
    build_chunk_context_prompt,
    build_knowledge_followup_context_prompt,
)

log = logging.getLogger("inqtrix")

CONTEXT_MARKER_APPLIED = "_chunk_context_applied"
# Some chunks carry no context: the provider's content filter blocked their
# batch. The document records how many and why (``_chunk_context_omitted``).
CONTEXT_MARKER_PARTIAL = "_chunk_context_partial"
CONTEXT_OMITTED_CONTENT_FILTER = "content_filter"
QUERY_CONTEXT_MARKER_APPLIED = "_knowledge_query_context_applied"
QUERY_CONTEXT_MARKER_UNCHANGED = "_knowledge_query_context_unchanged"
QUERY_CONTEXT_MARKER_FALLBACK = "_knowledge_query_context_fallback"

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_CONTEXT_RESPONSE_SCHEMA_NAME = "inqtrix_knowledge_chunk_contexts_v1"
_CHUNK_BATCH_SIZE = 25
"""Maximum chunks per contextualization call.

The numbered, schema-bound response keeps every description attached to its
target chunk. Twenty-five is an efficiency ceiling, not a forced size: the
planner reduces a batch whenever the actual rendered prompt or output reserve
does not fit the resolved model."""

_WINDOW_MARGIN_CHARS = 6_000
"""Document text kept on each side of a batch's own span, so the model
sees what leads into and follows the chunks it is describing."""

_DOCUMENT_HEAD_CHARS = 2_000
"""Opening of the document, prepended to every window for global
orientation (title, scope, table of contents) — without it a batch from
page 300 has no idea which regulation it belongs to."""

_DEFAULT_CONTEXT_WINDOW_TOKENS = 16_000
_CONTEXTUALIZATION_MAX_OUTPUT_TOKENS = 4_096
_CONTEXT_OUTPUT_BASE_TOKENS = 32
_CONTEXT_OUTPUT_ITEM_RESERVE_TOKENS = 128
_CONTEXTUALIZATION_MAX_PARALLEL = 3
"""Planning reserve for one retrieval-context item in a batched JSON reply.

This is not an acceptance limit: a complete, non-empty context may be longer
and is preserved without truncation. The reserve merely prevents the planner
from packing so many chunks into one call that the provider's total output
window is predictably too small for the JSON response.
"""
_ASCII_CHAR_DIVISOR = 2
"""Conservative prose estimate when no provider tokenizer is available.

Non-ASCII text is charged by UTF-8 byte below, because a global character
divisor drastically undercounts CJK and emoji.  ASCII punctuation and
high-entropy identifier-like runs are also charged more strictly.
"""


class ContextualizationError(RuntimeError):
    """Base error for a revision that cannot be contextualized safely."""


class ContextualizationDependencyError(ContextualizationError):
    """The external contextualization dependency failed or timed out."""

    _MESSAGES = {
        "contextualization_provider_timeout": (
            ("The contextualization provider did not respond in time. The unpublished "
            "revision is retained and can resume.")
        ),
        "contextualization_provider_rate_limited": (
            ("The contextualization provider is temporarily rate-limiting requests. The "
            "unpublished revision is retained and can resume.")
        ),
        "contextualization_provider_unavailable": (
            ("The contextualization provider is temporarily unavailable. The unpublished"
            " revision is retained and can resume.")
        ),
        "contextualization_provider_circuit_open": (
            ("Further contextualization calls for this provider and model were stopped "
            "after a transient provider failure. No replacement or raw index was "
            "published. The revision remains paused and can resume when the dependency "
            "recovers.")
        ),
        "contextualization_circuit_state_unavailable": (
            ("The shared persisted protection state of the contextualization provider "
            "could not be reliably read or updated. No unprotected provider call was "
            "made and no replacement index was published.")
        ),
    }

    def __init__(self, *, error_type: str) -> None:
        """Construct a resumable failure with a stable public reason."""

        if error_type not in self._MESSAGES:
            raise ValueError("invalid contextualization dependency error type")
        self.error_type = error_type
        super().__init__(self._MESSAGES[error_type])


class ContextualizationProviderError(ContextualizationError):
    """A deterministic provider/configuration rejection that must terminate."""

    def __init__(self) -> None:
        super().__init__(
            ("Contextualization failed because of a non-resumable provider or "
            "configuration condition.")
        )


class ContextualizationInternalError(ContextualizationError):
    """An unclassified programming failure that must terminate safely."""

    def __init__(self) -> None:
        super().__init__(
            "Contextualization stopped because of an internal, non-resumable error."
        )


class ContextualizationValidationError(ContextualizationError):
    """Prompt planning or provider output violated the context contract."""


class ContextualizationContentFiltered(ContextualizationValidationError):
    """The provider's content filter blocked one batch.

    ``repeatable`` holds for a completion the filter cut off
    (``finish_reason=content_filter``): the output filter is not
    deterministic, so one repetition may pass. A request the filter refused
    (a structured ``content_filter`` rejection) is bound to its content and
    is not repeated.
    """

    def __init__(self, batch_number: int, *, repeatable: bool) -> None:
        super().__init__(
            f"contextualization batch {batch_number} was blocked by the "
            "provider content filter"
        )
        self.repeatable = repeatable


class ContextualizationCancelled(ContextualizationError):
    """A server-side indexing cancellation was observed between batches."""


@dataclass(frozen=True)
class ContextualizationBatch:
    """One fully budgeted contextualization provider call."""

    chunks: tuple[ChunkSlice, ...]
    window: str
    prompt: str
    is_excerpt: bool
    estimated_input_tokens: int
    reserved_output_tokens: int


@dataclass(frozen=True)
class ContextualizationBatchCheckpoint:
    """Validated provider output for one resumable batch boundary."""

    batch_number: int
    total_batches: int
    start_chunk: int
    chunk_count: int
    prompt_hash: str
    model: str
    contexts: tuple[str, ...]
    # Why this batch carries no contexts at all (the provider's content
    # filter blocked it); ``None`` for a contextualized batch.
    omitted_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        stored = {
            "batch_number": self.batch_number,
            "total_batches": self.total_batches,
            "start_chunk": self.start_chunk,
            "chunk_count": self.chunk_count,
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "contexts": list(self.contexts),
        }
        if self.omitted_reason is not None:
            stored["omitted_reason"] = self.omitted_reason
        return stored

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContextualizationBatchCheckpoint":
        try:
            contexts = value["contexts"]
            if not isinstance(contexts, list) or not all(
                isinstance(item, str) for item in contexts
            ):
                raise TypeError("contexts must be a list of strings")
            return cls(
                batch_number=int(value["batch_number"]),
                total_batches=int(value["total_batches"]),
                start_chunk=int(value["start_chunk"]),
                chunk_count=int(value["chunk_count"]),
                prompt_hash=str(value["prompt_hash"]),
                model=str(value.get("model") or ""),
                contexts=tuple(contexts),
                omitted_reason=(
                    str(value["omitted_reason"])
                    if value.get("omitted_reason")
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextualizationValidationError(
                "stored contextualization checkpoint is invalid"
            ) from exc


def _estimate_prompt_tokens(prompt: str) -> int:
    """Bound prompt size conservatively without assuming Latin prose.

    Model tokenizers differ and the provider port exposes no portable exact
    counter.  UTF-8 bytes are a safe upper-bound proxy for non-ASCII byte-
    fallback tokenizers; ordinary ASCII prose uses two characters per token,
    while punctuation and high-entropy long identifiers are charged one per
    character.  This deliberately overestimates CJK/emoji and code so an
    overflowing batch is rejected before a provider request.
    """
    if not prompt:
        return 0
    estimate = 0
    for segment in re.findall(r"[^\x00-\x7f]+|[A-Za-z0-9_]+|\s+|.", prompt):
        if any(ord(char) > 127 for char in segment):
            estimate += len(segment.encode("utf-8"))
        elif segment.isspace():
            estimate += math.ceil(len(segment) / 4)
        elif segment.isalnum() or all(
            char.isalnum() or char == "_" for char in segment
        ):
            high_entropy = (
                len(segment) >= 32
                and len(set(segment.casefold())) / len(segment) >= 0.35
            )
            estimate += (
                len(segment)
                if high_entropy
                else math.ceil(len(segment) / _ASCII_CHAR_DIVISOR)
            )
        else:
            estimate += len(segment)
    return estimate


def _document_window(
    document_text: str,
    batch: Sequence[ChunkSlice],
    *,
    max_chars: int,
) -> tuple[str, bool]:
    """Build a window containing every batch chunk by exact source offset."""
    if not batch:
        return "", False
    first = batch[0]
    last = batch[-1]
    if first.start < 0 or last.end > len(document_text) or first.start >= last.end:
        raise ContextualizationValidationError(
            "chunk span lies outside the canonical document"
        )
    for chunk in batch:
        if document_text[chunk.start : chunk.end] != chunk.text:
            raise ContextualizationValidationError(
                "chunk source span no longer matches the canonical document"
            )
    own_span = last.end - first.start
    if own_span > max_chars:
        raise ContextualizationValidationError(
            f"batch source span requires {own_span} characters, "
            f"budget permits {max_chars}"
        )
    if len(document_text) <= max_chars:
        return document_text, False

    separator = "\n\n[…]\n\n"
    head = document_text[: min(_DOCUMENT_HEAD_CHARS, first.start)]
    include_head = bool(head) and first.start > len(head) + _WINDOW_MARGIN_CHARS
    head_cost = len(head) + len(separator) if include_head else 0
    # The source span is the hard invariant. If one unusually large span
    # leaves no room for the global head, preserve the span and omit only that
    # orientation aid; the model still receives the document title.
    if own_span + head_cost > max_chars:
        include_head = False
        head_cost = 0

    spare = max_chars - head_cost - own_span
    left = min(_WINDOW_MARGIN_CHARS, first.start, spare // 2)
    right = min(_WINDOW_MARGIN_CHARS, len(document_text) - last.end, spare - left)
    remaining = spare - left - right
    if remaining:
        extra_left = min(first.start - left, remaining)
        left += extra_left
        remaining -= extra_left
        right += min(len(document_text) - last.end - right, remaining)
    start = first.start - left
    stop = last.end + right
    window = document_text[start:stop]

    if include_head and start > len(head):
        window = f"{head}{separator}{window}"
    return window, True


@dataclass(frozen=True)
class ContextualizedChunks:
    """Result of contextualizing one document's chunks.

    Attributes:
        texts: One embedding text per input chunk.
        contexts: Generated retrieval context kept separate from source text;
            empty for a chunk whose batch the content filter blocked.
        marker: ``_chunk_context_applied``, or ``_chunk_context_partial``
            when some chunks carry no context; other failures raise.
        batch_count: Number of provider calls in the resolved plan.
        omitted_chunks: Chunks indexed without context because the provider's
            content filter blocked their batch.
    """

    texts: list[str]
    marker: str
    contexts: list[str]
    batch_count: int
    omitted_chunks: int = 0


@dataclass(frozen=True)
class ContextualizedQuestion:
    """Standalone retrieval question derived from a conversation turn.

    Attributes:
        question: The query text to use for retrieval. On fallback this
            is the original user question.
        marker: ``_knowledge_query_context_applied``,
            ``_knowledge_query_context_unchanged`` or
            ``_knowledge_query_context_fallback``.
        rewritten: Whether the returned query differs from the current
            user question.
    """

    question: str
    marker: str
    rewritten: bool


class ChunkContextualizer(ABC):
    """Port for ingestion-time chunk contextualization."""

    @abstractmethod
    def contextualize(
        self,
        *,
        document_title: str,
        document_text: str,
        chunks: Sequence[ChunkSlice],
        on_batch_completed: Callable[[int, int], None] | None = None,
        on_batch_checkpoint: Callable[
            [ContextualizationBatchCheckpoint], None
        ]
        | None = None,
        completed_batches: Sequence[ContextualizationBatchCheckpoint] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> ContextualizedChunks:
        """Return contexts and embedding texts (same order and count)."""


class LLMChunkContextualizer(ChunkContextualizer):
    """Batched per-document contextualization via the run LLM.

    Args:
        llm: LLM provider (``complete_with_metadata``). Constructor-
            First — the composition root wires the deployment's
            provider in.
        model: Model override for the calls; ``None`` resolves the
            ``knowledge_contextualize`` fast-tier assignment against
            the provider's model bundle (mini-model by default).
        timeout: Per-call timeout in seconds.
    """

    def __init__(
        self,
        llm: Any,
        *,
        model: str | None = None,
        timeout: float = REASONING_TIMEOUT,
        max_output_tokens: int = _CONTEXTUALIZATION_MAX_OUTPUT_TOKENS,
        circuit_cooldown_seconds: float = 60.0,
        circuit_probe_lease_seconds: float = 900.0,
        provider_key: str | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._timeout = timeout
        self._max_output_tokens = max(256, int(max_output_tokens))
        self._circuit_cooldown_seconds = max(
            1.0, float(circuit_cooldown_seconds)
        )
        # A half-open lease is not an operation timeout.  It is a
        # cross-worker ownership fence and must outlive a legitimate provider
        # call including its visible retry/backoff budget.
        self._circuit_probe_lease_seconds = max(
            float(circuit_probe_lease_seconds),
            float(timeout) + 30.0,
        )
        self._provider_key = (
            str(provider_key).strip()
            if provider_key is not None and str(provider_key).strip()
            else self._provider_identity(llm)
        )
        self._circuit_breaker: ContextualizationCircuitBreaker | None = None
        self._circuit_bind_lock = threading.Lock()
        provider_cap = int(
            get_llm_provider_capabilities(llm).max_concurrency or 0
        )
        self._parallelism = max(
            1,
            min(
                _CONTEXTUALIZATION_MAX_PARALLEL,
                provider_cap or _CONTEXTUALIZATION_MAX_PARALLEL,
            ),
        )
        # One contextualizer instance is shared by all indexing jobs in a
        # process.  The gate therefore enforces the declared provider ceiling
        # across documents instead of multiplying it by every job executor.
        self._provider_gate = threading.BoundedSemaphore(self._parallelism)

    @staticmethod
    def _provider_identity(llm: Any) -> str:
        """Return a stable, secret-free provider key for circuit isolation.

        Names the backend behind every decorator (rule, tracing,
        configuration): one unwrap step would name a decorator, and every
        backend would share one circuit.
        """
        from inqtrix.runtime_logging import unwrap_provider

        provider_type = type(unwrap_provider(llm))
        return f"{provider_type.__module__}.{provider_type.__qualname__}"

    def bind_circuit_breaker(
        self,
        breaker: ContextualizationCircuitBreaker,
    ) -> None:
        """Bind the deployment's shared indexing circuit authority once."""

        if not isinstance(breaker, ContextualizationCircuitBreaker):
            raise TypeError("invalid contextualization circuit breaker")
        with self._circuit_bind_lock:
            existing = self._circuit_breaker
            if existing is not None and existing is not breaker:
                raise RuntimeError(
                    "contextualization circuit breaker is already bound"
                )
            self._circuit_breaker = breaker

    def circuit_view(self) -> ContextualizationCircuitView | None:
        """State of this contextualizer's circuit, keyed as its calls acquire it."""
        breaker = self._circuit_breaker
        if breaker is None:
            return None
        return breaker.contextualization_circuit_view(
            provider_key=self._provider_key,
            model=self._resolved_model() or "provider-default",
        )

    def _resolved_model(self) -> str | None:
        if self._model:
            return self._model
        provider_models = getattr(self._llm, "models", None)
        if provider_models is None:
            return None
        return resolve_model(
            "knowledge_contextualize", provider_models, None
        ) or None

    def _resolved_max_output_tokens(self) -> int:
        """Return the output ceiling shared by planning and invocation."""
        resolved_model = self._resolved_model()
        card = resolve_model_card(resolved_model) if resolved_model else None
        model_ceiling = (
            int(card.max_output_tokens) if card is not None else self._max_output_tokens
        )
        return min(self._max_output_tokens, model_ceiling)

    def contextualize(
        self,
        *,
        document_title: str,
        document_text: str,
        chunks: Sequence[ChunkSlice],
        on_batch_completed: Callable[[int, int], None] | None = None,
        on_batch_checkpoint: Callable[
            [ContextualizationBatchCheckpoint], None
        ]
        | None = None,
        completed_batches: Sequence[ContextualizationBatchCheckpoint] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> ContextualizedChunks:
        """Contextualize all chunks or fail the revision before publication."""
        if not chunks:
            return ContextualizedChunks(
                texts=[], contexts=[], marker=CONTEXT_MARKER_APPLIED, batch_count=0
            )
        plans = self.plan_batches(
            document_title=document_title,
            document_text=document_text,
            chunks=chunks,
        )
        checkpoints = list(completed_batches or ())
        checkpoint_by_number: dict[int, ContextualizationBatchCheckpoint] = {}
        for checkpoint in checkpoints:
            if (
                checkpoint.batch_number < 1
                or checkpoint.batch_number > len(plans)
                or checkpoint.batch_number in checkpoint_by_number
            ):
                raise ContextualizationValidationError(
                    "stored contextualization checkpoints contain an invalid "
                    "or duplicate batch number"
                )
            checkpoint_by_number[checkpoint.batch_number] = checkpoint
        resolved_model = self._resolved_model() or ""
        batch_specs: list[tuple[int, ContextualizationBatch, int, str]] = []
        start_chunk = 0
        for batch_number, plan in enumerate(plans, start=1):
            prompt_hash = hashlib.sha256(
                f"{resolved_model}\0{plan.prompt}".encode("utf-8")
            ).hexdigest()
            batch_specs.append((batch_number, plan, start_chunk, prompt_hash))
            start_chunk += len(plan.chunks)

        contexts_by_batch: dict[int, list[str]] = {}
        # Batches the provider's content filter blocked, with the reason.
        omitted_by_batch: dict[int, str] = {}
        completed_count = 0
        stale: list[int] = []
        for batch_number, plan, start_chunk, prompt_hash in batch_specs:
            checkpoint = checkpoint_by_number.get(batch_number)
            if checkpoint is None:
                continue
            if (
                checkpoint.total_batches != len(plans)
                or checkpoint.start_chunk != start_chunk
                or checkpoint.chunk_count != len(plan.chunks)
                or checkpoint.prompt_hash != prompt_hash
                or checkpoint.model != resolved_model
                or len(checkpoint.contexts) != len(plan.chunks)
                or (
                    # An omitted batch holds no context by design; any other
                    # batch needs one valid context per chunk.
                    any(checkpoint.contexts)
                    if checkpoint.omitted_reason is not None
                    else any(
                        not self._valid_context(context)
                        for context in checkpoint.contexts
                    )
                )
            ):
                stale.append(batch_number)
                continue
            contexts_by_batch[batch_number] = [
                context.strip() for context in checkpoint.contexts
            ]
            if checkpoint.omitted_reason is not None:
                omitted_by_batch[batch_number] = checkpoint.omitted_reason
        if stale:
            # A checkpoint from another prompt version, model or batch plan
            # would mix contexts of two generations inside one document. The
            # document restarts from scratch, visibly, instead of failing the
            # revision or publishing a mixture.
            log.warning(
                "contextualization checkpoints discarded for %r: %d of %d "
                "stored batches no longer match the current prompt, model or "
                "batch plan; the document is contextualized from scratch",
                document_title,
                len(stale),
                len(checkpoint_by_number),
            )
            contexts_by_batch = {}
            omitted_by_batch = {}
        completed_count = len(contexts_by_batch)
        if on_batch_completed is not None:
            for done in range(1, completed_count + 1):
                on_batch_completed(done, len(plans))

        pending_specs = [
            spec for spec in batch_specs if spec[0] not in contexts_by_batch
        ]
        first_error: BaseException | None = None
        pending_cursor = 0
        in_flight: dict[
            Future[tuple[list[str], str | None]],
            tuple[int, ContextualizationBatch, int, str],
        ] = {}

        def _submit_available(executor: ThreadPoolExecutor) -> None:
            nonlocal pending_cursor, first_error
            while (
                first_error is None
                and pending_cursor < len(pending_specs)
                and len(in_flight) < self._parallelism
            ):
                try:
                    if cancel_check is not None:
                        cancel_check()
                except Exception as exc:  # cancellation type belongs to caller
                    first_error = exc
                    return
                spec = pending_specs[pending_cursor]
                pending_cursor += 1
                batch_number, plan, _start, _hash = spec
                # Snapshot the submitting thread's contextvars: the pool
                # thread would otherwise meter this batch's LLM calls
                # with feature="other" instead of the bound feature.
                submit_context = contextvars.copy_context()
                future = executor.submit(
                    submit_context.run,
                    functools.partial(
                        self._batch_contexts,
                        document_title=document_title,
                        prompt=plan.prompt,
                        expected=len(plan.chunks),
                        batch_number=batch_number,
                        cancel_check=cancel_check,
                    ),
                )
                in_flight[future] = spec

        if pending_specs:
            with ThreadPoolExecutor(
                max_workers=self._parallelism,
                thread_name_prefix="inqtrix-context",
            ) as executor:
                _submit_available(executor)
                while in_flight:
                    completed, _pending = wait(
                        tuple(in_flight), return_when=FIRST_COMPLETED
                    )
                    for future in completed:
                        batch_number, plan, start_chunk, prompt_hash = (
                            in_flight.pop(future)
                        )
                        try:
                            raw_contexts, omitted_reason = future.result()
                            contexts = [
                                context.strip() for context in raw_contexts
                            ]
                            if cancel_check is not None:
                                cancel_check()
                        except Exception as exc:
                            if first_error is None:
                                first_error = exc
                            continue
                        contexts_by_batch[batch_number] = contexts
                        if omitted_reason is not None:
                            omitted_by_batch[batch_number] = omitted_reason
                        if on_batch_checkpoint is not None:
                            on_batch_checkpoint(
                                ContextualizationBatchCheckpoint(
                                    batch_number=batch_number,
                                    total_batches=len(plans),
                                    start_chunk=start_chunk,
                                    chunk_count=len(plan.chunks),
                                    prompt_hash=prompt_hash,
                                    model=resolved_model,
                                    contexts=tuple(contexts),
                                    omitted_reason=omitted_reason,
                                )
                            )
                        completed_count += 1
                        if on_batch_completed is not None:
                            on_batch_completed(completed_count, len(plans))
                    _submit_available(executor)

        if first_error is not None:
            raise first_error
        if len(contexts_by_batch) != len(plans):
            raise ContextualizationValidationError(
                "contextualization stopped before every batch was validated"
            )
        contexts_out = [
            context
            for batch_number in range(1, len(plans) + 1)
            for context in contexts_by_batch[batch_number]
        ]
        texts = [
            f"{context}\n\n{chunk.text}" if context else chunk.text
            for context, chunk in zip(contexts_out, chunks)
        ]
        omitted_chunks = sum(
            len(contexts_by_batch[batch_number]) for batch_number in omitted_by_batch
        )
        return ContextualizedChunks(
            texts=texts,
            contexts=contexts_out,
            marker=(
                CONTEXT_MARKER_PARTIAL if omitted_chunks else CONTEXT_MARKER_APPLIED
            ),
            batch_count=len(plans),
            omitted_chunks=omitted_chunks,
        )

    def plan_batches(
        self,
        *,
        document_title: str,
        document_text: str,
        chunks: Sequence[ChunkSlice],
    ) -> list[ContextualizationBatch]:
        """Greedily group contiguous chunks under the resolved model budget."""
        resolved_model = self._resolved_model()
        card = resolve_model_card(resolved_model) if resolved_model else None
        provider_window = getattr(self._llm, "context_window_tokens", None)
        context_window = int(
            (card.context_window_tokens if card is not None else 0)
            or (provider_window if isinstance(provider_window, int) else 0)
            or _DEFAULT_CONTEXT_WINDOW_TOKENS
        )
        output_tokens = self._resolved_max_output_tokens()
        output_batch_limit = min(
            _CHUNK_BATCH_SIZE,
            max(
                0,
                (output_tokens - _CONTEXT_OUTPUT_BASE_TOKENS)
                // _CONTEXT_OUTPUT_ITEM_RESERVE_TOKENS,
            ),
        )
        if output_batch_limit < 1:
            raise ContextualizationValidationError(
                f"model output budget {output_tokens} cannot carry one "
                "validated retrieval context"
            )
        safety_tokens = max(2_048, math.ceil(context_window * 0.10))
        input_budget = context_window - output_tokens - safety_tokens
        if input_budget <= 0:
            raise ContextualizationValidationError(
                f"model context window {context_window} leaves no input budget "
                f"after {output_tokens} output and {safety_tokens} safety tokens"
            )
        # This is only a conservative translation of the real model input
        # budget for the window builder. There is no independent 60k character
        # cap: the fully rendered prompt below remains the authority.
        max_window_chars = input_budget * _ASCII_CHAR_DIVISOR

        plans: list[ContextualizationBatch] = []
        cursor = 0
        materialized = list(chunks)
        while cursor < len(materialized):
            accepted: ContextualizationBatch | None = None
            upper = min(len(materialized), cursor + output_batch_limit)
            for stop in range(cursor + 1, upper + 1):
                candidate = materialized[cursor:stop]
                try:
                    plan = self._build_batch_plan(
                        document_title=document_title,
                        document_text=document_text,
                        chunks=candidate,
                        max_window_chars=max_window_chars,
                        input_budget=input_budget,
                    )
                except ContextualizationValidationError:
                    break
                if plan.estimated_input_tokens > input_budget:
                    break
                accepted = plan
            if accepted is None:
                chunk = materialized[cursor]
                raise ContextualizationValidationError(
                    f"chunk {cursor} ({chunk.end - chunk.start} source chars) "
                    f"does not fit contextualization input budget {input_budget} tokens"
                )
            plans.append(accepted)
            cursor += len(accepted.chunks)
        return plans

    @staticmethod
    def _build_batch_plan(
        *,
        document_title: str,
        document_text: str,
        chunks: Sequence[ChunkSlice],
        max_window_chars: int,
        input_budget: int,
    ) -> ContextualizationBatch:
        chunks_text = [chunk.text for chunk in chunks]
        own_span = chunks[-1].end - chunks[0].start
        if own_span > max_window_chars:
            raise ContextualizationValidationError(
                "batch source span exceeds the input budget"
            )

        separator_chars = len("\n\n[…]\n\n")
        desired_window_chars = min(
            len(document_text),
            max_window_chars,
            own_span
            + 2 * _WINDOW_MARGIN_CHARS
            + _DOCUMENT_HEAD_CHARS
            + separator_chars,
        )

        # The useful context is the exact batch span plus bounded neighbouring
        # text and the document head. Do not expand to fill a giant model
        # window: that increases cost and distracts the model without adding
        # local meaning. If Unicode density or repeated chunk rendering makes
        # the prompt too large, shrink optional surroundings only; the target
        # span itself is never cut.
        low = own_span
        high = desired_window_chars
        best: ContextualizationBatch | None = None
        smallest: ContextualizationBatch | None = None
        while low <= high:
            available_window = (low + high) // 2
            window, is_excerpt = _document_window(
                document_text,
                chunks,
                max_chars=available_window,
            )
            prompt = build_chunk_context_prompt(
                document_title,
                window,
                chunks_text,
                is_excerpt=is_excerpt,
            )
            plan = ContextualizationBatch(
                chunks=tuple(chunks),
                window=window,
                prompt=prompt,
                is_excerpt=is_excerpt,
                estimated_input_tokens=_estimate_prompt_tokens(prompt),
                reserved_output_tokens=(
                    _CONTEXT_OUTPUT_BASE_TOKENS
                    + len(chunks)
                    * _CONTEXT_OUTPUT_ITEM_RESERVE_TOKENS
                ),
            )
            if smallest is None or len(window) < len(smallest.window):
                smallest = plan
            if plan.estimated_input_tokens <= input_budget:
                best = plan
                low = available_window + 1
            else:
                high = available_window - 1
        return best or smallest  # outer planner emits the typed no-fit error

    @staticmethod
    def _valid_context(context: str) -> bool:
        return bool(context.strip())

    def _structured_output_supported(self, model: str | None) -> bool:
        """Use the provider's existing JSON-schema path when available."""
        checker = getattr(self._llm, "supports_structured_output", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(model=model))
        except TypeError:
            return bool(checker())

    @staticmethod
    def _response_schema(expected: int) -> dict[str, Any]:
        """Require one non-empty context per chunk without a length cap."""
        return {
            "type": "object",
            "properties": {
                "contexts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "chunk_number": {"type": "integer"},
                            "context": {"type": "string", "minLength": 1},
                        },
                        "required": ["chunk_number", "context"],
                        "additionalProperties": False,
                    },
                    "minItems": expected,
                    "maxItems": expected,
                }
            },
            "required": ["contexts"],
            "additionalProperties": False,
        }

    def _batch_contexts(
        self,
        *,
        document_title: str,
        prompt: str,
        expected: int,
        batch_number: int,
        cancel_check: Callable[[], None] | None = None,
    ) -> tuple[list[str], str | None]:
        """One batch's contexts, or none when the content filter blocks it.

        A completion the filter cut off is repeated once, visibly; a request
        it refused is not (it is bound to its content). Either way the batch's
        chunks are then indexed without context and counted, never silently,
        and the revision does not pause for it.
        """
        call = functools.partial(
            self._contexts_for,
            document_title=document_title,
            prompt=prompt,
            expected=expected,
            batch_number=batch_number,
            cancel_check=cancel_check,
        )
        try:
            return call(), None
        except ContextualizationContentFiltered as filtered:
            if not filtered.repeatable:
                return self._omitted_batch(
                    document_title, batch_number, expected, "refused"
                )
            log.warning(
                "contextualization batch %d of %r was blocked by the provider "
                "content filter (finish_reason=content_filter); one visible retry",
                batch_number,
                document_title,
            )
        try:
            return call(), None
        except ContextualizationContentFiltered:
            return self._omitted_batch(
                document_title, batch_number, expected, "blocked twice"
            )

    @staticmethod
    def _omitted_batch(
        document_title: str, batch_number: int, expected: int, how: str
    ) -> tuple[list[str], str]:
        log.warning(
            "contextualization batch %d of %r was %s by the provider content "
            "filter; its %d chunks are indexed without context",
            batch_number,
            document_title,
            how,
            expected,
        )
        return [""] * expected, CONTEXT_OMITTED_CONTENT_FILTER

    def _contexts_for(
        self,
        *,
        document_title: str,
        prompt: str,
        expected: int,
        batch_number: int,
        cancel_check: Callable[[], None] | None = None,
    ) -> list[str]:
        """Return one batch's contexts or raise a typed visible failure."""
        del document_title
        if cancel_check is not None:
            cancel_check()
        resolved_model = self._resolved_model()
        model = resolved_model or "provider-default"
        structured_output = self._structured_output_supported(resolved_model)
        breaker = self._circuit_breaker
        permit: ContextualizationCircuitPermit | None = None
        if breaker is not None:
            try:
                permit = breaker.acquire_contextualization_circuit(
                    provider_key=self._provider_key,
                    model=model,
                    cooldown_seconds=self._circuit_cooldown_seconds,
                    probe_lease_seconds=self._circuit_probe_lease_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed on state loss
                raise ContextualizationDependencyError(
                    error_type="contextualization_circuit_state_unavailable"
                ) from exc
            if permit is None:
                log.warning(
                    "Contextualization provider call suppressed by open circuit",
                    extra={
                        "event": "knowledge.contextualization.circuit.rejected",
                        "provider": self._provider_key,
                        "model": model,
                        "cooldown_seconds": self._circuit_cooldown_seconds,
                    },
                )
                raise ContextualizationDependencyError(
                    error_type="contextualization_provider_circuit_open"
                )

        def _cancel_probe() -> bool:
            if cancel_check is None:
                return False
            try:
                cancel_check()
            except Exception:
                return True
            return False

        try:
            with provider_cancel_scope(
                _cancel_probe if cancel_check is not None else None
            ):
                while not self._provider_gate.acquire(timeout=0.1):
                    if _cancel_probe():
                        cancel_check()
                try:
                    with bind_purpose("knowledge_contextualize"):
                        if structured_output:
                            response = self._llm.complete_structured(
                                prompt,
                                schema=self._response_schema(expected),
                                schema_name=_CONTEXT_RESPONSE_SCHEMA_NAME,
                                schema_description=(
                                    "One source-grounded retrieval context per "
                                    "document chunk."
                                ),
                                model=resolved_model,
                                timeout=self._timeout,
                                max_output_tokens=self._resolved_max_output_tokens(),
                            )
                        else:
                            response = self._llm.complete_with_metadata(
                                prompt,
                                model=resolved_model,
                                timeout=self._timeout,
                                max_output_tokens=self._resolved_max_output_tokens(),
                            )
                finally:
                    self._provider_gate.release()
            if cancel_check is not None:
                cancel_check()
        except Exception as exc:  # noqa: BLE001 - provider boundary is typed here
            # Provider retry helpers raise their generic cancellation type.
            # Re-run the indexing probe so the caller's own cancellation type
            # wins and the job converges to ``cancelled`` rather than a
            # dependency pause.
            if cancel_check is not None:
                try:
                    cancel_check()
                except Exception as cancel_exc:
                    raise cancel_exc from exc
            failure = classify_provider_failure(exc)
            if failure.transient:
                error_type = {
                    ProviderFailureKind.TIMEOUT: "contextualization_provider_timeout",
                    ProviderFailureKind.RATE_LIMITED: (
                        "contextualization_provider_rate_limited"
                    ),
                    ProviderFailureKind.UNAVAILABLE: (
                        "contextualization_provider_unavailable"
                    ),
                }[failure.kind]
                if breaker is not None and permit is not None:
                    try:
                        breaker.record_contextualization_circuit_failure(
                            permit,
                            error_type=error_type,
                        )
                    except Exception as circuit_exc:  # fail closed, never bypass
                        raise ContextualizationDependencyError(
                            error_type=(
                                "contextualization_circuit_state_unavailable"
                            )
                        ) from circuit_exc
                raise ContextualizationDependencyError(
                    error_type=error_type
                ) from exc
            if breaker is not None and permit is not None:
                try:
                    # The circuit tracks transient availability only. A
                    # terminal/configuration rejection must stay terminal and
                    # must not strand a previous half-open probe.
                    breaker.record_contextualization_circuit_success(permit)
                except Exception as circuit_exc:  # fail closed, never bypass
                    raise ContextualizationDependencyError(
                        error_type="contextualization_circuit_state_unavailable"
                    ) from circuit_exc
            if failure.content_refused:
                raise ContextualizationContentFiltered(
                    batch_number, repeatable=False
                ) from exc
            if failure.kind == ProviderFailureKind.TERMINAL:
                raise ContextualizationProviderError() from exc
            raise ContextualizationInternalError() from exc

        if breaker is not None and permit is not None:
            try:
                # A completed transport call proves dependency recovery even
                # when the response subsequently fails deterministic schema
                # validation. Validation remains visible on the job, but it is
                # not a reason to keep the availability circuit open.
                breaker.record_contextualization_circuit_success(permit)
            except Exception as exc:  # noqa: BLE001 - shared state is mandatory
                raise ContextualizationDependencyError(
                    error_type="contextualization_circuit_state_unavailable"
                ) from exc
        finish_reason = str(
            getattr(response, "finish_reason", "") or ""
        ).strip().lower()
        if finish_reason == "content_filter":
            raise ContextualizationContentFiltered(batch_number, repeatable=True)
        if finish_reason in INCOMPLETE_FINISH_REASONS:
            raise ContextualizationValidationError(
                "contextualization provider stopped before completing "
                f"batch {batch_number} (finish_reason={finish_reason})"
            )
        if structured_output:
            parsed = getattr(response, "parsed", None)
            raw_contexts = (
                parsed.get("contexts") if isinstance(parsed, dict) else None
            )
            contexts = self._coerce_contexts(
                raw_contexts,
                expected=expected,
                require_chunk_numbers=True,
            )
        else:
            contexts = self._coerce_contexts(
                self._parse(getattr(response, "content", "") or ""),
                expected=expected,
                require_chunk_numbers=False,
            )
        if contexts is None:
            raise ContextualizationValidationError(
                f"contextualization response invalid at batch {batch_number} "
                f"({expected} chunks)"
            )
        normalized = [context.strip() for context in contexts]
        if any(not context for context in normalized):
            raise ContextualizationValidationError(
                "contextualization response contains an empty context at "
                f"batch {batch_number}"
            )
        return normalized

    @staticmethod
    def _parse(content: str) -> Any:
        candidates = [content.strip()]
        candidates.extend(
            match.group(0)
            for pattern in (_JSON_OBJECT, _JSON_ARRAY)
            if (match := pattern.search(content)) is not None
        )
        for candidate in candidates:
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
            except ValueError:
                continue
            contexts = (
                payload.get("contexts")
                if isinstance(payload, dict)
                else payload
            )
            if isinstance(contexts, list):
                return contexts
        return None

    @staticmethod
    def _coerce_contexts(
        payload: Any,
        *,
        expected: int,
        require_chunk_numbers: bool,
    ) -> list[str] | None:
        if not isinstance(payload, list) or len(payload) != expected:
            return None
        if not require_chunk_numbers and all(
            isinstance(item, str) for item in payload
        ):
            return list(payload)
        contexts: list[str] = []
        for chunk_number, item in enumerate(payload, start=1):
            if (
                not isinstance(item, dict)
                or item.get("chunk_number") != chunk_number
                or not isinstance(item.get("context"), str)
            ):
                return None
            contexts.append(item["context"])
        return contexts


def contextualize_followup_question(
    llm: Any,
    *,
    question: str,
    history: str,
    model: str | None = None,
    timeout: float = REASONING_TIMEOUT,
) -> tuple[ContextualizedQuestion, dict[str, int]]:
    """Rewrite a follow-up into a standalone retrieval query.

    The history is used only for anaphora/topic resolution. Any provider
    or parse failure falls back loudly to the original question so RAG
    remains available and observable.

    Args:
        llm: Provider exposing ``complete_with_metadata``.
        question: Current user question from the active turn.
        history: Prior conversation text. It is context for rewriting only,
            never an evidence source.
        model: Optional routed model name for the contextualization call.
        timeout: Provider timeout in seconds. Defaults to the algorithm's
            reasoning timeout when called from ``KnowledgeAlgorithm``.

    Returns:
        A contextualized question plus prompt/completion token usage. Empty
        history or empty questions return the original question with zero
        usage and the ``_knowledge_query_context_unchanged`` marker.
    """
    original = question.strip()
    if not history.strip() or not original:
        return (
            ContextualizedQuestion(
                question=original,
                marker=QUERY_CONTEXT_MARKER_UNCHANGED,
                rewritten=False,
            ),
            {"prompt_tokens": 0, "completion_tokens": 0},
        )
    prompt = build_knowledge_followup_context_prompt(original, history)
    try:
        with bind_purpose("knowledge_contextualize"):
            response = llm.complete_with_metadata(
                prompt, model=model, timeout=timeout
            )
    except AgentCancelled:
        # A run cancellation is not a provider failure — swallowing it
        # into the fallback would keep a cancelled ask running.
        raise
    except Exception as exc:  # noqa: BLE001 - visible fallback, not fatal
        log.warning(
            "Knowledge follow-up contextualization failed "
            "(error_type=%s); using the original question for retrieval "
            "(%s).",
            type(exc).__name__,
            QUERY_CONTEXT_MARKER_FALLBACK,
        )
        return (
            ContextualizedQuestion(
                question=original,
                marker=QUERY_CONTEXT_MARKER_FALLBACK,
                rewritten=False,
            ),
            {"prompt_tokens": 0, "completion_tokens": 0},
        )

    usage = {
        "prompt_tokens": getattr(response, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(response, "completion_tokens", 0) or 0,
    }
    parsed = _parse_contextualized_question(
        getattr(response, "content", "") or ""
    )
    if parsed is None:
        log.warning(
            "Knowledge follow-up contextualization response was not "
            "parseable; using the original question for retrieval (%s).",
            QUERY_CONTEXT_MARKER_FALLBACK,
        )
        return (
            ContextualizedQuestion(
                question=original,
                marker=QUERY_CONTEXT_MARKER_FALLBACK,
                rewritten=False,
            ),
            usage,
        )

    rewritten = parsed != original
    marker = (
        QUERY_CONTEXT_MARKER_APPLIED
        if rewritten
        else QUERY_CONTEXT_MARKER_UNCHANGED
    )
    return (
        ContextualizedQuestion(
            question=parsed,
            marker=marker,
            rewritten=rewritten,
        ),
        usage,
    )


def _parse_contextualized_question(content: str) -> str | None:
    """Parse the strict JSON object returned by the contextualizer.

    Args:
        content: Raw provider response. Surrounding prose is tolerated only
            when a single JSON object can still be extracted.

    Returns:
        The non-empty ``question`` field, or ``None`` when the response cannot
        be trusted.
    """
    match = _JSON_OBJECT.search(content)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    question = payload.get("question")
    if not isinstance(question, str):
        return None
    question = question.strip()
    return question or None
