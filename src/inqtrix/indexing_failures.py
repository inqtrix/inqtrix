"""Failure typing for resumable knowledge-indexing dependencies.

Only failures whose exception contract proves a transient provider or vector
transport condition may park a job as ``paused_dependency``.  Validation
errors, malformed responses, bad requests, and ordinary programming errors
remain failures so a resumable state never hides a deterministic defect.
"""

from __future__ import annotations

from inqtrix.knowledge.contextualize import ContextualizationDependencyError
from inqtrix.provider_failure_contract import (
    ProviderFailureKind,
    classify_provider_failure,
    exception_chain,
)
from inqtrix.providers.embeddings import EmbeddingProviderError


class IndexingDependencyError(RuntimeError):
    """A safely classified dependency outage that retains resumable work."""

    def __init__(self, message: str, *, error_type: str) -> None:
        self.error_type = error_type
        super().__init__(message)


def _transient_kind(exc: BaseException) -> str | None:
    """Return a transient class only from stable type/status attributes."""

    classification = classify_provider_failure(exc)
    if not classification.transient:
        return None
    return {
        ProviderFailureKind.TIMEOUT: "timeout",
        ProviderFailureKind.RATE_LIMITED: "rate_limited",
        ProviderFailureKind.UNAVAILABLE: "unavailable",
    }[classification.kind]


def _is_qdrant_contract(exc: BaseException) -> bool:
    """Whether a Qdrant SDK wrapper is present in the exception chain."""

    return any(
        type(current).__module__.startswith(("qdrant_client.", "grpc."))
        for current in exception_chain(exc)
    )


def dependency_error_from_exception(
    exc: BaseException,
    *,
    vector_surface: bool = False,
) -> IndexingDependencyError | None:
    """Translate only proven transient embedding/vector failures.

    ``EmbeddingProviderError`` is intentionally not sufficient by itself: the
    same provider contract also reports deterministic vector-count validation
    failures.  A transient cause or status must be present.  Likewise, raw
    HTTP transport failures are accepted only while executing a known vector
    surface; otherwise an unrelated HTTP programming error could be parked.
    """

    if isinstance(exc, IndexingDependencyError):
        return exc
    if isinstance(exc, ContextualizationDependencyError):
        # Contextualization owns its own resumable dependency vocabulary.
        # Its HTTP cause does not prove that the vector store failed.
        return None
    if isinstance(exc, EmbeddingProviderError):
        kind = _transient_kind(exc)
        if kind is None:
            return None
        return IndexingDependencyError(
            "The embedding provider is temporarily unavailable. The "
            "unpublished generation is kept and can be resumed.",
            error_type=f"embedding_provider_{kind}",
        )

    vector_contract = _is_qdrant_contract(exc)
    if not vector_contract and vector_surface:
        vector_contract = type(exc).__module__.startswith(("httpx", "httpcore"))
    if not vector_contract:
        return None
    kind = _transient_kind(exc)
    if kind is None:
        return None
    return IndexingDependencyError(
        "The vector store is temporarily unavailable. The unpublished "
        "generation is kept and can be resumed.",
        error_type=f"vector_store_{kind}",
    )
