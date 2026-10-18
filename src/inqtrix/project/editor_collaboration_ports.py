"""Contracts for durable editor live-collaboration persistence.

The collaboration server never receives database credentials. It calls the
internal FastAPI surface, whose service persists through this port. Keeping the
transaction boundary in Python makes FastAPI the sole authority for identity,
sharing, sequence allocation, fencing, and retention while Hocuspocus remains
a replaceable synchronization component.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

CollaborationPermission = Literal["view", "suggest", "edit"]
CollaborationActorKind = Literal["human", "guest", "assistant", "agent", "system"]
CollaborationChangeKind = Literal["direct", "suggestion", "decision", "system"]
CollaborationDecision = Literal["accept", "reject"]
CollaborationSuggestionKind = Literal[
    "insertion",
    "deletion",
    "replacement",
    "format",
    "structure",
]


class CollaborationDocumentNotFound(KeyError):
    """Raised when a collaboration document is absent or tenant-invisible."""


class CollaborationConflict(RuntimeError):
    """Raised when a generation, revision, mode, schema, or command conflicts.

    Attributes:
        reason: Stable machine-readable conflict reason.
        current_sequence: Current durable sequence when relevant.
    """

    def __init__(self, reason: str, *, current_sequence: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current_sequence = current_sequence


class CollaborationLeaseInvalid(PermissionError):
    """Raised when a lease is unknown, expired, revoked, or mismatched."""

    def __init__(self, reason: str = "lease_invalid") -> None:
        super().__init__(reason)
        self.reason = reason


class CollaborationInstanceFenced(RuntimeError):
    """Raised when a stale Node instance attempts a durable write."""


class CollaborationRateLimited(RuntimeError):
    """Raised when lease issuance or concurrent-session limits are exceeded."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CollaborationDocumentState:
    """Durable state required to load or authorize one collaboration room."""

    document_id: str
    tenant_id: str
    generation: int
    schema_version: int
    schema_hash: str
    persisted_sequence: int
    projection_sequence: int
    content_markdown: str
    projection_updated_at: float | None
    owner_user_id: uuid.UUID | None
    deleted_at: float | None = None


@dataclass(frozen=True)
class CollaborationUpdate:
    """One idempotently persisted binary Yjs update and its audit metadata."""

    document_id: str
    tenant_id: str
    generation: int
    sequence: int
    update_hash: str
    update_bytes: bytes | None = field(repr=False)
    actor_user_id: uuid.UUID | None
    actor_kind: CollaborationActorKind
    change_kind: CollaborationChangeKind
    actor_guest_identity_id: uuid.UUID | None = None
    suggestion_ids: tuple[str, ...] = ()
    change_summary: dict[str, Any] = field(default_factory=dict, repr=False)
    decision_outcome: Literal["accepted", "rejected"] | None = None
    command_id: uuid.UUID | None = None
    created_at: float = 0.0
    payload_pruned_at: float | None = None


@dataclass(frozen=True)
class CollaborationSnapshot:
    """Verified binary Yjs state covering all updates through a sequence."""

    document_id: str
    tenant_id: str
    generation: int
    covered_sequence: int
    state_update: bytes = field(repr=False)
    state_vector: bytes = field(repr=False)
    state_hash: str
    projection_hash: str
    schema_version: int
    schema_hash: str
    created_at: float


@dataclass(frozen=True)
class CollaborationSnapshotCandidate:
    """One verified snapshot and a complete hash-verifiable durable tail."""

    snapshot: CollaborationSnapshot
    updates: tuple[CollaborationUpdate, ...]


@dataclass(frozen=True)
class CollaborationLoadedState:
    """Latest verified snapshot plus fallback candidates and update tails."""

    document: CollaborationDocumentState
    snapshot: CollaborationSnapshot
    updates: tuple[CollaborationUpdate, ...]
    fallback_candidates: tuple[CollaborationSnapshotCandidate, ...] = ()


@dataclass(frozen=True)
class CollaborationLease:
    """Database-backed, document-scoped authorization lease."""

    lease_id: uuid.UUID
    token_hash: str = field(repr=False)
    tenant_id: str
    document_id: str
    generation: int
    user_id: uuid.UUID | None
    permission: CollaborationPermission
    session_id: str | None
    issued_at: float
    expires_at: float
    last_validated_at: float
    actor_kind: Literal["user", "guest"] = "user"
    guest_identity_id: uuid.UUID | None = None
    guest_link_id: uuid.UUID | None = None
    rotation_command_id: uuid.UUID | None = None
    rotated_from_lease_id: uuid.UUID | None = None
    revoked_at: float | None = None


@dataclass(frozen=True)
class CollaborationInstanceLease:
    """Single-writer fencing lease held by the active Node process."""

    instance_id: str
    epoch: int
    lease_expires_at: float
    updated_at: float


