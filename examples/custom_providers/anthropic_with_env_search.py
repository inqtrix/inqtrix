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
# File-based logging with automatic secret redaction.  Disabled by
# default — enable via environment variables to keep terminal clean:
#
#   INQTRIX_LOG_ENABLED=true   — write logs to logs/inqtrix_*.log
#   INQTRIX_LOG_LEVEL=DEBUG    — DEBUG / INFO / WARNING (default: INFO)
#   INQTRIX_LOG_CONSOLE=true   — also print WARNING+ to stderr
#   OBSERVABILITY_PROFILE=forensic — lineage EVENT JSON in the same log file; requires
#     INQTRIX_LOG_LEVEL=DEBUG. docs/observability/logging.md,
#     docs/observability/forensic-cookbook.md
from inqtrix.logging_config import configure_logging

_log_path = configure_logging(
    enabled=os.getenv("INQTRIX_LOG_ENABLED", "").lower() == "true",
    level=os.getenv("INQTRIX_LOG_LEVEL", "INFO"),
    console=os.getenv("INQTRIX_LOG_CONSOLE", "").lower() == "true",
)
if _log_path:
    print(f"Logging to {_log_path}")


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
