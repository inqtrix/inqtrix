"""Provider abstractions, response types, and shared infrastructure.

Defines the abstract base classes :class:`LLMProvider` and
:class:`SearchProvider`, frozen response dataclasses, deadline helpers,
the :class:`_NonFatalNoticeMixin` for thread-safe fallback notices, the
:class:`ConfiguredLLMProvider` adapter, and the :class:`ProviderContext`
container.

All concrete provider implementations (LiteLLM, Anthropic, Bedrock, etc.)
import their base contracts and shared utilities from this module.
"""

from __future__ import annotations

import json
import contextvars
import logging
import math
import random
import threading
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, ContextManager, Iterator

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import (
    AgentCancelled,
    AgentModelCapacityError,
    AgentProviderAttemptsExceeded,
    AgentProviderTimeout,
    AgentStructuredOutputError,
    AgentToolArgumentsError,
    AgentTimeout,
    ModelCapacityReason,
)
from inqtrix.search_result import GroundedSearchResult

if TYPE_CHECKING:
    from inqtrix.settings import ModelSettings

log = logging.getLogger("inqtrix")

# =========================================================
# Retry budget for OpenAI-SDK-based providers
# =========================================================
# Inqtrix disables hidden SDK retries and owns one visible retry authority.
# The contract is three TOTAL attempts inside one logical-operation deadline,
# not three retries per layer.  The ``*_MAX_RETRIES`` aliases remain because
# replay tests and provider modules intentionally patch them to zero.
MAX_PROVIDER_ATTEMPTS = 3
_SDK_MAX_RETRIES = MAX_PROVIDER_ATTEMPTS - 1
_RETRYABLE_SDK_STATUS_CODES = frozenset({408, 409, 500, 502, 503, 504})
_RETRYABLE_SDK_ERROR_TYPES = frozenset({"APIConnectionError", "APITimeoutError"})

# Rate-limit (HTTP 429) handling retains a separately patchable ceiling for
# replay tests, but it shares the operation's single attempt counter with
# transport and 5xx failures. Retry-After is honoured and every sleep is
# clamped to the same operation/run deadline; mixed failures can therefore
# never stack independent retry budgets.
_SDK_RATE_LIMIT_MAX_RETRIES = MAX_PROVIDER_ATTEMPTS - 1

ProviderRetryCallback = Callable[[dict[str, Any]], None]


@dataclass
class ProviderAttemptBudget:
    """One request's shared admission budget for transport and model repair.

    Bound only around the final model invocation; independent summarization
    calls keep their own provider operation. The object never lives on a
    shared provider or compiled graph.
    """

    deadline: float | None = None
    used: int = 0
    max_attempts: int = MAX_PROVIDER_ATTEMPTS

    @property
    def remaining(self) -> int:
        return max(0, self.max_attempts - self.used)

    def check(self) -> None:
        _check_provider_cancel()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise AgentProviderTimeout("Model request reached its shared time limit.")
        if not self.remaining:
            raise AgentProviderAttemptsExceeded("Model request exhausted its provider attempts.")

    def claim(self) -> None:
        self.check()
        self.used += 1


_PROVIDER_ATTEMPT_BUDGET: contextvars.ContextVar[ProviderAttemptBudget | None] = (
    contextvars.ContextVar("inqtrix_provider_attempt_budget", default=None)
)


def current_provider_attempt_budget() -> ProviderAttemptBudget | None:
    return _PROVIDER_ATTEMPT_BUDGET.get()


def provider_attempt_timeout(timeout: float) -> float:
    """Clamp one adapter's timeout to the logical model operation."""
    budget = current_provider_attempt_budget()
    if budget is None:
        return timeout
    budget.check()
    return (
        min(timeout, budget.deadline - time.monotonic())
        if budget.deadline is not None else timeout
    )


@contextmanager
def provider_attempt_scope(budget: ProviderAttemptBudget) -> Iterator[None]:
    """Make retries consume the caller's budget without widening a parent."""
    parent = current_provider_attempt_budget()
    token = _PROVIDER_ATTEMPT_BUDGET.set(parent or budget)
    try:
        yield
    finally:
        _PROVIDER_ATTEMPT_BUDGET.reset(token)


def observe_provider_retries(
    provider: object,
    callback: ProviderRetryCallback | None,
) -> ContextManager[object]:
    """Bind one retry observer when the provider exposes the shared seam.

    Adapters that do not implement retry diagnostics remain compatible and
    simply return a no-op context. Keeping this duck-typed binding here lets
    Research nodes and capability-backed Agent tools share one retry bridge.
    """
    observer = getattr(provider, "observe_retries", None)
    if callback is None or not callable(observer):
        return nullcontext()
    return observer(callback)

# =========================================================
# Shared retry / backoff constants
# =========================================================
# Exponential backoff — delay doubles each attempt (1, 2, 4, 8, 8, …)
# but is capped at _BACKOFF_MAX_SECONDS.  Jitter spreads concurrent
# retries to avoid thundering-herd bursts against rate-limited APIs.
_BACKOFF_BASE_SECONDS: float = 1.0
_BACKOFF_MAX_SECONDS: float = 8.0
_JITTER_RANGE: tuple[float, float] = (0.5, 1.5)
# Additive spread (seconds) layered ON TOP of a honoured Retry-After. The
# server hint is a floor we must respect, so this only ever ADDS delay -- it
# desynchronises concurrent callers (e.g. the claim-extraction fan-out) that
# would otherwise receive the same Retry-After and wake in lockstep, re-bursting
# the endpoint. Kept small so it barely extends the honoured wait.
_RETRY_AFTER_JITTER_RANGE: tuple[float, float] = (0.0, 1.0)

# Floor for max_tokens when extended thinking is enabled.  Anthropic
# counts thinking tokens *inside* max_tokens, so a low budget leaves
# almost nothing for the visible answer.
_THINKING_MIN_MAX_TOKENS: int = 16_384

_SEARCH_PARAMETER_NAMES = frozenset({
    "search_context_size",
    "recency_filter",
    "language_filter",
    "domain_filter",
    "search_mode",
    "return_related",
})

# =========================================================
# Internal deadline helpers
# =========================================================


def _check_deadline(deadline: float) -> None:
    """Raise AgentTimeout when the deadline has passed."""
    if time.monotonic() > deadline:
        raise AgentTimeout(
            "Agent time limit exceeded."
        )


