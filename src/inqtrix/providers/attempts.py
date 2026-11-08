"""Retries of a provider call reach the caller who made it, on a thread that may report them.

A caller binds a retry log for one kind of provider (``model``, ``search`` or
``rerank``) in its context. The innermost binding of a kind takes the retries of
that kind; the other kinds pass it by.
A context travels where a thread does not: into the fan-out threads that copy the
caller's context, and into the coroutine that :func:`inqtrix.aio.call_from_thread`
runs on the loop. A retry reported on the loop therefore still lands in the log of
the caller who waits for it.

The log's callback writes a visible event, and that write may bridge to the loop.
It runs on the thread that reports the retry when that thread may wait for the
loop, and never on the loop thread or the ``run_blocking`` lane: there the log keeps
the retry until the waiting caller takes it, which :func:`call_on_loop` does while
it waits.

An attempt observer (:func:`bind_attempt_observer`) sees every physical attempt
made in its context, before the request with an :class:`AttemptInfo` and after
it with an :class:`AttemptOutcome`; it travels like a retry log.

The async transport of the built-in adapters lives here as well:
:func:`provider_attempt` is one request, :func:`call_with_retries` the one
retry loop of a logical call, and :class:`AsyncNativeLLMProvider` gives such an
adapter its sync methods as faces over :func:`call_on_loop`.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeVar

from abc import abstractmethod

from inqtrix import aio
from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import AgentProviderTimeout, AgentTimeout
from inqtrix.observability.context import current_purpose
from inqtrix.search_result import GroundedSearchResult
from inqtrix.text import estimate_tokens
from inqtrix.providers.base import (
    _CANCEL_PROBE_POLL_SECONDS,
    ChatTurn,
    LLMProvider,
    LLMResponse,
    ProviderAttemptBudget,
    SearchProvider,
    StructuredLLMResponse,
    _check_deadline,
    _check_provider_cancel,
    _check_provider_operation_deadline,
    _plan_retry,
    current_provider_attempt_budget,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

RetryKind = Literal["model", "search", "rerank"]
"""The kind of provider a retry log listens to."""

RetryCallback = Callable[[dict[str, Any]], None]
"""Receives one retry notice; ``progress_emitted`` is set in what it gets."""


class RetryLog:
    """The retries of one binding, and the callback that reports them.

    A notice counts as reported (``progress_emitted``) only once the callback took
    it without an error, so a caller's own summary still covers the rest.
    """

    def __init__(self, callback: RetryCallback | None) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._notices: list[dict[str, Any]] = []
        self._undelivered: list[dict[str, Any]] = []

    def record(self, notice: Mapping[str, Any]) -> None:
        """Keep one retry and report it here, unless this thread may not wait for the loop."""
        item = dict(notice)
        with self._lock:
            self._notices.append(item)
            if self._callback is None:
                return
            if not aio.may_wait_for_loop():
                self._undelivered.append(item)
                return
        self._deliver(item)

    def deliver_pending(self) -> None:
        """Report the retries kept back on the loop; call it where the caller waits."""
        with self._lock:
            pending, self._undelivered = self._undelivered, []
        for item in pending:
            self._deliver(item)

    def notices(self) -> list[dict[str, Any]]:
        """Every retry this binding took, in the order it took them."""
        with self._lock:
            return [dict(item) for item in self._notices]

    def _deliver(self, item: dict[str, Any]) -> None:
        assert self._callback is not None
        try:
            self._callback({**item, "progress_emitted": True})
        except Exception:
            log.exception("Provider retry observer failed")
            return
        item["progress_emitted"] = True


_RETRY_LOGS: contextvars.ContextVar[Mapping[str, RetryLog]] = contextvars.ContextVar(
    "inqtrix_retry_logs", default=MappingProxyType({})
)


@contextmanager
def observe_retries(kind: RetryKind, callback: RetryCallback | None = None) -> Iterator[RetryLog]:
    """Bind a retry log for *kind* to this context; inside it, it takes that kind's retries."""
    retry_log = RetryLog(callback)
    token = _RETRY_LOGS.set(MappingProxyType({**_RETRY_LOGS.get(), kind: retry_log}))
    try:
        yield retry_log
    finally:
        _RETRY_LOGS.reset(token)


def report_retry(kind: RetryKind, notice: Mapping[str, Any]) -> None:
    """Hand one retry to the log bound for *kind* where the call was made; without one nobody listens."""
    retry_log = _RETRY_LOGS.get().get(kind)
    if retry_log is not None:
        retry_log.record(notice)


