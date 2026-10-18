"""Contracts of the file-asset-record store.

Mirrors editor_ports: the store owns persistence only; scoping lives in
:class:`~inqtrix.services.asset_records_service.AssetRecordsService` and
the wire shape in the router. Two implementations behind one port:
:class:`~inqtrix.project.asset_records_memory.MemoryAssetStore` (offline/
test) and :class:`~inqtrix.project.asset_records_postgres.PostgresAssetStore`.

Like editor documents, an asset carries a heavy ``extracted_text`` body:
``list_assets_page`` returns metadata only (``extracted_text=""``);
``get_asset`` returns the full record with the text (load-on-open).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol, get_args, runtime_checkable

from inqtrix.project.scoped_upsert import ResourceScope

# The two intents a caller can create an asset row with. A Literal rather
# than a free string: the store contract knows exactly these two, and a typo
# must fall at typecheck time, not land as a silent new state in the
# database. The column itself cannot carry the intent -- server_file_id=NULL
# also means "local-only asset that was never uploaded", which is
# legitimately "ready".
InitialUploadStatus = Literal["ready", "awaiting_upload"]


def ensure_initial_upload_status(value: str) -> None:
    """Reject unknown insert intents at runtime, derived from the Literal.

    The Literal above binds only a typechecker, and none runs in this
    repo's verification chain -- a typo would otherwise land as a silent
    new state in the database. Both store backends call this before the
    INSERT; the allowed set has exactly one definition.
    """
    if value not in get_args(InitialUploadStatus):
        raise ValueError(
            "initial_upload_status must be one of "
            f"{get_args(InitialUploadStatus)}, got {value!r}"
        )


class SectionNotFound(KeyError):
    """Raised when a section id is unknown to the store (HTTP 404)."""


class GroupNotFound(KeyError):
    """Raised when a group id is unknown to the store (HTTP 404)."""


class AssetNotFound(KeyError):
    """Raised when an asset id is unknown to the store (HTTP 404)."""


class AssetDeletionInProgress(RuntimeError):
    """Raised when a client mutation targets an asset being deleted."""


class AssetUploadConflict(RuntimeError):
    """Raised when an upload id is reused for a different binding or blob."""


AssetSectionSemanticRole = Literal[
    "temporary",
    "library",
    "project_sources",
    "custom",
]
"""Server-owned meaning of a file-library section.

