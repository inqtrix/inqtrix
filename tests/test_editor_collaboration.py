"""Observable HTTP contracts for editor live collaboration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.permissions import (
    AccessMode,
    ResourceAccess,
    SharePermission,
)
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.project.editor_collaboration_ports import (
    CollaborationActivity,
    CollaborationConflict,
    CollaborationCommentActivity,
    CollaborationCommentMessage,
    CollaborationCommentPage,
    CollaborationCommentThread,
    CollaborationDocumentNotFound,
    CollaborationDocumentState,
    CollaborationInstanceFenced,
    CollaborationInstanceLease,
    CollaborationLease,
    CollaborationLeaseInvalid,
    CollaborationLoadedState,
    CollaborationOpenPatch,
    CollaborationOpenPatchPage,
    CollaborationPersistedCommand,
    CollaborationRateLimited,
    CollaborationSnapshot,
    CollaborationSuggestionRejection,
    CollaborationSnapshotCandidate,
    CollaborationUpdate,
    CollaborationUpdateLookup,
    PersistCollaborationUpdate,
    PersistedCollaborationUpdate,
)
from inqtrix.project.editor_ports import DocumentNotFound, EditorDocument
from inqtrix.server.routers import capabilities as capabilities_router
from inqtrix.server.routers import editor_collaboration, internal_collaboration
from inqtrix.services.collaboration_client import (
    CollaborationConversion,
    CollaborationDecisionResult,
    CollaborationNodeClient,
    CollaborationProjection,
    CollaborationSuggestionResult,
)
from inqtrix.services.editor_collaboration_service import (
    CollaborationAuthenticationRequired,
    CollaborationDocumentTooLarge,
    CollaborationProtocolConflict,
    EditorCollaborationService,
)
from inqtrix.services.user_erasure import (
    DELETED_ACCOUNT_DISPLAY_NAME,
    DELETED_ACCOUNT_EMAIL,
    DELETED_ACCOUNT_ISSUER,
)
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    CollaborationSettings,
    QuotaSettings,
)


DOCUMENT_ID = "ed_collaboration"
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
#: The tenant's deleted-account placeholder; edits of erased accounts point here.
ERASED_EDITOR_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
SESSION_ID = "session-collaboration-test"
INTERNAL_SECRET = "collaboration-internal-test-secret"
INTERNAL_HEADERS = {"Authorization": f"Bearer {INTERNAL_SECRET}"}
INTERNAL_TENANT_ID = "tenant-collaboration-test"


def _sha256(value: bytes) -> str:
    """Return the canonical lowercase digest used by collaboration payloads."""
    return hashlib.sha256(value).hexdigest()


def test_collaboration_settings_canonicalize_the_deployment_tenant() -> None:
    """The API and Node share one bounded nonblank tenant setting."""
    settings = CollaborationSettings.model_validate(
        {"tenant_id": "  tenant-primary  "}
    )

    assert settings.tenant_id == "tenant-primary"
    with pytest.raises(ValueError, match="must not be blank"):
        CollaborationSettings.model_validate({"tenant_id": "   "})


def _cookie_principal() -> Principal:
    """Return a fully scoped browser-session principal."""
    return Principal(
        user_id=USER_ID,
        kind="oidc_session",
        tenant_id="default",
        role="member",
        display_name="Ada Editor",
        email="ada@example.test",
        session_id=SESSION_ID,
    )


def _document(*, collaboration: bool = False) -> EditorDocument:
    """Build one owner document in either Markdown or collaboration mode."""
    return EditorDocument(
        id=DOCUMENT_ID,
        title="Shared draft",
        content_markdown="# Shared draft\n\nInitial body.",
        revision=7,
        metadata_revision=3,
        created_by_user_id=USER_ID,
        content_mode="collaboration" if collaboration else "markdown",
        collaboration_generation=4 if collaboration else 0,
        collaboration_schema_version=1 if collaboration else None,
        collaboration_schema_hash=_sha256(b"schema") if collaboration else None,
        persisted_sequence=11 if collaboration else 0,
        projection_sequence=10 if collaboration else 0,
    )


class _Documents:
    """Narrow document port fake retaining the authoritative document record."""

    def __init__(
        self,
        document: EditorDocument,
        access: ResourceAccess | None = None,
    ) -> None:
        self.document = document
        self.access = access or ResourceAccess(AccessMode.OWNER)
        self.get_calls: list[dict[str, Any]] = []
        self.facts_calls: list[dict[str, Any]] = []

    async def get_document(
        self,
        document_id: str,
        *,
        visible_to: UserContext | None,
        minimum: Any,
    ) -> EditorDocument:
        """Return the document while recording the requested access floor."""
        self.get_calls.append(
            {
                "document_id": document_id,
                "visible_to": visible_to,
                "minimum": minimum,
            }
        )
        if document_id != self.document.id:
            raise DocumentNotFound(document_id)
        return self.document

    async def get_document_facts(
        self,
        document_id: str,
        *,
        visible_to: UserContext | None,
        minimum: Any,
    ) -> Any:
        """Return the body-free facts while recording the access floor."""
        from inqtrix.project.editor_ports import EditorDocumentFacts

        self.facts_calls.append(
            {
                "document_id": document_id,
                "visible_to": visible_to,
                "minimum": minimum,
            }
        )
        if document_id != self.document.id:
            raise DocumentNotFound(document_id)
        return EditorDocumentFacts(
            id=self.document.id,
            tenant_id=self.document.tenant_id,
            created_by_user_id=self.document.created_by_user_id,
            title=self.document.title,
            content_mode=self.document.content_mode,
            collaboration_generation=self.document.collaboration_generation,
            deleted_at=self.document.deleted_at,
        )

    async def get_document_with_access(
        self,
        document_id: str,
        *,
        visible_to: UserContext | None,
        minimum: Any,
    ) -> tuple[EditorDocument, ResourceAccess]:
        """Return the document with owner access for session issuance."""
        document = await self.get_document(
            document_id,
            visible_to=visible_to,
            minimum=minimum,
        )
        if (
            self.access.mode is AccessMode.SHARED
            and (
                self.access.permission is None
                or not self.access.permission.at_least(minimum)
            )
        ):
            raise DocumentNotFound(document_id)
        return document, self.access


class _Node:
    """Side-effect-free collaboration sidecar fake used by the real service."""

    def __init__(self) -> None:
        self.ready = True
        self.convert_calls: list[dict[str, Any]] = []
        self.decide_calls: list[dict[str, Any]] = []
        self.project_calls: list[dict[str, Any]] = []
        self.projection_results: list[CollaborationProjection] = []
        self.publish_calls: list[dict[str, Any]] = []
        self.conversion = CollaborationConversion(
            schema_hash=_sha256(b"schema"),
            state_update=b"initial-yjs-state",
            state_vector=b"initial-yjs-vector",
            state_hash=_sha256(b"initial-yjs-state"),
            projection_markdown="# Shared draft\n\nInitial body.",
            projection_hash=_sha256(b"# Shared draft\n\nInitial body."),
        )

    async def available(self) -> bool:
        """Report the configured readiness state."""
        return self.ready

    async def convert(
        self,
        *,
        document_id: str,
        markdown: str,
        schema_version: int,
        max_document_bytes: int,
    ) -> CollaborationConversion:
        """Record conversion input and return a verified binary snapshot."""
        self.convert_calls.append(
            {
                "document_id": document_id,
                "markdown": markdown,
                "schema_version": schema_version,
                "max_document_bytes": max_document_bytes,
            }
        )
        return self.conversion

    async def decide(self, **kwargs: Any) -> CollaborationDecisionResult:
        """Record one public decision and return its durable command result."""
        self.decide_calls.append(dict(kwargs))
        return CollaborationDecisionResult(
            command_id=kwargs["command_id"],
            sequence=kwargs["expected_sequence"] + 1,
            suggestion_ids=("suggestion-decision",),
        )

    async def project(self, **kwargs: Any) -> CollaborationProjection:
        """Return a projection whose publication may race a later update."""
        self.project_calls.append(dict(kwargs))
        if self.projection_results:
            return self.projection_results.pop(0)
        return CollaborationProjection(
            generation=4,
            sequence=12,
            markdown="# Projected\n\nDurable body.",
            projection_hash=_sha256(b"# Projected\n\nDurable body."),
            schema_hash=_sha256(b"schema"),
        )

    async def publish_suggestion(
        self, **kwargs: Any
    ) -> CollaborationSuggestionResult:
        """Record a private-patch publication and return its durable identity."""
        self.publish_calls.append(dict(kwargs))
        return CollaborationSuggestionResult(
            command_id=kwargs["command_id"],
            patch_id=kwargs["patch_id"],
            sequence=12,
            suggestion_ids=(
                str(uuid.UUID("77777777-7777-4777-8777-777777777777")),
            ),
        )

    async def aclose(self) -> None:
        """Satisfy the service lifecycle contract."""


class _PublicStore:
    """Lease-aware collaboration port fake for public service behavior."""

    def __init__(self, documents: _Documents) -> None:
        self.documents = documents
        self.enable_calls: list[dict[str, Any]] = []
        self.issue_calls: list[CollaborationLease] = []
        self.rotate_calls: list[dict[str, Any]] = []
        self.leases: dict[uuid.UUID, CollaborationLease] = {}
        self.rotation_results: dict[uuid.UUID, CollaborationLease] = {}
        self.issue_error: Exception | None = None
        self.rotate_error: Exception | None = None
        self.activity_calls: list[dict[str, Any]] = []
        self.activity_rows: tuple[CollaborationActivity, ...] = ()
        self.comment_activity_rows: tuple[CollaborationCommentActivity, ...] = ()
        self.rejection_rows: tuple[CollaborationSuggestionRejection, ...] = ()
        self.rejection_calls: list[dict[str, Any]] = []
        self.open_patch_calls: list[dict[str, Any]] = []
        self.open_patch_rows: tuple[CollaborationOpenPatch, ...] = ()
        self.open_patch_next_cursor: tuple[float, str] | None = None
        self.open_patch_pages: dict[
            tuple[float, str] | None, CollaborationOpenPatchPage
        ] = {}
        self.projection_authoritative_sequences: list[int] = []
        self.current_instance_results: list[
            CollaborationInstanceLease | None
        ] = []
        self.current_instance_calls: list[dict[str, Any]] = []
        self.instance_validation_calls: list[dict[str, Any]] = []
        self.policy_cursor = 0
        self.policy_cursor_calls: list[str] = []
        self.compact_calls: list[dict[str, Any]] = []
        self.purge_calls: list[dict[str, Any]] = []
        self.comment_threads: dict[uuid.UUID, CollaborationCommentThread] = {}
        self.comment_revision = 0
        self.comment_read_revision = 0
        self.comment_calls: list[dict[str, Any]] = []

    async def enable_document(self, **kwargs: Any) -> CollaborationDocumentState:
        """Apply activation atomically to the fake document authority."""
        self.enable_calls.append(dict(kwargs))
        document = self.documents.document
        schema_hash = str(kwargs["schema_hash"])
        projection_markdown = str(kwargs["projection_markdown"])
        self.documents.document = replace(
            document,
            content_markdown=projection_markdown,
            content_mode="collaboration",
            collaboration_generation=1,
            collaboration_schema_version=int(kwargs["schema_version"]),
            collaboration_schema_hash=schema_hash,
            persisted_sequence=0,
            projection_sequence=0,
        )
        return CollaborationDocumentState(
            document_id=document.id,
            tenant_id=document.tenant_id,
            generation=1,
            schema_version=int(kwargs["schema_version"]),
            schema_hash=schema_hash,
            persisted_sequence=0,
            projection_sequence=0,
            content_markdown=projection_markdown,
            projection_updated_at=float(kwargs["now"]),
            owner_user_id=USER_ID,
        )

    async def issue_lease(
        self,
        lease: CollaborationLease,
        *,
        max_active: int,
        max_issued_per_window: int,
        issued_since: float,
    ) -> CollaborationLease:
        """Persist one lease or expose the configured durable rate error."""
        del max_active, max_issued_per_window, issued_since
        if self.issue_error is not None:
            raise self.issue_error
        self.issue_calls.append(lease)
        self.leases[lease.lease_id] = lease
        return lease

    async def rotate_lease(
        self,
        *,
        previous_lease_id: uuid.UUID,
        previous_token_hash: str,
        replacement: CollaborationLease,
        max_issued_per_window: int,
        issued_since: float,
    ) -> CollaborationLease:
        """Atomically replace exactly the live lease named by its token hash."""
        del max_issued_per_window, issued_since
        if self.rotate_error is not None:
            raise self.rotate_error
        if replacement.rotation_command_id in self.rotation_results:
            return self.rotation_results[replacement.rotation_command_id]
        previous = self.leases.get(previous_lease_id)
        if previous is None or previous.token_hash != previous_token_hash:
            raise CollaborationLeaseInvalid("lease_invalid")
        self.rotate_calls.append(
            {
                "previous_lease_id": previous_lease_id,
                "previous_token_hash": previous_token_hash,
                "replacement": replacement,
            }
        )
        del self.leases[previous_lease_id]
        self.leases[replacement.lease_id] = replacement
        if replacement.rotation_command_id is not None:
            self.rotation_results[replacement.rotation_command_id] = replacement
        return replacement

    async def list_activity(
        self, **kwargs: Any
    ) -> tuple[CollaborationActivity, ...]:
        """Return configured durable history after recording its filters."""
        self.activity_calls.append(dict(kwargs))
        return self.activity_rows

    async def list_comment_activity(
        self, **kwargs: Any
    ) -> tuple[CollaborationCommentActivity, ...]:
        """Return configured content-free shared-comment activity."""
        self.activity_calls.append(dict(kwargs))
        return self.comment_activity_rows

    async def list_suggestion_rejections(
        self, **kwargs: Any
    ) -> tuple[CollaborationSuggestionRejection, ...]:
        """Return the configured audit entries that match the filters."""
        self.rejection_calls.append(dict(kwargs))
        return tuple(
            row for row in self.rejection_rows if row.sequence in kwargs["sequences"]
        )

    async def list_open_patches(
        self, **kwargs: Any
    ) -> CollaborationOpenPatchPage:
        """Return configured pending patches after recording their filters."""
        self.open_patch_calls.append(dict(kwargs))
        if self.open_patch_pages:
            return self.open_patch_pages[kwargs["before"]]
        return CollaborationOpenPatchPage(
            patches=self.open_patch_rows,
            next_cursor=self.open_patch_next_cursor,
        )

    async def update_projection(
        self, **kwargs: Any
    ) -> CollaborationDocumentState:
        """Publish a projection while retaining the newest global sequence."""
        document = self.documents.document
        persisted_sequence = (
            self.projection_authoritative_sequences.pop(0)
            if self.projection_authoritative_sequences
            else max(document.persisted_sequence, kwargs["covered_sequence"])
        )
        self.documents.document = replace(
            document,
            content_markdown=kwargs["content_markdown"],
            persisted_sequence=persisted_sequence,
            projection_sequence=kwargs["covered_sequence"],
            projection_updated_at=kwargs["now"],
        )
        return CollaborationDocumentState(
            document_id=document.id,
            tenant_id=document.tenant_id,
            generation=document.collaboration_generation,
            schema_version=document.collaboration_schema_version or 1,
            schema_hash=document.collaboration_schema_hash or _sha256(b"schema"),
            persisted_sequence=persisted_sequence,
            projection_sequence=kwargs["covered_sequence"],
            content_markdown=kwargs["content_markdown"],
            projection_updated_at=kwargs["now"],
            owner_user_id=document.created_by_user_id,
        )

    async def validate_instance(self, **kwargs: Any) -> None:
        """Record the concrete service's tenant-scoped instance fence."""
        self.instance_validation_calls.append(dict(kwargs))

    async def current_policy_cursor(self, *, tenant_id: str) -> int:
        """Return the configured tenant cursor without exposing event content."""
        self.policy_cursor_calls.append(tenant_id)
        return self.policy_cursor

    async def get_current_instance(
        self, **kwargs: Any
    ) -> CollaborationInstanceLease | None:
        """Return scripted authoritative instance reads for readiness probes."""
        self.current_instance_calls.append(dict(kwargs))
        if self.current_instance_results:
            return self.current_instance_results.pop(0)
        return None

    async def compact(self, **kwargs: Any) -> tuple[int, int]:
        """Record one tenant-scoped document compaction."""
        self.compact_calls.append(dict(kwargs))
        return 2, 3

    async def purge_tombstones(self, **kwargs: Any) -> int:
        """Record one tenant-scoped tombstone purge."""
        self.purge_calls.append(dict(kwargs))
        return 4

    async def list_comment_threads(
        self, **kwargs: Any
    ) -> CollaborationCommentPage:
        """Return incremental shared-comment rows and participant identities."""
        self.comment_calls.append({"operation": "list", **kwargs})
        status = kwargs["status"]
        since_revision = kwargs["since_revision"]
        rows = tuple(
            thread
            for thread in self.comment_threads.values()
            if thread.revision > since_revision
            and (
                since_revision > 0
                or status == "all"
                or thread.status == status
            )
        )
        return CollaborationCommentPage(
            threads=tuple(sorted(rows, key=lambda thread: thread.revision)),
            revision=self.comment_revision,
            last_read_revision=self.comment_read_revision,
            participant_user_ids=(USER_ID, ACTOR_ID),
        )

    async def create_comment_thread(
        self, **kwargs: Any
    ) -> CollaborationCommentThread:
        """Create one fake durable thread with its first message.

        Like the real store, a new thread has no revision precondition.
        """
        self.comment_calls.append({"operation": "create", **kwargs})
        existing = self.comment_threads.get(kwargs["thread_id"])
        if existing is not None:
            return existing
        self.comment_revision += 1
        message = CollaborationCommentMessage(
            message_id=kwargs["message_id"],
            thread_id=kwargs["thread_id"],
            revision=self.comment_revision,
            author_user_id=kwargs["actor_user_id"],
            body_markdown=kwargs["body_markdown"],
            mention_user_ids=kwargs["mention_user_ids"],
            created_at=kwargs["now"],
        )
        thread = CollaborationCommentThread(
            thread_id=kwargs["thread_id"],
            document_id=kwargs["document_id"],
            generation=kwargs["generation"],
            revision=self.comment_revision,
            status="open",
            created_by_user_id=kwargs["actor_user_id"],
            resolved_by_user_id=None,
            resolved_at=None,
            anchor=kwargs["anchor"],
            quote_text=kwargs["quote_text"],
            created_at=kwargs["now"],
            updated_at=kwargs["now"],
            messages=(message,),
        )
        self.comment_threads[thread.thread_id] = thread
        return thread

    async def add_comment_reply(
        self, **kwargs: Any
    ) -> CollaborationCommentThread:
        """Append one fake reply under thread revision CAS."""
        self.comment_calls.append({"operation": "reply", **kwargs})
        thread = self.comment_threads[kwargs["thread_id"]]
        if kwargs["expected_revision"] != thread.revision:
            raise CollaborationConflict(
                "comment_revision_conflict",
                current_sequence=thread.revision,
            )
        self.comment_revision += 1
        message = CollaborationCommentMessage(
            message_id=kwargs["message_id"],
            thread_id=thread.thread_id,
            revision=self.comment_revision,
            author_user_id=kwargs["actor_user_id"],
            body_markdown=kwargs["body_markdown"],
            mention_user_ids=kwargs["mention_user_ids"],
            created_at=kwargs["now"],
        )
        updated = replace(
            thread,
            messages=(*thread.messages, message),
            revision=self.comment_revision,
            updated_at=kwargs["now"],
        )
        self.comment_threads[thread.thread_id] = updated
        return updated

    async def update_comment_message(
        self, **kwargs: Any
    ) -> CollaborationCommentThread:
        """Edit or tombstone one fake author contribution."""
        self.comment_calls.append({"operation": "message", **kwargs})
        thread = self.comment_threads[kwargs["thread_id"]]
        if kwargs["expected_revision"] != thread.revision:
            raise CollaborationConflict(
                "comment_revision_conflict",
                current_sequence=thread.revision,
            )
        target = next(
            message
            for message in thread.messages
            if message.message_id == kwargs["message_id"]
        )
        if target.author_user_id != kwargs["actor_user_id"]:
            raise CollaborationConflict("comment_author_required")
        self.comment_revision += 1
        updated_message = replace(
            target,
            body_markdown=(
                "" if kwargs["delete_message"] else kwargs["body_markdown"]
            ),
            deleted_at=kwargs["now"] if kwargs["delete_message"] else None,
            edited_at=None if kwargs["delete_message"] else kwargs["now"],
            mention_user_ids=(
                () if kwargs["delete_message"] else kwargs["mention_user_ids"]
            ),
            revision=self.comment_revision,
        )
        updated = replace(
            thread,
            messages=tuple(
                updated_message if message.message_id == target.message_id
                else message
                for message in thread.messages
            ),
            revision=self.comment_revision,
            updated_at=kwargs["now"],
        )
        self.comment_threads[thread.thread_id] = updated
        return updated

    async def set_comment_thread_status(
        self, **kwargs: Any
    ) -> CollaborationCommentThread:
        """Resolve or reopen one fake thread under the service policy."""
        self.comment_calls.append({"operation": "status", **kwargs})
        thread = self.comment_threads[kwargs["thread_id"]]
        if kwargs["expected_revision"] != thread.revision:
            raise CollaborationConflict(
                "comment_revision_conflict",
                current_sequence=thread.revision,
            )
        if (
            thread.created_by_user_id != kwargs["actor_user_id"]
            and not kwargs["can_moderate"]
        ):
            raise CollaborationConflict("comment_resolve_forbidden")
        self.comment_revision += 1
        resolved = kwargs["status"] == "resolved"
        updated = replace(
            thread,
            revision=self.comment_revision,
            status=kwargs["status"],
            resolved_by_user_id=kwargs["actor_user_id"] if resolved else None,
            resolved_at=kwargs["now"] if resolved else None,
            updated_at=kwargs["now"],
        )
        self.comment_threads[thread.thread_id] = updated
        return updated

    async def delete_comment_thread(
        self, **kwargs: Any
    ) -> CollaborationCommentThread:
        """Retire one fake thread under the author-only policy."""
        self.comment_calls.append({"operation": "delete_thread", **kwargs})
        thread = self.comment_threads[kwargs["thread_id"]]
        if kwargs["expected_revision"] != thread.revision:
            raise CollaborationConflict(
                "comment_revision_conflict",
                current_sequence=thread.revision,
            )
        # Deliberately no moderation escape: deleting takes other people's
        # replies out of view, so only the author may.
        if thread.created_by_user_id != kwargs["actor_user_id"]:
            raise CollaborationConflict("comment_delete_forbidden")
        self.comment_revision += 1
        updated = replace(
            thread,
            revision=self.comment_revision,
            deleted_at=kwargs["now"],
            updated_at=kwargs["now"],
        )
        self.comment_threads[thread.thread_id] = updated
        return updated

    async def mark_comments_read(self, **kwargs: Any) -> int:
        """Advance the fake personal read coordinate."""
        self.comment_calls.append({"operation": "read", **kwargs})
        self.comment_read_revision = max(
            self.comment_read_revision,
            kwargs["revision"],
        )
        return self.comment_read_revision