AttemptStatus = Literal["ok", "failed", "unknown"]
"""How one attempt ended; ``unknown`` when the request may have reached the provider unanswered."""


@dataclass(frozen=True, slots=True)
class AttemptInfo:
    """One physical attempt of a provider call, as its observers see it before the request."""

    purpose: str
    provider: str
    model: str
    operation: str
    attempt: int
    estimated_input_tokens: int
    max_output_tokens: int | None
    """The output budget actually sent, after raising and clamping; ``None`` when none is sent."""
    reasoning_effort: str | None = None
    """The reasoning effort the request carries, after the adapter's rules; ``None`` when none is sent
    or the adapter does not say."""


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """How one attempt ended, and the retry its failure announced."""

    status: AttemptStatus
    retry: Mapping[str, Any] | None = None


class AttemptObserver(Protocol):
    """Sees the attempts made where it is bound: synchronous, O(1), never I/O, because it runs on the loop."""

    def before_attempt(self, info: AttemptInfo) -> object:
        """Called before the request; the return value comes back to :meth:`after_attempt`."""

    def after_attempt(self, ticket: object, outcome: AttemptOutcome) -> None:
        """Called once the attempt ended, unless :meth:`before_attempt` failed."""


_OBSERVERS: contextvars.ContextVar[tuple[AttemptObserver, ...]] = contextvars.ContextVar(
    "inqtrix_attempt_observers", default=()
)


@contextmanager
def bind_attempt_observer(observer: AttemptObserver) -> Iterator[None]:
    """Let *observer* see every attempt made in this context, also on the loop the call moves to."""
    token = _OBSERVERS.set((*_OBSERVERS.get(), observer))
    try:
        yield
    finally:
        _OBSERVERS.reset(token)


def estimate_request_tokens(*parts: object) -> int:
    """Rough input tokens of a request: :func:`~inqtrix.text.estimate_tokens` over its JSON text.

    Encoded images count as their text, so the estimate errs high for them.
    """
    return estimate_tokens(json.dumps(parts, ensure_ascii=False, default=str))


class AttemptCount:
    """The physical requests of one logical provider call, counted as an attempt observer.

    It also keeps the output budget (``max_output_tokens``) and the reasoning
    effort (``reasoning_effort``) the last request sent, which a trace reports
    next to what the caller asked for.
    """

    __slots__ = ("value", "max_output_tokens", "reasoning_effort")

    def __init__(self) -> None:
        self.value = 0
        self.max_output_tokens: int | None = None
        self.reasoning_effort: str | None = None

    def before_attempt(self, info: AttemptInfo) -> object:
        self.value += 1
        self.max_output_tokens = info.max_output_tokens
        self.reasoning_effort = info.reasoning_effort
        return None

    def after_attempt(self, ticket: object, outcome: AttemptOutcome) -> None:
        return None


@contextmanager
def count_attempts() -> Iterator[AttemptCount]:
    """Count the requests of the logical call made inside this context.

    The count is an attempt observer, so it also counts a call nested inside;
    a traced call has none.
    """
    count = AttemptCount()
    with bind_attempt_observer(count):
        yield count


class AttemptSettlement:
    """What the retry loop tells the attempt it runs in: the retry its caught failure announces."""

    __slots__ = ("retry",)

    def __init__(self) -> None:
        self.retry: Mapping[str, Any] | None = None

    def will_retry(self, notice: Mapping[str, Any]) -> None:
        """Mark the attempt failed; the loop caught its error and sends again."""
        self.retry = MappingProxyType(dict(notice))


def _before_attempt(info: AttemptInfo) -> list[tuple[AttemptObserver, object]]:
    tickets = []
    for observer in _OBSERVERS.get():
        try:
            tickets.append((observer, observer.before_attempt(info)))
        except Exception:
            log.exception("Provider attempt observer failed before the request")
    return tickets


def _after_attempt(tickets: list[tuple[AttemptObserver, object]], outcome: AttemptOutcome) -> None:
    for observer, ticket in tickets:
        try:
            observer.after_attempt(ticket, outcome)
        except Exception:
            log.exception("Provider attempt observer failed after the request")


