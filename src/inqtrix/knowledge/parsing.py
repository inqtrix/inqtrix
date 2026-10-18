"""Document parsing behind a Baukasten port (file-to-text for ingestion).

The parser ladder mirrors the auth and object-store ports: standards first,
nothing hardwired.

* :class:`MarkItDownParser` — the DEFAULT. Pure-Python conversion to
  Markdown (Microsoft MarkItDown, MIT): no ML models, no cloud
  dependency, always available. Handles PDF (text layer), DOCX, PPTX,
  XLSX, HTML, Markdown, plain text.
* Azure Document Intelligence — the optional cloud tier for scans and
  complex table layouts; a drop-in behind this port when a deployment
  configures it (not built until needed).
* Docling — the documented air-gapped option (local layout models);
  deliberately NOT the default because it hosts model weights.

Parsing failures are loud: a file that cannot be converted raises
:class:`DocumentParseError`, mapped to a clear client error — never a
silently empty document.
"""

from __future__ import annotations

import io
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Iterable

from inqtrix.knowledge.page_mapping import extract_pdf_page_texts

log = logging.getLogger("inqtrix")


class DocumentParseError(RuntimeError):
    """Raised when a file cannot be converted to text."""


class DocumentParser(ABC):
    """Port for converting an uploaded file into ingestion text."""

    @property
    @abstractmethod
    def parser_id(self) -> str:
        """Stable identifier recorded in document metadata."""

    @property
    def parser_revision(self) -> str:
        """Revision of the implementation that produces the text.

        Recorded with every new extraction, so a stored text names what made
        it. Empty means unknown: a reader then says "unknown" instead of
        assuming the parser that happens to be installed today.
        """
        return ""

    @abstractmethod
    def parse(self, *, file_name: str, content: bytes) -> str:
        """Convert *content* to text (Markdown preferred).

        Args:
            file_name: Original filename — the extension steers format
                detection.
            content: Raw file bytes.

        Raises:
            DocumentParseError: When conversion fails or produces no
                text (scanned PDFs without a text layer being the
                classic case — the error says so).
        """


class MarkItDownParser(DocumentParser):
    """Default parser: MarkItDown file-to-Markdown conversion.

    Construction imports the converter stack once; per-call work is
    pure CPU parsing with no network and no model inference.
    """

    def __init__(self) -> None:
        from markitdown import MarkItDown

        # Plugins stay off: deterministic, dependency-free conversion.
        self._converter = MarkItDown(enable_plugins=False)

    @property
    def parser_id(self) -> str:
        """``"markitdown"``."""
        return "markitdown"

    @property
    def parser_revision(self) -> str:
        """The installed converter distribution, e.g. ``"0.1.3"``."""
        try:
            return metadata.version("markitdown")
        except metadata.PackageNotFoundError:  # pragma: no cover - import above proves presence
            return ""

    def parse(self, *, file_name: str, content: bytes) -> str:
        """Convert the file to Markdown text."""
        stream = io.BytesIO(content)
        try:
            result = self._converter.convert_stream(
                stream, file_extension=_extension_of(file_name)
            )
        except Exception as exc:  # noqa: BLE001 — normalized below, visibly
            log.warning(
                "File parsing failed (error_type=%s)",
                type(exc).__name__,
            )
            raise DocumentParseError(
                f'File {file_name!r} could not be converted: {exc}'
            ) from exc
        text = (result.text_content or "").strip()
        if not text:
            raise DocumentParseError(
                f'File {file_name!r} produced no text (possibly a scanned PDF without a text layer)'
            )
        return text


def _extension_of(file_name: str) -> str:
    """Dot-prefixed lowercase extension (MarkItDown's detection hint)."""
    dot = file_name.rfind(".")
    return file_name[dot:].lower() if dot >= 0 else ""


def extraction_coverage(page_texts: Iterable[str] | None) -> dict[str, Any]:
    """What a paged extraction did NOT capture; empty when pages are unknown.

    A page whose text layer is empty (a scan, a full-page image) is part of
    the source but not of the extracted text. Readers show these page numbers
    so a text-only extraction never passes for the whole document.
    """
    pages = list(page_texts or ())
    if not pages:
        return {}
    return {
        "pages": len(pages),
        "pages_without_text": [
            number for number, text in enumerate(pages, 1) if not text.strip()
        ],
    }


def extraction_facts(
    *, parser_id: str, parser_revision: str | None, file_sha256: str,
    page_texts: Iterable[str] | None,
) -> dict[str, Any]:
    """Identity and coverage of a text derived from a file, for every reader."""
    return {
        "parser_id": parser_id,
        "parser_revision": parser_revision or None,
        "file_sha256": file_sha256,
        **extraction_coverage(page_texts),
    }


@dataclass(frozen=True)
class FileExtraction:
    """The one product of turning file bytes into text."""

    text: str
    parser_id: str
    parser_revision: str
    page_texts: tuple[str, ...] = ()


def extract_file(
    parser: DocumentParser, *, file_name: str, content: bytes
) -> FileExtraction:
    """Parse *content* once: text, the parser's identity and the page pass.

    The single producer behind file reading, upload preparation and direct
    knowledge ingestion, so no second parse path can drift from it.
    Blocking; callers run it off the event loop.
    """
    return FileExtraction(
        text=parser.parse(file_name=file_name, content=content),
        parser_id=parser.parser_id,
        parser_revision=parser.parser_revision,
        page_texts=tuple(extract_pdf_page_texts(content) or ()),
    )
