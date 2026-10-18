"""Centralised cassette sanitization for VCR-recorded provider tests.

This module is the single source of truth for stripping secrets from
recorded HTTP interactions before they are committed to the repository.
It is consumed by :mod:`tests.replay.conftest` (as ``before_record_request``
and ``before_record_response`` hooks) and by
:mod:`tests.replay.test_sanitization` (as the protective unit-test gate
that scans every committed cassette for left-over credential material).

Lives in ``tests/fixtures/`` rather than ``src/inqtrix/`` because
sanitisation is a test-only concern: production providers receive their
secrets via constructor args and never write them anywhere persistent
(constructor-first configuration). Library code MUST
NOT import from this module.

Conventions:

* Header names are matched **case-insensitive** and replaced with the
  literal placeholder ``"REDACTED"``. Removing the header outright would
  change the request fingerprint and break VCR's matchers; replacing
  the value preserves the structure while neutralising the secret.
* Bearer tokens in any header value (``"Bearer eyJ..."``,
  ``"Bearer sk-..."``) are scrubbed regardless of the header name —
  belt-and-suspenders defence against custom auth headers.
* JSON request bodies are scrubbed key-wise: any field whose name
  matches one of :data:`SECRET_BODY_KEYS` (case-insensitive) gets its
  value replaced. Unknown body shapes (non-JSON, malformed JSON,
  binary) are left untouched — VCR will still see them, but the
  protective scan downstream will still flag obvious patterns.
* Query parameters carrying secrets (``?api_key=...``,
  ``?subscription_key=...``) are scrubbed in :func:`before_record_request`
  by URL-rewriting.
* Response bodies are not scrubbed by default — well-behaved backends
  do not echo secrets back. The protective scan still inspects them as
  a safety net.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

REDACTED = "REDACTED"
"""Single placeholder used for every scrubbed value.

Constant string keeps cassette diffs readable and avoids accidentally
introducing realistic-looking dummies that future scans might miss.
"""

SANITIZED_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "x-api-key",
    "api-key",
    "x-subscription-token",
    "x-subscription-key",
    "x-aws-ec2-metadata-token",
    "x-amz-security-token",
    "x-amz-date",
    "x-amz-content-sha256",
    "anthropic-api-key",
    "anthropic-version",
    "openai-api-key",
    "openai-organization",
    "openai-project",
    "azure-api-key",
    "ocp-apim-subscription-key",
    "set-cookie",
    "cookie",
})
"""Header names that always carry secret or session-bound values.

Matched case-insensitive. Anthropic-version is included because its
combination with anthropic-api-key narrows fingerprinting; AWS SigV4
signature material is included because the signature value is derived
from the secret access key and replaying it would re-leak that
relationship.
"""

SANITIZED_QUERY_KEYS: frozenset[str] = frozenset({
    "api_key",
    "apikey",
    "key",
    "subscription_key",
    "access_token",
    "x-api-key",
    "code",
})
"""Query-parameter names that carry credentials in URL form.

``"key"`` and ``"code"`` are intentionally generic to catch one-off
provider quirks; the protective scan downstream catches false negatives.
"""

SECRET_BODY_KEYS: frozenset[str] = frozenset({
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "secret_key",
    "secretkey",
    "client_secret",
    "clientsecret",
    "password",
    "token",
    "bearer",
    "session_token",
    "subscription_key",
})
"""JSON body keys whose values are scrubbed in request payloads.

Matched case-insensitive. ``"token"`` and ``"bearer"`` cover OAuth
exchange responses that providers occasionally re-POST.
"""

# Patterns the protective scan flags as "likely a real secret".  The
# patterns are intentionally permissive (false positives over false
# negatives) and pre-compiled at module load to keep the scan fast even
# on hundreds of cassettes.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("perplexity key", re.compile(r"\bpplx-[A-Za-z0-9_-]{20,}\b")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws secret access key", re.compile(r"(?i)aws_secret_access_key[\"\s:=]+[A-Za-z0-9/+=]{40,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("bearer literal", re.compile(r"(?i)bearer\s+[A-Za-z0-9_.\-]{20,}")),
    ("azure ad client secret", re.compile(r"(?i)client_secret[\"\s:=]+[A-Za-z0-9~_.\-]{30,}")),
)
"""Regex catalogue used by the protective scan.

