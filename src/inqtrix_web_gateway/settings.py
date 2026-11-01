"""Typed configuration boundary for the Inqtrix web gateway.

This module depends only on the standard library. Runtime adapters consume
validated values from here; they do not read environment variables directly.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

log = logging.getLogger("inqtrix.web_gateway")

_PROXY_BODY_HEADROOM_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
_DEFAULT_MAX_REQUEST_BYTES = _DEFAULT_MAX_FILE_BYTES + _PROXY_BODY_HEADROOM_BYTES
# Sized against the API's own admission caps, not guessed: chat and native
# runs each admit up to 100 by default, and every open event stream holds one
# upstream connection for the run's whole duration on top of that. At 200 the
# shipped caps alone could fill the pool, and the next request -- a page load,
# a quota poll -- gets a 503 rather than waiting. httpx opens connections on
# demand, so a higher ceiling costs nothing until the load actually arrives.
_DEFAULT_MAX_UPSTREAM_CONNECTIONS = 512

_COLLABORATION_MIN_FRAME_BYTES = 65_536
_COLLABORATION_MAX_FRAME_BYTES = 16 * 1_048_576
_COLLABORATION_DEFAULT_FRAME_BYTES = 2 * 1_048_576
_COLLABORATION_MIN_QUEUED_FRAMES = 1
_COLLABORATION_MAX_QUEUED_FRAMES = 256
_COLLABORATION_DEFAULT_QUEUED_FRAMES = 32


class CollaborationProxySettings:
    """Dependency-free WebSocket transport limits for the public edge."""

    __slots__ = ("max_frame_bytes", "max_queued_frames")

    def __init__(
        self,
        *,
        max_frame_bytes: int = _COLLABORATION_DEFAULT_FRAME_BYTES,
        max_queued_frames: int = _COLLABORATION_DEFAULT_QUEUED_FRAMES,
    ) -> None:
        if not (
            _COLLABORATION_MIN_FRAME_BYTES
            <= max_frame_bytes
            <= _COLLABORATION_MAX_FRAME_BYTES
        ):
            raise ValueError(
                "INQTRIX_COLLABORATION_MAX_FRAME_BYTES is outside the "
                "CollaborationSettings bounds"
            )
        if not (
            _COLLABORATION_MIN_QUEUED_FRAMES
            <= max_queued_frames
            <= _COLLABORATION_MAX_QUEUED_FRAMES
        ):
            raise ValueError(
                "INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES must be between "
                f"{_COLLABORATION_MIN_QUEUED_FRAMES} and "
                f"{_COLLABORATION_MAX_QUEUED_FRAMES}"
            )
        self.max_frame_bytes = max_frame_bytes
        self.max_queued_frames = max_queued_frames

    @classmethod
    def from_env(cls) -> "CollaborationProxySettings":
        """Build the collaboration transport contract from canonical env."""
        frame_bytes = _parse_optional_int(
            os.getenv("INQTRIX_COLLABORATION_MAX_FRAME_BYTES"),
            env_var="INQTRIX_COLLABORATION_MAX_FRAME_BYTES",
        )
        queued_frames = _parse_optional_int(
            os.getenv("INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES"),
            env_var="INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES",
        )
        return cls(
            max_frame_bytes=(
                frame_bytes
                if frame_bytes is not None
                else _COLLABORATION_DEFAULT_FRAME_BYTES
            ),
            max_queued_frames=(
                queued_frames
                if queued_frames is not None
                else _COLLABORATION_DEFAULT_QUEUED_FRAMES
            ),
        )


#: The identity, proof and assertion header names of the gateway mode; the
#: same variables the API server reads. The shared secret is not among them:
#: the API server checks the proof, the edge only needs the names.
_IDENTITY_HEADER_VARIABLES = (
    "INQTRIX_GATEWAY_JWT_HEADER",
    "INQTRIX_GATEWAY_SUBJECT_HEADER",
    "INQTRIX_GATEWAY_EMAIL_HEADER",
    "INQTRIX_GATEWAY_NAME_HEADER",
    "INQTRIX_GATEWAY_GROUPS_HEADER",
    "INQTRIX_GATEWAY_PROOF_HEADER",
)


#: Header names a resolver of its own may never read: credentials, routing,
#: the hop-by-hop headers and what the edge passes on unchanged. A test keeps
#: this equal to the API server's rule.
_RESOLVER_FORBIDDEN_HEADERS = frozenset(
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
        "x-csrf-token",
        "x-inqtrix-expected-user-id",
        "x-inqtrix-workspace-id",
        "x-inqtrix-guest-csrf",
        "origin",
    }
)

#: An HTTP field name (a token of RFC 9110).
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _strippable(
    entries: Iterable[str], *, proof_header: str = ""
) -> tuple[frozenset[str], list[str], list[str]]:
    """The names the edge may strip, then the entries it refuses, as written.

    A name the edge must leave alone (credentials, routing, the forwarded
    chain, the CSRF, generation, workspace and guest headers, ``Origin``) is
    never stripped, nor is *proof_header*, which the list of a resolver of
    its own may not name; those come second. Entries that are no header name
    come third.
    """
    names: set[str] = set()
    refused: list[str] = []
    invalid: list[str] = []
    proof = proof_header.strip().lower()
    for entry in entries:
        written = entry.strip()
        if not written:
            continue
        # The token rule applies to the name as written: lowering first would
        # turn a Kelvin sign into an ASCII k.
        if not _HEADER_TOKEN.match(written):
            invalid.append(written)
            continue
        name = written.lower()
        if (
            name in _RESOLVER_FORBIDDEN_HEADERS
            or name.startswith("x-forwarded-")
            or (proof and name == proof)
        ):
            refused.append(written)
            continue
        names.add(name)
    return frozenset(names), refused, invalid


def _refusal(source: str, reason: str, refused: list[str], invalid: list[str]) -> str | None:
    """The readiness problem with the entries of *source* the edge refuses, or ``None``."""
    parts = []
    if refused:
        parts.append(f"{source} {reason}: " + ", ".join(dict.fromkeys(refused)))
    if invalid:
        # ascii() shows a lookalike, such as a Kelvin sign for a K, for what it is.
        parts.append(
            f"these entries of {source} are no header name: "
            + ", ".join(ascii(entry) for entry in dict.fromkeys(invalid))
        )
    return "; ".join(parts) or None


class IdentityEdgePolicy:
    """The identity headers of the gateway mode and the peers that may send them.

    ``INQTRIX_GATEWAY_TRUSTED_PROXIES`` of this process lists the peers whose
    identity headers reach the API server (the API gateway in front of this
    edge). An empty list removes nothing, which only the token assertion
    allows, because its signature is the proof; with the header assertion
    the edge stays unready without a list. The subject and proof header names
    are kept apart because the header assertion needs both at the edge; the
    names a resolver of its own reads (``INQTRIX_GATEWAY_RESOLVER_HEADERS``)
    are kept apart for its readiness.

    Raises:
        ValueError: An entry is no IP network, or one trusts every address.
    """

    __slots__ = (
        "header_names",
        "proof_header",
        "refused_header_problem",
        "resolver_header_names",
        "resolver_header_problem",
        "subject_header",
        "trusted_networks",
    )

    def __init__(
        self,
        *,
        header_names: Iterable[str] = (),
        trusted_proxies: str = "",
        subject_header: str = "",
        proof_header: str = "",
        resolver_headers: str = "",
    ) -> None:
        self.subject_header = subject_header.strip().lower()
        self.proof_header = proof_header.strip().lower()
        self.resolver_header_names, refused, invalid = _strippable(
            resolver_headers.split(","), proof_header=proof_header
        )
        self.resolver_header_problem = _refusal(
            "INQTRIX_GATEWAY_RESOLVER_HEADERS",
            "names headers a resolver of its own may not read (credentials, routing, "
            "the proof header or a header the edge passes on unchanged)",
            refused,
            invalid,
        )
        named, refused, invalid = _strippable((*header_names, subject_header, proof_header))
        self.refused_header_problem = _refusal(
            "the identity header variables (INQTRIX_GATEWAY_*_HEADER)",
            "name headers that carry credentials or routing or that the edge passes on "
            "unchanged, which it never strips",
            refused,
            invalid,
        )
        self.header_names = named | self.resolver_header_names
        networks = []
        for entry in trusted_proxies.split(","):
            if not entry.strip():
                continue
            try:
                network = ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError:
                raise ValueError(
                    "INQTRIX_GATEWAY_TRUSTED_PROXIES must list IP networks in "
                    "CIDR form"
                ) from None
            if network.prefixlen == 0:
                raise ValueError(
                    "INQTRIX_GATEWAY_TRUSTED_PROXIES must not trust every address"
                )
            networks.append(network)
        self.trusted_networks = tuple(networks)

    @classmethod
    def from_env(cls) -> "IdentityEdgePolicy":
        """Read the header names and this process's peer list."""
        return cls(
            header_names=[os.getenv(name, "") for name in _IDENTITY_HEADER_VARIABLES],
            trusted_proxies=os.getenv("INQTRIX_GATEWAY_TRUSTED_PROXIES", ""),
            subject_header=os.getenv("INQTRIX_GATEWAY_SUBJECT_HEADER", ""),
            proof_header=os.getenv("INQTRIX_GATEWAY_PROOF_HEADER", ""),
            resolver_headers=os.getenv("INQTRIX_GATEWAY_RESOLVER_HEADERS", ""),
        )

    def trusts(self, peer: str | None) -> bool:
        """Whether the socket peer lies in the trusted list."""
        if peer is None:
            return False
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(address in network for network in self.trusted_networks)

    def strips(self, peer: str | None) -> bool:
        """Whether a request from *peer* loses its identity headers."""
        return (
            bool(self.header_names)
            and bool(self.trusted_networks)
            and not self.trusts(peer)
        )


