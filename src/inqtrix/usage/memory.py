"""In-memory usage-ledger store (memory deployments + tests).

Same lifecycle as the memory quota store: rows live for the process
lifetime, aggregation mirrors the Postgres reader so the future usage
UI behaves identically across backends.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict

from inqtrix.usage.grouping import (
    USAGE_GROUP_DEFAULT,
    normalize_usage_group_by,
)
from typing import Sequence

from inqtrix.usage.models import UsageRow


class MemoryUsageStore:
    """Process-local ledger twin of :class:`PostgresUsageStore`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[UsageRow] = []

    async def insert_rows(self, rows: Sequence[UsageRow]) -> int:
        with self._lock:
            self._rows.extend(rows)
        return len(rows)

    async def prune(self, *, days: int) -> int:
        cutoff = time.time() - days * 86400.0
        with self._lock:
            before = len(self._rows)
            self._rows = [r for r in self._rows if r.created_at >= cutoff]
            return before - len(self._rows)

    async def aggregate(
        self,
        *,
        tenant_id: str,
        group_by: tuple[str, ...] = USAGE_GROUP_DEFAULT,
        since: float | None = None,
        until: float | None = None,
        run_id: str | None = None,
        user_id: "uuid.UUID | None" = None,
    ) -> list[dict]:
        """Sum tokens/requests per group key.

        ``group_by`` is a tuple against the whitelist
        ``user_id | model | feature | operation | run_id``. The default pairs
        model with operation because pricing needs exactly that pair: the
        price catalogue is chosen by operation, the rate by model.

        ``run_id``/``user_id`` narrow the set to one run or one person —
        answering "what did this cost" without a second reader.
        """
        keys = normalize_usage_group_by(group_by)
        totals: dict[tuple[str, ...], dict[str, int]] = defaultdict(
            lambda: {"input_tokens": 0, "output_tokens": 0, "request_count": 0}
        )
        with self._lock:
            for row in self._rows:
                if row.tenant_id != tenant_id:
                    continue
                if since is not None and row.created_at < since:
                    continue
                if until is not None and row.created_at >= until:
                    continue
                if run_id is not None and row.run_id != run_id:
                    continue
                if user_id is not None and row.user_id != user_id:
                    continue
                key = tuple(
                    "" if getattr(row, name) is None else str(getattr(row, name))
                    for name in keys
                )
                bucket = totals[key]
                bucket["input_tokens"] += row.input_tokens
                bucket["output_tokens"] += row.output_tokens
                bucket["request_count"] += row.request_count
        return [
            {**dict(zip(keys, key)), **values}
            for key, values in sorted(totals.items())
        ]

    async def aclose(self) -> None:
        return None

    # Test helper — deliberately sync and read-only.
    def rows_snapshot(self) -> list[UsageRow]:
        with self._lock:
            return list(self._rows)
