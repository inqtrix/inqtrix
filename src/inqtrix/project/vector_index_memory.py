"""In-memory vector-index-record store (tier without Postgres + offline test).

Mirrors :class:`~inqtrix.project.vector_index_postgres.PostgresVectorIndexStore`
byte-for-byte (filter before slice; reuse keyset_page; members + history
replaced wholesale on upsert). Process-local, not durable.
"""

from __future__ import annotations

import uuid
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from inqtrix.pagination import keyset_page
from inqtrix.project.vector_index_ports import (
    VectorIndexMemberUnavailable,
    VectorIndexNotFound,
    VectorIndexRecord,
)
from inqtrix.project.scoped_upsert import ResourceScope, require_memory_scope
from inqtrix.source_authority import SourceLifecycleConflict, SourceScope

if TYPE_CHECKING:
    from inqtrix.source_authority import MemorySourceLifecycleAuthority


class MemoryVectorIndexStore:
    """Process-local :class:`~inqtrix.project.vector_index_ports.VectorIndexStore`."""

    def __init__(self) -> None:
        self._indexes: dict[str, VectorIndexRecord] = {}
        self._source_authority: MemorySourceLifecycleAuthority | None = None

    def bind_source_lifecycle_authority(
        self, authority: "MemorySourceLifecycleAuthority"
    ) -> None:
        """Use the same lifecycle authority as assets and Knowledge."""

        self._source_authority = authority

    async def upsert_index(
        self, *, id, title, handle, model, dims, status, server_collection_id,
        server_collection_model, last_error, members, history, created_at,
        updated_at, created_by_user_id: uuid.UUID | None, workspace_id,
    ) -> VectorIndexRecord:
        members = tuple(members)
        if self._source_authority is not None:
            for asset_id in sorted({member.file_id for member in members}):
                scope = SourceScope(
                    tenant_id="default",
                    source_id=f"asset:{asset_id}",
                    owner_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                )
                try:
                    with self._source_authority.active_write(
                        scope,
                        create_if_missing=False,
                    ):
                        pass
                except SourceLifecycleConflict as exc:
                    raise VectorIndexMemberUnavailable(asset_id) from exc
        existing = self._indexes.get(id)
        mutable = dict(
            title=title, handle=handle, model=model, dims=dims, status=status,
            server_collection_id=server_collection_id,
            server_collection_model=server_collection_model, last_error=last_error,
            members=tuple(members), history=tuple(history), updated_at=updated_at,
        )
        if existing is not None:
            require_memory_scope(
                existing,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=id,
                not_found=VectorIndexNotFound,
            )
            record = replace(existing, **mutable)
        else:
            record = VectorIndexRecord(
                id=id, created_at=created_at, created_by_user_id=created_by_user_id,
                workspace_id=workspace_id, **mutable,
            )
        self._indexes[id] = record
        return record

    async def list_indexes_page(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id, limit, after
    ) -> tuple[list[VectorIndexRecord], str | None]:
        items = _scoped(self._indexes.values(), created_by_user_id, workspace_id)
        items.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return keyset_page(
            items, limit=limit, after=after,
            created_at_of=lambda r: r.created_at, id_of=lambda r: r.id,
        )

    async def get_index(self, index_id: str) -> VectorIndexRecord:
        try:
            return self._indexes[index_id]
        except KeyError as exc:
            raise VectorIndexNotFound(index_id) from exc

    async def delete_index(
        self, index_id: str, *, scope: ResourceScope
    ) -> None:
        require_memory_scope(
            self._indexes.get(index_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=index_id,
            not_found=VectorIndexNotFound,
        )
        self._indexes.pop(index_id, None)

    async def set_deletion_state(
        self,
        index_id: str,
        *,
        scope: ResourceScope,
        status: str,
        error: str | None,
    ) -> None:
        record = self._indexes.get(index_id)
        require_memory_scope(
            record,
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=index_id,
            not_found=VectorIndexNotFound,
        )
        assert record is not None
        self._indexes[index_id] = replace(
            record,
            status=status,
            last_error=error,
            updated_at=time.time(),
        )

    async def count_index(self, index_id: str, *, scope: ResourceScope) -> int:
        record = self._indexes.get(index_id)
        if record is None:
            return 0
        return int(
            record.created_by_user_id == scope.created_by_user_id
            and record.workspace_id == scope.workspace_id
        )

    async def remove_asset_memberships(
        self, file_id: str, *, scope: ResourceScope
    ) -> int:
        removed = 0
        for index_id, index in list(self._indexes.items()):
            if (
                index.created_by_user_id != scope.created_by_user_id
                or index.workspace_id != scope.workspace_id
            ):
                continue
            kept = tuple(member for member in index.members if member.file_id != file_id)
            removed += len(index.members) - len(kept)
            if kept != index.members:
                self._indexes[index_id] = replace(index, members=kept)
        return removed

    async def count_asset_memberships(
        self, file_id: str, *, scope: ResourceScope
    ) -> int:
        return sum(
            1
            for index in self._indexes.values()
            if index.created_by_user_id == scope.created_by_user_id
            and index.workspace_id == scope.workspace_id
            for member in index.members
            if member.file_id == file_id
        )

    async def aclose(self) -> None:
        return None


def _scoped(values, created_by_user_id: uuid.UUID | None, workspace_id):
    items = list(values)
    if created_by_user_id is not None:
        items = [i for i in items if i.created_by_user_id == created_by_user_id]
    if workspace_id is not None:
        items = [i for i in items if i.workspace_id == workspace_id]
    return items