class _PublicOrigin(NamedTuple):
    """Validated public origin used to overwrite forwarding metadata."""

    scheme: str
    authority: str


def _hostname_is_unsafe(hostname: str) -> bool:
    """Reject controls, whitespace, and URL delimiters inside a hostname."""
    return any(
        character.isspace()
        or ord(character) < 33
        or character in "/?#@[]"
        for character in hostname
    )


def _parse_public_origin(value: str | None) -> _PublicOrigin | None:
    """Validate an optional public HTTP(S) origin."""
    if value is None or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "INQTRIX_PUBLIC_BASE_URL must be an absolute HTTP(S) origin"
        ) from exc
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or _hostname_is_unsafe(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "INQTRIX_PUBLIC_BASE_URL must be an absolute HTTP(S) origin"
        )
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    default_port = 443 if scheme == "https" else 80
    authority = (
        normalized_host
        if port is None or port == default_port
        else f"{normalized_host}:{port}"
    )
    return _PublicOrigin(scheme=scheme, authority=authority)


def _parse_external_scheme(value: str | None) -> str | None:
    """Validate an optional scheme-only forwarding override."""
    if value is None or not value.strip():
        return None
    scheme = value.strip().lower()
    if scheme not in {"http", "https"}:
        raise ValueError("INQTRIX_EXTERNAL_SCHEME must be http or https")
    return scheme