The three prepared roles are unique within one owner/workspace scope.
``custom`` is deliberately non-unique: titles are presentation data and two
user-created sections may have exactly the same title.  ``None`` is reserved
for rows created before this identity contract existed.
"""


DEFAULT_ASSET_SECTION_SPECS: tuple[
    tuple[AssetSectionSemanticRole, str, str], ...
] = (
    ("temporary", "temporary", "Temporäre Dateien"),
    ("library", "custom", "Bibliothek"),
    ("project_sources", "custom", "Projekt-Quellen"),
)
"""Stable prepared-role order and its initial presentation."""


@dataclass(frozen=True)
class AssetSection:
    """A top-level file-library section.

    Attributes:
        id: Client-supplied id (``fsec_...``), the primary key.
        kind: ``temporary`` or ``custom``.
        title: Section label.
        created_at/updated_at: Unix timestamps.
        tenant_id/created_by_user_id/workspace_id: The scope.
        semantic_role: Server-owned stable role.  ``None`` means the row
            predates the role contract and must not be guessed from its title.
    """

    id: str
    kind: str
    title: str
    created_at: float
    updated_at: float
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None
    semantic_role: AssetSectionSemanticRole | None = None


@dataclass(frozen=True)
class AssetGroup:
    """A group within a section (``section_id``)."""

    id: str
    section_id: str
    title: str
    created_at: float
    updated_at: float
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None


@dataclass(frozen=True)
class AssetRecord:
    """One file-asset record (library metadata + extracted text).

    Attributes:
        id: Client-supplied id (``fa_...``), the primary key.
        section_id: Owning section.
        group_id: Owning group, or ``None`` when ungrouped.
        title/label/file_name/mime_type/origin: Display + provenance.
        page_count: Page count (``None`` when not applicable).
        parse_status: ``parsed``/``partial``/``unsupported``/``error``.
        parse_warning: Optional parse warning text.
        text_truncated: Whether ``extracted_text`` was truncated at ingest.
        size_bytes: Original binary size.
        server_file_id: The /v1/files blob reference, or ``None`` (local-only).
        parser_id: Which parser produced ``extracted_text`` — ``markitdown``
            for the server-side parser ladder, ``client`` for the browser
            fallback, ``None`` when unknown (legacy/local-only rows).
        extracted_text: The heavy body — empty on list rows, full on get.
        created_at/updated_at: Unix timestamps.
    """

    id: str
    section_id: str
    group_id: str | None
    title: str
    label: str
    file_name: str
    mime_type: str
    origin: str
    page_count: int | None
    parse_status: str
    parse_warning: str | None
    text_truncated: bool
    size_bytes: int
    server_file_id: str | None
    extracted_text: str = field(repr=False, default="")
    parser_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    workspace_id: str | None = None
    lifecycle_status: str = "active"
    deletion_operation_id: str | None = None
    deletion_stage: str | None = None
    deletion_error: str | None = None
    upload_status: str = "ready"
    upload_error: str | None = None
    upload_operation_id: str | None = None
    # Server-owned canonical preparation material.  Browser asset PUTs may
    # update ``extracted_text`` for local UX, but document indexing reads only
    # this operation-fenced copy so a stale client cannot replace provenance.
    prepared_text: str = field(repr=False, default="")
    prepared_parser_id: str | None = None
    prepared_parser_revision: str | None = None
    prepared_content_hash: str | None = None
    prepared_file_sha256: str | None = None
    prepared_page_texts: tuple[str, ...] = field(repr=False, default=())
    prepared_at: float | None = None


@runtime_checkable
class AssetStore(Protocol):
    """Persistence port for file-library sections, groups, and asset records."""

    # -- sections --------------------------------------------------------- #

    async def upsert_section(
        self,
        *,
        id: str,
        kind: str,
        title: str,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> AssetSection: ...

    async def list_sections(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AssetSection]: ...

    async def ensure_default_sections(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> list[AssetSection]:
        """Return one canonical section for every prepared semantic role.

        The operation is idempotent and concurrency-safe within the exact
        owner/workspace scope.  Existing legacy rows are never relabelled.
        """
        ...

    async def delete_section(
        self, section_id: str, *, scope: ResourceScope
    ) -> None: ...

    # -- groups ----------------------------------------------------------- #

    async def upsert_group(
        self,
        *,
        id: str,
        section_id: str,
        title: str,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> AssetGroup: ...

    async def list_groups(
        self, *, created_by_user_id: uuid.UUID | None, workspace_id: str | None
    ) -> list[AssetGroup]: ...

    async def delete_group(
        self, group_id: str, *, scope: ResourceScope
    ) -> None: ...

    # -- assets ----------------------------------------------------------- #

    async def upsert_asset(
        self,
        *,
        id: str,
        section_id: str,
        group_id: str | None,
        title: str,
        label: str,
        file_name: str,
        mime_type: str,
        origin: str,
        page_count: int | None,
        parse_status: str,
        parse_warning: str | None,
        text_truncated: bool,
        size_bytes: int,
        server_file_id: str | None,
        parser_id: str | None = None,
        extracted_text: str,
        created_at: float,
        updated_at: float,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        initial_upload_status: InitialUploadStatus = "ready",
    ) -> AssetRecord:
        """Create or update one asset row.

        ``initial_upload_status`` takes effect ONLY when the row is
        inserted. The caller knows what it is creating: ``save_asset``
        a finished local asset (``"ready"``), ``reserve_upload`` a
        reserved landing slot (``"awaiting_upload"``). On an existing
        row the stored status is untouched -- ``upload_status`` is
        deliberately absent from the mutable column set, or a repeated
        reservation could reset a finalised row.
        """
        ...

    async def list_assets_page(
        self,
        *,
        created_by_user_id: uuid.UUID | None,
        workspace_id: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[AssetRecord], str | None]:
        """One keyset page of the caller's assets (newest first), METADATA
        ONLY (``extracted_text=""`` — the text loads via get_asset)."""
        ...

    async def get_asset(self, asset_id: str) -> AssetRecord:
        """One asset WITH its extracted text (load-on-open), or
        :class:`AssetNotFound`."""
        ...

    async def find_asset_by_server_file_id(
        self, server_file_id: str
    ) -> AssetRecord | None:
        """Return the asset bound to an original file, when one exists.

        The file identifier is globally opaque.  This lookup is used only
        after the file service has authorized the caller, so direct deletion
        cannot bypass the asset aggregate lifecycle.
        """
        ...

    async def list_assets_by_server_file_id(
        self, server_file_id: str
    ) -> list[AssetRecord]:
        """Return every legacy reference to one physical original file."""
        ...

    async def detach_server_file_for_deletion(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        operation_id: str,
        expected_server_file_id: str,
    ) -> AssetRecord:
        """Release one file FK while retaining the operation-owned asset.

        Only the deletion operation already recorded on the asset may clear
        its expected binding. Implementations must make a repeated call after
        a successful detach idempotent so the immutable operation manifest can
        resume blob and quota cleanup without discarding the visible retry
        anchor.
        """
        ...

    async def list_assets_by_ids(
        self,
        asset_ids: tuple[str, ...],
        *,
        scope: ResourceScope,
    ) -> list[AssetRecord]:
        """Batch-load exact assets inside one owner/workspace scope."""
        ...

    async def delete_asset(
        self, asset_id: str, *, scope: ResourceScope
    ) -> None: ...

    async def set_asset_deletion_state(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        lifecycle_status: str,
        deletion_operation_id: str | None,
        deletion_stage: str | None,
        deletion_error: str | None,
    ) -> AssetRecord: ...

    async def finalize_asset_upload(
        self,
        *,
        id: str,
        section_id: str,
        group_id: str | None,
        title: str,
        label: str,
        file_name: str,
        mime_type: str,
        origin: str,
        page_count: int | None,
        parse_status: str,
        parse_warning: str | None,
        text_truncated: bool,
        size_bytes: int,
        server_file_id: str,
        parser_id: str | None,
        created_at: float,
        updated_at: float,
        scope: ResourceScope,
        upload_operation_id: str | None = None,
    ) -> AssetRecord:
        """Attach one uploaded blob only while its reservation is active.

        Implementations must perform this as a lifecycle CAS and must never
        insert a missing row.
        """
        ...

    async def tombstone_asset_id(
        self, asset_id: str, *, scope: ResourceScope
    ) -> None:
        """Prevent a missing id from being recreated during retained deletion."""
        ...

    async def tombstone_section_id(
        self, section_id: str, *, scope: ResourceScope
    ) -> None:
        """Prevent new children/recreation while a section delete is retained."""
        ...

    async def tombstone_group_id(
        self, group_id: str, *, scope: ResourceScope
    ) -> None:
        """Prevent recreation or new child bindings during retained deletion."""
        ...

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
        """Persist the server-owned original-file transfer state."""
        ...

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
        """CAS-persist canonical server parsing for one bound original.

        ``None`` fences the compatibility path for assets that predate durable
        upload operations.  Their immutable server file remains the source;
        browser-provided display text is never promoted to canonical input.
        """
        ...

    async def set_asset_parse_failure(
        self,
        asset_id: str,
        *,
        scope: ResourceScope,
        server_file_id: str,
        expected_upload_operation_id: str,
        message: str,
    ) -> AssetRecord:
        """Record a deterministic parse failure without trusting client text."""
        ...

    async def list_assets_for_target(
        self,
        *,
        section_id: str | None,
        group_id: str | None,
        scope: ResourceScope,
    ) -> list[AssetRecord]:
        """Return full records covered by an owner-scoped container target."""
        ...

    async def aclose(self) -> None: ...
