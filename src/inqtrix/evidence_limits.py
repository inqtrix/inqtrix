"""Shared persistence ceilings for provider-grounded web evidence.

This module is a dependency-free leaf so provider normalization, evidence
ledgers, prompts, and audit artifacts enforce the same byte contract without
import cycles or parallel constants.
"""

from __future__ import annotations


OBSERVATION_TEXT_BYTES_LIMIT = 64 * 1024


def bounded_utf8_prefix(text: str, *, max_bytes: int) -> tuple[str, int]:
    """Return a valid UTF-8 prefix and the exact number of omitted bytes."""

    value = str(text)
    encoded = value.encode("utf-8")
    ceiling = max(0, int(max_bytes))
    if len(encoded) <= ceiling:
        return value, 0
    prefix = encoded[:ceiling].decode("utf-8", errors="ignore")
    retained = len(prefix.encode("utf-8"))
    return prefix, len(encoded) - retained
