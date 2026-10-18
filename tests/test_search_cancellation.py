"""Behavioral pins for run cancellation inside node fan-outs and sections.

Complements ``test_provider_cancel_probe.py``: these tests exercise the
node-level half of the fast-cancel contract — the cancel-aware fan-out
helper ``_map_cancellable`` (shared by the search and claim-extraction
fan-outs), the search node integration, and the per-section checkpoint in
answer composition. Visibility is part of the contract (no silent
fallbacks): abandonment must surface as a warning progress message and a
``cancel_abandoned_work`` iteration-log marker.
"""

from __future__ import annotations

import threading
import time
from queue import Queue
from types import SimpleNamespace

import pytest

from inqtrix.exceptions import AgentCancelled
from inqtrix.nodes import _map_cancellable, answer, search
from inqtrix.providers.base import LLMResponse, ProviderContext
from inqtrix.report_profiles import ReportProfile
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.settings import AgentSettings
from inqtrix.state import initial_state
from inqtrix.strategies import StrategyContext, create_default_strategies


def _drain_progress(queue: Queue) -> list[str]:
    messages = []
    while not queue.empty():
        messages.append(queue.get()[1])
    return messages


def test_map_cancellable_without_cancel_event_matches_ex_map():
    state = initial_state("Frage?", max_total_seconds=30)
    assert "_cancel_event" not in state

    results = _map_cancellable(
        state,
        lambda item: item * 2,
        [1, 2, 3],
        max_workers=2,
        operation_label="Websuche",
    )

    assert results == [2, 4, 6]


def test_map_cancellable_reraises_worker_exception():
    state = initial_state("Frage?", max_total_seconds=30)
    state["_cancel_event"] = threading.Event()

    def fail_on_two(item: int) -> int:
        if item == 2:
            raise ValueError("worker boom")
        return item

    with pytest.raises(ValueError, match="worker boom"):
        _map_cancellable(
            state,
            fail_on_two,
            [1, 2, 3],
            max_workers=2,
            operation_label="Websuche",
        )


def test_map_cancellable_abandons_queue_after_worker_failure():
    """ex.map parity: a fatal worker failure abandons the queued remainder.

    Without the abandonment, all six items would execute (>= 3s with one
    worker); with it, at most the failing item plus one raced-in successor
    run. The executor may hand the NEXT queued item to a freed worker
    before the coordinator reacts, so the pin is a bound, not equality.
    """
    state = initial_state("Frage?", max_total_seconds=60)
    state["_cancel_event"] = threading.Event()
    calls: list[int] = []

    def fail_first_then_slow(item: int) -> int:
        calls.append(item)
        if item == 0:
            raise ValueError("fatal provider failure")
        time.sleep(0.6)
        return item

    started = time.monotonic()
    with pytest.raises(ValueError, match="fatal provider failure"):
        _map_cancellable(
            state,
            fail_first_then_slow,
            [0, 1, 2, 3, 4, 5],
            max_workers=1,
            operation_label="Claim-Extraktion",
        )
    elapsed = time.monotonic() - started

    assert len(calls) <= 2
    assert elapsed < 2.0


def test_map_cancellable_abandons_queued_work_visibly():
    """Cancel mid-fan-out abandons queued items and reports all counts."""
    cancel_event = threading.Event()
    progress_queue = Queue()
    state = initial_state(
        "Was ist passiert?",
        progress_queue=progress_queue,
        max_total_seconds=60,
        cancel_event=cancel_event,
    )

    def slow_call(item: int) -> int:
        # The first running calls request the cancel themselves, then stay
        # busy long enough (vs. the 0.5s coordinator poll) that the third,
        # queued item is deterministically cancelled before it starts.
        cancel_event.set()
        time.sleep(1.5)
        return item

    started = time.monotonic()
    with pytest.raises(AgentCancelled):
        _map_cancellable(
            state,
            slow_call,
            [0, 1, 2],
            max_workers=2,
            operation_label="Websuche",
            testing_mode=True,
        )
    elapsed = time.monotonic() - started

    # The coordinator waited for the two in-flight calls (~1.5s) but never
    # ran the queued third one (which would push past ~3s with 2 workers).
    assert elapsed < 2.5

    markers = [
        entry
        for entry in state["iteration_logs"]
        if entry.get("event") == "cancel_abandoned_work"
    ]
    assert len(markers) == 1
    assert markers[0]["operation"] == "Websuche"
    assert markers[0]["total"] == 3
    assert markers[0]["abandoned"] == 1
    assert markers[0]["in_flight"] == 2

    messages = _drain_progress(progress_queue)
    assert any("Abbruch angefordert" in message for message in messages)


