""" Robust JSON parsing from LLM output."""

from __future__ import annotations

import json
import re
from typing import Any

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_json_string_list(
    text: str,
    *,
    fallback: list[str],
    max_items: int | None = None,
) -> list[str]:
    """Parse a JSON array of strings from model output.

    Robust against code fences and minor format deviations.
    """
    items, _status = parse_json_string_list_with_status(
        text,
        fallback=fallback,
        max_items=max_items,
    )
    return items


def parse_json_string_list_with_status(
    text: str,
    *,
    fallback: list[str],
    max_items: int | None = None,
) -> tuple[list[str], str]:
    """Parse a JSON string list and report whether the fallback was used.

    Args:
        text: Raw model output that should contain a JSON array of
            strings, optionally wrapped in a fenced code block.
        fallback: Values returned when parsing fails or yields an empty
            list. Truncated by ``max_items`` like successful parses.
        max_items: Optional upper bound on returned items.

    Returns:
        Tuple ``(items, status)``. ``status == "parsed"`` means the
        returned items came from valid JSON. Other status values mean
        the returned items came from ``fallback`` and name the visible
        reason for operator diagnostics.
    """
    cleaned = (text or "").strip()
    m = _CODE_FENCE_RE.search(cleaned)
    if m:
        cleaned = m.group(1).strip()

    def _try_load(candidate: str) -> list[str] | None:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        if not isinstance(obj, list) or not obj:
            return None
        items = [str(x).strip() for x in obj if x]
        if not items:
            return None
        if max_items is not None:
            items = items[:max_items]
        return items

    loaded = _try_load(cleaned)
    if loaded is not None:
        return loaded, "parsed"

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        loaded = _try_load(cleaned[start: end + 1])
        if loaded is not None:
            return loaded, "parsed"

    status = "fallback_empty_output" if not cleaned else "fallback_invalid_json_list"
    return fallback[: (max_items or len(fallback))], status


def parse_json_object(
    text: str,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse a JSON object from model output (robust against code fences)."""
    fb = dict(fallback or {})
    cleaned = (text or "").strip()
    m = _CODE_FENCE_RE.search(cleaned)
    if m:
        cleaned = m.group(1).strip()

    def _try_load(candidate: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        if isinstance(obj, dict):
            return obj
        return None

    loaded = _try_load(cleaned)
    if loaded is not None:
        return loaded

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        loaded = _try_load(cleaned[start: end + 1])
        if loaded is not None:
            return loaded

    return fb
