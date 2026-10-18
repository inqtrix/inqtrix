"""Durable editor-patch store (Postgres tier).

Lockstep counterpart of
:class:`~inqtrix.project.editor_patch_memory.MemoryEditorPatchStore` —
same port, same error types, same ordering. HTTP-loop only (NullPool
engine via :class:`~inqtrix.project.base_session_store.BaseSessionStore`);
the ``mark_*`` decision writes are single-row CAS updates guarded by
``status = 'pending'`` so a concurrent decision loses deterministically
with :class:`~inqtrix.project.editor_patch_ports.PatchAlreadyDecided`.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select, update

from inqtrix.project.base_session_store import BaseSessionStore
from inqtrix.project.editor_patch_ports import (
    EditorPatchRecord,
    PatchAlreadyDecided,
    PatchNotFound,
)
from inqtrix.storage.editor_patch_orm import (
    editor_patch_created_at_ms,
    editor_patches,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PostgresEditorPatchStore(BaseSessionStore):
    """Postgres :class:`~inqtrix.project.editor_patch_ports.EditorPatchStore`."""

    async def create(self, patch: EditorPatchRecord) -> EditorPatchRecord:
        async with self._session() as session:
            created_at = patch.created_at or time.time()
            await session.execute(
                insert(editor_patches).values(
                    patch_id=patch.patch_id,
                    document_id=patch.document_id,
                    run_id=patch.run_id,
                    source=patch.source,
                    status=patch.status,
                    edits=[dict(edit) for edit in patch.edits],
                    summary=patch.summary,
                    warnings=list(patch.warnings),
                    revision_before=patch.revision_before,
                    collaboration_generation=patch.collaboration_generation,
                    base_sequence=patch.base_sequence,
                    decision_sequence=patch.decision_sequence,
                    suggestion_ids=list(patch.suggestion_ids),
                    applied_revision=patch.applied_revision,
                    applied_edit_ids=(
                        list(patch.applied_edit_ids)
                        if patch.applied_edit_ids is not None
                        else None
                    ),
                    note=patch.note,
                    created_by_user_id=patch.created_by_user_id,
                    decided_by_user_id=patch.decided_by_user_id,
                    command_id=patch.command_id,
                    created_at=created_at,
                    decided_at=patch.decided_at,
                )
            )
            return replace(patch, created_at=created_at)

    async def get(self, patch_id: str) -> EditorPatchRecord:
        async with self._session() as session:
            return await _get_patch_tx(session, patch_id)

    async def list_for_document(
        self, document_id: str, *, status: str | None = None
    ) -> list[EditorPatchRecord]:
        async with self._session() as session:
            query = select(editor_patches).where(
                editor_patches.c.document_id == document_id
            )
            if status is not None:
                query = query.where(editor_patches.c.status == status)
            rows = (
                (
                    await session.execute(
                        query.order_by(
                            editor_patch_created_at_ms.desc(),
                            editor_patches.c.patch_id.desc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
            return [_patch_from_row(row) for row in rows]

    async def mark_applied(
        self,
        patch_id: str,
        *,
        applied_revision: int,
        applied_edit_ids: list[str],
        decision_sequence: int | None = None,
        decided_by_user_id: uuid.UUID | None = None,
        command_id: uuid.UUID | None = None,
    ) -> EditorPatchRecord:
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        update(editor_patches)
                        .where(
                            editor_patches.c.patch_id == patch_id,
                            editor_patches.c.status == "pending",
                        )
                        .values(
                            status="accepted",
                            applied_revision=applied_revision,
                            applied_edit_ids=list(applied_edit_ids),
                            decision_sequence=decision_sequence,
                            decided_by_user_id=decided_by_user_id,
                            command_id=command_id,
                            decided_at=time.time(),
                        )
                        .returning(editor_patches)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise await _patch_cas_miss(session, patch_id)
            return _patch_from_row(row)

    async def mark_rejected(
        self,
        patch_id: str,
        *,
        note: str,
        decision_sequence: int | None = None,
        decided_by_user_id: uuid.UUID | None = None,
        command_id: uuid.UUID | None = None,
    ) -> EditorPatchRecord:
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        update(editor_patches)
                        .where(
                            editor_patches.c.patch_id == patch_id,
                            editor_patches.c.status == "pending",
                        )
                        .values(
                            status="rejected",
                            note=note,
                            decision_sequence=decision_sequence,
                            decided_by_user_id=decided_by_user_id,
                            command_id=command_id,
                            decided_at=time.time(),
                        )
                        .returning(editor_patches)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise await _patch_cas_miss(session, patch_id)
            return _patch_from_row(row)


async def _patch_cas_miss(session: "AsyncSession", patch_id: str) -> Exception:
    stored = await _get_patch_tx(session, patch_id)
    return PatchAlreadyDecided(stored)


async def _get_patch_tx(session: "AsyncSession", patch_id: str) -> EditorPatchRecord:
    row = (
        (
            await session.execute(
                select(editor_patches).where(
                    editor_patches.c.patch_id == patch_id
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PatchNotFound(patch_id)
    return _patch_from_row(row)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return json.loads(value or "[]")
    return list(value or [])


def _patch_from_row(row: Any) -> EditorPatchRecord:
    applied_edit_ids = row["applied_edit_ids"]
    return EditorPatchRecord(
        patch_id=row["patch_id"],
        document_id=row["document_id"],
        run_id=row["run_id"],
        source=row["source"],
        status=row["status"],
        edits=tuple(_json_list(row["edits"])),
        summary=row["summary"],
        warnings=tuple(_json_list(row["warnings"])),
        revision_before=row["revision_before"],
        collaboration_generation=row["collaboration_generation"],
        base_sequence=row["base_sequence"],
        decision_sequence=row["decision_sequence"],
        suggestion_ids=tuple(_json_list(row["suggestion_ids"])),
        applied_revision=row["applied_revision"],
        applied_edit_ids=(
            tuple(_json_list(applied_edit_ids))
            if applied_edit_ids is not None
            else None
        ),
        note=row["note"],
        created_by_user_id=row["created_by_user_id"],
        decided_by_user_id=row["decided_by_user_id"],
        command_id=row["command_id"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )
