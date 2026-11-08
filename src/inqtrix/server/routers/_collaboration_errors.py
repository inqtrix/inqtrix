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
run.
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
from inqtrix.project.editor_ports import DocumentNotFound
from inqtrix.services.collaboration_client import (
    CollaborationNodeConflict,
    CollaborationServiceUnavailable,
)
from inqtrix.services.editor_collaboration_service import (
    CollaborationAuthenticationRequired,
    CollaborationDocumentTooLarge,
    CollaborationProtocolConflict,
)


@dataclass(frozen=True, slots=True)
class CollaborationErrorView:
    """Status, message, error type and extra fields of one public answer."""

    status: int
    message: str
    error_type: str
    extra: dict[str, Any] = field(default_factory=dict)


def collaboration_error_view(exc: Exception) -> CollaborationErrorView | None:
    """Map a collaboration failure to its public answer, or ``None``.

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
        # Nur wo der Server einen konkreten Stand nennen kann, ist die
        # Aussage "nicht mehr aktuell" wahr. Fuer jeden anderen Konflikt
        # benennt die Meldung den Vorgang, ohne eine Ursache zu erfinden:
        # eine falsche Begruendung schickt den Nutzer in eine Wiederholung,
        # die nie gelingen kann, und verdeckt den echten Grund.
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
    if isinstance(exc, ValueError):
        return CollaborationErrorView(400, str(exc), "invalid_request_error")
    return None