def test_search_node_cancel_mid_fanout_raises_and_reports():
    class _CancelRequestingSearch:
        """Simulates a user cancel arriving while queries are in flight."""

        def __init__(self, cancel_event: threading.Event) -> None:
            self._cancel_event = cancel_event

        def search(self, *a, **kw):
            self._cancel_event.set()
            time.sleep(1.2)
            return GroundedSearchResult(
                answer="Gefundener Text",
                sources=[GroundedSource(url="https://example.com/report", rank=1)],
            )

        def consume_nonfatal_notice(self):
            return None

        def is_available(self):
            return True

    class _UnusedLLM:
        models = SimpleNamespace(
            reasoning_model="reasoning-model",
            effective_claim_extract_model="claim-extract-model",
        )

        def complete(self, *a, **kw):
            raise AssertionError("cancel must stop the node before claims")

        def is_available(self):
            return True

    settings = AgentSettings(
        first_round_queries=2, max_rounds=4, testing_mode=True
    )
    defaults = create_default_strategies(settings)
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=defaults.claim_extraction,
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    cancel_event = threading.Event()
    progress_queue = Queue()
    state = initial_state(
        "Was ist passiert?",
        progress_queue=progress_queue,
        max_total_seconds=60,
        cancel_event=cancel_event,
    )
    state["queries"] = ["q1", "q2"]

    with pytest.raises(AgentCancelled):
        search(
            state,
            providers=ProviderContext(
                llm=_UnusedLLM(), search=_CancelRequestingSearch(cancel_event)
            ),
            strategies=strategies,
            settings=settings,
        )

    markers = [
        entry
        for entry in state["iteration_logs"]
        if entry.get("event") == "cancel_abandoned_work"
    ]
    assert len(markers) == 1
    assert markers[0]["total"] == 2

    messages = _drain_progress(progress_queue)
    assert any("Abbruch angefordert" in message for message in messages)


def _ledger_record(evidence_id: str, url: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "record_type": "source",
        "report_eligible": True,
        "query_id": f"qry_{evidence_id}",
        "query": "test query",
        "canonical_url": url,
        "source_title": "Source report",
        "source_date": "2026-05-10",
        "tier": "mainstream",
        "source_snippet": "Source snippet with substance.",
        "source_passages": [
            {
                "passage_id": f"passage_{evidence_id}",
                "origin": "source_snippet",
                "text": "Source passage with concrete detail.",
            }
        ],
        "citation_set": [
            {"label": "E1.1", "url": url, "role": "source", "title": "Source report"}
        ],
        "claims": [],
    }


def test_answer_stops_after_first_section_on_cancel():
    """The per-section checkpoint stops composition mid-report."""

    class _CancelAfterFirstSectionLLM:
        def __init__(self, cancel_event: threading.Event) -> None:
            self.calls = 0
            self._cancel_event = cancel_event
            self.models = SimpleNamespace(reasoning_model="reasoning-model")

        def complete_with_metadata(self, *a, **kw):
            self.calls += 1
            self._cancel_event.set()
            return LLMResponse(
                content="Abschnittstext [1](https://source1.example/report).",
                prompt_tokens=10,
                completion_tokens=20,
                model="reasoning-model",
                finish_reason="stop",
            )

        def complete(self, *a, **kw):
            raise AssertionError("answer() should use section-wise completions")

        def is_available(self):
            return True

    class _SearchStub:
        def search(self, *a, **kw):
            return GroundedSearchResult()

    settings = AgentSettings(
        report_profile=ReportProfile.COMPACT, testing_mode=True
    )
    defaults = create_default_strategies(settings)
    cancel_event = threading.Event()
    llm = _CancelAfterFirstSectionLLM(cancel_event)
    state = initial_state(
        "Was ist passiert?", max_total_seconds=30, cancel_event=cancel_event
    )
    state["round"] = 1
    state["queries"] = ["q1"]
    state["final_confidence"] = 7
    state["evidence_ledger"] = [
        _ledger_record("ev_1", "https://source1.example/report")
    ]
    state["all_citations"] = ["https://source1.example/report"]

    with pytest.raises(AgentCancelled):
        answer(
            state,
            providers=ProviderContext(llm=llm, search=_SearchStub()),
            strategies=StrategyContext(
                source_tiering=defaults.source_tiering,
                claim_extraction=defaults.claim_extraction,
                claim_consolidation=defaults.claim_consolidation,
                risk_scoring=defaults.risk_scoring,
                stop_criteria=defaults.stop_criteria,
            ),
            settings=settings,
        )

    assert llm.calls == 1
