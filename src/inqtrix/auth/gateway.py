"""Gateway mode: an API gateway in front of Inqtrix asserts the signed-in user.

``INQTRIX_AUTH_MODE=gateway`` trusts identity only as a signed user token
(``INQTRIX_GATEWAY_ASSERTION=jwt``) or as identity headers that carry a shared
secret from a trusted peer (``header``). Every gateway identity becomes an
ordinary Inqtrix user with a local session, bound by (issuer, subject) and never
by email.

This module holds the identity sources (the resolvers that turn one request
into a :class:`VerifiedIdentity` or reject it with one :data:`GatewayOutcome`,
and their start checks), the provider of the mode (session start, the check
of every request against the gateway sign-in, logout, admission), the
accounting of rejections in the log and the metrics, and the builder that
wires a deployment from the settings. A resolver never decides about
accounts, sessions or rights. Checks every process of a gateway deployment
needs (secrets, storage backend, SCIM, token bounds) run in
:func:`inqtrix.auth.principal.resolve_auth_mode`. Every problem is named at
once; secret values never appear in a message, and no claim or header value
reaches the log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import inspect
import ipaddress
import json
import logging
import re
import secrets
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, runtime_checkable

import httpx
from fastapi import HTTPException
from joserfc import jwt as jose_jwt
from starlette.requests import Request

from inqtrix.auth.directory import RESERVED_ISSUERS
from inqtrix.auth.invitations import CONFIRMED_EMAIL_REQUIRED, RegistrationDenied
from inqtrix.auth.jwks import (
    GATEWAY_JWKS_TTL_SECONDS,
    JwksCache,
    KeysUnavailable,
    StaticKeys,
    fetched_without_tls,
    load_key_file,
)
from inqtrix.auth.lifecycle import LoginCommand, UserDisabledError, UserNotProvisionedError
from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.auth.mapping import (
    ClaimMappingConfig,
    DistributedClaimError,
    claim_path,
    derive_is_admin,
    domain_admission_error,
    email_is_confirmed,
    extract_groups,
    extract_roles,
    group_admission_error,
    normalise_claim_values,
)
from inqtrix.auth.oidc import OidcAuthProvider
from inqtrix.auth.sessions import AuthSession, new_session_id
from inqtrix.observability.metrics_defs import active_metrics

if TYPE_CHECKING:
    from inqtrix.auth.directory import MirroredUser, UserDirectory
    from inqtrix.auth.invitations import InvitationRepository, RegistrationGate
    from inqtrix.auth.lifecycle import UserLifecycleService
    from inqtrix.auth.pat import PatService, PatVerifier
    from inqtrix.auth.principal import AuthMode
    from inqtrix.auth.sessions import FlowStore, SessionStore
    from inqtrix.settings import AuthSettings, Settings

log = logging.getLogger(__name__)

#: Longest issuer an identity source may bind users under.
ISSUER_KEY_MAX_LENGTH = 255

_SHARED_SECRET_MIN_LENGTH = 32

#: Asymmetric signature algorithms a user token may use. ``none`` and the
#: HMAC family are never valid: with a public key as HMAC secret anyone could
#: sign a token.
ALLOWED_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"}
)

#: Claims that can change or be given to another person; none of them may
#: serve as the subject.
_MUTABLE_SUBJECT_CLAIMS = frozenset({"email", "upn", "preferred_username", "unique_name"})

#: Header names an identity header may never use: credentials of the
#: browser session, routing, and the hop-by-hop headers of RFC 9110 that a
#: proxy removes or rewrites.
_RESERVED_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Header names the edge passes on unchanged (the CSRF, generation, workspace
#: and guest CSRF headers and ``Origin``): a resolver of its own may not read
#: them, or the edge would have to strip what the browser needs.
_EDGE_PASSED_HEADER_NAMES = frozenset(
    {
        "x-csrf-token",
        "x-inqtrix-expected-user-id",
        "x-inqtrix-workspace-id",
        "x-inqtrix-guest-csrf",
        "origin",
    }
)

#: An HTTP field name (a token of RFC 9110).
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

#: ``INQTRIX_GATEWAY_RESOLVER``: a dotted module path, one colon, a class name.
_IMPORT_PATH = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")

#: Largest assertion header a JWT resolver reads.
ASSERTION_MAX_BYTES = 16 * 1024

#: Header parameters that would let a token bring its own key; never used.
_TOKEN_KEY_PARAMETERS = frozenset({"jwk", "jku", "x5u", "x5c"})

SUBJECT_MAX_LENGTH = 255
EMAIL_MAX_LENGTH = 320
NAME_MAX_LENGTH = 256
GROUPS_MAX_BYTES = 8 * 1024
GROUPS_MAX_ENTRIES = 256

#: ``local@domain.tld``: a value of this form is never accepted as subject.
_EMAIL_FORM = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Unresolved distributed claims are reported at most this often per claim.
_UNRESOLVED_WARNING_INTERVAL_SECONDS = 60.0

GatewayOutcome = Literal[
    "assertion_missing",
    "malformed",
    "duplicate_header",
    "untrusted_source",
    "signature_invalid",
    "expired",
    "claims_invalid",
    "subject_is_email",
    "keys_unavailable",
    "resolver_failed",
    "principal_mismatch",
    "registration_denied",
    "admission_denied",
    "email_unverified",
    "not_provisioned",
    "account_disabled",
    "scim_unauthorized",
    "scim_erase_limited",
    "scim_erase_start_failed",
    "scim_erase_not_started",
    "erasure_refused",
    "provisioned",
    "deprovisioned",
]
"""Every outcome of a gateway sign-in: the ``reason`` of a response and the class in the log are the same value."""

OUTCOME_STATUS: Mapping[GatewayOutcome, int] = MappingProxyType(
    {
        "assertion_missing": 401,
        "malformed": 400,
        "duplicate_header": 400,
        "untrusted_source": 401,
        "signature_invalid": 401,
        "expired": 401,
        "claims_invalid": 401,
        "subject_is_email": 403,
        "keys_unavailable": 503,
        "resolver_failed": 503,
        "principal_mismatch": 401,
        "registration_denied": 403,
        "admission_denied": 403,
        "email_unverified": 403,
        "not_provisioned": 403,
        "account_disabled": 403,
        "scim_unauthorized": 401,
    }
)
"""HTTP status of each rejecting outcome; the core derives it, a resolver never chooses one."""

ASSERTION_FAILURES: frozenset[GatewayOutcome] = frozenset(
    {
        "malformed",
        "duplicate_header",
        "untrusted_source",
        "signature_invalid",
        "expired",
        "claims_invalid",
        "subject_is_email",
        "keys_unavailable",
        "resolver_failed",
    }
)
"""The outcomes a resolver may raise; the core also raises ``resolver_failed`` for any other exception of a resolver of its own."""


@dataclass(frozen=True)
class VerifiedIdentity:
    """What the gateway asserted for one request about a signed-in human.

    Never a local user, issuer or tenant, never the gateway's own identity, a
    workload or a client.

    Attributes:
        subject: The immutable id the identity provider gives the person.
        email: Profile data and invitation match only, never identity.
        email_verified: ``True`` only when the assertion proves it.
        display_name: Name for the profile, when the assertion carries one.
        groups: Group values, for admission and (only from tokens) admin.
        roles: Role values, for admin (only from tokens).
    """

    subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


class GatewayAssertionError(Exception):
    """An invalid assertion; its status comes from :data:`OUTCOME_STATUS`.

    Raises:
        ValueError: The outcome is not one a resolver may raise.
    """

    def __init__(self, outcome: GatewayOutcome) -> None:
        if outcome not in ASSERTION_FAILURES:
            raise ValueError(f"{outcome!r} is not an outcome a resolver may raise")
        super().__init__(outcome)
        self.outcome: GatewayOutcome = outcome

    @property
    def status(self) -> int:
        """The HTTP status of the outcome."""
        return OUTCOME_STATUS[self.outcome]


@runtime_checkable
class GatewayIdentityResolver(Protocol):
    """Turns one request into the identity the gateway asserted for it."""

    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        """``None``: no assertion on this request. Raise :class:`GatewayAssertionError`: an invalid one."""
        ...


@dataclass(frozen=True)
class IdentitySource:
    """The source whose users a gateway deployment binds.

    Attributes:
        issuer_key: Issuer under which users are bound (the JWT issuer in
            ``jwt`` mode, ``INQTRIX_GATEWAY_ISSUER_KEY`` in ``header`` mode).
            Never empty, at most :data:`ISSUER_KEY_MAX_LENGTH` characters and
            never one of :data:`~inqtrix.auth.directory.RESERVED_ISSUERS`;
            :meth:`check_issuer_key` is the only place that rule lives.
        resolver: Reads the identity from a request.

    Raises:
        ValueError: The issuer key is empty, too long or reserved.
        TypeError: The resolver does not implement :class:`GatewayIdentityResolver`.
    """

    issuer_key: str
    resolver: GatewayIdentityResolver

    def __post_init__(self) -> None:
        self.check_issuer_key(self.issuer_key)
        if not isinstance(self.resolver, GatewayIdentityResolver):
            raise TypeError("the resolver does not implement GatewayIdentityResolver")

    @staticmethod
    def check_issuer_key(issuer_key: str) -> None:
        """Reject an empty, overlong or reserved issuer key.

        Raises:
            ValueError: With the reason; the issuer itself is never quoted.
        """
        if not issuer_key.strip():
            raise ValueError("the issuer key is empty")
        if len(issuer_key) > ISSUER_KEY_MAX_LENGTH:
            raise ValueError(
                f"the issuer key is longer than {ISSUER_KEY_MAX_LENGTH} characters"
            )
        if issuer_key in RESERVED_ISSUERS:
            raise ValueError("the issuer key is reserved for Inqtrix itself")


class _Throttle:
    """Allows one line per key and interval and counts the lines it held back."""

    def __init__(self, interval_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._last: dict[str, float] = {}
        self._held_back: dict[str, int] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        """Whether a line may go out now, and how many were held back before it."""
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._interval_seconds:
            self._held_back[key] = self._held_back.get(key, 0) + 1
            return False, 0
        self._last[key] = now
        return True, self._held_back.pop(key, 0)


_OUTCOME_MESSAGES: Mapping[GatewayOutcome, str] = MappingProxyType(
    {
        "assertion_missing": "No gateway sign-in was found on this request.",
        "malformed": "The gateway assertion is malformed.",
        "duplicate_header": "An identity header was sent more than once.",
        "untrusted_source": "The identity headers did not come from a trusted gateway.",
        "signature_invalid": "The gateway assertion has no valid signature.",
        "expired": "The gateway assertion is outside its lifetime.",
        "claims_invalid": "The gateway assertion does not prove a signed-in user.",
        "subject_is_email": "The gateway sent an email address as the user id.",
        "keys_unavailable": "The signing keys of the identity provider are unavailable.",
        "resolver_failed": "The identity resolver of this deployment failed.",
        "principal_mismatch": "The session belongs to another user than the gateway sign-in.",
        "registration_denied": "Registration requires an invitation. Contact the administrator.",
        "admission_denied": "This account is not admitted to Inqtrix. Contact the administrator.",
        "email_unverified": CONFIRMED_EMAIL_REQUIRED,
        "not_provisioned": "This account has not been provisioned yet. Contact the administrator.",
        "account_disabled": "Account is disabled.",
        "scim_unauthorized": "The SCIM token is missing or invalid.",
    }
)
"""Fixed message of each rejecting outcome; no request value ever appears in it."""


class GatewayRejected(Exception):
    """A gateway sign-in rejected with one outcome.

    The outcome fixes the HTTP status (:data:`OUTCOME_STATUS`) and the
    message; the outcome itself is the ``type`` of the error response.

    Raises:
        ValueError: The outcome does not reject.
    """

    def __init__(self, outcome: GatewayOutcome) -> None:
        if outcome not in OUTCOME_STATUS:
            raise ValueError(f"{outcome!r} is not a rejecting outcome")
        super().__init__(outcome)
        self.outcome: GatewayOutcome = outcome

    @property
    def status(self) -> int:
        """The HTTP status of the outcome."""
        return OUTCOME_STATUS[self.outcome]

    @property
    def message(self) -> str:
        """The fixed message of the outcome."""
        return _OUTCOME_MESSAGES[self.outcome]

    @property
    def detail(self) -> dict[str, dict[str, str]]:
        """The error body of an HTTP exception for this outcome."""
        return {"error": {"message": self.message, "type": self.outcome}}


#: A rejected gateway sign-in is reported at most this often per outcome.
REJECTION_WARNING_INTERVAL_SECONDS = 60.0

_DAY_SECONDS = 86_400.0


class GatewayRejections:
    """Counts rejected gateway sign-ins; one WARNING per outcome and interval.

    Every request without a valid assertion is rejected, for instance each
    direct request to the API port, so the log must not grow with traffic:
    the metric counts each rejection, and the WARNING names the outcome and
    how many it held back since its last line. No header, claim or identity
    value reaches either.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = REJECTION_WARNING_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._throttle = _Throttle(interval_seconds, clock)

    def record(self, outcome: GatewayOutcome) -> None:
        """Count one rejection and report it unless its outcome was just reported."""
        metrics = active_metrics()
        if metrics is not None:
            metrics.count_gateway_rejection(outcome)
        allowed, held_back = self._throttle.allow(outcome)
        if allowed:
            log.warning(
                "Gateway sign-in rejected (outcome=%s; %d more since the last report).",
                outcome,
                held_back,
            )


