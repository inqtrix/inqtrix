"""An excerpt is checked against its document body, which is hashed once per batch.

The check needs the UTF-8 bytes of the whole body and their hash. Done per
excerpt, a document with hundreds of chunks was encoded and hashed hundreds of
times: about half a second for 1.5 MB, on the event loop for one citation.
"""

from __future__ import annotations

import hashlib

import pytest

from inqtrix.knowledge import evidence
from inqtrix.knowledge.evidence import CanonicalBodies

BODY = "Die Haftung ist auf Vorsatz beschränkt. " * 200
DIGEST = hashlib.sha256(BODY.encode("utf-8")).hexdigest()


def _span(text: str, start: int, end: int) -> tuple[str, int, int]:
    """An excerpt with the byte offsets the chunker stores."""
    return text[start:end], len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8"))


def test_many_excerpts_of_one_body_hash_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    hashed: list[int] = []
    real = hashlib.sha256

    def counting(data: bytes = b"") -> object:
        hashed.append(len(data))
        return real(data)

    monkeypatch.setattr(evidence.hashlib, "sha256", counting)
    bodies = CanonicalBodies()

    for start in range(0, 4000, 40):
        excerpt, byte_start, byte_end = _span(BODY, start, start + 40)
        assert bodies.excerpt_is_verified(
            body_key=("document", "revision"),
            canonical_text=BODY,
            source_text=excerpt,
            source_start=byte_start,
            source_end=byte_end,
            document_content_hash=DIGEST,
        )

    assert hashed == [len(BODY.encode("utf-8"))]


@pytest.mark.parametrize(
    ("source_text", "content_hash"),
    [
        ("Die Haftung ist auf Vorsatz beschränkt. ", "0" * 64),
        ("Die Haftung ist auf Fahrlässigkeit beschr", DIGEST),
    ],
    ids=["other_revision", "other_text"],
)
def test_an_excerpt_of_another_body_is_not_verified(source_text: str, content_hash: str) -> None:
    excerpt, byte_start, byte_end = _span(BODY, 0, len(source_text))

    assert not CanonicalBodies().excerpt_is_verified(
        body_key="document",
        canonical_text=BODY,
        source_text=source_text,
        source_start=byte_start,
        source_end=byte_end,
        document_content_hash=content_hash,
    )
