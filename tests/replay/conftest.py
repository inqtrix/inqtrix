"""VCR + Stubber test configuration shared by every replay test module.

Centralises:

* The pytest-recording ``vcr_config`` session fixture (cassette format,
  record-mode resolution from ``INQTRIX_RECORD_MODE`` env, sanitization
  hooks).
* An ``autouse`` fixture that patches ``time.sleep`` to no-op for the
  duration of each replay test, so retry-loops in providers do not turn
  the suite into a 30-second wait.
* An ``autouse`` env-isolation fixture that strips provider-related
  ``*_API_KEY`` / ``*_ENDPOINT`` variables before every test, preventing
  a key exported in the shell from accidentally turning a replay test
  into a live call.

Recording workflow (an explicit maintainer operation, deliberately controlled
by ``INQTRIX_RECORD_MODE`` rather than a second pytest marker):

::

    INQTRIX_RECORD_MODE=once \\
      ANTHROPIC_API_KEY=sk-ant-... \\
      uv run pytest tests/replay/test_anthropic_replay.py::test_complete_success_replay -v

VCR will execute one real HTTP call, run the request/response through
the sanitization hooks, and write a YAML cassette to
``tests/fixtures/cassettes/anthropic/test_complete_success_replay.yaml``.
On every subsequent run (default ``INQTRIX_RECORD_MODE`` unset →
``record_mode="none"``) the cassette is replayed offline.
"""

from __future__ import annotations

import logging
import os
import pathlib
import time

import pytest

from tests.fixtures.sanitize import (
    before_record_request,
    before_record_response,
)

log = logging.getLogger(__name__)

# Provider env-vars that, if set on the developer's machine, would let
# providers wander into unintended live calls during replay tests. The
# auto-isolation fixture below removes them with monkeypatch.delenv so
# the change is reverted after each test.
_PROVIDER_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "ANTHROPIC_API_KEY",
    "PERPLEXITY_API_KEY",
    "PPLX_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT_NAME",
    "AZURE_AI_PROJECT_API_KEY",
    "AZURE_AI_PROJECT_ENDPOINT",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "WEB_SEARCH_AGENT_NAME",
    # ServerSettings fields. Not used by any current replay test, but
    # stripped defensively so a future test cannot accidentally pull a
    # real LITELLM_API_KEY from the shell into Settings() /
    # ServerSettings().
    "LITELLM_API_KEY",
    "LITELLM_BASE_URL",
)


def _resolve_record_mode() -> str:
    """Translate ``INQTRIX_RECORD_MODE`` env into a VCR ``record_mode``.

    Accepts the four canonical VCR values (``none``, ``once``,
    ``new_episodes``, ``all``). Anything else falls back to ``none`` so
    a typo cannot accidentally enable live recording in CI. The default
    when the variable is unset is ``none`` — the offline-only mode used
    by every regular ``pytest tests/`` run.
    """
    raw = (os.environ.get("INQTRIX_RECORD_MODE") or "none").strip().lower()
    return raw if raw in {"none", "once", "new_episodes", "all"} else "none"


@pytest.fixture(scope="session")
def vcr_config() -> dict:
    """Pytest-recording session fixture controlling every cassette interaction.

    Returns a dict consumed by pytest-recording. Important keys:

    * ``record_mode``: derived from ``INQTRIX_RECORD_MODE`` env (see
      :func:`_resolve_record_mode`). Default ``"none"``.
    * ``serializer``: YAML — diffable, human-readable, compatible with
      vcrpy 8.x default.
    * ``decode_compressed_response``: ``True`` so gzipped backends
      produce readable cassettes instead of base64 blobs.
    * ``before_record_request`` / ``before_record_response``: import
      from ``tests.fixtures.sanitize`` so every recorded interaction is
      scrubbed before it lands on disk.
    * ``filter_headers``: defence-in-depth for known-secret headers,
      even if the request hook is somehow skipped.
    * ``match_on``: default minus ``body`` — request body matching
      breaks for OpenAI-SDK requests because the SDK injects
      randomised IDs and timestamps into payloads.
    """
    return {
        "record_mode": _resolve_record_mode(),
        "serializer": "yaml",
        "decode_compressed_response": True,
        "before_record_request": before_record_request,
        "before_record_response": before_record_response,
        "filter_headers": [
            "authorization",
            "x-api-key",
            "api-key",
            "x-subscription-token",
            "anthropic-api-key",
            "ocp-apim-subscription-key",
            "cookie",
            "set-cookie",
        ],
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
    }


@pytest.fixture(autouse=True)
def disable_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``time.sleep`` with a no-op for the duration of each replay test.

    Anthropic, Bedrock and the OpenAI SDK all back off with
    exponential delays on transient errors. Multi-interaction cassettes
    (``500 → 500 → 200``) would otherwise wait 1–8 seconds between
    interactions, blowing up the suite runtime for no test value. The
    monkeypatch is reverted automatically at the end of each test, so
    non-replay tests remain unaffected.
    """
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def isolate_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove provider env-vars before every replay test.

    Without this guard, an exported real API key could turn a
    ``record_mode="none"`` test into a live call the moment any cassette
    goes missing. Stripping the vars forces every replay test to rely
    exclusively on its cassette / stub. ``.env`` files never reach the
    session: the root ``conftest.py`` keeps them out of the environment
    and out of the settings classes.
    """
    for name in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


_CASSETTE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "cassettes"


@pytest.fixture(scope="session")
def cassette_root() -> pathlib.Path:
    """Return the absolute path to ``tests/fixtures/cassettes``.

    Tests typically rely on ``vcr_cassette_dir`` (overridden per module
    to a provider sub-folder), but a few tests need the root path
    directly (e.g. the protective scan walks it recursively).
    """
    return _CASSETTE_ROOT
