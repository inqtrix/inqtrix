"""The one reader of a file's canonical prepared text.

The upload lifecycle persists exactly one text per original file on its
asset: produced once by the server parser, bound to the file digest and the
text digest, named by parser id and revision. Knowledge ingestion reads it
through :func:`canonical_preparation`, which re-verifies that identity and
the asset's current state on every call and never re-parses a file whose
preparation exists. The file text route reads through
:func:`read_file_text`, which serves that same preparation for a library
file and parses only a file that has none. There is no second cache next
to these asset fields.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from inqtrix.knowledge.parsing import extraction_facts
from inqtrix.services.file_service import (
    FileParserUnavailable,
    FileTextExtractionError,
)

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal, UserContext
    from inqtrix.content.ports import FileRecord
    from inqtrix.project.asset_records_ports import AssetRecord
    from inqtrix.services.asset_records_service import AssetRecordsService
    from inqtrix.services.file_service import FileService

_IN_FLIGHT = frozenset(
    {"awaiting_upload", "uploading", "retrying", "parsing", "finalizing"}
)
_UNAVAILABLE = "No canonical server extract is available for this file."


class PreparationError(RuntimeError):
    """The canonical text cannot be served; ``code`` and ``status`` are stable."""

    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class CanonicalPreparation:
    """A verified canonical text together with the records that identify it."""

    asset: "AssetRecord"
    file: "FileRecord"

    @property
    def text(self) -> str:
        return self.asset.prepared_text

    @property
    def page_texts(self) -> tuple[str, ...]:
        return self.asset.prepared_page_texts

    def facts(self) -> dict[str, Any]:
        """Parser identity and extraction coverage, as every reader shows them."""
        return extraction_facts(
            parser_id=self.asset.prepared_parser_id or "",
            parser_revision=self.asset.prepared_parser_revision,
            file_sha256=self.file.sha256,
            page_texts=self.page_texts,
        )


async def canonical_preparation(
    asset: "AssetRecord",
    *,
    assets: "AssetRecordsService",
    files: "FileService",
    principal: "Principal",
    visible_to: "UserContext | None",
) -> CanonicalPreparation:
    """Return the verified canonical text of *asset*'s original file.

    The caller has already resolved *asset* under the reader's access. An
    asset created before durable upload operations has an immutable original
    but no preparation; that gap is repaired once from the original bytes
    (a NEW extraction, so it records today's parser revision). A modern
    operation-owned asset never takes that path: its worker stays the sole
    producer.

    Raises:
        PreparationError: pending, failed, unavailable (also while the asset
            is being deleted) or an identity mismatch between the stored text
            and its file -- never a silent re-parse.
    """
    if asset.lifecycle_status in {"deleting", "delete_failed"}:
        raise PreparationError(_UNAVAILABLE, code="source_preparation_unavailable")
    if asset.upload_status in _IN_FLIGHT:
        raise PreparationError(
            "The file is still being prepared on the server.",
            code="source_preparation_pending",
        )
    if asset.upload_status in {"failed", "cancelled"}:
        raise PreparationError(
            asset.upload_error or "Server-side file preparation failed.",
            code="source_preparation_failed",
        )
    if not asset.server_file_id:
        raise PreparationError(
            asset.parse_warning or _UNAVAILABLE, code="source_preparation_unavailable"
        )
    file_record = await files.get(asset.server_file_id, principal=principal)
    if (
        not asset.prepared_text.strip()
        or not asset.prepared_parser_id
        or not asset.prepared_content_hash
        or not asset.prepared_file_sha256
    ):
        if asset.upload_status != "ready" or asset.upload_operation_id is not None:
            raise PreparationError(
                asset.parse_warning or _UNAVAILABLE,
                code="source_preparation_unavailable",
            )
        try:
            extracted = await files.extract_text(
                asset.server_file_id, principal=principal
            )
        except FileParserUnavailable as exc:
            raise PreparationError(
                str(exc), code="source_preparation_unavailable"
            ) from exc
        except FileTextExtractionError as exc:
            raise PreparationError(
                str(exc), code="source_preparation_failed", status=422
            ) from exc
        clean_text = extracted.text.strip()
        asset = await assets.publish_legacy_prepared_text(
            asset.id,
            visible_to=visible_to,
            server_file_id=file_record.id,
            text=clean_text,
            parser_id=extracted.parser_id,
            parser_revision=extracted.parser_revision,
            content_hash=hashlib.sha256(clean_text.encode("utf-8")).hexdigest(),
            file_sha256=file_record.sha256,
            page_texts=list(extracted.page_texts),
            prepared_at=time.time(),
        )
    actual_text_hash = hashlib.sha256(asset.prepared_text.encode("utf-8")).hexdigest()
    if (
        file_record.sha256 != asset.prepared_file_sha256
        or actual_text_hash != asset.prepared_content_hash
    ):
        raise PreparationError(
            "The prepared file extract does not match its stored source identity.",
            code="source_preparation_integrity_error",
        )
    return CanonicalPreparation(asset=asset, file=file_record)


@dataclass(frozen=True)
class FileText:
    """One file's text as a reader receives it, with what identifies it."""

    file: "FileRecord"
    text: str
    representation: str
    facts: dict[str, Any]


async def read_file_text(
    file_id: str,
    *,
    files: "FileService",
    assets: "AssetRecordsService | None",
    principal: "Principal",
    visible_to: "UserContext | None",
) -> FileText:
    """Return the text of one file: the stored preparation when it has one.

    A file bound to a library asset is served from the asset's canonical
    preparation (``prepared_file_text``), verified on this very read. A raw
    upload outside the library has no stored preparation and is parsed here
    (``parsed_file_text``).

    Raises:
        FileNotFound: The reader may not see the file.
        PreparationError: See :func:`canonical_preparation`.
        FileParserUnavailable / FileTextExtractionError / ObjectStoreError:
            Propagated from parsing an unbound file.
    """
    # The access check on the file is the reader's right to its text: an
    # asset and the original it wraps are created together under one owner.
    record = await files.get(file_id, principal=principal)
    asset = (
        await assets.find_asset_by_server_file_id(record.id)
        if assets is not None else None
    )
    if asset is not None:
        preparation = await canonical_preparation(
            asset, assets=assets, files=files,
            principal=principal, visible_to=visible_to,
        )
        return FileText(
            file=record,
            text=preparation.text,
            representation="prepared_file_text",
            facts=preparation.facts(),
        )
    extracted = await files.extract_text(record.id, principal=principal)
    return FileText(
        file=record,
        text=extracted.text,
        representation="parsed_file_text",
        facts=extraction_facts(
            parser_id=extracted.parser_id,
            parser_revision=extracted.parser_revision,
            file_sha256=record.sha256,
            page_texts=extracted.page_texts,
        ),
    )
