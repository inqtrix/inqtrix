"""Behavior tests for the editor-patch flow (memory tier).

Three layers: the deterministic :func:`apply_edits` anchor matrix, the
service lifecycle (visibility, CAS, replay idempotency) over the memory
store pair, and the HTTP route matrix over the full oidc container. The
Postgres lockstep counterpart lives in
``tests/storage/test_editor_patches_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import (
    AccessMode,
    AuthorizationService,
    ResourceAccess,
    SharePermission,
)
from inqtrix.auth.principal import ANONYMOUS_PRINCIPAL, Principal, UserContext
from inqtrix.capabilities import CapabilityContext, build_capability_registry
from inqtrix.project.editor_memory import MemoryEditorStore
from inqtrix.project.editor_patch_memory import MemoryEditorPatchStore
from inqtrix.project.editor_patch_ports import (
    EditorPatchRecord,
    PatchAlreadyDecided,
    PatchNotFound,
    PatchRevisionConflict,
)
from inqtrix.project.editor_ports import DocumentNotFound
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers import editor_patches, editor_persistence
from inqtrix.services.editor_patch_service import (
    EditorPatchService,
    EditorPatchValidationError,
    _resolve_anchor,
    apply_edits,
)
from inqtrix.services.editor_persistence_service import EditorPersistenceService
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM
from tests.test_runs_sharing import (
    OWNER,
    RECIPIENT,
    SUB_HEADER,
    OidcHeaderProvider,
)


def scoped(user_id: uuid.UUID, *, tenant_id: str = "default") -> UserContext:
    """Build the canonical local-user context used by these tests."""
    return UserContext(
        principal=Principal(
            user_id=user_id,
            kind="oidc_session",
            tenant_id=tenant_id,
            role="member",
        )
    )


class _CanonicalOidcHeaderProvider(OidcHeaderProvider):
    """Adapt the shared HTTP test provider to canonical local user ids."""

    def resolve_principal(self, request: Request) -> Principal:
        user_id = request.headers.get(SUB_HEADER, "")
        if not user_id:
            return ANONYMOUS_PRINCIPAL
        return Principal(
            user_id=uuid.UUID(user_id),
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )

# ------------------------------------------------------------------ #
# apply_edits: the deterministic anchor matrix
# ------------------------------------------------------------------ #

_DOC = "# Titel\n\nAlpha beta gamma.\n\nDelta epsilon zeta."


def _edit(edit_id: str, **fields: str) -> dict:
    base = {
        "id": edit_id,
        "find": "",
        "quote_before": "",
        "quote_after": "",
        "position": "replace",
        "text": "",
        "note": "",
    }
    base.update(fields)
    return base


def test_apply_edits_replace_and_delete() -> None:
    content, applied = apply_edits(
        _DOC,
        [
            _edit("ed_1", find="Alpha beta gamma.", text="Alpha bleibt."),
            # Empty text on replace is a deletion.
            _edit("ed_2", find="Delta epsilon zeta.", text=""),
        ],
    )
    assert "Alpha bleibt." in content
    assert "Alpha beta gamma." not in content
    assert "Delta epsilon zeta." not in content
    assert applied == ["ed_1", "ed_2"]


def test_apply_edits_before_after_and_append() -> None:
    content, applied = apply_edits(
        _DOC,
        [
            _edit("ed_1", find="Alpha beta gamma.", position="before", text="Davor."),
            _edit("ed_2", find="Delta epsilon zeta.", position="after", text="Danach."),
            _edit("ed_3", position="append", text="## Anhang"),
        ],
    )
    assert "Davor.\n\nAlpha beta gamma." in content
    assert "Delta epsilon zeta.\n\nDanach." in content
    assert content.endswith("\n\n## Anhang")
    assert applied == ["ed_1", "ed_2", "ed_3"]


def test_apply_edits_append_to_empty_document() -> None:
    content, applied = apply_edits(
        "", [_edit("ed_1", position="append", text="# Neu")]
    )
    assert content == "# Neu"
    assert applied == ["ed_1"]


def test_apply_edits_quote_disambiguation_picks_nearest() -> None:
    doc = "Erstens: Preis steigt.\n\nZweitens: Preis steigt.\n\nSchluss."
    content, applied = apply_edits(
        doc,
        [
            _edit(
                "ed_1",
                find="Preis steigt.",
                quote_before="Zweitens:",
                text="Preis sinkt.",
            )
        ],
    )
    assert content == "Erstens: Preis steigt.\n\nZweitens: Preis sinkt.\n\nSchluss."
    assert applied == ["ed_1"]


def test_apply_edits_skips_ambiguous_and_missing_anchors() -> None:
    doc = "Q Preis X Q Preis Y"
    content, applied = apply_edits(
        doc,
        [
            # Two occurrences, no quotes -> ambiguous -> skip.
            _edit("ed_1", find="Preis", text="Wert"),
            # Two occurrences, quote matches both at the same distance
            # (tie) -> still ambiguous -> skip.
            _edit("ed_2", find="Preis", quote_before="Q ", text="Wert"),
            # Anchor not present at all -> skip.
            _edit("ed_3", find="NICHT VORHANDEN", text="x"),
        ],
    )
    assert content == doc
    assert applied == []


def test_apply_edits_applies_sequentially_against_evolving_text() -> None:
    content, applied = apply_edits(
        "Der alte Satz.",
        [
            _edit("ed_1", find="Der alte Satz.", text="Der neue Satz."),
            # Anchors against the text edit ed_1 just produced.
            _edit("ed_2", find="Der neue Satz.", position="after", text="Folgesatz."),
        ],
    )
    assert content == "Der neue Satz.\n\nFolgesatz."
    assert applied == ["ed_1", "ed_2"]


def test_apply_edits_skips_insert_without_text() -> None:
    content, applied = apply_edits(
        _DOC, [_edit("ed_1", find="Alpha beta gamma.", position="after", text="")]
    )
    assert content == _DOC
    assert applied == []


# ------------------------------------------------------------------ #
# service lifecycle + visibility (memory store pair)
# ------------------------------------------------------------------ #


def _memory_service() -> tuple[EditorPatchService, EditorPersistenceService]:
    persistence = EditorPersistenceService(
        store=MemoryEditorStore(), durable=False
    )
    return (
        EditorPatchService(
            store=MemoryEditorPatchStore(),
            editor_persistence=persistence,
            audit=None,
            durable=False,
        ),
        persistence,
    )


async def _seed_document(
    persistence: EditorPersistenceService,
    *,
    document_id: str = "ed_doc",
    sub: str = OWNER,
    body: str = _DOC,
    revision: int = 3,
) -> None:
    await persistence.save_document(
        id=document_id,
        title="Bericht",
        content_markdown=body,
        folder_id=None,
        source="blank",
        source_run_id=None,
        revision=revision,
        diff_anchor_markdown=None,
        diff_anchor_updated_at=None,
        created_at=1.0,
        updated_at=1.0,
        caller_user_id=sub,
        workspace_id=None,
        visible_to=scoped(sub),
    )


def _raw_edits() -> list[dict]:
    return [
        {
            "find": "Alpha beta gamma.",
            "quote_before": "",
            "quote_after": "",
            "position": "replace",
            "text": "Alpha verbessert.",
            "note": "Straffung",
        },
        {
            "find": "NICHT VORHANDEN",
            "quote_before": "",
            "quote_after": "",
            "position": "replace",
            "text": "x",
            "note": "",
        },
    ]


@pytest.mark.asyncio
async def test_service_lifecycle_propose_apply_replay_and_conflicts() -> None:
    service, persistence = _memory_service()
    await _seed_document(persistence)

    patch = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="instruct",
        edits=_raw_edits(),
        summary="Zwei Aenderungen",
        warnings=["Ein Anker unsicher"],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
    )
    assert patch.status == "pending"
    assert patch.revision_before == 3
    assert [edit["id"] for edit in patch.edits] == ["ed_1", "ed_2"]

    fetched, document_revision = await service.get_patch(
        patch.patch_id, visible_to=scoped(OWNER)
    )
    assert fetched.patch_id == patch.patch_id
    assert document_revision == 3

    with pytest.raises(PatchRevisionConflict) as conflict:
        await service.apply(
            patch.patch_id, expected_revision=2, visible_to=scoped(OWNER)
        )
    assert conflict.value.current_revision == 3
    assert conflict.value.revision_before == 3

    applied = await service.apply(
        patch.patch_id, expected_revision=3, visible_to=scoped(OWNER)
    )
    assert applied.status == "accepted"
    assert applied.applied_revision == 4
    # The unresolvable anchor is skipped, visible as the id difference.
    assert applied.applied_edit_ids == ("ed_1",)
    document = await persistence.get_document("ed_doc", visible_to=scoped(OWNER))
    assert document.revision == 4
    assert "Alpha verbessert." in document.content_markdown

    # Replay with the SAME expected revision answers the stored record.
    replay = await service.apply(
        patch.patch_id, expected_revision=3, visible_to=scoped(OWNER)
    )
    assert replay == applied

    # A different expected revision on the decided patch conflicts.
    with pytest.raises(PatchAlreadyDecided):
        await service.apply(
            patch.patch_id, expected_revision=4, visible_to=scoped(OWNER)
        )
    # Reject after apply conflicts too.
    with pytest.raises(PatchAlreadyDecided):
        await service.reject(
            patch.patch_id, note="zu spaet", visible_to=scoped(OWNER)
        )


@pytest.mark.asyncio
async def test_service_reject_flow_and_replay() -> None:
    service, persistence = _memory_service()
    await _seed_document(persistence)
    patch = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="suggest",
        edits=_raw_edits()[:1],
        summary="",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
    )

    rejected = await service.reject(
        patch.patch_id, note="Passt nicht.", visible_to=scoped(OWNER)
    )
    assert rejected.status == "rejected"
    assert rejected.note == "Passt nicht."
    assert rejected.decided_at is not None

    # Reject replay is idempotent (stored record, note unchanged).
    replay = await service.reject(
        patch.patch_id, note="anders", visible_to=scoped(OWNER)
    )
    assert replay == rejected

    # Apply after reject conflicts, and the document never moved.
    with pytest.raises(PatchAlreadyDecided):
        await service.apply(
            patch.patch_id, expected_revision=3, visible_to=scoped(OWNER)
        )
    document = await persistence.get_document("ed_doc", visible_to=scoped(OWNER))
    assert document.revision == 3


class _CapturingAudit:
    """Records the AuditEntry objects the service emits (test sink)."""

    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def record(self, entry: Any) -> None:
        self.entries.append(entry)


@pytest.mark.asyncio
async def test_agent_proposal_is_audited_as_an_agent_write() -> None:
    """An agent-sourced proposal records editor.patch_proposed with
    actor_type='agent' (the audit trail) — mirroring apply/reject."""
    persistence = EditorPersistenceService(
        store=MemoryEditorStore(), durable=False
    )
    audit = _CapturingAudit()
    service = EditorPatchService(
        store=MemoryEditorPatchStore(),
        editor_persistence=persistence,
        audit=audit,
        durable=False,
    )
    await _seed_document(persistence)
    principal = Principal(
        user_id=OWNER, kind="oidc_session", tenant_id="default", role="member"
    )

    patch = await service.propose(
        document_id="ed_doc",
        run_id="run-agent-1",
        source="agent",
        edits=_raw_edits()[:1],
        summary="Agentenvorschlag",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
        principal=principal,
    )

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.action == "editor.patch_proposed"
    assert entry.actor_type == "agent"
    assert entry.actor_user_id == OWNER  # the owner acted on whose behalf
    assert entry.resource_type == "editor_document"
    assert entry.resource_id == "ed_doc"
    assert entry.detail["patch_id"] == patch.patch_id
    assert entry.detail["source"] == "agent"
    assert entry.detail["edit_count"] == "1"


@pytest.mark.asyncio
async def test_proposal_audit_is_skipped_without_a_principal() -> None:
    """Memory/dev (no principal) skips the audit, exactly like apply/reject."""
    persistence = EditorPersistenceService(
        store=MemoryEditorStore(), durable=False
    )
    audit = _CapturingAudit()
    service = EditorPatchService(
        store=MemoryEditorPatchStore(),
        editor_persistence=persistence,
        audit=audit,
        durable=False,
    )
    await _seed_document(persistence)

    await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="suggest",
        edits=_raw_edits()[:1],
        summary="",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
    )
    assert audit.entries == []


@pytest.mark.asyncio
async def test_service_list_filters_by_status_and_orders_newest_first() -> None:
    service, persistence = _memory_service()
    await _seed_document(persistence)
    first = await service.propose(
        document_id="ed_doc", run_id=None, source="instruct",
        edits=_raw_edits()[:1], summary="", warnings=[],
        created_by_user_id=OWNER, visible_to=scoped(OWNER),
    )
    second = await service.propose(
        document_id="ed_doc", run_id="run-1", source="agent",
        edits=_raw_edits()[:1], summary="", warnings=[],
        created_by_user_id=OWNER, visible_to=scoped(OWNER),
    )
    await service.reject(first.patch_id, note="", visible_to=scoped(OWNER))

    everything = await service.list_for_document(
        "ed_doc", status=None, visible_to=scoped(OWNER)
    )
    assert [p.patch_id for p in everything] == [second.patch_id, first.patch_id]
    pending = await service.list_for_document(
        "ed_doc", status="pending", visible_to=scoped(OWNER)
    )
    assert [p.patch_id for p in pending] == [second.patch_id]
    with pytest.raises(EditorPatchValidationError):
        await service.list_for_document(
            "ed_doc", status="bogus", visible_to=scoped(OWNER)
        )


# One moment in Unix seconds; the stamps lie 10, 20 and 30 seconds after it,
# written in the unit their writer uses: the agent patch API stamps Unix
# seconds, a patch materialized from a collaboration suggestion carries the
# suggestion's time (epoch milliseconds, Unix seconds for older AI ones).
MIXED_UNIT_STAMPS = {
    "pch_agent_oldest": 1_790_942_600.0,
    "pch_typed": 1_790_942_610_000.0,
    "pch_ai_in_seconds": 1_790_942_620.0,
    "pch_ai_newest": 1_790_942_630_000.0,
}
NEWEST_FIRST = ["pch_ai_newest", "pch_ai_in_seconds", "pch_typed", "pch_agent_oldest"]


@pytest.mark.asyncio
async def test_store_lists_newest_first_whichever_unit_stamped_the_patch() -> None:
    store = MemoryEditorPatchStore()
    for patch_id, created_at in MIXED_UNIT_STAMPS.items():
        await store.create(
            EditorPatchRecord(
                patch_id=patch_id, document_id="ed_doc", created_at=created_at
            )
        )

    listed = await store.list_for_document("ed_doc")

    assert [patch.patch_id for patch in listed] == NEWEST_FIRST
    # Each patch keeps the time in the unit it was written in.
    assert [patch.created_at for patch in listed] == [
        MIXED_UNIT_STAMPS[patch_id] for patch_id in NEWEST_FIRST
    ]


@pytest.mark.asyncio
async def test_service_validates_source_and_edit_positions() -> None:
    service, persistence = _memory_service()
    await _seed_document(persistence)
    with pytest.raises(EditorPatchValidationError):
        await service.propose(
            document_id="ed_doc", run_id=None, source="bogus",
            edits=_raw_edits()[:1], summary="", warnings=[],
            created_by_user_id=OWNER, visible_to=scoped(OWNER),
        )
    with pytest.raises(EditorPatchValidationError):
        await service.propose(
            document_id="ed_doc", run_id=None, source="instruct",
            edits=[{"find": "x", "position": "sideways", "text": "y"}],
            summary="", warnings=[],
            created_by_user_id=OWNER, visible_to=scoped(OWNER),
        )


@pytest.mark.asyncio
async def test_foreign_caller_gets_indistinct_not_found_everywhere() -> None:
    """A stranger sees byte-identical 404 semantics: the document-scoped
    call raises the document's not-found, every patch-scoped call the
    patch's — never a hint that the patch exists."""
    service, persistence = _memory_service()
    await _seed_document(persistence)
    patch = await service.propose(
        document_id="ed_doc", run_id=None, source="instruct",
        edits=_raw_edits()[:1], summary="", warnings=[],
        created_by_user_id=OWNER, visible_to=scoped(OWNER),
    )

    stranger = scoped(RECIPIENT)
    with pytest.raises(DocumentNotFound):
        await service.propose(
            document_id="ed_doc", run_id=None, source="instruct",
            edits=_raw_edits()[:1], summary="", warnings=[],
            created_by_user_id=RECIPIENT, visible_to=stranger,
        )
    with pytest.raises(DocumentNotFound):
        await service.list_for_document(
            "ed_doc", status=None, visible_to=stranger
        )
    with pytest.raises(PatchNotFound):
        await service.get_patch(patch.patch_id, visible_to=stranger)
    with pytest.raises(PatchNotFound):
        await service.apply(
            patch.patch_id, expected_revision=3, visible_to=stranger
        )
    with pytest.raises(PatchNotFound):
        await service.reject(patch.patch_id, note="", visible_to=stranger)
    # The owner still sees the untouched pending patch.
    stored, _revision = await service.get_patch(
        patch.patch_id, visible_to=scoped(OWNER)
    )
    assert stored.status == "pending"