class _Users:
    """Minimal mirrored-user directory for activity labels."""

    async def profiles_for_user_ids(
        self, *, tenant_id: str, user_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, Any]:
        del tenant_id
        return {
            user_id: (
                SimpleNamespace(
                    display_name=DELETED_ACCOUNT_DISPLAY_NAME,
                    email=DELETED_ACCOUNT_EMAIL,
                    issuer=DELETED_ACCOUNT_ISSUER,
                )
                if user_id == ERASED_EDITOR_ID
                else SimpleNamespace(
                    display_name=(
                        "Ada Editor" if user_id == USER_ID else "Other Editor"
                    ),
                    email=None,
                    issuer="https://idp.example",
                )
            )
            for user_id in user_ids
        }


@dataclass(frozen=True)
class _PublicHarness:
    """Bound public API test client and its observable collaborators."""

    client: TestClient
    documents: _Documents
    node: _Node
    service: EditorCollaborationService
    store: _PublicStore


def _public_harness(
    *,
    principal: Principal | None = None,
    collaboration: bool = False,
    permission: SharePermission | None = None,
    tenant_id: str = "default",
    guest_links: Any | None = None,
) -> _PublicHarness:
    """Wire the real public router and service around bounded port fakes."""
    active_principal = principal or _cookie_principal()
    documents = _Documents(
        _document(collaboration=collaboration),
        ResourceAccess(AccessMode.SHARED, permission)
        if permission is not None
        else None,
    )
    node = _Node()
    store = _PublicStore(documents)
    settings = SimpleNamespace(
        secret=INTERNAL_SECRET,
        tenant_id=tenant_id,
        protocol_version=1,
        schema_version=1,
        max_document_bytes=1024 * 1024,
        max_frame_bytes=64 * 1024,
        max_queued_bytes=512 * 1024,
        max_queued_frames=11,
        update_rate_count=77,
        update_rate_window_seconds=9,
        lease_ttl_seconds=60,
        token_refresh_seconds=17,
        provider_flush_ms=75,
        max_sessions_per_user_document=5,
        session_rate_per_minute=30,
        instance_lease_seconds=15.0,
        update_payload_retention_seconds=60.0,
        activity_retention_seconds=120.0,
        tombstone_retention_seconds=180.0,
    )
    service = EditorCollaborationService(
        store=store,
        documents=documents,
        node=node,
        settings=settings,
        users=_Users(),
        guest_links=guest_links,
    )

    def principal_dependency() -> Principal:
        return active_principal

    def user_context_dependency() -> UserContext | None:
        if active_principal.user_id is None:
            return None
        return UserContext(principal=active_principal)

    container = SimpleNamespace(
        editor_collaboration_service=service,
        principal_dependency=principal_dependency,
        settings=SimpleNamespace(collaboration=settings),
        user_context_dependency=user_context_dependency,
    )
    app = FastAPI()
    app.include_router(editor_collaboration.build_router(container))
    return _PublicHarness(
        client=TestClient(app, raise_server_exceptions=False),
        documents=documents,
        node=node,
        service=service,
        store=store,
    )


# The limits the public harness configures, spelled out independently of its
# settings object: every value differs from the setting's default and from
# every other published limit, so a swapped, dropped or defaulted field shows.
_PUBLISHED_TRANSPORT_LIMITS = {
    "update_rate_count": 77,
    "update_rate_window_seconds": 9,
    "max_queued_frames": 11,
    "max_frame_bytes": 65_536,
    "max_queued_bytes": 524_288,
}


