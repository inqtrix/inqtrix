"""One public answer per live-collaboration failure, for every route.

The signed-in collaboration routes and the guest-link routes call the same
collaboration service, so they meet the same failures: an invalid lease, a
rate limit, an unavailable or fenced service, a conflict, a document that is
too large or gone. This module maps each failure once to its status, message
and machine-readable reason. Each router renders the mapping in its own
envelope; the guest router adds its no-store headers and keeps its own
answers for guest-link failures in front of it.

A failure mapped by only one router would answer the other with an unhandled
500, where the browser's lease recovery, which decides on the status, cannot
run. The patch decision routes answer their own failures first and the named
collaboration failures through :func:`collaboration_failure_view`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from inqtrix.project.editor_collaboration_ports import (
    CollaborationConflict,
    CollaborationDocumentNotFound,
    CollaborationInstanceFenced,
    CollaborationLeaseInvalid,
    CollaborationRateLimited,
)
from inqtrix.project.editor_ports import (
    CONTENT_MODE_CHANGED_TEXT,
    DocumentContentModeConflict,
    DocumentNotFound,
)
from inqtrix.services.collaboration_client import (
    CollaborationNodeConflict,
    CollaborationServiceUnavailable,
)
from inqtrix.services.editor_collaboration_service import (
    CollaborationAuthenticationRequired,
    CollaborationDocumentTooLarge,
    CollaborationProtocolConflict,
)
from inqtrix.services.editor_persistence_service import (
    COLLABORATION_PROJECTION_UNAVAILABLE_TEXT,
    CollaborationProjectionUnavailable,
)


@dataclass(frozen=True, slots=True)
class CollaborationErrorView:
    """Status, message, error type and extra fields of one public answer."""

    status: int
    message: str
    error_type: str
    extra: dict[str, Any] = field(default_factory=dict)


def collaboration_error_view(exc: Exception) -> CollaborationErrorView | None:
    """Map a collaboration failure or a rejected request value, or ``None``.

    The named failures answer as :func:`collaboration_failure_view` maps
    them. Every other ``ValueError`` answers 400 with its own text: the
    collaboration and guest routes raise one for a rejected request value
    inside the same ``try`` as the service call. ``None`` means the
    exception is neither; the caller re-raises it.
    """
    view = collaboration_failure_view(exc)
    if view is not None:
        return view
    if isinstance(exc, ValueError):
        return CollaborationErrorView(400, str(exc), "invalid_request_error")
    return None


def collaboration_failure_view(exc: Exception) -> CollaborationErrorView | None:
    """Map a named collaboration failure to its public answer, or ``None``.

    Only the failure types below count, never a bare ``ValueError``: on a
    route that validates no request value inside its ``try``, a
    ``ValueError`` is an inner error, such as a collaboration-service reply
    that failed its shape check, and must not become a 400 with inner text.
    ``None`` means the exception is no collaboration failure this module
    knows; the caller re-raises it.
    """
    if isinstance(exc, (DocumentNotFound, CollaborationDocumentNotFound)):
        return CollaborationErrorView(404, "Document not found", "not_found")
    if isinstance(exc, CollaborationAuthenticationRequired):
        return CollaborationErrorView(
            403,
            "Live collaboration requires an active browser session.",
            "forbidden",
            {"reason": "cookie_session_required"},
        )
    if isinstance(exc, CollaborationDocumentTooLarge):
        return CollaborationErrorView(
            413,
            "The document is too large for live collaboration.",
            "invalid_request_error",
            {"reason": "document_too_large"},
        )
    if isinstance(exc, CollaborationRateLimited):
        return CollaborationErrorView(
            429,
            "Too many collaboration sessions or session requests.",
            "rate_limit_error",
            {"reason": exc.reason},
        )
    if isinstance(exc, CollaborationServiceUnavailable):
        return CollaborationErrorView(
            503,
            "Live collaboration is currently unavailable.",
            "service_unavailable",
            {"reason": "collaboration_unavailable"},
        )
    if isinstance(exc, CollaborationProjectionUnavailable):
        # The wording of the editor capabilities, whose reads meet the same
        # barrier.
        return CollaborationErrorView(
            503,
            COLLABORATION_PROJECTION_UNAVAILABLE_TEXT,
            "service_unavailable",
            {"reason": "collaboration_projection_unavailable"},
        )
    if isinstance(exc, DocumentContentModeConflict):
        # The answer the Markdown save route gives the same conflict.
        return CollaborationErrorView(
            409,
            CONTENT_MODE_CHANGED_TEXT,
            "conflict",
            {"reason": "content_mode_changed"},
        )
    if isinstance(
        exc,
        (
            CollaborationConflict,
            CollaborationProtocolConflict,
            CollaborationNodeConflict,
        ),
    ):
        reason = getattr(exc, "reason", str(exc))
        current_sequence = getattr(exc, "current_sequence", None)
        # Only where the server can name a concrete state is "no longer
        # current" true. Every other conflict names what happened without
        # inventing a cause: a wrong cause sends the user into a retry that
        # can never succeed, and it hides the real reason.
        message = (
            "The collaboration state is no longer current."
            if current_sequence is not None
            else f'The change was rejected (reason: {reason}).'
        )
        return CollaborationErrorView(
            409,
            message,
            "conflict",
            {"reason": reason, "current_sequence": current_sequence},
        )
    if isinstance(exc, CollaborationLeaseInvalid):
        if exc.reason in {"access_revoked", "permission_denied"}:
            return CollaborationErrorView(
                403,
                "Collaboration access was revoked.",
                "forbidden",
                {"reason": exc.reason},
            )
        return CollaborationErrorView(
            401,
            "The collaboration session is no longer valid.",
            "authentication_error",
            {"reason": exc.reason},
        )
    if isinstance(exc, CollaborationInstanceFenced):
        return CollaborationErrorView(
            503,
            "Live collaboration is not ready.",
            "service_unavailable",
            {"reason": str(exc)},
        )
    return None