def _bounded_timeout(
    default_timeout: int | float, deadline: float | None = None
) -> float:
    """Clamp API timeout to the remaining agent time budget."""
    if deadline is None:
        return float(default_timeout)
    _check_deadline(deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AgentTimeout(
            "Agent time limit exceeded."
        )
    return min(float(default_timeout), remaining)


def _operation_deadline(
    timeout: int | float,
    outer_deadline: float | None = None,
) -> float:
    """Return one absolute deadline shared by all attempts of an operation.

    ``timeout`` is the budget for the complete logical provider operation,
    including retries and backoff.  ``outer_deadline`` is an optional run
    ceiling; the earlier boundary wins.  Computing this once at operation
    entry prevents a retry from silently receiving a fresh full timeout.
    """
    local_deadline = time.monotonic() + max(0.0, float(timeout))
    resolved = (
        min(local_deadline, outer_deadline)
        if outer_deadline is not None
        else local_deadline
    )
    budget = current_provider_attempt_budget()
    if budget is not None:
        budget.deadline = (
            min(resolved, budget.deadline)
            if budget.deadline is not None else resolved
        )
        return budget.deadline
    return resolved


def _check_provider_operation_deadline(
    operation_deadline: float,
    outer_deadline: float | None,
    *,
    label: str,
) -> None:
    """Raise the correctly typed timeout for one provider operation.

    An outer run deadline remains :class:`AgentTimeout`; expiry of the local
    logical-operation budget is :class:`AgentProviderTimeout`.  Keeping the
    distinction here prevents a slow provider call from being misreported as
    exhaustion of the complete research run.
    """
    if time.monotonic() <= operation_deadline:
        return
    if outer_deadline is not None and outer_deadline <= operation_deadline:
        _check_deadline(outer_deadline)
    raise AgentProviderTimeout(f'{label} reached the operation time limit.')


# =========================================================
# Run-cancellation probe (execution-context local, optional)
# =========================================================
# How often a backoff sleep re-checks the bound cancel probe. A module
# constant rather than a setting: it bounds internal wake-up granularity,
# not any operator-tunable behaviour.
_CANCEL_PROBE_POLL_SECONDS: float = 0.5

_cancel_probe: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar(
    "inqtrix_provider_cancel_probe", default=None
)


@contextmanager
def provider_cancel_scope(
    probe: Callable[[], bool] | None,
) -> Iterator[None]:
    """Bind one run-cancellation probe to the current execution context.

    Providers consult the probe before every retry attempt and while
    sleeping between attempts, so a cancelled run stops within
    ``_CANCEL_PROBE_POLL_SECONDS`` of the request instead of serving the
    full retry ladder. The binding follows execution-context copies and keeps
    provider signatures unchanged, including async model adapters that execute
    synchronous providers in an executor.

    Args:
        probe: Zero-argument callable returning ``True`` once the run has
            been cancelled (typically ``threading.Event.is_set``). ``None``
            binds nothing and preserves the historical behaviour exactly
            (library mode, custom adapters) — note that inside an outer
            scope, ``None`` therefore INHERITS that scope rather than
            suspending it; there is deliberately no way to mark a
            sub-call uncancellable.
    """
    if probe is None:
        yield
        return
    token = _cancel_probe.set(probe)
    try:
        yield
    finally:
        _cancel_probe.reset(token)


def _provider_cancel_requested() -> bool:
    """Return whether the bound cancel probe reports cancellation.

    Never raises: a failing probe is logged and treated as "not
    cancelled" so a diagnostics problem cannot abort a healthy run.
    """
    probe = _cancel_probe.get()
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:
        log.exception("Provider cancel probe failed")
        return False


def _check_provider_cancel(*, label: str = "Provider call") -> None:
    """Raise :class:`AgentCancelled` when the bound probe reports cancel.

    Args:
        label: Operation label used in the log line and exception message
            so the abandoned call is identifiable in run diagnostics.

    Raises:
        AgentCancelled: When a probe is bound and reports cancellation.
            The graph unwinds this into the run's ``cancelled`` terminal
            state; no retry ladder or backoff sleep is served first.
    """
    if not _provider_cancel_requested():
        return
    log.warning(
        "%s abandoned after run cancellation was requested.", label
    )
    raise AgentCancelled(
        f'{label} cancelled after cancellation was requested.'
    )


class _NonFatalNoticeMixin:
    """Thread-local helper for surfacing provider fallback notices."""

    _nonfatal_init_lock = threading.Lock()

    def _notice_state(self) -> threading.local:
        """Return the thread-local state object, creating it on first access."""
        state = getattr(self, "_nonfatal_notice_state", None)
        if state is None:
            with self._nonfatal_init_lock:
                state = getattr(self, "_nonfatal_notice_state", None)
                if state is None:
                    state = threading.local()
                    self._nonfatal_notice_state = state
        return state

    def _set_nonfatal_notice(
        self,
        message: str,
        *,
        code: str = "",
        http_status: int | None = None,
    ) -> None:
        """Store one structured fallback notice for the current thread.

        ``consume_nonfatal_notice()`` remains the compatibility surface used
        by the research graph. Capability adapters may consume the additive
        detail shape in the SAME provider thread to distinguish a genuine
        upstream failure from an honest empty result.
        """
        if message:
            self._notice_state().notice = {
                "message": message,
                **({"code": code} if code else {}),
                **(
                    {"http_status": int(http_status)}
                    if http_status is not None
                    else {}
                ),
            }

    def _clear_nonfatal_notice(self) -> None:
        """Clear any pending notice for the current thread."""
        state = self._notice_state()
        if hasattr(state, "notice"):
            delattr(state, "notice")
        # Compatibility with provider instances that were populated by an
        # older implementation before a hot reload.
        if hasattr(state, "message"):
            delattr(state, "message")

    def consume_nonfatal_notice(self) -> str | None:
        """Return and clear the pending notice, or ``None`` if none exists."""
        detail = self.consume_nonfatal_notice_detail()
        return str(detail.get("message")) if detail else None

    def consume_nonfatal_notice_detail(self) -> dict[str, Any] | None:
        """Return and clear the current thread's structured fallback fact."""
        state = self._notice_state()
        raw = getattr(state, "notice", None)
        if hasattr(state, "notice"):
            delattr(state, "notice")
        if isinstance(raw, dict) and raw.get("message"):
            return dict(raw)
        message = getattr(state, "message", None)
        if hasattr(state, "message"):
            delattr(state, "message")
        return {"message": str(message)} if message else None


class _RetryNoticeMixin:
    """Thread-local helper for surfacing provider retry attempts."""

    _retry_notice_init_lock = threading.Lock()

    def _retry_notice_state(self) -> threading.local:
        """Return the thread-local retry diagnostics state."""
        state = getattr(self, "_retry_notice_state_obj", None)
        if state is None:
            with self._retry_notice_init_lock:
                state = getattr(self, "_retry_notice_state_obj", None)
                if state is None:
                    state = threading.local()
                    self._retry_notice_state_obj = state
        return state

    @contextmanager
    def observe_retries(self, callback: ProviderRetryCallback) -> Iterator[object]:
        """Call *callback* synchronously whenever this thread retries."""
        state = self._retry_notice_state()
        previous = getattr(state, "retry_callback", None)
        state.retry_callback = callback
        try:
            yield self
        finally:
            if previous is not None:
                state.retry_callback = previous
            else:
                try:
                    delattr(state, "retry_callback")
                except AttributeError:
                    pass

    def _append_retry_notice(self, notice: dict[str, Any]) -> None:
        """Store retry diagnostics and notify any active observer."""
        state = self._retry_notice_state()
        item = dict(notice)
        callback = getattr(state, "retry_callback", None)
        if callable(callback):
            item["progress_emitted"] = True
            try:
                callback(dict(item))
            except Exception:
                log.exception("Provider retry observer failed")
        notices = getattr(state, "notices", None)
        if not isinstance(notices, list):
            notices = []
        notices.append(item)
        state.notices = notices

    def _clear_retry_notices(self) -> None:
        """Clear pending retry diagnostics for the current thread."""
        state = self._retry_notice_state()
        if hasattr(state, "notices"):
            delattr(state, "notices")

    def consume_retry_notices(self) -> list[dict[str, Any]]:
        """Return and clear retry diagnostics for the current thread."""
        state = self._retry_notice_state()
        notices = getattr(state, "notices", None)
        if hasattr(state, "notices"):
            delattr(state, "notices")
        if not isinstance(notices, list):
            return []
        return [dict(item) for item in notices if isinstance(item, dict)]


@dataclass(frozen=True, slots=True)
class SearchProviderCapabilities:
    """Describe which generic search hints a provider supports.

    This is an additive helper for the Baukastensystem: providers may
    expose a capability object without changing the stable
    :class:`SearchProvider` contract. Callers should treat omitted
    capability metadata as "supports everything" to preserve backward
    compatibility with existing providers and custom user adapters.

    Attributes:
        supported_parameters: Search-hint names accepted by the backend.
            Valid values correspond to the optional keyword parameters of
            :meth:`SearchProvider.search` except ``deadline``, which is
            always handled separately.
        max_concurrency: Optional provider-level cap for simultaneous search
            calls. ``0`` means no cap beyond the agent's own query limit.
    """

    supported_parameters: frozenset[str] = _SEARCH_PARAMETER_NAMES
    max_concurrency: int = 0

    def supports(self, parameter_name: str) -> bool:
        """Return whether the provider accepts a generic search hint."""
        return parameter_name in self.supported_parameters


def get_search_provider_capabilities(provider: object) -> SearchProviderCapabilities:
    """Resolve optional capability metadata from a search provider.

    Providers may expose either a ``search_capabilities`` attribute that
    returns a :class:`SearchProviderCapabilities` instance or a simpler
    ``supported_search_parameters`` attribute returning an iterable of
    accepted parameter names. When neither is present, the default is to
    preserve today's behavior and assume all generic hints are allowed.
    """

    def _max_concurrency() -> int:
        try:
            raw = int(getattr(provider, "max_search_concurrency", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, raw)

    raw_capabilities = getattr(provider, "search_capabilities", None)
    if callable(raw_capabilities):
        raw_capabilities = raw_capabilities()
    if isinstance(raw_capabilities, SearchProviderCapabilities):
        return raw_capabilities

    raw_supported = getattr(provider, "supported_search_parameters", None)
    if callable(raw_supported):
        raw_supported = raw_supported()
    if raw_supported is None:
        return SearchProviderCapabilities(max_concurrency=_max_concurrency())

    try:
        supported = frozenset(
            str(name).strip()
            for name in raw_supported
            if str(name).strip()
        )
    except TypeError:
        return SearchProviderCapabilities(max_concurrency=_max_concurrency())

    return SearchProviderCapabilities(
        supported_parameters=supported or _SEARCH_PARAMETER_NAMES,
        max_concurrency=_max_concurrency(),
    )


DEFAULT_LLM_FANOUT = 4
"""Fallback simultaneous LLM-call width when a provider declares no
``max_llm_concurrency``. Deliberately modest — the per-round claim
extraction fans out one LLM call per search result, and against a
provider with a per-minute request/token ceiling a wide unpaced burst is
exactly what trips a 429. Sized to sit comfortably under typical
provider RPM headroom; a provider that knows its real limit overrides it
via the capability below (constructor-first — providers never read env)."""


@dataclass(frozen=True, slots=True)
class LLMProviderCapabilities:
    """Describe generic LLM-call hints a provider supports.

    Additive Baukasten helper, parallel to
    :class:`SearchProviderCapabilities`: a provider may advertise its
    real simultaneous-call ceiling without changing the stable
    :class:`LLMProvider` contract. Callers treat an omitted cap as
    "use :data:`DEFAULT_LLM_FANOUT`" so existing providers and custom
    adapters keep working unchanged.

    Attributes:
        max_concurrency: Provider-declared cap for simultaneous LLM
            calls (e.g. the claim-extraction fan-out). ``0`` means the
            provider declares none — callers fall back to
            :data:`DEFAULT_LLM_FANOUT`, never to 0 workers.
    """

    max_concurrency: int = 0


def get_llm_provider_capabilities(provider: object) -> LLMProviderCapabilities:
    """Resolve optional LLM capability metadata from a provider.

    Mirrors :func:`get_search_provider_capabilities`: a provider may
    expose an ``llm_capabilities`` attribute returning a
    :class:`LLMProviderCapabilities`, or the simpler
    ``max_llm_concurrency`` scalar attribute. Neither present → an empty
    capability (``max_concurrency=0``), i.e. defer to
    :data:`DEFAULT_LLM_FANOUT`.
    """

    def _max_concurrency() -> int:
        try:
            raw = int(getattr(provider, "max_llm_concurrency", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, raw)

    raw_capabilities = getattr(provider, "llm_capabilities", None)
    if callable(raw_capabilities):
        raw_capabilities = raw_capabilities()
    if isinstance(raw_capabilities, LLMProviderCapabilities):
        return raw_capabilities
    return LLMProviderCapabilities(max_concurrency=_max_concurrency())


def reported_capability(
    provider: object, name: str, *, model: str | None = None
) -> bool:
    """Ask a provider about an optional capability without assuming it.

    The stable :class:`LLMProvider` contract lets an adapter leave a
    capability unimplemented, and a provider passes through several
    decorators (configuration, tracing, execution authority) before
    anyone asks. THE single place that reads such a predicate, so a
    provider that declares none answers "absent" at every layer instead
    of raising through whichever decorator forwarded the question.

    Args:
        provider: Provider or decorator to ask.
        name: Predicate to read, e.g. ``"supports_tool_calls"``.
        model: Model the question is about; passed on when the
            implementation accepts the keyword.

    Returns:
        The provider's answer, ``False`` when it declares none.
    """
    checker = getattr(provider, name, None)
    if not callable(checker):
        return False
    try:
        return bool(checker(model=model))
    except TypeError:
        return bool(checker())


# --------------------------------------------------------------------------- #
# Per-call reasoning-effort helpers (provider-neutral)
# --------------------------------------------------------------------------- #

REASONING_EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")
"""Graded reasoning-effort levels, weakest to strongest, in the neutral vocabulary.

``""`` (inherit the provider's constructor default) and ``"none"`` (force
reasoning off for this call) are handled separately and are not listed here.
"""


def normalize_reasoning_effort(value: str | None) -> str:
    """Lower-case and trim a per-call reasoning-effort token.

    Args:
        value: A caller-supplied effort token, or ``None``.

    Returns:
        The normalized token. ``""`` means "inherit the provider's constructor
        default"; ``"none"`` means "force reasoning off for this call"; any
        other value is a (possibly provider-unsupported) graded level.
    """
    return (value or "").strip().lower()


def validate_reasoning_effort(
    value: str,
    *,
    supported_levels: tuple[str, ...],
    label: str = "",
) -> tuple[str, list[str]]:
    """Validate a normalized graded effort token against a provider's levels.

    ``""`` (inherit) and ``"none"`` (off) always pass through unchanged. A
    graded level outside *supported_levels* is downgraded to ``"none"`` and a
    human-readable warning is returned so the caller can surface it
    (no silent fallbacks).

    Args:
        value: A normalized effort token (see :func:`normalize_reasoning_effort`).
        supported_levels: The graded levels this provider/model accepts.
        label: Optional provider/model label included in the warning text.

    Returns:
        A ``(effective_effort, warnings)`` tuple. ``effective_effort`` is
        ``""``, ``"none"``, or one of *supported_levels*; ``warnings`` is empty
        unless a downgrade happened.
    """
    if value in ("", "none") or value in supported_levels:
        return value, []
    prefix = f"{label}: " if label else ""
    warning = (
        f"{prefix}reasoning_effort={value!r} is not supported "
        f"(allowed: {', '.join(supported_levels)}); reasoning disabled for "
        f"this call."
    )
    return "none", [warning]


# =========================================================
# Shared retry helpers (Anthropic & Bedrock)
# =========================================================


def _retry_delay_seconds(
    attempt: int, retry_after: str | None = None
) -> float:
    """Compute retry delay with exponential backoff and jitter.

    If the server sent a ``Retry-After`` header, honour it as a floor and
    add a small positive jitter on top, so concurrent callers handed the
    same header still desynchronise instead of waking in lockstep and
    re-bursting the endpoint. Otherwise use exponential backoff
    (base * 2^attempt, capped) multiplied by a random jitter factor.
    """
    if retry_after:
        try:
            parsed = float(retry_after)
        except ValueError:
            try:
                parsed = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                parsed = 0.0
        if math.isfinite(parsed) and parsed > 0:
            return parsed + random.uniform(*_RETRY_AFTER_JITTER_RANGE)
    base = min(_BACKOFF_BASE_SECONDS * (2 ** attempt), _BACKOFF_MAX_SECONDS)
    return base * random.uniform(*_JITTER_RANGE)


def _sleep_before_retry(
    delay: float, deadline: float | None = None
) -> None:
    """Sleep for *delay* seconds, clamped to the remaining deadline.

    The sleep is sliced into ``_CANCEL_PROBE_POLL_SECONDS`` intervals so a
    cancellation bound via :func:`provider_cancel_scope` interrupts the
    backoff promptly instead of serving the full delay. Without a bound
    probe the timing behaviour is identical to one uninterrupted sleep.
    """
    if delay <= 0:
        return
    _check_provider_cancel()
    if deadline is not None:
        _check_deadline(deadline)
        delay = min(delay, max(0.0, deadline - time.monotonic()))
    if delay <= 0:
        return
    wake_at = time.monotonic() + delay
    while True:
        remaining = wake_at - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_CANCEL_PROBE_POLL_SECONDS, remaining))
        _check_provider_cancel()


def _sdk_error_status_code(exc: BaseException) -> int | None:
    """Extract an HTTP status code from an OpenAI-SDK style exception."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _sdk_error_code(exc: BaseException) -> str:
    """Return a compact retry reason for OpenAI-SDK style exceptions."""
    status = _sdk_error_status_code(exc)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


def sdk_retry_after(exc: BaseException) -> str | None:
    """Extract the ``Retry-After`` header from an OpenAI-SDK exception.

    The SDK's ``APIStatusError``/``RateLimitError`` carry the HTTP
    ``response``; its headers hold the server's back-off hint. Returned
    verbatim so :func:`_retry_delay_seconds` can honour it (and fall
    back to exponential backoff on absent/non-numeric values). Public
    because the knowledge embedding batcher reads the same hint from a
    wrapped provider failure.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get("retry-after")
    except Exception:  # noqa: BLE001 — a hostile headers object is not fatal
        return None


def _is_sdk_rate_limit(exc: BaseException) -> bool:
    """Return whether an OpenAI-SDK exception is a 429 / rate limit."""
    return (
        _sdk_error_status_code(exc) == 429
        or type(exc).__name__ == "RateLimitError"
    )


def _is_retryable_sdk_error(exc: BaseException) -> bool:
    """Return whether an OpenAI-SDK style exception is transient."""
    status = _sdk_error_status_code(exc)
    if status is not None:
        return status in _RETRYABLE_SDK_STATUS_CODES
    return type(exc).__name__ in _RETRYABLE_SDK_ERROR_TYPES


def _call_openai_chat_completion_with_retries(
    *,
    provider_label: str,
    model: str,
    operation: str,
    deadline: float | None,
    outer_deadline: float | None = None,
    timeout_label: str = "Provider call",
    configured_timeout_seconds: float | None = None,
    create: Callable[[], Any],
    append_retry_notice: Callable[[dict[str, Any]], None],
    error_code_for: Callable[[BaseException], str] | None = None,
    request_id_for: Callable[[BaseException], str] | None = None,
) -> Any:
    """Call an OpenAI-SDK style operation with visible transient retries.

    Transient and rate-limit failures share one total attempt counter. Their
    separately patchable retry ceilings remain useful for replay tests, but a
    mixed sequence can never exceed the larger ceiling (three attempts in
    production). Every sleep and request is clamped to ``deadline``.
    """
    effective_timeout_seconds = (
        max(0.0, deadline - time.monotonic())
        if deadline is not None
        else None
    )
    attempt = 1
    budget = current_provider_attempt_budget()
    while True:
        _check_provider_cancel(label=timeout_label)
        if deadline is not None:
            _check_provider_operation_deadline(
                deadline,
                outer_deadline,
                label=timeout_label,
            )
        if budget is not None:
            budget.claim()
        try:
            return create()
        except Exception as exc:
            if budget is not None and not budget.remaining:
                raise
            status_code = _sdk_error_status_code(exc)
            if _is_sdk_rate_limit(exc):
                max_attempts = _SDK_RATE_LIMIT_MAX_RETRIES + 1
                if attempt >= max_attempts:
                    raise
                delay = _retry_delay_seconds(
                    attempt - 1, sdk_retry_after(exc)
                )
            else:
                max_attempts = _SDK_MAX_RETRIES + 1
                if (
                    not _is_retryable_sdk_error(exc)
                    or attempt >= max_attempts
                ):
                    raise
                delay = _retry_delay_seconds(attempt - 1)
            error_code = (
                error_code_for(exc)
                if error_code_for is not None
                else _sdk_error_code(exc)
            )
            request_id = (
                request_id_for(exc)
                if request_id_for is not None
                else ""
            )
            reported_attempt = budget.used if budget is not None else attempt
            reported_max_attempts = (
                min(budget.max_attempts, budget.used + max_attempts - attempt)
                if budget is not None else max_attempts
            )
            append_retry_notice({
                "provider": provider_label,
                "model": model,
                "operation": operation,
                "error_code": error_code,
                "status_code": status_code,
                "request_id": request_id,
                "attempt": reported_attempt,
                "max_attempts": reported_max_attempts,
                "delay_seconds": round(delay, 3),
                **(
                    {
                        "configured_timeout_seconds": round(
                            float(configured_timeout_seconds), 3
                        )
                    }
                    if configured_timeout_seconds is not None
                    else {}
                ),
                **(
                    {
                        "effective_timeout_seconds": round(
                            effective_timeout_seconds, 3
                        )
                    }
                    if effective_timeout_seconds is not None
                    else {}
                ),
            })
            if status_code is not None:
                log.warning(
                    "%s transient error (%s, code=%s, status=%s, request-id=%s, attempt=%d/%d). Retrying in %.2fs.",
                    provider_label,
                    model,
                    error_code,
                    status_code,
                    request_id or "-",
                    reported_attempt,
                    reported_max_attempts,
                    delay,
                )
            else:
                log.warning(
                    "%s transport error "
                    "(model=%s, code=%s, error_type=%s, attempt=%d/%d). "
                    "Retrying in %.2fs.",
                    provider_label,
                    model,
                    error_code,
                    type(exc).__name__,
                    reported_attempt,
                    reported_max_attempts,
                    delay,
                )
            try:
                _sleep_before_retry(delay, deadline)
            except AgentTimeout:
                _check_provider_operation_deadline(
                    deadline,
                    outer_deadline,
                    label=timeout_label,
                )
                raise
            attempt += 1


# =========================================================
# Response dataclasses
# =========================================================


INCOMPLETE_FINISH_REASONS: frozenset[str] = frozenset(
    {
        "content_filter",
        "length",
        "max_tokens",
        "max_tokens_reached",
        "model_length",
        "token_limit",
    }
)
"""Provider stop signals that mean the backend stopped BEFORE the completion
was finished: the output budget ran out, or the backend's content filter cut
the response.  A caller that needs a whole completion (a structured batch, a
grounded answer) treats these as an incomplete delivery, never as model output
to parse — the visible text, often empty, is not what the model was asked
for.  One definition for every consumer (contextualisation, claim extraction,
the knowledge answer)."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Structured wrapper around an LLM completion result.

    Attributes:
        content: Visible assistant text.
        prompt_tokens: Total input tokens, including reported cache reads/writes.
        completion_tokens: Output tokens billed by the backend. For Anthropic
            with extended thinking enabled this includes both visible and
            hidden (thinking) tokens. A reasoning breakdown is optional and
            available only when the backend reports it.
        cache_read_tokens: Input tokens read from cache; ``None`` if unavailable.
        cache_creation_tokens: Input tokens written to cache; ``None`` if unavailable.
        reasoning_tokens: Reasoning subset of output; ``None`` if unavailable.
        model: Effective model identifier reported by the backend.
        finish_reason: Provider-specific stop signal (``stop``, ``end_turn``,
            ``length``, ...).
        raw: Original payload for debugging.
        request_max_tokens: The max-output budget the provider actually sent
            to the backend, **after** any clamping (e.g. Anthropic's
            thinking auto-raise to 16384). Use this rather than the caller's
            requested ``max_output_tokens`` when computing token-utilization
            ratios — otherwise thinking models appear to run "over budget"
            even though they finished freely.  ``0`` means the caller did
            not pass a budget and the provider default (or backend default)
            was used.
    """

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    raw: dict[str, Any] | None = None
    request_max_tokens: int = 0
    duration_ms: float = 0.0
    """Wall-clock call duration, stamped by the tracing wrapper; ``0.0`` when the provider is used unwrapped."""
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class StructuredLLMResponse:
    """Structured-output completion result with parsed JSON payload.

    Attributes:
        parsed: Parsed JSON object produced by the provider's native
            structured-output mechanism. The top-level value is always
            a dictionary; adapters raise :class:`AgentStructuredOutputError`
            for non-object responses.
        content: Visible assistant text returned by the backend. Providers
            usually return the JSON serialization here. Kept for forensic
            diagnostics and token accounting parity with ``LLMResponse``.
        prompt_tokens: Input tokens billed by the backend.
        completion_tokens: Output tokens billed by the backend.
        model: Effective model identifier reported by the backend.
        finish_reason: Provider-specific stop signal.
        raw: Original provider payload for diagnostics.
        request_max_tokens: Output-token budget sent to the backend.
        schema_name: Stable schema name sent with the structured request.
    """

    parsed: dict[str, Any]
    content: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    raw: dict[str, Any] | None = None
    request_max_tokens: int = 0
    schema_name: str = ""
    duration_ms: float = 0.0
    """Wall-clock call duration, stamped by the tracing wrapper; ``0.0`` when the provider is used unwrapped."""
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """One tool invocation the model requested in a chat turn.

    Attributes:
        id: Provider tool-call id, passed back verbatim on the follow-up
            ``tool`` message so the backend can correlate result to call.
            Providers synthesize a stable id when the backend omits one —
            the kernel derives deterministic interrupt ids from it, so it
            must never be empty.
        name: Registered tool name the model wants to invoke.
        arguments: Parsed JSON arguments object. Providers parse the
            backend's argument STRING and surface a parse failure loudly
            (empty dict + ``finish_reason`` untouched is never silent —
            the adapter raises instead).
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One assistant turn of a tool-calling chat conversation.

    The native function-calling counterpart of :class:`LLMResponse`
    carries the assistant text AND any requested tool calls, so an
    agent loop can decide whether to execute tools or
    finish. Exactly one of ``text``/``tool_calls`` may be empty; both
    empty means the backend produced nothing visible (callers treat that
    as a loud failure, not a silent stop).

    Attributes:
        text: Visible assistant text ('' when the turn is tool-calls
            only).
        tool_calls: Tool invocations requested by the model, in call
            order (empty for a final answer).
        finish_reason: Provider stop signal (``stop``, ``tool_calls``,
            ``length``, ...).
        model: Effective model identifier reported by the backend.
        prompt_tokens: Input tokens billed by the backend.
        completion_tokens: Output tokens billed by the backend.
        raw: Original payload for diagnostics.
    """

    text: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    finish_reason: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] | None = None
    duration_ms: float = 0.0
    """Wall-clock call duration, stamped by the tracing wrapper; ``0.0`` when the provider is used unwrapped."""
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None