# ------------------------------------------------------------------ #
# capability wrappers
# ------------------------------------------------------------------ #


def test_patch_capabilities_register_only_with_patch_service() -> None:
    service, _persistence = _memory_service()
    registry = build_capability_registry(editor_patch_service=service)
    assert registry.ids() == ("editor.patch.propose", "editor.patch.apply")
    manifest = {entry["id"]: entry for entry in registry.manifest()}
    assert manifest["editor.patch.propose"]["effect"] == "write"
    assert manifest["editor.patch.propose"]["read_only"] is False
    assert manifest["editor.patch.apply"]["effect"] == "write"
    # Without the patch service the pair stays absent (degrades visibly).
    assert "editor.patch.propose" not in build_capability_registry().ids()


def test_patch_capability_propose_and_apply_roundtrip() -> None:
    service, persistence = _memory_service()
    asyncio.run(_seed_document(persistence))
    registry = build_capability_registry(editor_patch_service=service)
    context = CapabilityContext(
        principal=Principal(
            user_id=OWNER, kind="oidc_session", tenant_id="default", role="member"
        ),
        visible_to=scoped(OWNER),
        run_id="run-agent-1",
    )

    proposed = asyncio.run(
        registry.invoke(
            "editor.patch.propose",
            {
                "document_id": "ed_doc",
                "edits": [
                    {
                        "find": "Alpha beta gamma.",
                        "position": "replace",
                        "text": "Alpha kompakt.",
                    }
                ],
                "summary": "Agentenvorschlag",
            },
            context,
        )
    )
    assert proposed.status == "pending"
    assert proposed.revision_before == 3
    # The capability path pins source=agent and the run attribution.
    record = asyncio.run(service.store.get(proposed.patch_id))
    assert record.source == "agent"
    assert record.run_id == "run-agent-1"

    applied = asyncio.run(
        registry.invoke(
            "editor.patch.apply",
            {"patch_id": proposed.patch_id, "expected_revision": 3},
            context,
        )
    )
    assert applied.revision == 4
    assert applied.applied_edit_ids == ["ed_1"]


# ------------------------------------------------------------------ #
# HTTP route matrix (full oidc container, memory backend)
# ------------------------------------------------------------------ #


def make_world() -> tuple[TestClient, object]:
    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()

    async def mirror() -> None:
        for sub, name in ((OWNER, "Olga Owner"), (RECIPIENT, "Rita Recipient")):
            await users.record_login(
                tenant_id="default",
                issuer="http://idp.example",
                subject=str(sub),
                email=f"{sub}@example.com",
                email_verified=True,
                display_name=name,
                canonical_user_id=sub,
            )

    asyncio.run(mirror())
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=_CanonicalOidcHeaderProvider(users),
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
    )
    app = FastAPI()
    app.include_router(editor_persistence.build_router(container))
    app.include_router(editor_patches.build_router(container))
    return TestClient(app), container


def as_user(user_id: uuid.UUID) -> dict[str, str]:
    return {SUB_HEADER: str(user_id)}


def _seed_http_document(client: TestClient, *, revision: int = 3) -> None:
    response = client.put(
        "/v1/editor/documents/ed_doc",
        json={
            "title": "Bericht",
            "content_markdown": _DOC,
            "source": "blank",
            "revision": revision,
            "created_at": 1.0,
            "updated_at": 1.0,
        },
        headers=as_user(OWNER),
    )
    assert response.status_code == 200