def _session_body(
    *,
    lease_token: str | None = None,
    rotation_command_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Return the public session request contract."""
    body: dict[str, Any] = {"protocol_version": 1, "schema_version": 1}
    if lease_token is not None:
        body["lease_token"] = lease_token
    if rotation_command_id is not None:
        body["rotation_command_id"] = str(rotation_command_id)
    return body


def _comment_body(
    *,
    expected_revision: int = 0,
    thread_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Return one bounded public shared-comment create command."""
    return {
        "anchor": {
            "from": 1,
            "to": 7,
            "quoteBefore": "",
            "selectedText": "Shared",
            "quoteAfter": " draft",
            "relativeFrom": "relative-from",
            "relativeTo": "relative-to",
            "relativeVersion": "yjs-relative-position-base64-v1",
        },
        "body_markdown": "Please review this wording.",
        "command_id": str(uuid.uuid4()),
        "expected_revision": expected_revision,
        "generation": 4,
        "mention_user_ids": [str(ACTOR_ID)],
        "message_id": str(message_id or uuid.uuid4()),
        "quote": "Shared",
        "thread_id": str(thread_id or uuid.uuid4()),
    }


def test_public_shared_comment_thread_deletion_is_author_only() -> None:
    """Retiring a whole thread is the author's alone, and it leaves a record.

    Resolving hides a discussion that happened; deletion is for one that never
    should have existed, such as a card added by accident. Unlike resolving
    there is no moderation escape, because deleting takes other people's
    replies out of view.
    """
    harness = _public_harness(collaboration=True)
    thread_id = uuid.uuid4()
    with harness.client:
        created = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments",
            json=_comment_body(thread_id=thread_id),
        )
        assert created.status_code == 200
        assert created.json()["thread"]["can_delete"] is True
        assert created.json()["thread"]["deleted_at"] is None

        deleted = harness.client.request(
            "DELETE",
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/"
            f"{thread_id}",
            json={
                "command_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "generation": 4,
            },
        )
        assert deleted.status_code == 200
        thread = deleted.json()["thread"]
        # The tombstone is what tells a client holding the card to drop it,
        # and what keeps the row readable for an audit.
        assert thread["deleted_at"] is not None
        assert thread["can_delete"] is False

    operations = [call["operation"] for call in harness.store.comment_calls]
    assert "delete_thread" in operations


def test_public_shared_comment_thread_deletion_refuses_a_stranger() -> None:
    """Someone else's thread stays put, moderation rights or not."""
    harness = _public_harness(collaboration=True)
    thread_id = uuid.uuid4()
    with harness.client:
        created = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments",
            json=_comment_body(thread_id=thread_id),
        )
        assert created.status_code == 200
        harness.store.comment_threads[thread_id] = replace(
            harness.store.comment_threads[thread_id],
            created_by_user_id=ACTOR_ID,
        )
        refused = harness.client.request(
            "DELETE",
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/"
            f"{thread_id}",
            json={
                "command_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "generation": 4,
            },
        )
        assert refused.status_code == 409
        assert (
            refused.json()["error"]["reason"] == "comment_delete_forbidden"
        )
        assert harness.store.comment_threads[thread_id].deleted_at is None


def test_public_shared_comment_thread_lifecycle_is_author_enriched() -> None:
    """Create, reply, edit, resolve, tombstone, list, and read stay separate."""
    harness = _public_harness(collaboration=True)
    thread_id = uuid.uuid4()
    first_message_id = uuid.uuid4()
    reply_id = uuid.uuid4()

    with harness.client:
        created = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments",
            json=_comment_body(
                thread_id=thread_id,
                message_id=first_message_id,
            ),
        )
        assert created.status_code == 200
        created_thread = created.json()["thread"]
        assert created_thread["author"]["name"] == "Ada Editor"
        assert created_thread["messages"][0]["mentions"] == [
            {"id": str(ACTOR_ID), "name": "Other Editor"}
        ]
        replied = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/"
            f"{thread_id}/replies",
            json={
                "body_markdown": "I agree.",
                "command_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "generation": 4,
                "mention_user_ids": [],
                "message_id": str(reply_id),
            },
        )
        assert replied.status_code == 200
        edited = harness.client.patch(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/"
            f"{thread_id}/messages/{reply_id}",
            json={
                "body_markdown": "I agree with this revision.",
                "command_id": str(uuid.uuid4()),
                "expected_revision": 2,
                "generation": 4,
                "mention_user_ids": [],
            },
        )
        assert edited.status_code == 200
        resolved = harness.client.patch(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/"
            f"{thread_id}",
            json={
                "command_id": str(uuid.uuid4()),
                "expected_revision": 3,
                "generation": 4,
                "status": "resolved",
            },
        )
        assert resolved.status_code == 200
        deleted = harness.client.request(
            "DELETE",
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/"
            f"{thread_id}/messages/{reply_id}",
            json={
                "command_id": str(uuid.uuid4()),
                "expected_revision": 4,
                "generation": 4,
            },
        )
        assert deleted.status_code == 200
        assert deleted.json()["thread"]["messages"][1] == {
            **deleted.json()["thread"]["messages"][1],
            "body_markdown": None,
            "can_delete": False,
            "can_edit": False,
        }
        listed = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments"
        )
        assert listed.status_code == 200
        assert listed.json()["current_revision"] == 5
        assert listed.json()["has_more"] is False
        assert listed.json()["participants"] == [
            {"id": str(USER_ID), "name": "Ada Editor"},
            {"id": str(ACTOR_ID), "name": "Other Editor"},
        ]
        assert listed.json()["data"][0]["status"] == "resolved"
        read = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/read",
            json={"generation": 4, "revision": 5},
        )

    assert read.status_code == 200
    assert read.json() == {"last_read_revision": 5}
    assert [
        call["operation"] for call in harness.store.comment_calls
    ] == ["create", "reply", "message", "status", "message", "list", "read"]