@dataclass(frozen=True)
class CollaborationSuggestion:
    """One changed suggestion descriptor validated by the Node sidecar."""

    suggestion_id: str
    patch_id: str
    author_id: uuid.UUID
    created_at: float
    kind: CollaborationSuggestionKind


@dataclass(frozen=True)
class CollaborationPatchState:
    """Current active suggestion membership for one affected patch."""

    patch_id: str
    author_id: uuid.UUID
    created_at: float
    active_suggestion_ids: tuple[str, ...]
    kinds: tuple[CollaborationSuggestionKind, ...]
    superseded_suggestion_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersistCollaborationUpdate:
    """Input contract for one durable update append."""

    tenant_id: str
    document_id: str
    generation: int
    instance_id: str
    instance_epoch: int
    lease_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    update_hash: str
    update_bytes: bytes = field(repr=False)
    actor_kind: CollaborationActorKind
    change_kind: CollaborationChangeKind
    suggestion_ids: tuple[str, ...] = ()
    change_summary: dict[str, Any] = field(default_factory=dict, repr=False)
    decision_outcome: Literal["accepted", "rejected"] | None = None
    suggestions: tuple[CollaborationSuggestion, ...] = ()
    patches: tuple[CollaborationPatchState, ...] = ()
    decision: CollaborationDecision | None = None
    command_id: uuid.UUID | None = None
    command_payload_hash: str | None = None
    expected_sequence: int | None = None
    now: float = 0.0
    actor_guest_identity_id: uuid.UUID | None = None
    # Suggestions an edit-lease update removed. Each is removed by the
    # update's actor in the update's transaction; its patch in ``patches``
    # shows it gone from the active set.
    rejected_suggestion_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersistedCollaborationUpdate:
    """Idempotent append result returned for the durable acknowledgement."""

    sequence: int
    persisted_sequence: int
    duplicate: bool
    persisted_at: float


@dataclass(frozen=True)
class CollaborationUpdateLookup:
    """Sequence coordinate for a previously persisted update digest."""

    update_hash: str
    sequence: int


@dataclass(frozen=True)
class CollaborationPersistedCommand:
    """Durable idempotency record returned before a server-side mutation."""

    actor_kind: CollaborationActorKind
    actor_user_id: uuid.UUID
    change_kind: Literal["decision", "suggestion"]
    command_id: uuid.UUID
    command_payload_hash: str
    decision: CollaborationDecision | None
    generation: int
    patch_ids: tuple[str, ...]
    sequence: int
    suggestion_ids: tuple[str, ...]
    update_hash: str


@dataclass(frozen=True)
class CollaborationActivity:
    """Content-bounded activity row used by the Changes inspector."""

    sequence: int
    actor_user_id: uuid.UUID | None
    actor_kind: CollaborationActorKind
    change_kind: CollaborationChangeKind
    suggestion_ids: tuple[str, ...]
    command_id: uuid.UUID | None
    created_at: float
    actor_guest_identity_id: uuid.UUID | None = None
    change_summary: dict[str, Any] = field(default_factory=dict, repr=False)
    decision_outcome: Literal["accepted", "rejected"] | None = None
    rejected_suggestion_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollaborationSuggestionRejection:
    """One pending suggestion an edit removed, as its audit entry records it.

    The author is a workspace member or a guest identity, exactly one of the
    two. The update row at ``sequence`` names the actor who removed it.
    """

    sequence: int
    suggestion_id: str
    patch_id: str
    author_user_id: uuid.UUID | None
    author_guest_identity_id: uuid.UUID | None


@dataclass(frozen=True)
class CollaborationCommentActivity:
    """One content-free shared-comment audit event for the activity filter."""

    id: int
    actor_user_id: uuid.UUID | None
    action: str
    created_at: float


@dataclass(frozen=True)
class CollaborationOpenPatch:
    """One pending collaboration patch for the open-changes view.

    Its author is a workspace member or a guest identity, exactly one of the
    two.
    """

    patch_id: str
    author_user_id: uuid.UUID | None
    created_at: float
    suggestion_ids: tuple[str, ...]
    kinds: tuple[CollaborationSuggestionKind, ...]
    exact_edits: tuple[dict[str, Any], ...] | None = field(repr=False)
    author_guest_identity_id: uuid.UUID | None = None


@dataclass(frozen=True)
class CollaborationOpenPatchPage:
    """One bounded raw keyset page of published pending patches."""

    patches: tuple[CollaborationOpenPatch, ...]
    next_cursor: tuple[float, str] | None


@dataclass(frozen=True)
class CollaborationPolicyEvent:
    """Content-free user-event coordinate relevant to live connections."""

    id: int
    target_user_id: uuid.UUID
    scope: str
    resource_type: str
    resource_id: str | None


