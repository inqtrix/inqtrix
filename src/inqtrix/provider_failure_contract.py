"""Stable, text-independent provider failure classification.

Provider SDKs expose different exception classes, but their typed status and
transport attributes are sufficient to distinguish proven transient outages
from deterministic request/configuration errors.  Callers share this one
classifier so retry, pause, and terminal-state decisions cannot drift.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from inqtrix.exceptions import AgentProviderTimeout, AgentRateLimited


class ProviderFailureKind(str, Enum):
    """Stable provider failure categories used by execution boundaries."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderFailureClassification:
    """One typed provider failure decision without exception message text."""

    kind: ProviderFailureKind
    status_code: int | None = None
    content_refused: bool = False
    """A terminal rejection whose structured code says the provider's content
    filter refused the request: deterministic and bound to the request's
    content, so repeating the same request cannot succeed."""

    @property
    def transient(self) -> bool:
        """Whether the failure is safe to park for a later resume."""

        return self.kind in {
            ProviderFailureKind.TIMEOUT,
            ProviderFailureKind.RATE_LIMITED,
            ProviderFailureKind.UNAVAILABLE,
        }


_TIMEOUT_TYPES = frozenset(
    {
        "APITimeoutError",
        "ConnectTimeout",
        "ConnectTimeoutError",
        "ReadTimeout",
        "ReadTimeoutError",
        "TimeoutException",
    }
)
_RATE_LIMIT_TYPES = frozenset({"RateLimitError"})
CONTENT_REFUSAL_CODES = frozenset({"content_filter"})
"""Structured provider error codes of a request a content filter refused."""
_TRANSPORT_TYPES = frozenset(
    {
        "APIConnectionError",
        "ConnectError",
        "NetworkError",
        "ReadError",
        "RemoteProtocolError",
        "TransportError",
        "WriteError",
    }
)


def exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Walk explicit provider wrappers without inspecting message text.

    ``__context__`` is deliberately excluded: it can describe an unrelated
    exception handled earlier in the same block and is therefore not proof
    that the current operation failed transiently.
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        for candidate in (
            getattr(current, "source", None),
            getattr(current, "original", None),
            current.__cause__,
        ):
            if isinstance(candidate, BaseException):
                pending.append(candidate)


def _status_code(exc: BaseException) -> int | None:
    """Read common SDK HTTP status attributes without parsing strings."""

    response = getattr(exc, "response", None)
    response_metadata = (
        response.get("ResponseMetadata") if isinstance(response, dict) else None
    )
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "http_status", None),
        getattr(exc, "http_status_code", None),
        getattr(exc, "code", None),
        getattr(response, "status_code", None),
        (
            response_metadata.get("HTTPStatusCode")
            if isinstance(response_metadata, dict)
            else None
        ),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _structured_code(exc: BaseException) -> str:
    """A provider's structured error code: the Inqtrix wrapper's ``error_code``
    or an SDK's string ``code`` attribute — never message text."""
    for value in (getattr(exc, "error_code", None), getattr(exc, "code", None)):
        if isinstance(value, str) and value:
            return value
    return ""


def classify_provider_failure(
    exc: BaseException,
) -> ProviderFailureClassification:
    """Classify a provider failure from stable types and status attributes.

    An explicit non-transient HTTP response is terminal even when a nested
    exception happens to look transport-related.  Unknown exceptions are not
    assumed transient; callers must fail them terminally.
    """

    chain = tuple(exception_chain(exc))

    # Inqtrix wrappers are semantic outcomes produced only after the shared
    # provider attempt budget has been exhausted.  They therefore outrank
    # incidental details retained on their SDK exception.
    for current in chain:
        if isinstance(current, AgentProviderTimeout):
            return ProviderFailureClassification(ProviderFailureKind.TIMEOUT)
        if isinstance(current, AgentRateLimited):
            return ProviderFailureClassification(
                ProviderFailureKind.RATE_LIMITED
            )

    # An explicit HTTP response is stronger evidence than any transport-like
    # exception elsewhere in the wrapper chain.  Scan all statuses before
    # considering class names so a deterministic 4xx cannot be parked merely
    # because the wrapper also retained an earlier timeout.
    for current in chain:
        status_code = _status_code(current)
        if status_code is not None:
            if status_code == 408:
                return ProviderFailureClassification(
                    ProviderFailureKind.TIMEOUT, status_code
                )
            if status_code == 429:
                return ProviderFailureClassification(
                    ProviderFailureKind.RATE_LIMITED, status_code
                )
            if 500 <= status_code <= 599:
                return ProviderFailureClassification(
                    ProviderFailureKind.UNAVAILABLE, status_code
                )
            # Timeouts, rate limits and outages returned above; what reaches
            # here is a deterministic rejection of the request.
            return ProviderFailureClassification(
                ProviderFailureKind.TERMINAL,
                status_code,
                content_refused=any(
                    _structured_code(item) in CONTENT_REFUSAL_CODES for item in chain
                ),
            )

    # gRPC status is the protocol-level equivalent of an HTTP response.
    for current in chain:
        code_reader = getattr(current, "code", None)
        if callable(code_reader):
            try:
                code = code_reader()
            except Exception:  # pragma: no cover - defensive SDK boundary
                code = None
            code_name = getattr(code, "name", None)
            if code_name == "DEADLINE_EXCEEDED":
                return ProviderFailureClassification(ProviderFailureKind.TIMEOUT)
            if code_name == "RESOURCE_EXHAUSTED":
                return ProviderFailureClassification(
                    ProviderFailureKind.RATE_LIMITED
                )
            if code_name == "UNAVAILABLE":
                return ProviderFailureClassification(
                    ProviderFailureKind.UNAVAILABLE
                )

    # Only after structured response signals have been exhausted are SDK and
    # Python transport types sufficient proof of a transient outage.
    for current in chain:
        type_name = type(current).__name__
        if isinstance(current, TimeoutError) or type_name in _TIMEOUT_TYPES:
            return ProviderFailureClassification(ProviderFailureKind.TIMEOUT)
        if type_name in _RATE_LIMIT_TYPES:
            return ProviderFailureClassification(
                ProviderFailureKind.RATE_LIMITED
            )
        if isinstance(current, ConnectionError) or type_name in _TRANSPORT_TYPES:
            return ProviderFailureClassification(
                ProviderFailureKind.UNAVAILABLE
            )

    return ProviderFailureClassification(ProviderFailureKind.UNKNOWN)