def test_public_shared_comment_retries_a_postgres_deadlock_victim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully rolled-back SQLSTATE 40P01 is replayed at the command seam."""
    harness = _public_harness(collaboration=True)
    original_create = harness.store.create_comment_thread
    attempts = 0

    class DeadlockVictim(RuntimeError):
        sqlstate = "40P01"

    async def create_after_one_deadlock(**kwargs: Any):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DeadlockVictim("deadlock detected")
        return await original_create(**kwargs)

    monkeypatch.setattr(
        harness.store,
        "create_comment_thread",
        create_after_one_deadlock,
    )

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments",
            json=_comment_body(),
        )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert attempts == 2
    assert [
        call["operation"] for call in harness.store.comment_calls
    ] == ["create"]


def test_public_shared_comments_enforce_view_and_suggest_permissions() -> None:
    """View-only stays read-only; suggest access may create and resolve own."""
    view = _public_harness(
        collaboration=True,
        permission=SharePermission.VIEW,
    )
    suggest = _public_harness(
        collaboration=True,
        permission=SharePermission.SUGGEST,
    )

    with view.client:
        denied = view.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments",
            json=_comment_body(),
        )
        visible = view.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments"
        )
    assert denied.status_code == 404
    assert visible.status_code == 200

    thread_id = uuid.uuid4()
    with suggest.client:
        created = suggest.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments",
            json=_comment_body(thread_id=thread_id),
        )
        resolved = suggest.client.patch(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments/"
            f"{thread_id}",
            json={
                "command_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "generation": 4,
                "status": "resolved",
            },
        )
    assert created.status_code == 200
    assert resolved.status_code == 200


def test_public_shared_comment_validation_is_content_bounded() -> None:
    """Malformed anchors and oversized bodies never reach persistence."""
    harness = _public_harness(collaboration=True)
    invalid = _comment_body()
    invalid["body_markdown"] = "x" * 8_193
    invalid["anchor"]["from"] = 20
    invalid["anchor"]["to"] = 10

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/comments",
            json=invalid,
        )

    assert response.status_code == 400
    assert harness.store.comment_calls == []


def test_public_activation_converts_owner_document_atomically() -> None:
    """Activation returns the durable collaboration state, not Node internals."""
    harness = _public_harness()

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration:enable",
            json={
                "expected_revision": 7,
                "expected_metadata_revision": 3,
                "schema_version": 1,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "content_mode": "collaboration",
        "generation": 1,
        "schema_version": 1,
        "schema_hash": _sha256(b"schema"),
        "persisted_sequence": 0,
        "projection_sequence": 0,
    }
    assert harness.node.convert_calls == [
        {
            "document_id": DOCUMENT_ID,
            "markdown": "# Shared draft\n\nInitial body.",
            "schema_version": 1,
            "max_document_bytes": 1024 * 1024,
        }
    ]
    activation = harness.store.enable_calls[0]
    assert activation["expected_revision"] == 7
    assert activation["expected_metadata_revision"] == 3
    assert activation["snapshot"].state_update == b"initial-yjs-state"
    assert harness.documents.document.content_mode == "collaboration"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration:enable",
            {
                "expected_revision": 7,
                "expected_metadata_revision": 3,
                "schema_version": 1,
            },
        ),
        (
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            _session_body(),
        ),
        (
            f"/v1/editor/documents/{DOCUMENT_ID}/suggestions:publish",
            {
                "patch_id": str(
                    uuid.UUID("66666666-6666-4666-8666-666666666666")
                ),
                "command_id": str(
                    uuid.UUID("55555555-5555-4555-8555-555555555555")
                ),
                "actor_kind": "assistant",
                "expected_sequence": 11,
                "target_markdown": "# Suggested body",
            },
        ),
    ],
)
def test_public_collaboration_requires_cookie_session_not_pat(
    path: str,
    body: dict[str, Any],
) -> None:
    """A bearer PAT cannot activate or mint browser collaboration credentials."""
    pat = Principal(
        user_id=USER_ID,
        kind="pat",
        tenant_id="default",
        role="member",
        pat_id="pat_test",
    )
    harness = _public_harness(principal=pat, collaboration=True)

    with harness.client:
        response = harness.client.post(path, json=body)

    assert response.status_code == 403
    assert response.json()["error"] == {
        "message": "Live collaboration requires an active browser session.",
        "type": "forbidden",
        "reason": "cookie_session_required",
    }
    assert harness.store.issue_calls == []


@pytest.mark.asyncio
async def test_ready_instance_requires_a_stable_fence_around_node_readiness() -> None:
    """The public probe identity is read from the DB before and after Node."""
    harness = _public_harness(collaboration=True)
    lease = CollaborationInstanceLease(
        instance_id="node-ready",
        epoch=7,
        lease_expires_at=10_000.0,
        updated_at=9_000.0,
    )
    harness.store.current_instance_results = [lease, lease]

    result = await harness.service.ready_instance()

    assert result == lease
    assert [
        call["tenant_id"] for call in harness.store.current_instance_calls
    ] == ["default", "default"]


@pytest.mark.asyncio
async def test_ready_instance_rejects_a_takeover_during_the_probe() -> None:
    """A fencing change during the health request yields not-ready, not a mix."""
    harness = _public_harness(collaboration=True)
    harness.store.current_instance_results = [
        CollaborationInstanceLease("node-old", 7, 10_000.0, 9_000.0),
        CollaborationInstanceLease("node-new", 8, 10_100.0, 9_100.0),
    ]

    assert await harness.service.ready_instance() is None


@pytest.mark.asyncio
async def test_node_facing_maintenance_rejects_an_unconfigured_tenant() -> None:
    """A compromised sidecar cannot select another tenant for retention."""
    harness = _public_harness(collaboration=True)

    with pytest.raises(CollaborationProtocolConflict, match="tenant_conflict"):
        await harness.service.run_maintenance(
            tenant_id="foreign-tenant",
            document_id=None,
            generation=None,
            instance_id="node-a",
            epoch=1,
        )

    assert harness.store.purge_calls == []


def test_public_session_issues_then_rotates_one_opaque_lease() -> None:
    """Refresh replaces the old lease instead of consuming a session slot."""
    harness = _public_harness(collaboration=True)

    with harness.client:
        first = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(),
        )
        assert first.status_code == 200
        first_payload = first.json()
        second = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(lease_token=first_payload["lease_token"]),
        )

    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["lease_token"] != first_payload["lease_token"]
    assert second_payload == {
        "websocket_path": "/collaboration",
        "room": f"inqtrix-editor-v1:{DOCUMENT_ID}:g4",
        "lease_token": second_payload["lease_token"],
        "expires_at": second_payload["expires_at"],
        "refresh_after": second_payload["refresh_after"],
        "provider_flush_ms": 75,
        **_PUBLISHED_TRANSPORT_LIMITS,
        "access": "edit",
        "initial_write_mode": "edit",
        "user": {
            "id": str(USER_ID),
            "name": "Ada Editor",
            "color": EditorCollaborationService.user_color(USER_ID),
        },
        "protocol_version": 1,
        "schema_version": 1,
    }
    assert second_payload["refresh_after"] == pytest.approx(
        second_payload["expires_at"] - 43
    )
    assert len(harness.store.issue_calls) == 1
    assert len(harness.store.rotate_calls) == 1
    rotation = harness.store.rotate_calls[0]
    assert rotation["previous_lease_id"] == harness.store.issue_calls[0].lease_id
    assert len(harness.store.leases) == 1
    assert harness.store.issue_calls[0].lease_id not in harness.store.leases


@pytest.mark.asyncio
async def test_guest_session_uses_identity_without_account_session_fk() -> None:
    """Guest leases leave the account-only auth session reference empty."""
    harness = _public_harness(
        collaboration=True,
        guest_links=object(),
    )
    now = time.time()
    guest_identity_id = uuid.uuid4()
    guest_link_id = uuid.uuid4()
    access = SimpleNamespace(
        link=SimpleNamespace(
            id=guest_link_id,
            tenant_id="default",
            document_id=DOCUMENT_ID,
            generation=4,
            permission="edit",
            label="External review",
            expires_at=now + 600,
        ),
        identity=SimpleNamespace(
            id=guest_identity_id,
            display_name="Guest Reviewer",
            expires_at=now + 600,
        ),
    )

    result = await harness.service.create_guest_session(
        access=access,
        protocol_version=1,
        schema_version=1,
    )

    assert result["access"] == "edit"
    assert result["user"]["id"] == str(guest_identity_id)
    assert len(harness.store.issue_calls) == 1
    lease = harness.store.issue_calls[0]
    assert lease.actor_kind == "guest"
    assert lease.user_id is None
    assert lease.guest_identity_id == guest_identity_id
    assert lease.guest_link_id == guest_link_id
    assert lease.session_id is None


@pytest.mark.asyncio
async def test_guest_session_publishes_the_enforced_transport_limits() -> None:
    """A guest session returns the transport limits the service enforces."""
    harness = _public_harness(collaboration=True, guest_links=object())
    now = time.time()
    access = SimpleNamespace(
        link=SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id="default",
            document_id=DOCUMENT_ID,
            generation=4,
            permission="suggest",
            label="External review",
            expires_at=now + 600,
        ),
        identity=SimpleNamespace(
            id=uuid.uuid4(),
            display_name="Guest Reviewer",
            expires_at=now + 600,
        ),
    )

    result = await harness.service.create_guest_session(
        access=access,
        protocol_version=1,
        schema_version=1,
    )

    assert {
        name: result[name] for name in _PUBLISHED_TRANSPORT_LIMITS
    } == _PUBLISHED_TRANSPORT_LIMITS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("link_permission", "lease_permission"),
    [
        ("view", "view"),
        ("comment", "view"),
        ("suggest", "suggest"),
        ("edit", "edit"),
    ],
)
async def test_guest_comment_link_holds_a_view_lease_at_the_collaboration_service(
    link_permission: str,
    lease_permission: str,
) -> None:
    """The collaboration service knows view, suggest and edit leases only.

    A comment-only guest writes comments through the API, never document
    updates, so the lease it presents to the collaboration service is a
    read-only view lease; the session still tells the browser its comment
    access.
    """
    harness = _public_harness(collaboration=True, guest_links=object())
    now = time.time()
    access = SimpleNamespace(
        link=SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id="default",
            document_id=DOCUMENT_ID,
            generation=4,
            permission=link_permission,
            label="External review",
            expires_at=now + 600,
        ),
        identity=SimpleNamespace(
            id=uuid.uuid4(),
            display_name="Guest Reviewer",
            expires_at=now + 600,
        ),
    )

    result = await harness.service.create_guest_session(
        access=access,
        protocol_version=1,
        schema_version=1,
    )

    assert result["access"] == link_permission
    assert [lease.permission for lease in harness.store.issue_calls] == [
        lease_permission
    ]


@pytest.mark.asyncio
async def test_guest_lease_is_rejected_after_link_permission_changes() -> None:
    """An open guest socket cannot retain authority after a live role change."""
    guest_links = SimpleNamespace(guest_identity_by_id=AsyncMock())
    harness = _public_harness(
        collaboration=True,
        guest_links=guest_links,
    )
    now = time.time()
    guest_identity_id = uuid.uuid4()
    guest_link_id = uuid.uuid4()
    identity = SimpleNamespace(
        id=guest_identity_id,
        display_name="Guest Reviewer",
        expires_at=now + 600,
    )
    original_link = SimpleNamespace(
        id=guest_link_id,
        tenant_id="default",
        document_id=DOCUMENT_ID,
        generation=4,
        permission="edit",
        label="External review",
        expires_at=now + 600,
    )
    result = await harness.service.create_guest_session(
        access=SimpleNamespace(link=original_link, identity=identity),
        protocol_version=1,
        schema_version=1,
    )
    lease = harness.store.issue_calls[0]
    harness.store.introspect_lease = AsyncMock(return_value=lease)  # type: ignore[method-assign]
    harness.store.load_state = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            document=SimpleNamespace(schema_hash=_sha256(b"schema"), schema_version=1)
        )
    )
    guest_links.guest_identity_by_id.return_value = (
        identity,
        SimpleNamespace(**{**vars(original_link), "permission": "view"}),
    )

    with pytest.raises(
        CollaborationAuthenticationRequired,
        match="guest permission changed",
    ):
        await harness.service.introspect_lease(
            token=result["lease_token"],
            room=result["room"],
            instance_id="node-a",
            epoch=1,
        )


@pytest.mark.asyncio
async def test_lease_introspection_captures_policy_cursor_before_checks() -> None:
    """A successful lease records which committed policy events its checks cover."""
    order: list[str] = []
    guest_links = SimpleNamespace()
    harness = _public_harness(
        collaboration=True,
        guest_links=guest_links,
    )
    now = time.time()
    guest_identity_id = uuid.uuid4()
    guest_link_id = uuid.uuid4()
    identity = SimpleNamespace(
        id=guest_identity_id,
        display_name="Guest Reviewer",
        expires_at=now + 600,
    )
    link = SimpleNamespace(
        id=guest_link_id,
        tenant_id="default",
        document_id=DOCUMENT_ID,
        generation=4,
        permission="edit",
        label="External review",
        expires_at=now + 600,
    )
    result = await harness.service.create_guest_session(
        access=SimpleNamespace(link=link, identity=identity),
        protocol_version=1,
        schema_version=1,
    )
    lease = harness.store.issue_calls[0]

    async def current_policy_cursor(**kwargs: Any) -> int:
        del kwargs
        order.append("cursor")
        return 41

    async def validate_instance(**kwargs: Any) -> None:
        del kwargs
        order.append("instance")

    async def introspect_lease(**kwargs: Any) -> CollaborationLease:
        del kwargs
        order.append("lease")
        return lease

    async def load_state(**kwargs: Any) -> Any:
        del kwargs
        order.append("state")
        return SimpleNamespace(
            document=SimpleNamespace(
                schema_hash=_sha256(b"schema"),
                schema_version=1,
            )
        )

    async def guest_identity_by_id(**kwargs: Any) -> Any:
        del kwargs
        order.append("guest")
        return identity, link

    harness.store.current_policy_cursor = current_policy_cursor  # type: ignore[attr-defined]
    harness.store.validate_instance = validate_instance  # type: ignore[method-assign]
    harness.store.introspect_lease = introspect_lease  # type: ignore[method-assign]
    harness.store.load_state = load_state  # type: ignore[method-assign]
    guest_links.guest_identity_by_id = guest_identity_by_id

    payload = await harness.service.introspect_lease(
        token=result["lease_token"],
        room=result["room"],
        instance_id="node-a",
        epoch=1,
    )

    assert payload["policy_cursor"] == 41
    assert order == ["cursor", "instance", "lease", "state", "guest"]


def test_public_lease_rotation_rejects_malformed_token_as_401() -> None:
    """A malformed lease token yields the STRUCTURED lease error, never a 500.

    Pins the service's own token-validation raise sites: a missing
    ``CollaborationLeaseInvalid`` import turns every invalid or expired lease
    into a ``NameError`` 500, preventing clients from recovering a lease and
    leaving them read-only.
    Fake stores raising the exception themselves cannot catch that class of
    regression — this drives the service's OWN ``_decode_token`` path.
    """
    harness = _public_harness(collaboration=True)

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(lease_token="cl1.not-a-real-payload.bad-sig"),
        )

    assert response.status_code == 401
    payload = response.json()["error"]
    assert payload["type"] == "authentication_error"
    assert payload["reason"] == "lease_invalid"


def test_public_lease_rotation_reconstructs_a_lost_response_idempotently() -> None:
    """Retrying one rotation command returns the same durable replacement."""
    harness = _public_harness(collaboration=True)
    rotation_command_id = uuid.UUID("99999999-9999-4999-8999-999999999999")

    with harness.client:
        first = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(),
        )
        assert first.status_code == 200
        request = _session_body(
            lease_token=first.json()["lease_token"],
            rotation_command_id=rotation_command_id,
        )
        rotated = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=request,
        )
        replayed = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=request,
        )

    assert rotated.status_code == replayed.status_code == 200
    assert replayed.json()["lease_token"] == rotated.json()["lease_token"]
    assert replayed.json()["expires_at"] == rotated.json()["expires_at"]
    assert len(harness.store.rotate_calls) == 1
    assert len(harness.store.leases) == 1


@pytest.mark.parametrize("reason", ["lease_expired", "lease_revoked"])
def test_public_lease_rotation_preserves_terminal_lease_reason(reason: str) -> None:
    """Expired and revoked leases remain distinct public authentication errors."""
    harness = _public_harness(collaboration=True)

    with harness.client:
        first = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(),
        )
        assert first.status_code == 200
        harness.store.rotate_error = CollaborationLeaseInvalid(reason)
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(
                lease_token=first.json()["lease_token"],
                rotation_command_id=uuid.uuid4(),
            ),
        )

    assert response.status_code == 401
    assert response.json()["error"]["reason"] == reason


@pytest.mark.parametrize("reason", ["session_limit", "session_rate_limited"])
def test_public_session_preserves_durable_rate_limit_reason(reason: str) -> None:
    """Both durable lease limits map to the public 429 envelope."""
    harness = _public_harness(collaboration=True)
    harness.store.issue_error = CollaborationRateLimited(reason)

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(),
        )

    assert response.status_code == 429
    assert response.json()["error"] == {
        "message": "Too many collaboration sessions or session requests.",
        "type": "rate_limit_error",
        "reason": reason,
    }


def test_public_lease_rotation_preserves_rate_limit_error() -> None:
    """A refresh-window limit remains a public 429 instead of a generic conflict."""
    harness = _public_harness(collaboration=True)

    with harness.client:
        first = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(),
        )
        assert first.status_code == 200
        harness.store.rotate_error = CollaborationRateLimited(
            "session_rate_limited"
        )
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/session",
            json=_session_body(lease_token=first.json()["lease_token"]),
        )

    assert response.status_code == 429
    assert response.json()["error"] == {
        "message": "Too many collaboration sessions or session requests.",
        "type": "rate_limit_error",
        "reason": "session_rate_limited",
    }


def test_concrete_maintenance_service_propagates_explicit_tenant() -> None:
    """Fencing, compaction, and purge share the caller's canonical tenant."""
    harness = _public_harness(
        collaboration=True,
        tenant_id=INTERNAL_TENANT_ID,
    )

    result = asyncio.run(
        harness.service.run_maintenance(
            tenant_id=INTERNAL_TENANT_ID,
            document_id=DOCUMENT_ID,
            generation=4,
            instance_id="node-a",
            epoch=7,
        )
    )

    assert result == {
        "payloads_pruned": 2,
        "metadata_pruned": 3,
        "tombstones_purged": 4,
    }
    assert harness.store.compact_calls[0]["tenant_id"] == INTERNAL_TENANT_ID
    assert harness.store.purge_calls[0]["tenant_id"] == INTERNAL_TENANT_ID
    assert harness.store.compact_calls[0]["instance_id"] == "node-a"
    assert harness.store.compact_calls[0]["instance_epoch"] == 7
    assert harness.store.purge_calls[0]["instance_id"] == "node-a"
    assert harness.store.purge_calls[0]["instance_epoch"] == 7


def _publish_suggestion_body() -> dict[str, Any]:
    return {
        "patch_id": str(
            uuid.UUID("66666666-6666-4666-8666-666666666666")
        ),
        "command_id": str(
            uuid.UUID("55555555-5555-4555-8555-555555555555")
        ),
        "actor_kind": "assistant",
        "expected_sequence": 11,
        "target_markdown": "# Shared suggestion\n\nRevised body.",
    }


def test_public_publish_suggestion_accepts_private_assistant_patch() -> None:
    """A cookie user may publish one UUID-addressed private AI patch."""
    harness = _public_harness(collaboration=True)
    body = _publish_suggestion_body()

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/suggestions:publish",
            json=body,
        )

    assert response.status_code == 200
    assert response.json() == {
        "command_id": body["command_id"],
        "patch_id": body["patch_id"],
        "sequence": 12,
        "suggestion_ids": [
            str(uuid.UUID("77777777-7777-4777-8777-777777777777"))
        ],
    }
    assert harness.node.publish_calls == [
        {
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "expected_sequence": 11,
            "command_id": uuid.UUID(body["command_id"]),
            "patch_id": body["patch_id"],
            "actor_kind": "assistant",
            "actor_user_id": USER_ID,
            "target_markdown": body["target_markdown"],
            "base_markdown": None,
        }
    ]


def test_public_publish_suggestion_passes_the_base_it_was_built_on() -> None:
    """The projection the target was built on reaches Node unchanged."""
    harness = _public_harness(collaboration=True)
    body = {**_publish_suggestion_body(), "base_markdown": "# Shared suggestion\n\nBody."}

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/suggestions:publish",
            json=body,
        )

    assert response.status_code == 200
    assert [call["base_markdown"] for call in harness.node.publish_calls] == [
        "# Shared suggestion\n\nBody."
    ]
    assert [call["target_markdown"] for call in harness.node.publish_calls] == [
        body["target_markdown"]
    ]


