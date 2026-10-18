"""In-memory file-asset-record store (tier without Postgres + offline test).

Mirrors :class:`~inqtrix.project.asset_records_postgres.PostgresAssetStore`
byte-for-byte (filter before slice; reuse keyset_page; ``list_assets_page``
returns an empty body). Process-local, not durable.
"""

from __future__ import annotations

import time
import uuid
from contextlib import nullcontext
from dataclasses import replace

from inqtrix.pagination import keyset_page
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetGroup,
    AssetNotFound,
    AssetRecord,
    AssetSection,
    AssetUploadConflict,
    DEFAULT_ASSET_SECTION_SPECS,
    ensure_initial_upload_status,
    GroupNotFound,
    SectionNotFound,
)
from inqtrix.project.scoped_upsert import ResourceScope, require_memory_scope
from inqtrix.source_authority import (
    MemorySourceLifecycleAuthority,
    SourceLifecycleConflict,
    SourceScope,
)


class MemoryAssetStore:
    """Process-local :class:`~inqtrix.project.asset_records_ports.AssetStore`."""

    def __init__(self) -> None:
        self._sections: dict[str, AssetSection] = {}
        self._groups: dict[str, AssetGroup] = {}
        self._assets: dict[str, AssetRecord] = {}
        self._asset_tombstones: set[str] = set()
        self._group_tombstones: set[str] = set()
        self._section_tombstones: set[str] = set()
        self._source_authority: MemorySourceLifecycleAuthority | None = None

    def bind_source_lifecycle_authority(
        self, authority: MemorySourceLifecycleAuthority
    ) -> None:
        """Share one source fence with Knowledge and vector-index stores."""

        self._source_authority = authority

    # -- sections --------------------------------------------------------- #

    async def upsert_section(
        self, *, id, kind, title, created_at, updated_at,
        created_by_user_id: uuid.UUID | None, workspace_id
    ) -> AssetSection:
        if id in self._section_tombstones:
            raise SectionNotFound(id)
        existing = self._sections.get(id)
        if existing is not None:
            require_memory_scope(
                existing,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=id,
                not_found=SectionNotFound,
            )
            semantic_role = existing.semantic_role
            if existing.kind != kind or existing.title != title:
                semantic_role = "custom"
            section = replace(
                existing,
                kind=kind,
                title=title,
                updated_at=updated_at,
                semantic_role=semantic_role,
            )
        else:
            section = AssetSection(
                id=id, kind=kind, title=title, created_at=created_at,
                updated_at=updated_at, created_by_user_id=created_by_user_id,
                workspace_id=workspace_id, semantic_role="custom",
            )
        self._sections[id] = section
        return section

    async def list_sections(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id
    ) -> list[AssetSection]:
        items = _scoped(self._sections.values(), created_by_user_id, workspace_id)
        items.sort(key=lambda s: (s.created_at, s.id), reverse=True)
        return items

    async def ensure_default_sections(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id,
    ) -> list[AssetSection]:
        """Create each prepared role once within the exact in-memory scope."""

        scoped = _scoped(self._sections.values(), created_by_user_id, workspace_id)
        by_role = {
            section.semantic_role: section
            for section in scoped
            if section.semantic_role is not None
        }
        ensured: list[AssetSection] = []
        now = time.time()
        for role, kind, title in DEFAULT_ASSET_SECTION_SPECS:
            existing = by_role.get(role)
            if existing is not None:
                ensured.append(existing)
                continue
            section_id = f"fsec_{uuid.uuid4().hex}"
            while section_id in self._sections or section_id in self._section_tombstones:
                section_id = f"fsec_{uuid.uuid4().hex}"
            section = AssetSection(
                id=section_id,
                kind=kind,
                title=title,
                created_at=now,
                updated_at=now,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                semantic_role=role,
            )
            self._sections[section_id] = section
            by_role[role] = section
            ensured.append(section)
        return ensured

    async def delete_section(
        self, section_id: str, *, scope: ResourceScope
    ) -> None:
        require_memory_scope(
            self._sections.get(section_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=section_id,
            not_found=SectionNotFound,
        )
        self._sections.pop(section_id, None)
        self._section_tombstones.add(section_id)
        self._groups = {
            gid: g for gid, g in self._groups.items() if g.section_id != section_id
        }
        self._assets = {
            aid: a for aid, a in self._assets.items() if a.section_id != section_id
        }

    # -- groups ----------------------------------------------------------- #

    async def upsert_group(
        self, *, id, section_id, title, created_at, updated_at,
        created_by_user_id: uuid.UUID | None, workspace_id
    ) -> AssetGroup:
        if section_id in self._section_tombstones:
            raise SectionNotFound(section_id)
        if id in self._group_tombstones:
            raise GroupNotFound(id)
        require_memory_scope(
            self._sections.get(section_id),
            created_by_user_id=created_by_user_id,
            workspace_id=workspace_id,
            resource_id=section_id,
            not_found=SectionNotFound,
        )
        existing = self._groups.get(id)
        if existing is not None:
            require_memory_scope(
                existing,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=id,
                not_found=GroupNotFound,
            )
            group = replace(existing, section_id=section_id, title=title, updated_at=updated_at)
        else:
            group = AssetGroup(
                id=id, section_id=section_id, title=title, created_at=created_at,
                updated_at=updated_at, created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        self._groups[id] = group
        return group

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id
    ) -> list[AssetGroup]:
        items = _scoped(self._groups.values(), created_by_user_id, workspace_id)
        items.sort(key=lambda g: (g.created_at, g.id), reverse=True)
        return items

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        require_memory_scope(
            self._groups.get(group_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=group_id,
            not_found=GroupNotFound,
        )
        self._groups.pop(group_id, None)
        for aid, asset in list(self._assets.items()):
            if asset.group_id == group_id:
                self._assets[aid] = replace(asset, group_id=None)

    # -- assets ----------------------------------------------------------- #

    async def upsert_asset(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, parser_id=None, extracted_text, created_at,
        updated_at, created_by_user_id: uuid.UUID | None, workspace_id,
        initial_upload_status: str = "ready",
    ) -> AssetRecord:
        # Validate BEFORE touching the source authority: active_write with
        # create_if_missing registers a lifecycle that has no rollback, so
        # a garbage value must fall here -- same placement and error
        # precedence as the Postgres backend.
        ensure_initial_upload_status(initial_upload_status)
        guard = (
            self._source_authority.active_write(
                SourceScope(
                    tenant_id="default",
                    source_id=f"asset:{id}",
                    owner_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                ),
                create_if_missing=True,
            )
            if self._source_authority is not None
            else nullcontext()
        )
        try:
            with guard:
                return await self._upsert_asset_under_authority(
                    id=id,
                    section_id=section_id,
                    group_id=group_id,
                    title=title,
                    label=label,
                    file_name=file_name,
                    mime_type=mime_type,
                    origin=origin,
                    page_count=page_count,
                    parse_status=parse_status,
                    parse_warning=parse_warning,
                    text_truncated=text_truncated,
                    size_bytes=size_bytes,
                    server_file_id=server_file_id,
                    parser_id=parser_id,
                    extracted_text=extracted_text,
                    created_at=created_at,
                    updated_at=updated_at,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    initial_upload_status=initial_upload_status,
                )
        except SourceLifecycleConflict as exc:
            raise AssetDeletionInProgress(id) from exc

    async def _upsert_asset_under_authority(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, parser_id=None, extracted_text, created_at,
        updated_at, created_by_user_id: uuid.UUID | None, workspace_id,
        initial_upload_status: str = "ready",
    ) -> AssetRecord:
        if id in self._asset_tombstones:
            raise AssetDeletionInProgress(id)
        if section_id in self._section_tombstones:
            raise SectionNotFound(section_id)
        require_memory_scope(
            self._sections.get(section_id),
            created_by_user_id=created_by_user_id,
            workspace_id=workspace_id,
            resource_id=section_id,
            not_found=SectionNotFound,
        )
        if group_id is not None:
            if group_id in self._group_tombstones:
                raise GroupNotFound(group_id)
            group = require_memory_scope(
                self._groups.get(group_id),
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=group_id,
                not_found=GroupNotFound,
            )
            if group.section_id != section_id:
                raise GroupNotFound(group_id)
        existing = self._assets.get(id)
        if server_file_id is not None and any(
            asset.id != id and asset.server_file_id == server_file_id
            for asset in self._assets.values()
        ):
            raise AssetUploadConflict(server_file_id)
        mutable = dict(
            section_id=section_id, group_id=group_id, title=title, label=label,
            file_name=file_name, mime_type=mime_type, origin=origin,
            page_count=page_count, parse_status=parse_status,
            parse_warning=parse_warning, text_truncated=text_truncated,
            size_bytes=size_bytes, server_file_id=server_file_id,
            parser_id=parser_id, extracted_text=extracted_text,
            updated_at=updated_at,
        )
        if existing is not None:
            require_memory_scope(
                existing,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                resource_id=id,
                not_found=AssetNotFound,
            )
            if existing.lifecycle_status != "active":
                raise AssetDeletionInProgress(id)
            # Once an original file has been accepted, the binding belongs to
            # the server-side upload lifecycle.  A stale/local PUT may update
            # extracted text and display metadata but cannot clear or replace
            # the blob reference it did not create.
            if existing.server_file_id is not None:
                mutable["server_file_id"] = existing.server_file_id
                mutable["size_bytes"] = existing.size_bytes
                mutable["file_name"] = existing.file_name
                mutable["mime_type"] = existing.mime_type
            asset = replace(existing, **mutable)
        else:
            asset = AssetRecord(
                id=id, created_at=created_at, created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                # INSERT-only, mirroring the Postgres upsert exactly: the
                # caller's intent decides the initial status; an existing
                # row above keeps its stored one (replace() without
                # upload_status in `mutable`).
                upload_status=initial_upload_status,
                **mutable,
            )
        self._assets[id] = asset
        return asset

    async def finalize_asset_upload(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, parser_id, created_at, updated_at, scope,
        upload_operation_id=None,
    ) -> AssetRecord:
        guard = (
            self._source_authority.active_write(
                SourceScope(
                    tenant_id="default",
                    source_id=f"asset:{id}",
                    owner_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                ),
                create_if_missing=False,
            )
            if self._source_authority is not None
            else nullcontext()
        )
        try:
            with guard:
                return await self._finalize_asset_upload_under_authority(
                    id=id,
                    section_id=section_id,
                    group_id=group_id,
                    title=title,
                    label=label,
                    file_name=file_name,
                    mime_type=mime_type,
                    origin=origin,
                    page_count=page_count,
                    parse_status=parse_status,
                    parse_warning=parse_warning,
                    text_truncated=text_truncated,
                    size_bytes=size_bytes,
                    server_file_id=server_file_id,
                    parser_id=parser_id,
                    created_at=created_at,
                    updated_at=updated_at,
                    scope=scope,
                    upload_operation_id=upload_operation_id,
                )
        except SourceLifecycleConflict as exc:
            raise AssetDeletionInProgress(id) from exc

    async def _finalize_asset_upload_under_authority(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, parser_id, created_at, updated_at, scope,
        upload_operation_id=None,
    ) -> AssetRecord:
        del created_at
        if id in self._asset_tombstones:
            raise AssetDeletionInProgress(id)
        if section_id in self._section_tombstones:
            raise AssetDeletionInProgress(id)
        if group_id is not None and group_id in self._group_tombstones:
            raise AssetDeletionInProgress(id)
        asset = require_memory_scope(
            self._assets.get(id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=id,
            not_found=AssetNotFound,
        )
        if asset.lifecycle_status != "active":
            raise AssetDeletionInProgress(id)
        if asset.section_id != section_id or asset.group_id != group_id:
            raise AssetNotFound(id)
        if asset.server_file_id is not None:
            if asset.server_file_id == server_file_id:
                return asset
            raise AssetUploadConflict(id)
        if any(
            other.id != id and other.server_file_id == server_file_id
            for other in self._assets.values()
        ):
            raise AssetUploadConflict(server_file_id)
        updated = replace(
            asset,
            title=title,
            label=label,
            file_name=file_name,
            mime_type=mime_type,
            origin=origin,
            page_count=page_count,
            parse_status=parse_status,
            parse_warning=parse_warning,
            text_truncated=text_truncated,
            size_bytes=size_bytes,
            server_file_id=server_file_id,
            parser_id=parser_id,
            upload_status=("finalizing" if upload_operation_id else "ready"),
            upload_error=None,
            upload_operation_id=upload_operation_id,
            updated_at=updated_at,
        )
        self._assets[id] = updated
        return updated

    async def tombstone_asset_id(
        self, asset_id: str, *, scope: ResourceScope
    ) -> None:
        del scope
        self._asset_tombstones.add(asset_id)

    async def tombstone_section_id(
        self, section_id: str, *, scope: ResourceScope
    ) -> None:
        del scope
        self._section_tombstones.add(section_id)

    async def tombstone_group_id(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        del scope
        self._group_tombstones.add(group_id)

    async def set_asset_upload_state(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        upload_status: str,
        upload_error: str | None,
        upload_operation_id: str | None,
        expected_upload_operation_id: str | None = None,
    ) -> AssetRecord:
        guard = (
            self._source_authority.active_write(
                SourceScope(
                    tenant_id="default",
                    source_id=f"asset:{asset_id}",
                    owner_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                ),
                create_if_missing=False,
            )
            if self._source_authority is not None
            else nullcontext()
        )
        try:
            with guard:
                return await self._set_asset_upload_state_under_authority(
                    asset_id,
                    scope=scope,
                    upload_status=upload_status,
                    upload_error=upload_error,
                    upload_operation_id=upload_operation_id,
                    expected_upload_operation_id=expected_upload_operation_id,
                )
        except SourceLifecycleConflict as exc:
            raise AssetDeletionInProgress(asset_id) from exc

    async def _set_asset_upload_state_under_authority(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        upload_status: str,
        upload_error: str | None,
        upload_operation_id: str | None,
        expected_upload_operation_id: str | None = None,
    ) -> AssetRecord:
        asset = require_memory_scope(
            self._assets.get(asset_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=asset_id,
            not_found=AssetNotFound,
        )
        if asset.lifecycle_status != "active":
            raise AssetDeletionInProgress(asset_id)
        if (
            expected_upload_operation_id is not None
            and asset.upload_operation_id != expected_upload_operation_id
        ):
            raise AssetUploadConflict(asset_id)
        updated = replace(
            asset,
            upload_status=upload_status,
            upload_error=upload_error,
            upload_operation_id=upload_operation_id,
            updated_at=time.time(),
        )
        self._assets[asset_id] = updated
        return updated

    async def set_asset_prepared_text(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        server_file_id: str,
        expected_upload_operation_id: str | None,
        text: str,
        parser_id: str,
        parser_revision: str | None,
        content_hash: str,
        file_sha256: str,
        page_texts: list[str] | None,
        prepared_at: float,
    ) -> AssetRecord:
        return await self._set_asset_parse_result(
            asset_id,
            scope=scope,
            server_file_id=server_file_id,
            expected_upload_operation_id=expected_upload_operation_id,
            changes={
                "extracted_text": text,
                "parser_id": parser_id,
                "parse_status": "parsed",
                "parse_warning": None,
                "text_truncated": False,
                "prepared_text": text,
                "prepared_parser_id": parser_id,
                "prepared_parser_revision": parser_revision or None,
                "prepared_content_hash": content_hash,
                "prepared_file_sha256": file_sha256,
                "prepared_page_texts": tuple(page_texts or ()),
                # Derived where the pages are known. Omitted for an empty
                # list, because page_texts is only populated for paginated
                # formats — writing 0 there would replace a real count with
                # a wrong one.
                **({"page_count": len(page_texts)} if page_texts else {}),
                "prepared_at": prepared_at,
                "updated_at": prepared_at,
            },
        )

    async def set_asset_parse_failure(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        server_file_id: str,
        expected_upload_operation_id: str,
        message: str,
    ) -> AssetRecord:
        return await self._set_asset_parse_result(
            asset_id,
            scope=scope,
            server_file_id=server_file_id,
            expected_upload_operation_id=expected_upload_operation_id,
            changes={
                "parse_status": "error",
                "parse_warning": message,
                "prepared_text": "",
                "prepared_parser_id": None,
                "prepared_parser_revision": None,
                "prepared_content_hash": None,
                "prepared_file_sha256": None,
                "prepared_page_texts": (),
                "prepared_at": None,
                "updated_at": time.time(),
            },
        )

    async def _set_asset_parse_result(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        server_file_id: str,
        expected_upload_operation_id: str | None,
        changes: dict,
    ) -> AssetRecord:
        guard = (
            self._source_authority.active_write(
                SourceScope(
                    tenant_id="default",
                    source_id=f"asset:{asset_id}",
                    owner_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                ),
                create_if_missing=False,
            )
            if self._source_authority is not None
            else nullcontext()
        )
        try:
            with guard:
                asset = require_memory_scope(
                    self._assets.get(asset_id),
                    created_by_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                    resource_id=asset_id,
                    not_found=AssetNotFound,
                )
                if asset.lifecycle_status != "active":
                    raise AssetDeletionInProgress(asset_id)
                if (
                    asset.server_file_id != server_file_id
                    or asset.upload_operation_id != expected_upload_operation_id
                ):
                    raise AssetUploadConflict(asset_id)
                updated = replace(asset, **changes)
                self._assets[asset_id] = updated
                return updated
        except SourceLifecycleConflict as exc:
            raise AssetDeletionInProgress(asset_id) from exc

    async def list_assets_page(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id, limit, after
    ) -> tuple[list[AssetRecord], str | None]:
        items = _scoped(self._assets.values(), created_by_user_id, workspace_id)
        items.sort(key=lambda a: (a.created_at, a.id), reverse=True)
        page, cursor = keyset_page(
            items, limit=limit, after=after,
            created_at_of=lambda a: a.created_at, id_of=lambda a: a.id,
        )
        return [replace(a, extracted_text="", prepared_text="") for a in page], cursor

    async def get_asset(self, asset_id: str) -> AssetRecord:
        try:
            return self._assets[asset_id]
        except KeyError as exc:
            raise AssetNotFound(asset_id) from exc

    async def find_asset_by_server_file_id(
        self, server_file_id: str
    ) -> AssetRecord | None:
        return next(
            (
                asset
                for asset in self._assets.values()
                if asset.server_file_id == server_file_id
            ),
            None,
        )

    async def list_assets_by_server_file_id(
        self, server_file_id: str
    ) -> list[AssetRecord]:
        return [
            asset
            for asset in self._assets.values()
            if asset.server_file_id == server_file_id
        ]

    async def detach_server_file_for_deletion(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        operation_id: str,
        expected_server_file_id: str,
    ) -> AssetRecord:
        asset = require_memory_scope(
            self._assets.get(asset_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=asset_id,
            not_found=AssetNotFound,
        )
        if (
            asset.lifecycle_status not in {"deleting", "delete_failed"}
            or asset.deletion_operation_id != operation_id
        ):
            raise AssetDeletionInProgress(asset_id)
        if asset.server_file_id is None:
            return asset
        if asset.server_file_id != expected_server_file_id:
            raise RuntimeError(
                "asset file binding changed during deletion"
            )
        detached = replace(
            asset,
            server_file_id=None,
            updated_at=time.time(),
        )
        self._assets[asset_id] = detached
        return detached

    async def list_assets_by_ids(
        self,
        asset_ids: tuple[str, ...],
        *,
        scope: ResourceScope,
    ) -> list[AssetRecord]:
        return [
            asset
            for asset_id in asset_ids
            if (asset := self._assets.get(asset_id)) is not None
            and asset.created_by_user_id == scope.created_by_user_id
            and asset.workspace_id == scope.workspace_id
        ]

    async def delete_asset(
        self, asset_id: str, *, scope: ResourceScope
    ) -> None:
        require_memory_scope(
            self._assets.get(asset_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=asset_id,
            not_found=AssetNotFound,
        )
        self._assets.pop(asset_id, None)
        self._asset_tombstones.add(asset_id)

    async def set_asset_deletion_state(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        lifecycle_status: str,
        deletion_operation_id: str | None,
        deletion_stage: str | None,
        deletion_error: str | None,
    ) -> AssetRecord:
        asset = require_memory_scope(
            self._assets.get(asset_id),
            created_by_user_id=scope.created_by_user_id,
            workspace_id=scope.workspace_id,
            resource_id=asset_id,
            not_found=AssetNotFound,
        )
        updated = replace(
            asset,
            lifecycle_status=lifecycle_status,
            deletion_operation_id=deletion_operation_id,
            deletion_stage=deletion_stage,
            deletion_error=deletion_error,
            updated_at=time.time(),
        )
        self._assets[asset_id] = updated
        return updated

    async def list_assets_for_target(
        self,
        *,
        section_id: str | None,
        group_id: str | None,
        scope: ResourceScope,
    ) -> list[AssetRecord]:
        items = _scoped(
            self._assets.values(),
            scope.created_by_user_id,
            scope.workspace_id,
        )
        if section_id is not None:
            items = [item for item in items if item.section_id == section_id]
        if group_id is not None:
            items = [item for item in items if item.group_id == group_id]
        return items

    async def aclose(self) -> None:
        return None


def _scoped(values, created_by_user_id: uuid.UUID | None, workspace_id):
    items = list(values)
    if created_by_user_id is not None:
        items = [i for i in items if i.created_by_user_id == created_by_user_id]
    if workspace_id is not None:
        items = [i for i in items if i.workspace_id == workspace_id]
    return items