def chat_turn_from_openai_response(
    response: Any,
    *,
    model: str,
) -> "ChatTurn":
    """Map an OpenAI-SDK chat-completions response to a :class:`ChatTurn`.

    Shared by every OpenAI-compatible provider (LiteLLM, Azure) so the
    tool-call parsing rules are defined once and cannot drift:

    - tool-call ids pass through verbatim; a missing id is synthesized
      ONCE here (``call_<hex>``) — the kernel freezes it into the
      checkpointed message, so later re-executions see the same id;
    - argument strings are parsed strictly; invalid JSON or a non-object
      value raises :class:`AgentStructuredOutputError` loudly instead of
      degrading to an empty argument dict.

    Args:
        response: The SDK response object (``choices[0].message``).
        model: Effective model identifier for diagnostics.

    Returns:
        ChatTurn with text, tool calls, finish reason, and token counts.

    Raises:
        AgentStructuredOutputError: On unparsable tool-call arguments.
    """
    choice = response.choices[0] if getattr(response, "choices", None) else None
    message = getattr(choice, "message", None)
    tool_calls: list[ToolCallRequest] = []
    raw_tool_calls: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    original_error: Exception | None = None
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        name = str(getattr(function, "name", "") or "")
        raw_arguments = str(getattr(function, "arguments", "") or "")
        call_id = str(getattr(call, "id", "") or "")
        if not call_id:
            call_id = f"call_{uuid.uuid4().hex[:12]}"
        raw_tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": raw_arguments},
        })
        if raw_arguments.strip():
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                original_error = original_error or exc
                validation_errors.append("Arguments are not valid JSON.")
                continue
            if not isinstance(arguments, dict):
                validation_errors.append(
                    f"Arguments are {type(arguments).__name__}; expected a JSON object."
                )
                continue
        else:
            arguments = {}
        tool_calls.append(
            ToolCallRequest(id=call_id, name=name, arguments=arguments)
        )
    usage = getattr(response, "usage", None)
    turn = ChatTurn(
        text=str(getattr(message, "content", "") or ""),
        tool_calls=tuple(tool_calls),
        finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        model=model,
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        **openai_usage_details(usage),
        raw=response_raw_payload(response),
    )
    if validation_errors:
        raise AgentToolArgumentsError(
            model,
            turn=turn,
            raw_tool_calls=tuple(raw_tool_calls),
            validation_errors=tuple(validation_errors),
            original=original_error,
        ) from original_error
    return turn


