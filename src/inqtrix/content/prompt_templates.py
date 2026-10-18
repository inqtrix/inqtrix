"""Prompt-template records: the server side of the prompt library.

Templates were browser-only state until sharing v1; this module is their
persistence contract. ``owner_user_id`` is the canonical local UUID, while
``None`` is reserved for ownerless records in unscoped deployments.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.shares import ShareFacts

if TYPE_CHECKING:
    from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator

TEMPLATE_CATEGORIES = ("instruction", "function", "context")


class PromptTemplateNotFound(KeyError):
    """Raised when a template id is unknown (or hidden — same signal)."""


class PromptTemplateConflict(RuntimeError):
    """Raised when a mandatory integer revision is stale."""

    def __init__(self, template_id: str, current_revision: int) -> None:
        self.template_id = template_id
        self.current_revision = current_revision
        super().__init__(
            f"prompt template {template_id} is at revision {current_revision}"
        )


@dataclass(frozen=True)
class PromptTemplateRecord:
    """One stored prompt template.

    Attributes:
        id: Server-assigned stable identifier (``pt_...``).
        tenant_id: Tenant scope (v1 runs one tenant per deployment).
        owner_user_id: Canonical creating user UUID; ``None`` means an
            ownerless record in an unscoped deployment.
        title: Display title in the prompt library.
        label: The ``@``-mention label used in the composer.
        category: One of :data:`TEMPLATE_CATEGORIES` or ``None``
            (uncategorized legacy rules).
        content_markdown: The template body.
        visibility: Surface flags, e.g. ``{"chat": true, "editor":
            false}`` — stored verbatim, interpreted by the client.
        include_in_autocomplete: Whether the ``@``-menu offers it.
        created_at: Unix timestamp of creation.
        revision: Monotonic compare-and-swap version, starting at one.
        updated_at: Unix timestamp of the last write, used for display only.
    """

    id: str
    tenant_id: str
    owner_user_id: uuid.UUID | None
    title: str
    label: str
    category: str | None
    content_markdown: str
    visibility: dict[str, Any] = field(default_factory=dict)
    include_in_autocomplete: bool = True
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


@runtime_checkable
class PromptTemplateRepository(Protocol):
    """Persistence port for prompt templates (memory + Postgres)."""

    async def create(self, record: PromptTemplateRecord) -> PromptTemplateRecord: ...

    async def get(
        self, template_id: str, *, tenant_id: str
    ) -> PromptTemplateRecord: ...

    async def list_for_tenant(
        self, *, tenant_id: str
    ) -> list[PromptTemplateRecord]: ...

    async def share_facts(
        self, template_ids: Sequence[str], *, tenant_id: str
    ) -> dict[str, ShareFacts]:
        """Owner and title of each listed template of the tenant, in one
        read and regardless of visibility (share layer); others are absent."""
        ...

    async def update(
        self,
        record: PromptTemplateRecord,
        *,
        expected_revision: int,
        actor_user_id: uuid.UUID | None,
    ) -> PromptTemplateRecord: ...

    async def delete(
        self,
        template_id: str,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None: ...

    async def seed_default_templates(
        self,
        records: Sequence[PromptTemplateRecord],
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> bool: ...
    """Claim the per-user seed marker and, when the user owns no
    template yet, insert *records* — both in ONE atomic step. Returns
    whether records were inserted. The marker is claimed either way, so
    seeding runs at most once per user forever (a deleted default stays
    deleted; an already-grown library is never injected into)."""


def new_template_id() -> str:
    """Mint one ``pt_``-prefixed identifier."""
    return f"pt_{uuid.uuid4().hex[:20]}"


class MemoryPromptTemplateRepository:
    """Thread-safe in-process implementation (zero-infrastructure default)."""

    def __init__(self) -> None:
        self._records: dict[str, PromptTemplateRecord] = {}
        # Volatile like the records themselves: after a restart the
        # defaults reseed together with the store being empty again —
        # consistent memory-backend semantics (nothing here is durable).
        self._seed_markers: set[tuple[str, uuid.UUID]] = set()
        self._lock = threading.RLock()
        self._authority: MemoryAuthorityCoordinator | None = None

    @property
    def atomic_resource_effects(self) -> bool:
        """Whether writes include audit and invalidations under one lock."""
        return self._authority is not None

    def bind_authority_coordinator(
        self, coordinator: "MemoryAuthorityCoordinator"
    ) -> None:
        """Join the process-wide memory authority and register ownership."""
        self._authority = coordinator
        self._lock = coordinator.lock
        coordinator.register_resource(
            "prompt_template", self._resource_snapshot
        )

    def _resource_snapshot(self, tenant_id: str, template_id: str):
        """Return existence and owner while the shared lock is held."""
        from inqtrix.auth.memory_authority import MemoryResourceSnapshot

        record = self._records.get(template_id)
        return MemoryResourceSnapshot(
            exists=record is not None and record.tenant_id == tenant_id,
            owner_user_id=(
                record.owner_user_id
                if record is not None and record.tenant_id == tenant_id
                else None
            ),
        )

    @contextmanager
    def _mutation_guard(
        self,
        record: PromptTemplateRecord,
        *,
        actor_user_id: uuid.UUID | None,
        owner_only: bool = False,
    ) -> Iterator[None]:
        """Hold final live authority across one repository mutation."""
        if self._authority is None:
            yield
            return
        from inqtrix.execution_authority import AuthorizationRevoked

        try:
            with self._authority.resource_access_guard(
                tenant_id=record.tenant_id,
                owner_user_id=record.owner_user_id,
                actor_user_id=actor_user_id,
                resource_type="prompt_template",
                resource_id=record.id,
                minimum=SharePermission.EDIT,
                owner_only=owner_only,
            ):
                yield
        except AuthorizationRevoked as exc:
            raise PromptTemplateNotFound(record.id) from exc

    async def create(self, record: PromptTemplateRecord) -> PromptTemplateRecord:
        with self._lock:
            guard = (
                self._authority.creation_guard(
                    tenant_id=record.tenant_id,
                    actor_user_id=record.owner_user_id,
                )
                if self._authority is not None
                else nullcontext()
            )
            with guard:
                self._records[record.id] = record
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=record.tenant_id,
                        actor_user_id=record.owner_user_id,
                        owner_user_id=record.owner_user_id,
                        action="prompt_template.created",
                        resource_type="prompt_template",
                        resource_id=record.id,
                        scope="prompt_templates",
                    )
                return record

    async def get(
        self, template_id: str, *, tenant_id: str
    ) -> PromptTemplateRecord:
        with self._lock:
            record = self._records.get(template_id)
        if record is None or record.tenant_id != tenant_id:
            raise PromptTemplateNotFound(template_id)
        return record

    async def list_for_tenant(
        self, *, tenant_id: str
    ) -> list[PromptTemplateRecord]:
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
            ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def share_facts(
        self, template_ids: Sequence[str], *, tenant_id: str
    ) -> dict[str, ShareFacts]:
        with self._lock:
            return {
                template_id: ShareFacts(
                    owner_user_id=record.owner_user_id, title=record.title
                )
                for template_id in template_ids
                if (record := self._records.get(template_id)) is not None
                and record.tenant_id == tenant_id
            }

    async def update(
        self,
        record: PromptTemplateRecord,
        *,
        expected_revision: int,
        actor_user_id: uuid.UUID | None,
    ) -> PromptTemplateRecord:
        with self._lock:
            current = self._records.get(record.id)
            if current is None or current.tenant_id != record.tenant_id:
                raise PromptTemplateNotFound(record.id)
            with self._mutation_guard(
                current, actor_user_id=actor_user_id
            ):
                if current.revision != expected_revision:
                    raise PromptTemplateConflict(record.id, current.revision)
                stored = replace(
                    record,
                    tenant_id=current.tenant_id,
                    owner_user_id=current.owner_user_id,
                    revision=current.revision + 1,
                    updated_at=time.time(),
                )
                self._records[record.id] = stored
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=stored.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=stored.owner_user_id,
                        action="prompt_template.updated",
                        resource_type="prompt_template",
                        resource_id=stored.id,
                        scope="prompt_templates",
                    )
                return stored

    async def seed_default_templates(
        self,
        records: Sequence[PromptTemplateRecord],
        *,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> bool:
        with self._lock:
            key = (tenant_id, user_id)
            if key in self._seed_markers:
                return False
            self._seed_markers.add(key)
            owns_any = any(
                record.tenant_id == tenant_id
                and record.owner_user_id == user_id
                for record in self._records.values()
            )
            if owns_any:
                return False
            for record in records:
                self._records[record.id] = record
            return True

    async def delete(
        self,
        template_id: str,
        *,
        tenant_id: str,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        with self._lock:
            record = self._records.get(template_id)
            if record is None or record.tenant_id != tenant_id:
                raise PromptTemplateNotFound(template_id)
            with self._mutation_guard(
                record, actor_user_id=actor_user_id, owner_only=True
            ):
                if record.owner_user_id != actor_user_id:
                    raise PromptTemplateNotFound(template_id)
                if self._authority is not None:
                    self._authority.revoke_deleted_resource(
                        tenant_id=record.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=record.owner_user_id,
                        action="prompt_template.deleted",
                        resource_type="prompt_template",
                        resource_id=record.id,
                        scope="prompt_templates",
                    )
                del self._records[template_id]
