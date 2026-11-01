"""Use Anthropic directly for LLM calls while keeping search on env-based setup.

Architecture
------------
This example demonstrates the **partial override** pattern:

- **LLM** — ``AnthropicLLM`` with a direct Anthropic API key.
  Handles all reasoning roles (classify, plan, evaluate, answer)
  and claim extraction.

- **Search** — omitted (``None``).  The agent auto-creates the
  search provider from environment variables / ``.env`` on first
  ``.research()`` call, typically ``PerplexitySearch`` through a
  LiteLLM proxy.

This is useful when only the LLM side needs to change (e.g. for cost
or latency reasons), while the search backend stays on the existing
env-based setup.

Use this example when:
- you want to swap only the LLM provider without touching search
- your search backend should still come from the normal env-based provider path

Required environment variables (in .env or process env):
- ANTHROPIC_API_KEY
- the normal env-based search variables (LITELLM_BASE_URL, LITELLM_API_KEY, SEARCH_MODEL)

Run with::

    uv run python examples/custom_providers/anthropic_with_env_search.py
"""

from __future__ import annotations
import os
from dotenv import load_dotenv
from inqtrix import AgentConfig, AnthropicLLM, ResearchAgent


QUESTION = "Was ist der aktuelle Stand der GKV-Reform?"


load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────
# Logging with automatic secret redaction. Off by default so the
# terminal stays clean — enable via environment variables:
#
#   INQTRIX_LOG_TARGET=file    — console, file (logs/inqtrix-script.log) or both
#   INQTRIX_LOG_LEVEL=DEBUG    — DEBUG / INFO / WARNING (default: INFO)
#   INQTRIX_LOG_FORMAT=text    — json (default) or text
#   See docs/observability/logging.md.
from inqtrix.logging_config import configure_logging

# Examples keep the terminal for their own output: logs appear only when
# INQTRIX_LOG_TARGET is set (console, file or both).
_logging = configure_logging(
    service="script", target=os.environ.get("INQTRIX_LOG_TARGET") or "none"
)
if _logging.file_path:
    print(f"Logging to {_logging.file_path}")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    agent = ResearchAgent(AgentConfig(
        llm=AnthropicLLM(
            api_key=_require_env("ANTHROPIC_API_KEY"),
            # Strong model for reasoning, planning, and final answer.
            default_model="claude-sonnet-4-6",
            # Optional per-role overrides; leave unset to fall back to
            # default_model.
            # classify_model="claude-haiku-4-5",
            claim_extract_model="claude-haiku-4-5",
            # evaluate_model="claude-sonnet-4-6",
            # thinking={"type": "adaptive"},
        ),
    ))
    result = agent.research(QUESTION)

    print(result.answer)
    print()
    print(f"Confidence: {result.metrics.confidence}/10")
    print(f"Sources: {result.metrics.total_citations}")
    print(f"Rounds: {result.metrics.rounds}")


if __name__ == "__main__":
    main()
