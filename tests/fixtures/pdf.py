"""A minimal PDF builder for tests that need real page structure.

``build_pdf(["first", None, "third"])`` yields a three-page PDF whose second
page has no text layer: it only paints a grey box, the way a scanned page
carries an image and nothing a text extractor can read. The file is plain
PDF 1.4 with a correct cross-reference table, so pdfminer and MarkItDown
parse it like any other document.
"""

from __future__ import annotations

_WIN_ANSI = "cp1252"


def _text_stream(text: str) -> bytes:
    lines = text.split("\n")
    body = [b"BT /F1 12 Tf 14 TL 72 720 Td"]
    for line in lines:
        escaped = (
            line.encode(_WIN_ANSI)
            .replace(b"\\", b"\\\\")
            .replace(b"(", b"\\(")
            .replace(b")", b"\\)")
        )
        body.append(b"(" + escaped + b") Tj T*")
    body.append(b"ET")
    return b"\n".join(body)


_SCAN_STREAM = b"0.85 g 72 400 451 300 re f"


def build_pdf(pages: list[str | None]) -> bytes:
    """Return PDF bytes with one page per entry; ``None`` is a page without text."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the page tree is filled in once the page object numbers exist
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    kids: list[int] = []
    for text in pages:
        stream = _SCAN_STREAM if text is None else _text_stream(text)
        page_number = len(objects) + 1
        kids.append(page_number)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> "
            + f"/Contents {page_number + 1} 0 R >>".encode("ascii")
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
    objects[1] = (
        "<< /Type /Pages /Kids [{}] /Count {} >>".format(
            " ".join(f"{number} 0 R" for number in kids), len(kids)
        )
    ).encode("ascii")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)