def test_shared_document_contract_includes_owner_and_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, container = make_world()
    _seed_http_document(client)
    stored = asyncio.run(
        container.editor_persistence_service.get_document(
            "ed_doc",
            visible_to=scoped(OWNER),
        )
    )
    collaboration_document = replace(
        stored,
        collaboration_generation=1,
        collaboration_schema_version=1,
        content_mode="collaboration",
    )
    shared_access = ResourceAccess(
        mode=AccessMode.SHARED,
        permission=SharePermission.SUGGEST,
    )

    async def list_shared_documents(**_kwargs):
        return [
            (replace(collaboration_document, content_markdown=""), shared_access)
        ], None

    async def get_shared_document(
        _document_id: str,
        *,
        visible_to,
        minimum=SharePermission.VIEW,
    ):
        del visible_to, minimum
        return collaboration_document, shared_access

    monkeypatch.setattr(
        container.editor_persistence_service,
        "list_visible_documents",
        list_shared_documents,
    )
    monkeypatch.setattr(
        container.editor_persistence_service,
        "get_document_with_access",
        get_shared_document,
    )

    listed = client.get(
        "/v1/editor/documents?scope=all",
        headers=as_user(RECIPIENT),
    )
    assert listed.status_code == 200
    [row] = listed.json()["data"]
    assert "content_markdown" not in row
    assert row["access"] == {
        "mode": "shared",
        "owner": {"id": str(OWNER), "name": "Olga Owner"},
        "permission": "suggest",
    }

    detail = client.get(
        "/v1/editor/documents/ed_doc",
        headers=as_user(RECIPIENT),
    )
    assert detail.status_code == 200
    assert detail.json()["access"] == row["access"]


