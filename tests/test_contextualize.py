"""Offline tests for ingestion-time chunk contextualization."""

from __future__ import annotations

import dataclasses

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from inqtrix.exceptions import AgentProviderTimeout, AgentRateLimited
from inqtrix.contextualization_circuit import (
    CircuitAdmission,
    ContextualizationCircuitState,
    MemoryContextualizationCircuitBreaker,
)
from inqtrix.knowledge.contextualize import (
    CONTEXT_MARKER_APPLIED,
    ContextualizationBatchCheckpoint,
    ContextualizationCancelled,
    ContextualizationDependencyError,
    ContextualizationInternalError,
    ContextualizationProviderError,
    ContextualizationValidationError,
    LLMChunkContextualizer,
)
from inqtrix.knowledge.chunking import ChunkSlice
from inqtrix.knowledge.evidence import UnverifiedKnowledgeEvidence
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.base import (
    LLMResponse,
    StructuredLLMResponse,
    _sleep_before_retry,
)
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import ModelSettings

from tests.test_knowledge_engine import StubEmbeddings


def _exact_slices(document: str, chunks: list[str]) -> list[ChunkSlice]:
    """Build deterministic test spans without production prefix matching."""
    cursor = 0
    result: list[ChunkSlice] = []
    for chunk in chunks:
        start = document.index(chunk, cursor)
        end = start + len(chunk)
        result.append(ChunkSlice(chunk, start, end))
        cursor = end
    return result