@pytest.mark.parametrize(
    ("base_markdown", "sent"),
    [
        pytest.param(None, False, id="without-base"),
        pytest.param("# Base\n\nBody.", True, id="with-base"),
    ],
)
@pytest.mark.asyncio
async def test_node_client_sends_the_base_only_when_there_is_one(
    base_markdown: str | None,
    sent: bool,
) -> None:
    """Without a base the request is the one Node always received."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "command_id": "55555555-5555-4555-8555-555555555555",
                "patch_id": "66666666-6666-4666-8666-666666666666",
                "sequence": 12,
                "suggestion_ids": ["77777777-7777-4777-8777-777777777777"],
            },
        )

    client = CollaborationNodeClient(
        base_url="http://collaboration.internal",
        secret="test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.publish_suggestion(
            document_id=DOCUMENT_ID,
            generation=4,
            expected_sequence=11,
            command_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
            patch_id="66666666-6666-4666-8666-666666666666",
            actor_kind="assistant",
            actor_user_id=USER_ID,
            target_markdown="# Target",
            base_markdown=base_markdown,
        )
    finally:
        await client.aclose()

    assert len(bodies) == 1
    assert ("base_markdown" in bodies[0]) is sent
    assert bodies[0].get("base_markdown") == base_markdown
    assert bodies[0]["target_markdown"] == "# Target"


def test_public_projection_reprojects_until_markdown_is_globally_current() -> None:
    """A peer update racing publication forces a projection at the new watermark."""
    harness = _public_harness(collaboration=True)
    harness.store.projection_authoritative_sequences = [15]
    harness.node.projection_results = [
        CollaborationProjection(
            generation=4,
            sequence=12,
            markdown="# First projection",
            projection_hash=_sha256(b"# First projection"),
            schema_hash=_sha256(b"schema"),
        ),
        CollaborationProjection(
            generation=4,
            sequence=15,
            markdown="# Current projection",
            projection_hash=_sha256(b"# Current projection"),
            schema_hash=_sha256(b"schema"),
        ),
    ]

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/projection:flush"
        )

    assert response.status_code == 200
    assert response.json()["sequence"] == 15
    assert response.json()["authoritative_sequence"] == 15
    assert response.json()["content_markdown"] == "# Current projection"
    assert harness.node.project_calls == [
        {
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "minimum_sequence": 11,
        },
        {
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "minimum_sequence": 15,
        },
    ]


def test_public_projection_fails_visibly_when_updates_never_quiesce() -> None:
    """A continuously advancing document cannot yield falsely current Markdown."""
    harness = _public_harness(collaboration=True)
    harness.store.projection_authoritative_sequences = [13, 15, 17]
    harness.node.projection_results = [
        CollaborationProjection(
            generation=4,
            sequence=sequence,
            markdown=f"# Projection {sequence}",
            projection_hash=_sha256(f"# Projection {sequence}".encode()),
            schema_hash=_sha256(b"schema"),
        )
        for sequence in (12, 14, 16)
    ]

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/collaboration/projection:flush"
        )

    assert response.status_code == 409
    assert response.json()["error"]["reason"] == "projection_not_current"
    assert response.json()["error"]["current_sequence"] == 17
    assert [call["minimum_sequence"] for call in harness.node.project_calls] == [
        11,
        13,
        15,
    ]


def test_public_patch_decision_accepts_an_explicit_uuid_batch() -> None:
    """The documented explicit batch reaches Node with one sequence precondition."""
    harness = _public_harness(collaboration=True)
    patch_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    decision_id = uuid.uuid4()

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/patches:decide",
            json={
                "patch_ids": patch_ids,
                "decision": "accept",
                "expected_sequence": 11,
                "decision_id": str(decision_id),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "decision_id": str(decision_id),
        "sequence": 12,
        "suggestion_ids": ["suggestion-decision"],
    }
    assert harness.node.decide_calls[0]["patch_ids"] == tuple(patch_ids)
    assert harness.node.decide_calls[0]["expected_sequence"] == 11


@pytest.mark.parametrize(
    "selection",
    [
        pytest.param({}, id="no-patch-ids"),
        pytest.param(
            {"all_open": True, "confirm_all_open": True},
            id="every-open-patch",
        ),
        pytest.param({"patch_ids": []}, id="empty-list"),
        pytest.param({"patch_ids": [""]}, id="empty-id"),
    ],
)
def test_public_patch_decision_requires_the_patches_it_decides(
    selection: dict[str, Any],
) -> None:
    """A decision names every patch it decides. A body that names none, such
    as a request to decide every open patch, is rejected before Node."""
    harness = _public_harness(collaboration=True)

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/patches:decide",
            json={
                **selection,
                "decision": "accept",
                "expected_sequence": 11,
                "decision_id": str(uuid.uuid4()),
            },
        )

    assert response.status_code == 400
    # The route's own message, so a rejection that only a later layer
    # produces does not pass here; a reword must be updated here.
    assert response.json()["error"] == {
        "message": "Invalid decision command",
        "type": "invalid_request_error",
    }
    assert harness.node.decide_calls == []


@pytest.mark.parametrize(
    ("field", "value", "expected_status"),
    [
        ("patch_id", "not-a-uuid", 400),
        ("command_id", "not-a-uuid", 400),
        ("actor_kind", "agent", 400),
        ("expected_sequence", -1, 400),
        ("expected_sequence", True, 400),
        ("target_markdown", "x" * (1_048_576 + 1), 413),
        ("base_markdown", 7, 400),
        ("base_markdown", "x" * (1_048_576 + 1), 413),
    ],
)
def test_public_publish_suggestion_rejects_invalid_browser_contract(
    field: str,
    value: Any,
    expected_status: int,
) -> None:
    """Only bounded assistant publications with canonical UUIDs reach Node."""
    harness = _public_harness(collaboration=True)
    body = _publish_suggestion_body()
    body[field] = value

    with harness.client:
        response = harness.client.post(
            f"/v1/editor/documents/{DOCUMENT_ID}/suggestions:publish",
            json=body,
        )

    assert response.status_code == expected_status
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert harness.node.publish_calls == []


def test_public_activity_history_applies_author_and_type_filters() -> None:
    """History filters are forwarded before adjacent-edit grouping."""
    harness = _public_harness(collaboration=True)
    harness.store.activity_rows = (
        CollaborationActivity(
            sequence=9,
            actor_user_id=ACTOR_ID,
            actor_kind="human",
            change_kind="direct",
            suggestion_ids=(),
            command_id=None,
            created_at=12.0,
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={
                "view": "history",
                "author_id": str(ACTOR_ID),
                "type": "direct",
                "limit": 20,
            },
        )

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "from_sequence": 9,
            "to_sequence": 9,
            "type": "direct",
            "actor_kind": "human",
            "actor": {"id": str(ACTOR_ID), "name": "Other Editor"},
            "suggestion_ids": [],
                "command_id": None,
                "created_at": 12.0,
                "summary": {"edits": [], "omitted_edit_count": 0},
                "update_count": 1,
                "outcome": None,
                "rejected_suggestions": [],
                "rejected_suggestion_count": 0,
        }
    ]
    assert harness.store.activity_calls == [
        {
            "tenant_id": "default",
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "before_sequence": None,
            "author_user_id": ACTOR_ID,
            "change_kind": "direct",
            "limit": 21,
        }
    ]


def test_public_activity_marks_the_work_of_an_erased_account() -> None:
    """An erased account's edits stay in a colleague's document under the
    deleted-account placeholder; its actor says so, so the interface can
    name it in the reader's language instead of the stored English name."""
    harness = _public_harness(collaboration=True)
    harness.store.activity_rows = (
        CollaborationActivity(
            sequence=9,
            actor_user_id=ERASED_EDITOR_ID,
            actor_kind="human",
            change_kind="direct",
            suggestion_ids=(),
            command_id=None,
            created_at=12.0,
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "history"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["actor"] == {
        "id": str(ERASED_EDITOR_ID),
        "name": DELETED_ACCOUNT_DISPLAY_NAME,
        "kind": "deleted",
    }


def test_public_activity_checks_access_on_document_facts_without_the_body() -> None:
    """The activity page reads only identity, ownership and mode of the
    document; its Markdown body is never loaded for it."""
    harness = _public_harness(collaboration=True)

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "history"},
        )

    assert response.status_code == 200
    assert harness.documents.get_calls == []
    assert [
        (call["document_id"], call["minimum"])
        for call in harness.documents.facts_calls
    ] == [(DOCUMENT_ID, SharePermission.VIEW)]
    assert harness.documents.facts_calls[0]["visible_to"] is not None
    assert harness.store.activity_calls[0]["generation"] == 4


def test_public_activity_answers_without_the_generic_response_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page is JSON-native, so it is answered directly instead of being
    walked by FastAPI's generic encoder on every poll."""
    import fastapi.routing

    encoded: list[object] = []
    original = fastapi.routing.jsonable_encoder

    def counting_encoder(*args: Any, **kwargs: Any) -> Any:
        encoded.append(args[0] if args else None)
        return original(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "jsonable_encoder", counting_encoder)
    harness = _public_harness(collaboration=True)
    harness.store.activity_rows = (
        CollaborationActivity(
            sequence=9,
            actor_user_id=ACTOR_ID,
            actor_kind="human",
            change_kind="direct",
            suggestion_ids=("suggestion-1",),
            command_id=None,
            created_at=12.5,
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "history"},
        )

    assert response.status_code == 200
    assert encoded == []
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "from_sequence": 9,
                "to_sequence": 9,
                "type": "direct",
                "actor_kind": "human",
                "actor": {"id": str(ACTOR_ID), "name": "Other Editor"},
                "suggestion_ids": ["suggestion-1"],
                "command_id": None,
                "created_at": 12.5,
                "summary": {"edits": [], "omitted_edit_count": 0},
                "update_count": 1,
                "outcome": None,
                "rejected_suggestions": [],
                "rejected_suggestion_count": 0,
            }
        ],
        "next_cursor": None,
    }


def test_public_activity_exposes_shared_comments_as_own_filter() -> None:
    """Comment audit events are readable without mixing IDs into summaries."""
    harness = _public_harness(collaboration=True)
    harness.store.comment_activity_rows = (
        CollaborationCommentActivity(
            id=17,
            actor_user_id=ACTOR_ID,
            action="editor.collaboration_comment.replied",
            created_at=14.0,
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "history", "type": "comment"},
        )

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "from_sequence": 17,
            "to_sequence": 17,
            "type": "comment",
            "actor_kind": "human",
            "actor": {"id": str(ACTOR_ID), "name": "Other Editor"},
            "comment_action": "replied",
            "suggestion_ids": [],
            "command_id": None,
            "created_at": 14.0,
            "summary": {"edits": [], "omitted_edit_count": 0},
            "update_count": 1,
            "outcome": None,
            "rejected_suggestions": [],
            "rejected_suggestion_count": 0,
        }
    ]


def test_public_activity_history_cursor_uses_raw_boundary_before_grouping() -> None:
    """A full raw page keeps its continuation when direct rows collapse."""
    harness = _public_harness(collaboration=True)
    harness.store.activity_rows = tuple(
        CollaborationActivity(
            sequence=sequence,
            actor_user_id=ACTOR_ID,
            actor_kind="human",
            change_kind="direct",
            suggestion_ids=(),
            command_id=None,
            created_at=float(sequence),
        )
        for sequence in range(100, 49, -1)
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "history", "limit": 50},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["next_cursor"] == "51"
    assert len(payload["data"]) == 1
    assert payload["data"][0]["from_sequence"] == 51
    assert payload["data"][0]["to_sequence"] == 100
    assert harness.store.activity_calls[0]["limit"] == 51


GUEST_EDITOR_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
GUEST_AUTHOR_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")


class _GuestProfiles:
    """Guest identities by the names they gave or their link labels."""

    async def guest_actor_profiles(
        self, *, tenant_id: str, guest_identity_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, Any]:
        del tenant_id
        names = {GUEST_EDITOR_ID: "Gina Gast", GUEST_AUTHOR_ID: None}
        return {
            guest_id: SimpleNamespace(
                id=guest_id,
                display_name=names.get(guest_id),
                link_label="Review link",
            )
            for guest_id in guest_identity_ids
        }


def _rejection(
    *,
    sequence: int,
    suggestion_id: str,
    author_user_id: uuid.UUID | None = None,
    author_guest_identity_id: uuid.UUID | None = None,
) -> CollaborationSuggestionRejection:
    return CollaborationSuggestionRejection(
        sequence=sequence,
        suggestion_id=suggestion_id,
        patch_id=f"patch-{suggestion_id}",
        author_user_id=author_user_id,
        author_guest_identity_id=author_guest_identity_id,
    )


def test_public_activity_history_names_the_suggestions_an_edit_removed() -> None:
    """A guest's edit shows the guest by name and the authors whose
    suggestions it removed.

    The authors are resolved from the audit entries of that update row,
    member and guest alike, so each author can find their own suggestion.
    """
    harness = _public_harness(collaboration=True, guest_links=_GuestProfiles())
    harness.store.activity_rows = (
        CollaborationActivity(
            sequence=12,
            actor_user_id=None,
            actor_guest_identity_id=GUEST_EDITOR_ID,
            actor_kind="guest",
            change_kind="direct",
            suggestion_ids=(),
            command_id=None,
            created_at=30.0,
            rejected_suggestion_ids=("suggestion-a", "suggestion-b"),
        ),
    )
    harness.store.rejection_rows = (
        _rejection(
            sequence=12,
            suggestion_id="suggestion-b",
            author_guest_identity_id=GUEST_AUTHOR_ID,
        ),
        _rejection(
            sequence=12,
            suggestion_id="suggestion-a",
            author_user_id=USER_ID,
        ),
        _rejection(sequence=99, suggestion_id="elsewhere", author_user_id=ACTOR_ID),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "history"},
        )

    assert response.status_code == 200
    [item] = response.json()["data"]
    assert item["actor"] == {
        "id": str(GUEST_EDITOR_ID),
        "name": "Gina Gast",
        "kind": "guest",
        "link_label": "Review link",
    }
    assert item["rejected_suggestion_count"] == 2
    assert item["rejected_suggestions"] == [
        {
            "suggestion_id": "suggestion-a",
            "patch_id": "patch-suggestion-a",
            "author": {"id": str(USER_ID), "name": "Ada Editor"},
        },
        {
            "suggestion_id": "suggestion-b",
            "patch_id": "patch-suggestion-b",
            "author": {
                "id": str(GUEST_AUTHOR_ID),
                "name": "Guest Review link",
                "kind": "guest",
                "link_label": "Review link",
            },
        },
    ]
    assert harness.store.rejection_calls == [
        {
            "tenant_id": "default",
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "sequences": (12,),
            "limit": 1000,
        }
    ]


