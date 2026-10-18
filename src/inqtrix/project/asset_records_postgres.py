"""Postgres-backed file-asset-record store (durable project tier).

Sections, groups, and asset records persist relationally, scoped per
``(tenant_id, created_by_user_id, workspace_id)`` with RLS + the inherited
tenant-session lifecycle (:class:`BaseSessionStore`). Like editor
documents, ``list_assets_page`` SELECTs metadata columns only (NOT the
heavy ``extracted_text``); ``get_asset`` SELECTs the full row.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import and_, case, null, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from inqtrix.pagination import encode_cursor
from inqtrix.project.base_session_store import (
    BaseSessionStore,
    DEFAULT_TENANT as _DEFAULT_TENANT,
)
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
from inqtrix.project.asset_lifecycle import (
    lock_asset_lifecycle,
    lock_group_lifecycle,
    lock_section_lifecycle,
)
from inqtrix.project.scoped_upsert import (
    ResourceScope,
    delete_scoped_postgres,
    require_scoped_parent,
    scoped_postgres_upsert,
)
from inqtrix.storage.asset_records_orm import (
    asset_groups,
    asset_records,
    asset_sections,
)
from inqtrix.storage.deletions_orm import (
    deletion_operation_assets,
    deletion_operations,
)
from inqtrix.source_authority import (
    PostgresSourceLifecycleAuthority,
    SourceLifecycleConflict,
    SourceScope,
)

_SOURCE_AUTHORITY = PostgresSourceLifecycleAuthority()

# Asset metadata columns (everything EXCEPT the heavy extracted_text) for
# the list path — the text is transferred only on get_asset.
_ASSET_META_COLUMNS = (
    asset_records.c.id,
    asset_records.c.tenant_id,
    asset_records.c.created_by_user_id,
    asset_records.c.workspace_id,
    asset_records.c.section_id,
    asset_records.c.group_id,
    asset_records.c.title,
    asset_records.c.label,
    asset_records.c.file_name,
    asset_records.c.mime_type,
    asset_records.c.origin,
    asset_records.c.page_count,
    asset_records.c.parse_status,
    asset_records.c.parse_warning,
    asset_records.c.text_truncated,
    asset_records.c.size_bytes,
    asset_records.c.server_file_id,
    asset_records.c.parser_id,
    asset_records.c.prepared_parser_id,
    asset_records.c.prepared_parser_revision,
    asset_records.c.prepared_content_hash,
    asset_records.c.prepared_at,
    asset_records.c.lifecycle_status,
    asset_records.c.deletion_operation_id,
    asset_records.c.deletion_stage,
    asset_records.c.deletion_error,
    asset_records.c.upload_status,
    asset_records.c.upload_error,
    asset_records.c.upload_operation_id,
    asset_records.c.created_at,
    asset_records.c.updated_at,
)


class PostgresAssetStore(BaseSessionStore):
    """Durable :class:`~inqtrix.project.asset_records_ports.AssetStore`.

    Inherits the engine + tenant-session lifecycle from
    :class:`~inqtrix.project.base_session_store.BaseSessionStore`.
    """

    # -- sections --------------------------------------------------------- #

    async def upsert_section(
        self, *, id, kind, title, created_at, updated_at,
        created_by_user_id: uuid.UUID | None, workspace_id
    ) -> AssetSection:
        insert_stmt = pg_insert(asset_sections).values(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, kind=kind, title=title,
            created_at=created_at, updated_at=updated_at,
            semantic_role="custom",
        )
        same_presentation = and_(
            asset_sections.c.kind == insert_stmt.excluded.kind,
            asset_sections.c.title == insert_stmt.excluded.title,
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[asset_sections.c.id],
            set_={
                "kind": insert_stmt.excluded.kind,
                "title": insert_stmt.excluded.title,
                "updated_at": insert_stmt.excluded.updated_at,
                # The role is server-owned.  An ordinary PUT cannot assign a
                # prepared role; changing a prepared section's presentation
                # explicitly turns it into an ordinary custom section.
                "semantic_role": case(
                    (same_presentation, asset_sections.c.semantic_role),
                    else_="custom",
                ),
            },
            where=and_(
                asset_sections.c.tenant_id == insert_stmt.excluded.tenant_id,
                asset_sections.c.created_by_user_id.is_not_distinct_from(
                    insert_stmt.excluded.created_by_user_id
                ),
                asset_sections.c.workspace_id.is_not_distinct_from(
                    insert_stmt.excluded.workspace_id
                ),
            ),
        ).returning(asset_sections)
        async with self._session() as session:
            await lock_section_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                section_id=id,
            )
            await _require_no_target_tombstone(
                session,
                target_kind="section",
                target_id=id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                not_found=SectionNotFound,
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                raise SectionNotFound(id)
        return self._section_from_row(row)

    async def list_sections(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id
    ) -> list[AssetSection]:
        query = _scoped_query(select(asset_sections), asset_sections, created_by_user_id, workspace_id)
        query = query.order_by(asset_sections.c.created_at.desc(), asset_sections.c.id.desc())
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [self._section_from_row(row) for row in rows]

    async def ensure_default_sections(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id,
    ) -> list[AssetSection]:
        """Atomically converge concurrent clients on the prepared roles."""

        now = time.time()
        roles = tuple(spec[0] for spec in DEFAULT_ASSET_SECTION_SPECS)
        async with self._session() as session:
            # Every caller uses the same role order.  The partial unique index
            # serializes a same-scope race; no title participates in identity.
            for role, kind, title in DEFAULT_ASSET_SECTION_SPECS:
                await session.execute(
                    pg_insert(asset_sections)
                    .values(
                        id=f"fsec_{uuid.uuid4().hex}",
                        tenant_id=_DEFAULT_TENANT,
                        created_by_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                        kind=kind,
                        title=title,
                        semantic_role=role,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing()
                )
            rows = (
                await session.execute(
                    _scoped_query(
                        select(asset_sections),
                        asset_sections,
                        created_by_user_id,
                        workspace_id,
                    ).where(asset_sections.c.semantic_role.in_(roles))
                )
            ).all()
            by_role = {
                row.semantic_role: self._section_from_row(row) for row in rows
            }
            missing = [role for role in roles if role not in by_role]
            if missing:
                raise RuntimeError(
                    "default asset-section identity did not converge: "
                    + ", ".join(missing)
                )
            return [by_role[role] for role in roles]

    async def delete_section(
        self, section_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=asset_sections, resource_id=section_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=SectionNotFound,
            )

    # -- groups ----------------------------------------------------------- #

    async def upsert_group(
        self, *, id, section_id, title, created_at, updated_at,
        created_by_user_id: uuid.UUID | None, workspace_id
    ) -> AssetGroup:
        stmt = scoped_postgres_upsert(pg_insert(asset_groups), asset_groups, dict(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, section_id=section_id, title=title,
            created_at=created_at, updated_at=updated_at,
        ), ["section_id", "title", "updated_at"]).returning(asset_groups)
        async with self._session() as session:
            await lock_section_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                section_id=section_id,
            )
            await lock_group_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                group_id=id,
            )
            await _require_no_target_tombstone(
                session,
                target_kind="section",
                target_id=section_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                not_found=SectionNotFound,
            )
            await _require_no_target_tombstone(
                session,
                target_kind="group",
                target_id=id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                not_found=GroupNotFound,
            )
            await require_scoped_parent(
                session,
                table=asset_sections,
                parent_id=section_id,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                not_found=SectionNotFound,
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                raise GroupNotFound(id)
        return self._group_from_row(row)

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id
    ) -> list[AssetGroup]:
        query = _scoped_query(select(asset_groups), asset_groups, created_by_user_id, workspace_id)
        query = query.order_by(asset_groups.c.created_at.desc(), asset_groups.c.id.desc())
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [self._group_from_row(row) for row in rows]

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=asset_groups, resource_id=group_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=GroupNotFound,
            )

    # -- assets ----------------------------------------------------------- #

    async def upsert_asset(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, parser_id=None, extracted_text, created_at,
        updated_at, created_by_user_id: uuid.UUID | None, workspace_id,
        initial_upload_status: str = "ready",
    ) -> AssetRecord:
        ensure_initial_upload_status(initial_upload_status)
        values = dict(
            id=id, tenant_id=_DEFAULT_TENANT, created_by_user_id=created_by_user_id,
            workspace_id=workspace_id, section_id=section_id, group_id=group_id,
            title=title, label=label, file_name=file_name, mime_type=mime_type,
            origin=origin, page_count=page_count, parse_status=parse_status,
            parse_warning=parse_warning, text_truncated=1 if text_truncated else 0,
            size_bytes=size_bytes, server_file_id=server_file_id,
            parser_id=parser_id, extracted_text=extracted_text,
            created_at=created_at, updated_at=updated_at,
            # INSERT-only intent from the caller: reserve_upload passes
            # "awaiting_upload" so a reservation NEVER exists as 'ready'
            # without bytes -- not even between two transactions. NOT in
            # `mutable` below, so an existing row keeps its stored status
            # and a repeated reservation cannot reset a finalised one.
            upload_status=initial_upload_status,
        )
        mutable = ["section_id", "group_id", "title", "label", "origin",
                   "page_count", "parse_status",
                   "parse_warning", "text_truncated",
                   "parser_id", "extracted_text", "updated_at"]
        stmt = scoped_postgres_upsert(
            pg_insert(asset_records),
            asset_records,
            values,
            mutable,
            extra_condition=asset_records.c.lifecycle_status == "active",
        ).returning(asset_records)
        async with self._session() as session:
            await lock_section_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                section_id=section_id,
            )
            await _require_no_target_tombstone(
                session,
                target_kind="section",
                target_id=section_id,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                not_found=SectionNotFound,
            )
            if group_id is not None:
                await lock_group_lifecycle(
                    session,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    group_id=group_id,
                )
                await _require_no_target_tombstone(
                    session,
                    target_kind="group",
                    target_id=group_id,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    not_found=GroupNotFound,
                )
            await lock_asset_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                asset_id=id,
            )
            try:
                await _SOURCE_AUTHORITY.register_active_in_session(
                    session,
                    SourceScope(
                        tenant_id=_DEFAULT_TENANT,
                        source_id=f"asset:{id}",
                        owner_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                    ),
                )
            except SourceLifecycleConflict as exc:
                raise AssetDeletionInProgress(id) from exc
            tombstone = (
                await session.execute(
                    select(deletion_operation_assets.c.asset_id)
                    .where(
                        deletion_operation_assets.c.tenant_id == _DEFAULT_TENANT,
                        deletion_operation_assets.c.asset_id == id,
                        deletion_operation_assets.c.created_by_user_id.is_not_distinct_from(
                            created_by_user_id
                        ),
                        deletion_operation_assets.c.workspace_id.is_not_distinct_from(
                            workspace_id
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if tombstone is not None:
                raise AssetDeletionInProgress(id)
            await require_scoped_parent(
                session,
                table=asset_sections,
                parent_id=section_id,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
                not_found=SectionNotFound,
            )
            if group_id is not None:
                await require_scoped_parent(
                    session,
                    table=asset_groups,
                    parent_id=group_id,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    not_found=GroupNotFound,
                    extra_condition=asset_groups.c.section_id == section_id,
                )
            row = (await session.execute(stmt)).first()
            if row is None:
                raise AssetNotFound(id)
        return self._asset_from_row(row)

    async def finalize_asset_upload(
        self, *, id, section_id, group_id, title, label, file_name, mime_type,
        origin, page_count, parse_status, parse_warning, text_truncated,
        size_bytes, server_file_id, parser_id, created_at, updated_at, scope,
        upload_operation_id=None,
    ) -> AssetRecord:
        del created_at
        conditions = [
            asset_records.c.tenant_id == _DEFAULT_TENANT,
            asset_records.c.id == id,
            asset_records.c.created_by_user_id.is_not_distinct_from(
                scope.created_by_user_id
            ),
            asset_records.c.workspace_id.is_not_distinct_from(scope.workspace_id),
            asset_records.c.section_id == section_id,
            asset_records.c.group_id.is_not_distinct_from(group_id),
            asset_records.c.lifecycle_status == "active",
            asset_records.c.deletion_operation_id.is_(None),
            asset_records.c.server_file_id.is_(None),
        ]
        try:
            async with self._session() as session:
                await lock_section_lifecycle(
                    session,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                    section_id=section_id,
                )
                await _require_no_target_tombstone(
                    session,
                    target_kind="section",
                    target_id=section_id,
                    created_by_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                    not_found=SectionNotFound,
                )
                if group_id is not None:
                    await lock_group_lifecycle(
                        session,
                        tenant_id=_DEFAULT_TENANT,
                        created_by_user_id=scope.created_by_user_id,
                        workspace_id=scope.workspace_id,
                        group_id=group_id,
                    )
                    await _require_no_target_tombstone(
                        session,
                        target_kind="group",
                        target_id=group_id,
                        created_by_user_id=scope.created_by_user_id,
                        workspace_id=scope.workspace_id,
                        not_found=GroupNotFound,
                    )
                await lock_asset_lifecycle(
                    session,
                    tenant_id=_DEFAULT_TENANT,
                    created_by_user_id=scope.created_by_user_id,
                    workspace_id=scope.workspace_id,
                    asset_id=id,
                )
                try:
                    await _SOURCE_AUTHORITY.active_write(
                        session,
                        SourceScope(
                            tenant_id=_DEFAULT_TENANT,
                            source_id=f"asset:{id}",
                            owner_user_id=scope.created_by_user_id,
                            workspace_id=scope.workspace_id,
                        ),
                        create_if_missing=False,
                    )
                except SourceLifecycleConflict as exc:
                    raise AssetDeletionInProgress(id) from exc
                row = (
                    await session.execute(
                        update(asset_records)
                        .where(*conditions)
                        .values(
                            title=title,
                            label=label,
                            file_name=file_name,
                            mime_type=mime_type,
                            origin=origin,
                            page_count=page_count,
                            parse_status=parse_status,
                            parse_warning=parse_warning,
                            text_truncated=1 if text_truncated else 0,
                            size_bytes=size_bytes,
                            server_file_id=server_file_id,
                            parser_id=parser_id,
                            upload_status=(
                                "finalizing" if upload_operation_id else "ready"
                            ),
                            upload_error=None,
                            upload_operation_id=upload_operation_id,
                            updated_at=updated_at,
                        )
                        .returning(asset_records)
                    )
                ).first()
                if row is None:
                    current = (
                        await session.execute(
                            select(asset_records).where(
                                asset_records.c.tenant_id == _DEFAULT_TENANT,
                                asset_records.c.id == id,
                                asset_records.c.created_by_user_id.is_not_distinct_from(
                                    scope.created_by_user_id
                                ),
                                asset_records.c.workspace_id.is_not_distinct_from(
                                    scope.workspace_id
                                ),
                            )
                        )
                    ).first()
                    if current is not None and current.lifecycle_status != "active":
                        raise AssetDeletionInProgress(id)
                    if current is not None and current.server_file_id == server_file_id:
                        row = current
                    elif current is not None and current.server_file_id is not None:
                        raise AssetUploadConflict(id)
                    else:
                        raise AssetNotFound(id)
        except IntegrityError as exc:
            raise AssetUploadConflict(server_file_id) from exc
        return self._asset_from_row(row)

    async def tombstone_asset_id(
        self, asset_id: str, *, scope: ResourceScope
    ) -> None:
        # Durable tombstones are inserted atomically by the deletion-operation
        # store while holding the same advisory lock.
        del asset_id, scope

    async def tombstone_section_id(
        self, section_id: str, *, scope: ResourceScope
    ) -> None:
        # Durable target receipts are inserted atomically by the operation
        # store while holding the section lifecycle lock.
        del section_id, scope

    async def tombstone_group_id(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        # Durable target receipts are inserted atomically by the operation
        # store while holding the group lifecycle lock.
        del group_id, scope

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
        async with self._session() as session:
            await lock_asset_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                asset_id=asset_id,
            )
            try:
                await _SOURCE_AUTHORITY.active_write(
                    session,
                    SourceScope(
                        tenant_id=_DEFAULT_TENANT,
                        source_id=f"asset:{asset_id}",
                        owner_user_id=scope.created_by_user_id,
                        workspace_id=scope.workspace_id,
                    ),
                    create_if_missing=False,
                )
            except SourceLifecycleConflict as exc:
                raise AssetDeletionInProgress(asset_id) from exc
            conditions = [
                asset_records.c.tenant_id == _DEFAULT_TENANT,
                asset_records.c.id == asset_id,
                asset_records.c.created_by_user_id.is_not_distinct_from(
                    scope.created_by_user_id
                ),
                asset_records.c.workspace_id.is_not_distinct_from(
                    scope.workspace_id
                ),
                asset_records.c.lifecycle_status == "active",
                asset_records.c.deletion_operation_id.is_(None),
            ]
            if expected_upload_operation_id is not None:
                conditions.append(
                    asset_records.c.upload_operation_id
                    == expected_upload_operation_id
                )
            row = (
                await session.execute(
                    update(asset_records)
                    .where(*conditions)
                    .values(
                        upload_status=upload_status,
                        upload_error=upload_error,
                        upload_operation_id=upload_operation_id,
                        updated_at=time.time(),
                    )
                    .returning(asset_records)
                )
            ).first()
        if row is None:
            if expected_upload_operation_id is not None:
                raise AssetUploadConflict(asset_id)
            raise AssetDeletionInProgress(asset_id)
        return self._asset_from_row(row)

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
            values={
                "extracted_text": text,
                "parser_id": parser_id,
                "parse_status": "parsed",
                "parse_warning": None,
                "text_truncated": 0,
                "prepared_text": text,
                "prepared_parser_id": parser_id,
                "prepared_parser_revision": parser_revision or None,
                "prepared_content_hash": content_hash,
                "prepared_file_sha256": file_sha256,
                "prepared_page_texts": list(page_texts or []),
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
            values={
                "parse_status": "error",
                "parse_warning": message,
                "prepared_text": None,
                "prepared_parser_id": None,
                "prepared_parser_revision": None,
                "prepared_content_hash": None,
                "prepared_file_sha256": None,
                # SQL NULL: a plain JSON column stores Python None as JSON
                # null, which the prepared-text identity check rejects.
                "prepared_page_texts": null(),
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
        values: dict,
    ) -> AssetRecord:
        async with self._session() as session:
            await lock_asset_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                asset_id=asset_id,
            )
            try:
                await _SOURCE_AUTHORITY.active_write(
                    session,
                    SourceScope(
                        tenant_id=_DEFAULT_TENANT,
                        source_id=f"asset:{asset_id}",
                        owner_user_id=scope.created_by_user_id,
                        workspace_id=scope.workspace_id,
                    ),
                    create_if_missing=False,
                )
            except SourceLifecycleConflict as exc:
                raise AssetDeletionInProgress(asset_id) from exc
            row = (
                await session.execute(
                    update(asset_records)
                    .where(
                        asset_records.c.tenant_id == _DEFAULT_TENANT,
                        asset_records.c.id == asset_id,
                        asset_records.c.created_by_user_id.is_not_distinct_from(
                            scope.created_by_user_id
                        ),
                        asset_records.c.workspace_id.is_not_distinct_from(
                            scope.workspace_id
                        ),
                        asset_records.c.lifecycle_status == "active",
                        asset_records.c.deletion_operation_id.is_(None),
                        asset_records.c.server_file_id == server_file_id,
                        asset_records.c.upload_operation_id
                        == expected_upload_operation_id,
                    )
                    .values(**values)
                    .returning(asset_records)
                )
            ).first()
        if row is None:
            raise AssetUploadConflict(asset_id)
        return self._asset_from_row(row)

    async def list_assets_page(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id, limit, after
    ) -> tuple[list[AssetRecord], str | None]:
        query = _scoped_query(select(*_ASSET_META_COLUMNS), asset_records, created_by_user_id, workspace_id)
        if after is not None:
            query = query.where(
                tuple_(asset_records.c.created_at, asset_records.c.id)
                < tuple_(after[0], after[1])
            )
        query = query.order_by(
            asset_records.c.created_at.desc(), asset_records.c.id.desc()
        ).limit(limit + 1)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        assets = [self._asset_from_row(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(assets[-1].created_at, assets[-1].id)
            if len(rows) > limit and assets else None
        )
        return assets, next_cursor

    async def get_asset(self, asset_id: str) -> AssetRecord:
        async with self._session() as session:
            row = (await session.execute(select(asset_records).where(
                asset_records.c.tenant_id == _DEFAULT_TENANT,
                asset_records.c.id == asset_id,
            ))).first()
        if row is None:
            raise AssetNotFound(asset_id)
        return self._asset_from_row(row)

    async def find_asset_by_server_file_id(
        self, server_file_id: str
    ) -> AssetRecord | None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(asset_records)
                    .where(
                        asset_records.c.tenant_id == _DEFAULT_TENANT,
                        asset_records.c.server_file_id == server_file_id,
                    )
                    .limit(1)
                )
            ).first()
        return self._asset_from_row(row) if row is not None else None

    async def list_assets_by_server_file_id(
        self, server_file_id: str
    ) -> list[AssetRecord]:
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(asset_records).where(
                        asset_records.c.tenant_id == _DEFAULT_TENANT,
                        asset_records.c.server_file_id == server_file_id,
                    )
                )
            ).all()
        return [self._asset_from_row(row) for row in rows]

    async def detach_server_file_for_deletion(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        operation_id: str,
        expected_server_file_id: str,
    ) -> AssetRecord:
        async with self._session() as session:
            await lock_asset_lifecycle(
                session,
                tenant_id=_DEFAULT_TENANT,
                created_by_user_id=scope.created_by_user_id,
                workspace_id=scope.workspace_id,
                asset_id=asset_id,
            )
            conditions = [
                asset_records.c.tenant_id == _DEFAULT_TENANT,
                asset_records.c.id == asset_id,
                asset_records.c.created_by_user_id.is_not_distinct_from(
                    scope.created_by_user_id
                ),
                asset_records.c.workspace_id.is_not_distinct_from(
                    scope.workspace_id
                ),
            ]
            current = (
                await session.execute(
                    select(asset_records).where(*conditions).with_for_update()
                )
            ).first()
            if current is None:
                raise AssetNotFound(asset_id)
            if (
                current.lifecycle_status not in {"deleting", "delete_failed"}
                or current.deletion_operation_id != operation_id
            ):
                raise AssetDeletionInProgress(asset_id)
            if current.server_file_id is None:
                return self._asset_from_row(current)
            if current.server_file_id != expected_server_file_id:
                raise RuntimeError(
                    "asset file binding changed during deletion"
                )
            detached = (
                await session.execute(
                    update(asset_records)
                    .where(*conditions)
                    .values(
                        server_file_id=None,
                        updated_at=time.time(),
                    )
                    .returning(asset_records)
                )
            ).first()
            if detached is None:
                raise AssetNotFound(asset_id)
        return self._asset_from_row(detached)

    async def list_assets_by_ids(
        self,
        asset_ids: tuple[str, ...],
        *,
        scope: ResourceScope,
    ) -> list[AssetRecord]:
        if not asset_ids:
            return []
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(asset_records).where(
                        asset_records.c.tenant_id == _DEFAULT_TENANT,
                        asset_records.c.id.in_(asset_ids),
                        asset_records.c.created_by_user_id.is_not_distinct_from(
                            scope.created_by_user_id
                        ),
                        asset_records.c.workspace_id.is_not_distinct_from(
                            scope.workspace_id
                        ),
                    )
                )
            ).all()
        by_id = {row.id: self._asset_from_row(row) for row in rows}
        return [by_id[asset_id] for asset_id in asset_ids if asset_id in by_id]

    async def delete_asset(
        self, asset_id: str, *, scope: ResourceScope
    ) -> None:
        async with self._session() as session:
            await delete_scoped_postgres(
                session, table=asset_records, resource_id=asset_id,
                tenant_id=_DEFAULT_TENANT, scope=scope,
                not_found=AssetNotFound,
            )

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
        conditions = [
            asset_records.c.tenant_id == _DEFAULT_TENANT,
            asset_records.c.id == asset_id,
            asset_records.c.created_by_user_id.is_not_distinct_from(
                scope.created_by_user_id
            ),
            asset_records.c.workspace_id.is_not_distinct_from(
                scope.workspace_id
            ),
        ]
        stmt = (
            update(asset_records)
            .where(*conditions)
            .values(
                lifecycle_status=lifecycle_status,
                deletion_operation_id=deletion_operation_id,
                deletion_stage=deletion_stage,
                deletion_error=deletion_error,
                updated_at=time.time(),
            )
            .returning(asset_records)
        )
        async with self._session() as session:
            row = (await session.execute(stmt)).first()
        if row is None:
            raise AssetNotFound(asset_id)
        return self._asset_from_row(row)

    async def list_assets_for_target(
        self,
        *,
        section_id: str | None,
        group_id: str | None,
        scope: ResourceScope,
    ) -> list[AssetRecord]:
        query = select(asset_records).where(
            asset_records.c.tenant_id == _DEFAULT_TENANT,
            asset_records.c.created_by_user_id.is_not_distinct_from(
                scope.created_by_user_id
            ),
            asset_records.c.workspace_id.is_not_distinct_from(
                scope.workspace_id
            ),
        )
        if section_id is not None:
            query = query.where(asset_records.c.section_id == section_id)
        if group_id is not None:
            query = query.where(asset_records.c.group_id == group_id)
        async with self._session() as session:
            rows = (await session.execute(query)).all()
        return [self._asset_from_row(row) for row in rows]

    # -- row mapping ------------------------------------------------------ #

    @staticmethod
    def _section_from_row(row) -> AssetSection:
        return AssetSection(
            id=row.id, kind=row.kind, title=row.title, created_at=row.created_at,
            updated_at=row.updated_at, tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id, workspace_id=row.workspace_id,
            semantic_role=row.semantic_role,
        )

    @staticmethod
    def _group_from_row(row) -> AssetGroup:
        return AssetGroup(
            id=row.id, section_id=row.section_id, title=row.title,
            created_at=row.created_at, updated_at=row.updated_at,
            tenant_id=row.tenant_id, created_by_user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
        )

    @staticmethod
    def _asset_from_row(row) -> AssetRecord:
        return AssetRecord(
            id=row.id, section_id=row.section_id, group_id=row.group_id,
            title=row.title, label=row.label, file_name=row.file_name,
            mime_type=row.mime_type, origin=row.origin, page_count=row.page_count,
            parse_status=row.parse_status, parse_warning=row.parse_warning,
            text_truncated=bool(row.text_truncated), size_bytes=row.size_bytes,
            server_file_id=row.server_file_id,
            parser_id=row.parser_id,
            extracted_text=getattr(row, "extracted_text", "") or "",
            created_at=row.created_at, updated_at=row.updated_at,
            tenant_id=row.tenant_id, created_by_user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
            lifecycle_status=getattr(row, "lifecycle_status", "active") or "active",
            deletion_operation_id=getattr(row, "deletion_operation_id", None),
            deletion_stage=getattr(row, "deletion_stage", None),
            deletion_error=getattr(row, "deletion_error", None),
            upload_status=getattr(row, "upload_status", "ready") or "ready",
            upload_error=getattr(row, "upload_error", None),
            upload_operation_id=getattr(row, "upload_operation_id", None),
            prepared_text=getattr(row, "prepared_text", "") or "",
            prepared_parser_id=getattr(row, "prepared_parser_id", None),
            prepared_parser_revision=getattr(row, "prepared_parser_revision", None),
            prepared_content_hash=getattr(row, "prepared_content_hash", None),
            prepared_file_sha256=getattr(row, "prepared_file_sha256", None),
            prepared_page_texts=tuple(
                getattr(row, "prepared_page_texts", None) or ()
            ),
            prepared_at=getattr(row, "prepared_at", None),
        )


def _scoped_query(
    query, table, created_by_user_id: uuid.UUID | None, workspace_id
):
    query = query.where(table.c.tenant_id == _DEFAULT_TENANT)
    if created_by_user_id is not None:
        query = query.where(table.c.created_by_user_id == created_by_user_id)
    if workspace_id is not None:
        query = query.where(table.c.workspace_id == workspace_id)
    return query


async def _require_no_target_tombstone(
    session,
    *,
    target_kind: str,
    target_id: str,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
    not_found,
) -> None:
    receipt = (
        await session.execute(
            select(deletion_operations.c.operation_id)
            .where(
                deletion_operations.c.tenant_id == _DEFAULT_TENANT,
                deletion_operations.c.target_kind == target_kind,
                deletion_operations.c.target_id == target_id,
                deletion_operations.c.created_by_user_id.is_not_distinct_from(
                    created_by_user_id
                ),
                deletion_operations.c.workspace_id.is_not_distinct_from(
                    workspace_id
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if receipt is not None:
        raise not_found(target_id)
