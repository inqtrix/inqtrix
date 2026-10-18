"""Per-surface router factories for the Inqtrix HTTP server.

Each module exposes ``build_router(container) -> APIRouter`` and stays
thin: parse the request, delegate to a service, serialize the result.
The shared :class:`~inqtrix.server.container.AppContainer` carries the
collaborators; FastAPI's ``Depends`` is used only for the principal
dependency so every gated endpoint resolves a
:class:`~inqtrix.auth.principal.Principal` uniformly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from inqtrix.quota.models import QuotaDimension, QuotaExceeded
from inqtrix.server.metrics import record_admission_rejected
from inqtrix.services.agent_context import StackResolutionError

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal


async def quota_admission(
    quota_service: Any,
    principal: "Principal",
    dimension: QuotaDimension,
    amount: int = 1,
) -> JSONResponse | None:
    """Admit *amount* of one quota dimension before its cost is paid.

    Returns a 429 :class:`JSONResponse` (the quota envelope) when the
    principal's effective limit for *dimension* would be crossed, else
    ``None``. A no-op (``None``) when quotas are unwired — the service
    is ``None`` for disabled or non-oidc deployments. This is the ONE
    guard site so the cost routers never branch on the service's
    presence themselves.
    """
    if quota_service is None:
        return None
    try:
        await quota_service.check(principal, dimension, amount)
    except QuotaExceeded as exc:
        return quota_error_response(exc)
    return None


async def quota_record(
    quota_service: Any,
    principal: "Principal",
    dimension: QuotaDimension,
    amount: int,
) -> None:
    """Book *amount* of one quota dimension after the cost is known.

    No-op when quotas are unwired (service ``None``); the service
    itself short-circuits exempt principals and zero amounts. Paired
    with :func:`quota_admission` so recording mirrors admission at one
    site.
    """
    if quota_service is not None:
        await quota_service.record(principal, dimension, amount)


async def quota_record_for_subject(
    quota_service: Any,
    subject: Any,
    dimension: QuotaDimension,
    amount: int,
) -> None:
    """Book *amount* against an EXPLICIT subject (stock attribution).

    Stored bytes belong to a file's owner, so an upload charges and a
    delete frees the owner's counter regardless of who acts. No-op when
    quotas are unwired; the service short-circuits a missing subject or
    zero amount.
    """
    if quota_service is not None:
        await quota_service.record_for_subject(subject, dimension, amount)


def quota_error_response(exc) -> JSONResponse:
    """Map a :class:`~inqtrix.quota.models.QuotaExceeded` to HTTP 429.

    The envelope names the dimension, the limit, the current usage, and
    the reset timestamp so the UI can render a precise, actionable
    message (never a silent block). Also the single quota-rejection
    chokepoint, so every surface's quota 429 is counted once for
    ``/metrics`` here (no-op when metrics are off).
    """
    record_admission_rejected("quota")
    return JSONResponse(
        status_code=429,
        content={"error": {
            "message": "Kontingent aufgebraucht",
            "type": "quota_exceeded",
            "dimension": exc.dimension.value,
            "limit": exc.limit,
            "used": exc.used,
            "reset_at": exc.reset_at,
        }},
    )


def stack_error_response(exc: StackResolutionError) -> JSONResponse:
    """Map a stack-resolution failure to the historical 400 envelope."""
    content: dict = {"error": {
        "message": exc.message,
        "type": "invalid_request_error",
    }}
    if exc.available:
        content["error"]["available_stacks"] = exc.available
    return JSONResponse(status_code=400, content=content)
