"""Editor-patch lifecycle service.

Orchestrates the patch store pair behind
:class:`~inqtrix.project.editor_patch_ports.EditorPatchStore` together
with the :class:`~inqtrix.services.editor_persistence_service.EditorPersistenceService`:
propose snapshots the document revision, apply is a CAS against the
document's CURRENT revision plus the deterministic server-side edit
application (:func:`apply_edits`), reject records the decision.

Access: EVERY method resolves the parent DOCUMENT through the editor
persistence service with the caller's ``visible_to`` context first — an
unknown or foreign document answers with the same indistinct not-found
the document routes use (denial == absence). Patch-scoped methods convert
that into :class:`PatchNotFound` so a foreign caller cannot learn that a
patch exists at all.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from inqtrix.auth.permissions import AuditEntry, SharePermission
from inqtrix.project.editor_patch_ports import (
    PATCH_SOURCES,
    PATCH_STATUSES,
    EditorPatchRecord,
    PatchAlreadyDecided,
    PatchNotFound,
    PatchRevisionConflict,
)
from inqtrix.project.editor_ports import (
    DocumentNotFound,
    DocumentRevisionConflict,
    EditorDocument,
)

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal, UserContext
    from inqtrix.project.editor_patch_ports import EditorPatchStore
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )
    from inqtrix.services.editor_collaboration_service import (
        EditorCollaborationService,
    )

log = logging.getLogger("inqtrix")

_EDIT_POSITIONS = frozenset({"replace", "before", "after", "append"})

_EDIT_TEXT_KEYS = ("find", "quote_before", "quote_after", "text", "note")
"""The string fields of one edit in the ``editor_instructions`` shape;
propose normalizes exactly these plus ``position`` and the assigned
``id``, so foreign keys in a raw payload can never be persisted."""


class EditorPatchValidationError(ValueError):
    """Raised for invalid propose/list input (maps to HTTP 400).

    The source and position domains are rejected here, before the
    database CHECK constraint, so a bad value is a clean 400 instead of
    an opaque 500 (No Silent Fallbacks).
    """


def apply_edits(
    content: str, edits: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """Apply anchored edits to a markdown document, deterministically.

    Mirrors the anchor semantics of
    :mod:`inqtrix.server.editor_instructions` and the frontend's
    anchoring intent — and NEVER guesses:

    * ``find`` must occur verbatim in the current document. When it
      occurs multiple times, ``quote_before``/``quote_after`` pick the
      occurrence whose surrounding text contains them nearest; a still
      ambiguous (tied) or unresolvable anchor SKIPS the edit (non-fatal,
      excluded from the applied ids).
    * ``replace`` substitutes ``text`` for ``find`` (empty ``text`` is a
      deletion); ``before``/``after`` insert ``text`` with a blank-line
      separation adjacent to the anchor; ``append`` (empty ``find``)
      appends ``text`` after a blank line at the document end.
    * Edits apply SEQUENTIALLY against the evolving document — later
      anchors resolve against the already-updated text.

    Args:
        content: The current document markdown.
        edits: Edit objects in the persisted shape (each carrying its
            propose-assigned ``id``); read-only here — the dicts are
            never mutated. Unknown positions or no-op inserts are
            skipped, never fatal.

    Returns:
        ``(new_content, applied_edit_ids)`` — the ids of exactly the
        edits that changed the document, in application order.
    """
    applied: list[str] = []
    for edit in edits:
        edit_id = str(edit.get("id", ""))
        position = edit.get("position")
        find = str(edit.get("find", "") or "")
        text = str(edit.get("text", "") or "")
        if position == "append" and not find:
            if not text:
                continue
            content = f"{content}\n\n{text}" if content else text
            applied.append(edit_id)
            continue
        if position not in ("replace", "before", "after") or not find:
            continue
        if position in ("before", "after") and not text:
            # Inserting nothing is a no-op, not an applied edit.
            continue
        index = _resolve_anchor(
            content,
            find,
            quote_before=str(edit.get("quote_before", "") or ""),
            quote_after=str(edit.get("quote_after", "") or ""),
        )
        if index is None:
            continue
        end = index + len(find)
        if position == "replace":
            content = content[:index] + text + content[end:]
        elif position == "before":
            content = content[:index] + text + "\n\n" + content[index:]
        else:
            content = content[:end] + "\n\n" + text + content[end:]
        applied.append(edit_id)
    return content, applied


def _resolve_anchor(
    content: str, find: str, *, quote_before: str, quote_after: str
) -> int | None:
    """The start index of the ONE occurrence the quotes select, or None.

    Single occurrence wins directly. Multiple occurrences are scored by
    the summed distance between the occurrence and its nearest matching
    quotes; occurrences missing a given quote are disqualified. A tie or
    no qualifying occurrence stays ambiguous (``None``) — skipping beats
    guessing.
    """
    occurrences: list[int] = []
    start = 0
    while True:
        index = content.find(find, start)
        if index < 0:
            break
        occurrences.append(index)
        start = index + 1
    if not occurrences:
        return None
    if len(occurrences) == 1:
        return occurrences[0]
    if not quote_before and not quote_after:
        return None
    best: int | None = None
    best_score: int | None = None
    tied = False
    for index in occurrences:
        score = 0
        if quote_before:
            before_pos = content.rfind(quote_before, 0, index)
            if before_pos < 0:
                continue
            score += index - (before_pos + len(quote_before))
        if quote_after:
            after_pos = content.find(quote_after, index + len(find))
            if after_pos < 0:
                continue
            score += after_pos - (index + len(find))
        if best_score is None or score < best_score:
            best, best_score, tied = index, score, False
        elif score == best_score:
            tied = True
    return None if tied else best


class EditorPatchService:
    """Patch lifecycle over one patch store pair + the editor persistence.

    Args:
        store: The patch store (memory or Postgres, one port).
        editor_persistence: The document service every method resolves
            the parent document through (visibility + the CAS save).
        audit: Optional audit sink (``record(AuditEntry)``); ``None``
            skips audit rows (memory/dev deployments without identity
            persistence) — the AgentControlService precedent.
        durable: Whether the backing store pair survives a restart;
            surfaced to capability consumers, never branched on here.
    """

    def __init__(
        self,
        *,
        store: "EditorPatchStore",
        editor_persistence: "EditorPersistenceService",
        collaboration: "EditorCollaborationService | None" = None,
        audit: Any = None,
        durable: bool = False,
    ) -> None:
        self._store = store
        self._editor_persistence = editor_persistence
        self._collaboration = collaboration
        self._audit = audit
        self._durable = durable

    @property
    def store(self) -> "EditorPatchStore":
        """The wired patch store (shutdown disposes its engine)."""
        return self._store

    @property
    def durable(self) -> bool:
        """Whether patches survive a restart (Postgres backend)."""
        return self._durable

    # -- lifecycle ---------------------------------------------------------- #

    async def propose(
        self,
        *,
        document_id: str,
        run_id: str | None,
        source: str,
        edits: list[dict[str, Any]],
        summary: str,
        warnings: list[str],
        created_by_user_id: uuid.UUID | None,
        visible_to: "UserContext | None",
        principal: "Principal | None" = None,
        expected_revision: int | None = None,
    ) -> EditorPatchRecord:
        """Persist one pending patch against a document the caller may edit.

        Snapshots ``revision_before`` from the document's CURRENT
        revision and assigns the per-edit ids ``ed_1..n`` in order.
        Auditing mirrors apply/reject: an agent-sourced proposal records
        ``editor.patch_proposed`` with ``actor_type='agent'``;
        ``principal`` supplies the tenant/actor and, when absent
        (memory/dev), the audit is skipped like apply/reject.

        ``expected_revision`` pins the document state the caller
        READ before proposing: when set and the document has moved on,
        the proposal is refused with the SAME conflict apply uses — a
        patch anchored against stale text must never be created quietly,
        it would only resolve against content its author never saw.
        ``None`` keeps the legacy unpinned behavior.

        Raises:
            DocumentNotFound: Unknown/foreign document (indistinct 404).
            EditorPatchValidationError: Unknown source or edit position.
            PatchRevisionConflict: ``expected_revision`` no longer matches
                the document's current revision.
        """
        if source not in PATCH_SOURCES:
            raise EditorPatchValidationError(f"unknown patch source: {source!r}")
        document = await self._editor_persistence.get_document_for_ai(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.SUGGEST,
        )
        if (
            expected_revision is not None
            and expected_revision != document.revision
        ):
            raise PatchRevisionConflict(
                current_revision=document.revision,
                revision_before=expected_revision,
            )
        record = EditorPatchRecord(
            patch_id=(
                str(uuid.uuid4())
                if document.content_mode == "collaboration"
                else f"pch_{uuid.uuid4().hex}"
            ),
            document_id=document.id,
            run_id=run_id,
            source=source,
            status="pending",
            edits=tuple(
                self._parse_edit(index, raw) for index, raw in enumerate(edits)
            ),
            summary=summary,
            warnings=tuple(warnings),
            revision_before=document.revision,
            collaboration_generation=(
                document.collaboration_generation
                if document.content_mode == "collaboration"
                else None
            ),
            base_sequence=(
                document.persisted_sequence
                if document.content_mode == "collaboration"
                else None
            ),
            created_by_user_id=(
                created_by_user_id
                or (
                    principal.user_id
                    if document.content_mode == "collaboration"
                    and principal is not None
                    else None
                )
            ),
            created_at=time.time(),
        )
        stored = await self._store.create(record)
        await self._record_audit(
            principal,
            action="editor.patch_proposed",
            document_id=document.id,
            detail={
                "patch_id": stored.patch_id,
                "source": source,
                "edit_count": str(len(stored.edits)),
            },
            actor_type="agent" if source == "agent" else "user",
        )
        return stored

    async def get_patch(
        self,
        patch_id: str,
        *,
        visible_to: "UserContext | None",
    ) -> tuple[EditorPatchRecord, int]:
        """One patch plus the document's CURRENT revision.

        The frontend applies against fresh state, so the pair travels
        together. The returned record carries the PROPOSE-TIME warnings
        (anchor/truncation notes from the proposing call) — they describe
        the proposal, not the current apply outcome. Raises
        :class:`PatchNotFound` for unknown patches AND for patches on
        documents the caller may not see (indistinct).
        """
        patch = await self._store.get(patch_id)
        document = await self._patch_document(
            patch,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        self._require_private_patch_access(
            patch, document=document, visible_to=visible_to
        )
        return patch, document.revision

    async def list_for_document(
        self,
        document_id: str,
        *,
        status: str | None,
        visible_to: "UserContext | None",
    ) -> list[EditorPatchRecord]:
        """All patches of a readable document, newest first.

        Raises:
            DocumentNotFound: Unknown/foreign document (indistinct 404).
            EditorPatchValidationError: Unknown status filter value.
        """
        if status is not None and status not in PATCH_STATUSES:
            raise EditorPatchValidationError(f"unknown patch status: {status!r}")
        document = await self._document_for(
            document_id,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        patches = await self._store.list_for_document(document_id, status=status)
        return [
            patch
            for patch in patches
            if self._private_patch_visible(
                patch, document=document, visible_to=visible_to
            )
            and not (
                document.content_mode == "collaboration"
                and patch.source == "human"
                and patch.status == "pending"
                and not patch.suggestion_ids
            )
        ]

    async def apply(
        self,
        patch_id: str,
        *,
        expected_revision: int | None,
        expected_sequence: int | None = None,
        decision_id: uuid.UUID | None = None,
        visible_to: "UserContext | None",
        principal: "Principal | None" = None,
    ) -> EditorPatchRecord:
        """Apply a pending patch to its document (CAS + server-side edits).

        *expected_revision* must equal the document's CURRENT revision.
        On success the edits are applied via :func:`apply_edits`, the
        document is saved with ``revision = current + 1`` (all other
        fields preserved), and the patch flips to ``accepted`` with the
        applied revision and edit ids.

        Replay: an already-APPLIED patch answers idempotently with the
        stored record when the request carries the same
        ``expected_revision`` the apply consumed; anything else — and
        every rejected patch — raises :class:`PatchAlreadyDecided`.

        Raises:
            PatchNotFound: Unknown patch or invisible parent document.
            PatchAlreadyDecided: Rejected patch, or applied with a
                different context than the replay.
            PatchRevisionConflict: The document moved past
                *expected_revision* (carries current + proposed-against).
        """
        patch = await self._store.get(patch_id)
        document = await self._patch_document(
            patch,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        self._require_private_patch_access(
            patch, document=document, visible_to=visible_to
        )
        if document.content_mode == "collaboration":
            return await self._apply_collaboration_patch(
                patch,
                document=document,
                expected_sequence=expected_sequence,
                decision_id=decision_id,
                visible_to=visible_to,
                principal=principal,
            )
        await self._document_for(
            document.id,
            visible_to=visible_to,
            minimum=SharePermission.EDIT,
        )
        if expected_revision is None:
            raise EditorPatchValidationError("expected_revision is required")
        if patch.status != "pending":
            replay = self._applied_replay(patch, expected_revision)
            if replay is not None:
                return replay
            raise PatchAlreadyDecided(patch)
        if expected_revision != document.revision:
            raise PatchRevisionConflict(
                current_revision=document.revision,
                revision_before=patch.revision_before,
            )
        new_content, applied_edit_ids = apply_edits(
            document.content_markdown, list(patch.edits)
        )
        if len(applied_edit_ids) < len(patch.edits):
            skipped = [
                str(edit.get("id", ""))
                for edit in patch.edits
                if str(edit.get("id", "")) not in applied_edit_ids
            ]
            log.warning(
                "Editor patch %s: skipped %d edit(s) without a resolvable anchor (%s).",
                patch_id,
                len(skipped),
                ", ".join(skipped),
            )
        applied_revision = document.revision + 1
        # Write order (document BEFORE the mark_applied CAS) is deliberate:
        # a crash between the two writes leaves the document advanced and
        # the patch still pending — a VISIBLE, recoverable state (the next
        # apply CAS-conflicts loudly and the client refetches; re-applied
        # anchors that no longer match are skipped visibly). The reverse
        # order would record an accepted patch whose body write never
        # happened, and every replay would then report success against an
        # unchanged document — silent data loss (No Silent Fallbacks).
        # The document write itself is guarded by the store's revision
        # CAS: a concurrent writer that advanced the document first
        # surfaces as DocumentRevisionConflict below, disambiguated into
        # the harmless same-patch replay vs. a genuine interleave.
        try:
            await self._save_applied_document(
                document,
                content_markdown=new_content,
                revision=applied_revision,
                visible_to=visible_to,
            )
        except DocumentRevisionConflict as exc:
            # The document moved under us. Two shapes lose the CAS:
            # (a) the PARALLEL apply of this very patch landed first —
            # same base, identical content; (b) a human autosave
            # interleaved between our read and our write — a REAL
            # conflict that must stay pending for a fresh apply.
            fresh = await self._store.get(patch_id)
            replay = self._applied_replay(fresh, expected_revision)
            if replay is not None:
                return replay
            # The winner may have written the doc but not yet marked the
            # patch applied, so status alone cannot disambiguate. Compare
            # by CONTENT, never by revision: a human edit can coincide
            # with our target revision, so a revision match would wrongly
            # mark the patch applied over human content. When the live
            # document already equals THIS patch's result, the effect is
            # present — fall through to mark_applied (marks it, or replays
            # if the winner marks first), avoiding a spurious 409.
            current = await self._patch_document(
                patch,
                visible_to=visible_to,
                minimum=SharePermission.EDIT,
            )
            if current.content_markdown != new_content:
                raise PatchRevisionConflict(
                    current_revision=exc.current_revision,
                    revision_before=patch.revision_before,
                ) from exc
        try:
            updated = await self._store.mark_applied(
                patch_id,
                applied_revision=applied_revision,
                applied_edit_ids=applied_edit_ids,
            )
        except PatchAlreadyDecided as exc:
            # Concurrent apply race: the parallel request already decided
            # (and wrote the same content off the same base). Same
            # context -> replay 200; anything else keeps the conflict.
            replay = self._applied_replay(exc.patch, expected_revision)
            if replay is None:
                raise
            updated = replay
        await self._record_audit(
            principal,
            action="editor.patch_applied",
            document_id=document.id,
            detail={
                "patch_id": patch_id,
                "revision": str(applied_revision),
                "applied_edits": str(len(applied_edit_ids)),
            },
        )
        return updated

    async def reject(
        self,
        patch_id: str,
        *,
        note: str,
        expected_sequence: int | None = None,
        decision_id: uuid.UUID | None = None,
        visible_to: "UserContext | None",
        principal: "Principal | None" = None,
    ) -> EditorPatchRecord:
        """Reject a pending patch (records the optional note).

        Replay: an already-rejected patch answers idempotently with the
        stored record; an APPLIED patch raises
        :class:`PatchAlreadyDecided`.
        """
        patch = await self._store.get(patch_id)
        document = await self._patch_document(
            patch,
            visible_to=visible_to,
            minimum=SharePermission.VIEW,
        )
        self._require_private_patch_access(
            patch, document=document, visible_to=visible_to
        )
        if document.content_mode == "collaboration" and patch.suggestion_ids:
            return await self._decide_collaboration_patch(
                patch,
                decision="reject",
                expected_sequence=expected_sequence,
                decision_id=decision_id,
                visible_to=visible_to,
                principal=principal,
            )
        await self._document_for(
            document.id,
            visible_to=visible_to,
            minimum=(
                SharePermission.SUGGEST
                if document.content_mode == "collaboration"
                else SharePermission.EDIT
            ),
        )
        if patch.status == "rejected":
            return patch
        if patch.status != "pending":
            raise PatchAlreadyDecided(patch)
        try:
            updated = await self._store.mark_rejected(patch_id, note=note)
        except PatchAlreadyDecided as exc:
            if exc.patch.status == "rejected":
                updated = exc.patch
            else:
                raise
        await self._record_audit(
            principal,
            action="editor.patch_rejected",
            document_id=document.id,
            detail={"patch_id": patch_id},
        )
        return updated

    async def _apply_collaboration_patch(
        self,
        patch: EditorPatchRecord,
        *,
        document: EditorDocument,
        expected_sequence: int | None,
        decision_id: uuid.UUID | None,
        visible_to: "UserContext | None",
        principal: "Principal | None",
    ) -> EditorPatchRecord:
        """Publish a private AI patch or accept an already shared patch."""
        if self._collaboration is None or principal is None:
            raise EditorPatchValidationError("collaboration is unavailable")
        if expected_sequence is None or decision_id is None:
            raise EditorPatchValidationError(
                "expected_sequence and decision_id are required"
            )
        if patch.status != "pending":
            if patch.status == "accepted" and patch.command_id == decision_id:
                return patch
            raise PatchAlreadyDecided(patch)
        if patch.command_id == decision_id and patch.suggestion_ids:
            return patch
        if patch.suggestion_ids:
            return await self._decide_collaboration_patch(
                patch,
                decision="accept",
                expected_sequence=expected_sequence,
                decision_id=decision_id,
                visible_to=visible_to,
                principal=principal,
            )
        if patch.source == "human":
            raise EditorPatchValidationError("empty human suggestion cannot be applied")
        fresh_document = await self._editor_persistence.get_document_for_ai(
            document.id,
            visible_to=visible_to,
            minimum=SharePermission.SUGGEST,
        )
        target_markdown, _ = apply_edits(
            fresh_document.content_markdown, list(patch.edits)
        )
        result = await self._collaboration.publish_suggestion(
            document_id=document.id,
            patch_id=patch.patch_id,
            target_markdown=target_markdown,
            actor_kind="agent" if patch.source == "agent" else "assistant",
            expected_sequence=expected_sequence,
            command_id=decision_id,
            principal=principal,
            visible_to=visible_to,
        )
        updated = await self._store.get(patch.patch_id)
        if set(updated.suggestion_ids) != set(result.suggestion_ids):
            log.error(
                "Collaboration patch %s was persisted without matching suggestions.",
                patch.patch_id,
            )
            raise RuntimeError("collaboration patch metadata is inconsistent")
        await self._record_audit(
            principal,
            action="editor.patch_shared",
            document_id=document.id,
            detail={
                "patch_id": patch.patch_id,
                "sequence": str(result.sequence),
            },
        )
        return updated

    async def _decide_collaboration_patch(
        self,
        patch: EditorPatchRecord,
        *,
        decision: Literal["accept", "reject"],
        expected_sequence: int | None,
        decision_id: uuid.UUID | None,
        visible_to: "UserContext | None",
        principal: "Principal | None",
    ) -> EditorPatchRecord:
        """Accept or reject one shared patch through the serialized Node queue."""
        if self._collaboration is None or principal is None:
            raise EditorPatchValidationError("collaboration is unavailable")
        if expected_sequence is None or decision_id is None:
            raise EditorPatchValidationError(
                "expected_sequence and decision_id are required"
            )
        expected_status = "accepted" if decision == "accept" else "rejected"
        if patch.status != "pending":
            if patch.status == expected_status and patch.command_id == decision_id:
                return patch
            raise PatchAlreadyDecided(patch)
        await self._document_for(
            patch.document_id,
            visible_to=visible_to,
            minimum=SharePermission.EDIT,
        )
        await self._collaboration.decide(
            document_id=patch.document_id,
            patch_ids=(patch.patch_id,),
            decision=decision,
            expected_sequence=expected_sequence,
            command_id=decision_id,
            principal=principal,
            visible_to=visible_to,
        )
        updated = await self._store.get(patch.patch_id)
        if updated.status != expected_status or updated.command_id != decision_id:
            log.error(
                "Collaboration decision %s did not update patch %s atomically.",
                decision_id,
                patch.patch_id,
            )
            raise RuntimeError("collaboration decision metadata is inconsistent")
        await self._record_audit(
            principal,
            action=f"editor.patch_{expected_status}",
            document_id=patch.document_id,
            detail={
                "patch_id": patch.patch_id,
                "sequence": str(updated.decision_sequence),
            },
        )
        return updated

    # -- helpers -------------------------------------------------------------- #

    @staticmethod
    def _applied_replay(
        patch: EditorPatchRecord, expected_revision: int
    ) -> EditorPatchRecord | None:
        """The stored record for an idempotent apply replay, or ``None``.

        Replay identity: the patch is accepted and the request carries
        the SAME ``expected_revision`` the original apply consumed
        (``applied_revision - 1``).
        """
        if (
            patch.status == "accepted"
            and patch.applied_revision is not None
            and expected_revision == patch.applied_revision - 1
        ):
            return patch
        return None

    async def _patch_document(
        self,
        patch: EditorPatchRecord,
        *,
        visible_to: "UserContext | None",
        minimum: SharePermission,
    ) -> EditorDocument:
        """The patch's parent document, or the indistinct PatchNotFound."""
        try:
            return await self._document_for(
                patch.document_id,
                visible_to=visible_to,
                minimum=minimum,
            )
        except DocumentNotFound:
            # A foreign caller must not learn the patch exists at all.
            raise PatchNotFound(patch.patch_id) from None

    async def _document_for(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
        minimum: SharePermission,
    ) -> EditorDocument:
        return await self._editor_persistence.get_document(
            document_id,
            visible_to=visible_to,
            minimum=minimum,
        )

    @staticmethod
    def _private_patch_visible(
        patch: EditorPatchRecord,
        *,
        document: EditorDocument,
        visible_to: "UserContext | None",
    ) -> bool:
        if (
            document.content_mode != "collaboration"
            or patch.suggestion_ids
            or patch.source == "human"
        ):
            return True
        caller = visible_to.principal.user_id if visible_to is not None else None
        return caller is not None and caller == patch.created_by_user_id

    @classmethod
    def _require_private_patch_access(
        cls,
        patch: EditorPatchRecord,
        *,
        document: EditorDocument,
        visible_to: "UserContext | None",
    ) -> None:
        if not cls._private_patch_visible(
            patch, document=document, visible_to=visible_to
        ):
            raise PatchNotFound(patch.patch_id)

    async def _save_applied_document(
        self,
        document: EditorDocument,
        *,
        content_markdown: str,
        revision: int,
        visible_to: "UserContext | None",
    ) -> None:
        """Persist the applied body with the bumped revision, all other
        fields preserved (owner/created_at stay stable in the service)."""
        await self._editor_persistence.save_document(
            id=document.id,
            title=document.title,
            content_markdown=content_markdown,
            folder_id=document.folder_id,
            source=document.source,
            source_run_id=document.source_run_id,
            revision=revision,
            diff_anchor_markdown=document.diff_anchor_markdown,
            diff_anchor_updated_at=document.diff_anchor_updated_at,
            created_at=document.created_at,
            updated_at=time.time(),
            caller_user_id=document.created_by_user_id,
            workspace_id=document.workspace_id,
            visible_to=visible_to,
        )

    @staticmethod
    def _parse_edit(index: int, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise EditorPatchValidationError("each edit must be an object")
        position = raw.get("position")
        if position not in _EDIT_POSITIONS:
            raise EditorPatchValidationError(
                f"unknown edit position: {position!r}"
            )
        edit: dict[str, Any] = {"id": f"ed_{index + 1}", "position": position}
        for key in _EDIT_TEXT_KEYS:
            value = raw.get(key, "")
            if value is not None and not isinstance(value, str):
                raise EditorPatchValidationError(
                    f"edit field {key!r} must be a string"
                )
            edit[key] = value or ""
        return edit

    async def _record_audit(
        self,
        principal: "Principal | None",
        *,
        action: str,
        document_id: str,
        detail: dict[str, str],
        actor_type: str = "user",
    ) -> None:
        if self._audit is None or principal is None:
            return
        await self._audit.record(
            AuditEntry(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action=action,
                resource_type="editor_document",
                resource_id=document_id,
                detail=detail,
                # ``agent`` when a workspace-agent run proposed the edits;
                # ``actor_user_id`` still carries the effective actor.
                # Apply/reject stay user actions (the default).
                actor_type=actor_type,
            )
        )
