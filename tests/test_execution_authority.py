"""Behavioral tests for effective-actor provider safepoints."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.core.results import RunRequest
from inqtrix.execution_authority import (
    AuthorizationRevoked,
    guard_provider_context,
    pinned_knowledge_collection_ids,
)
from inqtrix.server.runs import ANSWER_DELTA_MAX_BYTES, RunHandle, RunStore
from inqtrix.services.run_service import execute_run_request
from inqtrix.providers.base import (
    ChatTurn,
    LLMProvider,
    LLMResponse,
    ProviderContext,
    SearchProvider,
    StructuredLLMResponse,
)
from inqtrix.search_result import GroundedSearchResult


class _RecordingLLM(LLMProvider):
    """Provider stub that records externally observable call boundaries."""

    selectable_models = ["reasoning-model"]
    context_window_tokens = 32_000

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def complete(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append(f"llm:{prompt}")
        return "answer"

    def complete_with_metadata(
        self, prompt: str, **kwargs: Any
    ) -> LLMResponse:
        self.calls.append(f"metadata:{prompt}")
        return LLMResponse(content="answer")

    def complete_structured(
        self, prompt: str, **kwargs: Any
    ) -> StructuredLLMResponse:
        self.calls.append(f"structured:{prompt}")
        return StructuredLLMResponse(parsed={"ok": True})

    def chat(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> ChatTurn:
        self.calls.append("chat")
        return ChatTurn(text="answer")

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def is_available(self) -> bool:
        return True


class _RecordingSearch(SearchProvider):
    """Search stub that records the real provider invocation."""

    search_model = "search-model"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        self.calls.append(f"search:{query}")
        return GroundedSearchResult(answer="result")

    def is_available(self) -> bool:
        return True


def _providers(calls: list[str]) -> ProviderContext:
    return ProviderContext(
        llm=_RecordingLLM(calls),
        search=_RecordingSearch(calls),
    )


@pytest.mark.parametrize(
    ("invoke", "provider_call"),
    [
        (lambda context: context.llm.complete("plain"), "llm:plain"),
        (
            lambda context: context.llm.complete_with_metadata("metered"),
            "metadata:metered",
        ),
        (
            lambda context: context.llm.complete_structured(
                "json", schema={}, schema_name="test"
            ),
            "structured:json",
        ),
        (lambda context: context.llm.chat([]), "chat"),
        (lambda context: context.search.search("query"), "search:query"),
    ],
)
def test_external_provider_calls_are_guarded_before_and_after(
    invoke: Any,
    provider_call: str,
) -> None:
    calls: list[str] = []
    guarded = guard_provider_context(
        _providers(calls), lambda: calls.append("check")
    )

    invoke(guarded)

    assert calls == ["check", provider_call, "check"]


def test_revocation_during_external_call_discards_its_return_value() -> None:
    calls: list[str] = []
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1
        calls.append("check")
        if checks == 2:
            raise AuthorizationRevoked("share was revoked")

    guarded = guard_provider_context(_providers(calls), check)

    with pytest.raises(AuthorizationRevoked) as exc_info:
        guarded.llm.complete("in-flight")

    assert exc_info.value.code == "authorization_revoked"
    assert calls == ["check", "llm:in-flight", "check"]


def test_guard_preserves_provider_capability_metadata() -> None:
    guarded = guard_provider_context(_providers([]), lambda: None)

    assert guarded.llm.selectable_models == ["reasoning-model"]
    assert guarded.llm.context_window_tokens == 32_000
    assert guarded.llm.supports_structured_output()
    assert guarded.llm.supports_tool_calls()
    assert guarded.search.search_model == "search-model"


def test_persisted_knowledge_scope_is_immutable_and_missing_fails_closed() -> None:
    assert pinned_knowledge_collection_ids(
        {"collection_ids": ["kc_a", "kc_b", "kc_a"]},
        scoped_principal=True,
    ) == frozenset({"kc_a", "kc_b"})
    assert pinned_knowledge_collection_ids(
        {"collection_ids": []}, scoped_principal=True
    ) == frozenset()
    assert pinned_knowledge_collection_ids(
        {}, scoped_principal=True
    ) == frozenset()
    assert pinned_knowledge_collection_ids(
        {}, scoped_principal=False
    ) is None


@pytest.mark.parametrize(
    "invalid_scope",
    ["kc_a", [""], [1]],
)
def test_malformed_persisted_knowledge_scope_fails_loudly(
    invalid_scope: object,
) -> None:
    with pytest.raises(RuntimeError, match="list of IDs"):
        pinned_knowledge_collection_ids(
            {"collection_ids": invalid_scope},
            scoped_principal=True,
        )


# --- Safepoint frequency ------------------------------------------------- #
#
# Until v0.2.0.5 RunHandle.emit ran a full authority check per EVENT, and
# emit_answer fans the answer into one event per WORD: a 1500-word answer paid
# ~1500 checks (actor probe + a row-locking store check + the pinned-dependency
# probe each) AFTER the answer was already produced. None of it was pinned by a
# test, and none of it was in the documented safepoint list
# (docs/architecture/agent-platform.md) — which the surrounding code already
# implements at its own explicit call sites.


def _stub_providers() -> ProviderContext:
    """Providers the guard can decorate (it wraps llm and search)."""
    calls: list[str] = []
    return ProviderContext(
        llm=_RecordingLLM(calls), search=_RecordingSearch(calls)
    )


class _StubAlgorithm:
    """Minimal algorithm returning one fixed answer."""

    def __init__(self, answer: str) -> None:
        self._answer = answer

    def capabilities(self) -> dict[str, Any]:
        return {"terminal_node": "answer"}

    def run(self, request: Any, *, runtime: Any, context: Any) -> Any:
        context.event_sink("inqtrix.progress.message", {"message": "working"})
        return SimpleNamespace(
            raw={"answer": self._answer, "usage": {}, "result_state": {}},
            cancelled=False,
            cancel_reason=None,
        )


def _running_run() -> tuple[RunStore, str, RunHandle]:
    """A store with one run held in `running`, plus a handle onto it.

    The work closure parks on an Event so the store does not auto-complete
    the run before the body under test has emitted into it.
    """
    store = RunStore(
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=30,
        event_buffer_size=10_000,
    )
    release = threading.Event()
    summary = store.submit(
        question="q",
        stack_name="default",
        work=lambda handle: release.wait(5),
    )
    run_id = str(summary["run_id"])
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if store.get(run_id)["status"] == "running":
            break
        time.sleep(0.01)
    else:  # pragma: no cover - the store failed to dispatch
        pytest.fail("run did not reach running")
    _RELEASES.append(release)
    return store, run_id, RunHandle(store, run_id, threading.Event())


_RELEASES: list[threading.Event] = []


@pytest.fixture(autouse=True)
def _release_parked_work():
    """Let every parked work closure finish, whatever the test did."""
    yield
    for event in _RELEASES:
        event.set()
    _RELEASES.clear()


def _execute_with_counting_check(answer: str) -> tuple[int, list[dict[str, Any]]]:
    """Run one request, returning the check count and the emitted events."""
    store, run_id, handle = _running_run()
    checks: list[int] = []

    execute_run_request(
        handle,
        algorithm=_StubAlgorithm(answer),
        run_request=RunRequest(mode="research", question="q"),
        resolved=SimpleNamespace(
            providers=_stub_providers(),
            strategies=SimpleNamespace(),
            agent_settings=SimpleNamespace(),
        ),
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                quota=SimpleNamespace(max_tokens_per_run=0)
            )
        ),
        principal=None,
        authority_check=lambda: checks.append(1),
    )
    return len(checks), store.subscribe(run_id).replay


def test_authority_checks_do_not_scale_with_answer_length() -> None:
    # The answer is ONE final-publication boundary, not one per word. This is
    # the anti-regression test for the whole defect: it goes red the moment a
    # per-event or per-chunk check comes back, without pinning a magic number.
    short_checks, short_events = _execute_with_counting_check("one two three")
    long_checks, long_events = _execute_with_counting_check(
        " ".join(f"w{i}" for i in range(5_000))
    )

    assert short_checks == long_checks
    # The deltas DO scale with the answer's size — otherwise the test would
    # pass on a broken emit_answer that stopped streaming.
    def _deltas(events: list[dict[str, Any]]) -> int:
        return sum(
            1 for e in events if e["type"] == "inqtrix.output_text.delta"
        )

    assert _deltas(long_events) > _deltas(short_events) >= 1


def test_a_finished_answer_is_published_in_byte_bounded_deltas() -> None:
    """A finished answer is a few deltas, not one per word.

    Every delta is a durable event with its own sequence allocation and
    execution-path check; publishing a finished answer word by word wrote
    thousands of them and showed the text over twenty seconds although it
    was complete. The deltas stay contiguous, so a reader rebuilds the exact
    text, multi-byte characters included.
    """
    answer = " ".join(f"Wort{i} Größe" for i in range(3_000))
    total = len(answer.encode("utf-8"))
    _checks, events = _execute_with_counting_check(answer)
    deltas = [e["data"] for e in events if e["type"] == "inqtrix.output_text.delta"]

    assert len(deltas) <= -(-total // ANSWER_DELTA_MAX_BYTES) + 1
    offset = 0
    for delta in deltas:
        assert delta["offset"] == offset
        size = len(delta["delta"].encode("utf-8"))
        assert size <= ANSWER_DELTA_MAX_BYTES
        offset += size
    assert "".join(delta["delta"] for delta in deltas) == answer
    ready = next(e["data"] for e in events if e["type"] == "inqtrix.answer.ready")
    assert ready["bytes"] == total


def test_event_emission_costs_no_authority_check() -> None:
    # Event emission is not a documented safepoint: it produces no external
    # effect a check could recall, so neither the sink nor RunHandle.emit
    # checks per graph event. The checks are exactly the admission and the
    # two publication boundaries.
    checks, events = _execute_with_counting_check("one two three")
    assert any(e["type"] == "inqtrix.progress.message" for e in events)
    assert checks == 3


def test_revocation_before_publication_emits_no_answer_delta() -> None:
    # Fail closed at the publication boundary: nothing of a revoked actor's
    # answer reaches the event stream.
    store, run_id, handle = _running_run()

    def _revoked() -> None:
        raise AuthorizationRevoked("actor is gone")

    with pytest.raises(AuthorizationRevoked):
        execute_run_request(
            handle,
            algorithm=_StubAlgorithm("secret answer"),
            run_request=RunRequest(mode="research", question="q"),
            resolved=SimpleNamespace(
                providers=_stub_providers(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(),
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
            authority_check=_revoked,
        )
    events = store.subscribe(run_id).replay
    assert not [
        e for e in events if e["type"] == "inqtrix.output_text.delta"
    ]


def test_revocation_after_publication_never_completes_the_run() -> None:
    # The load-bearing half of the final-publication boundary: the
    # post-emit check must still stop the run from completing,
    # so a revoked actor's result payload never persists.
    store, run_id, handle = _running_run()
    calls: list[int] = []

    def _revoke_on_second() -> None:
        calls.append(1)
        if len(calls) >= 3:
            raise AuthorizationRevoked("actor revoked mid-run")

    with pytest.raises(AuthorizationRevoked):
        execute_run_request(
            handle,
            algorithm=_StubAlgorithm("one two three"),
            run_request=RunRequest(mode="research", question="q"),
            resolved=SimpleNamespace(
                providers=_stub_providers(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(),
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
            authority_check=_revoke_on_second,
        )
    assert store.get(run_id)["status"] != "completed"