def parse_structured_response_content(
    content: str,
    *,
    model: str,
    schema_name: str,
) -> dict[str, Any]:
    """Parse a provider structured-output text response into a dict.

    Args:
        content: Visible model response, expected to be JSON text because
            the provider used a native JSON-schema response format.
        model: Effective model or deployment identifier, used only for
            diagnostics.
        schema_name: Stable JSON schema name sent to the provider.

    Returns:
        Parsed top-level JSON object.

    Raises:
        AgentStructuredOutputError: If ``content`` is empty, invalid JSON,
            or a valid JSON value other than an object.
    """
    stripped = str(content or "").strip()
    if not stripped:
        raise AgentStructuredOutputError(
            model,
            schema_name,
            "provider returned empty structured-output content",
        )
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AgentStructuredOutputError(
            model,
            schema_name,
            "provider returned invalid structured-output JSON",
            original=exc,
        ) from exc
    if not isinstance(parsed, dict):
        raise AgentStructuredOutputError(
            model,
            schema_name,
            f"provider returned {type(parsed).__name__}, expected object",
        )
    return parsed


_MODEL_CAPACITY_ERROR_FRAGMENTS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "input is too long",
    "input too long",
    "too many tokens",
    "maximum number of tokens",
    "max_tokens",
    "max_completion_tokens",
    "max output tokens",
    "output token",
    "token budget",
)


def is_model_capacity_error(exc: object) -> bool:
    """Return True when an upstream error is about model token capacity."""
    return model_capacity_reason(exc) is not None