def has_control_characters(value: str) -> bool:
    """Whether *value* holds a C0 control character or DEL; shared with SCIM."""
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _is_email_like(subject: str, email: str | None) -> bool:
    if _EMAIL_FORM.match(subject):
        return True
    return email is not None and subject.casefold() == email.casefold()


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scope_values(claims: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for name in ("scope", "scp"):
        value = claims.get(name)
        if isinstance(value, str):
            values.update(value.split())
        elif isinstance(value, list):
            values.update(item for item in value if isinstance(item, str))
    return values


def _jws_header(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or not parts[0]:
        raise GatewayAssertionError("malformed")
    try:
        padded = parts[0] + "=" * (-len(parts[0]) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, RecursionError):
        # A header nested deeper than the parser recurses is unreadable too.
        raise GatewayAssertionError("malformed") from None
    if not isinstance(header, dict):
        raise GatewayAssertionError("malformed")
    return header


class JwtAssertionResolver:
    """A signed user token in one request header (``INQTRIX_GATEWAY_ASSERTION=jwt``).

    Only a token issued after a person signed in counts: the pinned issuer and
    audience, a valid signature from the configured keys, the user scope in
    ``scope`` or ``scp`` (never in ``roles``), a lifetime that includes now, and
    a subject that is not an email.

    Args:
        header_name: Request header with the raw token, without ``Bearer``.
        issuer: Exact ``iss``, compared byte for byte.
        audience: Value ``aud`` must contain.
        user_scope: Scope only a person's sign-in receives.
        algorithms: Allowed signature algorithms.
        leeway_seconds: Clock tolerance for ``exp``, ``nbf`` and ``iat``.
        subject_claim: Claim path of the immutable subject.
        claim_mapping: Where name, email, groups and roles sit in the claims.
        keys: The key source (:class:`~inqtrix.auth.jwks.JwksCache` or
            :class:`~inqtrix.auth.jwks.StaticKeys`).
        clock: Wall clock in seconds (tests pass their own).
    """

    def __init__(
        self,
        *,
        header_name: str,
        issuer: str,
        audience: str,
        user_scope: str,
        algorithms: Sequence[str],
        leeway_seconds: int,
        subject_claim: str,
        claim_mapping: ClaimMappingConfig,
        keys: JwksCache | StaticKeys,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._header_name = header_name
        self._issuer = issuer
        self._audience = audience
        self._user_scope = user_scope
        self._algorithms = tuple(algorithms)
        self._leeway_seconds = leeway_seconds
        self._subject_claim = subject_claim
        self._claim_mapping = claim_mapping
        self._keys = keys
        self._clock = clock
        self._unresolved = _Throttle(_UNRESOLVED_WARNING_INTERVAL_SECONDS)

    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        """The asserted identity, ``None`` without an assertion header.

        Raises:
            GatewayAssertionError: The assertion is present but invalid.
        """
        values = request.headers.getlist(self._header_name)
        if not values:
            return None
        if len(values) > 1:
            raise GatewayAssertionError("duplicate_header")
        token = values[0].strip()
        if len(token.encode("utf-8", "surrogateescape")) > ASSERTION_MAX_BYTES:
            raise GatewayAssertionError("malformed")
        header = _jws_header(token)
        if header.get("alg") not in self._algorithms or _TOKEN_KEY_PARAMETERS & header.keys():
            raise GatewayAssertionError("signature_invalid")
        try:
            keys = await self._keys.keys_for(token)
        except KeysUnavailable:
            raise GatewayAssertionError("keys_unavailable") from None
        try:
            decoded = jose_jwt.decode(token, keys, algorithms=list(self._algorithms))
        except Exception:  # noqa: BLE001 -- any verification failure is the same outcome
            raise GatewayAssertionError("signature_invalid") from None
        claims = dict(decoded.claims)
        self._check_audience_and_scope(claims)
        self._check_lifetime(claims)
        email_value = claim_path(claims, self._claim_mapping.email_claim)
        email = email_value if isinstance(email_value, str) and email_value else None
        subject = claim_path(claims, self._subject_claim)
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > SUBJECT_MAX_LENGTH
            or has_control_characters(subject)
        ):
            raise GatewayAssertionError("claims_invalid")
        if _is_email_like(subject, email):
            raise GatewayAssertionError("subject_is_email")
        try:
            groups = extract_groups(claims, self._claim_mapping, warn_unresolved=self._warn_unresolved)
            roles = extract_roles(claims, self._claim_mapping, warn_unresolved=self._warn_unresolved)
        except DistributedClaimError:
            raise GatewayAssertionError("claims_invalid") from None
        name = claim_path(claims, self._claim_mapping.username_claim)
        return VerifiedIdentity(
            subject=subject,
            email=email,
            email_verified=email_is_confirmed(claims, self._claim_mapping),
            display_name=name if isinstance(name, str) and name else None,
            groups=groups,
            roles=roles,
        )

    async def aclose(self) -> None:
        """Release the key source's HTTP client, when it has one."""
        closer = getattr(self._keys, "aclose", None)
        if closer is not None:
            await closer()

    def _check_audience_and_scope(self, claims: Mapping[str, Any]) -> None:
        if claims.get("iss") != self._issuer:
            raise GatewayAssertionError("claims_invalid")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self._audience not in audiences:
            raise GatewayAssertionError("claims_invalid")
        if self._user_scope not in _scope_values(claims):
            raise GatewayAssertionError("claims_invalid")

    def _check_lifetime(self, claims: Mapping[str, Any]) -> None:
        now = self._clock()
        expires_at = claims.get("exp")
        if not _is_number(expires_at) or expires_at <= now - self._leeway_seconds:
            raise GatewayAssertionError("expired")
        for name in ("nbf", "iat"):
            value = claims.get(name)
            if value is not None and (not _is_number(value) or value > now + self._leeway_seconds):
                raise GatewayAssertionError("expired")

    def _warn_unresolved(self, path: str, what: str) -> None:
        # Tokens arrive with every request; one line per claim and interval.
        allowed, held_back = self._unresolved.allow(f"{path}|{what}")
        if allowed:
            log.warning(
                "Claim '%s' is a distributed claim and was not resolved; %s remains "
                "empty (no privilege escalation); %d more since the last line.",
                path,
                what,
                held_back,
            )


class TrustedHeaderResolver:
    """Identity headers from a trusted peer with a shared secret (``INQTRIX_GATEWAY_ASSERTION=header``).

    The socket peer must be in the trusted networks (never a forwarded
    address), the proof header must carry the shared secret exactly once, each
    identity header may appear at most once, and every value must be short and
    free of control characters. A header never proves a verified email and
    never carries roles.

    Args:
        subject_header: Header with the stable id.
        email_header: Header with the email, or ``""``.
        name_header: Header with the display name, or ``""``.
        groups_header: Header with the groups, or ``""``.
        proof_header: Header that carries the shared secret.
        shared_secret: The secret the gateway sends.
        trusted_networks: Peers allowed to send identity headers.
        header_decoding: ``percent`` decodes name and groups.
        claim_mapping: Separators and path prefix for the groups value.
    """

    def __init__(
        self,
        *,
        subject_header: str,
        email_header: str,
        name_header: str,
        groups_header: str,
        proof_header: str,
        shared_secret: str,
        trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
        header_decoding: Literal["none", "percent"],
        claim_mapping: ClaimMappingConfig,
    ) -> None:
        self._subject_header = subject_header
        self._email_header = email_header
        self._name_header = name_header
        self._groups_header = groups_header
        self._proof_header = proof_header
        self._secret_digest = hashlib.sha256(shared_secret.encode("utf-8")).digest()
        self._trusted_networks = tuple(trusted_networks)
        self._percent_decoding = header_decoding == "percent"
        self._claim_mapping = claim_mapping

    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        """The asserted identity, ``None`` when no subject header is present.

        Raises:
            GatewayAssertionError: The headers are present but invalid.
        """
        subjects = request.headers.getlist(self._subject_header)
        if not subjects:
            return None
        _require_proof(
            request,
            networks=self._trusted_networks,
            proof_header=self._proof_header,
            secret_digest=self._secret_digest,
        )
        if len(subjects) != 1:
            raise GatewayAssertionError("duplicate_header")
        email = self._single(request, self._email_header)
        name = self._single(request, self._name_header)
        groups_value = self._single(request, self._groups_header)
        subject = subjects[0]
        if not self._is_valid_subject(subject):
            raise GatewayAssertionError("malformed")
        if email is not None and (len(email) > EMAIL_MAX_LENGTH or has_control_characters(email)):
            raise GatewayAssertionError("malformed")
        if name is not None:
            name = self._decoded(name)
            if len(name) > NAME_MAX_LENGTH or has_control_characters(name):
                raise GatewayAssertionError("malformed")
        groups: tuple[str, ...] = ()
        if groups_value is not None:
            if len(groups_value.encode("utf-8", "surrogateescape")) > GROUPS_MAX_BYTES:
                raise GatewayAssertionError("malformed")
            groups = tuple(self._decoded(group) for group in self._split_groups(groups_value))
            if len(groups) > GROUPS_MAX_ENTRIES or any(has_control_characters(group) for group in groups):
                raise GatewayAssertionError("malformed")
            if has_control_characters(groups_value):
                raise GatewayAssertionError("malformed")
        if _is_email_like(subject, email):
            raise GatewayAssertionError("subject_is_email")
        return VerifiedIdentity(
            subject=subject,
            email=email or None,
            email_verified=False,
            display_name=name or None,
            groups=groups,
            roles=(),
        )

    @staticmethod
    def _single(request: Request, header: str) -> str | None:
        if not header:
            return None
        values = request.headers.getlist(header)
        if len(values) > 1:
            raise GatewayAssertionError("duplicate_header")
        return values[0] if values else None

    @staticmethod
    def _is_valid_subject(subject: str) -> bool:
        # Visible ASCII without space and comma: a folded double value never
        # passes as one subject.
        return (
            0 < len(subject) <= SUBJECT_MAX_LENGTH
            and all(0x21 <= ord(character) <= 0x7E for character in subject)
            and "," not in subject
        )

    def _split_groups(self, value: str) -> tuple[str, ...]:
        return normalise_claim_values(
            value,
            separators=self._claim_mapping.separators,
            strip_path_prefix=self._claim_mapping.strip_group_path_prefix,
        )

    def _decoded(self, value: str) -> str:
        if not self._percent_decoding:
            return value
        try:
            return urllib.parse.unquote(value, errors="strict")
        except UnicodeDecodeError:
            raise GatewayAssertionError("malformed") from None


def _peer_is_trusted(
    request: Request, networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    """Whether the socket peer, never a forwarded address, lies in *networks*."""
    if request.client is None:
        return False
    try:
        peer = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(peer in network for network in networks)


def _require_proof(
    request: Request,
    *,
    networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
    proof_header: str,
    secret_digest: bytes,
) -> None:
    """Raise ``untrusted_source`` unless a trusted peer sent the secret exactly once."""
    if not _peer_is_trusted(request, networks):
        raise GatewayAssertionError("untrusted_source")
    presented = request.headers.getlist(proof_header)
    if len(presented) != 1:
        raise GatewayAssertionError("untrusted_source")
    digest = hashlib.sha256(presented[0].encode("utf-8", "surrogateescape")).digest()
    if not hmac.compare_digest(digest, secret_digest):
        raise GatewayAssertionError("untrusted_source")


def _resolver_header_problems(names: Sequence[str], *, source: str, proof_header: str) -> list[str]:
    """Why *names* cannot be the headers a resolver of its own reads."""
    if not names:
        return [f"{source} must name at least one header"]
    problems: list[str] = []
    seen: set[str] = set()
    proof = proof_header.strip().lower()
    for raw in names:
        written = raw.strip() if isinstance(raw, str) else ""
        if not _HEADER_TOKEN.match(written):
            problems.append(f"{source} contains an entry that is not a header name")
            continue
        name = written.lower()
        if (
            name in _RESERVED_HEADER_NAMES
            or name in _EDGE_PASSED_HEADER_NAMES
            or name.startswith("x-forwarded-")
        ):
            problems.append(
                f"{source} must not name {name}: it carries credentials or routing, "
                "or the edge passes it on unchanged"
            )
        elif name == proof:
            problems.append(f"{source} must not name the proof header")
        if name in seen:
            problems.append(f"{source} names {name} twice")
        seen.add(name)
    return problems


def _resolver_value_problem(identity: VerifiedIdentity) -> GatewayOutcome | None:
    """The outcome for values a resolver of its own may not hand in, or ``None``.

    The bounds of the header assertion. The built-in resolvers check their
    own values where they read them.
    """
    subject = identity.subject
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > SUBJECT_MAX_LENGTH
        or has_control_characters(subject)
    ):
        return "malformed"
    for value, limit in (
        (identity.email, EMAIL_MAX_LENGTH),
        (identity.display_name, NAME_MAX_LENGTH),
    ):
        if value is not None and (
            not isinstance(value, str) or len(value) > limit or has_control_characters(value)
        ):
            return "malformed"
    for values in (identity.groups, identity.roles):
        # A text would count as its characters, ``None`` would end as a server error.
        if (
            not isinstance(values, (tuple, list))
            or len(values) > GROUPS_MAX_ENTRIES
            or any(not isinstance(value, str) or has_control_characters(value) for value in values)
        ):
            return "malformed"
    listed = (*identity.groups, *identity.roles)
    if sum(len(value.encode("utf-8", "surrogateescape")) for value in listed) > GROUPS_MAX_BYTES:
        return "malformed"
    if _is_email_like(subject, identity.email):
        return "subject_is_email"
    return None


def _class_name(resolver: object) -> str:
    return f"{type(resolver).__module__}.{type(resolver).__qualname__}"


def _resolver_type_problem(resolver: object) -> str | None:
    """Why *resolver* cannot be awaited for an identity, or ``None``."""
    if not isinstance(resolver, GatewayIdentityResolver):
        return "the resolver does not implement GatewayIdentityResolver"
    if not inspect.iscoroutinefunction(resolver.resolve):
        return "the resolver's resolve must be a coroutine function (async def)"
    return None


def _declared_headers(resolver: object, proof_header: str) -> tuple[tuple[str, ...], list[str]]:
    """The headers *resolver* declares in ``header_names``, lowered, and why they cannot be."""
    declared = getattr(resolver, "header_names", None)
    if declared is None or isinstance(declared, (str, bytes)) or not isinstance(declared, Iterable):
        return (), [
            "the resolver must declare the headers it reads in header_names, "
            "a collection of header names"
        ]
    names = tuple(declared)
    problems = _resolver_header_problems(names, source="header_names", proof_header=proof_header)
    return tuple(name.strip().lower() for name in names if isinstance(name, str)), problems


def _declared_view(request: Request, header_names: Sequence[str]) -> Request:
    """The request as a resolver of its own sees it: only its declared headers.

    Without the cookie, the proof header and every other header, without the
    query string and with an empty body, a header it forgot to declare fails
    closed instead of reaching it unchecked.
    """
    declared = set(header_names)
    headers = [
        (name, value)
        for name, value in request.scope.get("headers", [])
        if name.decode("latin-1").lower() in declared
    ]

    async def empty_body() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({**request.scope, "headers": headers, "query_string": b""}, empty_body)


class CustomResolverGuard:
    """A resolver of its own, behind the proof the header assertion asks for.

    Inqtrix cannot see how such a resolver checks a request, so it gets the
    trust of the header assertion. A request that carries none of the
    declared headers has no assertion. One that carries them counts only
    from a trusted socket peer with the shared secret in the proof header,
    each declared header at most once; only then does the resolver read it,
    and it sees its declared headers alone. Its values must keep the bounds
    of the header assertion.

    Args:
        resolver: The operator's resolver. ``header_names`` names the headers
            it reads; ``resolve`` is a coroutine function.
        trusted_networks: Peers allowed to send identity headers.
        proof_header: Header that carries the shared secret.
        shared_secret: The secret the gateway sends.
        declared: The resolver's ``header_names`` as its builder already
            read and checked them; ``None`` reads them here. A plugin's
            ``header_names`` is read once, since it may yield only once.

    Raises:
        TypeError: The resolver does not implement the protocol, or its
            ``resolve`` is no coroutine function.
        ValueError: The declared headers, the proof or the peers do not allow
            the check.
    """

    def __init__(
        self,
        resolver: GatewayIdentityResolver,
        *,
        trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
        proof_header: str,
        shared_secret: str,
        declared: tuple[str, ...] | None = None,
    ) -> None:
        type_problem = _resolver_type_problem(resolver)
        if type_problem is not None:
            raise TypeError(type_problem)
        if declared is None:
            names, problems = _declared_headers(resolver, proof_header)
            if problems:
                raise ValueError("; ".join(problems))
        else:
            names = declared
        if not proof_header.strip():
            raise ValueError("a resolver of its own needs the proof header of the header assertion")
        if len(shared_secret.strip()) < _SHARED_SECRET_MIN_LENGTH:
            raise ValueError(
                f"the shared secret must have at least {_SHARED_SECRET_MIN_LENGTH} characters"
            )
        if not trusted_networks:
            raise ValueError("a resolver of its own needs the trusted peers of the header assertion")
        if any(network.prefixlen == 0 for network in trusted_networks):
            raise ValueError("the trusted peers must not include every address")
        self.resolver = resolver
        self.header_names = names
        self._proof_header = proof_header.strip()
        self._secret_digest = hashlib.sha256(shared_secret.strip().encode("utf-8")).digest()
        self._trusted_networks = tuple(trusted_networks)
        self._failures = _Throttle(REJECTION_WARNING_INTERVAL_SECONDS, time.monotonic)

    @property
    def name(self) -> str:
        """Module and class of the resolver, for the start line."""
        return _class_name(self.resolver)

    async def resolve(self, request: Request) -> VerifiedIdentity | None:
        """The resolver's identity for a proven request, ``None`` without an assertion.

        Raises:
            GatewayAssertionError: No proof, a doubled header, values out of
                bounds, or ``resolver_failed`` when the resolver's own code
                raised anything else.
        """
        if not any(request.headers.getlist(name) for name in self.header_names):
            return None
        _require_proof(
            request,
            networks=self._trusted_networks,
            proof_header=self._proof_header,
            secret_digest=self._secret_digest,
        )
        if any(len(request.headers.getlist(name)) > 1 for name in self.header_names):
            raise GatewayAssertionError("duplicate_header")
        try:
            identity = await self.resolver.resolve(_declared_view(request, self.header_names))
        except GatewayAssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 - the code of a resolver of its own is contained here
            # Only the type: its text and frames can carry header values.
            self._report_failure(type(exc).__name__)
            raise GatewayAssertionError("resolver_failed") from None
        if isinstance(identity, VerifiedIdentity):
            outcome = _resolver_value_problem(identity)
            if outcome is not None:
                raise GatewayAssertionError(outcome)
        return identity

    def _report_failure(self, error_type: str) -> None:
        allowed, held_back = self._failures.allow(error_type)
        if allowed:
            log.warning(
                "A resolver of its own failed (outcome=resolver_failed error_type=%s; %d more since the last report).",
                error_type,
                held_back,
            )

    async def aclose(self) -> None:
        """Let the resolver release what it holds, when it can."""
        closer = getattr(self.resolver, "aclose", None)
        if closer is not None:
            await closer()


class _JwksEndpoint:
    """Fetches the JWKS document of the configured URL over its own client."""

    def __init__(self, url: str, *, ca_cert: str) -> None:
        self._url = url
        self._ca_cert = ca_cert
        self._client: httpx.AsyncClient | None = None

    async def __call__(self) -> Mapping[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0, verify=self._ca_cert or True)
        response = await self._client.get(self._url)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


GatewayAssertion = Literal["jwt", "header", "resolver"]
"""How the gateway asserts the user; only a ``jwt`` sign-in grants admin from groups or roles."""

#: The one resolver each assertion may carry. The label decides admin,
#: invitations and the edge's duties, so it must name what checked the request.
_ASSERTION_RESOLVERS: Mapping[str, type] = MappingProxyType(
    {
        "jwt": JwtAssertionResolver,
        "header": TrustedHeaderResolver,
        "resolver": CustomResolverGuard,
    }
)


@dataclass(frozen=True)
class GatewaySessionStart:
    """What ``GET /api/auth/session`` answers in the gateway mode.

    Attributes:
        payload: The response body.
        new_session: The session this request started, whose cookies the
            response sets; ``None`` when no assertion came or a session of the
            same user resumed.
    """

    payload: dict[str, Any]
    new_session: AuthSession | None = None


class GatewayAuthProvider(OidcAuthProvider):
    """Gateway-asserted identity on the shared session-cookie machinery.

    The session cookie stays the anchor for CSRF, the admin surface, token
    management and collaboration. A session starts only in
    ``GET /api/auth/session`` (:meth:`start_session`), and every later request
    carries the assertion again: it must name the user the session was
    started for, otherwise the session ends. Bearer tokens are personal access
    tokens only; an assertion on such a request is never read.

    Args:
        source: The identity source users are bound under.
        assertion: ``jwt``, ``header`` or ``resolver``, each with exactly its
            resolver (:class:`JwtAssertionResolver`, :class:`TrustedHeaderResolver`,
            :class:`CustomResolverGuard`). Only a ``jwt`` sign-in grants admin
            from groups or roles and proves a confirmed email, since a header
            or a resolver of its own proves the hop, not its values.
        claim_mapping: The admission gates and the admin groups and roles.
        first_user_owner: Whether the first user of the source becomes admin
            while the source has no active admin.
        logout_url: Where the browser goes after signing out; empty keeps it
            on the signed-out page.
        rejections: Rejection accounting (tests pass their own clock).
        scim_enabled: Whether SCIM provisions the accounts of the source;
            a sign-in then never creates an account.

    The remaining arguments are the session collaborators of
    :class:`~inqtrix.auth.oidc.OidcAuthProvider`; there is no IdP client.

    Raises:
        ValueError: The assertion and its resolver do not belong together, or
            the lifecycle counts admins of another issuer than the source
            (``source_issuer`` must equal the source's issuer key).
    """

    def __init__(
        self,
        *,
        source: IdentitySource,
        assertion: GatewayAssertion,
        claim_mapping: ClaimMappingConfig,
        first_user_owner: bool = False,
        logout_url: str = "",
        sessions: "SessionStore",
        flows: "FlowStore",
        users: "UserDirectory | None" = None,
        session_secret: str,
        session_max_age_seconds: int,
        secure_cookies: bool = True,
        pats: "PatVerifier | None" = None,
        pat_service: "PatService | None" = None,
        registration_gate: "RegistrationGate | None" = None,
        invitations: "InvitationRepository | None" = None,
        lifecycle: "UserLifecycleService | None" = None,
        rejections: GatewayRejections | None = None,
        scim_enabled: bool = False,
    ) -> None:
        # The exact type: a subclass may replace resolve and is then a
        # resolver of its own, which only the guard carries.
        expected = _ASSERTION_RESOLVERS.get(assertion)
        if expected is None or type(source.resolver) is not expected:
            raise ValueError(
                f"the {assertion} assertion needs exactly the resolver "
                f"{expected.__name__ if expected is not None else 'of a known assertion'}; "
                "a resolver of its own goes through CustomResolverGuard with the "
                "assertion 'resolver'"
            )
        if lifecycle is not None and lifecycle.source_issuer != source.issuer_key:
            raise ValueError(
                "the user lifecycle must count the admins of the identity source "
                "(its source_issuer must equal the source's issuer key)"
            )
        if pats is not None and pats.source_issuer != source.issuer_key:
            raise ValueError(
                "the token verifier must accept only accounts of the identity "
                "source (its source_issuer must equal the source's issuer key)"
            )
        super().__init__(
            client=None,
            sessions=sessions,
            flows=flows,
            users=users,
            session_secret=session_secret,
            session_max_age_seconds=session_max_age_seconds,
            claim_mapping=claim_mapping,
            secure_cookies=secure_cookies,
            pats=pats,
            pat_service=pat_service,
            registration_gate=registration_gate,
            invitations=invitations,
            lifecycle=lifecycle,
        )
        self.source = source
        self.assertion = assertion
        self.first_user_owner = first_user_owner
        self.logout_url = logout_url
        self.rejections = rejections or GatewayRejections()
        self.scim_enabled = scim_enabled

    @property
    def mode(self) -> "AuthMode":
        """``"gateway"``."""
        return "gateway"

    @property
    def invitations_accepted_by_login(self) -> bool:
        """Only a signed token proves a confirmed email."""
        return self.assertion == "jwt"

    async def aclose(self) -> None:
        """Release the HTTP client of the key source; it reopens when used."""
        closer = getattr(self.source.resolver, "aclose", None)
        if closer is not None:
            await closer()

    def reject(self, outcome: GatewayOutcome) -> NoReturn:
        """Count and report the outcome, then raise it."""
        self.rejections.record(outcome)
        raise GatewayRejected(outcome)

    async def verified_identity(self, request: Request) -> VerifiedIdentity | None:
        """The identity the gateway asserted for this request, ``None`` without one.

        Raises:
            GatewayRejected: The assertion is invalid.
            TypeError: The resolver returned something else, a programming
                error of the resolver.
        """
        try:
            identity = await self.source.resolver.resolve(request)
        except GatewayAssertionError as error:
            self.reject(error.outcome)
        if identity is not None and not isinstance(identity, VerifiedIdentity):
            raise TypeError(
                f"{type(self.source.resolver).__name__}.resolve returned "
                f"{type(identity).__name__}, not a VerifiedIdentity or None"
            )
        if identity is not None and self.assertion != "jwt" and identity.email_verified is not False:
            # Only a signed token proves a confirmed email.
            identity = replace(identity, email_verified=False)
        return identity

    def binds(self, user: "MirroredUser | None", identity: VerifiedIdentity) -> bool:
        """Whether *user* is the account of this identity under the source."""
        return (
            user is not None
            and user.issuer == self.source.issuer_key
            and user.subject == identity.subject
        )

    def build_principal_dependency(self) -> Callable[..., Any]:
        """Bearer: a personal access token. Otherwise assertion and session must agree.

        The live resolver for streams checks the session and the account only:
        it works with the frozen headers of the original request, and a
        short-lived gateway token would otherwise end every long stream. A
        route that calls the dependency itself holds a fresh request and gets
        the full check (``__inqtrix_request_resolver__``).
        """

        async def resolve(request: Request):
            bearer = self._bearer_value(request)
            if bearer is not None:
                return await self._bearer_principal(bearer)
            try:
                identity = await self.verified_identity(request)
                if identity is None:
                    self.reject("assertion_missing")
                session, user = await self._session_and_user_for(request)
                if not self.binds(user, identity):
                    await self.sessions.delete(session.id)
                    self.reject("principal_mismatch")
            except GatewayRejected as rejected:
                raise HTTPException(status_code=rejected.status, detail=rejected.detail) from None
            self._require_csrf(request, session)
            return self._session_principal(session)

        async def resolve_live(request: Request):
            bearer = self._bearer_value(request)
            if bearer is not None:
                return await self._bearer_principal(bearer)
            session, _user = await self._session_and_user_for(request)
            return self._session_principal(session)

        setattr(resolve, "__inqtrix_live_resolver__", resolve_live)
        setattr(resolve, "__inqtrix_request_resolver__", resolve)
        return resolve

    async def start_session(self, request: Request) -> GatewaySessionStart:
        """Start or resume the session for this request's assertion.

        Without an assertion no session starts. A session of the same user
        resumes; a session of another user is deleted first. A new session
        passes the admission gates and the shared login provisioning.

        Raises:
            GatewayRejected: The assertion is invalid or the login is refused.
        """
        identity = await self.verified_identity(request)
        if identity is None:
            self.rejections.record("assertion_missing")
            return GatewaySessionStart({"authenticated": False, "reason": "assertion_missing"})
        try:
            session, user = await self._session_and_user_for(request)
        except HTTPException:
            session, user = None, None
        if session is not None:
            if self.binds(user, identity):
                return GatewaySessionStart(await self.authenticated_payload(request, session, user))
            await self.sessions.delete(session.id)
        started, admitted = await self._admit(identity)
        payload = await self.authenticated_payload(request, started, admitted)
        return GatewaySessionStart(payload, new_session=started)

    async def _admit(self, identity: VerifiedIdentity) -> tuple[AuthSession, "MirroredUser"]:
        """Admission and provisioning of a new session for *identity*."""
        if group_admission_error(identity.groups, self.claim_mapping):
            self.reject("admission_denied")
        if self.claim_mapping.allowed_domains:
            # The domain gate reads only an address the identity provider
            # confirms; without one the sign-in says so instead of failing
            # as an unknown domain.
            if not (identity.email and identity.email_verified):
                self.reject("email_unverified")
            if domain_admission_error(identity.email, self.claim_mapping):
                self.reject("admission_denied")
        if self.lifecycle is None:
            raise RuntimeError("the gateway mode needs the atomic user lifecycle")
        now = time.time()
        session = AuthSession(
            id=new_session_id(),
            user_id=uuid.uuid4(),
            issuer=self.source.issuer_key,
            subject=identity.subject,
            email=identity.email,
            display_name=identity.display_name,
            groups=identity.groups,
            csrf_random=secrets.token_hex(16),
            created_at=now,
            expires_at=now + self.session_max_age_seconds,
        )
        command = LoginCommand(
            tenant_id="default",
            issuer=self.source.issuer_key,
            subject=identity.subject,
            email=identity.email or "",
            email_verified=identity.email_verified,
            display_name=identity.display_name,
            session=session,
            is_admin=(
                self.assertion == "jwt"
                and derive_is_admin(identity.groups, identity.roles, self.claim_mapping)
            ),
            first_login_owner=self.first_user_owner,
            invitation_required=(
                self.registration_gate is not None
                and self.registration_gate.registration == "invite"
            ),
            provisioned_only=self.scim_enabled,
        )
        try:
            admitted = await self.lifecycle.provision_login(command)
        except RegistrationDenied as exc:
            self.reject(
                "email_unverified" if exc.reason == "email_unverified" else "registration_denied"
            )
        except UserDisabledError:
            self.reject("account_disabled")
        except UserNotProvisionedError:
            self.reject("not_provisioned")
        if admitted.user_id == session.user_id:
            # The proposed canonical id survives only when the login created
            # the account; an existing account keeps its own.
            log.info(
                "Gateway sign-in created an account (outcome=provisioned user_ref=%s).",
                pseudonymous_log_reference("usr", admitted.user_id),
            )
        stored = await self.sessions.get(session.id)
        if stored is None:
            raise RuntimeError("the provisioned gateway session was not stored")
        return stored, admitted


def gateway_identity_source(auth: "AuthSettings") -> IdentitySource:
    """Check the gateway settings and build the identity source they describe.

    Args:
        auth: The resolved :class:`~inqtrix.settings.AuthSettings`.

    Returns:
        The identity source of the configured assertion kind, with its resolver.

    Raises:
        RuntimeError: The configuration is incomplete or unsafe; the message
            names every problem and the variables involved.
    """
    problems: list[str] = []
    assertion = auth.gateway_assertion
    if assertion == "jwt":
        issuer_variable = "INQTRIX_GATEWAY_JWT_ISSUER"
        issuer_key = auth.gateway_jwt_issuer
        problems.extend(_jwt_problems(auth))
    elif assertion == "header":
        issuer_variable = "INQTRIX_GATEWAY_ISSUER_KEY"
        issuer_key = auth.gateway_issuer_key
        problems.extend(_header_problems(auth))
    else:
        raise RuntimeError(
            "INQTRIX_AUTH_MODE=gateway requires INQTRIX_GATEWAY_ASSERTION=jwt "
            "or INQTRIX_GATEWAY_ASSERTION=header, or a resolver of its own in "
            "INQTRIX_GATEWAY_RESOLVER."
        )
    # The edge strips every set identity variable, whichever assertion reads it.
    problems.extend(_header_name_problems(_identity_header_variables(auth)))
    problems.extend(_trusted_proxy_problems(auth.gateway_trusted_proxies, required=assertion == "header"))
    if issuer_key.strip():
        try:
            IdentitySource.check_issuer_key(issuer_key)
        except ValueError as rejected:
            problems.append(f"{issuer_variable}: {rejected}")
    else:
        problems.append(f"{issuer_variable} must be set")
    resolver: GatewayIdentityResolver | None = None
    if not problems:
        try:
            resolver = _build_resolver(auth)
        except ValueError as rejected:
            problems.append(f"INQTRIX_GATEWAY_JWT_PUBLIC_KEY_FILE: {rejected}")
    if problems or resolver is None:
        raise RuntimeError(
            f"INQTRIX_AUTH_MODE=gateway with INQTRIX_GATEWAY_ASSERTION={assertion} "
            "cannot start: " + "; ".join(problems) + "."
        )
    return IdentitySource(issuer_key=issuer_key, resolver=resolver)


def _load_resolver(path: str) -> Any:
    """Import ``module:Class`` from ``INQTRIX_GATEWAY_RESOLVER`` and build it without arguments.

    The class is its own composition root and reads its configuration
    itself. Every failure is a start error naming the variable, the path and
    the type of the error, never the error's text, which can carry values.

    Raises:
        RuntimeError: The path is malformed, or the class cannot be built.
    """
    where = f"INQTRIX_GATEWAY_RESOLVER={path}"
    if not _IMPORT_PATH.match(path):
        raise RuntimeError(f"{where} must be module:Class with Python identifiers")
    module_name, class_name = path.split(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as missing:
        raise RuntimeError(f"{where}: the module {missing.name} cannot be imported") from None
    except (Exception, SystemExit) as failed:  # noqa: BLE001 -- any import failure is a start error
        raise RuntimeError(
            f"{where}: importing {module_name} failed ({type(failed).__name__})"
        ) from None
    resolver_class = getattr(module, class_name, None)
    if resolver_class is None:
        raise RuntimeError(f"{where}: {module_name} has no attribute {class_name}")
    if not isinstance(resolver_class, type):
        raise RuntimeError(f"{where}: {class_name} is not a class")
    try:
        return resolver_class()
    except (Exception, SystemExit) as failed:  # noqa: BLE001 -- the constructor is the plugin's own code
        raise RuntimeError(
            f"{where}: building {class_name}() failed ({type(failed).__name__})"
        ) from None


def _custom_identity_source(
    auth: "AuthSettings", resolver: GatewayIdentityResolver, *, where: str = ""
) -> IdentitySource:
    """Check the settings of a resolver of its own and put it behind the guard.

    Such a resolver gets the trust of the header assertion: the proof header
    and the trusted peers, no admin from groups or roles, no confirmed email.
    ``INQTRIX_GATEWAY_RESOLVER_HEADERS`` names every header it declares, so
    the web gateway strips them from untrusted peers.

    Raises:
        RuntimeError: The settings or the resolver do not allow it; the
            message names every problem and the variables involved.
    """
    problems = _custom_setting_problems(auth)
    listed = [name.strip() for name in auth.gateway_resolver_headers.split(",") if name.strip()]
    named = _class_name(resolver)
    prefix = f"{where}: the resolver {named}" if where else f"the resolver {named}"
    try:
        type_problem = _resolver_type_problem(resolver)
        declared, header_problems = _declared_headers(resolver, auth.gateway_proof_header)
    except (Exception, SystemExit) as failed:  # noqa: BLE001 -- header_names may run the plugin's own code
        raise RuntimeError(
            f"{prefix}: reading its header_names failed ({type(failed).__name__})"
        ) from None
    if type_problem is not None:
        problems.append(f"{prefix}: {type_problem}")
    problems.extend(f"{prefix}: {problem}" for problem in header_problems)
    if not header_problems:
        missing = sorted(set(declared) - {name.lower() for name in listed})
        if missing:
            problems.append(
                "INQTRIX_GATEWAY_RESOLVER_HEADERS must name every header the resolver "
                "declares (missing: " + ", ".join(missing) + ")"
            )
    if problems:
        raise RuntimeError(
            "INQTRIX_AUTH_MODE=gateway with a resolver of its own cannot start: "
            + "; ".join(problems)
            + "."
        )
    guard = CustomResolverGuard(
        resolver,
        trusted_networks=_trusted_networks(auth.gateway_trusted_proxies),
        proof_header=auth.gateway_proof_header,
        shared_secret=auth.gateway_shared_secret,
        declared=declared,
    )
    return IdentitySource(issuer_key=auth.gateway_issuer_key, resolver=guard)


def _custom_setting_problems(auth: "AuthSettings") -> list[str]:
    """The settings problems of a resolver of its own, checked before it is loaded."""
    problems: list[str] = []
    issuer_key = auth.gateway_issuer_key
    if issuer_key.strip():
        try:
            IdentitySource.check_issuer_key(issuer_key)
        except ValueError as rejected:
            problems.append(f"INQTRIX_GATEWAY_ISSUER_KEY: {rejected}")
    else:
        problems.append("INQTRIX_GATEWAY_ISSUER_KEY must be set")
    problems.extend(_trusted_proxy_problems(auth.gateway_trusted_proxies, required=True))
    if not auth.gateway_proof_header.strip():
        problems.append("INQTRIX_GATEWAY_PROOF_HEADER must be set")
    # The edge strips every configured identity header, so the built-in ones
    # obey the same name rules beside a resolver of its own.
    problems.extend(_header_name_problems(_identity_header_variables(auth)))
    if len(auth.gateway_shared_secret.strip()) < _SHARED_SECRET_MIN_LENGTH:
        problems.append(
            f"INQTRIX_GATEWAY_SHARED_SECRET must have at least {_SHARED_SECRET_MIN_LENGTH} "
            "characters"
        )
    for variable, value in (
        ("INQTRIX_OIDC_ADMIN_GROUPS", auth.oidc_admin_groups),
        ("INQTRIX_OIDC_ADMIN_ROLES", auth.oidc_admin_roles),
    ):
        if value.strip():
            problems.append(f"{variable} must be empty: a resolver of its own never grants admin")
    if auth.registration == "invite":
        problems.append(
            "INQTRIX_REGISTRATION=invite needs a verified email, which a resolver "
            "of its own does not prove"
        )
    if auth.oidc_allowed_domains.strip():
        problems.append(
            "INQTRIX_OIDC_ALLOWED_DOMAINS needs a verified email, which a resolver "
            "of its own does not prove"
        )
    listed = [name.strip() for name in auth.gateway_resolver_headers.split(",") if name.strip()]
    problems.extend(
        _resolver_header_problems(
            listed,
            source="INQTRIX_GATEWAY_RESOLVER_HEADERS",
            proof_header=auth.gateway_proof_header,
        )
    )
    return problems


def build_gateway_provider(
    settings: "Settings", *, resolver: GatewayIdentityResolver | None = None
) -> GatewayAuthProvider:
    """Build the auth provider of a gateway deployment from settings.

    Runs the start checks every gateway process runs (also on the library
    path, where :func:`~inqtrix.auth.principal.resolve_auth_mode` may not
    run) and those of the identity source, then builds the provider on the
    Postgres stores with a lifecycle that counts the admins of the source.

    Args:
        settings: The resolved settings.
        resolver: A resolver of its own (library path). It runs behind
            :class:`CustomResolverGuard` with the trust of the header
            assertion, and ``INQTRIX_GATEWAY_ASSERTION`` stays empty. Without
            it, ``INQTRIX_GATEWAY_RESOLVER`` may name one to load; never both.

    Raises:
        RuntimeError: The gateway configuration is incomplete or unsafe.
    """
    from inqtrix.auth.api_key import build_session_backends, claim_mapping_from_settings
    from inqtrix.auth.invitations import RegistrationGate
    from inqtrix.auth.lifecycle import UserLifecycleService
    from inqtrix.auth.pat import PatService, PatVerifier
    from inqtrix.auth.principal import check_gateway_deployment

    auth = settings.auth
    if auth.mode != "gateway":
        raise RuntimeError(
            "build_gateway_provider needs INQTRIX_AUTH_MODE=gateway, so that every "
            "process of the deployment resolves the same mode"
        )
    check_gateway_deployment(auth, settings.storage.backend)
    setting = auth.gateway_resolver.strip()
    sources = [
        name
        for name, present in (
            ("build_gateway_provider(resolver=...)", resolver is not None),
            ("INQTRIX_GATEWAY_RESOLVER", bool(setting)),
            ("INQTRIX_GATEWAY_ASSERTION", bool(auth.gateway_assertion)),
        )
        if present
    ]
    if len(sources) > 1:
        raise RuntimeError(
            "exactly one of build_gateway_provider(resolver=...), INQTRIX_GATEWAY_RESOLVER "
            "and INQTRIX_GATEWAY_ASSERTION names the resolver; set are: " + ", ".join(sources)
        )
    if resolver is None and not setting:
        source = gateway_identity_source(auth)
        assertion = auth.gateway_assertion
        named = ""
    else:
        if resolver is None:
            problems = _custom_setting_problems(auth)
            if problems:
                # Checked before loading: the plugin's code runs only for a valid setup.
                raise RuntimeError(
                    "INQTRIX_AUTH_MODE=gateway with a resolver of its own cannot start: "
                    + "; ".join(problems)
                    + "."
                )
            resolver = _load_resolver(setting)
            named = f" resolver={setting}"
            where = f"INQTRIX_GATEWAY_RESOLVER={setting}"
        else:
            named = f" resolver={_class_name(resolver)}"
            where = ""
        source = _custom_identity_source(auth, resolver, where=where)
        assertion = "resolver"
    (
        sessions,
        flows,
        users,
        pat_store,
        invitation_repo,
        _session_factory,
        lifecycle_transaction,
    ) = build_session_backends(settings, source_issuer=source.issuer_key)
    # Without SCIM nothing tells Inqtrix that a person left; these bounds
    # end a token at the cap and once its owner stops signing in.
    pat_service = PatService(
        store=pat_store,
        pepper=auth.pat_pepper,
        max_per_user=auth.pat_max_per_user,
        default_ttl_days=auth.pat_default_ttl_days,
        max_ttl_days=auth.gateway_pat_max_ttl_days,
    )
    pat_verifier = PatVerifier(
        store=pat_store,
        pepper=auth.pat_pepper,
        user_lookup=users,
        max_ttl_seconds=auth.gateway_pat_max_ttl_days * _DAY_SECONDS,
        owner_inactive_seconds=auth.gateway_pat_inactive_days * _DAY_SECONDS,
        source_issuer=source.issuer_key,
    )
    mapping = claim_mapping_from_settings(auth)
    provider = GatewayAuthProvider(
        source=source,
        assertion=assertion,
        claim_mapping=mapping,
        first_user_owner=auth.gateway_first_user_owner,
        logout_url=auth.gateway_logout_url.strip(),
        sessions=sessions,
        flows=flows,
        users=users,
        session_secret=auth.session_secret,
        session_max_age_seconds=auth.session_max_age_seconds,
        secure_cookies=not auth.oidc_insecure_dev_cookies,
        pats=pat_verifier,
        pat_service=pat_service,
        registration_gate=RegistrationGate(
            invitations=invitation_repo,
            users=users,
            registration=auth.registration,
        ),
        invitations=invitation_repo,
        scim_enabled=auth.scim_enabled,
        lifecycle=UserLifecycleService(
            users=users,
            sessions=sessions,
            invitations=invitation_repo,
            pat_service=pat_service,
            transaction=lifecycle_transaction,
        ),
    )
    _start_warnings(auth, mapping)
    trusted_proxies = [entry for entry in auth.gateway_trusted_proxies.split(",") if entry.strip()]
    log.info(
        "Gateway authentication: assertion=%s issuer_key=%r trusted_proxies=%d "
        "logout_url=%s first_user_owner=%s scim=%s%s.",
        assertion,
        source.issuer_key,
        len(trusted_proxies),
        "set" if provider.logout_url else "unset",
        auth.gateway_first_user_owner,
        "on" if provider.scim_enabled else "off",
        named,
    )
    return provider


def _start_warnings(auth: "AuthSettings", mapping: ClaimMappingConfig) -> None:
    """Start warnings that do not stop the start: paths the operator should set
    up, and settings that have no effect in this mode. None sets up an admin."""
    if auth.oidc_skip_email_verified:
        log.warning(
            "INQTRIX_OIDC_SKIP_EMAIL_VERIFIED has no effect in gateway mode: "
            "invitations and INQTRIX_OIDC_ALLOWED_DOMAINS need "
            "email_verified=true in the gateway token."
        )
    if not auth.gateway_logout_url.strip():
        log.warning(
            "INQTRIX_GATEWAY_LOGOUT_URL is not set: after signing out of Inqtrix the "
            "browser stays on the signed-out page and the gateway session lives on."
        )
    admin_from_claims = auth.gateway_assertion == "jwt" and bool(
        mapping.admin_groups or mapping.admin_roles
    )
    if not admin_from_claims and not auth.gateway_first_user_owner:
        log.warning(
            "No gateway sign-in can become the first instance admin: set "
            "INQTRIX_OIDC_ADMIN_GROUPS or INQTRIX_OIDC_ADMIN_ROLES (jwt only) or "
            "INQTRIX_GATEWAY_FIRST_USER_OWNER, unless an admin of this identity "
            "source exists already."
        )
    if auth.trusted_proxy_hops != 1:
        log.warning(
            "INQTRIX_TRUSTED_PROXY_HOPS=%d in gateway mode: the web gateway passes "
            "the X-Forwarded-For chain of its trusted peer on unchanged, so the "
            "client sits one hop from the right. Another value makes the guest-link "
            "throttle and the ip log field read an address a client chose or one "
            "all clients share; keep 1 unless a trusted proxy in front of the API "
            "gateway appends its own entry.",
            auth.trusted_proxy_hops,
        )
    if auth.gateway_assertion == "jwt" and fetched_without_tls(auth.gateway_jwks_url):
        log.warning(
            "INQTRIX_GATEWAY_JWKS_URL does not use https: whoever controls the "
            "network path to it can sign assertions for any person. Use https "
            "outside a local test setup."
        )


def _build_resolver(auth: "AuthSettings") -> GatewayIdentityResolver:
    from inqtrix.auth.api_key import claim_mapping_from_settings

    mapping = claim_mapping_from_settings(auth)
    if auth.gateway_assertion == "jwt":
        keys: JwksCache | StaticKeys
        if auth.gateway_jwt_public_key_file.strip():
            keys = StaticKeys(load_key_file(auth.gateway_jwt_public_key_file.strip()))
        else:
            keys = JwksCache(
                _JwksEndpoint(auth.gateway_jwks_url.strip(), ca_cert=auth.gateway_ca_cert.strip()),
                cache="gateway",
                ttl_seconds=GATEWAY_JWKS_TTL_SECONDS,
            )
        return JwtAssertionResolver(
            header_name=auth.gateway_jwt_header.strip(),
            issuer=auth.gateway_jwt_issuer,
            audience=auth.gateway_jwt_audience.strip(),
            user_scope=auth.gateway_jwt_user_scope.strip(),
            algorithms=[item.strip() for item in auth.gateway_jwt_algorithms.split(",") if item.strip()],
            leeway_seconds=auth.gateway_jwt_leeway_seconds,
            subject_claim=auth.gateway_subject_claim.strip(),
            claim_mapping=mapping,
            keys=keys,
        )
    return TrustedHeaderResolver(
        subject_header=auth.gateway_subject_header.strip(),
        email_header=auth.gateway_email_header.strip(),
        name_header=auth.gateway_name_header.strip(),
        groups_header=auth.gateway_groups_header.strip(),
        proof_header=auth.gateway_proof_header.strip(),
        shared_secret=auth.gateway_shared_secret.strip(),
        trusted_networks=_trusted_networks(auth.gateway_trusted_proxies),
        header_decoding=auth.gateway_header_decoding,
        claim_mapping=mapping,
    )


def _jwt_problems(auth: "AuthSettings") -> list[str]:
    problems: list[str] = []
    for variable, value in (
        ("INQTRIX_GATEWAY_JWT_HEADER", auth.gateway_jwt_header),
        ("INQTRIX_GATEWAY_JWT_AUDIENCE", auth.gateway_jwt_audience),
        ("INQTRIX_GATEWAY_JWT_USER_SCOPE", auth.gateway_jwt_user_scope),
    ):
        if not value.strip():
            problems.append(f"{variable} must be set")
    scope = auth.gateway_jwt_user_scope.strip()
    if scope and any(character.isspace() for character in scope):
        problems.append("INQTRIX_GATEWAY_JWT_USER_SCOPE must be one scope value without spaces")
    key_sources = [
        variable
        for variable, value in (
            ("INQTRIX_GATEWAY_JWKS_URL", auth.gateway_jwks_url),
            ("INQTRIX_GATEWAY_JWT_PUBLIC_KEY_FILE", auth.gateway_jwt_public_key_file),
        )
        if value.strip()
    ]
    if len(key_sources) != 1:
        problems.append(
            "set exactly one of INQTRIX_GATEWAY_JWKS_URL and "
            "INQTRIX_GATEWAY_JWT_PUBLIC_KEY_FILE"
        )
    algorithms = [item.strip() for item in auth.gateway_jwt_algorithms.split(",") if item.strip()]
    rejected = sorted({item for item in algorithms if item not in ALLOWED_JWT_ALGORITHMS})
    if not algorithms:
        problems.append("INQTRIX_GATEWAY_JWT_ALGORITHMS must name at least one algorithm")
    elif rejected:
        problems.append(
            "INQTRIX_GATEWAY_JWT_ALGORITHMS allows only "
            + ", ".join(sorted(ALLOWED_JWT_ALGORITHMS))
            + " (rejected: "
            + ", ".join(rejected)
            + ")"
        )
    subject_claim = auth.gateway_subject_claim.strip()
    if not subject_claim:
        problems.append("INQTRIX_GATEWAY_SUBJECT_CLAIM must be set")
    elif (
        subject_claim.lower() in _MUTABLE_SUBJECT_CLAIMS
        or subject_claim == auth.oidc_email_claim.strip()
    ):
        problems.append(
            "INQTRIX_GATEWAY_SUBJECT_CLAIM must name an immutable id, never the "
            "email claim or email, upn, preferred_username or unique_name"
        )
    return problems


def _header_problems(auth: "AuthSettings") -> list[str]:
    problems: list[str] = []
    for variable, value in (
        ("INQTRIX_GATEWAY_SUBJECT_HEADER", auth.gateway_subject_header),
        ("INQTRIX_GATEWAY_PROOF_HEADER", auth.gateway_proof_header),
    ):
        if not value.strip():
            problems.append(f"{variable} must be set")
    if len(auth.gateway_shared_secret.strip()) < _SHARED_SECRET_MIN_LENGTH:
        problems.append(
            f"INQTRIX_GATEWAY_SHARED_SECRET must have at least {_SHARED_SECRET_MIN_LENGTH} "
            "characters"
        )
    if not auth.gateway_email_header.strip() and not auth.gateway_name_header.strip():
        # Without a name or an email no admin can tell who an account is,
        # and the share search cannot find the person.
        problems.append("set INQTRIX_GATEWAY_EMAIL_HEADER or INQTRIX_GATEWAY_NAME_HEADER")
    if auth.registration == "invite":
        problems.append(
            "INQTRIX_REGISTRATION=invite needs a verified email, which identity "
            "headers do not carry"
        )
    if auth.oidc_allowed_domains.strip():
        problems.append(
            "INQTRIX_OIDC_ALLOWED_DOMAINS needs a verified email, which identity "
            "headers do not carry"
        )
    for variable, value in (
        ("INQTRIX_OIDC_ADMIN_GROUPS", auth.oidc_admin_groups),
        ("INQTRIX_OIDC_ADMIN_ROLES", auth.oidc_admin_roles),
    ):
        if value.strip():
            problems.append(f"{variable} must be empty: identity headers never grant admin")
    return problems


def _identity_header_variables(auth: "AuthSettings") -> dict[str, str]:
    """The six identity header variables by name, as the edge strips them."""
    return {
        "INQTRIX_GATEWAY_JWT_HEADER": auth.gateway_jwt_header,
        "INQTRIX_GATEWAY_SUBJECT_HEADER": auth.gateway_subject_header,
        "INQTRIX_GATEWAY_EMAIL_HEADER": auth.gateway_email_header,
        "INQTRIX_GATEWAY_NAME_HEADER": auth.gateway_name_header,
        "INQTRIX_GATEWAY_GROUPS_HEADER": auth.gateway_groups_header,
        "INQTRIX_GATEWAY_PROOF_HEADER": auth.gateway_proof_header,
    }


def _header_name_problems(headers: dict[str, str]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for variable, raw in headers.items():
        written = raw.strip()
        if not written:
            continue
        # As written, like the edge: lowering first would turn a Kelvin sign into a k.
        if not _HEADER_TOKEN.match(written):
            problems.append(f"{variable} is not a header name")
            continue
        name = written.lower()
        if name in _RESERVED_HEADER_NAMES or name.startswith("x-forwarded-"):
            problems.append(
                f"{variable} must not be Authorization, Cookie, Host, a hop-by-hop "
                "header or an X-Forwarded-* header"
            )
        elif name in _EDGE_PASSED_HEADER_NAMES:
            problems.append(
                f"{variable} must not name {name}: the edge passes it on unchanged"
            )
        if name in seen:
            problems.append(
                f"identity header names must be pairwise distinct: {seen[name]} and "
                f"{variable} name the same header"
            )
        else:
            seen[name] = variable
    return problems


def _trusted_networks(value: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [
        ipaddress.ip_network(entry.strip(), strict=False)
        for entry in value.split(",")
        if entry.strip()
    ]


def _trusted_proxy_problems(value: str, *, required: bool) -> list[str]:
    entries = [item.strip() for item in value.split(",") if item.strip()]
    if not entries:
        return ["INQTRIX_GATEWAY_TRUSTED_PROXIES must be set"] if required else []
    problems: list[str] = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            problems.append("INQTRIX_GATEWAY_TRUSTED_PROXIES contains an entry that is not a CIDR network")
            continue
        if network.prefixlen == 0:
            problems.append(
                "INQTRIX_GATEWAY_TRUSTED_PROXIES must not trust every address "
                "(0.0.0.0/0 or ::/0)"
            )
    return problems
