"""Postgres usage-ledger store (NullPool — sync-thread flusher safe).

Every operation runs in one tenant-scoped transaction (``tenant_session``
sets the RLS GUC); batches are grouped per tenant before insert because
one transaction can carry exactly one tenant scope. Retention goes
through ``llm_usage_prune`` — the SECURITY DEFINER door through the
INSERT/SELECT-only grant wall (audit_log precedent, same owner-mode
tenant-scope caveat).
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Sequence

import sqlalchemy as sa

from inqtrix.storage.db import build_engine, build_session_factory, tenant_session
from inqtrix.storage.usage_orm import llm_usage
from inqtrix.usage.grouping import (
    USAGE_GROUP_DEFAULT,
    normalize_usage_group_by,
)
from inqtrix.usage.models import UsageRow


class PostgresUsageStore:
    """Durable ledger twin of :class:`MemoryUsageStore`."""

    def __init__(self, *, database_url: str, app_role: str) -> None:
        self._engine = build_engine(database_url, null_pool=True)
        self._session_factory = build_session_factory(self._engine)
        self._app_role = app_role

    async def insert_rows(self, rows: Sequence[UsageRow]) -> int:
        by_tenant: dict[str, list[UsageRow]] = defaultdict(list)
        for row in rows:
            by_tenant[row.tenant_id].append(row)
        written = 0
        for tenant_id, tenant_rows in by_tenant.items():
            async with tenant_session(
                self._session_factory,
                tenant_id=tenant_id,
                app_role=self._app_role,
            ) as session:
                await session.execute(
                    sa.insert(llm_usage),
                    [
                        {
                            "tenant_id": row.tenant_id,
                            "user_id": row.user_id,
                            "workspace_id": row.workspace_id,
                            "run_id": row.run_id,
                            "feature": row.feature,
                            "operation": row.operation,
                            "model": row.model,
                            "input_tokens": row.input_tokens,
                            "output_tokens": row.output_tokens,
                            "request_count": row.request_count,
                            "duration_ms": row.duration_ms,
                            "outcome": row.outcome,
                            "created_at": row.created_at,
                        }
                        for row in tenant_rows
                    ],
                )
            written += len(tenant_rows)
        return written

    async def prune(self, *, days: int) -> int:
        """Delete rows older than *days* via the SECURITY DEFINER door.

        Retention is an instance-level policy; the session runs under
        the ``default`` tenant exactly like ``prune_audit_log`` — with
        an RLS-exempt function owner the prune is cross-tenant, under
        owner-mode RLS it covers only this tenant (equivalent in the
        current single-tenant deployments; the audit_prune caveat
        applies verbatim).
        """
        cutoff = time.time() - int(days) * 86400.0
        async with tenant_session(
            self._session_factory,
            tenant_id="default",
            app_role=self._app_role,
        ) as session:
            result = await session.execute(
                sa.text("SELECT llm_usage_prune(:cutoff)"),
                {"cutoff": cutoff},
            )
            return int(result.scalar_one())

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
        columns = [llm_usage.c[key] for key in keys]
        stmt = (
            sa.select(
                *columns,
                sa.func.sum(llm_usage.c.input_tokens).label("input_tokens"),
                sa.func.sum(llm_usage.c.output_tokens).label("output_tokens"),
                sa.func.sum(llm_usage.c.request_count).label("request_count"),
            )
            .where(llm_usage.c.tenant_id == tenant_id)
            .group_by(*columns)
            .order_by(*columns)
        )
        if since is not None:
            stmt = stmt.where(llm_usage.c.created_at >= since)
        if until is not None:
            stmt = stmt.where(llm_usage.c.created_at < until)
        if run_id is not None:
            stmt = stmt.where(llm_usage.c.run_id == run_id)
        if user_id is not None:
            stmt = stmt.where(llm_usage.c.user_id == user_id)
        async with tenant_session(
            self._session_factory,
            tenant_id=tenant_id,
            app_role=self._app_role,
        ) as session:
            result = await session.execute(stmt)
            return [
                {
                    **{
                        key: ("" if row[i] is None else str(row[i]))
                        for i, key in enumerate(keys)
                    },
                    "input_tokens": int(row.input_tokens or 0),
                    "output_tokens": int(row.output_tokens or 0),
                    "request_count": int(row.request_count or 0),
                }
                for row in result.all()
            ]

    async def aclose(self) -> None:
        await self._engine.dispose()
