"""Contracts of the agent-session store (Agent-Desk saved sessions).

Mirrors :mod:`inqtrix.project.asset_records_ports`: the store owns persistence
only; scoping lives in
:class:`~inqtrix.services.agent_sessions_service.AgentSessionsService`
and the wire shape in the router. Two implementations behind one port:
:class:`~inqtrix.project.agent_sessions_memory.MemoryAgentSessionStore`
(offline/test) and
:class:`~inqtrix.project.agent_sessions_postgres.PostgresAgentSessionStore`.

A session carries a small ``items_json`` settings body (source policy,
knowledge switch, model pick); ``list_sessions`` and ``get_session`` both return
it, so a client compares its sessions against the real rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.project.session_titles import TitleSource


class AgentSessionNotFound(KeyError):
    """Raised when a session id is unknown to the store (HTTP 404)."""


class AgentSessionGroupNotFound(KeyError):
    """Raised when a session group id is unknown to the store (HTTP 404)."""


@dataclass(frozen=True)
class AgentSessionGroup:
    """One grouping of a user's agent sessions.

    Attributes:
        id: Client-supplied id (``agent-session-group-...``), the primary
            key.
        title: User-facing folder label in the Agent Desk history panel.
        created_at/updated_at: Unix timestamps.
        tenant_id/created_by_user_id/workspace_id: The owner scope.
    """

    id: str
    title: str
    created_at: float
    updated_at: float
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None


@dataclass(frozen=True)
class AgentSession:
    """One saved agent-desk session: a titled assignment timeline.

    Attributes:
        id: Client-supplied id (``as_...``), the primary key.
        title: Session label shown in the history panel.
        group_id: Owning :class:`AgentSessionGroup` id, or ``None`` when
            ungrouped.
        items_json: The ordered timeline items serialized as a JSON array
            (question + the rendered answer record). ``"[]"`` on list rows
            (load-on-open); the full array on get.
        created_at/updated_at: Unix timestamps.
        tenant_id/created_by_user_id/workspace_id: The owner scope.
    """

    id: str
    title: str
    created_at: float
    updated_at: float
    group_id: str | None = None
    items_json: str = field(repr=False, default="[]")
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None
    lifecycle_status: str = "active"
    deletion_operation_id: str | None = None
    deletion_stage: str | None = None
    deletion_error: str | None = None
    title_revision: int = 0
    title_source: TitleSource = "manual"


@runtime_checkable
class AgentSessionStore(Protocol):
    """Persistence port for agent sessions."""

    async def claim_session(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        title_source: TitleSource = "manual",
    ) -> AgentSession:
        """Insert a session when absent and otherwise return it unchanged.

        The operation is atomic at the store boundary.  In particular, a
        losing concurrent claimant must never rewrite ownership, title, or
        the saved timeline of the row that won.
        """
        ...

    async def upsert_session(
        self,
        *,
        id: str,
        title: str,
        items_json: str,
        group_id: str | None,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        title_revision: int | None = None,
        title_source: TitleSource = "manual",
    ) -> AgentSession: ...

    async def apply_generated_title(
        self, session_id: str, *, title: str, revision: int, scope: ResourceScope
    ) -> bool:
        """Replace an active initial title at its revision, without creating."""
        ...

    async def list_sessions(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSession]:
        """Sessions for the scope (newest-updated first), each with its
        ``items_json`` settings body."""
        ...

    async def get_session(self, session_id: str) -> AgentSession:
        """One session with its ``items_json``, or
        :class:`AgentSessionNotFound`."""
        ...

    async def delete_session(
        self, session_id: str, *, scope: ResourceScope
    ) -> None: ...

    async def set_session_deletion_state(
        self,
        session_id: str,
        *,
        scope: ResourceScope,
        lifecycle_status: str,
        deletion_operation_id: str,
        deletion_stage: str,
        deletion_error: str | None,
    ) -> None: ...

    async def count_session_residuals(
        self, session_id: str, *, scope: ResourceScope
    ) -> int: ...

    async def upsert_group(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> AgentSessionGroup: ...

    async def claim_group(
        self,
        *,
        id: str,
        title: str,
        created_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> AgentSessionGroup:
        """Insert a group when absent and otherwise return it unchanged."""
        ...

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AgentSessionGroup]:
        """Groups for the scope, newest-created first."""
        ...

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        """Delete a group; its sessions orphan to ungrouped."""
        ...

    async def aclose(self) -> None: ...
