"""Content-capture policy for trace attributes (the ONLY content path).

Whether prompts, raw responses, and tool payloads reach span attributes
is decided here — instrumentation code never writes content without
this policy. Langfuse OSS applies no server-side truncation or masking,
so redaction and byte caps on THIS side are the only guard.

Capture resolution (``INQTRIX_TRACE_CONTENT``):

* ``auto`` (default) — capture follows ``OBSERVABILITY_PROFILE``:
  content only in the ``forensic`` profile.
* ``on`` / ``off`` — explicit override, independent of the profile.

An incognito request withholds its content whatever the configuration
(:func:`withheld_content`): the policy then captures nothing in that
request's scope.

Redaction reuses the EXISTING sanitizers (no second redaction system):
:func:`inqtrix.runtime_logging.redact_trace_payload` for structured
payloads (drop-list for credential keys, URL secret scrubbing) and
``sanitize_log_message`` for plain strings; a hard per-attribute byte
cap with a visible truncation marker bounds every value.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from inqtrix.observability import semconv
from inqtrix.runtime_logging import redact_trace_payload
from inqtrix.urls import sanitize_log_message

try:  # Optional dependency, like the tracing wrappers that read this policy.
    from opentelemetry import context as _otel_context
except Exception:  # noqa: BLE001 — extra not installed
    _otel_context = None

log = logging.getLogger("inqtrix")

if TYPE_CHECKING:
    from inqtrix.settings import Settings

_TRUNCATION_MARKER = "…[truncated]"

# A private key in the OpenTelemetry context, not a context variable of its
# own: every executor hop already re-enters the caller's trace context
# (``capture_trace_context`` / ``attached_trace_context``, copied contexts),
# so the withheld scope reaches each span the request's trace reaches. A
# separate variable would have to be re-bound at every hop.
_WITHHELD_KEY = (
    _otel_context.create_key("inqtrix-content-withheld")
    if _otel_context is not None
    else None
)


@contextmanager
def withheld_content() -> Iterator[None]:
    """Withhold content from every span recorded in this scope.

    For an incognito request: no prompt, answer or tool payload reaches a
    span, whatever the configured policy, in this thread and in every thread
    the scope's trace context is handed to. Without the observability extra
    there are no spans to withhold content from.
    """
    if _otel_context is None:
        yield
        return
    token = _otel_context.attach(_otel_context.set_value(_WITHHELD_KEY, True))
    try:
        yield
    finally:
        _otel_context.detach(token)


def content_withheld() -> bool:
    """Whether the current scope withholds content (:func:`withheld_content`)."""
    return _otel_context is not None and bool(
        _otel_context.get_value(_WITHHELD_KEY)
    )


@dataclass(frozen=True)
class ClippedText:
    """One redacted, byte-capped attribute value plus its original size."""

    text: str
    original_size: int
    truncated: bool


@dataclass(frozen=True)
class ContentCapturePolicy:
    """Decides IF content is captured and bounds HOW MUCH of it."""

    configured_capture: bool
    max_attr_bytes: int

    @property
    def capture_content(self) -> bool:
        """Whether content is captured here: configured, and not withheld
        by the current request (:func:`withheld_content`)."""
        return self.configured_capture and not content_withheld()

    def clip_text(self, value: object, *, json_string: bool = False) -> ClippedText:
        """Redact and cap text, optionally retaining valid JSON string encoding.

        JSON strings preserve literal values such as ``null`` when a sink
        decodes attributes. Their byte budget includes quotes and escapes.
        """
        redacted = sanitize_log_message(str(value or ""))
        if json_string:
            rendered = json.dumps(redacted, ensure_ascii=False)
            original_size = len(rendered.encode("utf-8"))
            if original_size <= self.max_attr_bytes:
                return ClippedText(rendered, original_size, False)
            marker = _TRUNCATION_MARKER
            if len(json.dumps(marker, ensure_ascii=False).encode("utf-8")) > self.max_attr_bytes:
                marker = ""
            low, high = 0, min(len(redacted), self.max_attr_bytes)
            # Only the exceptional oversized path needs a prefix search.
            # Cutting serialized JSON bytes would break escapes and syntax.
            while low < high:
                middle = (low + high + 1) // 2
                candidate = json.dumps(redacted[:middle] + marker, ensure_ascii=False)
                if len(candidate.encode("utf-8")) <= self.max_attr_bytes:
                    low = middle
                else:
                    high = middle - 1
            return ClippedText(
                json.dumps(redacted[:low] + marker, ensure_ascii=False),
                original_size,
                True,
            )
        encoded = redacted.encode("utf-8")
        if len(encoded) <= self.max_attr_bytes:
            return ClippedText(redacted, len(encoded), False)
        clipped = encoded[: self.max_attr_bytes].decode("utf-8", "ignore")
        return ClippedText(
            clipped + _TRUNCATION_MARKER, len(encoded), True
        )

    def clip_payload(
        self, payload: Any, *, json_string: bool = False
    ) -> ClippedText:
        """Sanitize a structured payload and render it as capped JSON.

        Shares the runtime-logging credential-key drop-list and URL
        redaction, without its field-specific preview limits, before
        serializing, so ``raw_response``-style fields can never leak
        into a span attribute even under content capture.
        ``json_string`` encodes the rendering as a JSON string (see
        :meth:`clip_text`), which stays valid JSON when the cap cuts it.
        """
        sanitized = redact_trace_payload(payload)
        try:
            rendered = json.dumps(
                sanitized, ensure_ascii=False, default=str
            )
        except (TypeError, ValueError) as exc:
            # The text rendering is no JSON: record it as a JSON string so
            # the attribute stays valid JSON, and say so once.
            if _first_warning("unserializable_payload"):
                log.warning(
                    "A trace payload could not be serialized as JSON (%s); it is "
                    "recorded as a JSON string of its text rendering.",
                    type(exc).__name__,
                )
            return self.clip_text(str(sanitized), json_string=True)
        return self.clip_text(rendered, json_string=json_string)


def record_content(
    span: Any, key: str, value: Any, policy: ContentCapturePolicy
) -> ClippedText:
    """Write one content value to a span attribute as valid JSON within the cap.

    Text becomes a JSON string. A structure becomes its object JSON text;
    when that does not fit, the JSON string of its cut rendering instead,
    because cut object text is no longer JSON. Every cut adds the
    truncation event naming *key*. The caller checks
    ``policy.capture_content`` first; this function only bounds and
    redacts.
    """
    if isinstance(value, str):
        clipped = policy.clip_text(value, json_string=True)
    else:
        clipped = policy.clip_payload(value)
        if clipped.truncated:
            clipped = policy.clip_payload(value, json_string=True)
    span.set_attribute(key, clipped.text)
    if clipped.truncated:
        span.add_event(
            semconv.TRUNCATION_EVENT,
            {
                semconv.TRUNCATION_LIMIT_NAME: key,
                semconv.TRUNCATION_ORIGINAL_SIZE: clipped.original_size,
                semconv.TRUNCATION_CAPPED_SIZE: len(clipped.text.encode("utf-8")),
            },
        )
    return clipped


_warned: set[str] = set()
"""Content-policy WARNINGs already logged in this process (each once)."""


def _first_warning(key: str) -> bool:
    """``True`` the first time the WARNING *key* is due in this process."""
    if key in _warned:
        return False
    _warned.add(key)
    return True


def build_content_policy(settings: "Settings") -> ContentCapturePolicy:
    """Resolve the policy from settings (see module docstring)."""
    mode = settings.observability.trace_content
    profile = str(
        getattr(settings.agent, "observability_profile", "summary")
    ).lower()
    capture = mode == "on" or (mode == "auto" and profile == "forensic")
    return ContentCapturePolicy(
        configured_capture=capture,
        max_attr_bytes=settings.observability.trace_max_attr_bytes,
    )


# ONE process-wide policy, published by ``create_providers`` from the
# COMPOSED settings. Deep call sites that cannot be threaded a policy
# (the kernel tool boundary) read it here, so every content attribute in
# the process is gated by the same decision — a fresh env-driven
# ``Settings()`` could diverge from what the app was actually composed
# with (embedded callers, tests).
_active_policy: ContentCapturePolicy | None = None


def set_active_content_policy(policy: ContentCapturePolicy) -> None:
    """Publish the composed policy (called by ``create_providers``)."""
    global _active_policy
    _active_policy = policy


def active_content_policy() -> ContentCapturePolicy:
    """The published policy; falls back to env settings ONCE if none, with a WARNING.

    The fallback covers processes that trace without building providers
    (tooling, partial test apps); it is cached as the active policy so
    repeated reads stay consistent.
    """
    global _active_policy
    if _active_policy is None:
        from inqtrix.settings import Settings

        log.warning(
            "No trace content policy was published in this process; it is built "
            "from the environment settings."
        )
        _active_policy = build_content_policy(Settings())
    return _active_policy
