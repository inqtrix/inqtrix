"""Streaming library example using .env or process environment.

Use this example when:
- you want terminal-friendly live progress updates while the research loop runs
- the LLM and search side are still served by the standard env-based provider setup

Set INCLUDE_PROGRESS to False if another program should consume only answer chunks.

Run with:
    uv run python examples/quickstart/streaming.py
"""

from __future__ import annotations

from inqtrix import ResearchAgent


QUESTION = "Was ist der aktuelle Stand der GKV-Reform?"
INCLUDE_PROGRESS = True


def main() -> None:
    agent = ResearchAgent()
    for chunk in agent.stream(QUESTION, include_progress=INCLUDE_PROGRESS):
        print(chunk, end="", flush=True)


if __name__ == "__main__":
    main()