def model_capacity_reason(exc: object) -> ModelCapacityReason | None:
    """Classify capacity without mistaking output configuration for context.

    Structured provider codes and parameters outrank message wording. Ambiguous
    token-related text remains UNKNOWN: it must not activate context recovery.
    Authentication, rate limits and server failures retain their own policy.
    """
    if isinstance(exc, AgentModelCapacityError):
        return exc.reason
    status = _sdk_error_status_code(exc)
    if status is not None and status not in {400, 413, 422}:
        return None
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    body = body if isinstance(body, dict) else {}
    code = str(body.get("code") or getattr(exc, "error_code", "") or "").lower()
    parameter = str(body.get("param") or "").lower()
    if code in {"context_length_exceeded", "context_window_exceeded", "input_too_long"}:
        return ModelCapacityReason.CONTEXT_WINDOW
    if parameter in {"max_tokens", "max_completion_tokens", "max_output_tokens"}:
        return ModelCapacityReason.OUTPUT_LIMIT
    parts = [str(exc)]
    for attr in ("error_code", "error_type", "status_code"):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            parts.append(str(value))
    text = " ".join(parts).lower()
    # Message-only discrimination is allowed only on a proven request error.
    if status in {400, 413, 422}:
        if any(fragment in text for fragment in (
            "maximum context length", "maximum context window",
            "context_length_exceeded", "input is too long", "prompt is too long",
        )):
            return ModelCapacityReason.CONTEXT_WINDOW
        if any(fragment in text for fragment in (
            "max_tokens", "max_completion_tokens", "max_output_tokens",
            "max output tokens", "output token",
        )):
            return ModelCapacityReason.OUTPUT_LIMIT
    if any(fragment in text for fragment in _MODEL_CAPACITY_ERROR_FRAGMENTS):
        return ModelCapacityReason.UNKNOWN
    return None


@dataclass(frozen=True, slots=True)
class _NormalizedCompletion:
    """Internal normalized representation of a chat completion."""

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    raw: dict[str, Any] | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None


def response_raw_payload(response: Any) -> dict[str, Any] | None:
    """Keep the provider SDK payload without letting diagnostics break a call."""
    if isinstance(response, dict):
        return response
    dump = getattr(response, "model_dump", None)
    if not callable(dump):
        return None
    try:
        raw = dump()
    except Exception:  # noqa: BLE001 — the provider result remains usable
        log.warning("Provider raw response could not be serialized; raw capture unavailable.")
        return None
    return raw if isinstance(raw, dict) else None


def optional_usage_count(usage: Any, *path: str) -> int | None:
    """Read a provider-reported count; absence is not a measured zero."""
    value = usage
    for key in path:
        value = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    log.warning("Provider usage detail %s is not a nonnegative integer; omitted.", ".".join(path))
    return None


def openai_usage_details(usage: Any) -> dict[str, int | None]:
    """Optional subsets of OpenAI-compatible inclusive input/output totals."""
    return {
        "cache_read_tokens": optional_usage_count(usage, "prompt_tokens_details", "cached_tokens"),
        "cache_creation_tokens": optional_usage_count(usage, "prompt_tokens_details", "cache_write_tokens"),
        "reasoning_tokens": optional_usage_count(usage, "completion_tokens_details", "reasoning_tokens"),
    }


def cache_usage_details(
    usage: Any, *, read_key: str, creation_key: str,
) -> dict[str, int | None]:
    """Read the cache buckets of a native provider usage block."""
    return {
        "cache_read_tokens": optional_usage_count(usage, read_key),
        "cache_creation_tokens": optional_usage_count(usage, creation_key),
    }


def responses_usage_details(usage: Any) -> dict[str, int | None]:
    """Optional token subsets in the Responses API's usage schema."""
    return {
        "cache_read_tokens": optional_usage_count(usage, "input_tokens_details", "cached_tokens"),
        "cache_creation_tokens": optional_usage_count(usage, "input_tokens_details", "cache_write_tokens"),
        "reasoning_tokens": optional_usage_count(usage, "output_tokens_details", "reasoning_tokens"),
    }


def _extract_content_value(content: Any) -> str:
    """Extract text from a content field that may be a string, list, or object."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _extract_choice_content(choice: Any) -> str:
    """Extract text content from a chat completion choice (message or delta)."""
    for field_name in ("message", "delta"):
        if isinstance(choice, dict):
            payload = choice.get(field_name)
        else:
            payload = getattr(choice, field_name, None)
        if payload is None:
            continue
        if isinstance(payload, dict):
            content = payload.get("content")
        else:
            content = getattr(payload, "content", None)
        text = _extract_content_value(content)
        if text:
            return text
    return ""


def _extract_choice_finish_reason(choice: Any) -> str:
    """Extract the finish reason from a chat completion choice."""
    if isinstance(choice, dict):
        finish_reason = choice.get("finish_reason")
    else:
        finish_reason = getattr(choice, "finish_reason", None)
    if isinstance(finish_reason, str):
        return finish_reason
    return ""


def _extract_usage_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from a dict payload."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (0, 0)
    return (
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )


def extract_usage_tokens(
    payload: dict[str, Any] | None,
    *,
    input_keys: tuple[str, ...] = ("prompt_tokens", "input_tokens", "inputTokens"),
    output_keys: tuple[str, ...] = ("completion_tokens", "output_tokens", "outputTokens"),
    usage_key: str = "usage",
) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a payload ``usage`` block.

    Supports OpenAI-style (``prompt_tokens``/``completion_tokens``),
    Anthropic-style (``input_tokens``/``output_tokens``), and Bedrock-style
    (``inputTokens``/``outputTokens``) key variants. Returns the first key
    in each tuple that yields a positive integer.
    """
    if not isinstance(payload, dict):
        return (0, 0)
    usage = payload.get(usage_key)
    if not isinstance(usage, dict):
        return (0, 0)

    def _first_int(keys: tuple[str, ...]) -> int:
        for key in keys:
            val = usage.get(key)
            if val:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
        return 0

    return (_first_int(input_keys), _first_int(output_keys))


def _extract_finish_reason_from_payload(payload: dict[str, Any]) -> str:
    """Extract the first non-empty finish reason from a completion payload."""
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        finish_reason = _extract_choice_finish_reason(choice)
        if finish_reason:
            return finish_reason
    return ""