def test_public_activity_history_keeps_an_edit_that_removed_suggestions_apart() -> None:
    """Neighbouring edits of the same writer do not swallow a removal."""
    harness = _public_harness(collaboration=True)
    harness.store.activity_rows = (
        CollaborationActivity(
            sequence=14,
            actor_user_id=ACTOR_ID,
            actor_kind="human",
            change_kind="direct",
            suggestion_ids=(),
            command_id=None,
            created_at=40.0,
        ),
        CollaborationActivity(
            sequence=13,
            actor_user_id=ACTOR_ID,
            actor_kind="human",
            change_kind="direct",
            suggestion_ids=(),
            command_id=None,
            created_at=39.0,
            rejected_suggestion_ids=("suggestion-a",),
        ),
        CollaborationActivity(
            sequence=12,
            actor_user_id=ACTOR_ID,
            actor_kind="human",
            change_kind="direct",
            suggestion_ids=(),
            command_id=None,
            created_at=38.0,
        ),
    )
    harness.store.rejection_rows = (
        _rejection(
            sequence=13,
            suggestion_id="suggestion-a",
            author_user_id=USER_ID,
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "history"},
        )

    assert response.status_code == 200
    items = response.json()["data"]
    assert [(item["from_sequence"], item["to_sequence"]) for item in items] == [
        (14, 14),
        (13, 13),
        (12, 12),
    ]
    assert [item["rejected_suggestion_count"] for item in items] == [0, 1, 0]


def test_public_open_activity_names_a_guest_author_by_the_guests_name() -> None:
    """A pending suggestion a guest proposed shows its guest author."""
    harness = _public_harness(collaboration=True, guest_links=_GuestProfiles())
    harness.store.open_patch_rows = (
        CollaborationOpenPatch(
            patch_id="patch-guest",
            author_user_id=None,
            author_guest_identity_id=GUEST_EDITOR_ID,
            created_at=21.0,
            suggestion_ids=("suggestion-guest",),
            kinds=("insertion",),
            exact_edits=None,
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "open"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["author"] == {
        "id": str(GUEST_EDITOR_ID),
        "name": "Gina Gast",
        "kind": "guest",
        "link_label": "Review link",
    }


def test_public_open_activity_returns_exact_patch_preview_when_available() -> None:
    """Anchored AI edits remain inspectable while human descriptors stay bounded."""
    harness = _public_harness(collaboration=True)
    patch_id = "patch-open-exact"
    exact_edit = {
        "id": "edit-1",
        "find": "old text",
        "text": "new text",
        "position": "replace",
        "quote_before": "before",
        "quote_after": "after",
    }
    harness.store.open_patch_rows = (
        CollaborationOpenPatch(
            patch_id=patch_id,
            author_user_id=ACTOR_ID,
            created_at=13.0,
            suggestion_ids=("suggestion-1",),
            kinds=("replacement",),
            exact_edits=(exact_edit,),
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={
                "view": "open",
                "author_id": str(ACTOR_ID),
                "type": "replacement",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "patch_id": patch_id,
                "author": {"id": str(ACTOR_ID), "name": "Other Editor"},
                "created_at": 13.0,
                "suggestion_ids": ["suggestion-1"],
            "type": "replacement",
            "types": ["replacement"],
                "preview": {"edits": [exact_edit]},
            }
        ],
        "next_cursor": None,
    }
    assert harness.store.open_patch_calls == [
        {
            "tenant_id": "default",
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "before": None,
            "author_user_id": ACTOR_ID,
            "suggestion_kind": "replacement",
            "limit": 50,
        }
    ]


def test_public_open_activity_marks_human_exact_preview_unavailable() -> None:
    """Human Yjs patches expose metadata without promising stored exact text."""
    harness = _public_harness(collaboration=True)
    harness.store.open_patch_rows = (
        CollaborationOpenPatch(
            patch_id="patch-open-human",
            author_user_id=ACTOR_ID,
            created_at=14.0,
            suggestion_ids=("suggestion-human",),
            kinds=("insertion",),
            exact_edits=None,
        ),
    )

    with harness.client:
        response = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "open"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["preview"] is None


def test_public_open_activity_keyset_walks_beyond_two_hundred_rows() -> None:
    """The opaque cursor exposes every open patch without a hidden truncation."""
    harness = _public_harness(collaboration=True)
    patches = tuple(
        CollaborationOpenPatch(
            patch_id=str(uuid.UUID(int=index + 1)),
            author_user_id=ACTOR_ID,
            created_at=1_000.0 - index,
            suggestion_ids=(f"suggestion-{index}",),
            kinds=("insertion",),
            exact_edits=None,
        )
        for index in range(205)
    )
    boundary = (patches[199].created_at, patches[199].patch_id)
    harness.store.open_patch_pages = {
        None: CollaborationOpenPatchPage(
            patches=patches[:200], next_cursor=boundary
        ),
        boundary: CollaborationOpenPatchPage(
            patches=patches[200:], next_cursor=None
        ),
    }

    with harness.client:
        first = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={"view": "open", "limit": 200},
        )
        assert first.status_code == 200
        second = harness.client.get(
            f"/v1/editor/documents/{DOCUMENT_ID}/activity",
            params={
                "view": "open",
                "limit": 200,
                "cursor": first.json()["next_cursor"],
            },
        )

    assert second.status_code == 200
    observed_ids = [
        row["patch_id"]
        for response in (first, second)
        for row in response.json()["data"]
    ]
    assert observed_ids == [patch.patch_id for patch in patches]
    assert first.json()["next_cursor"] is not None
    assert second.json()["next_cursor"] is None
    assert harness.store.open_patch_calls[1]["before"] == boundary


class _InternalService:
    """Internal API service fake exposing validated DTOs and injected errors."""

    def __init__(self) -> None:
        snapshot = CollaborationSnapshot(
            document_id=DOCUMENT_ID,
            tenant_id=INTERNAL_TENANT_ID,
            generation=4,
            covered_sequence=4,
            state_update=b"snapshot-state",
            state_vector=b"snapshot-vector",
            state_hash=_sha256(b"snapshot-state"),
            projection_hash=_sha256(b"projection"),
            schema_version=1,
            schema_hash=_sha256(b"schema"),
            created_at=10.0,
        )
        document = CollaborationDocumentState(
            document_id=DOCUMENT_ID,
            tenant_id=INTERNAL_TENANT_ID,
            generation=4,
            schema_version=1,
            schema_hash=_sha256(b"schema"),
            persisted_sequence=5,
            projection_sequence=4,
            content_markdown="projected",
            projection_updated_at=10.0,
            owner_user_id=USER_ID,
        )
        update = CollaborationUpdate(
            document_id=DOCUMENT_ID,
            tenant_id=INTERNAL_TENANT_ID,
            generation=4,
            sequence=5,
            update_hash=_sha256(b"tail-update"),
            update_bytes=b"tail-update",
            actor_user_id=ACTOR_ID,
            actor_kind="human",
            change_kind="direct",
            created_at=11.0,
        )
        fallback_snapshot = replace(
            snapshot,
            covered_sequence=3,
            state_update=b"fallback-snapshot-state",
            state_vector=b"fallback-snapshot-vector",
            state_hash=_sha256(b"fallback-snapshot-state"),
            created_at=9.0,
        )
        fallback_update = replace(
            update,
            sequence=4,
            update_hash=_sha256(b"fallback-tail-update"),
            update_bytes=b"fallback-tail-update",
            created_at=10.0,
        )
        self.loaded_state = CollaborationLoadedState(
            document=document,
            snapshot=snapshot,
            updates=(update,),
            fallback_candidates=(
                CollaborationSnapshotCandidate(
                    snapshot=fallback_snapshot,
                    updates=(fallback_update, update),
                ),
            ),
        )
        self.error: Exception | None = None
        self.load_calls: list[dict[str, Any]] = []
        self.persisted_updates: list[PersistCollaborationUpdate] = []
        self.command_lookups: list[dict[str, Any]] = []
        self.instance_acquires: list[dict[str, Any]] = []
        self.instance_renewals: list[dict[str, Any]] = []
        self.maintenance_calls: list[dict[str, Any]] = []
        self.policy_calls: list[dict[str, Any]] = []
        self.update_lookups: list[dict[str, Any]] = []
        self.command_result: CollaborationPersistedCommand | None = None
        self.persisted_result = PersistedCollaborationUpdate(
            sequence=12,
            persisted_sequence=12,
            duplicate=False,
            persisted_at=0.0,
        )
        self.stored_snapshots: list[
            tuple[CollaborationSnapshot, str, str, int, str]
        ] = []

    async def load_state(self, **kwargs: Any) -> CollaborationLoadedState:
        """Return the prepared state after recording the fenced coordinate."""
        self.load_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.loaded_state

    async def acquire_instance(self, **kwargs: Any) -> Any:
        """Record acquisition of the configured deployment tenant."""
        self.instance_acquires.append(dict(kwargs))
        return SimpleNamespace(
            instance_id=kwargs["instance_id"],
            epoch=3,
            lease_expires_at=20.0,
        )

    async def renew_instance(self, **kwargs: Any) -> Any:
        """Record renewal of the configured deployment tenant."""
        self.instance_renewals.append(dict(kwargs))
        return SimpleNamespace(
            instance_id=kwargs["instance_id"],
            epoch=kwargs["epoch"],
            lease_expires_at=25.0,
        )

    async def policy_events(self, **kwargs: Any) -> dict[str, Any]:
        """Record a tenant-scoped invalidation cursor read."""
        self.policy_calls.append(dict(kwargs))
        return {"events": [], "cursor": 0, "reset_required": False}

    async def run_maintenance(self, **kwargs: Any) -> dict[str, int]:
        """Record one tenant-scoped fenced retention pass."""
        self.maintenance_calls.append(dict(kwargs))
        return {
            "payloads_pruned": 0,
            "metadata_pruned": 0,
            "tombstones_purged": 0,
        }

    async def persist_update(
        self, *, update: PersistCollaborationUpdate
    ) -> PersistedCollaborationUpdate:
        """Record the parsed update contract and acknowledge it durably."""
        if self.error is not None:
            raise self.error
        self.persisted_updates.append(update)
        return replace(self.persisted_result, persisted_at=update.now)

    async def lookup_command(
        self, **kwargs: Any
    ) -> CollaborationPersistedCommand | None:
        """Record one idempotency lookup before a sidecar command replay."""
        if self.error is not None:
            raise self.error
        self.command_lookups.append(dict(kwargs))
        return self.command_result

    async def lookup_updates(
        self, **kwargs: Any
    ) -> tuple[CollaborationUpdateLookup, ...]:
        """Record one fenced hash reconciliation lookup."""
        if self.error is not None:
            raise self.error
        self.update_lookups.append(dict(kwargs))
        hashes = kwargs["update_hashes"]
        return (
            CollaborationUpdateLookup(update_hash=hashes[0], sequence=12),
        )

    async def store_snapshot(
        self,
        *,
        snapshot: CollaborationSnapshot,
        projection_markdown: str,
        instance_id: str,
        epoch: int,
        tenant_id: str,
    ) -> None:
        """Record the fully validated snapshot DTO."""
        if self.error is not None:
            raise self.error
        self.stored_snapshots.append(
            (snapshot, projection_markdown, instance_id, epoch, tenant_id)
        )

    async def introspect_lease(self, **kwargs: Any) -> dict[str, Any]:
        """Return a minimal successful lease or expose a mapped domain error."""
        if self.error is not None:
            raise self.error
        return {"valid": True, **kwargs}


def _internal_client(service: _InternalService) -> TestClient:
    """Bind the real internal router to an exact-secret test container."""
    container = SimpleNamespace(
        editor_collaboration_service=service,
        settings=SimpleNamespace(
            collaboration=SimpleNamespace(
                secret=INTERNAL_SECRET,
                max_document_bytes=10 * 1_048_576,
            )
        ),
    )
    app = FastAPI()
    app.include_router(internal_collaboration.build_router(container))
    return TestClient(app, raise_server_exceptions=False)


def _state_path() -> str:
    """Return a fully fenced internal state-load URL."""
    return (
        f"/internal/collaboration/documents/{DOCUMENT_ID}/state"
        "?generation=4&instance_id=node-a&epoch=7"
        f"&tenant_id={INTERNAL_TENANT_ID}"
    )


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong", f"bearer {INTERNAL_SECRET}", f"Basic {INTERNAL_SECRET}"],
)
def test_internal_api_requires_the_exact_bearer_header(
    authorization: str | None,
) -> None:
    """Missing, wrong, or differently schemed credentials fail before service I/O."""
    service = _InternalService()
    client = _internal_client(service)
    headers = {"Authorization": authorization} if authorization is not None else {}

    with client:
        response = client.get(_state_path(), headers=headers)

    assert response.status_code == 401
    assert response.json()["error"] == {
        "message": "Internal authentication failed.",
        "type": "authentication_error",
        "reason": "internal_auth_failed",
    }
    assert service.load_calls == []


