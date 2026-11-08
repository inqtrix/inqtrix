"""Provider notices reach the caller who made the call, on the caller's thread.

A retry is reported on the process loop, where the provider's call runs. The
retry log bound in the caller's context takes it, and its callback runs where it
may write an event: never on the loop thread, whose writes through the run store's bridge
would be refused.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest

from inqtrix import aio
from inqtrix.providers.attempts import call_on_loop, observe_retries, report_retry


def _notice(attempt: int, provider: str = "Fake") -> dict[str, object]:
    return {
        "provider": provider,
        "model": "fake-model",
        "operation": "chat",
        "error_code": "HTTP 503",
        "status_code": 503,
        "attempt": attempt,
        "max_attempts": 3,
        "delay_seconds": 0.5,
    }


def test_a_retry_reported_on_the_loop_reaches_the_sync_caller_while_it_waits() -> None:
    """The callback runs on the waiting caller's thread, and before the call ends."""
    caller = threading.get_ident()
    delivered = threading.Event()
    written: list[dict[str, object]] = []

    async def write_event(notice: dict[str, object]) -> None:
        written.append(notice)

    def sink(notice: dict[str, object]) -> None:
        assert threading.get_ident() == caller
        # As the run store writes an event: a bridge the loop thread may not take.
        aio.call_from_thread(write_event(dict(notice)))
        delivered.set()

    async def retrying_call() -> str:
        report_retry("model", _notice(1))
        give_up = time.monotonic() + 3.0
        while not delivered.is_set() and time.monotonic() < give_up:
            await asyncio.sleep(0.02)  # the backoff the caller waits through
        report_retry("model", _notice(2))
        return "delivered while waiting" if delivered.is_set() else "only afterwards"

    with observe_retries("model", sink) as log:
        assert call_on_loop(retrying_call()) == "delivered while waiting"

    assert [notice["attempt"] for notice in written] == [1, 2]
    assert [notice["progress_emitted"] for notice in log.notices()] == [True, True]


@pytest.mark.asyncio
async def test_two_tasks_on_one_loop_each_see_only_their_own_retries() -> None:
    async def call(provider: str, attempts: int) -> list[object]:
        with observe_retries("model") as log:
            for attempt in range(1, attempts + 1):
                report_retry("model", _notice(attempt, provider=provider))
                await asyncio.sleep(0)
            return [notice["provider"] for notice in log.notices()]

    first, second = await asyncio.gather(call("A", 3), call("B", 2))

    assert first == ["A", "A", "A"]
    assert second == ["B", "B"]


@pytest.mark.asyncio
async def test_a_retry_on_the_loop_is_recorded_but_not_delivered_there() -> None:
    called: list[object] = []
    with observe_retries("model", called.append) as log:
        report_retry("model", _notice(1))

    assert called == []
    assert [notice.get("progress_emitted", False) for notice in log.notices()] == [False]


def test_the_innermost_binding_of_a_kind_takes_the_retry_and_other_kinds_pass_by() -> None:
    outer: list[dict[str, object]] = []
    inner: list[dict[str, object]] = []
    with observe_retries("model", outer.append):
        with observe_retries("model", inner.append):
            report_retry("model", _notice(1))
        # A search retry inside a model binding: nobody listens for searches here.
        report_retry("search", _notice(2))
        report_retry("model", _notice(3))

    assert [notice["attempt"] for notice in inner] == [1]
    assert [notice["attempt"] for notice in outer] == [3]


def test_a_fan_out_thread_without_its_own_binding_reports_to_the_parent_binding() -> None:
    """The fan-out copies the caller's context, so its threads reach the caller's log."""
    threads: list[int] = []
    with observe_retries("model", lambda notice: threads.append(threading.get_ident())) as log:
        context = contextvars.copy_context()
        worker = threading.Thread(target=context.run, args=(report_retry, "model", _notice(1)))
        worker.start()
        worker.join()

    assert threads == [worker.ident]
    assert [notice["attempt"] for notice in log.notices()] == [1]


def test_a_failing_callback_is_logged_and_the_retry_stays_unreported(caplog) -> None:
    """A retry whose event was not written stays open for the caller's own summary."""

    def broken(notice: dict[str, object]) -> None:
        raise RuntimeError("sink down")

    with observe_retries("model", broken) as log:
        report_retry("model", _notice(1))

    assert "Provider retry observer failed" in caplog.text
    assert [(n["attempt"], n.get("progress_emitted", False)) for n in log.notices()] == [(1, False)]


def test_a_structured_call_without_a_sink_leaves_the_outer_retry_log_in_place() -> None:
    from inqtrix.agents.patterns._structured import observe_bound_model_retries

    outer: list[dict[str, object]] = []
    with observe_retries("model", outer.append):
        with observe_bound_model_retries("agent_plan"):
            report_retry("model", _notice(1))

    assert [notice["attempt"] for notice in outer] == [1]


_OLD_NOTICE_SURFACE = (
    "observe_retries",
    "consume_retry_notices",
    "consume_nonfatal_notice",
    "consume_nonfatal_notice_detail",
)


@pytest.mark.parametrize(
    "provider_class",
    [
        "inqtrix.providers.anthropic:AnthropicLLM",
        "inqtrix.providers.azure:AzureOpenAILLM",
        "inqtrix.providers.azure_web_search:AzureFoundryWebSearch",
        "inqtrix.providers.base:ConfiguredLLMProvider",
        "inqtrix.providers.bedrock:BedrockLLM",
        "inqtrix.providers.litellm:LiteLLM",
        "inqtrix.providers.perplexity:PerplexitySearch",
        "inqtrix.providers.rerankers:CohereRerank",
        "inqtrix.providers.rerankers:LLMReranker",
    ],
)
def test_no_provider_offers_a_thread_local_notice_surface(provider_class: str) -> None:
    """Notices travel with the call's result and retry log; a slot nobody fills is a silent null."""
    import importlib

    module_name, class_name = provider_class.split(":")
    cls = getattr(importlib.import_module(module_name), class_name)

    assert [name for name in _OLD_NOTICE_SURFACE if hasattr(cls, name)] == []
