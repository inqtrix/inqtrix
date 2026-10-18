"""Unit tests for the cassette sanitization helper and a hard gate that
scans every committed cassette / fixture for residual credential
material.

The protective scan keeps Aufgabe 3's central promise: cassettes are
committable. If a contributor adds a new cassette without re-running
``INQTRIX_RECORD_MODE=once`` through ``before_record_request``, or if a
provider sneaks a new auth header into the request that
``tests/fixtures/sanitize.py`` does not yet know about, this test fails
in CI **before** the cassette ever reaches main.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tests.fixtures.sanitize import (
    REDACTED,
    SANITIZED_HEADERS,
    SECRET_BODY_KEYS,
    SANITIZED_QUERY_KEYS,
    _scrub_headers,
    _scrub_query,
    _scrub_text,
    assert_cassette_clean,
    before_record_request,
    before_record_response,
    scrub_json_body,
)

pytestmark = pytest.mark.replay


# ---------------------------------------------------------------------------
# Header scrubbing
# ---------------------------------------------------------------------------


def test_scrub_headers_dict_replaces_known_keys_case_insensitive() -> None:
    headers = {
        "Authorization": "Bearer eyJhbgciOiJIUzI1NiJ9.payload.sig-not-a-real-jwt-token",
        "X-API-Key": "sk-fake-but-lookalike-1234567890",
        "User-Agent": "inqtrix/0.1",
    }

    _scrub_headers(headers)

    assert headers["Authorization"] == REDACTED
    assert headers["X-API-Key"] == REDACTED
    assert headers["User-Agent"] == "inqtrix/0.1"


def test_scrub_headers_list_of_tuples_supported() -> None:
    headers: list = [
        ("authorization", "Bearer eyJabcdefghij.payload.signature1234567"),
        ("x-trace-id", "trace-1"),
    ]

    _scrub_headers(headers)

    assert headers[0] == ("authorization", REDACTED)
    assert headers[1] == ("x-trace-id", "trace-1")


def test_scrub_headers_replaces_bearer_pattern_in_unknown_header() -> None:
    headers = {"X-Custom-Auth": "Bearer sk-fake-but-lookalike-1234567890"}

    _scrub_headers(headers)

    assert REDACTED in headers["X-Custom-Auth"]
    assert "sk-" not in headers["X-Custom-Auth"]


def test_known_header_set_covers_every_provider() -> None:
    expected_min_coverage = {
        "authorization",
        "x-api-key",
        "api-key",
        "x-subscription-token",
        "anthropic-api-key",
        "ocp-apim-subscription-key",
    }
    assert expected_min_coverage.issubset(SANITIZED_HEADERS)


# ---------------------------------------------------------------------------
# Query parameter scrubbing
# ---------------------------------------------------------------------------


def test_scrub_query_replaces_known_query_keys() -> None:
    url = "https://api.example.com/v1/search?api_key=sk-real-fake-1234567890&q=hello"
    out = _scrub_query(url)
    assert "api_key=REDACTED" in out
    assert "q=hello" in out


def test_scrub_query_passes_through_url_without_query() -> None:
    assert _scrub_query("https://api.example.com/v1/search") == (
        "https://api.example.com/v1/search"
    )


def test_query_keys_match_documented_set() -> None:
    assert "api_key" in SANITIZED_QUERY_KEYS
    assert "subscription_key" in SANITIZED_QUERY_KEYS


# ---------------------------------------------------------------------------
# JSON body scrubbing
# ---------------------------------------------------------------------------


def test_scrub_json_body_replaces_top_level_secret_keys() -> None:
    payload = json.dumps({
        "model": "gpt-4o",
        "api_key": "sk-fake-but-lookalike-1234567890",
        "messages": [{"role": "user", "content": "Hello"}],
    }).encode("utf-8")

    sanitized = scrub_json_body(payload)
    assert isinstance(sanitized, bytes)
    parsed = json.loads(sanitized)
    assert parsed["api_key"] == REDACTED
    assert parsed["model"] == "gpt-4o"


def test_scrub_json_body_replaces_nested_secret_keys() -> None:
    payload = json.dumps({
        "auth": {"client_secret": "very-secret-azure-ad-12345"},
        "params": {"items": [{"token": "leaked-token-1234567890"}]},
    }).encode("utf-8")

    sanitized = scrub_json_body(payload)
    parsed = json.loads(sanitized)
    assert parsed["auth"]["client_secret"] == REDACTED
    assert parsed["params"]["items"][0]["token"] == REDACTED


def test_scrub_json_body_handles_string_input() -> None:
    sanitized = scrub_json_body('{"api_key": "sk-fake-1234567890123"}')
    assert isinstance(sanitized, str)
    assert json.loads(sanitized)["api_key"] == REDACTED


def test_scrub_json_body_passes_through_non_json_text() -> None:
    sanitized = scrub_json_body(b"plain text body without secrets")
    assert sanitized == b"plain text body without secrets"


def test_scrub_json_body_returns_none_for_empty_input() -> None:
    assert scrub_json_body(None) is None
    assert scrub_json_body(b"") is None
    assert scrub_json_body("") is None


def test_secret_body_keys_match_documented_set() -> None:
    for key in ("api_key", "client_secret", "access_key", "token"):
        assert key in SECRET_BODY_KEYS


# ---------------------------------------------------------------------------
# VCR-hook integration
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for a vcr.request.Request in unit tests."""

    def __init__(self, uri: str, headers: dict, body: bytes | None) -> None:
        self.uri = uri
        self.headers = headers
        self.body = body