def call_on_loop(coro: Coroutine[Any, Any, T]) -> T:
    """Await *coro* on the process loop from sync code, reporting its retries on this thread.

    The sync face of an async provider call: what the call reports on the loop
    reaches the callbacks bound here while this thread waits, and the rest when the
    call ends. A run cancelled while this thread waits (the probe of
    :func:`~inqtrix.providers.base.provider_cancel_scope`) ends the call at once with
    :class:`~inqtrix.exceptions.AgentCancelled`; a Bedrock thread runs on to its read
    timeout, as after a deadline.
    """
    logs = tuple(_RETRY_LOGS.get().values())

    def deliver() -> None:
        for retry_log in logs:
            retry_log.deliver_pending()

    def while_waiting() -> None:
        deliver()
        # A cancelled run ends the request it waits for: the bridge cancels
        # the call when this raises, and the attempt ends as unknown.
        _check_provider_cancel()

    try:
        return aio.call_from_thread(coro, while_waiting=while_waiting)
    finally:
        deliver()


@asynccontextmanager
async def provider_attempt(
    info: AttemptInfo,
    *,
    label: str,
    deadline: float,
    outer_deadline: float | None = None,
    budget: ProviderAttemptBudget | None = None,
) -> AsyncIterator[AttemptSettlement]:
    """Exactly one physical attempt of a provider call, one request.

    Before the request: the cooperative cancel probe of a caller on the
    transition lane, the operation deadline, a claim on the call's attempt
    budget, then :meth:`AttemptObserver.before_attempt` of every bound
    observer. The request runs under ``asyncio.timeout`` until the deadline,
    so an expired operation ends as its typed timeout even if the transport
    hangs. The observers learn the outcome: ``ok``, ``failed`` (also when the
    retry loop caught the error and marked a retry on the yielded settlement)
    or ``unknown`` (deadline or cancellation).
    A cancelled attempt ends with ``CancelledError``; the retry loop never
    repeats it.
    """
    _check_provider_cancel(label=label)
    _check_provider_operation_deadline(deadline, outer_deadline, label=label)
    if budget is not None:
        budget.claim()
    tickets = _before_attempt(info)
    settlement = AttemptSettlement()
    status: AttemptStatus = "unknown"
    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            yield settlement
        status = "ok" if settlement.retry is None else "failed"
    except TimeoutError as exc:
        # The timer may fire a hair before the clock passes the deadline, so
        # the type follows the binding deadline, not another clock reading.
        if outer_deadline is not None and outer_deadline <= deadline:
            raise AgentTimeout("Agent time limit exceeded.") from exc
        raise AgentProviderTimeout(f"{label} reached the operation time limit.") from exc
    except Exception:
        status = "failed"
        raise
    finally:
        _after_attempt(tickets, AttemptOutcome(status, settlement.retry))


async def _sleep_before_retry(delay: float, deadline: float) -> None:
    """Wait *delay* seconds, clamped to the deadline, probing for a cancelled run."""
    if delay <= 0:
        return
    _check_provider_cancel()
    _check_deadline(deadline)
    wake_at = time.monotonic() + min(delay, max(0.0, deadline - time.monotonic()))
    while (remaining := wake_at - time.monotonic()) > 0:
        await asyncio.sleep(min(_CANCEL_PROBE_POLL_SECONDS, remaining))
        _check_provider_cancel()