class ScriptedContextLLM:
    """Returns a fixed payload for every contextualization prompt."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.prompts: list[str] = []

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.prompts.append(prompt)
        content = (
            self._payload
            if isinstance(self._payload, str)
            else json.dumps(self._payload, ensure_ascii=False)
        )
        return LLMResponse(
            content=content,
            prompt_tokens=10,
            completion_tokens=5,
            model="stub-ctx",
            finish_reason="stop",
        )


def test_contexts_are_prefixed_in_chunk_order():
    llm = ScriptedContextLLM(["Kontext A", "Kontext B"])
    contextualizer = LLMChunkContextualizer(llm)

    document = "Absatz eins. Absatz zwei."
    result = contextualizer.contextualize(
        document_title="Artikel 26",
        document_text=document,
        chunks=_exact_slices(document, ["Absatz eins.", "Absatz zwei."]),
    )

    assert result.marker == CONTEXT_MARKER_APPLIED
    assert result.texts == [
        "Kontext A\n\nAbsatz eins.",
        "Kontext B\n\nAbsatz zwei.",
    ]
    # One batched call per document, never per chunk.
    assert len(llm.prompts) == 1
    assert "CHUNK 2" in llm.prompts[0]


def test_native_structured_output_guarantees_one_context_per_chunk():
    class StructuredContextLLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def supports_structured_output(self, *, model=None) -> bool:
            return True

        def complete_structured(
            self,
            prompt: str,
            *,
            schema: dict[str, Any],
            **kwargs: Any,
        ) -> StructuredLLMResponse:
            self.calls.append({"prompt": prompt, "schema": schema, **kwargs})
            return StructuredLLMResponse(
                parsed={
                    "contexts": [
                        {"chunk_number": 1, "context": "Kontext A"},
                        {"chunk_number": 2, "context": "Kontext B"},
                    ]
                },
                content=(
                    '{"contexts":[{"chunk_number":1,"context":"Kontext A"},'
                    '{"chunk_number":2,"context":"Kontext B"}]}'
                ),
                prompt_tokens=10,
                completion_tokens=8,
                model="stub-ctx",
                finish_reason="stop",
            )

        def complete_with_metadata(self, *args: Any, **kwargs: Any) -> LLMResponse:
            raise AssertionError("native structured output must be used")

    llm = StructuredContextLLM()
    document = "Absatz eins. Absatz zwei."

    result = LLMChunkContextualizer(llm).contextualize(
        document_title="Artikel 26",
        document_text=document,
        chunks=_exact_slices(document, ["Absatz eins.", "Absatz zwei."]),
    )

    assert result.contexts == ["Kontext A", "Kontext B"]
    contexts_schema = llm.calls[0]["schema"]["properties"]["contexts"]
    assert contexts_schema["minItems"] == 2
    assert contexts_schema["maxItems"] == 2
    assert "maxLength" not in contexts_schema["items"]["properties"]["context"]


@pytest.mark.parametrize(
    "payload",
    [
        "gar kein JSON",
        ["nur", "ein", "Kontext", "zu", "viel"],
        [1, 2],
        {"falsch": "geformt"},
    ],
)
def test_bad_responses_fail_the_unpublished_revision(payload):
    contextualizer = LLMChunkContextualizer(ScriptedContextLLM(payload))

    document = "a b"
    with pytest.raises(ContextualizationValidationError):
        contextualizer.contextualize(
            document_title="T",
            document_text=document,
            chunks=_exact_slices(document, ["a", "b"]),
        )


@pytest.mark.parametrize("context", ["", "   "])
def test_empty_context_pauses_instead_of_mixing_raw_and_contextualized_chunks(
    context: str,
):
    contextualizer = LLMChunkContextualizer(ScriptedContextLLM([context]))

    with pytest.raises(ContextualizationValidationError):
        contextualizer.contextualize(
            document_title="T",
            document_text="a",
            chunks=[ChunkSlice("a", 0, 1)],
        )


def test_prompt_requests_neighbor_aware_context_without_an_individual_size_cap():
    context = (
        "Der Abschnitt konkretisiert die zuvor eingeführten Betreiberpflichten "
        "für das in der Überschrift benannte Hochrisiko-System und ergänzt "
        "deren Anwendungsbereich. "
    ) * 4
    llm = ScriptedContextLLM([context])
    result = LLMChunkContextualizer(llm).contextualize(
        document_title="T",
        document_text="a",
        chunks=[ChunkSlice("a", 0, 1)],
    )

    assert result.contexts == [context.strip()]
    assert "immediately preceding and following text" in llm.prompts[0]
    assert "one or two precise sentences in the language of the document" in llm.prompts[0]
    assert "belong directly to the content of the respective target section" in llm.prompts[0]
    assert "occurs only in the neighbouring text" in llm.prompts[0]
    assert "Summarise neither the whole document nor the whole excerpt" in llm.prompts[0]
    assert "artificial length limit" in llm.prompts[0]


@pytest.mark.parametrize(
    ("failure", "expected_error_type"),
    [
        (TimeoutError("provider-secret-timeout"), "contextualization_provider_timeout"),
        (
            type(
                "Provider408",
                (RuntimeError,),
                {"status_code": 408},
            )("provider-secret-408"),
            "contextualization_provider_timeout",
        ),
        (
            type(
                "Provider429",
                (RuntimeError,),
                {"status_code": 429},
            )("provider-secret-429"),
            "contextualization_provider_rate_limited",
        ),
        (
            type(
                "Provider503",
                (RuntimeError,),
                {"status_code": 503},
            )("provider-secret-503"),
            "contextualization_provider_unavailable",
        ),
        (
            ConnectionError("provider-secret-transport"),
            "contextualization_provider_unavailable",
        ),
        (
            AgentProviderTimeout("provider-secret-wrapper"),
            "contextualization_provider_timeout",
        ),
        (
            AgentRateLimited(
                "private-provider-deployment",
                RuntimeError("provider-secret-wrapper"),
            ),
            "contextualization_provider_rate_limited",
        ),
    ],
)
def test_only_proven_transient_llm_failures_pause_without_publishing_raw_chunks(
    failure: BaseException,
    expected_error_type: str,
):
    class BrokenLLM:
        def complete_with_metadata(self, *args: Any, **kwargs: Any):
            raise failure

    contextualizer = LLMChunkContextualizer(BrokenLLM())
    with pytest.raises(ContextualizationDependencyError) as caught:
        contextualizer.contextualize(
            document_title="private-document-title",
            document_text="a",
            chunks=[ChunkSlice("a", 0, 1)],
        )
    assert caught.value.error_type == expected_error_type
    assert "provider-secret" not in str(caught.value)
    assert "private-document-title" not in str(caught.value)


def test_open_circuit_suppresses_provider_call_until_one_probe_succeeds() -> None:
    class MutableClock:
        value = 1_000.0

        def __call__(self) -> float:
            return self.value

    class RecoveringLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete_with_metadata(self, *_args: Any, **_kwargs: Any) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("private-provider-timeout")
            return LLMResponse(
                content='["recovered"]',
                prompt_tokens=1,
                completion_tokens=1,
                model="fast",
                finish_reason="stop",
            )

    clock = MutableClock()
    llm = RecoveringLLM()
    breaker = MemoryContextualizationCircuitBreaker(clock=clock)
    contextualizer = LLMChunkContextualizer(
        llm,
        model="fast",
        circuit_cooldown_seconds=10,
        circuit_probe_lease_seconds=30,
        provider_key="azure",
    )
    contextualizer.bind_circuit_breaker(breaker)
    arguments = {
        "document_title": "T",
        "document_text": "a",
        "chunks": [ChunkSlice("a", 0, 1)],
    }

    with pytest.raises(ContextualizationDependencyError) as first:
        contextualizer.contextualize(**arguments)
    assert first.value.error_type == "contextualization_provider_timeout"

    with pytest.raises(ContextualizationDependencyError) as blocked:
        contextualizer.contextualize(**arguments)
    assert blocked.value.error_type == "contextualization_provider_circuit_open"
    assert llm.calls == 1

    clock.value += 10
    result = contextualizer.contextualize(**arguments)
    assert result.contexts == ["recovered"]
    assert llm.calls == 2
    assert breaker.snapshot(provider_key="azure", model="fast")["state"] == "closed"


@pytest.mark.parametrize(
    ("model", "provider_models"),
    [
        ("fast", None),
        (None, None),
        (None, ModelSettings(reasoning_model="base", tier_fast_model="ctx-fast")),
    ],
    ids=["explicit", "provider-default", "routed"],
)
def test_the_circuit_view_reads_the_circuit_the_calls_opened(
    model: str | None, provider_models: ModelSettings | None
) -> None:
    # The auto-resume sweep holds contextualization pauses back while this
    # view admits nothing. Read under any other key it would find no circuit
    # and send every paused job into the open one.
    class TimeoutLLM:
        def complete_with_metadata(self, *_args: Any, **_kwargs: Any) -> LLMResponse:
            raise TimeoutError("private-provider-timeout")

        def complete_structured(self, *_args: Any, **_kwargs: Any) -> Any:
            raise TimeoutError("private-provider-timeout")

    llm = TimeoutLLM()
    if provider_models is not None:
        llm.models = provider_models  # type: ignore[attr-defined]
    contextualizer = LLMChunkContextualizer(llm, model=model, provider_key="azure")
    assert contextualizer.circuit_view() is None
    contextualizer.bind_circuit_breaker(MemoryContextualizationCircuitBreaker())
    assert contextualizer.circuit_view() is None

    with pytest.raises(ContextualizationDependencyError):
        contextualizer.contextualize(
            document_title="T", document_text="a", chunks=[ChunkSlice("a", 0, 1)]
        )

    view = contextualizer.circuit_view()
    assert view is not None
    assert view.state == ContextualizationCircuitState.OPEN
    assert view.admission(time.time()) == CircuitAdmission.NONE


def test_non_transient_provider_error_does_not_open_circuit() -> None:
    class Provider400(RuntimeError):
        status_code = 400

    class BrokenLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete_with_metadata(self, *_args: Any, **_kwargs: Any) -> LLMResponse:
            self.calls += 1
            raise Provider400("private-configuration-detail")

    llm = BrokenLLM()
    breaker = MemoryContextualizationCircuitBreaker()
    contextualizer = LLMChunkContextualizer(
        llm,
        model="fast",
        provider_key="azure",
    )
    contextualizer.bind_circuit_breaker(breaker)

    for _attempt in range(2):
        with pytest.raises(ContextualizationProviderError):
            contextualizer.contextualize(
                document_title="T",
                document_text="a",
                chunks=[ChunkSlice("a", 0, 1)],
            )
    assert llm.calls == 2
    assert breaker.snapshot(provider_key="azure", model="fast") is None


def test_provider_retry_backoff_observes_cancel_in_contextualization_thread() -> None:
    entered_backoff = threading.Event()
    cancelled = threading.Event()

    class RetryingLLM:
        def complete_with_metadata(self, *_args: Any, **_kwargs: Any) -> LLMResponse:
            entered_backoff.set()
            _sleep_before_retry(5.0, time.monotonic() + 10.0)
            raise AssertionError("cancelled backoff must not finish sleeping")

    def cancel_check() -> None:
        if cancelled.is_set():
            raise ContextualizationCancelled("cancelled")

    contextualizer = LLMChunkContextualizer(RetryingLLM(), timeout=10)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            contextualizer.contextualize,
            document_title="T",
            document_text="a",
            chunks=[ChunkSlice("a", 0, 1)],
            cancel_check=cancel_check,
        )
        assert entered_backoff.wait(timeout=1)
        cancelled.set()
        started = time.monotonic()
        with pytest.raises(ContextualizationCancelled):
            future.result(timeout=2)
        assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_non_transient_provider_http_failures_are_terminal_and_redacted(
    status_code: int,
):
    error_type = type(
        f"Provider{status_code}",
        (RuntimeError,),
        {"status_code": status_code},
    )

    class BrokenLLM:
        def complete_with_metadata(self, *args: Any, **kwargs: Any):
            raise error_type("provider-secret-response")

    contextualizer = LLMChunkContextualizer(BrokenLLM())
    with pytest.raises(ContextualizationProviderError) as caught:
        contextualizer.contextualize(
            document_title="private-document-title",
            document_text="a",
            chunks=[ChunkSlice("a", 0, 1)],
        )
    assert "provider-secret" not in str(caught.value)
    assert "private-document-title" not in str(caught.value)


def test_programming_failure_is_terminal_and_redacted():
    class BrokenLLM:
        def complete_with_metadata(self, *args: Any, **kwargs: Any):
            raise RuntimeError("provider-secret-program-state")

    contextualizer = LLMChunkContextualizer(BrokenLLM())
    with pytest.raises(ContextualizationInternalError) as caught:
        contextualizer.contextualize(
            document_title="private-document-title",
            document_text="a",
            chunks=[ChunkSlice("a", 0, 1)],
        )
    assert "provider-secret" not in str(caught.value)
    assert "private-document-title" not in str(caught.value)


def test_explicit_bad_request_cannot_be_overridden_by_nested_timeout():
    error_type = type(
        "Provider400",
        (RuntimeError,),
        {"status_code": 400},
    )

    class BrokenLLM:
        def complete_with_metadata(self, *args: Any, **kwargs: Any):
            try:
                raise TimeoutError("provider-secret-timeout")
            except TimeoutError as cause:
                raise error_type("provider-secret-bad-request") from cause

    contextualizer = LLMChunkContextualizer(BrokenLLM())
    with pytest.raises(ContextualizationProviderError):
        contextualizer.contextualize(
            document_title="private-document-title",
            document_text="a",
            chunks=[ChunkSlice("a", 0, 1)],
        )


def test_provider_call_uses_the_same_model_output_ceiling_as_batch_planning(
    monkeypatch: pytest.MonkeyPatch,
):
    class SmallOutputCard:
        context_window_tokens = 32_000
        max_output_tokens = 1_024

    class CapturingLLM(ScriptedContextLLM):
        def __init__(self) -> None:
            super().__init__(["Kontext"])
            self.max_output_tokens: int | None = None

        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            self.max_output_tokens = int(kwargs["max_output_tokens"])
            return super().complete_with_metadata(prompt, **kwargs)

    monkeypatch.setattr(
        "inqtrix.knowledge.contextualize.resolve_model_card",
        lambda _model: SmallOutputCard(),
    )
    llm = CapturingLLM()
    contextualizer = LLMChunkContextualizer(
        llm,
        model="small-output-model",
        max_output_tokens=4_096,
    )

    contextualizer.contextualize(
        document_title="T",
        document_text="a",
        chunks=[ChunkSlice("a", 0, 1)],
    )

    assert llm.max_output_tokens == 1_024


def test_empty_chunks_short_circuit_without_llm_call():
    llm = ScriptedContextLLM([])
    contextualizer = LLMChunkContextualizer(llm)
    result = contextualizer.contextualize(
        document_title="T", document_text="x", chunks=[]
    )
    assert result.texts == []
    assert llm.prompts == []


class BatchScriptedLLM:
    """Answers each call with one context per chunk found in that prompt."""

    def __init__(self, *, fail_batches: set[int] | None = None) -> None:
        self.prompts: list[str] = []
        self._fail = fail_batches or set()

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.prompts.append(prompt)
        count = prompt.count("CHUNK ")
        payload = (
            "kaputt"
            if len(self.prompts) in self._fail
            else json.dumps([f"Kontext {n}" for n in range(count)])
        )
        return LLMResponse(
            content=payload,
            prompt_tokens=10,
            completion_tokens=5,
            model="stub-ctx",
            finish_reason="stop",
        )


def test_large_documents_are_contextualized_in_batches():
    """A reply carries one context per chunk, so hundreds need several calls."""
    llm = BatchScriptedLLM()
    contextualizer = LLMChunkContextualizer(llm)
    chunks = [f"Abschnitt {n} mit Inhalt." for n in range(60)]
    document = " ".join(chunks)

    result = contextualizer.contextualize(
        document_title="Gross.pdf",
        document_text=document,
        chunks=_exact_slices(document, chunks),
    )

    assert result.marker == CONTEXT_MARKER_APPLIED
    assert len(llm.prompts) == 3
    assert all(text.startswith("Kontext ") for text in result.texts)
    assert len(result.texts) == len(chunks)


def test_a_failing_batch_prevents_mixed_generation_publication():
    llm = BatchScriptedLLM(fail_batches={2})
    contextualizer = LLMChunkContextualizer(llm)
    chunks = [f"Abschnitt {n}." for n in range(120)]

    document = " ".join(chunks)
    with pytest.raises(ContextualizationValidationError):
        contextualizer.contextualize(
            document_title="Gross.pdf",
            document_text=document,
            chunks=_exact_slices(document, chunks),
        )
    # The initial bounded fan-out may already be in flight. The first failure
    # opens the circuit, so the remaining two batches are never dispatched.
    assert len(llm.prompts) == 3


def test_resume_reuses_only_validated_completed_batch_outputs():
    chunks = [f"Abschnitt {n}." for n in range(30)]
    document = " ".join(chunks)
    captured = []
    first_llm = BatchScriptedLLM(fail_batches={2})
    first = LLMChunkContextualizer(first_llm)

    with pytest.raises(ContextualizationValidationError):
        first.contextualize(
            document_title="Gross.pdf",
            document_text=document,
            chunks=_exact_slices(document, chunks),
            on_batch_checkpoint=captured.append,
        )

    assert {checkpoint.batch_number for checkpoint in captured} == {1}
    resumed_llm = BatchScriptedLLM()
    resumed = LLMChunkContextualizer(resumed_llm)
    result = resumed.contextualize(
        document_title="Gross.pdf",
        document_text=document,
        chunks=_exact_slices(document, chunks),
        completed_batches=captured,
    )

    assert len(resumed_llm.prompts) == 1
    assert len(result.contexts) == 30


def test_resume_discards_a_checkpoint_with_an_empty_context_and_recomputes(caplog):
    """A stored checkpoint with an empty context is unusable; it is discarded
    with a WARNING and the batch runs again instead of failing the revision."""
    import logging

    chunks = [ChunkSlice("a", 0, 1)]
    document = "a"
    first = LLMChunkContextualizer(ScriptedContextLLM(["Kontext"]))
    captured = []
    first.contextualize(
        document_title="T",
        document_text=document,
        chunks=chunks,
        on_batch_checkpoint=captured.append,
    )
    invalid = captured[0].__class__(
        batch_number=captured[0].batch_number,
        total_batches=captured[0].total_batches,
        start_chunk=captured[0].start_chunk,
        chunk_count=captured[0].chunk_count,
        prompt_hash=captured[0].prompt_hash,
        model=captured[0].model,
        contexts=("",),
    )

    resumed_llm = ScriptedContextLLM(["Neuer Kontext"])
    with caplog.at_level(logging.WARNING):
        result = LLMChunkContextualizer(resumed_llm).contextualize(
            document_title="T",
            document_text=document,
            chunks=chunks,
            completed_batches=[invalid],
        )
    assert result.contexts == ["Neuer Kontext"]
    assert len(resumed_llm.prompts) == 1
    assert any("checkpoints discarded" in record.getMessage() for record in caplog.records)
def test_each_batch_sees_the_part_of_the_document_it_describes():
    """A long document is excerpted AROUND the chunks, never from the top.

    With a fixed prefix the model would have to invent context for every
    chunk past it — the failure mode this windowing exists to prevent.
    """
    llm = BatchScriptedLLM()
    contextualizer = LLMChunkContextualizer(llm)
    filler = "F" * 30_000
    early = [f"FRUEHER ABSCHNITT {n}. " for n in range(25)]
    late = [f"SPAETER ABSCHNITT {n}. " for n in range(25)]
    document = "KOPFZEILE DES DOKUMENTS. " + "".join(early) + filler * 4 + "".join(late)

    contextualizer.contextualize(
        document_title="Riesig.pdf",
        document_text=document,
        chunks=_exact_slices(document, early + late),
    )

    assert len(llm.prompts) >= 2
    # The batch about the late chunks must actually contain them, and the
    # document's opening travels along so the model knows what it is reading.
    late_prompt = next(
        prompt for prompt in llm.prompts if "SPAETER ABSCHNITT 24" in prompt
    )
    assert "KOPFZEILE DES DOKUMENTS" in late_prompt
    assert "DOCUMENT EXCERPT" in late_prompt


def test_repeated_boilerplate_uses_exact_chunk_spans_for_late_batch():
    llm = BatchScriptedLLM()
    contextualizer = LLMChunkContextualizer(llm)
    common = "IDENTISCHER BOILERPLATE " + "x" * 220
    chunks = [f"{common} EINDEUTIG-{index:02d}." for index in range(26)]
    document = "\n".join(chunks)

    contextualizer.contextualize(
        document_title="Boilerplate.pdf",
        document_text=document,
        chunks=_exact_slices(document, chunks),
    )

    assert len(llm.prompts) == 2
    last_prompt = next(
        prompt
        for prompt in llm.prompts
        if "EINDEUTIG-25" in prompt.split("SECTIONS:\n", 1)[-1]
    )
    described = last_prompt.split("SECTIONS:\n", 1)[-1]
    assert "EINDEUTIG-00" not in described


def test_twenty_kilobyte_chunks_are_batched_by_model_budget_not_fixed_count():
    llm = BatchScriptedLLM()
    llm.context_window_tokens = 32_000
    contextualizer = LLMChunkContextualizer(llm)
    chunks = [f"CHUNK-{index} " + (chr(65 + index) * 20_000) for index in range(3)]
    document = "\n".join(chunks)

    result = contextualizer.contextualize(
        document_title="Gross.pdf",
        document_text=document,
        chunks=_exact_slices(document, chunks),
    )

    assert result.batch_count == 3
    assert all(prompt.count("CHUNK ") == 1 for prompt in llm.prompts)


@pytest.mark.parametrize("text", ["漢" * 5_000, "😀" * 4_000])
def test_non_ascii_prompt_overflow_is_rejected_before_provider_dispatch(
    text: str,
):
    """CJK/emoji cannot hide behind a Latin chars-per-token divisor."""
    llm = BatchScriptedLLM()
    llm.context_window_tokens = 16_000
    contextualizer = LLMChunkContextualizer(llm, max_output_tokens=2_048)

    with pytest.raises(ContextualizationValidationError, match="does not fit"):
        contextualizer.contextualize(
            document_title="Unicode.pdf",
            document_text=text,
            chunks=[ChunkSlice(text, 0, len(text))],
        )

    assert llm.prompts == []


def test_cancel_stops_new_batches_and_checkpoints_only_validated_results():
    llm = BatchScriptedLLM()
    contextualizer = LLMChunkContextualizer(llm)
    chunks = [f"Abschnitt {index}." for index in range(96)]
    document = " ".join(chunks)
    checkpoints = []
    completed = 0

    def on_completed(_current: int, _total: int) -> None:
        nonlocal completed
        completed += 1

    def cancel_check() -> None:
        if completed:
            raise ContextualizationCancelled("cancelled")

    with pytest.raises(ContextualizationCancelled):
        contextualizer.contextualize(
            document_title="Gross.pdf",
            document_text=document,
            chunks=_exact_slices(document, chunks),
            on_batch_checkpoint=checkpoints.append,
            on_batch_completed=on_completed,
            cancel_check=cancel_check,
        )

    # Three calls may already be in flight; the fourth must never start.
    assert len(llm.prompts) == 3
    assert len(checkpoints) == 1
    assert checkpoints[0].batch_number in {1, 2, 3}


class OutOfOrderContextLLM:
    """Completes three provider calls out of order under a measurable gate."""

    max_llm_concurrency = 3

    def __init__(self, *, fail_starts: set[int] | None = None) -> None:
        self.fail_starts = fail_starts or set()
        self.prompts: list[str] = []
        self.completion_order: list[int] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(3)

    @staticmethod
    def _start(prompt: str) -> int:
        sections = prompt.split("SECTIONS:\n", 1)[1]
        match = re.search(r"ITEM-(\d+)", sections)
        assert match is not None
        return int(match.group(1))

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        start = self._start(prompt)
        count = prompt.count("CHUNK ")
        with self._lock:
            self.prompts.append(prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self._barrier.wait(timeout=2)
            time.sleep({0: 0.12, 25: 0.01, 50: 0.05}.get(start, 0))
            if start in self.fail_starts:
                raise TimeoutError(f"dependency failed for {start}")
            with self._lock:
                self.completion_order.append(start)
            return LLMResponse(
                content=json.dumps(
                    [f"Kontext {start + index}" for index in range(count)]
                ),
                prompt_tokens=10,
                completion_tokens=5,
                model="stub-ctx",
                finish_reason="stop",
            )
        finally:
            with self._lock:
                self.active -= 1


def _parallel_fixture() -> tuple[str, list[ChunkSlice]]:
    texts = [f"ITEM-{index:03d}." for index in range(72)]
    document = " ".join(texts)
    return document, _exact_slices(document, texts)


def test_contextualization_runs_at_most_three_and_assembles_source_order():
    llm = OutOfOrderContextLLM()
    contextualizer = LLMChunkContextualizer(llm)
    document, chunks = _parallel_fixture()
    checkpoints = []

    result = contextualizer.contextualize(
        document_title="Parallel.pdf",
        document_text=document,
        chunks=chunks,
        on_batch_checkpoint=checkpoints.append,
    )

    assert llm.max_active == 3
    assert llm.completion_order == [25, 50, 0]
    assert [checkpoint.batch_number for checkpoint in checkpoints] == [2, 3, 1]
    assert result.contexts == [f"Kontext {index}" for index in range(72)]


def test_provider_cap_is_shared_across_simultaneous_documents():
    class CappedLLM:
        max_llm_concurrency = 2

        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self._lock = threading.Lock()

        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            count = prompt.count("CHUNK ")
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.04)
                return LLMResponse(
                    content=json.dumps(["Kontext" for _ in range(count)]),
                    prompt_tokens=10,
                    completion_tokens=5,
                    model="stub-ctx",
                    finish_reason="stop",
                )
            finally:
                with self._lock:
                    self.active -= 1

    llm = CappedLLM()
    contextualizer = LLMChunkContextualizer(llm)
    texts = [f"Abschnitt {index}." for index in range(48)]
    document = " ".join(texts)
    chunks = _exact_slices(document, texts)

    def run(title: str):
        return contextualizer.contextualize(
            document_title=title,
            document_text=document,
            chunks=chunks,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ["A.pdf", "B.pdf"]))

    assert llm.max_active == 2
    assert all(len(result.contexts) == 48 for result in results)


def test_resume_accepts_non_contiguous_checkpoints_and_calls_only_missing_batch():
    first_llm = OutOfOrderContextLLM(fail_starts={25})
    contextualizer = LLMChunkContextualizer(first_llm)
    document, chunks = _parallel_fixture()
    checkpoints = []

    with pytest.raises(ContextualizationDependencyError):
        contextualizer.contextualize(
            document_title="Parallel.pdf",
            document_text=document,
            chunks=chunks,
            on_batch_checkpoint=checkpoints.append,
        )

    assert {checkpoint.batch_number for checkpoint in checkpoints} == {1, 3}

    class ResumeLLM:
        max_llm_concurrency = 3

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            self.prompts.append(prompt)
            count = prompt.count("CHUNK ")
            return LLMResponse(
                content=json.dumps(
                    [f"Kontext {25 + index}" for index in range(count)]
                ),
                prompt_tokens=10,
                completion_tokens=5,
                model="stub-ctx",
                finish_reason="stop",
            )

    resume_llm = ResumeLLM()
    resumed = LLMChunkContextualizer(resume_llm).contextualize(
        document_title="Parallel.pdf",
        document_text=document,
        chunks=chunks,
        completed_batches=checkpoints,
    )

    assert len(resume_llm.prompts) == 1
    assert "ITEM-025" in resume_llm.prompts[0].split("SECTIONS:\n", 1)[1]
    assert resumed.contexts == [f"Kontext {index}" for index in range(72)]


@pytest.mark.parametrize(
    ("max_output_tokens", "expected_batch_limit"),
    [(1_024, 7), (2_048, 15), (4_096, 25)],
)
def test_batch_planner_reserves_valid_json_output_space(
    monkeypatch: pytest.MonkeyPatch,
    max_output_tokens: int,
    expected_batch_limit: int,
):
    class OutputCard:
        context_window_tokens = 64_000

    OutputCard.max_output_tokens = max_output_tokens
    monkeypatch.setattr(
        "inqtrix.knowledge.contextualize.resolve_model_card",
        lambda _model: OutputCard(),
    )
    texts = [f"Abschnitt {index}." for index in range(30)]
    document = " ".join(texts)
    contextualizer = LLMChunkContextualizer(
        BatchScriptedLLM(), model="bounded-output"
    )

    plans = contextualizer.plan_batches(
        document_title="T",
        document_text=document,
        chunks=_exact_slices(document, texts),
    )

    assert max(len(plan.chunks) for plan in plans) == expected_batch_limit
    assert all(plan.reserved_output_tokens <= max_output_tokens for plan in plans)


def test_impossible_output_contract_fails_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
):
    class TinyOutputCard:
        context_window_tokens = 64_000
        max_output_tokens = 128

    monkeypatch.setattr(
        "inqtrix.knowledge.contextualize.resolve_model_card",
        lambda _model: TinyOutputCard(),
    )
    llm = BatchScriptedLLM()

    with pytest.raises(ContextualizationValidationError, match="output budget"):
        LLMChunkContextualizer(llm, model="tiny-output").contextualize(
            document_title="T",
            document_text="Ein Abschnitt.",
            chunks=[ChunkSlice("Ein Abschnitt.", 0, 14)],
        )

    assert llm.prompts == []


def test_short_documents_are_passed_whole():
    llm = BatchScriptedLLM()
    contextualizer = LLMChunkContextualizer(llm)

    contextualizer.contextualize(
        document_title="Klein.pdf",
        document_text="Kurzer Text",
        chunks=[ChunkSlice("Kurzer Text", 0, 11)],
    )

    assert "DOCUMENT (title: Klein.pdf)" in llm.prompts[0]
    assert "DOCUMENT EXCERPT" not in llm.prompts[0]


# ------------------------------------------------------------------ #
# Service integration
# ------------------------------------------------------------------ #


def make_service(contextualizer=None) -> KnowledgeService:
    return KnowledgeService(
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=MemoryKnowledgeStore(),
            default_top_k=4,
            contextualizer=contextualizer,
        ),
        chunk_max_chars=2_000,
        max_document_chars=100_000,
    )


@pytest.mark.asyncio
async def test_ingestion_applies_contexts_and_records_the_marker():
    llm = ScriptedContextLLM(["Pflichten der Betreiber, Artikel 26."])
    service = make_service(LLMChunkContextualizer(llm))
    collection = await service.create_collection(name="K")

    document = await service.add_document(
        collection_id=collection.id,
        title="Artikel 26",
        text="Die Betreiber treffen geeignete Massnahmen.",
    )

    assert document.metadata["_chunk_context"] == CONTEXT_MARKER_APPLIED
    # The retrieval text (chunk) carries the prefix; the document text
    # stays original for the sources view.
    candidates = await service.search(query="Pflichten Betreiber Artikel")
    assert candidates[0].chunk.text.startswith(
        "Pflichten der Betreiber, Artikel 26."
    )
    assert document.text == "Die Betreiber treffen geeignete Massnahmen."


@pytest.mark.asyncio
async def test_without_contextualizer_ingestion_is_unchanged():
    service = make_service()
    collection = await service.create_collection(name="K")
    document = await service.add_document(
        collection_id=collection.id, title="T", text="Inhalt."
    )
    assert "_chunk_context" not in document.metadata


def test_container_requires_llm_when_contextualize_is_on():
    from inqtrix.server.container import build_knowledge_context
    from inqtrix.settings import KnowledgeSettings, Settings

    settings = Settings(
        knowledge=KnowledgeSettings(enabled=True, contextualize="on")
    )
    with pytest.raises(RuntimeError, match="LLM provider"):
        build_knowledge_context(settings, llm=None)


def test_the_answer_prompt_carries_source_text_not_the_generated_prefix():
    """The context prefix is a retrieval artifact, never answer evidence.

    It is model-composed prose that can state facts the chunk itself does
    not contain, so an answer built on it would read as grounded while
    citing nothing — and quote verification, which runs against the source
    text, could not catch a paraphrase of it.
    """
    from inqtrix.knowledge.algorithm import _render_evidence_entry
    from inqtrix.knowledge.stores.ports import DocumentChunk, RetrievalCandidate

    source_text = "Der Umsatz stieg um 3 %."
    chunk = DocumentChunk(
        id="kch_1",
        document_id="kd_1",
        collection_id="kc_1",
        chunk_index=0,
        text=(
            "Dieser Abschnitt stammt aus dem Quartalsbericht; der Vorquartalsumsatz "
            "betrug 314 Millionen Euro.\n\nDer Umsatz stieg um 3 %."
        ),
        embedding=[0.0],
        source_text=source_text,
        page_number=None,
        source_start=0,
        source_end=len(source_text.encode("utf-8")),
        document_content_hash=hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest(),
        revision_id="rev_1",
        generation_id="gen_1",
        source_verified=True,
    )
    entry = _render_evidence_entry(
        1, RetrievalCandidate(chunk=chunk, score=1.0, document_title="Bericht.pdf")
    )

    assert "Der Umsatz stieg um 3 %." in entry
    # The figure exists only in the generated prefix — it must not reach the
    # answering model as if the document had said it.
    assert "314 Millionen" not in entry


def test_evidence_without_verified_source_fails_closed():
    """Legacy retrieval text never becomes answer evidence implicitly."""
    from inqtrix.knowledge.algorithm import _render_evidence_entry
    from inqtrix.knowledge.stores.ports import DocumentChunk, RetrievalCandidate

    chunk = DocumentChunk(
        id="kch_2",
        document_id="kd_1",
        collection_id="kc_1",
        chunk_index=0,
        text="Alter Chunk ohne getrennten Quelltext.",
        embedding=[0.0],
        source_text="",
        page_number=None,
    )
    with pytest.raises(UnverifiedKnowledgeEvidence):
        _render_evidence_entry(
            1,
            RetrievalCandidate(
                chunk=chunk, score=1.0, document_title="Alt.pdf"
            ),
        )


def test_followup_contextualization_reraises_cancellation() -> None:
    """A run cancel is not a provider failure — no fallback may eat it."""
    from inqtrix.exceptions import AgentCancelled
    from inqtrix.knowledge.contextualize import (
        contextualize_followup_question,
    )

    class CancelledLLM:
        def complete_with_metadata(self, *_args: Any, **_kwargs: Any):
            raise AgentCancelled("stop")

    with pytest.raises(AgentCancelled):
        contextualize_followup_question(
            CancelledLLM(),
            question="Und die Haftung?",
            history="Nutzer: Wie ist der Rahmenvertrag geregelt?",
        )


def test_stale_checkpoints_are_discarded_with_a_warning_and_the_document_restarts(caplog):
    """A checkpoint written under another prompt (or model, or batch plan)
    must never be mixed with fresh contexts inside one document: every
    stored batch is discarded, a WARNING says so, and all batches are
    contextualized again. Before, the revision failed with a validation
    error and had to be re-run by hand."""
    import logging

    chunks = [f"Abschnitt {n}." for n in range(30)]
    document = " ".join(chunks)
    captured = []
    first = LLMChunkContextualizer(BatchScriptedLLM(fail_batches={2}))
    with pytest.raises(ContextualizationValidationError):
        first.contextualize(
            document_title="Gross.pdf",
            document_text=document,
            chunks=_exact_slices(document, chunks),
            on_batch_checkpoint=captured.append,
        )
    assert {checkpoint.batch_number for checkpoint in captured} == {1}
    stale = [dataclasses.replace(captured[0], prompt_hash="0" * 64)]

    resumed_llm = BatchScriptedLLM()
    with caplog.at_level(logging.WARNING):
        result = LLMChunkContextualizer(resumed_llm).contextualize(
            document_title="Gross.pdf",
            document_text=document,
            chunks=_exact_slices(document, chunks),
            completed_batches=stale,
        )
    assert len(result.contexts) == 30
    # Both batches ran again: nothing of the stale generation survived.
    assert len(resumed_llm.prompts) == 2
    assert any("checkpoints discarded" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_ingestion_records_the_context_prompt_version():
    from inqtrix.prompts import CHUNK_CONTEXT_PROMPT_VERSION

    llm = ScriptedContextLLM(["Pflichten der Betreiber, Artikel 26."])
    service = make_service(LLMChunkContextualizer(llm))
    collection = await service.create_collection(name="K")
    document = await service.add_document(
        collection_id=collection.id,
        title="Artikel 26",
        text="Die Betreiber treffen geeignete Massnahmen.",
    )
    assert document.metadata["_chunk_context_prompt_version"] == CHUNK_CONTEXT_PROMPT_VERSION
    assert "in the language of the document" in llm.prompts[0]


class FilteringLLM(BatchScriptedLLM):
    """Answers like :class:`BatchScriptedLLM`, except for the batch of
    ``batch_chunks`` chunks (the document window shows every chunk's text in
    every prompt, so the batch is told apart by its own chunk entries): the
    provider's content filter cuts that completion off ``times`` times, or
    refuses the request outright."""

    def __init__(self, *, batch_chunks: int, times: int, refuse: bool = False) -> None:
        super().__init__()
        self._batch_chunks = batch_chunks
        self._times = times
        self._refuse = refuse
        self.blocked_calls = 0

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        if (
            prompt.count("CHUNK ") == self._batch_chunks
            and self.blocked_calls < self._times
        ):
            self.blocked_calls += 1
            self.prompts.append(prompt)
            if self._refuse:
                from inqtrix.exceptions import AzureOpenAIAPIError

                raise AzureOpenAIAPIError(
                    model="ctx", status_code=400, error_code="content_filter"
                )
            return LLMResponse(
                content="",
                prompt_tokens=10,
                completion_tokens=0,
                model="stub-ctx",
                finish_reason="content_filter",
            )
        return super().complete_with_metadata(prompt, **kwargs)


def _thirty_chunks() -> tuple[str, list[ChunkSlice]]:
    """Two batches: chunks 0-24 and chunks 25-29."""
    chunks = [f"Abschnitt {n}." for n in range(30)]
    document = " ".join(chunks)
    return document, _exact_slices(document, chunks)


def test_a_filtered_completion_is_retried_once_and_keeps_its_contexts(caplog):
    # The output filter is not deterministic: one visible retry, and a batch
    # that passes on it is contextualized like any other.
    import logging

    document, slices = _thirty_chunks()
    llm = FilteringLLM(batch_chunks=5, times=1)
    with caplog.at_level(logging.WARNING):
        result = LLMChunkContextualizer(llm).contextualize(
            document_title="Gross.pdf", document_text=document, chunks=slices
        )

    assert result.marker == CONTEXT_MARKER_APPLIED
    assert result.omitted_chunks == 0
    assert all(text.startswith("Kontext ") for text in result.texts)
    assert len(llm.prompts) == 3
    assert any("one visible retry" in record.getMessage() for record in caplog.records)


def test_a_batch_filtered_twice_is_indexed_without_context_and_counted(caplog):
    # Filtered again: that batch's chunks carry no context, the rest keep
    # theirs, and the omission is counted instead of pausing the revision.
    import logging

    from inqtrix.knowledge.contextualize import CONTEXT_MARKER_PARTIAL

    document, slices = _thirty_chunks()
    llm = FilteringLLM(batch_chunks=5, times=2)
    with caplog.at_level(logging.WARNING):
        result = LLMChunkContextualizer(llm).contextualize(
            document_title="Gross.pdf", document_text=document, chunks=slices
        )

    assert result.marker == CONTEXT_MARKER_PARTIAL
    assert result.omitted_chunks == 5
    assert result.texts[25:] == [chunk.text for chunk in slices[25:]]
    assert result.contexts[25:] == [""] * 5
    assert all(text.startswith("Kontext ") for text in result.texts[:25])
    assert llm.blocked_calls == 2
    assert any("without context" in record.getMessage() for record in caplog.records)


def test_a_request_the_filter_refused_is_not_repeated():
    # A refused request is bound to its content: repeating it cannot pass,
    # so the batch goes without context at once instead of failing the
    # whole document.
    document, slices = _thirty_chunks()
    llm = FilteringLLM(batch_chunks=5, times=5, refuse=True)

    result = LLMChunkContextualizer(llm).contextualize(
        document_title="Gross.pdf", document_text=document, chunks=slices
    )

    assert llm.blocked_calls == 1
    assert result.omitted_chunks == 5


def test_a_completion_cut_by_the_output_budget_still_pauses_the_revision():
    # Not a content filter: a repetition would hit the same budget, and
    # dropping the context silently would hide a real problem.
    class LengthLLM(BatchScriptedLLM):
        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            if prompt.count("CHUNK ") == 5:
                self.prompts.append(prompt)
                return LLMResponse(
                    content="",
                    prompt_tokens=10,
                    completion_tokens=0,
                    model="stub-ctx",
                    finish_reason="length",
                )
            return super().complete_with_metadata(prompt, **kwargs)

    document, slices = _thirty_chunks()
    with pytest.raises(ContextualizationValidationError, match="finish_reason=length"):
        LLMChunkContextualizer(LengthLLM()).contextualize(
            document_title="Gross.pdf", document_text=document, chunks=slices
        )


def test_resume_keeps_an_omitted_batch_without_asking_again():
    # The omission is a decision already made: a later resume neither asks
    # the provider again nor discards the checkpoint as invalid.
    document, slices = _thirty_chunks()
    captured: list[Any] = []
    LLMChunkContextualizer(FilteringLLM(batch_chunks=5, times=2)).contextualize(
        document_title="Gross.pdf",
        document_text=document,
        chunks=slices,
        on_batch_checkpoint=captured.append,
    )
    # The job stores checkpoints as JSON and reads them back for the resume.
    stored = [
        ContextualizationBatchCheckpoint.from_dict(json.loads(json.dumps(item.as_dict())))
        for item in captured
    ]
    resumed_llm = BatchScriptedLLM()

    result = LLMChunkContextualizer(resumed_llm).contextualize(
        document_title="Gross.pdf",
        document_text=document,
        chunks=slices,
        completed_batches=stored,
    )

    assert resumed_llm.prompts == []
    assert result.omitted_chunks == 5
    assert result.contexts[25:] == [""] * 5


@pytest.mark.asyncio
async def test_a_document_records_which_chunks_went_without_context():
    # The count travels with the document: the reader of the collection sees
    # it, and the omitted chunk is searchable by its own text.
    from inqtrix.knowledge.contextualize import CONTEXT_MARKER_PARTIAL

    llm = FilteringLLM(batch_chunks=1, times=2)
    service = make_service(LLMChunkContextualizer(llm))
    collection = await service.create_collection(name="K")

    document = await service.add_document(
        collection_id=collection.id,
        title="Artikel 26",
        text="Die Betreiber treffen geeignete Massnahmen.",
    )

    assert document.metadata["_chunk_context"] == CONTEXT_MARKER_PARTIAL
    assert document.metadata["_chunk_context_omitted"] == {
        "chunks": 1,
        "reason": "content_filter",
    }
    candidates = await service.search(query="Betreiber Massnahmen")
    assert candidates[0].chunk.text == "Die Betreiber treffen geeignete Massnahmen."
    assert candidates[0].chunk.retrieval_context is None


def test_a_cancel_while_the_retry_waits_stops_it_before_the_provider(caplog):
    # The user cancels just as the filtered batch is announced for its one
    # retry: the retry checks the cancel itself and never reaches the provider.
    document, slices = _thirty_chunks()
    llm = FilteringLLM(batch_chunks=5, times=5)
    cancelled = threading.Event()

    class CancelOnRetry(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "one visible retry" in record.getMessage():
                cancelled.set()

    def cancel_check() -> None:
        if cancelled.is_set():
            raise ContextualizationCancelled("cancelled by the user")

    handler = CancelOnRetry()
    logger = logging.getLogger("inqtrix")
    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            with pytest.raises(ContextualizationCancelled):
                LLMChunkContextualizer(llm).contextualize(
                    document_title="Gross.pdf",
                    document_text=document,
                    chunks=slices,
                    cancel_check=cancel_check,
                )
    finally:
        logger.removeHandler(handler)
    assert cancelled.is_set()
    assert llm.blocked_calls == 1


def test_a_cancel_during_the_filtered_attempt_prevents_the_retry():
    # The one retry never outlives the user's cancel: once the job is
    # cancelled, the filtered batch is not asked again.
    document, slices = _thirty_chunks()
    llm = FilteringLLM(batch_chunks=5, times=5)

    def cancel_check() -> None:
        if llm.blocked_calls:
            raise ContextualizationCancelled("cancelled by the user")

    with pytest.raises(ContextualizationCancelled):
        LLMChunkContextualizer(llm).contextualize(
            document_title="Gross.pdf",
            document_text=document,
            chunks=slices,
            cancel_check=cancel_check,
        )
    assert llm.blocked_calls == 1
