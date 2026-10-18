"""Shared session scaffolding for the tenant-scoped project stores.

Every Postgres project-persistence store (chat, editor, and the asset
/ vector-index / account-preferences stores) opens its sessions the same
way: a dedicated NullPool engine, a session factory bound to it, and a
:func:`~inqtrix.storage.db.tenant_session` (restricted role +
transaction-local tenant GUC, the RLS layering). Defined once here
so the boilerplate cannot drift between stores; the
entity-specific schema, queries, and upserts stay in each subclass.

The session lifecycle is identical regardless of a store's row shape: the
account-preferences store is a single-row, user-keyed table (no keyset
list) yet still subclasses this — the keyset shape only affects its
queries, never how it opens a tenant session.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from inqtrix.storage.db import build_session_factory, tenant_session

DEFAULT_TENANT = "default"


class BaseSessionStore:
    """Engine + tenant-scoped session lifecycle shared by project stores.

    Args:
        engine: A dedicated NullPool async engine (loop-agnostic) for the
            store's schema — never the shared HTTP-loop engine.
        app_role: Restricted Postgres role for the tenant sessions.
    """

    def __init__(self, *, engine: AsyncEngine, app_role: str) -> None:
        self._engine = engine
        self._session_factory = build_session_factory(engine)
        self._app_role = app_role

    def _session(self):
        """One tenant-scoped transaction (restricted role + tenant GUC)."""
        return tenant_session(
            self._session_factory,
            tenant_id=DEFAULT_TENANT,
            app_role=self._app_role,
        )

    async def aclose(self) -> None:
        """Dispose the dedicated engine at application shutdown."""
        await self._engine.dispose()