def _propose_http(container, *, edits: list[dict] | None = None) -> str:
    patch = asyncio.run(
        container.editor_patch_service.propose(
            document_id="ed_doc",
            run_id=None,
            source="instruct",
            edits=edits if edits is not None else _raw_edits(),
            summary="Zwei Aenderungen",
            warnings=["Ein Anker unsicher"],
            created_by_user_id=OWNER,
            visible_to=scoped(OWNER),
        )
    )
    return patch.patch_id


def test_http_patch_lifecycle_matrix() -> None:
    client, container = make_world()
    _seed_http_document(client)
    patch_id = _propose_http(container)

    # List: metadata only, no edit bodies; status filter works.
    listed = client.get(
        "/v1/editor/documents/ed_doc/patches", headers=as_user(OWNER)
    )
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert [row["patch_id"] for row in rows] == [patch_id]
    assert rows[0]["edit_count"] == 2
    assert rows[0]["status"] == "pending"
    assert "edits" not in rows[0]
    none_accepted = client.get(
        "/v1/editor/documents/ed_doc/patches?status=accepted",
        headers=as_user(OWNER),
    )
    assert none_accepted.json()["data"] == []
    bad_status = client.get(
        "/v1/editor/documents/ed_doc/patches?status=bogus",
        headers=as_user(OWNER),
    )
    assert bad_status.status_code == 400

    # Detail: full edits with ids, plus the CURRENT document revision.
    detail = client.get(
        f"/v1/editor/patches/{patch_id}", headers=as_user(OWNER)
    )
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["document_revision"] == 3
    assert [edit["id"] for edit in payload["edits"]] == ["ed_1", "ed_2"]
    assert payload["edits"][0]["position"] == "replace"
    assert payload["warnings"] == ["Ein Anker unsicher"]

    # Bool precondition is rejected explicitly (bool subclasses int).
    boolean = client.post(
        f"/v1/editor/patches/{patch_id}:apply",
        json={"expected_revision": True},
        headers=as_user(OWNER),
    )
    assert boolean.status_code == 400

    # Stale expected revision -> 409 with both revisions as extras.
    stale = client.post(
        f"/v1/editor/patches/{patch_id}:apply",
        json={"expected_revision": 2},
        headers=as_user(OWNER),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["current_revision"] == 3
    assert stale.json()["error"]["revision_before"] == 3

    # Happy apply: server-side edits, bumped revision, skipped anchor
    # visible as the missing ed_2.
    applied = client.post(
        f"/v1/editor/patches/{patch_id}:apply",
        json={"expected_revision": 3},
        headers=as_user(OWNER),
    )
    assert applied.status_code == 200
    assert applied.json() == {
        "document_id": "ed_doc",
        "revision": 4,
        "applied_edit_ids": ["ed_1"],
    }
    document = client.get(
        "/v1/editor/documents/ed_doc", headers=as_user(OWNER)
    ).json()
    assert document["revision"] == 4
    assert "Alpha verbessert." in document["content_markdown"]

    # Replay of the SAME apply answers 200 with the stored outcome.
    replay = client.post(
        f"/v1/editor/patches/{patch_id}:apply",
        json={"expected_revision": 3},
        headers=as_user(OWNER),
    )
    assert replay.status_code == 200
    assert replay.json() == applied.json()

    # A different expected revision on the decided patch conflicts.
    conflicting = client.post(
        f"/v1/editor/patches/{patch_id}:apply",
        json={"expected_revision": 4},
        headers=as_user(OWNER),
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["status"] == "accepted"

    # Reject after apply conflicts too.
    reject_late = client.post(
        f"/v1/editor/patches/{patch_id}:reject",
        json={"note": "zu spaet"},
        headers=as_user(OWNER),
    )
    assert reject_late.status_code == 409


def test_http_reject_flow_and_replay() -> None:
    client, container = make_world()
    _seed_http_document(client)
    patch_id = _propose_http(container)

    rejected = client.post(
        f"/v1/editor/patches/{patch_id}:reject",
        json={"note": "Passt nicht."},
        headers=as_user(OWNER),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["note"] == "Passt nicht."

    replay = client.post(
        f"/v1/editor/patches/{patch_id}:reject",
        json={},
        headers=as_user(OWNER),
    )
    assert replay.status_code == 200
    assert replay.json()["note"] == "Passt nicht."

    # Apply after reject -> 409, document untouched.
    late_apply = client.post(
        f"/v1/editor/patches/{patch_id}:apply",
        json={"expected_revision": 3},
        headers=as_user(OWNER),
    )
    assert late_apply.status_code == 409
    document = client.get(
        "/v1/editor/documents/ed_doc", headers=as_user(OWNER)
    ).json()
    assert document["revision"] == 3


def test_http_foreign_user_gets_indistinct_404() -> None:
    client, container = make_world()
    _seed_http_document(client)
    patch_id = _propose_http(container)

    listed = client.get(
        "/v1/editor/documents/ed_doc/patches", headers=as_user(RECIPIENT)
    )
    assert listed.status_code == 404
    assert listed.json()["error"]["message"] == "Document not found"

    for response in (
        client.get(f"/v1/editor/patches/{patch_id}", headers=as_user(RECIPIENT)),
        client.post(
            f"/v1/editor/patches/{patch_id}:apply",
            json={"expected_revision": 3},
            headers=as_user(RECIPIENT),
        ),
        client.post(
            f"/v1/editor/patches/{patch_id}:reject",
            json={},
            headers=as_user(RECIPIENT),
        ),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "Patch not found"

    # An unknown patch id answers byte-identically for the owner.
    unknown = client.get(
        "/v1/editor/patches/pch_missing", headers=as_user(OWNER)
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["message"] == "Patch not found"


def test_http_private_suggestion_draft_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nested private draft is revisioned, bounded and creator-scoped."""
    client, container = make_world()
    _seed_http_document(client)
    stored = asyncio.run(
        container.editor_persistence_service.store.get_document("ed_doc")
    )
    container.editor_persistence_service.store._documents[stored.id] = replace(
        stored,
        collaboration_generation=1,
        collaboration_schema_hash="0" * 64,
        collaboration_schema_version=1,
        content_mode="collaboration",
    )
    service = container.editor_persistence_service
    resolve_document_access = service._resolve_document_access

    async def shared_document_access(document, *, visible_to, minimum):
        if visible_to.principal.user_id == RECIPIENT:
            return ResourceAccess(
                mode=AccessMode.SHARED,
                permission=SharePermission.SUGGEST,
            )
        return await resolve_document_access(
            document,
            visible_to=visible_to,
            minimum=minimum,
        )

    monkeypatch.setattr(service, "_resolve_document_access", shared_document_access)
    comment_body = {
        "anchor": {
            "from": 10,
            "quoteAfter": "",
            "quoteBefore": "",
            "selectedText": "Alpha beta gamma.",
            "to": 27,
        },
        "comment_markdown": "Rewrite privately",
        "created_at": 2.0,
        "evidence_preset": None,
        "id": "edc_private_ai",
        "kind": "inline_edit",
        "status": "open",
        "updated_at": 2.0,
    }
    created_comment = client.post(
        "/v1/editor/documents/ed_doc/comments",
        json={"comments": [comment_body]},
        headers=as_user(OWNER),
    )
    assert created_comment.status_code == 201

    draft = {
        "anchor_version": 1,
        "change_summary": ["Shorter"],
        "evidence": None,
        "group_id": "editor-suggestion-group-http",
        "patch_id": "66666666-6666-4666-8666-666666666666",
        "proposed_text": "Alpha, improved.",
        "publication_command_id": "55555555-5555-4555-8555-555555555555",
        "suggestion_id": "editor-suggestion-http",
        "warnings": [],
    }
    endpoint = (
        "/v1/editor/documents/ed_doc/comments/edc_private_ai/"
        "suggestion-draft"
    )
    created = client.put(
        endpoint,
        json={"draft": draft, "expected_revision": 0},
        headers=as_user(OWNER),
    )
    assert created.status_code == 200
    assert created.json()["suggestion_draft"]["revision"] == 1

    listed = client.get(
        "/v1/editor/documents/ed_doc/comments",
        headers=as_user(OWNER),
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["suggestion_draft"]["patch_id"] == (
        draft["patch_id"]
    )

    recipient_list = client.get(
        "/v1/editor/documents/ed_doc/comments",
        headers=as_user(RECIPIENT),
    )
    assert recipient_list.status_code == 200
    assert recipient_list.json()["data"] == []

    foreign = client.put(
        endpoint,
        json={"draft": draft, "expected_revision": 0},
        headers=as_user(RECIPIENT),
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["message"] == "Draft not found"

    stale = client.put(
        endpoint,
        json={
            "draft": {
                "proposed_text": "Stale",
                "revision_source": "manual_edit",
            },
            "expected_revision": 0,
        },
        headers=as_user(OWNER),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["current_revision"] == 1

    too_large = client.put(
        endpoint,
        json={
            "draft": {
                "proposed_text": "x" * 1_048_577,
                "revision_source": "manual_edit",
            },
            "expected_revision": 1,
        },
        headers=as_user(OWNER),
    )
    assert too_large.status_code == 413

    deleted = client.request(
        "DELETE",
        endpoint,
        json={"expected_revision": 1, "patch_id": draft["patch_id"]},
        headers=as_user(OWNER),
    )
    assert deleted.status_code == 204
    after_delete = client.get(
        "/v1/editor/documents/ed_doc/comments",
        headers=as_user(OWNER),
    )
    assert after_delete.json()["data"][0]["suggestion_draft"] is None


# ------------------------------------------------------------------ #
# interleaved human autosave vs. patch apply (revision guard)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_interleaved_autosave_beats_patch_apply_with_409() -> None:
    """A human save landing between patch read and patch write WINS.

    The DB-level monotonic revision guard refuses the patch's document
    write built off the stale base — the user's keystrokes survive and
    the apply surfaces the conflict (PatchRevisionConflict) instead of
    silently clobbering. The patch stays pending for a fresh apply.
    """
    service, persistence = _memory_service()
    await _seed_document(persistence)

    patch = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="instruct",
        edits=_raw_edits(),
        summary="Aenderung",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
    )

    # The interleave: a human autosave advances the document AFTER the
    # apply request was formed (expected_revision=3) but BEFORE its
    # write. Service-level pre-check uses the fresh read, so simulate
    # the narrower DB-window by racing the store directly: save the
    # human's edit first, then apply with the stale expectation.
    await persistence.save_document(
        id="ed_doc",
        title="Bericht",
        content_markdown=_DOC + "\n\nHuman typed this.",
        folder_id=None,
        source="blank",
        source_run_id=None,
        revision=4,
        diff_anchor_markdown=None,
        diff_anchor_updated_at=None,
        created_at=1.0,
        updated_at=2.0,
        caller_user_id=OWNER,
        workspace_id=None,
        visible_to=scoped(OWNER),
    )
    with pytest.raises(PatchRevisionConflict) as conflict:
        await service.apply(
            patch.patch_id, expected_revision=3, visible_to=scoped(OWNER)
        )
    assert conflict.value.current_revision == 4
    document = await persistence.get_document("ed_doc", visible_to=scoped(OWNER))
    assert "Human typed this." in document.content_markdown
    assert document.revision == 4
    # The patch is still pending: a fresh apply against the live
    # revision succeeds (anchors re-resolve or skip visibly).
    fetched, live_revision = await service.get_patch(
        patch.patch_id, visible_to=scoped(OWNER)
    )
    assert fetched.status == "pending"
    applied = await service.apply(
        patch.patch_id, expected_revision=live_revision, visible_to=scoped(OWNER)
    )
    assert applied.status == "accepted"
    assert applied.applied_revision == 5


@pytest.mark.asyncio
async def test_db_window_conflict_disambiguates_replay_vs_interleave(
    monkeypatch,
) -> None:
    """The narrow DB window: doc moves between service read and write.

    (a) Parallel apply of the SAME patch landed first -> 200 replay.
    (b) Anything else (human autosave) -> PatchRevisionConflict, patch
        stays pending.
    """
    from inqtrix.project.editor_ports import DocumentRevisionConflict

    service, persistence = _memory_service()
    await _seed_document(persistence)
    patch = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="instruct",
        edits=_raw_edits(),
        summary="Aenderung",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
    )

    async def _lost_cas(*args, **kwargs):
        raise DocumentRevisionConflict(
            current_revision=4, expected_revision=3
        )

    # (b) Interleave shape: the write loses, the patch is still pending
    # -> the apply surfaces the conflict.
    monkeypatch.setattr(service, "_save_applied_document", _lost_cas)
    with pytest.raises(PatchRevisionConflict) as conflict:
        await service.apply(
            patch.patch_id, expected_revision=3, visible_to=scoped(OWNER)
        )
    assert conflict.value.current_revision == 4

    # (a) Replay shape: the PARALLEL apply of this very patch completes
    # (doc write + mark_applied) inside our write window — our lost CAS
    # must resolve as the stored 200 replay, not a conflict.
    store = service._store

    async def _parallel_apply_won(*args, **kwargs):
        await store.mark_applied(
            patch.patch_id,
            applied_revision=4,
            applied_edit_ids=["ed_1"],
        )
        raise DocumentRevisionConflict(
            current_revision=4, expected_revision=3
        )

    monkeypatch.setattr(
        service, "_save_applied_document", _parallel_apply_won
    )
    replay = await service.apply(
        patch.patch_id, expected_revision=3, visible_to=scoped(OWNER)
    )
    assert replay.status == "accepted"
    assert replay.applied_revision == 4


@pytest.mark.asyncio
async def test_same_patch_winner_wrote_doc_not_yet_marked_replays_not_409() -> None:
    """A document written but not yet marked applied must not 409.

    A same-patch parallel winner writes the document (content == this
    apply's result) but has not yet marked the patch applied. The loser
    must recognise the effect is present (content match) and complete
    with 200, NOT raise PatchRevisionConflict on a benign replay.
    """
    from inqtrix.project.editor_ports import DocumentRevisionConflict

    service, persistence = _memory_service()
    await _seed_document(persistence)
    patch = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="instruct",
        edits=_raw_edits(),
        summary="Aenderung",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
    )

    original_save = service._save_applied_document

    async def _winner_wrote_but_unmarked(
        document, *, content_markdown, revision, visible_to
    ):
        # Simulate the parallel winner: the doc IS advanced to this
        # apply's result, but the winner has not marked the patch yet.
        await persistence.save_document(
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
            updated_at=document.updated_at + 1,
            caller_user_id=OWNER,
            workspace_id=None,
            visible_to=scoped(OWNER),
        )
        raise DocumentRevisionConflict(
            current_revision=revision, expected_revision=revision - 1
        )

    service._save_applied_document = _winner_wrote_but_unmarked
    try:
        applied = await service.apply(
            patch.patch_id, expected_revision=3, visible_to=scoped(OWNER)
        )
    finally:
        service._save_applied_document = original_save
    # 200: the patch is marked applied (the doc already holds its result).
    assert applied.status == "accepted"
    assert applied.applied_revision == 4
    document = await persistence.get_document("ed_doc", visible_to=scoped(OWNER))
    assert "Alpha verbessert." in document.content_markdown
    assert document.revision == 4


@pytest.mark.asyncio
async def test_propose_expected_revision_pins_the_read_state() -> None:
    """A proposal pinned to the revision the caller READ succeeds
    only while the document has not moved; a stale pin conflicts with
    the SAME exception apply uses, and None keeps the legacy behavior."""
    service, persistence = _memory_service()
    await _seed_document(persistence, revision=3)

    pinned = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="agent",
        edits=_raw_edits(),
        summary="Gepinnt",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
        expected_revision=3,
    )
    assert pinned.revision_before == 3

    with pytest.raises(PatchRevisionConflict) as conflict:
        await service.propose(
            document_id="ed_doc",
            run_id=None,
            source="agent",
            edits=_raw_edits(),
            summary="Veraltet",
            warnings=[],
            created_by_user_id=OWNER,
            visible_to=scoped(OWNER),
            expected_revision=2,
        )
    assert conflict.value.current_revision == 3
    assert conflict.value.revision_before == 2

    legacy = await service.propose(
        document_id="ed_doc",
        run_id=None,
        source="agent",
        edits=_raw_edits(),
        summary="Ohne Pin",
        warnings=[],
        created_by_user_id=OWNER,
        visible_to=scoped(OWNER),
    )
    assert legacy.revision_before == 3


def test_anchor_parity_fixture_matches_the_python_resolver() -> None:
    """Cross-language parity: the shared fixture pins THE server
    semantics (byte-literal, hard quote disqualification, summed
    distance, tie abstention); anchoring.parity.test.ts runs the SAME
    cases against the TS port ``resolveAnchorInMarkdown``."""
    import json
    from pathlib import Path

    fixture = json.loads(
        Path(__file__).parent.joinpath(
            "fixtures/anchor_parity.json"
        ).read_text()
    )
    assert len(fixture["cases"]) >= 10
    for case in fixture["cases"]:
        got = _resolve_anchor(
            case["content"],
            case["find"],
            quote_before=case["quote_before"],
            quote_after=case["quote_after"],
        )
        assert got == case["expected"], case["name"]
