"""How parallel real runs are, read from the span timestamps of run trace exports.

The load stack cannot answer this: the stand-in's delays are drawn, and the run
events of a turn or a research round are written together, whether their work ran
side by side or one after another. A span carries the real start and end of the
tool call or search, so these numbers come from traced runs against real providers.

    python tests/load/run-load/trace_timing.py <directory of exports>

An export is the JSON of GET /v1/admin/runs/{run_id}/trace/export. The output holds
counts and durations only.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import statistics
import sys
from collections import defaultdict
from typing import Any, Iterable


def _seconds(stamp: Any) -> float | None:
    if not stamp:
        return None
    return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()


def _interval(observation: dict) -> tuple[float, float] | None:
    start, end = _seconds(observation.get("startTime")), _seconds(observation.get("endTime"))
    return None if start is None or end is None else (start, end)


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))], 3)


def _peak(intervals: Iterable[tuple[float, float]]) -> int:
    """Most intervals open at one moment; one that ends as another starts does not count."""
    edges = sorted((point, change) for start, end in intervals for point, change in ((start, 1), (end, -1)))
    peak = current = 0
    for _, change in edges:
        current += change
        peak = max(peak, current)
    return peak


def _covered(intervals: Iterable[tuple[float, float]]) -> float:
    """The time at least one interval was open."""
    covered = 0.0
    reach = float("-inf")
    for start, end in sorted(intervals):
        covered += max(0.0, end - max(start, reach))
        reach = max(reach, end)
    return covered


def tool_turns(observations: list[dict]) -> list[list[tuple[float, float]]]:
    """Group the tool spans of each run by the model turn that asked for them."""
    by_parent: dict[Any, list[dict]] = defaultdict(list)
    for observation in observations:
        if observation.get("type") in ("TOOL", "GENERATION") and _interval(observation):
            by_parent[observation.get("parentObservationId")].append(observation)
    turns: list[list[tuple[float, float]]] = []
    for siblings in by_parent.values():
        current: list[tuple[float, float]] = []
        for observation in sorted(siblings, key=lambda row: _interval(row)[0]):
            if observation["type"] == "GENERATION":
                if current:
                    turns.append(current)
                current = []
            else:
                current.append(_interval(observation))
        if current:
            turns.append(current)
    return turns


def tool_overlap(turns: list[list[tuple[float, float]]]) -> dict[str, Any]:
    """Whether the tools of one turn ran side by side.

    Busy time against the time any of them ran: 1 means one after another. Turns
    with a single tool cannot show it and are only counted.
    """
    several = [turn for turn in turns if len(turn) > 1]
    busy = sum(end - start for turn in several for start, end in turn)
    covered = sum(_covered(turn) for turn in several)
    return {
        "turns": len(turns),
        "turns_with_several_tools": len(several),
        "turns_that_overlapped": sum(1 for turn in several if _peak(turn) > 1),
        "tools_in_those_turns": sum(len(turn) for turn in several),
        "busy_seconds": round(busy, 2),
        "covered_seconds": round(covered, 2),
        "parallelism": round(busy / covered, 3) if covered > 0 else None,
        "peak_at_once": max((_peak(turn) for turn in several), default=0),
    }


def research_rounds(observations: list[dict]) -> dict[str, Any]:
    """Each research round against its slowest search; 1 means side by side."""
    rounds: dict[Any, list[tuple[float, float]]] = defaultdict(list)
    for observation in observations:
        if observation.get("name") == "web_search" and _interval(observation):
            rounds[observation.get("parentObservationId")].append(_interval(observation))
    walls = [max(end for _, end in row) - min(start for start, _ in row) for row in rounds.values()]
    ratios = [
        (max(end for _, end in row) - min(start for start, _ in row)) / slowest
        for row in rounds.values()
        if (slowest := max(end - start for start, end in row)) > 0
    ]
    return {
        "rounds": len(rounds),
        "searches": sum(len(row) for row in rounds.values()),
        "searches_per_round_median": statistics.median([len(row) for row in rounds.values()]) if rounds else None,
        "wall_seconds_p50": _quantile(walls, 0.5),
        "wall_seconds_p95": _quantile(walls, 0.95),
        "wall_to_slowest_search_p50": _quantile(ratios, 0.5),
        "wall_to_slowest_search_p95": _quantile(ratios, 0.95),
    }


def knowledge_runs(observations: list[dict]) -> dict[str, Any]:
    """The retrieval of each knowledge run by how many sub-questions it asked."""
    retrievals: dict[Any, list[tuple[float, float]]] = defaultdict(list)
    for observation in observations:
        if observation.get("name") == "knowledge.retrieve" and _interval(observation):
            retrievals[observation.get("parentObservationId")].append(_interval(observation))
    grouped: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for row in retrievals.values():
        grouped[len(row)].append((max(end for _, end in row) - min(start for start, _ in row), sum(end - start for start, end in row)))
    return {
        str(count): {
            "runs": len(rows),
            "retrieval_wall_seconds_p50": _quantile([wall for wall, _ in rows], 0.5),
            "retrieval_busy_seconds_p50": _quantile([busy for _, busy in rows], 0.5),
        }
        for count, rows in sorted(grouped.items())
    }


def summarize(documents: Iterable[dict]) -> dict[str, Any]:
    observations: list[dict] = []
    turns: list[list[tuple[float, float]]] = []
    traces = 0
    for document in documents:
        spans = (document.get("payload") or {}).get("observations") or []
        if spans:
            traces += 1
        # Identifiers are unique per trace, so each export is grouped on its own.
        turns.extend(tool_turns(spans))
        observations.extend(spans)
    return {
        "traces": traces,
        "tools": tool_overlap(turns),
        "research": research_rounds(observations),
        "knowledge": knowledge_runs(observations),
    }


def main() -> None:
    directory = pathlib.Path(sys.argv[1])
    documents = (json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json")))
    print(json.dumps(summarize(documents), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