async def call_with_retries(
    send: Callable[[], Awaitable[T]],
    *,
    provider_label: str,
    model: str,
    operation: str,
    deadline: float,
    outer_deadline: float | None,
    timeout_label: str,
    configured_timeout_seconds: float | None = None,
    error_code_for: Callable[[BaseException], str] | None = None,
    request_id_for: Callable[[BaseException], str] | None = None,
    transient_for: Callable[[BaseException], bool] | None = None,
    kind: RetryKind = "model",
    estimated_input_tokens: int = 0,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> T:
    """The one async retry loop of a logical provider call.

    Each request is one :func:`provider_attempt`; the decision per failure is
    :func:`inqtrix.providers.base._plan_retry`, and every retry reaches the
    caller's retry log of *kind*. An adapter whose client raises its own
    transient errors names them with *transient_for*. *max_output_tokens* is
    the output budget the request sends, *reasoning_effort* the effort it
    carries, *estimated_input_tokens* its rough size
    (:func:`estimate_request_tokens`); all reach the attempt observers.
    """
    effective_timeout_seconds = max(0.0, deadline - time.monotonic())
    budget = current_provider_attempt_budget()
    bound_purpose = current_purpose()
    info = AttemptInfo(
        purpose=(bound_purpose.purpose or "") if bound_purpose is not None else "",
        provider=provider_label,
        model=model,
        operation=operation,
        attempt=1,
        estimated_input_tokens=estimated_input_tokens,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
    )
    attempt = 1
    while True:
        async with provider_attempt(
            replace(info, attempt=attempt),
            label=timeout_label,
            deadline=deadline,
            outer_deadline=outer_deadline,
            budget=budget,
        ) as settlement:
            try:
                return await send()
            except Exception as exc:
                plan = _plan_retry(
                    exc,
                    attempt=attempt,
                    budget=budget,
                    provider_label=provider_label,
                    model=model,
                    operation=operation,
                    configured_timeout_seconds=configured_timeout_seconds,
                    effective_timeout_seconds=effective_timeout_seconds,
                    error_code_for=error_code_for,
                    request_id_for=request_id_for,
                    transient_for=transient_for,
                    deadline=deadline,
                )
                if plan is None:
                    raise
                settlement.will_retry(plan[1])
        delay, notice = plan
        report_retry(kind, notice)
        try:
            await _sleep_before_retry(delay, deadline)
        except AgentTimeout:
            _check_provider_operation_deadline(deadline, outer_deadline, label=timeout_label)
            raise
        attempt += 1


class AsyncNativeLLMProvider(LLMProvider):
    """A built-in adapter whose calls are async; its sync methods are faces of them.

    Each sync method waits on its thread while the async call runs on the
    process loop (:func:`call_on_loop`), reports the call's retries there, and
    refuses on the loop's own thread. The async methods are abstract: an
    adapter that left one to the base class would serve it through its own
    sync face, which would wait for the async method again.
    """

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Sync face of :meth:`acomplete`."""
        return call_on_loop(
            self.acomplete(
                prompt, system=system, model=model,
                max_output_tokens=max_output_tokens, timeout=timeout, state=state,
                deadline=deadline, reasoning_effort=reasoning_effort,
            )
        )

    def complete_with_metadata(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Sync face of :meth:`acomplete_with_metadata`."""
        return call_on_loop(
            self.acomplete_with_metadata(
                prompt, system=system, model=model,
                max_output_tokens=max_output_tokens, timeout=timeout, state=state,
                deadline=deadline, reasoning_effort=reasoning_effort,
            )
        )

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        schema_description: str = "",
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> StructuredLLMResponse:
        """Sync face of :meth:`acomplete_structured`."""
        return call_on_loop(
            self.acomplete_structured(
                prompt, schema=schema, schema_name=schema_name,
                schema_description=schema_description, system=system, model=model,
                max_output_tokens=max_output_tokens, timeout=timeout, state=state,
                deadline=deadline, reasoning_effort=reasoning_effort,
            )
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
        tool_choice: str | None = None,
    ) -> ChatTurn:
        """Sync face of :meth:`achat`."""
        return call_on_loop(
            self.achat(
                messages, tools=tools, model=model,
                max_output_tokens=max_output_tokens, timeout=timeout,
                deadline=deadline, reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
        )

    @abstractmethod
    async def acomplete(self, prompt: str, **kwargs: Any) -> str:
        """Generate text; see :meth:`LLMProvider.complete`."""

    @abstractmethod
    async def acomplete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        """Generate text with token metadata; see :meth:`LLMProvider.complete_with_metadata`."""

    @abstractmethod
    async def acomplete_structured(self, prompt: str, **kwargs: Any) -> StructuredLLMResponse:
        """Generate a schema-bound response; see :meth:`LLMProvider.complete_structured`."""

    @abstractmethod
    async def achat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatTurn:
        """Run one tool-calling chat turn; see :meth:`LLMProvider.chat`."""


class AsyncNativeSearchProvider(SearchProvider):
    """A built-in search adapter whose search is async; :meth:`search` is its face.

    The same contract as :class:`AsyncNativeLLMProvider`: the sync method
    waits on its thread while :meth:`asearch` runs on the process loop, and
    :meth:`asearch` is abstract.
    """

    def search(
        self,
        query: str,
        *,
        search_context_size: str = "high",
        recency_filter: str | None = None,
        language_filter: list[str] | None = None,
        domain_filter: list[str] | None = None,
        search_mode: str | None = None,
        return_related: bool = False,
        deadline: float | None = None,
    ) -> GroundedSearchResult:
        """Sync face of :meth:`asearch`."""
        return call_on_loop(
            self.asearch(
                query, search_context_size=search_context_size,
                recency_filter=recency_filter, language_filter=language_filter,
                domain_filter=domain_filter, search_mode=search_mode,
                return_related=return_related, deadline=deadline,
            )
        )

    @abstractmethod
    async def asearch(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        """Run one search; see :meth:`SearchProvider.search`."""