def test_internal_instance_policy_and_maintenance_carry_configured_tenant() -> None:
    """Every trusted sidecar control flow requires the same explicit tenant."""
    service = _InternalService()
    client = _internal_client(service)

    with client:
        acquired = client.post(
            "/internal/collaboration/instances/acquire",
            headers=INTERNAL_HEADERS,
            json={
                "tenant_id": INTERNAL_TENANT_ID,
                "instance_id": "node-a",
                "lease_seconds": 15,
                "protocol_version": 1,
                "schema_version": 1,
            },
        )
        renewed = client.post(
            "/internal/collaboration/instances/renew",
            headers=INTERNAL_HEADERS,
            json={
                "tenant_id": INTERNAL_TENANT_ID,
                "instance_id": "node-a",
                "epoch": 3,
                "lease_seconds": 15,
            },
        )
        policy = client.get(
            "/internal/collaboration/policy-events",
            headers=INTERNAL_HEADERS,
            params={"tenant_id": INTERNAL_TENANT_ID, "after_id": 0},
        )
        maintenance = client.post(
            "/internal/collaboration/maintenance:compact",
            headers=INTERNAL_HEADERS,
            json={
                "tenant_id": INTERNAL_TENANT_ID,
                "instance_id": "node-a",
                "epoch": 3,
            },
        )

    assert acquired.status_code == renewed.status_code == 200
    assert policy.status_code == maintenance.status_code == 200
    assert service.instance_acquires[0]["tenant_id"] == INTERNAL_TENANT_ID
    assert service.instance_renewals[0]["tenant_id"] == INTERNAL_TENANT_ID
    assert service.policy_calls == [
        {"tenant_id": INTERNAL_TENANT_ID, "cursor": 0, "limit": 500}
    ]
    assert service.maintenance_calls == [
        {
            "tenant_id": INTERNAL_TENANT_ID,
            "document_id": None,
            "generation": None,
            "instance_id": "node-a",
            "epoch": 3,
        }
    ]


def test_internal_state_load_serializes_snapshot_and_complete_update_tail() -> None:
    """The sidecar receives one canonical snapshot and every later update."""
    service = _InternalService()
    client = _internal_client(service)

    with client:
        response = client.get(_state_path(), headers=INTERNAL_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "document_id": DOCUMENT_ID,
        "generation": 4,
        "persisted_sequence": 5,
        "schema_version": 1,
        "schema_hash": _sha256(b"schema"),
        "snapshot": {
            "covered_sequence": 4,
            "state_update_base64": base64.b64encode(b"snapshot-state").decode(),
            "state_vector_base64": base64.b64encode(b"snapshot-vector").decode(),
            "state_hash": _sha256(b"snapshot-state"),
        },
        "updates": [
            {
                "sequence": 5,
                "update_hash": _sha256(b"tail-update"),
                "update_base64": base64.b64encode(b"tail-update").decode(),
            }
        ],
        "snapshot_candidates": [
            {
                "covered_sequence": 4,
                "state_update_base64": base64.b64encode(
                    b"snapshot-state"
                ).decode(),
                "state_vector_base64": base64.b64encode(
                    b"snapshot-vector"
                ).decode(),
                "state_hash": _sha256(b"snapshot-state"),
                "updates": [
                    {
                        "sequence": 5,
                        "update_hash": _sha256(b"tail-update"),
                        "update_base64": base64.b64encode(
                            b"tail-update"
                        ).decode(),
                    }
                ],
            },
            {
                "covered_sequence": 3,
                "state_update_base64": base64.b64encode(
                    b"fallback-snapshot-state"
                ).decode(),
                "state_vector_base64": base64.b64encode(
                    b"fallback-snapshot-vector"
                ).decode(),
                "state_hash": _sha256(b"fallback-snapshot-state"),
                "updates": [
                    {
                        "sequence": 4,
                        "update_hash": _sha256(b"fallback-tail-update"),
                        "update_base64": base64.b64encode(
                            b"fallback-tail-update"
                        ).decode(),
                    },
                    {
                        "sequence": 5,
                        "update_hash": _sha256(b"tail-update"),
                        "update_base64": base64.b64encode(
                            b"tail-update"
                        ).decode(),
                    },
                ],
            },
        ],
    }
    assert service.load_calls == [
        {
            "tenant_id": INTERNAL_TENANT_ID,
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "instance_id": "node-a",
            "epoch": 7,
        }
    ]


def _update_payload(update_bytes: bytes = b"binary-yjs-update") -> dict[str, Any]:
    """Return a valid internal update request."""
    suggestion_id = str(uuid.UUID("44444444-4444-4444-8444-444444444444"))
    patch_id = str(uuid.UUID("66666666-6666-4666-8666-666666666666"))
    return {
        "tenant_id": INTERNAL_TENANT_ID,
        "generation": 4,
        "instance_id": "node-a",
        "epoch": 7,
        "lease_id": str(uuid.UUID("33333333-3333-4333-8333-333333333333")),
        "actor_user_id": str(ACTOR_ID),
        "update_hash": _sha256(update_bytes),
        "update_base64": base64.b64encode(update_bytes).decode("ascii"),
        "actor_kind": "human",
        "change_kind": "suggestion",
        "change_summary": {
            "changes_document": True,
            "edits": [
                {
                    "before": "",
                    "after": "New text",
                    "kind": "insertion",
                    "position": 4,
                }
            ],
            "omitted_edit_count": 0,
        },
        "suggestion_ids": [suggestion_id],
        "suggestions": [
            {
                "suggestion_id": suggestion_id,
                "patch_id": patch_id,
                "author_id": str(ACTOR_ID),
                "created_at": 1_784_112_000,
                "kind": "insertion",
            }
        ],
        "patches": [
            {
                "patch_id": patch_id,
                "author_id": str(ACTOR_ID),
                "created_at": 1_784_112_000,
                "active_suggestion_ids": [suggestion_id],
                "kinds": ["insertion"],
                "superseded_suggestion_ids": [],
            }
        ],
        "decision": None,
        "decision_outcome": None,
        "command_id": str(uuid.UUID("55555555-5555-4555-8555-555555555555")),
        "command_payload_hash": _sha256(b"canonical-command"),
        "expected_sequence": 11,
    }


def test_internal_update_validates_and_preserves_binary_metadata() -> None:
    """A valid append reaches the service as the typed durable update contract."""
    service = _InternalService()
    client = _internal_client(service)

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=_update_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "sequence": 12,
        "persisted_sequence": 12,
        "duplicate": False,
    }
    update = service.persisted_updates[0]
    assert update.update_bytes == b"binary-yjs-update"
    assert update.tenant_id == INTERNAL_TENANT_ID
    assert update.update_hash == _sha256(b"binary-yjs-update")
    assert update.actor_user_id == ACTOR_ID
    assert update.suggestion_ids == (
        str(uuid.UUID("44444444-4444-4444-8444-444444444444")),
    )
    assert update.suggestions[0].kind == "insertion"
    assert update.patches[0].active_suggestion_ids == update.suggestion_ids
    assert update.patches[0].superseded_suggestion_ids == ()
    assert update.change_summary["edits"][0]["after"] == "New text"
    assert update.decision_outcome is None
    assert update.decision is None
    assert update.command_payload_hash == _sha256(b"canonical-command")
    assert update.expected_sequence == 11


def test_internal_update_carries_the_suggestions_an_edit_removed() -> None:
    """A direct edit names the suggestions it removed and the patches they
    left; the store receives them as the typed contract."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    rejected = str(uuid.UUID("44444444-4444-4444-8444-444444444444"))
    payload["change_kind"] = "direct"
    payload["suggestion_ids"] = []
    payload["suggestions"] = []
    payload["patches"][0]["active_suggestion_ids"] = []
    payload["patches"][0]["kinds"] = []
    payload["rejected_suggestion_ids"] = [rejected]
    payload["command_id"] = None
    payload["command_payload_hash"] = None

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 200
    update = service.persisted_updates[0]
    assert update.change_kind == "direct"
    assert update.rejected_suggestion_ids == (rejected,)
    assert update.patches[0].active_suggestion_ids == ()


def test_internal_update_without_removals_from_an_older_sidecar_removes_nothing() -> None:
    """A sidecar that predates removal by edit omits the field."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload.pop("rejected_suggestion_ids", None)

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 200
    assert service.persisted_updates[0].rejected_suggestion_ids == ()


@pytest.mark.parametrize(
    "rejected",
    [
        ["not-a-uuid"],
        [str(ACTOR_ID), str(ACTOR_ID)],
        "44444444-4444-4444-8444-444444444444",
    ],
)
def test_internal_update_refuses_a_malformed_rejection_list(rejected: Any) -> None:
    """Rejected ids are UUIDs, listed once each."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload["rejected_suggestion_ids"] = rejected

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 400
    assert service.persisted_updates == []


def test_internal_update_accepts_angle_brackets_in_change_summary() -> None:
    """Getippte Winkelklammern duerfen einen Schreibvorgang nicht scheitern lassen.

    ``before``/``after`` sind woertlicher Nutzertext. Eine Zeichenregel auf
    diesem Anzeigefeld lehnte darum keine Auszeichnung ab, sondern eine
    Tastatureingabe -- und weil die Ablehnung ein HTTP 400 auf dem Schreibpfad
    ist, verlor der Nutzer nicht die Zusammenfassung, sondern die Aenderung.
    Der Sidecar benannte die 400 als ``internal_consistency`` und schloss die
    Verbindung; der Browser verband endlos neu, und das Dokument war fuer ALLE
    Beteiligten gesperrt.
    """
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload["change_summary"]["edits"][0]["after"] = "if (a < b) return x > y"

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 200
    persisted = service.persisted_updates[0]
    # Woertlich, nicht escapt: die einzige Senke ist ein React-Textkind.
    assert persisted.change_summary["edits"][0]["after"] == "if (a < b) return x > y"


def test_internal_update_still_rejects_a_malformed_change_summary() -> None:
    """Gegenprobe: Form und Groesse bleiben hart.

    Der Wegfall der Zeichenregel darf die Pruefung nicht insgesamt aufweichen.
    """
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload["change_summary"]["edits"][0]["after"] = "x" * 161

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["error"]["reason"] == "invalid_change_summary"
    assert service.persisted_updates == []


@pytest.mark.parametrize("changes_document", [True, False])
def test_internal_update_keeps_whether_the_change_altered_the_document(
    changes_document: bool,
) -> None:
    """The sidecar reports whether an update changed what a reader sees; the
    summary keeps it, and the activity history reads it there."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload["change_summary"]["changes_document"] = changes_document

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 200
    persisted = service.persisted_updates[0]
    assert persisted.change_summary["changes_document"] is changes_document


def test_internal_update_from_an_older_sidecar_keeps_no_claim_about_the_document() -> None:
    """A sidecar that predates the field never sends it; the summary then
    claims nothing, and the history lists the row as before."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload["change_summary"].pop("changes_document")

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 200
    assert "changes_document" not in service.persisted_updates[0].change_summary


@pytest.mark.parametrize("value", [None, "false", 0, 1])
def test_internal_update_refuses_a_document_change_flag_that_is_not_a_boolean(
    value: Any,
) -> None:
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload["change_summary"]["changes_document"] = value

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["error"]["reason"] == "invalid_change_summary"
    assert service.persisted_updates == []


def test_internal_duplicate_update_preserves_current_document_watermark() -> None:
    """A replay returns its original coordinate and the locked room watermark."""
    service = _InternalService()
    service.persisted_result = PersistedCollaborationUpdate(
        sequence=4,
        persisted_sequence=9,
        duplicate=True,
        persisted_at=0.0,
    )
    client = _internal_client(service)

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=_update_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "sequence": 4,
        "persisted_sequence": 9,
        "duplicate": True,
    }


def test_internal_update_hash_lookup_is_fenced_and_tenant_scoped() -> None:
    """Reconnect reconciliation returns only hashes durably stored in scope."""
    service = _InternalService()
    client = _internal_client(service)
    persisted_hash = _sha256(b"persisted-update")
    missing_hash = _sha256(b"missing-update")

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates:lookup",
            headers=INTERNAL_HEADERS,
            json={
                "tenant_id": INTERNAL_TENANT_ID,
                "generation": 4,
                "hashes": [persisted_hash, missing_hash],
                "instance_id": "node-a",
                "epoch": 7,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "updates": [{"hash": persisted_hash, "sequence": 12}]
    }
    assert service.update_lookups == [
        {
            "tenant_id": INTERNAL_TENANT_ID,
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "update_hashes": (persisted_hash, missing_hash),
            "instance_id": "node-a",
            "epoch": 7,
        }
    ]


def test_internal_durable_update_flow_requires_explicit_tenant() -> None:
    """The trusted sidecar cannot fall through to an implicit deployment tenant."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    del payload["tenant_id"]

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["error"]["reason"] == "invalid_tenant_id"
    assert service.persisted_updates == []