@dataclass(frozen=True)
class CollaborationPolicyPage:
    """Ordered policy-event page with a global durable cursor."""

    events: tuple[CollaborationPolicyEvent, ...]
    current_cursor: int
    reset_required: bool = False


@dataclass(frozen=True)
class CollaborationCommentMessage:
    """One shared-comment message; deleted rows retain an audit tombstone."""

    message_id: uuid.UUID
    thread_id: uuid.UUID
    revision: int
    author_user_id: uuid.UUID | None
    body_markdown: str
    mention_user_ids: tuple[uuid.UUID, ...]
    created_at: float
    author_guest_identity_id: uuid.UUID | None = None
    edited_at: float | None = None
    deleted_at: float | None = None


@dataclass(frozen=True)
class CollaborationCommentThread:
    """One durable shared discussion thread and its ordered messages."""

    thread_id: uuid.UUID
    document_id: str
    generation: int
    revision: int
    status: Literal["open", "resolved"]
    created_by_user_id: uuid.UUID | None
    resolved_by_user_id: uuid.UUID | None
    resolved_at: float | None
    anchor: dict[str, Any]
    quote_text: str
    created_at: float
    updated_at: float
    created_by_guest_identity_id: uuid.UUID | None = None
    resolved_by_guest_identity_id: uuid.UUID | None = None
    #: Set once the author retired the whole thread. The row and its messages
    #: stay for the audit; every read path skips it, so the card is gone for
    #: the people in the document.
    deleted_at: float | None = None
    messages: tuple[CollaborationCommentMessage, ...] = ()


@dataclass(frozen=True)
class CollaborationCommentPage:
    """Incremental comment snapshot at one document-scoped revision."""

    threads: tuple[CollaborationCommentThread, ...]
    revision: int
    last_read_revision: int
    current_revision: int | None = None
    has_more: bool = False
    participant_user_ids: tuple[uuid.UUID, ...] = ()
    participant_guest_identity_ids: tuple[uuid.UUID, ...] = ()


