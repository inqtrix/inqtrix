"""Basic blocking library example using .env or process environment.

Use this example when:
- reasoning and search models are reachable through one LiteLLM- or OpenAI-compatible endpoint
- you want the final structured result object instead of incremental streaming

Typical setup:
- configure LITELLM_BASE_URL, LITELLM_API_KEY, REASONING_MODEL, SEARCH_MODEL
- optionally configure CLASSIFY_MODEL, CLAIM_EXTRACT_MODEL, EVALUATE_MODEL

Run with:
    uv run python examples/quickstart/basic_env.py
"""

from __future__ import annotations

from inqtrix import ResearchAgent


QUESTION = "Was ist der aktuelle Stand der GKV-Reform?"


def main() -> None:
    agent = ResearchAgent()
    result = agent.research(QUESTION)

    print(result.answer)
    print()
    print(f"Confidence: {result.metrics.confidence}/10")
    print(f"Sources: {result.metrics.total_citations}")
    print(f"Rounds: {result.metrics.rounds}")


if __name__ == "__main__":
    main()
