"""File-asset-record persistence service (project tier).

The asset-library counterpart of the chat/editor services: payload
validation, the owner-only access rule
(:func:`~inqtrix.auth.permissions.require_owned_access`), and the
"which records belong to this caller" resolution before the store query.
Sections/groups/assets are private per-user (no sharing surface);
all denials are the indistinct not-found.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from inqtrix.auth.permissions import require_owned_access
from inqtrix.services.workspace_guard import deny_cross_workspace
from inqtrix.project.asset_records_ports import (
    AssetDeletionInProgress,
    AssetGroup,
    AssetNotFound,
    AssetRecord,
    AssetSection,
    AssetUploadConflict,
    AssetStore,
    GroupNotFound,
    InitialUploadStatus,
    SectionNotFound,
)
from inqtrix.project.scoped_upsert import ResourceScope

if TYPE_CHECKING:
    from inqtrix.auth.principal import UserContext

_VALID_KINDS = frozenset({"temporary", "custom"})
_VALID_ORIGINS = frozenset({"chat", "editor", "library"})
_VALID_PARSE = frozenset({"parsed", "partial", "unsupported", "error"})


class AssetValidationError(ValueError):
    """Raised for client-payload problems (maps to HTTP 400)."""


class AssetRecordsService:
    """Application service over an :class:`AssetStore`."""

    def __init__(self, *, store: AssetStore, durable: bool = False) -> None:
        self._store = store
        self._durable = durable

    @property
    def store(self) -> AssetStore:
        return self._store

    @property
    def durable(self) -> bool:
        return self._durable

    # -- sections --------------------------------------------------------- #

    async def save_section(
        self, *, id, kind, title, created_at, updated_at,
        caller_user_id: uuid.UUID | None,
        workspace_id, visible_to,
    ) -> AssetSection:
        if kind not in _VALID_KINDS:
            raise AssetValidationError(f"unknown section kind: {kind!r}")
        existing = _find(await self._store.list_sections(created_by_user_id=None, workspace_id=None), id)
        owner_user_id, owner_ws = self._owner_for(
            existing, caller_user_id, workspace_id, visible_to, SectionNotFound
        )
        return await self._store.upsert_section(
            id=id, kind=kind, title=title, created_at=created_at,
            updated_at=updated_at, created_by_user_id=owner_user_id, workspace_id=owner_ws,
        )

    async def list_sections(
        self, *, caller_user_id: uuid.UUID | None, workspace_id
    ) -> list[AssetSection]:
        return await self._store.list_sections(created_by_user_id=caller_user_id, workspace_id=workspace_id)

    async def ensure_default_sections(
        self,
        *,
        caller_user_id: uuid.UUID | None,
        workspace_id,
    ) -> list[AssetSection]:
        """Converge first-load clients on one prepared-role set.

        This is intentionally separate from the ordinary client-addressed
        section PUT.  Titles never grant a semantic role, and historical rows
        are not relabelled by inference.
        """

        return await self._store.ensure_default_sections(
            created_by_user_id=caller_user_id,
            workspace_id=workspace_id,
        )

    async def delete_section(
        self, section_id, *, visible_to, request_workspace_id=None
    ) -> None:
        existing = _find(await self._store.list_sections(created_by_user_id=None, workspace_id=None), section_id)
        if existing is None:
            raise SectionNotFound(section_id)
        self._require_owner(existing, visible_to, SectionNotFound)
        deny_cross_workspace(
            resource_workspace_id=existing.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: SectionNotFound(section_id),
        )
        await self._store.delete_section(
            section_id, scope=ResourceScope.from_record(existing)
        )

    # -- groups ----------------------------------------------------------- #

    async def save_group(
        self, *, id, section_id, title, created_at, updated_at,
        caller_user_id: uuid.UUID | None,
        workspace_id, visible_to,
    ) -> AssetGroup:
        existing = _find(await self._store.list_groups(created_by_user_id=None, workspace_id=None), id)
        owner_user_id, owner_ws = self._owner_for(
            existing, caller_user_id, workspace_id, visible_to, GroupNotFound
        )
        return await self._store.upsert_group(
            id=id, section_id=section_id, title=title, created_at=created_at,
            updated_at=updated_at, created_by_user_id=owner_user_id, workspace_id=owner_ws,
        )

    async def list_groups(
        self, *, caller_user_id: uuid.UUID | None, workspace_id
    ) -> list[AssetGroup]:
        return await self._store.list_groups(created_by_user_id=caller_user_id, workspace_id=workspace_id)

    async def get_group(
        self, group_id, *, visible_to, request_workspace_id=None
    ) -> AssetGroup:
        existing = _find(
            await self._store.list_groups(
                created_by_user_id=None,
                workspace_id=None,
            ),
            group_id,
        )
        if existing is None:
            raise GroupNotFound(group_id)
        self._require_owner(existing, visible_to, GroupNotFound)
        deny_cross_workspace(
            resource_workspace_id=existing.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: GroupNotFound(group_id),
        )
        return existing

    async def delete_group(
        self, group_id, *, visible_to, request_workspace_id=None
    ) -> None:
        existing = await self.get_group(
            group_id,
            visible_to=visible_to,
            request_workspace_id=request_workspace_id,
        )
        await self._store.delete_group(
            group_id, scope=ResourceScope.from_record(existing)
        )

    # -- assets ----------------------------------------------------------- #

    async def save_asset(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, extracted_text, created_at,
        updated_at, caller_user_id: uuid.UUID | None, workspace_id, visible_to,
        parser_id=None,
        initial_upload_status: "InitialUploadStatus" = "ready",
    ) -> AssetRecord:
        if origin not in _VALID_ORIGINS:
            raise AssetValidationError(f"unknown asset origin: {origin!r}")
        if parse_status not in _VALID_PARSE:
            raise AssetValidationError(f"unknown parse status: {parse_status!r}")
        try:
            existing = await self._store.get_asset(id)
        except AssetNotFound:
            existing = None
        if existing is not None:
            if existing.lifecycle_status in {"deleting", "delete_failed"}:
                raise AssetDeletionInProgress(id)
            require_owned_access(
                owner_user_id=existing.created_by_user_id, resource_tenant_id=existing.tenant_id,
                resource_id=existing.id, visible_to=visible_to,
                not_found=AssetNotFound,
            )
            if (
                server_file_id is not None
                and server_file_id != existing.server_file_id
            ):
                raise AssetUploadConflict(
                    "server_file_id is owned by the upload lifecycle"
                )
            owner_user_id, owner_ws = existing.created_by_user_id, existing.workspace_id
        else:
            if server_file_id is not None:
                raise AssetUploadConflict(
                    "server_file_id requires a validated upload finalization"
                )
            owner_user_id, owner_ws = caller_user_id, workspace_id
        return await self._store.upsert_asset(
            id=id, section_id=section_id, group_id=group_id, title=title,
            label=label, file_name=file_name, mime_type=mime_type, origin=origin,
            page_count=page_count, parse_status=parse_status,
            parse_warning=parse_warning, text_truncated=text_truncated,
            size_bytes=size_bytes, server_file_id=server_file_id,
            parser_id=parser_id, extracted_text=extracted_text,
            created_at=created_at, updated_at=updated_at,
            created_by_user_id=owner_user_id, workspace_id=owner_ws,
            initial_upload_status=initial_upload_status,
        )

    async def bind_uploaded_file(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, created_at, updated_at,
        caller_user_id: uuid.UUID | None, workspace_id, visible_to,
        parser_id=None, upload_operation_id=None,
    ) -> AssetRecord:
        """Create the library record for a just-uploaded file, bound to its
        target section, so the upload response already carries a durable
        collection placement (a page reload cannot lose it).

        Stricter than :meth:`save_asset`: the target section (and group,
        when given) must exist and be visible to the caller — this path is
        driven by upload metadata rather than an explicit asset PUT, so a
        dangling or foreign target is rejected with the indistinct
        not-found instead of being caught later by the FK. The body is
        always empty here; the extracted text follows via the regular
        asset upsert.
        """
        try:
            reservation = await self._store.get_asset(id)
        except AssetNotFound:
            # A bound upload always reserves its stable id before bytes move.
            # Missing here means a concurrent deletion won; never recreate it.
            raise AssetNotFound(id) from None
        if reservation.lifecycle_status in {"deleting", "delete_failed"}:
            raise AssetDeletionInProgress(id)
        require_owned_access(
            owner_user_id=reservation.created_by_user_id,
            resource_tenant_id=reservation.tenant_id,
            resource_id=reservation.id,
            visible_to=visible_to,
            not_found=AssetNotFound,
        )
        if (
            reservation.section_id != section_id
            or reservation.group_id != group_id
        ):
            raise AssetUploadConflict("upload binding changed after reservation")
        return await self._store.finalize_asset_upload(
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
            scope=ResourceScope.from_record(reservation),
            upload_operation_id=upload_operation_id,
        )

    async def reserve_upload(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, created_at, updated_at, caller_user_id: uuid.UUID | None,
        workspace_id, visible_to, parser_id=None,
    ) -> AssetRecord:
        """Register a stable active asset before its first upload byte moves.

        The reservation closes the upload/delete race: deletion can tombstone
        this id while the blob transfer is running, and the final binding then
        fails instead of recreating a removed asset. Retrying an unbound active
        reservation is idempotent and preserves any extracted text already
        saved by the client.
        """

        section = _find(
            await self._store.list_sections(
                created_by_user_id=None, workspace_id=None
            ),
            section_id,
        )
        if section is None:
            raise SectionNotFound(section_id)
        self._require_owner(section, visible_to, SectionNotFound)
        if group_id is not None:
            group = _find(
                await self._store.list_groups(
                    created_by_user_id=None, workspace_id=None
                ),
                group_id,
            )
            if group is None:
                raise GroupNotFound(group_id)
            self._require_owner(group, visible_to, GroupNotFound)
            if group.section_id != section_id:
                raise AssetValidationError(
                    "group does not belong to the target section"
                )
        try:
            existing = await self._store.get_asset(id)
        except AssetNotFound:
            existing = None
        if existing is not None:
            if existing.lifecycle_status in {"deleting", "delete_failed"}:
                raise AssetDeletionInProgress(id)
            require_owned_access(
                owner_user_id=existing.created_by_user_id,
                resource_tenant_id=existing.tenant_id,
                resource_id=existing.id,
                visible_to=visible_to,
                not_found=AssetNotFound,
            )
            if (
                existing.section_id != section_id
                or existing.group_id != group_id
            ):
                raise AssetUploadConflict(
                    "upload binding changed after reservation"
                )
            if existing.server_file_id:
                # The client can legitimately repeat the same request after a
                # response was lost.  The stable asset id is the idempotency
                # key: return the accepted reservation instead of creating a
                # second blob.  Reusing the id for different bytes/metadata is
                # a conflict, never an implicit replacement.
                if (
                    existing.file_name != file_name
                    or existing.mime_type != mime_type
                    or existing.size_bytes != size_bytes
                ):
                    raise AssetUploadConflict(
                        "asset id already belongs to a different original file"
                    )
                return existing
            extracted_text = existing.extracted_text
            parser_id = existing.parser_id or parser_id
            parse_status = existing.parse_status
            parse_warning = existing.parse_warning
            page_count = existing.page_count
            text_truncated = existing.text_truncated
        else:
            extracted_text = ""
        reserved = await self.save_asset(
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
            server_file_id=None,
            parser_id=parser_id,
            extracted_text=extracted_text,
            created_at=created_at,
            updated_at=updated_at,
            caller_user_id=caller_user_id,
            workspace_id=workspace_id,
            visible_to=visible_to,
            # The caller's intent, applied AT the insert: a reservation
            # never exists as 'ready' without bytes, not even between two
            # transactions. This closes the window a failing follow-up
            # write would leave open.
            initial_upload_status="awaiting_upload",
        )
        # Kept deliberately: on an EXISTING row the insert-only intent does
        # nothing, and this write is what brings a row parked at 'failed'
        # back to 'awaiting_upload' on a retried reservation.
        return await self._store.set_asset_upload_state(
            id,
            scope=ResourceScope.from_record(reserved),
            upload_status="awaiting_upload",
            upload_error=None,
            upload_operation_id=None,
        )

    async def mark_upload_failed(
        self,
        asset_id: str,
        *,
        visible_to: "UserContext | None",
        message: str,
    ) -> AssetRecord:
        asset = await self.get_asset(asset_id, visible_to=visible_to)
        return await self._store.set_asset_upload_state(
            asset_id,
            scope=ResourceScope.from_record(asset),
            upload_status="failed",
            upload_error=message,
            upload_operation_id=asset.upload_operation_id,
        )

    async def set_upload_operation_state(
        self,
        asset_id: str,
        *,
        visible_to: "UserContext | None",
        upload_status: str,
        upload_error: str | None,
        upload_operation_id: str | None,
        expected_upload_operation_id: str | None = None,
    ) -> AssetRecord:
        """Set server-owned upload state, optionally fenced to one operation."""

        asset = await self.get_asset(asset_id, visible_to=visible_to)
        return await self._store.set_asset_upload_state(
            asset_id,
            scope=ResourceScope.from_record(asset),
            upload_status=upload_status,
            upload_error=upload_error,
            upload_operation_id=upload_operation_id,
            expected_upload_operation_id=expected_upload_operation_id,
        )

    async def publish_prepared_text(
        self,
        asset_id: str,
        *,
        visible_to: "UserContext | None",
        server_file_id: str,
        upload_operation_id: str,
        text: str,
        parser_id: str,
        parser_revision: str | None,
        content_hash: str,
        file_sha256: str,
        page_texts: list[str] | None,
        prepared_at: float,
    ) -> AssetRecord:
        """Publish the operation-fenced server parse used by indexing."""

        asset = await self.get_asset(asset_id, visible_to=visible_to)
        return await self._store.set_asset_prepared_text(
            asset_id,
            scope=ResourceScope.from_record(asset),
            server_file_id=server_file_id,
            expected_upload_operation_id=upload_operation_id,
            text=text,
            parser_id=parser_id,
            parser_revision=parser_revision,
            content_hash=content_hash,
            file_sha256=file_sha256,
            page_texts=page_texts,
            prepared_at=prepared_at,
        )

    async def publish_legacy_prepared_text(
        self,
        asset_id: str,
        *,
        visible_to: "UserContext | None",
        server_file_id: str,
        text: str,
        parser_id: str,
        parser_revision: str | None,
        content_hash: str,
        file_sha256: str,
        page_texts: list[str] | None,
        prepared_at: float,
    ) -> AssetRecord:
        """Publish a server parse for an asset created before upload jobs.

        The null operation id is part of the compare-and-swap guard.  A
        concurrent modern upload, file replacement, or deletion therefore
        wins instead of letting this compatibility repair overwrite it.
        """

        asset = await self.get_asset(asset_id, visible_to=visible_to)
        if asset.upload_operation_id is not None:
            raise AssetUploadConflict(asset_id)
        return await self._store.set_asset_prepared_text(
            asset_id,
            scope=ResourceScope.from_record(asset),
            server_file_id=server_file_id,
            expected_upload_operation_id=None,
            text=text,
            parser_id=parser_id,
            parser_revision=parser_revision,
            content_hash=content_hash,
            file_sha256=file_sha256,
            page_texts=page_texts,
            prepared_at=prepared_at,
        )

    async def publish_parse_failure(
        self,
        asset_id: str,
        *,
        visible_to: "UserContext | None",
        server_file_id: str,
        upload_operation_id: str,
        message: str,
    ) -> AssetRecord:
        """Expose a deterministic server-parse failure on the source asset."""

        asset = await self.get_asset(asset_id, visible_to=visible_to)
        return await self._store.set_asset_parse_failure(
            asset_id,
            scope=ResourceScope.from_record(asset),
            server_file_id=server_file_id,
            expected_upload_operation_id=upload_operation_id,
            message=message,
        )

    async def list_assets(
        self, *, caller_user_id: uuid.UUID | None, workspace_id, limit, after
    ):
        return await self._store.list_assets_page(
            created_by_user_id=caller_user_id, workspace_id=workspace_id, limit=limit, after=after
        )

    async def get_asset(self, asset_id, *, visible_to) -> AssetRecord:
        asset = await self._store.get_asset(asset_id)
        require_owned_access(
            owner_user_id=asset.created_by_user_id, resource_tenant_id=asset.tenant_id,
            resource_id=asset.id, visible_to=visible_to,
            not_found=AssetNotFound,
        )
        return asset

    async def find_asset_by_server_file_id(
        self, server_file_id: str
    ) -> AssetRecord | None:
        """Internal aggregate lookup for guarding the raw file endpoint."""

        return await self._store.find_asset_by_server_file_id(server_file_id)

    async def list_assets_by_server_file_id(
        self, server_file_id: str
    ) -> list[AssetRecord]:
        """Internal integrity lookup used by aggregate deletion."""

        return await self._store.list_assets_by_server_file_id(server_file_id)

    async def assets_by_ids(
        self,
        asset_ids: tuple[str, ...],
        *,
        visible_to: "UserContext | None",
        workspace_id: str | None,
    ) -> list[AssetRecord]:
        owner_user_id = (
            visible_to.principal.user_id if visible_to is not None else None
        )
        assets = await self._store.list_assets_by_ids(
            asset_ids,
            scope=ResourceScope(owner_user_id, workspace_id),
        )
        if len(assets) != len(asset_ids):
            raise AssetNotFound("one or more assets")
        for asset in assets:
            self._require_owner(asset, visible_to, AssetNotFound)
        return assets

    async def delete_asset(
        self, asset_id, *, visible_to, request_workspace_id=None
    ) -> None:
        asset = await self._store.get_asset(asset_id)
        require_owned_access(
            owner_user_id=asset.created_by_user_id, resource_tenant_id=asset.tenant_id,
            resource_id=asset.id, visible_to=visible_to,
            not_found=AssetNotFound,
        )
        deny_cross_workspace(
            resource_workspace_id=asset.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: AssetNotFound(asset_id),
        )
        await self._store.delete_asset(
            asset_id, scope=ResourceScope.from_record(asset)
        )

    async def set_asset_deletion_state(
        self,
        asset_id: str,
        *,
        visible_to,
        request_workspace_id=None,
        lifecycle_status: str,
        deletion_operation_id: str | None,
        deletion_stage: str | None,
        deletion_error: str | None,
    ) -> AssetRecord:
        """Update only the server-owned destructive lifecycle fields."""

        asset = await self.get_asset(asset_id, visible_to=visible_to)
        deny_cross_workspace(
            resource_workspace_id=asset.workspace_id,
            request_workspace_id=request_workspace_id,
            not_found=lambda: AssetNotFound(asset_id),
        )
        return await self._store.set_asset_deletion_state(
            asset_id,
            scope=ResourceScope.from_record(asset),
            lifecycle_status=lifecycle_status,
            deletion_operation_id=deletion_operation_id,
            deletion_stage=deletion_stage,
            deletion_error=deletion_error,
        )

    async def assets_for_group(self, group_id, *, visible_to) -> list[AssetRecord]:
        group = _find(
            await self._store.list_groups(created_by_user_id=None, workspace_id=None),
            group_id,
        )
        if group is None:
            raise GroupNotFound(group_id)
        self._require_owner(group, visible_to, GroupNotFound)
        return await self._store.list_assets_for_target(
            section_id=None,
            group_id=group_id,
            scope=ResourceScope.from_record(group),
        )

    async def assets_for_section(self, section_id, *, visible_to) -> list[AssetRecord]:
        section = _find(
            await self._store.list_sections(created_by_user_id=None, workspace_id=None),
            section_id,
        )
        if section is None:
            raise SectionNotFound(section_id)
        self._require_owner(section, visible_to, SectionNotFound)
        return await self._store.list_assets_for_target(
            section_id=section_id,
            group_id=None,
            scope=ResourceScope.from_record(section),
        )

    # -- helpers ---------------------------------------------------------- #

    def _owner_for(
        self,
        existing,
        caller_user_id: uuid.UUID | None,
        workspace_id,
        visible_to,
        not_found,
    ):
        if existing is not None:
            require_owned_access(
                owner_user_id=existing.created_by_user_id, resource_tenant_id=existing.tenant_id,
                resource_id=existing.id, visible_to=visible_to,
                not_found=not_found,
            )
            return existing.created_by_user_id, existing.workspace_id
        return caller_user_id, workspace_id

    def _require_owner(self, existing, visible_to, not_found):
        require_owned_access(
            owner_user_id=existing.created_by_user_id, resource_tenant_id=existing.tenant_id,
            resource_id=existing.id, visible_to=visible_to,
            not_found=not_found,
        )


def _find(items, item_id):
    for item in items:
        if item.id == item_id:
            return item
    return None
