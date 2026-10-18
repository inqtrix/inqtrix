""" Tests for credential sanitization in error and log messages.

Two related helpers:

* :func:`sanitize_error` — used in explicit error stringification paths
  (HTTP responses, stderr prints). Removes credentials.
* :func:`sanitize_log_message` — used by the centralized logging filter.
  Currently shares the same scrubbing rules; kept as a separate symbol so
  log handlers and explicit error paths can diverge later if needed.

Both intentionally keep harmless URLs intact (so logs stay debuggable) and
only strip credential values *inside* URLs.
"""

import pytest

from inqtrix.urls import (
    CredentialBearingUrlError,
    safe_public_url_identity,
    sanitize_error,
    sanitize_log_message,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/data?client%5Fsecret=value",
        "https://example.com/data?ok=1;X-Amz-Signature=value",
        "https://example.com/data#access_token=value",
        "https://user:password@example.com/data",
    ],
)
def test_safe_public_url_identity_rejects_encoded_and_fragment_credentials(
    url: str,
) -> None:
    with pytest.raises(CredentialBearingUrlError):
        safe_public_url_identity(url)


def test_safe_public_url_identity_allows_noncredential_pagination_token() -> None:
    identity = safe_public_url_identity(
        "https://example.com/data?$skiptoken=public-page-cursor"
    )
    assert identity.canonical_url.endswith("$skiptoken=public-page-cursor")


class TestSanitizeError:

    def test_redacts_sk_key(self):
        # Realistic length: real API keys are 30+ chars after the prefix.
        err = Exception("Error with key sk-25a910468036a4ff70d035b2241e088d")
        result = sanitize_error(err)
        assert "sk-25a910468036a4ff70d035b2241e088d" not in result
        assert "[KEY]" in result

    def test_redacts_pplx_key(self):
        err = Exception("Error with key pplx-6MdfNZ6uKh9qTestKeyLongerThanSixteen")
        result = sanitize_error(err)
        assert "pplx-6MdfNZ6uKh9qTestKeyLongerThanSixteen" not in result
        assert "[KEY]" in result

    def test_redacts_bearer_token(self):
        err = Exception("Auth failed: Bearer eyJhbGciOi.token.here")
        result = sanitize_error(err)
        assert "eyJhbGciOi" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_aws_access_key(self):
        err = Exception("Calling AWS: AKIAIOSFODNN7EXAMPLE failed")
        result = sanitize_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[AWS_KEY]" in result

    def test_redacts_aws_session_token_kv(self):
        err = Exception("aws_session_token=secret123/xyz==")
        result = sanitize_error(err)
        assert "secret123" not in result
        assert "[REDACTED]" in result

    def test_preserves_safe_text(self):
        err = Exception("Connection timeout after 30 seconds")
        result = sanitize_error(err)
        assert result == "Connection timeout after 30 seconds"

    def test_preserves_plain_url(self):
        """Benign URLs must remain visible so logs stay debuggable."""
        err = Exception("Failed to connect to https://api.example.com/v1/chat")
        result = sanitize_error(err)
        assert "https://api.example.com/v1/chat" in result
        assert "[URL]" not in result

    def test_redacts_url_credential_query_parameter(self):
        """URLs that carry a credential as ?api_key=... lose only the credential value."""
        err = Exception(
            "Failing GET https://api.example.com/v1/chat?api_key=sk-secret123abcdef&page=2"
        )
        result = sanitize_error(err)
        # URL itself remains visible
        assert "https://api.example.com/v1/chat" in result
        # Credential value is gone
        assert "sk-secret123abcdef" not in result
        # And the query parameter is marked as redacted
        assert "api_key=[REDACTED]" in result
        # Non-credential parameter is preserved
        assert "page=2" in result

    def test_redacts_url_token_query_parameter(self):
        err = Exception("https://x.example.com/data?token=abcDEF123 returned 500")
        result = sanitize_error(err)
        assert "abcDEF123" not in result
        assert "token=[REDACTED]" in result
        assert "https://x.example.com/data" in result

    def test_multiple_redactions(self):
        err = Exception(
            "Key sk-abcdefghijklmnopqr at https://example.com with pplx-zyxwvutsrqponmlkjih"
        )
        result = sanitize_error(err)
        assert "sk-abcdefghijklmnopqr" not in result
        assert "pplx-zyxwvutsrqponmlkjih" not in result
        # URL stays — it carried no credential
        assert "https://example.com" in result


class TestSanitizeLogMessage:

    def test_log_message_keeps_markdown_link(self):
        msg = "ANSWER text:\n## Kurzfazit\n[6](https://www.zacks.com/article)"
        result = sanitize_log_message(msg)
        assert "https://www.zacks.com/article" in result
        assert "[URL]" not in result

    def test_log_message_strips_credential_in_link(self):
        msg = "Calling https://api.x.com/v1?api_key=sk-secretkey1234567890&q=foo"
        result = sanitize_log_message(msg)
        assert "sk-secretkey1234567890" not in result
        assert "api_key=[REDACTED]" in result
        assert "q=foo" in result

    def test_log_message_strips_bare_api_key(self):
        msg = "Loaded credential pplx-realLookingPplxKeyHere1234567890"
        result = sanitize_log_message(msg)
        assert "pplx-realLookingPplxKeyHere1234567890" not in result
        assert "[KEY]" in result