@runtime_checkable
class EditorCollaborationStore(Protocol):
    """Persistence boundary for collaboration state and authorization."""

    async def load_state(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int | None = None,
    ) -> CollaborationLoadedState:
        """Load the latest snapshot and complete update tail."""
        ...

    async def enable_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
        owner_user_id: uuid.UUID,
        expected_revision: int,
        expected_metadata_revision: int,
        schema_version: int,
        schema_hash: str,
        snapshot: CollaborationSnapshot,
        projection_markdown: str,
        now: float,
    ) -> CollaborationDocumentState:
        """Atomically convert one unshared Markdown document to generation one."""
        ...

    async def issue_lease(
        self,
        lease: CollaborationLease,
        *,
        max_active: int,
        max_issued_per_window: int,
        issued_since: float,
    ) -> CollaborationLease:
        """Persist a bounded lease while enforcing durable issuance limits."""
        ...

    async def rotate_lease(
        self,
        *,
        previous_lease_id: uuid.UUID,
        previous_token_hash: str,
        replacement: CollaborationLease,
        max_issued_per_window: int,
        issued_since: float,
    ) -> CollaborationLease:
        """Atomically revoke one live lease and insert its replacement."""
        ...

    async def introspect_lease(
        self,
        *,
        tenant_id: str,
        lease_id: uuid.UUID,
        token_hash: str,
        now: float,
    ) -> CollaborationLease:
        """Return a currently valid lease and update its validation time."""
        ...

    async def acquire_instance(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        now: float,
        lease_seconds: float,
    ) -> CollaborationInstanceLease:
        """Acquire the single-writer slot and advance its fencing epoch."""
        ...

    async def renew_instance(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        epoch: int,
        now: float,
        lease_seconds: float,
    ) -> CollaborationInstanceLease:
        """Renew an unexpired slot without changing its epoch."""
        ...

    async def get_current_instance(
        self,
        *,
        tenant_id: str,
        now: float,
    ) -> CollaborationInstanceLease | None:
        """Return the authoritative unexpired single-writer lease, if any."""
        ...

    async def validate_instance(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        epoch: int,
        now: float,
    ) -> CollaborationInstanceLease:
        """Require the currently active instance without extending its lease."""
        ...

    async def append_update(
        self, update: PersistCollaborationUpdate
    ) -> PersistedCollaborationUpdate:
        """Fence, authorize, sequence, and idempotently append one Yjs update."""
        ...

    async def lookup_command(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
    ) -> CollaborationPersistedCommand | None:
        """Return an exact prior server command or fail on identity reuse."""
        ...

    async def lookup_updates_by_hashes(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        update_hashes: tuple[str, ...],
        instance_id: str,
        instance_epoch: int,
        now: float,
    ) -> tuple[CollaborationUpdateLookup, ...]:
        """Resolve hashes within one atomically fenced tenant generation."""
        ...

    async def store_snapshot(
        self,
        snapshot: CollaborationSnapshot,
        *,
        projection_markdown: str,
        instance_id: str,
        instance_epoch: int,
        now: float,
    ) -> None:
        """Fence and persist a verified snapshot in one transaction."""
        ...

    async def update_projection(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        covered_sequence: int,
        content_markdown: str,
        projection_hash: str,
        now: float,
    ) -> CollaborationDocumentState:
        """Publish a canonical Markdown projection through a durable sequence."""
        ...

    async def list_activity(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        before_sequence: int | None,
        author_user_id: uuid.UUID | None,
        change_kind: CollaborationChangeKind | None,
        limit: int,
    ) -> tuple[CollaborationActivity, ...]:
        """List durable activity metadata without returning binary payloads.

        A row whose summary says it changed nothing a reader sees is left
        out before the limit applies; it stays stored for replay.
        """
        ...

    async def list_comment_activity(
        self,
        *,
        tenant_id: str,
        document_id: str,
        before_id: int | None,
        author_user_id: uuid.UUID | None,
        limit: int,
    ) -> tuple[CollaborationCommentActivity, ...]:
        """List content-free shared-comment events from the audit trail."""
        ...

    async def list_suggestion_rejections(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        sequences: tuple[int, ...],
        limit: int,
    ) -> tuple[CollaborationSuggestionRejection, ...]:
        """List the suggestions the given update rows removed, newest first,
        from the audit trail."""
        ...

    async def list_open_patches(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        before: tuple[float, str] | None,
        author_user_id: uuid.UUID | None,
        suggestion_kind: CollaborationSuggestionKind | None,
        limit: int,
    ) -> CollaborationOpenPatchPage:
        """List one bounded keyset page of published pending patches,
        newest first by their creation time in one unit
        (:func:`~inqtrix.project.editor_patch_ports.patch_created_at_ms`)."""
        ...

    async def current_policy_cursor(self, *, tenant_id: str) -> int:
        """Return the greatest committed tenant policy-event identifier."""
        ...

    async def policy_events_after(
        self,
        *,
        tenant_id: str,
        cursor: int,
        limit: int,
    ) -> CollaborationPolicyPage:
        """Read editor/user invalidations from the existing user-events feed."""
        ...

    async def list_comment_threads(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        since_revision: int,
        status: Literal["all", "open", "resolved"],
        limit: int,
    ) -> CollaborationCommentPage:
        """List changed shared-comment threads after a durable revision."""
        ...

    async def create_comment_thread(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        anchor: dict[str, Any],
        quote_text: str,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        """Atomically create a thread, first message, audit, and refetch event."""
        ...

    async def add_comment_reply(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str,
        mention_user_ids: tuple[uuid.UUID, ...],
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        """Append one reply using thread revision conflict detection."""
        ...

    async def update_comment_message(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        message_id: uuid.UUID,
        body_markdown: str | None,
        mention_user_ids: tuple[uuid.UUID, ...],
        delete_message: bool,
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        """Edit or tombstone an actor-owned message."""
        ...

    async def set_comment_thread_status(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        thread_id: uuid.UUID,
        status: Literal["open", "resolved"],
        can_moderate: bool,
        expected_revision: int,
        command_id: uuid.UUID,
        command_payload_hash: str,
        now: float,
    ) -> CollaborationCommentThread:
        """Resolve/reopen a thread under author-or-moderator policy."""
        ...

    async def mark_comments_read(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        actor_user_id: uuid.UUID | None,
        actor_guest_identity_id: uuid.UUID | None = None,
        guest_link_id: uuid.UUID | None = None,
        revision: int,
        now: float,
    ) -> int:
        """Advance the caller's personal shared-comment read coordinate."""
        ...

    async def compact(
        self,
        *,
        tenant_id: str,
        document_id: str,
        generation: int,
        instance_id: str,
        instance_epoch: int,
        now: float,
        payload_retention_seconds: float,
        metadata_retention_seconds: float,
    ) -> tuple[int, int]:
        """Fence and prune covered payloads and metadata in one transaction."""
        ...

    async def purge_tombstones(
        self,
        *,
        tenant_id: str,
        instance_id: str,
        instance_epoch: int,
        now: float,
        retention_seconds: float,
    ) -> int:
        """Fence and remove collaboration tombstones in one transaction."""
        ...

    async def tombstone_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
        owner_user_id: uuid.UUID,
        now: float,
    ) -> int:
        """Delete logically, advance generation, revoke shares, and invalidate."""
        ...
