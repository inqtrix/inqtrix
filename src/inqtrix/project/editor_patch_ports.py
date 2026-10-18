"""Contracts of the editor-patch store.

One persisted proposal of anchored document edits (from the editor
assistant's suggest/instruct calls or the workspace agent) with its
apply/reject lifecycle. Mirrors :mod:`inqtrix.project.editor_ports`:
the store owns persistence only; the parent-document visibility rule and
the CAS apply live in
:class:`~inqtrix.services.editor_patch_service.EditorPatchService`, the
wire shape in the router. Two implementations behind the same port:
:class:`~inqtrix.project.editor_patch_memory.MemoryEditorPatchStore`
(offline/test tier) and
:class:`~inqtrix.storage.editor_patch_postgres.PostgresEditorPatchStore`.

Retention: a patch is a child of its DOCUMENT (``ON DELETE CASCADE`` in
Postgres; the memory tier mirrors it logically — every access resolves
the document first, so patches of a deleted document become unreachable,
the ``control_memory`` precedent). The optional ``run_id`` back-reference
uses ``ON DELETE SET NULL`` instead: the durable run retention window may
expire the run long before the document dies, and the patch record (the
applied-edit truth) must survive that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

PATCH_SOURCES = ("suggest", "instruct", "agent", "human")
"""Proposal representation and origin.