Each entry is ``(label, compiled_pattern)``. Adding a new pattern only
requires appending to this tuple — the scan iterates it generically.
"""


def _scrub_text(text: str) -> str:
    """Replace any matched secret pattern in *text* with :data:`REDACTED`."""
    for _label, pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _scrub_headers(headers: Any) -> None:
    """Mutate *headers* in place, replacing values for sanitized header names.

    VCR exposes headers as either a dict-like or a list of tuples
    depending on which transport recorded them. This helper supports
    both shapes without raising for unexpected types.
    """
    if isinstance(headers, dict):
        for key in list(headers.keys()):
            if key.lower() in SANITIZED_HEADERS:
                headers[key] = REDACTED
            else:
                value = headers[key]
                if isinstance(value, str):
                    headers[key] = _scrub_text(value)
                elif isinstance(value, list):
                    headers[key] = [
                        _scrub_text(v) if isinstance(v, str) else v
                        for v in value
                    ]
    elif isinstance(headers, list):
        for index, item in enumerate(headers):
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            key, value = item
            new_value = (
                REDACTED
                if isinstance(key, str) and key.lower() in SANITIZED_HEADERS
                else (_scrub_text(value) if isinstance(value, str) else value)
            )
            headers[index] = (key, new_value)


def _scrub_query(url: str) -> str:
    """Return *url* with sanitized query-parameter values replaced."""
    if not url or "?" not in url:
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    rewritten = [
        (key, REDACTED if key.lower() in SANITIZED_QUERY_KEYS else value)
        for key, value in pairs
    ]
    return urlunsplit(parts._replace(query=urlencode(rewritten)))


def scrub_json_body(body: bytes | str | None) -> bytes | str | None:
    """Return a sanitized copy of a JSON request/response body.

    Non-JSON bodies are returned unchanged (VCR still records them; the
    protective scan downstream will catch obvious leaks). Empty bodies
    return ``None`` to keep the cassette diff minimal.

    Args:
        body: Raw payload as ``bytes`` (typical for VCR-recorded
            requests) or ``str`` (typical for stub-fed test inputs).
            ``None`` is passed through.

    Returns:
        The sanitized body in the same type as the input. JSON
        structures get their secret fields replaced with
        :data:`REDACTED`; non-JSON content is returned untouched.
    """
    if body is None or body == b"" or body == "":
        return None

    raw_bytes = body.encode("utf-8") if isinstance(body, str) else body
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return body

    try:
        parsed = json.loads(decoded)
    except (json.JSONDecodeError, ValueError):
        scrubbed_text = _scrub_text(decoded)
        if scrubbed_text == decoded:
            return body
        return scrubbed_text if isinstance(body, str) else scrubbed_text.encode("utf-8")

    sanitized = _sanitize_json_node(parsed)
    out = json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False)
    return out if isinstance(body, str) else out.encode("utf-8")


def _sanitize_json_node(node: Any) -> Any:
    """Recursively sanitize a JSON node, replacing secret-key values."""
    if isinstance(node, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in SECRET_BODY_KEYS
                else _sanitize_json_node(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_sanitize_json_node(item) for item in node]
    if isinstance(node, str):
        return _scrub_text(node)
    return node


def before_record_request(request: Any) -> Any:
    """VCR hook: sanitize a request before it is written to a cassette.

    Scrubs headers (via :data:`SANITIZED_HEADERS`), query parameters
    (via :data:`SANITIZED_QUERY_KEYS`), and JSON body fields (via
    :data:`SECRET_BODY_KEYS`). The request object is mutated in place
    and returned so VCR can chain further hooks.

    Returns ``None`` would cause VCR to drop the interaction entirely;
    we always want to keep the interaction (just scrubbed).
    """
    headers = getattr(request, "headers", None)
    if headers is not None:
        _scrub_headers(headers)

    uri = getattr(request, "uri", None)
    if isinstance(uri, str):
        scrubbed_uri = _scrub_query(uri)
        if scrubbed_uri != uri:
            request.uri = scrubbed_uri

    body = getattr(request, "body", None)
    if body is not None:
        sanitized = scrub_json_body(body)
        if sanitized is not body:
            request.body = sanitized

    return request


def before_record_response(response: Any) -> Any:
    """VCR hook: light scrub of a response before writing to a cassette.

    Backends should not echo secrets back, but ``Set-Cookie`` and
    request-id headers are scrubbed for diff stability. JSON bodies are
    NOT touched by default — the protective scan provides a second
    defence layer if a backend ever leaks something.
    """
    if isinstance(response, dict):
        headers = response.get("headers")
        if headers is not None:
            _scrub_headers(headers)
    return response


def assert_cassette_clean(path: pathlib.Path) -> None:
    """Fail loudly when *path* contains residue of a real secret.

    Used by the protective unit test in ``tests/replay/test_sanitization.py``
    to guarantee that no committed cassette/fixture leaks credentials.
    The function reads the file as UTF-8 (cassettes are YAML/JSON; binary
    blobs would be base64 inside YAML and would not match the textual
    patterns) and runs every entry of :data:`_SECRET_PATTERNS` against it.

    Raises:
        AssertionError: When any pattern matches. The error message
            names the file, the pattern label, and the first matching
            substring (truncated to 60 chars) so a developer can locate
            the offending field quickly.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("assert_cassette_clean: cannot read %s as utf-8 (%s)", path, exc)
        return

    for label, pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            preview = match.group(0)[:60]
            raise AssertionError(
                f"Cassette {path} contains a likely secret "
                f"({label!r}): {preview!r}. "
                "Re-record with INQTRIX_RECORD_MODE=once after extending "
                "tests/fixtures/sanitize.py."
            )