def test_before_record_request_scrubs_headers_query_and_body() -> None:
    request = _FakeRequest(
        uri="https://api.example.com/v1/x?api_key=sk-fake-1234567890",
        headers={"Authorization": "Bearer eyJabcd.efgh.ijkl"},
        body=json.dumps({"client_secret": "very-secret-12345"}).encode("utf-8"),
    )

    out = before_record_request(request)

    assert out is request
    assert "api_key=REDACTED" in request.uri
    assert request.headers["Authorization"] == REDACTED
    assert json.loads(request.body)["client_secret"] == REDACTED


def test_before_record_response_scrubs_set_cookie_header() -> None:
    response = {"headers": {"Set-Cookie": "session=very-real-session-token-xyz"}}

    out = before_record_response(response)

    assert out["headers"]["Set-Cookie"] == REDACTED


# ---------------------------------------------------------------------------
# Helpers for pattern catalogue
# ---------------------------------------------------------------------------


def test_scrub_text_replaces_obvious_openai_key() -> None:
    out = _scrub_text("token=sk-abcdefghijklmnopqrstuvwxyz")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in out
    assert REDACTED in out


def test_scrub_text_leaves_clean_text_untouched() -> None:
    text = "The quick brown fox jumps over the lazy dog."
    assert _scrub_text(text) == text


# ---------------------------------------------------------------------------
# Protective scan: every committed cassette / fixture
# ---------------------------------------------------------------------------


_FIXTURES_ROOT = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def _committed_fixture_files() -> list[pathlib.Path]:
    """Return every YAML / JSON file under ``tests/fixtures/`` recursively.

    Empty Bedrock JSON-stub fixtures or new-and-not-yet-committed cassettes
    are included automatically — the directory walk is the discovery
    mechanism, no manual list to maintain.
    """
    if not _FIXTURES_ROOT.exists():
        return []
    return [
        path
        for ext in ("*.yaml", "*.yml", "*.json")
        for path in _FIXTURES_ROOT.rglob(ext)
    ]


def test_every_committed_cassette_passes_secret_scan() -> None:
    files = _committed_fixture_files()
    if not files:
        pytest.skip("No cassettes/fixtures are committed; nothing to scan.")
    for path in files:
        assert_cassette_clean(path)


def test_assert_cassette_clean_flags_a_planted_secret(tmp_path: pathlib.Path) -> None:
    leaky = tmp_path / "leaky.yaml"
    leaky.write_text(
        "interactions:\n"
        "  - request:\n"
        "      headers:\n"
        "        Authorization: Bearer sk-ant-this-is-fake-but-matches-pattern-12345\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="anthropic key|likely secret"):
        assert_cassette_clean(leaky)