``suggest``/``instruct``/``agent`` retain the assistant's exact anchored
edits. ``human`` stores sidecar-validated descriptors for a patch materialized
through the live collaboration document; the attributed user remains its
creator even when a creator-private assistant draft supplied the content. The
ORM builds its CHECK constraint from this tuple.
"""

PATCH_STATUSES = ("pending", "accepted", "rejected")
"""Lifecycle of one patch. ``accepted`` means the edits were applied
server-side to the document (``applied_revision`` / ``applied_edit_ids``
carry the outcome); both decisions are terminal."""

UNIX_SECONDS_UPPER_BOUND = 100_000_000_000
"""Below this a patch's ``created_at`` is Unix seconds; at or above it,
epoch milliseconds. The bound is the year 5138 in seconds and March 1973 in
milliseconds, so no real time of either unit falls on the wrong side."""


def patch_created_at_ms(created_at: float) -> float:
    """A patch's creation time in epoch milliseconds, the one unit patches
    are ordered by.

    A proposal through the patch API is stamped in Unix seconds, the API's
    unit. A patch materialized from a collaboration suggestion carries the
    suggestion's time: epoch milliseconds for a typed suggestion and a
    published AI suggestion, Unix seconds for AI suggestions written before
    the collaboration service moved to milliseconds. Every order by creation
    time reads it through this (in SQL: ``editor_patch_created_at_ms``); the
    stored and returned value stays as written.
    """
    return created_at * 1_000 if created_at < UNIX_SECONDS_UPPER_BOUND else created_at


class PatchNotFound(KeyError):
    """Raised when a patch id is unknown for the caller (HTTP 404).

    Also the indistinct denial when the patch exists but its parent
    document is not visible to the caller — denial and absence stay
    byte-identical, like the document routes.
    """


class PatchAlreadyDecided(Exception):
    """Raised when a decision hits a patch that is no longer pending.

    Carries the stored record so the caller can distinguish a replay of
    the SAME decision (idempotent 200) from a conflicting one (409) —
    the ``ApprovalAlreadyDecided`` replay pattern.
    """

    def __init__(self, patch: "EditorPatchRecord") -> None:
        super().__init__(f"patch {patch.patch_id} already decided")
        self.patch = patch


class PatchRevisionConflict(Exception):
    """CAS miss on apply: the document moved past ``expected_revision``.

    Attributes:
        current_revision: The revision the document actually holds — the
            client refetches and re-anchors instead of blind-retrying.
        revision_before: The document revision the patch was proposed
            against, for the client's staleness display.
    """

    def __init__(self, current_revision: int, revision_before: int) -> None:
        super().__init__(f"document is at revision {current_revision}")
        self.current_revision = current_revision
        self.revision_before = revision_before


@dataclass(frozen=True)
class EditorPatchRecord:
    """One proposed set of anchored edits against an editor document.

    Attributes:
        patch_id: Row id (``pch_...``).
        document_id: The editor document the edits target (cascade
            parent).
        run_id: The proposing workspace-agent run, or ``None`` for
            suggest/instruct proposals (and after run retention expired
            the run — ``SET NULL``).
        source: One of :data:`PATCH_SOURCES`.
        status: One of :data:`PATCH_STATUSES`.
        edits: For assistant/agent sources, the anchored edit objects in the
            ``editor_instructions`` shape (``find``/``quote_before``/
            ``quote_after``/``position``/``text``/``note``) plus a per-edit
            id. For descriptor-backed collaboration patches, the
            sidecar-validated suggestion membership metadata.
        summary: The assistant message that accompanied the proposal.
        warnings: Visible warnings from the proposing call (anchor
            validation, truncation), carried for the review UI.
        revision_before: The document revision snapshotted at propose —
            the staleness reference for the apply CAS.
        applied_revision: The document revision AFTER a successful apply
            (``None`` until accepted).
        applied_edit_ids: The subset of edit ids that actually anchored
            and were applied (``None`` until accepted; skipped edits are
            visible as the difference).
        note: Free-text the rejecting user attached (empty otherwise).
        created_by_user_id: Canonical UUID of the user that proposed the
            patch, or ``None`` for unscoped principals.
        created_at: Creation time as its writer stamped it: Unix seconds for
            a proposal, the suggestion's time for a descriptor-backed
            collaboration patch (see :func:`patch_created_at_ms`).
        decided_at: Unix seconds of the accept/reject decision, ``None``
            while pending.
    """

    patch_id: str
    document_id: str
    run_id: str | None = None
    source: str = "instruct"
    status: str = "pending"
    edits: tuple[dict[str, Any], ...] = ()
    summary: str = ""
    warnings: tuple[str, ...] = ()
    revision_before: int = 0
    collaboration_generation: int | None = None
    base_sequence: int | None = None
    decision_sequence: int | None = None
    suggestion_ids: tuple[str, ...] = ()
    applied_revision: int | None = None
    applied_edit_ids: tuple[str, ...] | None = None
    note: str = ""
    created_by_user_id: uuid.UUID | None = None
    decided_by_user_id: uuid.UUID | None = None
    command_id: uuid.UUID | None = None
    created_at: float = 0.0
    decided_at: float | None = None


@runtime_checkable
class EditorPatchStore(Protocol):
    """Persistence port for editor patches.

    Scoping stays OUT of the store: the service resolves the parent
    document with the caller's visibility first (denial == absence), so
    the store never sees principals. The ``mark_*`` writes are the
    pending -> decided CAS — a non-pending row raises
    :class:`PatchAlreadyDecided` with the stored record so the service
    can replay idempotently.
    """

    async def create(self, patch: EditorPatchRecord) -> EditorPatchRecord:
        """Persist one new pending patch (``created_at`` defaults to now
        when the record carries 0)."""
        ...

    async def get(self, patch_id: str) -> EditorPatchRecord:
        """One patch, or :class:`PatchNotFound`."""
        ...

    async def list_for_document(
        self, document_id: str, *, status: str | None = None
    ) -> list[EditorPatchRecord]:
        """All patches of a document, newest first by
        :func:`patch_created_at_ms`, optionally filtered by status.
        Documents carry few patches — no keyset page."""
        ...

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
        """CAS ``pending -> accepted`` with the apply outcome.

        Raises :class:`PatchNotFound` for unknown ids and
        :class:`PatchAlreadyDecided` (with the stored record) when the
        patch is no longer pending.
        """
        ...

    async def mark_rejected(
        self,
        patch_id: str,
        *,
        note: str,
        decision_sequence: int | None = None,
        decided_by_user_id: uuid.UUID | None = None,
        command_id: uuid.UUID | None = None,
    ) -> EditorPatchRecord:
        """CAS ``pending -> rejected`` with the optional note.

        Same error contract as :meth:`mark_applied`.
        """
        ...

    async def aclose(self) -> None:
        """Release backing resources at application shutdown."""
        ...
