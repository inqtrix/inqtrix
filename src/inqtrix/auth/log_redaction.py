"""Privacy-safe logging helpers for authorization decisions.

The durable audit trail is the authoritative place for actor and resource
identities.  Ordinary application logs need enough context to correlate a
burst of denials, but must not duplicate those raw identifiers.

Two key tiers back the pseudonymous references:

* the INSTANCE tier — installed via :func:`configure_stable_pseudonyms`
  from the ``INQTRIX_PSEUDONYM_PEPPER`` secret that the API server and
  every worker share.  References are then stable across processes and
  restarts, so one subject carries ONE pseudonym through logs, traces,
  and audit correlation (deterministic pseudonymisation within one
  domain — the property an audit trail needs).
* the PROCESS tier — a random per-process HMAC key.  The fallback when
  no pepper is configured: deterministic only for the lifetime of one
  process, the historical behaviour.

In both tiers references are domain-separated by identifier kind and
protected by a keyed MAC, so low-entropy resource identifiers cannot be
recovered with an offline dictionary. Hash only canonical high-entropy
identifiers such as UUIDs or cryptographically random session ids, never
e-mails or usernames: the input domain must stay high-entropy even if
the pepper leaks.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from typing import Final

log = logging.getLogger("inqtrix")

_PROCESS_LOG_KEY: Final = secrets.token_bytes(32)
_SAFE_LABEL: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REFERENCE_HEX_LENGTH: Final = 16

# Domain-separation label: the instance key is derived from the pepper
# under this constant, so another consumer of the same pepper can never
# produce colliding MACs by accident.
_STABLE_KEY_DOMAIN: Final = b"inqtrix.pseudonym.v1"

# Instance-wide key derived from INQTRIX_PSEUDONYM_PEPPER; None keeps the
# process-local fallback. Module state because the reference helpers are
# called from contexts that have no Settings handle.
_stable_key: bytes | None = None
_fallback_warned: bool = False


def configure_stable_pseudonyms(pepper: str | None) -> bool:
    """Install (or clear) the instance-wide pseudonym key.

    Called once per process during bootstrap (``create_app``, the worker
    ``main``) with ``settings.auth.pseudonym_pepper``. An empty pepper
    keeps the process-local fallback and logs ONE warning so operators
    learn why pseudonyms do not correlate across processes or restarts.

    Args:
        pepper: The shared instance secret; empty/None selects the
            process-local fallback.

    Returns:
        True when the instance key is active, False on fallback.
    """
    global _stable_key, _fallback_warned
    cleaned = (pepper or "").strip()
    if not cleaned:
        _stable_key = None
        if not _fallback_warned:
            _fallback_warned = True
            log.warning(
                "INQTRIX_PSEUDONYM_PEPPER is unset; log pseudonyms are stable "
                "only within this process. API, worker and restarted processes "
                "produce different references for the same person."
            )
        return False
    _stable_key = hmac.new(
        cleaned.encode("utf-8"), _STABLE_KEY_DOMAIN, hashlib.sha256
    ).digest()
    _fallback_warned = False
    return True


def stable_pseudonyms_active() -> bool:
    """Whether references are instance-stable (a pepper is configured)."""
    return _stable_key is not None


def _reference(key: bytes, namespace: str, identifier: object | None) -> str:
    """MAC-based reference shared by both key tiers."""
    safe_namespace = _safe_label(namespace)
    if identifier is None or str(identifier) == "":
        return "none"
    message = f"{safe_namespace}\0{identifier}".encode(
        "utf-8", errors="surrogatepass"
    )
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()[
        :_REFERENCE_HEX_LENGTH
    ]
    return f"{safe_namespace}_{digest}"


def pseudonymous_log_reference(
    namespace: str,
    identifier: object | None,
) -> str:
    """Return a correlation reference for *identifier*.

    Uses the instance key when :func:`configure_stable_pseudonyms`
    installed one (same person == same reference in every process), and
    the process-local key otherwise (the historical behaviour).

    ``none`` is explicit so a missing actor cannot be confused with a real
    pseudonym.  The namespace is validated rather than escaped to prevent
    caller-controlled labels from forging log fields.
    """
    return _reference(
        _stable_key if _stable_key is not None else _PROCESS_LOG_KEY,
        namespace,
        identifier,
    )


def stable_pseudonym(namespace: str, identifier: object | None) -> str:
    """Return the instance-stable pseudonym for *identifier*.

    The reference for durable correlation surfaces (trace attributes,
    audit columns, admin resolution). Identical to
    :func:`pseudonymous_log_reference` while a pepper is configured;
    without one it falls back to the process-local key — callers that
    NEED stability should gate on :func:`stable_pseudonyms_active`.

    Only hash canonical high-entropy identifiers here, never e-mails or
    usernames.
    """
    return pseudonymous_log_reference(namespace, identifier)


def log_authorization_denial(
    logger: logging.Logger,
    *,
    action: str,
    principal_kind: object | None,
    actor_user_id: object | None,
    tenant_id: object | None,
    resource_type: str,
    resource_id: object | None,
) -> None:
    """Log one denial without copying identity or policy detail into logs.

    Full actor, tenant, resource, recipient, permission, and reason data
    belongs in the audit sink.  This operational signal deliberately exposes
    only bounded categorical labels and process-local correlation references.
    """
    logger.warning(
        "authz denied: action=%s kind=%s actor_ref=%s tenant_ref=%s "
        "resource_type=%s resource_ref=%s",
        _safe_label(action),
        _safe_label(principal_kind),
        pseudonymous_log_reference("usr", actor_user_id),
        pseudonymous_log_reference("ten", tenant_id),
        _safe_label(resource_type),
        pseudonymous_log_reference("res", resource_id),
    )


def _safe_label(value: object | None) -> str:
    candidate = str(value or "").lower()
    return candidate if _SAFE_LABEL.fullmatch(candidate) else "unknown"
