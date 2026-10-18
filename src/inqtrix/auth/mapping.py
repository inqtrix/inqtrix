"""IdP-agnostic claim mapping for the OIDC BFF.

One generalist helper, not a per-provider zoo: enterprise identity
providers differ only in WHERE a claim sits and HOW its value is shaped,
never in the mapping algorithm. Those two axes are absorbed by
configuration — a dot-path (Keycloak ``realm_access.roles``), and a
value normalisation (a JSON array, a separator-joined string, or a
leading-slash group path like ``/Engineering/Backend``) — and the result
is matched literally against operator-configured allow/admin lists.
Provider names (Entra, Okta, Keycloak) live only in the docs cheat sheet,
never in a code branch: no per-provider special cases and one shared
mapping for every provider.

Distributed/aggregated claims (OIDC Core 5.6.2 — ``_claim_names`` plus
``_claim_sources``; the common case is Microsoft Entra's groups
"overage", which replaces an inline ``groups`` array with a pointer to a
Graph endpoint once a user exceeds the group limit) cannot be resolved
from the token alone. Rather than silently treating the user as
claim-less — a security fallback — the mapper makes the condition
VISIBLE instead of falling back silently: it fails loud when the
claim gates admission, and logs a warning when the claim only elevates
(an unresolved role claim degrades to "no elevation", never to a 500 or
a silent admit).

This module is the lowest auth layer: it has no dependency on
:mod:`inqtrix.auth.oidc`, so the shared primitives :func:`claim_path` and
:class:`OidcExchangeError` are defined here and re-exported there for
backward compatibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

log = logging.getLogger("inqtrix")

DEFAULT_CLAIM_SEPARATORS = " ,"
"""Characters a string-valued claim is split on by default: whitespace
AND comma. Matches the Open WebUI ``OAUTH_ROLES_SEPARATOR`` convention so
a claim delivered as ``"admin, staff"`` or ``"admin staff"`` both parse."""


class OidcExchangeError(RuntimeError):
    """Raised when the IdP interaction or claim mapping fails.

    Carries a user-facing German message (the HTTP-status-string
    exception in :mod:`conventions`); the callback surfaces it as a 403.
    Defined here (not in :mod:`inqtrix.auth.oidc`) so the claim mapper can
    raise it without importing the higher OIDC layer; re-exported from
    ``oidc`` for the historical import path.
    """


class DistributedClaimError(OidcExchangeError):
    """An admission-gating claim is delivered as a distributed claim.

    A precise subtype so callers/tests can distinguish "the IdP did not
    put the value in the token" from other exchange failures. Only raised
    when the claim actually gates admission; a merely elevating claim
    degrades to a logged warning instead (see :func:`extract_roles`).
    """


def claim_path(claims: dict[str, Any], path: str) -> Any:
    """Resolve a dot-separated claim path (``realm_access.roles``).

    Args:
        claims: The decoded id_token / userinfo claim set.
        path: A dot-separated path; each segment indexes one nesting
            level. Keycloak roles live at ``realm_access.roles`` or
            ``resource_access.<client>.roles``, never at the root.

    Returns:
        The value at *path*, or ``None`` if any segment is missing or a
        non-object is encountered mid-path.
    """
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


@dataclass(frozen=True)
class ClaimMappingConfig:
    """The full, IdP-agnostic claim-mapping contract for one provider.

    The single source of truth for how an OIDC login's claims become
    Inqtrix identity facts. Every field is operator configuration fed
    through the settings bridge (Constructor-First); the mapper code
    never special-cases a provider.

    Attributes:
        username_claim: Display-name claim (dot-path capable), falling
            back to email then sub.
        email_claim: Email claim (dot-path capable).
        groups_claim: Group-membership claim (dot-path capable). Values
            are opaque identifiers — Entra emits GUIDs, Keycloak emits
            ``/path`` strings — matched literally against the lists below.
        roles_claim: Role claim (dot-path capable). May coincide with
            ``groups_claim`` for tenants that emit groups-as-roles; the
            two are independent paths that are allowed to be equal.
        separators: Characters a STRING-valued claim is split on; a
            JSON array is used as-is. Default whitespace + comma.
        strip_group_path_prefix: Strip a single leading ``/`` from each
            group value (Keycloak full-path groups). Explicit and
            documented rather than a silent normalisation.
        allowed_groups: Non-empty set gates admission on group overlap; a
            ``"*"`` member admits any authenticated user.
        admin_groups: Group values that grant instance-admin (grant-only).
        admin_roles: Role values that grant instance-admin (grant-only).
        allowed_domains: Non-empty set gates admission on the email
            domain (lower-cased), orthogonal to the group gate.
    """

    username_claim: str = "preferred_username"
    email_claim: str = "email"
    groups_claim: str = "groups"
    roles_claim: str = "roles"
    separators: str = DEFAULT_CLAIM_SEPARATORS
    strip_group_path_prefix: bool = False
    allowed_groups: frozenset[str] = frozenset()
    admin_groups: frozenset[str] = frozenset()
    admin_roles: frozenset[str] = frozenset()
    allowed_domains: frozenset[str] = frozenset()


def _is_distributed(claims: dict[str, Any], path: str) -> bool:
    """Whether *path* is delivered as an UNRESOLVED distributed claim.

    OIDC Core 5.6.2: a distributed claim's name is listed in
    ``_claim_names`` and its body pointer in ``_claim_sources``; the value
    is not inline. Entra's groups overage is the common case.

    The pointer can survive a userinfo merge even after the value itself
    has been resolved inline (the merge keeps every non-null id_token key,
    including ``_claim_names``). A present value must win over a stale
    pointer — otherwise a valid login fails closed — so the claim counts
    as distributed only when it is declared AND not resolvable inline.
    """
    names = claims.get("_claim_names")
    if not isinstance(names, dict) or path.split(".", 1)[0] not in names:
        return False
    return claim_path(claims, path) is None


def _split_string(value: str, separators: str) -> list[str]:
    """Split on any character in *separators* (folded to the first)."""
    if not separators:
        return [value]
    primary = separators[0]
    for sep in separators[1:]:
        value = value.replace(sep, primary)
    return value.split(primary)


def normalise_claim_values(
    value: Any, *, separators: str, strip_path_prefix: bool
) -> tuple[str, ...]:
    """Normalise a resolved claim value to a tuple of clean tokens.

    A JSON array is used element-wise; a single string is split on
    *separators*; ``None`` and objects (the path should have descended
    into them) yield no tokens; any other scalar becomes its ``str``.
    Empty tokens are dropped and, when *strip_path_prefix* is set, a
    single leading ``/`` is removed.
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        raw: Iterable[str] = [str(item) for item in value]
    elif isinstance(value, str):
        raw = _split_string(value, separators)
    elif isinstance(value, dict):
        return ()
    else:
        raw = [str(value)]
    tokens: list[str] = []
    for item in raw:
        token = item.strip()
        if strip_path_prefix and token.startswith("/"):
            token = token[1:]
        if token:
            tokens.append(token)
    return tuple(tokens)