def _parse_optional_int(raw_value: str | None, *, env_var: str) -> int | None:
    """Parse an optional integer with an operator-facing named error."""
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return int(raw_value.strip())
    except ValueError as exc:
        raise ValueError(f"{env_var} must be an integer") from exc


def _validate_positive_int(value: int | None, *, env_var: str) -> int | None:
    """Validate an optional positive integer, rejecting booleans."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{env_var} must be a positive integer")
    return value


def _resolve_dist_dir() -> Path:
    """Resolve the SPA build directory and fail loudly when it is absent."""
    explicit = os.getenv("INQTRIX_DIST_DIR", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
    else:
        repo_root = Path(__file__).resolve().parents[2]
        path = (repo_root / "apps" / "research-desk" / "dist").resolve()
    if not path.is_dir():
        raise RuntimeError(
            f"dist/ not found at {path}. Run `npm run ui:build` first, or "
            "set INQTRIX_DIST_DIR to point at an existing build."
        )
    index_path = path / "index.html"
    try:
        with index_path.open("rb") as index_file:
            index_file.read(1)
    except OSError as exc:
        raise RuntimeError(
            f"dist/index.html is missing or unreadable at {index_path}. "
            "Run `npm run ui:build` first, or set INQTRIX_DIST_DIR to a "
            "complete build."
        ) from exc
    return path


def _validate_web_adapter() -> None:
    """Reject an image/Compose adapter mismatch before opening a socket."""
    adapter = os.getenv("INQTRIX_WEB_ADAPTER", "python").strip().lower()
    if adapter != "python":
        raise ValueError(
            "the Python web image requires INQTRIX_WEB_ADAPTER=python"
        )


def _resolve_backend_url() -> str:
    """Resolve the canonical backend origin."""
    canonical = os.getenv("INQTRIX_BACKEND_URL", "").strip()
    return _parse_backend_origin(canonical or "http://localhost:5100")


def _parse_backend_origin(value: str) -> str:
    """Return one credential-free absolute HTTP(S) backend origin.

    The value is shared by the HTTP and WebSocket adapters and may appear in
    startup diagnostics. Rejecting userinfo, paths, queries, and fragments
    here prevents ambiguous routing and keeps credentials out of downstream
    client exceptions and logs.
    """
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "INQTRIX_BACKEND_URL must be an absolute HTTP(S) origin"
        ) from exc
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or _hostname_is_unsafe(hostname)
    ):
        raise ValueError(
            "INQTRIX_BACKEND_URL must be an absolute HTTP(S) origin"
        )
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    default_port = 443 if scheme == "https" else 80
    authority = (
        normalized_host
        if port is None or port == default_port
        else f"{normalized_host}:{port}"
    )
    return f"{scheme}://{authority}"


def _resolve_proxy_max_body_bytes() -> int | None:
    """Resolve the proxy cap from canonical byte-valued configuration."""
    override = _validate_positive_int(
        _parse_optional_int(
            os.getenv("INQTRIX_PROXY_MAX_BODY_BYTES"),
            env_var="INQTRIX_PROXY_MAX_BODY_BYTES",
        ),
        env_var="INQTRIX_PROXY_MAX_BODY_BYTES",
    )
    if override is not None:
        return override
    max_file_bytes = _validate_positive_int(
        _parse_optional_int(
            os.getenv("INQTRIX_MAX_FILE_BYTES"),
            env_var="INQTRIX_MAX_FILE_BYTES",
        ),
        env_var="INQTRIX_MAX_FILE_BYTES",
    )
    if max_file_bytes is None:
        log.warning(
            "INQTRIX_MAX_FILE_BYTES is not set in the gateway environment; "
            "the request-body cap falls back to the packaged default "
            "(%d bytes) and may not match the backend's upload limit. "
            "Mirror INQTRIX_MAX_FILE_BYTES here or set "
            "INQTRIX_PROXY_MAX_BODY_BYTES explicitly.",
            _DEFAULT_MAX_REQUEST_BYTES,
        )
        return None
    return max_file_bytes + _PROXY_BODY_HEADROOM_BYTES


def _resolve_ssl_options() -> dict[str, str]:
    """Resolve a both-or-none direct-TLS certificate contract."""
    certfile = os.getenv("RESEARCH_DESK_SSL_CERTFILE", "").strip()
    keyfile = os.getenv("RESEARCH_DESK_SSL_KEYFILE", "").strip()
    if bool(certfile) != bool(keyfile):
        raise ValueError(
            "RESEARCH_DESK_SSL_CERTFILE and RESEARCH_DESK_SSL_KEYFILE must "
            "be set together"
        )
    if not certfile:
        return {}
    options: dict[str, str] = {
        "ssl_certfile": certfile,
        "ssl_keyfile": keyfile,
    }
    password = os.getenv("RESEARCH_DESK_SSL_KEYFILE_PASSWORD", "")
    if password:
        options["ssl_keyfile_password"] = password
    return options


def _resolve_bind_host() -> str:
    """Resolve and validate the bind host."""
    host = os.getenv("RESEARCH_DESK_HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("RESEARCH_DESK_HOST must not be empty")
    return host


def _resolve_bind_port() -> int:
    """Resolve and validate the bind port."""
    port = _parse_optional_int(
        os.getenv("RESEARCH_DESK_PORT", "8080"),
        env_var="RESEARCH_DESK_PORT",
    )
    assert port is not None
    if not 1 <= port <= 65_535:
        raise ValueError("RESEARCH_DESK_PORT must be between 1 and 65535")
    return port


def _resolve_worker_count() -> int:
    """Resolve the explicit worker count independently of WEB_CONCURRENCY."""
    return (
        _validate_positive_int(
            _parse_optional_int(
                os.getenv("RESEARCH_DESK_WORKERS"),
                env_var="RESEARCH_DESK_WORKERS",
            ),
            env_var="RESEARCH_DESK_WORKERS",
        )
        or 1
    )