def _extract_usage_from_response(response: Any) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from an SDK response object."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return (0, 0)
    return (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _extract_finish_reason_from_response(response: Any) -> str:
    """Extract the first non-empty finish reason from an SDK response object."""
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    for choice in choices:
        finish_reason = _extract_choice_finish_reason(choice)
        if finish_reason:
            return finish_reason
    return ""


def _normalize_completion_payload(payload: dict[str, Any]) -> _NormalizedCompletion:
    """Normalize a JSON chat completion payload into a ``_NormalizedCompletion``."""
    content_parts: list[str] = []
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            text = _extract_choice_content(choice)
            if text:
                content_parts.append(text)
    prompt_tokens, completion_tokens = _extract_usage_from_payload(payload)
    finish_reason = _extract_finish_reason_from_payload(payload)
    return _NormalizedCompletion(
        content="".join(content_parts),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        raw=payload,
        **openai_usage_details(payload.get("usage")),
    )


def _normalize_text_completion(response: str) -> _NormalizedCompletion:
    """Normalize a raw text response (plain JSON or SSE stream) into a ``_NormalizedCompletion``."""
    response_text = response.strip()
    if not response_text:
        return _NormalizedCompletion(content="", raw={"response_text": response})

    if not response_text.startswith("data:"):
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            return _NormalizedCompletion(content=response_text, raw={"response_text": response})
        if isinstance(payload, dict):
            return _normalize_completion_payload(payload)
        return _NormalizedCompletion(content=response_text, raw={"response_text": response})

    content_parts: list[str] = []
    citations: list[str] = []
    related_questions: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason = ""
    model = ""
    raw_usage: dict[str, Any] = {}

    for line in response.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload_text = line[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        normalized = _normalize_completion_payload(payload)
        if isinstance(payload.get("usage"), dict):
            raw_usage = payload["usage"]
        if normalized.content:
            content_parts.append(normalized.content)
        if normalized.prompt_tokens or normalized.completion_tokens:
            prompt_tokens = normalized.prompt_tokens
            completion_tokens = normalized.completion_tokens
        if normalized.finish_reason:
            finish_reason = normalized.finish_reason

        payload_citations = payload.get("citations")
        if isinstance(payload_citations, list):
            for citation in payload_citations:
                text = str(citation)
                if text and text not in citations:
                    citations.append(text)

        payload_related = payload.get("related_questions")
        if isinstance(payload_related, list):
            for question in payload_related:
                text = str(question)
                if text and text not in related_questions:
                    related_questions.append(text)

        payload_model = payload.get("model")
        if isinstance(payload_model, str) and payload_model:
            model = payload_model

    raw: dict[str, Any] = {"response_text": response}
    if citations:
        raw["citations"] = citations
    if related_questions:
        raw["related_questions"] = related_questions
    if model:
        raw["model"] = model
    if prompt_tokens or completion_tokens:
        raw["usage"] = {
            **raw_usage,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    if finish_reason:
        raw["finish_reason"] = finish_reason

    return _NormalizedCompletion(
        content="".join(content_parts),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        raw=raw,
        **openai_usage_details(raw_usage),
    )


def _normalize_completion_response(response: Any) -> _NormalizedCompletion:
    """Normalize any completion response (str, SDK object, or dict) into a ``_NormalizedCompletion``."""
    if isinstance(response, str):
        return _normalize_text_completion(response)
    if isinstance(response, dict):
        return _normalize_completion_payload(response)

    content_parts: list[str] = []
    choices = getattr(response, "choices", None)
    if choices:
        for choice in choices:
            text = _extract_choice_content(choice)
            if text:
                content_parts.append(text)

    prompt_tokens, completion_tokens = _extract_usage_from_response(response)
    finish_reason = _extract_finish_reason_from_response(response)
    raw = response_raw_payload(response) or {}
    if raw:
        if not content_parts:
            normalized = _normalize_completion_payload(raw)
            if normalized.content:
                content_parts.append(normalized.content)
            if normalized.finish_reason and not finish_reason:
                finish_reason = normalized.finish_reason
        if not prompt_tokens and not completion_tokens:
            prompt_tokens, completion_tokens = _extract_usage_from_payload(raw)
        if not finish_reason:
            finish_reason = _extract_finish_reason_from_payload(raw)

    return _NormalizedCompletion(
        content="".join(content_parts),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        raw=raw,
        **openai_usage_details(getattr(response, "usage", None) or raw.get("usage")),
    )


# =========================================================
# Shared search helpers
# =========================================================


def _apply_domain_filters(query: str, domain_filter: list[str] | None) -> str:
    """Inject ``site:`` / ``-site:`` operators into the query string."""
    if not domain_filter:
        return query

    suffix_parts: list[str] = []
    for raw_domain in domain_filter[:10]:
        domain = str(raw_domain or "").strip()
        if not domain:
            continue
        is_exclusion = domain.startswith("-")
        if is_exclusion:
            domain = domain[1:].strip()
        if not domain:
            continue
        token = f"site:{domain}"
        if is_exclusion:
            token = f"-site:{domain}"
        suffix_parts.append(token)

    if not suffix_parts:
        return query
    return f"{query} {' '.join(suffix_parts)}"


def _build_recency_language_hints(
    recency_filter: str | None,
    language_filter: list[str] | None,
) -> str | None:
    """Build best-effort recency/language hints for search agents."""
    parts: list[str] = []

    recency = (recency_filter or "").strip().lower()
    if recency == "hour":
        parts.append("Focus exclusively on results from the past hour.")
    elif recency == "day":
        parts.append("Focus on results from the past 24 hours.")
    elif recency == "week":
        parts.append("Focus on results from the past week.")
    elif recency == "month":
        parts.append("Focus on results from the past month.")
    elif recency == "year":
        parts.append("Focus on results from the past year.")

    if language_filter:
        lang = language_filter[0]
        parts.append(
            f"Answer in {lang} and prefer sources in that language."
        )

    return " ".join(parts) if parts else None


# =========================================================
# Abstract base classes
# =========================================================


class LLMProvider(ABC):
    """Define the contract for language-model completions.

    Use this abstract base class when implementing a custom language
    model backend for Inqtrix. Concrete providers are expected to offer
    a reasoning call path for graph nodes such as classify, plan,
    evaluate, and answer. Claim extraction is implemented as a strategy
    on top of the same completion methods instead of as a separate
    provider capability.
    """

    complete_uses_metadata: bool = False
    """Whether complete delegates to the same native metadata call.

    Tracing may retain that response before projecting it to text. Custom
    providers opt in only when both methods execute the identical call.
    """

    @property
    def selectable_models(self) -> list[str]:
        """Return the model ids the UI may offer for direct selection.

        Empty (the default) keeps the chat/editor model picker on the
        high/mid/fast tier choices. A provider that overrides this exposes a
        curated list of concrete model ids the operator wants selectable; the
        catalogue resolves each to a model card, and an unknown id degrades
        visibly rather than silently.
        """
        return []

    @property
    def context_window_tokens(self) -> int | None:
        """Return the configured model context window, when known.

        Providers may leave this as ``None`` when the backend or gateway
        does not expose a reliable value. The runtime treats unknown
        capacity as a visible warning, not as proof that the model is
        large enough.
        """
        return None

    @abstractmethod
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
        """Generate a completion and return only the visible text.

        Call this method for standard reasoning work when the caller only
        needs the model's text output and does not care about explicit
        token accounting. Implementations typically route ``model=None``
        to the provider's default reasoning model and may internally call
        :meth:`complete_with_metadata` before discarding token metadata.

        Args:
            prompt: User-facing input text to send to the model.
            system: Optional system instruction. Omit this when the
                provider or backend does not need a separate system role.
                The default is ``None``.
            model: Optional per-call model override. When omitted, the
                provider should use its default reasoning model or the
                role-specific fallback selected by the runtime.
            max_output_tokens: Optional output-token budget for the visible
                answer. Providers may ignore it when unsupported.
            timeout: Per-call timeout budget in seconds. Providers may
                shorten this further when ``deadline`` leaves less time.
                The default is ``600.0`` seconds.
            state: Optional mutable agent state used for token tracking
                in non-parallel code paths. Omit this in helper threads or
                whenever shared state would be unsafe to mutate.
            deadline: Optional absolute monotonic deadline for the whole
                agent run. When present, providers should clamp the call
                timeout to the remaining budget and raise ``AgentTimeout``
                once the budget is exhausted.
            reasoning_effort: Optional per-call reasoning-effort override.
                ``None`` or ``""`` inherits the provider's constructor
                default; ``"none"`` forces reasoning off for this call; a
                graded level (``"minimal"`` .. ``"xhigh"``) requests reasoning
                where the model supports it.

        Returns:
            str: The visible assistant text. Providers should return an
            empty string when the backend responded without user-visible
            content rather than fabricating placeholder output.

        Raises:
            AgentTimeout: If the provider detects that the absolute agent
                deadline has been reached before or during the request.
            AgentRateLimited: If the backend explicitly rate-limits the
                request and the provider chooses to surface that as a
                fatal graph-level error.
            Exception: Backend-specific errors may propagate when the
                provider cannot degrade safely.
        """

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
        """Generate a completion together with token metadata.

        Override this method when the backend can report token counts,
        model identity, or other response metadata that the runtime wants
        to preserve for logging, diagnostics, and cost tracking. Custom
        providers may implement only :meth:`complete`; in that case the
        default implementation wraps the returned text in ``LLMResponse``
        without token counts.

        Args:
            prompt: User-facing input text to send to the model.
            system: Optional system instruction. The default is ``None``.
            model: Optional per-call model override. The default is
                ``None``, which signals the provider to use its default
                reasoning model.
            max_output_tokens: Optional output-token budget for the visible
                answer. Providers may ignore it when unsupported.
            timeout: Per-call timeout budget in seconds before deadline
                clamping. The default is ``600.0`` seconds.
            state: Optional mutable agent state for token aggregation in
                non-parallel code paths. Omit it when no shared token
                accounting is needed.
            deadline: Optional absolute monotonic deadline for the whole
                run. Providers should treat it as the hard ceiling for all
                retries and request backoff.
            reasoning_effort: Optional per-call reasoning-effort override.
                ``None`` or ``""`` inherits the provider's constructor
                default; ``"none"`` forces reasoning off; a graded level
                (``"minimal"`` .. ``"xhigh"``) requests reasoning where the
                model supports it.

        Returns:
            LLMResponse: Structured response containing visible content,
            token counts when available, and the effective model label.

        Raises:
            AgentTimeout: If the absolute run deadline is exceeded.
            AgentRateLimited: If the backend returns a fatal rate-limit
                condition that should abort the run.
            Exception: Backend-specific provider errors may propagate.
        """
        return LLMResponse(
            content=self.complete(
                prompt,
                system=system,
                model=model,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
                state=state,
                deadline=deadline,
                reasoning_effort=reasoning_effort,
            ),
            model=model or "",
        )

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        """Return whether native JSON-schema output is available.

        Providers should override this when their backend can constrain
        responses to a caller-supplied JSON schema. The default is
        intentionally ``False`` so existing custom providers keep their
        current prompt-based JSON behavior until they opt in.

        Args:
            model: Optional per-call model or deployment identifier. Use
                this when support depends on the specific model role.

        Returns:
            ``True`` only when :meth:`complete_structured` is expected to
            work for the requested model.
        """
        return False

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
        """Generate a JSON-schema-constrained completion.

        This optional method is used only by callers that need stronger
        machine-readable guarantees than prompt-only JSON can provide.
        The base implementation is deliberately non-functional; callers
        must first check :meth:`supports_structured_output` and preserve a
        visible fallback or failure path when a provider does not opt in.

        Args:
            prompt: User-facing input text to send to the model.
            schema: JSON Schema object describing the required response.
            schema_name: Stable provider-facing schema name.
            schema_description: Optional provider-facing schema purpose.
            system: Optional system instruction.
            model: Optional per-call model override.
            max_output_tokens: Optional output-token budget.
            timeout: Per-call timeout budget in seconds.
            state: Optional mutable token-accounting state.
            deadline: Optional absolute monotonic deadline.
            reasoning_effort: Optional per-call reasoning-effort override.
                ``None`` or ``""`` inherits the provider default; ``"none"``
                forces reasoning off; a graded level requests reasoning where
                the model supports it.

        Returns:
            StructuredLLMResponse with parsed top-level JSON object.

        Raises:
            NotImplementedError: Always raised by the base class.
            AgentStructuredOutputError: Raised by concrete providers when
                a structured response cannot be parsed into an object.
        """
        raise NotImplementedError("Provider does not implement structured output.")

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        """Return whether native function calling is available.

        The cognitive kernel registers only against providers that opt
        in here — the capabilities endpoint then reports the
        kernel as unavailable instead of failing mid-run. The default is
        intentionally ``False`` so existing custom providers stay on the
        prompt-only surface until they implement :meth:`chat`.

        Args:
            model: Optional per-call model or deployment identifier for
                backends where support depends on the model role.

        Returns:
            ``True`` only when :meth:`chat` is expected to work for the
            requested model.
        """
        return False

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
    ) -> "ChatTurn":
        """Run ONE native tool-calling chat turn.

        This is the message-array counterpart of :meth:`complete`: the
        caller owns the conversation (system/user/assistant/tool
        messages in the OpenAI chat shape, assistant messages carrying
        ``tool_calls``, tool results as ``role='tool'`` messages with
        ``tool_call_id``) and the provider returns the next assistant
        turn — text, tool-call requests, or both. Callers must check
        :meth:`supports_tool_calls` first; the base implementation
        raises loudly so a missing opt-in can never look like an empty
        answer.

        Args:
            messages: Conversation so far in the OpenAI chat-completions
                message shape. The provider sends it verbatim (it never
                rewrites history — the agent loop owns trimming).
            tools: Tool definitions in the OpenAI function-tool shape
                (``{"type": "function", "function": {"name", "description",
                "parameters"}}``). ``None``/empty runs a plain chat turn.
            model: Optional per-call model override; ``None`` uses the
                provider's default reasoning model.
            max_output_tokens: Optional output-token budget for the
                visible answer; providers may ignore it when unsupported.
            timeout: Per-call timeout budget in seconds before deadline
                clamping.
            deadline: Optional absolute monotonic deadline for the whole
                run — the hard ceiling for retries and backoff.
            reasoning_effort: Optional per-call reasoning-effort override
                with the same vocabulary as :meth:`complete`.
            tool_choice: Optional obligation for the turn's tool use in the
                OpenAI-compatible vocabulary (``"required"``: the turn must
                call a tool). ``None`` sends nothing and leaves the choice
                to the model. Sent verbatim: a backend or model that does
                not support it rejects the request visibly, never silently.

        Returns:
            ChatTurn: The next assistant turn with tool-call requests,
            token counts, and the effective model label.

        Raises:
            NotImplementedError: Always raised by the base class — check
                :meth:`supports_tool_calls` before calling.
            AgentTimeout: If the absolute run deadline is exceeded.
            AgentRateLimited: On a fatal backend rate-limit condition.
        """
        raise NotImplementedError(
            "Provider does not implement native tool calling; check "
            "supports_tool_calls() before calling chat()."
        )

    @abstractmethod
    def is_available(self) -> bool:
        """Report whether the provider is configured to attempt requests.

        Use this method for readiness checks such as health endpoints or
        auto-create diagnostics. ``True`` means the provider has enough
        configuration to make a request; it does not guarantee that the
        remote backend is reachable or healthy at this exact moment.

        Returns:
            bool: ``True`` when the provider is configured and ready to
            attempt requests, otherwise ``False``.
        """


class SearchProvider(ABC):
    """Define the contract for structured web-search providers.

    Use this abstract base class when connecting a search backend such as
    Perplexity or Azure Foundry to the research graph. Concrete
    providers are responsible for translating Inqtrix search hints into
    backend-specific request parameters and normalizing the result into a
    typed :class:`~inqtrix.search_result.GroundedSearchResult`.
    """

    @abstractmethod
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
        """Execute a search request and normalize the backend response.

        Concrete implementations should map the generic Inqtrix search
        hints onto whatever the backend supports natively, best-effort, or
        not at all. The returned :class:`GroundedSearchResult` keeps the
        same typed shape across providers so later graph nodes can consume
        results without special cases.

        Args:
            query: User-facing search query text.
            search_context_size: Backend-independent hint for how much web
                context to request. Common values are ``"low"``,
                ``"medium"``, and ``"high"``. Providers may map this to
                result counts, search depth, or ignore it when unsupported.
            recency_filter: Optional freshness hint such as ``"day"``,
                ``"week"``, ``"month"``, or ``"year"``. Providers may map
                it natively or approximate it through prompt hints.
            language_filter: Optional language hints, usually ISO 639-1
                codes such as ``["de"]``. Most providers use only the
                first value when the backend accepts a single language.
            domain_filter: Optional allow/deny list of domains. Entries
                starting with ``"-"`` mean exclusion. Providers may pass
                this natively or inject ``site:`` operators into the query.
            search_mode: Optional backend-specific mode such as
                ``"academic"``. Omit it when the backend does not expose a
                matching concept.
            return_related: Whether the caller wants related questions or
                query suggestions when the backend supports them. The
                default is ``False``.
            deadline: Optional absolute monotonic deadline for the whole
                run. Providers should clamp their timeout budget and stop
                retrying once it is exceeded.

        Returns:
            GroundedSearchResult: Normalized result carrying ``answer``,
            ``sources`` (each a :class:`~inqtrix.search_result.GroundedSource`),
            ``related_questions``, ``prompt_tokens``, and
            ``completion_tokens``. Providers that do not receive token usage
            from the backend should leave the token counts at ``0``.

        Raises:
            AgentTimeout: If the global run deadline has already been
                exhausted.
            AgentRateLimited: If the backend signals a fatal rate-limit
                condition that should abort the run.
            Exception: Provider-specific fatal errors may propagate when no
                safe empty-result fallback exists.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Report whether the search provider is configured to run.

        Returns:
            bool: ``True`` when the provider has enough local
            configuration to attempt a request, otherwise ``False``.
        """

    @property
    def search_model(self) -> str:
        """Operator-friendly identifier of the search backend in use.

        Surfaces in the ``GET /health`` payload and in the
        ``GET /v1/stacks`` discovery response so an operator (or a UI
        rendering stack chips) can verify which engine each request
        actually routes through. Subclasses MUST override; the default
        returns ``"<ClassName>(unknown)"`` to make a missing override
        loud rather than silently leaking the global
        ``Settings.models.search_model`` default. The loud default keeps
        fallbacks visible: otherwise the health endpoint could report a
        search model name that belongs to a different provider stack.

        Returns:
            str: Stable identifier of the underlying search engine —
            e.g. ``"sonar-pro"`` for PerplexitySearch or
            ``"foundry-web:my-agent@v1"`` for Foundry web search.
            Empty strings are not returned; subclasses that genuinely
            have no model concept should return a stable backend
            identifier.
        """
        return f"{type(self).__name__}(unknown)"


class ConfiguredLLMProvider(LLMProvider):
    """Attach explicit model metadata to a custom LLM provider.

    Use this adapter when a custom provider implements the runtime call
    methods but does not expose a ``models`` property with the effective
    reasoning, classify, claim-extraction, and evaluate model names. Wrapping
    the provider here preserves the lightweight custom-provider contract
    while still giving the graph stable role-to-model metadata.

    Attributes:
        _provider (LLMProvider): Wrapped provider that performs the real
            backend calls.
        _models (ModelSettings): Effective role-to-model mapping exposed to
            the runtime.
    """

    def __init__(self, provider: LLMProvider, models: ModelSettings) -> None:
        """Initialize the adapter with a provider and explicit models.

        Args:
            provider: Wrapped provider that already implements the
                ``LLMProvider`` call methods.
            models: Effective model settings to expose through the
                adapter's ``models`` property.
        """
        self._provider = provider
        self._models = models

    @property
    def models(self) -> ModelSettings:
        """Return the explicit role-to-model mapping injected at construction.

        Returns:
            The :class:`~inqtrix.settings.ModelSettings` instance the
            adapter was built with. The graph reads ``reasoning_model``,
            ``effective_classify_model`` etc. from this object to
            select per-call model ids.
        """
        return self._models

    @property
    def complete_uses_metadata(self) -> bool:
        """Forward the wrapped provider's text/metadata equivalence declaration."""
        return getattr(self._provider, "complete_uses_metadata", False) is True

    @property
    def selectable_models(self) -> list[str]:
        """Delegate to the wrapped provider's selectable model ids, if any."""
        value = getattr(self._provider, "selectable_models", None)
        if isinstance(value, (list, tuple)):
            return [str(model_id) for model_id in value]
        return []

    @property
    def context_window_tokens(self) -> int | None:
        """Return the wrapped provider's configured context window."""
        value = getattr(self._provider, "context_window_tokens", None)
        if callable(value):
            value = value()
        if isinstance(value, int) and value > 0:
            return value
        raw = getattr(self._provider, "_context_window_tokens", None)
        return raw if isinstance(raw, int) and raw > 0 else None

    @property
    def llm_capabilities(self):
        """Forward the wrapped provider's structured LLM capabilities.

        Without this the claim-fan-out limiter probes the WRAPPER (which
        has no capability of its own) and silently loses a custom
        provider's declared ``max_llm_concurrency``, falling back to
        :data:`DEFAULT_LLM_FANOUT`. Returns ``None`` when the inner
        provider declares none, so :func:`get_llm_provider_capabilities`
        then tries the scalar attribute below.
        """
        return getattr(self._provider, "llm_capabilities", None)

    @property
    def max_llm_concurrency(self) -> int:
        """Forward the wrapped provider's LLM-concurrency cap (0 = defer)."""
        try:
            return int(getattr(self._provider, "max_llm_concurrency", 0) or 0)
        except (TypeError, ValueError):
            return 0

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
        """Forward to the wrapped provider, defaulting ``model`` from settings.

        Args:
            prompt: User-facing input text.
            system: Optional system instruction. Forwarded unchanged.
            model: Optional per-call model override. When ``None``,
                falls back to ``self.models.reasoning_model``; the
                effective value is forwarded only if it is non-empty
                (so a wrapped provider with its own default model
                can still receive ``None``).
            max_output_tokens: Optional output-token budget. Forwarded
                unchanged.
            timeout: Per-call timeout in seconds. Forwarded unchanged.
            state: Optional mutable agent state for token tracking.
                Forwarded unchanged.
            deadline: Optional absolute monotonic deadline. Forwarded
                unchanged.
            reasoning_effort: Optional per-call reasoning-effort override.
                Forwarded to the wrapped provider only when it is a non-empty
                string, so the common ``""``/``None`` case never reaches a
                custom provider whose ``complete`` predates this parameter.

        Returns:
            The visible assistant text from the wrapped provider.

        Raises:
            AgentTimeout: Surfaced from the wrapped provider.
            AgentRateLimited: Surfaced from the wrapped provider.
        """
        call_kwargs: dict[str, object] = {
            "system": system,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
            "state": state,
            "deadline": deadline,
        }
        effective_model = model or self._models.reasoning_model
        if effective_model:
            call_kwargs["model"] = effective_model
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
        return self._provider.complete(prompt, **call_kwargs)

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
        """Forward to the wrapped provider, defaulting ``model`` from settings.

        Behaves identically to :meth:`complete` except it returns the
        full :class:`LLMResponse` (text + token counts + effective
        model label).

        Args:
            prompt: See :meth:`complete`.
            system: See :meth:`complete`.
            model: See :meth:`complete`.
            max_output_tokens: See :meth:`complete`.
            timeout: See :meth:`complete`.
            state: See :meth:`complete`.
            deadline: See :meth:`complete`.
            reasoning_effort: See :meth:`complete`.

        Returns:
            :class:`LLMResponse` from the wrapped provider.

        Raises:
            AgentTimeout: Surfaced from the wrapped provider.
            AgentRateLimited: Surfaced from the wrapped provider.
        """
        call_kwargs: dict[str, object] = {
            "system": system,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
            "state": state,
            "deadline": deadline,
        }
        effective_model = model or self._models.reasoning_model
        if effective_model:
            call_kwargs["model"] = effective_model
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
        return self._provider.complete_with_metadata(prompt, **call_kwargs)

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        """Forward structured-output capability checks to the wrapped provider.

        Args:
            model: Optional per-call model override. When omitted, the
                adapter checks the wrapped provider against the explicit
                reasoning model from ``self.models``.

        Returns:
            ``True`` when the wrapped provider exposes
            ``supports_structured_output`` and reports support for the
            effective model; otherwise ``False``.
        """
        return reported_capability(
            self._provider,
            "supports_structured_output",
            model=model or self._models.reasoning_model,
        )

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        """Forward tool-calling capability checks to the wrapped provider.

        Same capability-swallowing hazard as
        :meth:`supports_structured_output`: without this forward a
        wrapped tool-capable provider inherits the base ``False`` and
        the kernel gate reads a capable stack as tool-incapable.
        """
        return reported_capability(
            self._provider,
            "supports_tool_calls",
            model=model or self._models.reasoning_model,
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
    ) -> "ChatTurn":
        """Forward a native tool-calling chat turn to the wrapped provider.

        Args:
            messages: OpenAI-shaped conversation, forwarded VERBATIM.
            tools: Optional OpenAI function schemas, forwarded unchanged.
            model: Optional per-call model override. When ``None``, the
                adapter forwards ``self.models.reasoning_model`` when it
                is non-empty (same defaulting rule as :meth:`complete`).
            max_output_tokens: Optional output-token budget.
            timeout: Per-call timeout in seconds.
            deadline: Optional absolute monotonic deadline.
            reasoning_effort: Optional per-call reasoning-effort
                override; forwarded only when non-empty.
            tool_choice: Optional obligation for the turn's tool use;
                forwarded only when set, so a wrapped provider that never
                learned the parameter still serves plain turns.

        Returns:
            The wrapped provider's :class:`ChatTurn`.

        Raises:
            NotImplementedError: The wrapped provider has no ``chat``
                (check :meth:`supports_tool_calls` first).
        """
        inner_chat = getattr(self._provider, "chat", None)
        if not callable(inner_chat):
            raise NotImplementedError(
                "Wrapped provider does not implement native tool calling."
            )
        call_kwargs: dict[str, object] = {
            "tools": tools,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
            "deadline": deadline,
        }
        effective_model = model or self._models.reasoning_model
        if effective_model:
            call_kwargs["model"] = effective_model
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
        if tool_choice is not None:
            call_kwargs["tool_choice"] = tool_choice
        return inner_chat(messages, **call_kwargs)

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
        """Forward a structured-output request to the wrapped provider.

        Args:
            prompt: User-facing input text.
            schema: JSON Schema object for the expected response.
            schema_name: Stable provider-facing schema name.
            schema_description: Optional provider-facing schema purpose.
            system: Optional system instruction.
            model: Optional per-call model override. When ``None``, the
                adapter forwards ``self.models.reasoning_model`` when it
                is non-empty.
            max_output_tokens: Optional output-token budget.
            timeout: Per-call timeout in seconds.
            state: Optional mutable token-accounting state.
            deadline: Optional absolute monotonic deadline.
            reasoning_effort: Optional per-call reasoning-effort override.
                Forwarded only when it is a non-empty string, so a wrapped
                provider whose ``complete_structured`` predates this parameter
                keeps working for the common ``""``/``None`` case.

        Returns:
            StructuredLLMResponse from the wrapped provider.
        """
        structured = getattr(self._provider, "complete_structured", None)
        if not callable(structured):
            raise NotImplementedError(
                "Wrapped provider does not implement structured output."
            )
        call_kwargs: dict[str, object] = {
            "schema": schema,
            "schema_name": schema_name,
            "schema_description": schema_description,
            "system": system,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
            "state": state,
            "deadline": deadline,
        }
        effective_model = model or self._models.reasoning_model
        if effective_model:
            call_kwargs["model"] = effective_model
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort
        return structured(prompt, **call_kwargs)

    @contextmanager
    def observe_retries(self, callback: ProviderRetryCallback) -> Iterator[object]:
        """Forward retry observation to the wrapped provider when available."""
        observer = getattr(self._provider, "observe_retries", None)
        if callable(observer):
            with observer(callback):
                yield self
        else:
            yield self

    def consume_retry_notices(self) -> list[dict[str, Any]]:
        """Forward retry diagnostics from the wrapped provider when available."""
        consumer = getattr(self._provider, "consume_retry_notices", None)
        if not callable(consumer):
            return []
        notices = consumer()
        if not isinstance(notices, list):
            return []
        return [dict(item) for item in notices if isinstance(item, dict)]

    def is_available(self) -> bool:
        """Forward the availability check to the wrapped provider.

        Returns:
            ``True`` when the wrapped provider reports itself ready
            to attempt requests; ``False`` otherwise. The adapter
            adds no availability logic of its own.
        """
        return self._provider.is_available()

    def consume_nonfatal_notice(self) -> str | None:
        """Forward an optional non-fatal notice from the wrapped provider.

        Returns:
            The wrapped provider's ``consume_nonfatal_notice()`` value
            when that method exists (typically a one-shot warning
            string used by the runtime to surface degraded-but-
            successful conditions). ``None`` when the wrapped provider
            does not implement this hook.
        """
        consumer = getattr(self._provider, "consume_nonfatal_notice", None)
        if callable(consumer):
            return consumer()
        return None


class UntrustedContentRuleProvider(LLMProvider):
    """Every model call carries the rule that fenced content is data.

    A rule attached by prompt builders, one at a time, is missing from
    every call whose builder does not attach it — and such a call reads
    source text with no word about what a fence means, so an instruction
    planted in a document stands on the same footing as its own. This
    decorator makes the rule a property of calling a model at all:
    it appends :data:`~inqtrix.untrusted_content.UNTRUSTED_CONTENT_RULE`
    to the system instruction of every ``complete*`` call and to the
    system message of every ``chat`` turn, exactly once.

    Applied by :func:`inqtrix.providers.instrument_providers`, the one
    wrap point the provider factories pass through, and OUTSIDE the
    tracing decorator: tracing records what it is handed, so the rule has
    to be attached before it for a trace to show the prompt the model
    actually received.

    Transparency contract, as for the other decorators: every member of
    the :class:`LLMProvider` surface is forwarded explicitly (a base-class
    default must never shadow the wrapped provider's answer), everything
    else reaches the wrapped provider through ``__getattr__``, and the
    wrapped provider stays reachable as ``_provider`` for the unwrap
    helpers.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        # Reached only when normal lookup fails. Guard the delegate
        # attribute itself so a half-constructed instance cannot recurse.
        if name == "_provider":
            raise AttributeError(name)
        return getattr(self._provider, name)

    # -- forwarded LLMProvider surface -----------------------------------
    @property
    def complete_uses_metadata(self) -> bool:
        return getattr(self._provider, "complete_uses_metadata", False) is True

    @property
    def selectable_models(self) -> list[str]:
        return self._provider.selectable_models

    @property
    def context_window_tokens(self) -> int | None:
        return self._provider.context_window_tokens

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return reported_capability(
            self._provider, "supports_structured_output", model=model
        )

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return reported_capability(
            self._provider, "supports_tool_calls", model=model
        )

    def is_available(self) -> bool:
        return self._provider.is_available()

    # -- the calls, each carrying the rule ------------------------------
    def complete(self, prompt: str, *, system: str | None = None, **kwargs: Any) -> str:
        from inqtrix.untrusted_content import with_untrusted_content_rule

        return self._provider.complete(
            prompt, system=with_untrusted_content_rule(system), **kwargs
        )

    def complete_with_metadata(
        self, prompt: str, *, system: str | None = None, **kwargs: Any
    ) -> "LLMResponse":
        from inqtrix.untrusted_content import with_untrusted_content_rule

        return self._provider.complete_with_metadata(
            prompt, system=with_untrusted_content_rule(system), **kwargs
        )

    def complete_structured(
        self, prompt: str, *, system: str | None = None, **kwargs: Any
    ) -> "StructuredLLMResponse":
        from inqtrix.untrusted_content import with_untrusted_content_rule

        return self._provider.complete_structured(
            prompt, system=with_untrusted_content_rule(system), **kwargs
        )

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> "ChatTurn":
        from inqtrix.untrusted_content import messages_with_untrusted_content_rule

        return self._provider.chat(
            messages_with_untrusted_content_rule(messages), **kwargs
        )


def with_untrusted_content_rule_provider(provider: LLMProvider) -> LLMProvider:
    """*provider* decorated with the rule, unless its chain already is.

    Walks the ``_provider`` chain rather than looking at the outermost
    object only: the wrap point runs again on an already-instrumented
    context, where the rule sits under the tracing decorator.
    """
    link: Any = provider
    while link is not None:
        if isinstance(link, UntrustedContentRuleProvider):
            return provider
        link = link.__dict__.get("_provider") if hasattr(link, "__dict__") else None
    return UntrustedContentRuleProvider(provider)

# =========================================================
# ProviderContext
# =========================================================


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """Group the active LLM and search providers for runtime injection.

    Attributes:
        llm (LLMProvider): Active language-model provider used by the
            graph's reasoning and claim-extraction paths.
        search (SearchProvider): Active search provider used by the
            graph's search node.
    """

    llm: LLMProvider
    search: SearchProvider
