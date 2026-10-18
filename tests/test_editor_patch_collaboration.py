"""Behavior tests for collaboration-aware editor patch decisions."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Literal

import pytest

from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.project.editor_patch_ports import (
    EditorPatchRecord,
    PatchAlreadyDecided,
    PatchNotFound,
)
from inqtrix.project.editor_ports import DocumentNotFound, EditorDocument
from inqtrix.services.collaboration_client import (
    CollaborationDecisionResult,
    CollaborationSuggestionResult,
)
from inqtrix.services.editor_patch_service import EditorPatchService


OWNER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
SUGGESTER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOCUMENT_ID = "ed_collaboration_patch"
DOCUMENT_MARKDOWN = "# Report\n\nOld text."


def _principal(user_id: uuid.UUID) -> Principal:
    """Build a cookie-authenticated principal suitable for collaboration."""
    return Principal(
        user_id=user_id,
        kind="oidc_session",
        tenant_id="default",
        role="member",
        session_id=f"session-{user_id}",
    )


def _context(user_id: uuid.UUID) -> UserContext:
    """Build the user context paired with a collaboration principal."""
    return UserContext(principal=_principal(user_id))


def _document() -> EditorDocument:
    """Return one collaboration document with a current Markdown projection."""
    return EditorDocument(
        id=DOCUMENT_ID,
        title="Report",
        content_markdown=DOCUMENT_MARKDOWN,
        revision=7,
        created_at=1.0,
        updated_at=2.0,
        tenant_id="default",
        created_by_user_id=OWNER_ID,
        content_mode="collaboration",
        metadata_revision=3,
        collaboration_generation=4,
        collaboration_schema_version=1,
        collaboration_schema_hash="a" * 64,
        persisted_sequence=19,
        projection_sequence=19,
        projection_updated_at=2.0,
    )


def _edits() -> list[dict[str, str]]:
    """Return one deterministic AI edit that changes the projected Markdown."""
    return [
        {
            "find": "Old text.",
            "quote_before": "",
            "quote_after": "",
            "position": "replace",
            "text": "New text.",
            "note": "Clarify the wording.",
        }
    ]


class _PatchStore:
    """Small patch store whose helpers model atomic Node persistence."""

    def __init__(self) -> None:
        self.records: dict[str, EditorPatchRecord] = {}

    async def create(self, patch: EditorPatchRecord) -> EditorPatchRecord:
        self.records[patch.patch_id] = patch
        return patch

    async def get(self, patch_id: str) -> EditorPatchRecord:
        try:
            return self.records[patch_id]
        except KeyError:
            raise PatchNotFound(patch_id) from None

    async def list_for_document(
        self, document_id: str, *, status: str | None = None
    ) -> list[EditorPatchRecord]:
        return [
            patch
            for patch in self.records.values()
            if patch.document_id == document_id
            and (status is None or patch.status == status)
        ]

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
        patch = await self._require_pending(patch_id)
        updated = replace(
            patch,
            status="accepted",
            applied_revision=applied_revision,
            applied_edit_ids=tuple(applied_edit_ids),
            decision_sequence=decision_sequence,
            decided_by_user_id=decided_by_user_id,
            command_id=command_id,
            decided_at=3.0,
        )
        self.records[patch_id] = updated
        return updated

    async def mark_rejected(
        self,
        patch_id: str,
        *,
        note: str,
        decision_sequence: int | None = None,
        decided_by_user_id: uuid.UUID | None = None,
        command_id: uuid.UUID | None = None,
    ) -> EditorPatchRecord:
        patch = await self._require_pending(patch_id)
        updated = replace(
            patch,
            status="rejected",
            note=note,
            decision_sequence=decision_sequence,
            decided_by_user_id=decided_by_user_id,
            command_id=command_id,
            decided_at=3.0,
        )
        self.records[patch_id] = updated
        return updated

    async def publish(
        self,
        patch_id: str,
        *,
        suggestion_ids: tuple[str, ...],
        command_id: uuid.UUID,
    ) -> EditorPatchRecord:
        patch = await self.get(patch_id)
        updated = replace(
            patch,
            suggestion_ids=suggestion_ids,
            command_id=command_id,
        )
        self.records[patch_id] = updated
        return updated

    async def decide(
        self,
        patch_id: str,
        *,
        decision: Literal["accept", "reject"],
        sequence: int,
        command_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> EditorPatchRecord:
        patch = await self._require_pending(patch_id)
        updated = replace(
            patch,
            status="accepted" if decision == "accept" else "rejected",
            decision_sequence=sequence,
            decided_by_user_id=actor_user_id,
            command_id=command_id,
            decided_at=4.0,
        )
        self.records[patch_id] = updated
        return updated

    async def _require_pending(self, patch_id: str) -> EditorPatchRecord:
        patch = await self.get(patch_id)
        if patch.status != "pending":
            raise PatchAlreadyDecided(patch)
        return patch

    async def aclose(self) -> None:
        """Release no resources; included for port parity."""


class _Documents:
    """Permission-aware document facade that rejects Markdown body writes."""

    def __init__(
        self,
        document: EditorDocument,
        permissions: dict[uuid.UUID, SharePermission],
    ) -> None:
        self.document = document
        self.permissions = permissions
        self.reads: list[SharePermission] = []
        self.ai_reads: list[SharePermission] = []
        self.markdown_puts: list[dict[str, Any]] = []

    async def get_document(
        self,
        document_id: str,
        *,
        visible_to: UserContext | None,
        minimum: SharePermission = SharePermission.VIEW,
    ) -> EditorDocument:
        self.reads.append(minimum)
        return self._require_access(document_id, visible_to, minimum)

    async def get_document_for_ai(
        self,
        document_id: str,
        *,
        visible_to: UserContext | None,
        minimum: SharePermission = SharePermission.VIEW,
    ) -> EditorDocument:
        self.ai_reads.append(minimum)
        return self._require_access(document_id, visible_to, minimum)

    async def save_document(self, **values: Any) -> EditorDocument:
        self.markdown_puts.append(values)
        raise AssertionError("collaboration patches must not use Markdown PUT")

    def _require_access(
        self,
        document_id: str,
        visible_to: UserContext | None,
        minimum: SharePermission,
    ) -> EditorDocument:
        if document_id != self.document.id or visible_to is None:
            raise DocumentNotFound(document_id)
        user_id = visible_to.principal.user_id
        permission = self.permissions.get(user_id) if user_id is not None else None
        if permission is None or not permission.at_least(minimum):
            raise DocumentNotFound(document_id)
        return self.document


class _Collaboration:
    """Node-facing service fake that models the durable patch side effects."""

    def __init__(
        self,
        store: _PatchStore,
        *,
        persisted_suggestion_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.store = store
        self.persisted_suggestion_ids = persisted_suggestion_ids
        self.publish_calls: list[dict[str, Any]] = []
        self.decision_calls: list[dict[str, Any]] = []

    async def publish_suggestion(self, **values: Any) -> CollaborationSuggestionResult:
        self.publish_calls.append(values)
        result_ids = ("suggestion-1", "suggestion-2")
        persisted_ids = (
            result_ids
            if self.persisted_suggestion_ids is None
            else self.persisted_suggestion_ids
        )
        await self.store.publish(
            values["patch_id"],
            suggestion_ids=persisted_ids,
            command_id=values["command_id"],
        )
        return CollaborationSuggestionResult(
            command_id=values["command_id"],
            patch_id=values["patch_id"],
            sequence=20,
            suggestion_ids=result_ids,
        )

    async def decide(self, **values: Any) -> CollaborationDecisionResult:
        self.decision_calls.append(values)
        patch_id = values["patch_ids"][0]
        await self.store.decide(
            patch_id,
            decision=values["decision"],
            sequence=20,
            command_id=values["command_id"],
            actor_user_id=values["principal"].user_id,
        )
        patch = await self.store.get(patch_id)
        return CollaborationDecisionResult(
            command_id=values["command_id"],
            sequence=20,
            suggestion_ids=patch.suggestion_ids,
        )


class _Harness:
    """Collaboration patch service and its observable fake boundaries."""

    def __init__(
        self,
        *,
        actor_id: uuid.UUID,
        permission: SharePermission,
        persisted_suggestion_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.actor_id = actor_id
        self.principal = _principal(actor_id)
        self.context = _context(actor_id)
        self.store = _PatchStore()
        self.documents = _Documents(_document(), {actor_id: permission})
        self.collaboration = _Collaboration(
            self.store,
            persisted_suggestion_ids=persisted_suggestion_ids,
        )
        self.service = EditorPatchService(
            store=self.store,
            editor_persistence=self.documents,  # type: ignore[arg-type]
            collaboration=self.collaboration,  # type: ignore[arg-type]
            durable=True,
        )

    async def propose(self, *, source: str = "instruct") -> EditorPatchRecord:
        return await self.service.propose(
            document_id=DOCUMENT_ID,
            run_id=None,
            source=source,
            edits=_edits(),
            summary="Improve the paragraph.",
            warnings=[],
            created_by_user_id=None,
            visible_to=self.context,
            principal=self.principal,
        )


@pytest.mark.asyncio
async def test_collaboration_proposal_uses_uuid_and_principal_attribution() -> None:
    """A collaboration proposal records stable CRDT coordinates and its author."""
    harness = _Harness(actor_id=SUGGESTER_ID, permission=SharePermission.SUGGEST)

    patch = await harness.propose(source="agent")

    assert str(uuid.UUID(patch.patch_id)) == patch.patch_id
    assert patch.created_by_user_id == SUGGESTER_ID
    assert patch.collaboration_generation == 4
    assert patch.base_sequence == 19
    assert harness.documents.ai_reads == [SharePermission.SUGGEST]


@pytest.mark.asyncio
async def test_private_ai_patch_publishes_yjs_suggestion_without_markdown_put() -> None:
    """Applying private AI work publishes once and never mutates Markdown."""
    harness = _Harness(actor_id=OWNER_ID, permission=SharePermission.EDIT)
    patch = await harness.propose()
    command_id = uuid.UUID("33333333-3333-4333-8333-333333333333")

    published = await harness.service.apply(
        patch.patch_id,
        expected_revision=None,
        expected_sequence=19,
        decision_id=command_id,
        visible_to=harness.context,
        principal=harness.principal,
    )
    replay = await harness.service.apply(
        patch.patch_id,
        expected_revision=None,
        expected_sequence=19,
        decision_id=command_id,
        visible_to=harness.context,
        principal=harness.principal,
    )

    assert published == replay
    assert published.status == "pending"
    assert published.suggestion_ids == ("suggestion-1", "suggestion-2")
    assert published.command_id == command_id
    assert harness.documents.markdown_puts == []
    assert len(harness.collaboration.publish_calls) == 1
    call = harness.collaboration.publish_calls[0]
    assert call["document_id"] == DOCUMENT_ID
    assert call["patch_id"] == patch.patch_id
    assert call["target_markdown"] == "# Report\n\nNew text."
    assert call["actor_kind"] == "assistant"
    assert call["expected_sequence"] == 19
    assert call["command_id"] == command_id
    assert harness.collaboration.decision_calls == []


@pytest.mark.asyncio
async def test_suggest_user_can_publish_but_cannot_decide_shared_patch() -> None:
    """Suggest access permits publication but cannot cross the edit decision gate."""
    harness = _Harness(actor_id=SUGGESTER_ID, permission=SharePermission.SUGGEST)
    patch = await harness.propose()
    publish_id = uuid.UUID("44444444-4444-4444-8444-444444444444")

    await harness.service.apply(
        patch.patch_id,
        expected_revision=None,
        expected_sequence=19,
        decision_id=publish_id,
        visible_to=harness.context,
        principal=harness.principal,
    )

    with pytest.raises(DocumentNotFound):
        await harness.service.apply(
            patch.patch_id,
            expected_revision=None,
            expected_sequence=20,
            decision_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
            visible_to=harness.context,
            principal=harness.principal,
        )

    assert len(harness.collaboration.publish_calls) == 1
    assert harness.collaboration.decision_calls == []
    assert SharePermission.EDIT in harness.documents.reads


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("accept", "accepted"), ("reject", "rejected")],
)
@pytest.mark.asyncio
async def test_shared_patch_decision_delegates_once_and_replays_by_command(
    decision: Literal["accept", "reject"], expected_status: str
) -> None:
    """Shared decisions use Node and replay one matching command idempotently."""
    harness = _Harness(actor_id=OWNER_ID, permission=SharePermission.EDIT)
    patch = await harness.propose()
    await harness.store.publish(
        patch.patch_id,
        suggestion_ids=("suggestion-1",),
        command_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
    )
    decision_id = uuid.UUID("77777777-7777-4777-8777-777777777777")

    if decision == "accept":
        decided = await harness.service.apply(
            patch.patch_id,
            expected_revision=None,
            expected_sequence=20,
            decision_id=decision_id,
            visible_to=harness.context,
            principal=harness.principal,
        )
        replay = await harness.service.apply(
            patch.patch_id,
            expected_revision=None,
            expected_sequence=20,
            decision_id=decision_id,
            visible_to=harness.context,
            principal=harness.principal,
        )
    else:
        decided = await harness.service.reject(
            patch.patch_id,
            note="Not suitable.",
            expected_sequence=20,
            decision_id=decision_id,
            visible_to=harness.context,
            principal=harness.principal,
        )
        replay = await harness.service.reject(
            patch.patch_id,
            note="A retry must not overwrite the decision.",
            expected_sequence=20,
            decision_id=decision_id,
            visible_to=harness.context,
            principal=harness.principal,
        )

    assert decided == replay
    assert decided.status == expected_status
    assert decided.command_id == decision_id
    assert decided.decision_sequence == 20
    assert decided.decided_by_user_id == OWNER_ID
    assert harness.documents.markdown_puts == []
    assert len(harness.collaboration.decision_calls) == 1
    call = harness.collaboration.decision_calls[0]
    assert call["patch_ids"] == (patch.patch_id,)
    assert call["decision"] == decision
    assert call["expected_sequence"] == 20
    assert call["command_id"] == decision_id


@pytest.mark.asyncio
async def test_publish_fails_visibly_when_persisted_suggestions_disagree() -> None:
    """A sidecar result cannot mask inconsistent durable patch metadata."""
    harness = _Harness(
        actor_id=OWNER_ID,
        permission=SharePermission.EDIT,
        persisted_suggestion_ids=("different-suggestion",),
    )
    patch = await harness.propose()

    with pytest.raises(
        RuntimeError, match="collaboration patch metadata is inconsistent"
    ):
        await harness.service.apply(
            patch.patch_id,
            expected_revision=None,
            expected_sequence=19,
            decision_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
            visible_to=harness.context,
            principal=harness.principal,
        )

    assert harness.documents.markdown_puts == []
    assert len(harness.collaboration.publish_calls) == 1

