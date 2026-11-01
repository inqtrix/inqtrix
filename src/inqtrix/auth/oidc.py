"""Generic OIDC backend-for-frontend: client, provider, CSRF.

Implements the browser-apps BCP (draft-ietf-oauth-browser-based-apps
-26, IESG-approved) in a standards-first form. No IdP is hardwired:
Dex, Keycloak, Entra ID, Okta, and authentik are configuration, not code.

* Confidential client, authorization code + PKCE S256, ``state`` AND
  ``nonce`` on every request (RFC 9700: PKCE alone counters CSRF only
  with confirmed AS support — sending state unconditionally removes
  that failure mode).
* Tokens never reach the browser; the session cookie carries an
  opaque id referencing a server-side record; tokens are validated at
  login and then DISCARDED.
* id_token validation with an explicit algorithm allowlist
  (RS256/ES256 — ``none``/HS256 and token-supplied ``jwk``/``jku``
  headers are structurally rejected by joserfc's registry), pinned
  issuer, audience, expiry, and one-time nonce.
* JWKS through the shared cache in :mod:`inqtrix.auth.jwks` (six-hour
  TTL here): an unknown ``kid`` triggers at most one refresh per
  cooldown for the whole cache, so garbage-kid tokens cannot drive
  outbound fetch storms; concurrent refreshes share one fetch.
* CSRF: OWASP signed double-submit — the token is
  ``hex(HMAC(secret, session_id + random)) . hex(random)``, delivered
  in a non-HttpOnly cookie and required back in the ``X-CSRF-Token``
  header on every unsafe method of a cookie-authenticated request.

The discovery/JWKS/token fetches go to the OPERATOR-configured issuer
(deployment configuration, not user input), which is why a loopback Dex
in the dev stack must keep working: these URLs are never user input and
are not validated against an outbound-address policy.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable

import httpx
from fastapi import HTTPException, Request
from joserfc import jwt as jose_jwt

from inqtrix.auth.jwks import OIDC_JWKS_TTL_SECONDS, JwksCache, KeysUnavailable
from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.auth.mapping import (
    ClaimMappingConfig,
    OidcExchangeError,
    admission_error,
    claim_path,
    derive_is_admin,
    extract_groups,
    extract_roles,
)
from inqtrix.auth.principal import AuthProvider, Principal
from inqtrix.auth.sessions import AuthSession, FlowStore, SessionStore
from inqtrix.observability.context import bind_principal_context
from inqtrix.services.request_parsing import workspace_id_from_request

if TYPE_CHECKING:
    from inqtrix.auth.permissions import AuditSink
    from inqtrix.auth.lifecycle import UserLifecycleService
    from inqtrix.auth.directory import MirroredUser, UserDirectory
    from inqtrix.auth.invitations import (
        InvitationRepository,
        RegistrationGate,
    )
    from inqtrix.auth.pat import PatService, PatVerifier
    from inqtrix.auth.principal import AuthMode

log = logging.getLogger(__name__)

#: Added to the warning of the email_verified switch when invitations gate
#: the first login.
_SKIP_WITH_INVITE = (
    " INQTRIX_REGISTRATION=invite then admits a new user only with an open "
    "invitation for an email address the identity provider confirms "
    "(email_verified=true): sign-ins without that claim (Entra ID omits it) "
    "create no account, existing users keep signing in. To admit new users, "
    "set INQTRIX_REGISTRATION=open and limit sign-in at the identity provider "
    "(Entra ID: assignment required) or with INQTRIX_OIDC_ALLOWED_GROUPS."
)
#: Added when the domain gate is set: it then checks unconfirmed addresses.
_SKIP_WITH_DOMAINS = (
    " INQTRIX_OIDC_ALLOWED_DOMAINS then checks an address the identity "
    "provider does not confirm."
)

ALLOWED_ALGORITHMS = ("RS256", "ES256")
"""Accepted id_token signature algorithms. ``none`` and the symmetric
family are rejected by allowlist — the alg-confusion regression test
pins this."""

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSRF_HEADER = "X-CSRF-Token"

_UNAUTHENTICATED = {
    "error": {"message": "Not signed in", "type": "unauthorized"}
}
_CSRF_INVALID = {
    "error": {
        "message": "CSRF token is missing or invalid",
        "type": "csrf_error",
    }
}


def make_csrf_token(secret: str, session_id: str, random_hex: str) -> str:
    """Signed double-submit token bound to one session (OWASP form)."""
    message = f"{len(session_id)}!{session_id}!{len(random_hex)}!{random_hex}"
    digest = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{digest}.{random_hex}"


def verify_csrf_token(secret: str, session_id: str, token: str) -> bool:
    """Constant-time check of a submitted CSRF token."""
    _digest, separator, random_hex = token.partition(".")
    if not separator or not random_hex:
        return False
    expected = make_csrf_token(secret, session_id, random_hex)
    return hmac.compare_digest(expected, token)


class OidcClient:
    """Minimal OIDC relying-party client over httpx + joserfc.

    Args:
        issuer: Pinned issuer URL; the discovery document and every
            id_token must echo it exactly.
        client_id: Registered OAuth client id.
        client_secret: Confidential-client secret
            (``client_secret_basic`` at the token endpoint).
        redirect_url: Registered callback URL (exact-match at IdPs).
        scopes: Space-separated authorization scopes.
        discovery_url: Metadata URL override; empty derives
            ``{issuer}/.well-known/openid-configuration``.
        ca_cert: PEM bundle path for private-CA IdPs; empty uses the
            system trust store.
        transport: httpx transport override — tests inject a
            ``MockTransport`` here instead of monkeypatching.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_url: str,
        scopes: str = "openid profile email",
        discovery_url: str = "",
        ca_cert: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_url = redirect_url
        self._scopes = scopes
        self._discovery_url = discovery_url or (
            f"{self._issuer}/.well-known/openid-configuration"
        )
        self._http = httpx.AsyncClient(
            timeout=10.0,
            verify=ca_cert if ca_cert else True,
            transport=transport,
        )
        self._metadata: dict[str, Any] | None = None
        self.jwks = JwksCache(
            self._fetch_jwks_document, cache="oidc", ttl_seconds=OIDC_JWKS_TTL_SECONDS
        )

    @property
    def client_id(self) -> str:
        """Registered OAuth client id (audience check input)."""
        return self._client_id

    @property
    def redirect_url(self) -> str:
        """Registered callback URL."""
        return self._redirect_url

    async def metadata(self) -> dict[str, Any]:
        """Fetch and pin the discovery document (cached for the
        process lifetime — issuer metadata is deployment-stable)."""
        if self._metadata is None:
            response = await self._http.get(self._discovery_url)
            response.raise_for_status()
            document = response.json()
            advertised = str(document.get("issuer", "")).rstrip("/")
            if advertised != self._issuer:
                raise OidcExchangeError(
                    f"Discovery issuer {advertised!r} differs from "
                    f"configured issuer {self._issuer!r}."
                )
            self._metadata = document
        return self._metadata

    async def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str
    ) -> str:
        """Authorization-request URL (code flow, PKCE S256)."""
        document = await self.metadata()
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_url,
                "scope": self._scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{document['authorization_endpoint']}?{query}"

    async def exchange_code(
        self, *, code: str, code_verifier: str
    ) -> dict[str, Any]:
        """Redeem the authorization code at the token endpoint."""
        document = await self.metadata()
        response = await self._http.post(
            document["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_url,
                "code_verifier": code_verifier,
            },
            auth=(self._client_id, self._client_secret),
        )
        if response.status_code != 200:
            raise OidcExchangeError(
                f"Token endpoint returned status "
                f"{response.status_code}."
            )
        return response.json()

    async def validate_id_token(
        self, id_token: str, *, nonce: str
    ) -> dict[str, Any]:
        """Validate signature and claims; returns the claim set.

        Enforces the algorithm allowlist, the pinned issuer, the
        client id as audience, expiry with small leeway, and the
        transaction nonce.
        """
        try:
            keys = await self.jwks.keys_for(id_token)
        except KeysUnavailable as exc:
            raise OidcExchangeError(
                "The identity provider's signing keys are unavailable."
            ) from exc
        try:
            token = jose_jwt.decode(
                id_token, keys, algorithms=list(ALLOWED_ALGORITHMS)
            )
        except Exception as exc:
            raise OidcExchangeError(
                f"Invalid id_token signature: {exc}"
            ) from exc
        claims = dict(token.claims)
        if str(claims.get("iss", "")).rstrip("/") != self._issuer:
            raise OidcExchangeError("id_token issuer mismatch.")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self._client_id not in audiences:
            raise OidcExchangeError(
                "id_token audience does not include the client id."
            )
        expires_at = claims.get("exp")
        if not isinstance(expires_at, (int, float)) or (
            expires_at <= time.time() - 60
        ):
            raise OidcExchangeError("id_token has expired.")
        if claims.get("nonce") != nonce:
            raise OidcExchangeError("id_token nonce mismatch.")
        return claims

    async def userinfo(self, access_token: str) -> dict[str, Any]:
        """Fetch the userinfo endpoint (thin-token fallback)."""
        document = await self.metadata()
        endpoint = document.get("userinfo_endpoint")
        if not endpoint:
            return {}
        response = await self._http.get(
            endpoint, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code != 200:
            log.warning(
                "Userinfo endpoint returned status %d; claims come only "
                "from the id_token.",
                response.status_code,
            )
            return {}
        return response.json()

    async def _fetch_jwks_document(self) -> dict[str, Any]:
        """The JWKS document of the discovered ``jwks_uri``."""
        document = await self.metadata()
        response = await self._http.get(document["jwks_uri"])
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._http.aclose()


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for S256."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64

    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OidcAuthProvider(AuthProvider):
    """Session-cookie principal resolution for the OIDC BFF.

    Args:
        client: The relying-party client (login routes use it; the
            per-request path never talks to the IdP).
        sessions: Session store (memory default, Postgres opt-in).
        flows: Login-flow store.
        users: Optional local user mirror, upserted on every login
            (JIT provisioning keyed on the (issuer, subject) anchor).
        session_secret: CSRF-derivation secret.
        session_max_age_seconds: Absolute session lifetime.
        claim_mapping: Which claims carry the name, email, groups and
            roles, and the admission gates and admin values; the settings
            bridge builds it from the ``INQTRIX_OIDC_*`` claim settings
            (:func:`inqtrix.auth.api_key.claim_mapping_from_settings`).
            ``None`` uses the defaults.
        provider_name: Display name for the SSO login button (surfaced by
            the auth-config endpoint; empty falls back to a generic label).
        skip_email_verified: Accept logins whose token lacks
            ``email_verified=true`` (Entra ID omits the claim); logged as a
            warning at construction. A login without the claim carries no
            confirmed email and therefore accepts no workspace invitation;
            with ``INQTRIX_REGISTRATION=invite`` it creates no account.
        userinfo_fallback: Fetch userinfo when mapped claims are
            missing from the id_token.
        secure_cookies: ``False`` drops the ``Secure`` flag and the
            ``__Host-`` prefix for plain-HTTP loopback development;
            the activation is logged loudly at construction.
        pats: Optional personal-access-token verifier. When set, an
            ``Authorization: Bearer`` header routes EXCLUSIVELY to PAT
            verification — never falling through to the cookie, so a
            wrong token cannot silently ride an ambient session.
        pat_service: Optional token-management service backing the
            ``/api/auth/tokens`` routes; ``None`` leaves those routes
            unmounted.
    """

    def __init__(
        self,
        *,
        client: "OidcClient | None" = None,
        sessions: SessionStore,
        flows: FlowStore,
        users: "UserDirectory | None" = None,
        session_secret: str,
        session_max_age_seconds: int,
        claim_mapping: ClaimMappingConfig | None = None,
        provider_name: str = "",
        skip_email_verified: bool = False,
        userinfo_fallback: bool = True,
        secure_cookies: bool = True,
        pats: "PatVerifier | None" = None,
        pat_service: "PatService | None" = None,
        registration_gate: "RegistrationGate | None" = None,
        invitations: "InvitationRepository | None" = None,
        lifecycle: "UserLifecycleService | None" = None,
    ) -> None:
        if not session_secret:
            raise ValueError(
                f"{type(self).__name__} requires a non-empty session_secret"
            )
        self.client = client
        self.sessions = sessions
        self.flows = flows
        self.users = users
        self.pats = pats
        self.pat_service = pat_service
        self.registration_gate = registration_gate
        self.invitations = invitations
        if lifecycle is None and users is not None:
            raise RuntimeError(
                "Scoped OIDC/local auth requires an atomic user lifecycle"
            )
        self.lifecycle = lifecycle
        self.session_secret = session_secret
        self.session_max_age_seconds = session_max_age_seconds
        self.claim_mapping = (
            claim_mapping if claim_mapping is not None else ClaimMappingConfig()
        )
        self.provider_name = provider_name
        self.skip_email_verified = skip_email_verified
        if skip_email_verified:
            invite = (
                registration_gate is not None
                and registration_gate.registration == "invite"
            )
            log.warning(
                "OIDC email_verified check is explicitly disabled; missing "
                "or false claims are accepted.%s%s",
                _SKIP_WITH_INVITE if invite else "",
                _SKIP_WITH_DOMAINS if self.claim_mapping.allowed_domains else "",
            )
        self.userinfo_fallback = userinfo_fallback
        self.secure_cookies = secure_cookies
        if not secure_cookies:
            log.warning(
                "OIDC cookies use INSECURE development mode (no Secure flag "
                "or __Host- prefix); never use this in production."
            )

    def bind_pat_audit(self, audit: "AuditSink") -> None:
        """Bind PAT lifecycle and rejection events to the platform sink."""
        if self.pats is not None:
            self.pats.bind_audit(audit)
        if self.pat_service is not None:
            self.pat_service.bind_audit(audit)

    @property
    def mode(self) -> "AuthMode":
        """``"oidc"``."""
        return "oidc"

    @property
    def session_cookie(self) -> str:
        """Session-cookie name (``__Host-`` prefixed when secure)."""
        return (
            "__Host-inqtrix_session"
            if self.secure_cookies
            else "inqtrix_session"
        )

    @property
    def csrf_cookie(self) -> str:
        """CSRF-cookie name (readable by the SPA by design)."""
        return "__Host-inqtrix_csrf" if self.secure_cookies else "inqtrix_csrf"

    @property
    def username_claim(self) -> str:
        """Display-username claim (read-through to the claim mapping)."""
        return self.claim_mapping.username_claim

    @property
    def email_claim(self) -> str:
        """Email claim (read-through to the claim mapping)."""
        return self.claim_mapping.email_claim

    @property
    def groups_claim(self) -> str:
        """Group-membership claim (read-through to the claim mapping)."""
        return self.claim_mapping.groups_claim

    @property
    def roles_claim(self) -> str:
        """Role claim (read-through to the claim mapping)."""
        return self.claim_mapping.roles_claim

    @property
    def allowed_groups(self) -> frozenset[str]:
        """Group admission allowlist (read-through to the claim mapping)."""
        return self.claim_mapping.allowed_groups

    @property
    def admin_elevation_enabled(self) -> bool:
        """Whether any admin-from-claims elevation is configured.

        Gates the userinfo fetch for the roles claim: with no admin roles
        or groups configured the roles value provably changes nothing
        (:func:`~inqtrix.auth.mapping.derive_is_admin` short-circuits), so
        fetching it would be pure waste.
        """
        return bool(
            self.claim_mapping.admin_roles or self.claim_mapping.admin_groups
        )

    @property
    def invitations_accepted_by_login(self) -> bool:
        """Whether a login of this provider can accept a workspace invitation.

        Acceptance needs a confirmed email. With ``skip_email_verified`` the
        provider admits logins without one, so it does not promise
        acceptance; an instance admin assigns those people to workspaces.
        """
        return not self.skip_email_verified

    def resolve_principal(self, request: Request) -> Principal:
        """Unsupported synchronous path — session lookup is async."""
        raise RuntimeError(
            "OidcAuthProvider resolves principals asynchronously; use "
            "build_principal_dependency()."
        )

    def build_principal_dependency(self) -> Callable[..., Principal]:
        """Async dependency: Bearer -> PAT, else cookie -> session.

        An ``Authorization: Bearer`` header routes EXCLUSIVELY to PAT
        verification (no cookie fallback — a wrong token must fail,
        not silently ride an ambient session); the CSRF check stays
        inside the cookie branch, because PAT requests carry no
        cookie-bound ambient authority for a cross-site page to abuse.
        """

        async def resolve_session_principal(request: Request) -> Principal:
            bearer = self._bearer_value(request)
            if bearer is not None:
                return await self._bearer_principal(bearer)
            session = await self._session_for(request)
            self._require_csrf(request, session)
            return self._session_principal(session)

        return resolve_session_principal

    @staticmethod
    def _bearer_value(request: Request) -> str | None:
        """The token of an ``Authorization: Bearer`` header, or ``None``."""
        header = request.headers.get("Authorization", "").strip()
        if header.lower().startswith("bearer "):
            return header[len("Bearer "):]
        return None

    async def _bearer_principal(self, token: str) -> Principal:
        """The principal of a personal access token; never a cookie fallback."""
        if self.pats is None:
            raise HTTPException(
                status_code=401,
                detail=_UNAUTHENTICATED,
                headers={"WWW-Authenticate": "Bearer"},
            )
        principal = await self.pats.verify(token)
        bind_principal_context(principal)
        return principal

    def _require_csrf(self, request: Request, session: AuthSession) -> None:
        """Double-submit check on unsafe methods of a cookie session."""
        if request.method in _SAFE_METHODS:
            return
        token = request.headers.get(CSRF_HEADER, "")
        if not token or not verify_csrf_token(
            self.session_secret, session.id, token
        ):
            log.warning(
                "CSRF check failed "
                "(session_ref=%s method=%s).",
                pseudonymous_log_reference("ses", session.id),
                request.method,
            )
            raise HTTPException(status_code=403, detail=_CSRF_INVALID)

    def _session_principal(self, session: AuthSession) -> Principal:
        """The principal of a cookie session, bound to the log context."""
        principal = Principal(
            user_id=session.user_id,
            kind="oidc_session",
            tenant_id="default",
            display_name=session.display_name,
            email=session.email,
            session_id=session.id,
        )
        # Stamp the subject onto the log context (stable pseudonym,
        # never the raw id); the request task owns the binding.
        bind_principal_context(principal)
        return principal

    async def _session_and_user_for(
        self, request: Request
    ) -> tuple[AuthSession, "MirroredUser | None"]:
        """The cookie's session and its user row, from the same two lookups.

        A session whose user is gone or disabled is deleted and answers 401.
        The user row is ``None`` only without a user store.
        """
        session_id = request.cookies.get(self.session_cookie, "")
        if not session_id:
            raise HTTPException(status_code=401, detail=_UNAUTHENTICATED)
        session = await self.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=401, detail=_UNAUTHENTICATED)
        user = None
        if self.users is not None:
            user = await self.users.find_by_user_id(
                tenant_id="default", user_id=session.user_id
            )
            if user is None or user.disabled_at is not None:
                await self.sessions.delete(session_id)
                raise HTTPException(status_code=401, detail=_UNAUTHENTICATED)
        return session, user

    async def _session_for(self, request: Request) -> AuthSession:
        session, _user = await self._session_and_user_for(request)
        return session

    async def session_payload(self, request: Request) -> dict[str, Any]:
        """``/api/auth/session`` bootstrap payload for the SPA.

        A still-live session whose user was disabled mid-session reads as
        logged-out, so the SPA never renders a surface the disable cascade
        already revoked server-side.
        """
        try:
            session, mirrored = await self._session_and_user_for(request)
        except HTTPException:
            return {"authenticated": False}
        return await self.authenticated_payload(request, session, mirrored)

    async def authenticated_payload(
        self,
        request: Request,
        session: AuthSession,
        mirrored: "MirroredUser | None",
    ) -> dict[str, Any]:
        """The signed-in payload of *session*, with role and namespace."""
        # Instance role drives the admin-UI gate. Resolved from the mirror;
        # absent mirror (or no users store) reads as the default "user", so
        # an older backend simply yields no admin surface (fail-closed).
        role = "user"
        # The user's canonical project namespace (cross-device). Resolved from
        # the mirror; adopted from the browser's namespace header on first boot
        # unless a write without a namespace claimed one before
        # (see resolve_default_workspace). Stays None for an un-mirrored
        # identity or when no namespace header is present yet, so the SPA falls
        # back to its browser-local id.
        project_namespace: str | None = None
        if mirrored is not None and self.users is not None:
            role = mirrored.instance_role
            project_namespace = mirrored.default_workspace_id
            if project_namespace is None:
                candidate = workspace_id_from_request(request)
                if candidate:
                    project_namespace = await self.users.resolve_default_workspace(
                        tenant_id="default",
                        user_id=session.user_id,
                        candidate=candidate,
                    )
        return {
            "authenticated": True,
            "user": {
                "id": str(session.user_id),
                "email": session.email,
                "display_name": session.display_name,
                "role": role,
            },
            "project_namespace": project_namespace,
            "csrf_token": make_csrf_token(
                self.session_secret, session.id, session.csrf_random
            ),
        }

    def resolve_identity(
        self, claims: dict[str, Any]
    ) -> tuple[str, str | None, str | None, tuple[str, ...]]:
        """Map validated claims to ``(sub, email, display_name, groups)``.

        Group extraction and the admission gates (group allowlist with a
        ``"*"`` wildcard, email-domain allowlist) are delegated to the
        IdP-agnostic claim mapper; admin elevation is a separate,
        grant-only signal (see :meth:`map_admin`). ``display_name`` is
        ``None`` without a username claim: login provisioning falls back to
        the email or the subject only when the stored name is empty too.

        Raises:
            OidcExchangeError: When the subject is missing, the email is
                unverified (unless skipped), an admission gate rejects the
                login, or an admission-gating group claim is distributed
                and cannot be resolved — every rejection names its reason.
        """
        cfg = self.claim_mapping
        subject = str(claims.get("sub", "")).strip()
        if not subject:
            raise OidcExchangeError("id_token has no Subject (sub).")
        if (
            claims.get("email_verified") is not True
            and not self.skip_email_verified
        ):
            raise OidcExchangeError(
                "Email address is not verified by the identity provider."
            )
        email_value = claim_path(claims, cfg.email_claim)
        email = str(email_value) if email_value else None
        username = claim_path(claims, cfg.username_claim)
        display_name = str(username) if username else None
        groups = extract_groups(claims, cfg)
        reason = admission_error(groups, email, cfg)
        if reason:
            raise OidcExchangeError(reason)
        return subject, email, display_name, groups

    def map_admin(
        self, claims: dict[str, Any], groups: tuple[str, ...]
    ) -> bool:
        """Whether the IdP claims elevate this login to instance-admin.

        A grant-only signal: the caller promotes on True but never demotes
        on False — the admin surface owns ``instance_role`` and the
        last-admin guard. Reuses
        the already-resolved *groups* and reads the roles claim; a
        distributed roles claim degrades to "no elevation" with a visible
        warning, never a hard failure.
        """
        roles = extract_roles(claims, self.claim_mapping)
        return derive_is_admin(groups, roles, self.claim_mapping)
