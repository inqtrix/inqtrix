"""Per-request ORIGIN facts for the audit trail (ip, user agent, auth).

Separate from :mod:`inqtrix.observability.context` on purpose: the log
context feeds EVERY JSON log line, and client ip / user agent are PII
that belongs in the audit ``origin`` column (365-day retention, admin
access, BSI mapping) — not sprayed across operational logs. Only the
audit writers read these values.

The request-context middleware binds ip + user agent for the lifetime
of the request; the auth dependencies stamp the auth method after
principal resolution. Worker-side audit events simply see an empty
origin — absence is meaningful (no request happened), never fabricated.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_ORIGIN_IP: ContextVar[str] = ContextVar("inqtrix_origin_ip", default="")
_ORIGIN_USER_AGENT: ContextVar[str] = ContextVar(
    "inqtrix_origin_user_agent", default=""
)
_ORIGIN_AUTH_METHOD: ContextVar[str] = ContextVar(
    "inqtrix_origin_auth_method", default=""
)

# Conservative caps: header values are attacker-controlled input and the
# origin column is long-lived storage.
_USER_AGENT_MAX_CHARS = 256


def bind_request_origin(
    *, ip: str = "", user_agent: str = ""
) -> tuple[Token, ...]:
    """Bind transport facts for this request; returns reset tokens."""
    return (
        _ORIGIN_IP.set(str(ip or "")),
        _ORIGIN_USER_AGENT.set(str(user_agent or "")[:_USER_AGENT_MAX_CHARS]),
        _ORIGIN_AUTH_METHOD.set(""),
    )


def reset_request_origin(tokens: tuple[Token, ...]) -> None:
    for token, var in zip(
        tokens, (_ORIGIN_IP, _ORIGIN_USER_AGENT, _ORIGIN_AUTH_METHOD)
    ):
        try:
            var.reset(token)
        except ValueError:
            # Token from another context (task hop) — clearing keeps the
            # invariant that nothing leaks into the next request.
            var.set("")


def bind_auth_method(principal: Any) -> None:
    """Stamp how this request authenticated (after principal resolution).

    Uses the principal ``kind`` verbatim (``oidc_session`` | ``pat`` |
    ``static`` | ...); guest surfaces pass their own marker explicitly.
    """
    kind = str(getattr(principal, "kind", "") or "")
    if kind:
        _ORIGIN_AUTH_METHOD.set(kind)


def current_request_origin() -> dict[str, str]:
    """Non-empty origin facts of the current request (may be ``{}``)."""
    origin: dict[str, str] = {}
    ip = _ORIGIN_IP.get()
    if ip:
        origin["ip"] = ip
    user_agent = _ORIGIN_USER_AGENT.get()
    if user_agent:
        origin["user_agent"] = user_agent
    auth_method = _ORIGIN_AUTH_METHOD.get()
    if auth_method:
        origin["auth_method"] = auth_method
    return origin
