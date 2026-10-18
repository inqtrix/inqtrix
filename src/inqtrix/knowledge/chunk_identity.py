"""Stable identities for canonical knowledge chunks and vector points.

Chunk metadata in Postgres and its derived vector point share one identifier.
The identifier is a pure function of the immutable build coordinates, so a
worker retry overwrites the same point instead of leaking another physical
copy. Generation and revision are both part of the identity: publishing a
new source revision or building a shadow generation can therefore never
overwrite evidence that is still active.
"""

from __future__ import annotations

import hashlib


def deterministic_chunk_id(
    *,
    document_id: str,
    generation_id: str | None,
    revision_id: str | None,
    content_hash: str | None,
    chunk_index: int,
) -> str:
    """Return the stable public id for one immutable chunk-build coordinate."""

    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    parts = (
        document_id,
        generation_id or "legacy-generation",
        revision_id or "legacy-revision",
        content_hash or "legacy-content",
        str(chunk_index),
    )
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"kch_{digest.hexdigest()[:40]}"