def _extract_list(
    claims: dict[str, Any],
    path: str,
    *,
    separators: str,
    strip_path_prefix: bool,
    required: bool,
    what: str,
) -> tuple[str, ...]:
    """Resolve and normalise a list-shaped claim, distributed-aware.

    When the claim is distributed: raise :class:`DistributedClaimError`
    if it is *required* for admission (cannot admit safely without it),
    otherwise log a visible warning and return ``()`` (fail-safe: a value
    that only elevates degrades to no elevation, never to a silent admit).
    """
    if not path:
        return ()
    if _is_distributed(claims, path):
        if required:
            raise DistributedClaimError(
                f"Claim '{path}' is supplied as a distributed claim "
                "(for example, Entra group overage) and cannot be resolved "
                "from the token. Configure the identity provider to include "
                "the claim inline."
            )
        log.warning(
            "Claim '%s' is a distributed claim and was not resolved; "
            "%s remains empty (no privilege escalation).",
            path,
            what,
        )
        return ()
    return normalise_claim_values(
        claim_path(claims, path),
        separators=separators,
        strip_path_prefix=strip_path_prefix,
    )


def groups_gate_active(config: ClaimMappingConfig) -> bool:
    """Whether group VALUES actually gate admission.

    True only when an allowlist is set AND it is not the ``"*"`` wildcard
    (which admits any authenticated user regardless of group values). The
    single predicate shared by the distributed-required decision and the
    admission check, so the two cannot diverge in wildcard mode.
    """
    return bool(config.allowed_groups) and "*" not in config.allowed_groups


def extract_groups(
    claims: dict[str, Any], config: ClaimMappingConfig
) -> tuple[str, ...]:
    """Extract the group values; distributed-required only when gating."""
    return _extract_list(
        claims,
        config.groups_claim,
        separators=config.separators,
        strip_path_prefix=config.strip_group_path_prefix,
        required=groups_gate_active(config),
        what="groups",
    )


def extract_roles(
    claims: dict[str, Any], config: ClaimMappingConfig
) -> tuple[str, ...]:
    """Extract the role values; never admission-gating (elevate only)."""
    return _extract_list(
        claims,
        config.roles_claim,
        separators=config.separators,
        strip_path_prefix=False,
        required=False,
        what="roles",
    )


def derive_is_admin(
    groups: Iterable[str],
    roles: Iterable[str],
    config: ClaimMappingConfig,
) -> bool:
    """Whether the mapped groups/roles grant instance-admin (grant-only)."""
    return bool(config.admin_roles & frozenset(roles)) or bool(
        config.admin_groups & frozenset(groups)
    )


def admission_error(
    groups: Iterable[str],
    email: str | None,
    config: ClaimMappingConfig,
) -> str | None:
    """Return a rejection reason, or ``None`` when the login is admitted.

    Two orthogonal gates, each active only when configured: a group
    allowlist (with a ``"*"`` wildcard for "any authenticated user") and
    an email-domain allowlist. A login with no email is rejected by the
    domain gate (fail-closed), which is the safe default.
    """
    if groups_gate_active(config):
        if not config.allowed_groups & frozenset(groups):
            return (
                "No group matches the configured access list "
                "(INQTRIX_OIDC_ALLOWED_GROUPS)."
            )
    if config.allowed_domains:
        normalised = (email or "").strip()
        domain = (
            normalised.rsplit("@", 1)[-1].lower() if "@" in normalised else ""
        )
        if domain not in config.allowed_domains:
            return (
                "Email domain is not in the configured access list "
                "(INQTRIX_OIDC_ALLOWED_DOMAINS)."
            )
    return None
