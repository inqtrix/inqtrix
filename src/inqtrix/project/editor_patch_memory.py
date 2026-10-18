"""In-memory editor-patch store (offline/test tier).

Lockstep counterpart of
:class:`~inqtrix.storage.editor_patch_postgres.PostgresEditorPatchStore`:
same port, same error types, same ordering. Thread-safe via one lock —
the workspace-agent runtime proposes patches from worker threads (the
``control_memory`` precedent); no awaits happen while the lock is held.

Retention mirrors the Postgres ``ON DELETE CASCADE`` only logically:
patches of a deleted document become unreachable (the service resolves
the document first on every access), and process lifetime bounds growth.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace

from inqtrix.project.editor_patch_ports import (
    EditorPatchRecord,
    PatchAlreadyDecided,
    PatchNotFound,
    patch_created_at_ms,
)


class MemoryEditorPatchStore:
    """Dict-backed :class:`~inqtrix.project.editor_patch_ports.EditorPatchStore`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._patches: dict[str, EditorPatchRecord] = {}

    async def create(self, patch: EditorPatchRecord) -> EditorPatchRecord:
        with self._lock:
            stored = replace(
                patch, created_at=patch.created_at or time.time()
            )
            self._patches[stored.patch_id] = stored
            return stored

    async def get(self, patch_id: str) -> EditorPatchRecord:
        with self._lock:
            patch = self._patches.get(patch_id)
            if patch is None:
                raise PatchNotFound(patch_id)
            return patch

    async def list_for_document(
        self, document_id: str, *, status: str | None = None
    ) -> list[EditorPatchRecord]:
        with self._lock:
            return sorted(
                (
                    p
                    for p in self._patches.values()
                    if p.document_id == document_id
                    and (status is None or p.status == status)
                ),
                key=lambda p: (patch_created_at_ms(p.created_at), p.patch_id),
                reverse=True,
            )

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
        with self._lock:
            patch = self._require_pending_locked(patch_id)
            decided = replace(
                patch,
                status="accepted",
                applied_revision=applied_revision,
                applied_edit_ids=tuple(applied_edit_ids),
                decision_sequence=decision_sequence,
                decided_by_user_id=decided_by_user_id,
                command_id=command_id,
                decided_at=time.time(),
            )
            self._patches[patch_id] = decided
            return decided

    async def mark_rejected(
        self,
        patch_id: str,
        *,
        note: str,
        decision_sequence: int | None = None,
        decided_by_user_id: uuid.UUID | None = None,
        command_id: uuid.UUID | None = None,
    ) -> EditorPatchRecord:
        with self._lock:
            patch = self._require_pending_locked(patch_id)
            decided = replace(
                patch,
                status="rejected",
                note=note,
                decision_sequence=decision_sequence,
                decided_by_user_id=decided_by_user_id,
                command_id=command_id,
                decided_at=time.time(),
            )
            self._patches[patch_id] = decided
            return decided

    def _require_pending_locked(self, patch_id: str) -> EditorPatchRecord:
        patch = self._patches.get(patch_id)
        if patch is None:
            raise PatchNotFound(patch_id)
        if patch.status != "pending":
            raise PatchAlreadyDecided(patch)
        return patch

    async def aclose(self) -> None:
        """No-op; symmetric with the Postgres store."""