def test_internal_command_lookup_returns_exact_persisted_identity() -> None:
    """A lost sidecar response resolves only through its canonical payload hash."""
    service = _InternalService()
    command_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
    command_hash = _sha256(b"canonical-command")
    service.command_result = CollaborationPersistedCommand(
        actor_kind="human",
        actor_user_id=ACTOR_ID,
        change_kind="decision",
        command_id=command_id,
        command_payload_hash=command_hash,
        decision="accept",
        generation=4,
        patch_ids=(str(uuid.UUID("66666666-6666-4666-8666-666666666666")),),
        sequence=12,
        suggestion_ids=(),
        update_hash=_sha256(b"binary-yjs-update"),
    )
    client = _internal_client(service)

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/commands:lookup",
            headers=INTERNAL_HEADERS,
            json={
                "generation": 4,
                "tenant_id": INTERNAL_TENANT_ID,
                "command_id": str(command_id),
                "command_payload_hash": command_hash,
                "instance_id": "node-a",
                "epoch": 7,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "found": True,
        "actor_kind": "human",
        "actor_user_id": str(ACTOR_ID),
        "change_kind": "decision",
        "command_id": str(command_id),
        "command_payload_hash": command_hash,
        "decision": "accept",
        "generation": 4,
        "patch_ids": [
            str(uuid.UUID("66666666-6666-4666-8666-666666666666"))
        ],
        "sequence": 12,
        "suggestion_ids": [],
        "update_hash": _sha256(b"binary-yjs-update"),
    }
    assert service.command_lookups == [
        {
            "tenant_id": INTERNAL_TENANT_ID,
            "document_id": DOCUMENT_ID,
            "generation": 4,
            "command_id": command_id,
            "command_payload_hash": command_hash,
            "instance_id": "node-a",
            "epoch": 7,
        }
    ]


def test_internal_command_lookup_reports_missing_without_mutation() -> None:
    """An unseen command is a typed miss so the sidecar may execute it once."""
    service = _InternalService()
    client = _internal_client(service)

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/commands:lookup",
            headers=INTERNAL_HEADERS,
            json={
                "generation": 4,
                "tenant_id": INTERNAL_TENANT_ID,
                "command_id": str(
                    uuid.UUID("55555555-5555-4555-8555-555555555555")
                ),
                "command_payload_hash": _sha256(b"canonical-command"),
                "instance_id": "node-a",
                "epoch": 7,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"found": False}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("update_base64", "not!base64", "invalid_update_base64"),
        ("update_base64", "YR==", "non_canonical_update_base64"),
        ("update_hash", "0" * 64, "update_hash_mismatch"),
        ("update_hash", "A" * 64, "invalid_update_hash"),
    ],
)
def test_internal_update_rejects_noncanonical_base64_and_bad_hashes(
    field: str,
    value: str,
    reason: str,
) -> None:
    """Malformed bytes and unverifiable digests never reach persistence."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _update_payload()
    payload[field] = value

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/updates",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "message": "Invalid collaboration request.",
        "type": "invalid_request_error",
        "reason": reason,
    }
    assert service.persisted_updates == []


def _snapshot_payload(state_update: bytes = b"compacted-state") -> dict[str, Any]:
    """Return a valid internal snapshot request."""
    projection = "projection-at-12"
    return {
        "tenant_id": INTERNAL_TENANT_ID,
        "generation": 4,
        "covered_sequence": 12,
        "state_update_base64": base64.b64encode(state_update).decode("ascii"),
        "state_vector_base64": base64.b64encode(b"compacted-vector").decode(
            "ascii"
        ),
        "state_hash": _sha256(state_update),
        "projection_markdown": projection,
        "projection_hash": _sha256(projection.encode("utf-8")),
        "schema_version": 1,
        "schema_hash": _sha256(b"schema"),
        "instance_id": "node-a",
        "epoch": 7,
    }


def test_internal_snapshot_validates_binary_contract_before_acknowledgement() -> None:
    """A 204 means the service received a verified, fenced snapshot DTO."""
    service = _InternalService()
    client = _internal_client(service)

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/snapshots",
            headers=INTERNAL_HEADERS,
            json=_snapshot_payload(),
        )

    assert response.status_code == 204
    snapshot, projection, instance_id, epoch, tenant_id = service.stored_snapshots[0]
    assert snapshot.state_update == b"compacted-state"
    assert snapshot.state_vector == b"compacted-vector"
    assert snapshot.state_hash == _sha256(b"compacted-state")
    assert snapshot.covered_sequence == 12
    assert projection == "projection-at-12"
    assert instance_id == "node-a"
    assert epoch == 7
    assert tenant_id == INTERNAL_TENANT_ID


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "state_update_base64",
            "YR==",
            "non_canonical_state_update_base64",
        ),
        (
            "state_vector_base64",
            "YR==",
            "non_canonical_state_vector_base64",
        ),
        ("state_hash", "0" * 64, "state_hash_mismatch"),
        ("projection_hash", "F" * 64, "invalid_projection_hash"),
    ],
)
def test_internal_snapshot_rejects_unverifiable_payloads(
    field: str,
    value: str,
    reason: str,
) -> None:
    """Snapshot validation is fail-closed for every binary authority field."""
    service = _InternalService()
    client = _internal_client(service)
    payload = _snapshot_payload()
    payload[field] = value

    with client:
        response = client.post(
            f"/internal/collaboration/documents/{DOCUMENT_ID}/snapshots",
            headers=INTERNAL_HEADERS,
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["error"]["reason"] == reason
    assert service.stored_snapshots == []


@pytest.mark.parametrize(
    ("error", "status", "reason", "current_sequence"),
    [
        (CollaborationDocumentNotFound(DOCUMENT_ID), 404, "not_found", None),
        (CollaborationLeaseInvalid("lease_expired"), 401, "lease_expired", None),
        (CollaborationLeaseInvalid("access_revoked"), 403, "access_revoked", None),
        (
            CollaborationAuthenticationRequired("inactive"),
            401,
            "lease_invalid",
            None,
        ),
        (CollaborationInstanceFenced("stale"), 409, "instance_fenced", None),
        (
            CollaborationConflict("sequence_conflict", current_sequence=19),
            409,
            "sequence_conflict",
            19,
        ),
        (CollaborationDocumentTooLarge("frame"), 413, "payload_too_large", None),
    ],
)
def test_internal_domain_errors_have_stable_http_semantics(
    error: Exception,
    status: int,
    reason: str,
    current_sequence: int | None,
) -> None:
    """The sidecar can distinguish auth, fencing, conflict, and size failures."""
    service = _InternalService()
    service.error = error
    client = _internal_client(service)

    with client:
        response = client.post(
            "/internal/collaboration/leases/introspect",
            headers=INTERNAL_HEADERS,
            json={
                "lease_token": "opaque-lease",
                "room": f"inqtrix-editor-v1:{DOCUMENT_ID}:g4",
                "instance_id": "node-a",
                "epoch": 7,
            },
        )

    assert response.status_code == status
    assert response.json()["error"]["reason"] == reason
    if current_sequence is not None:
        assert response.json()["error"]["current_sequence"] == current_sequence


class _Registry:
    """Empty algorithm registry required by the capability manifest."""

    def ids(self) -> tuple[str, ...]:
        """Return no optional algorithms."""
        return ()

    def manifest(self) -> list[dict[str, Any]]:
        """Return an empty public algorithm manifest."""
        return []


class _AvailabilityService:
    """Collaboration readiness probe fake."""

    def __init__(self, available: bool) -> None:
        self.available = available
        self.calls = 0

    async def service_available(self) -> bool:
        """Return the current sidecar readiness result."""
        self.calls += 1
        return self.available


def _capabilities_client(
    service: _AvailabilityService | None,
    *,
    sharing_enabled: bool = False,
    sharing_available: bool = False,
    guest_links_enabled: bool = False,
    guest_links_available: bool = False,
    guest_link_stats_enabled: bool = True,
    guest_links_allow_insecure_http: bool = False,
    public_base_url: str = "http://localhost:8080",
) -> TestClient:
    """Build the real manifest route with a minimal no-infrastructure container."""
    settings = SimpleNamespace(
        server=SimpleNamespace(
            enable_openapi=False,
            public_base_url=public_base_url,
            session_titles_enabled=False,
        ),
        queue=SimpleNamespace(backend="memory"),
        storage=SimpleNamespace(backend="postgres", max_file_bytes=1024),
        sharing=SimpleNamespace(enabled=sharing_enabled),
        collaboration=SimpleNamespace(
            enabled=service is not None,
            protocol_version=3,
            schema_version=5,
        ),
        editor_guest_links=SimpleNamespace(
            enabled=guest_links_enabled,
            stats_enabled=guest_link_stats_enabled,
            allow_insecure_http=guest_links_allow_insecure_http,
        ),
        agent=AgentSettings(
            agent_tier="",
            depth="normal",
            max_total_seconds=300,
            reasoning_timeout=120,
            editor_assistant_timeout=120,
            search_timeout=90,
            claim_extract_timeout=60,
        ),
        agent_platform=AgentPlatformSettings(
            default_autonomy="balanced",
            default_agent_mode="workspace_agent",
            max_clarification_rounds=2,
            advanced_autonomy=False,
            max_parallel_children=2,
            discovery_max_tool_calls=4,
            max_plan_tasks=12,
            skills_max_attached=5,
            skills_disclosure_budget_chars=20_000,
        ),
        quota=QuotaSettings(max_tokens_per_run=0),
    )
    registry = _Registry()
    container = SimpleNamespace(
        settings=settings,
        registry=registry,
        providers=SimpleNamespace(llm=None),
        knowledge_service=None,
        knowledge_ceiling=None,
        file_service=None,
        object_store_backend="none",
        capability_registry=None,
        auth_provider=SimpleNamespace(mode="local"),
        editor_collaboration_service=service,
        editor_guest_link_service=object() if guest_links_available else None,
        share_service=object() if sharing_available else None,
        prompt_template_service=None,
        skill_service=None,
        quota_service=None,
        chat_history_service=None,
    )
    app = FastAPI()
    app.include_router(capabilities_router.build_router(container))
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("service", "configured", "available"),
    [
        (None, False, False),
        (_AvailabilityService(False), True, False),
        (_AvailabilityService(True), True, True),
    ],
)
def test_capabilities_publish_configured_and_live_collaboration_state(
    service: _AvailabilityService | None,
    configured: bool,
    available: bool,
) -> None:
    """Clients see disabled, degraded, and ready states without private URLs."""
    client = _capabilities_client(service)

    with client:
        response = client.get("/v1/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["features"]["collaboration"] is available
    assert payload["collaboration"] == {
        "configured": configured,
        "service_available": available,
        "transport_path": "/collaboration",
        "protocol_version": 3,
        "schema_version": 5,
        "mode": "single_replica",
    }
    assert payload["feature_status"]["collaboration"] == {
        "configured": configured,
        "available": available,
        "state": (
            "enabled" if available else "degraded" if configured else "disabled"
        ),
        "reason_code": (
            None if available
            else "service_unreachable" if configured
            else "operator_disabled"
        ),
    }
    assert INTERNAL_SECRET not in response.text


def test_capabilities_publish_independent_modular_feature_states() -> None:
    """Simple gates and operator diagnostics remain consistent per module."""
    enabled = _capabilities_client(
        _AvailabilityService(True),
        sharing_enabled=True,
        sharing_available=True,
        guest_links_enabled=True,
        guest_links_available=True,
        guest_link_stats_enabled=False,
        public_base_url="https://inqtrix.example.test",
    ).get("/v1/capabilities").json()

    assert enabled["features"] == {
        **enabled["features"],
        "sharing": True,
        "collaboration": True,
        "editor_guest_links": True,
        "editor_guest_link_stats": False,
    }
    assert enabled["feature_status"]["sharing"] == {
        "configured": True,
        "available": True,
        "state": "enabled",
        "reason_code": None,
    }
    assert enabled["feature_status"]["editor_guest_links"] == {
        "configured": True,
        "available": True,
        "state": "enabled",
        "reason_code": None,
    }
    assert enabled["feature_status"]["editor_guest_link_stats"] == {
        "configured": False,
        "available": False,
        "state": "disabled",
        "reason_code": "operator_disabled",
    }

    degraded = _capabilities_client(
        _AvailabilityService(False),
        sharing_enabled=True,
        sharing_available=True,
        guest_links_enabled=True,
        public_base_url="http://localhost:8080",
    ).get("/v1/capabilities").json()

    assert degraded["features"]["sharing"] is True
    assert degraded["features"]["collaboration"] is False
    assert degraded["features"]["editor_guest_links"] is False
    assert degraded["feature_status"]["collaboration"]["state"] == "degraded"
    assert (
        degraded["feature_status"]["collaboration"]["reason_code"]
        == "service_unreachable"
    )
    assert degraded["feature_status"]["editor_guest_links"] == {
        "configured": True,
        "available": False,
        "state": "degraded",
        "reason_code": "collaboration_required",
    }


def test_capabilities_explain_https_requirement_for_configured_guest_links() -> None:
    """A configured guest module never degrades into a generic reason."""
    payload = _capabilities_client(
        _AvailabilityService(True),
        sharing_enabled=True,
        sharing_available=True,
        guest_links_enabled=True,
        guest_links_available=False,
        public_base_url="http://localhost:8080",
    ).get("/v1/capabilities").json()

    assert payload["features"]["editor_guest_links"] is False
    assert payload["feature_status"]["editor_guest_links"] == {
        "configured": True,
        "available": False,
        "state": "degraded",
        "reason_code": "https_required",
    }


def test_capabilities_accept_http_guest_links_with_explicit_opt_in() -> None:
    """The insecure-HTTP opt-in serves guest links over http — the
    manifest must not report https_required for a feature the server
    actually provides (the FE would hide it)."""
    payload = _capabilities_client(
        _AvailabilityService(True),
        sharing_enabled=True,
        sharing_available=True,
        guest_links_enabled=True,
        guest_links_available=True,
        guest_links_allow_insecure_http=True,
        public_base_url="http://localhost:8080",
    ).get("/v1/capabilities").json()

    assert payload["features"]["editor_guest_links"] is True
    assert payload["feature_status"]["editor_guest_links"] == {
        "configured": True,
        "available": True,
        "state": "enabled",
        "reason_code": None,
    }
