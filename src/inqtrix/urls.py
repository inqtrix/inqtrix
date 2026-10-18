""" URL processing utilities."""

from __future__ import annotations

import datetime
import functools
import re
from typing import Any
from dataclasses import dataclass
from urllib.parse import (
    SplitResult,
    quote,
    unquote,
    unquote_plus,
    urlparse,
    urlsplit,
    urlunsplit,
)

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BROKEN_BARE_MARKDOWN_URL_RE = re.compile(r"\[\s*https?://[^\]\s)]+(?:\)|\])?")


class CredentialBearingUrlError(ValueError):
    """Raised when a public URL itself carries authentication material.

    Search providers and public pages are untrusted inputs.  A credential in
    userinfo or a query parameter must therefore never become a durable
    source identity, a follow-up fetch target, or a user-visible failure
    string.  The exception deliberately contains no URL or parameter value.
    """


@dataclass(frozen=True, slots=True)
class SafePublicUrlIdentity:
    """Validated HTTP(S) identity shared by discovery and source reading."""

    url: str
    canonical_url: str
    origin: tuple[str, str, int]


_CREDENTIAL_KEY_EXACT = frozenset(
    {
        "apikey",
        "xapikey",
        "key",
        "accesskey",
        "accesskeyid",
        "awsaccesskeyid",
        "subscriptionkey",
        "clientkey",
        "privatekey",
        "token",
        "accesstoken",
        "authtoken",
        "bearertoken",
        "idtoken",
        "refreshtoken",
        "sessiontoken",
        "secret",
        "clientsecret",
        "apisecret",
        "signature",
        "sig",
        "xamzsignature",
        "xgoogsignature",
        "password",
        "passwd",
        "pwd",
        "credential",
        "xamzcredential",
    }
)
_CREDENTIAL_KEY_MARKERS = (
    "apikey",
    "accesskey",
    "privatekey",
    "subscriptionkey",
    "accesstoken",
    "authtoken",
    "bearertoken",
    "idtoken",
    "refreshtoken",
    "sessiontoken",
    "secret",
    "signature",
    "password",
    "passwd",
    "credential",
)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_URL_START_RE = re.compile(r"https?://", re.IGNORECASE)
_URL_COMPONENT_SAFE = "/:@!$&'()*+,;=-._~%?"
_MAX_URL_DECODE_ROUNDS = 3


def is_credential_query_key(key: str) -> bool:
    """Return whether a decoded query key denotes authentication material.

    Separators and casing are ignored so spellings such as ``client_secret``,
    ``client-secret``, ``X-Api-Key`` and ``X-Amz-Signature`` have the same
    security meaning.  A lone ``key`` is treated as sensitive: public APIs
    commonly use exactly that spelling for access credentials.
    """

    compact = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    if not compact:
        return False
    return compact in _CREDENTIAL_KEY_EXACT or any(
        marker in compact for marker in _CREDENTIAL_KEY_MARKERS
    )


def _split_url_without_credentials(url: str) -> tuple[SplitResult, bool]:
    """Return a parsed URL and whether it contains credential material."""

    split = urlsplit(str(url).strip())
    credential_bearing = split.username is not None or split.password is not None
    for parameter_string in (split.query, split.fragment):
        for component in re.split(r"[&;]", parameter_string):
            raw_key, _separator, _value = component.partition("=")
            if is_credential_query_key(unquote_plus(raw_key)):
                credential_bearing = True
                break
    return split, credential_bearing


def _encoded_public_url(url: str) -> str:
    """Percent-encode whitespace/control characters without hiding syntax."""

    split = urlsplit(str(url).strip())
    path = quote(split.path, safe=_URL_COMPONENT_SAFE)
    query = quote(split.query, safe=_URL_COMPONENT_SAFE)
    return urlunsplit((split.scheme, split.netloc, path, query, split.fragment))


def _embedded_public_url(value: str) -> bool:
    """Detect a second literal or repeatedly percent-encoded HTTP(S) URL."""

    decoded = str(value)
    for _round in range(_MAX_URL_DECODE_ROUNDS + 1):
        starts = list(_URL_START_RE.finditer(decoded))
        if len(starts) > 1:
            return True
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return False


def safe_public_url_identity(url: str) -> SafePublicUrlIdentity:
    """Validate and canonicalize a credential-free public HTTP(S) URL.

    Network-address safety remains the responsibility of the egress guard,
    which resolves and pins DNS immediately before a connection.  This helper
    owns the orthogonal durable-identity contract and is intentionally used
    before a URL enters an evidence ledger.
    """

    try:
        raw = str(url).strip()
        if _embedded_public_url(raw):
            raise ValueError(
                "public source URL must not contain another HTTP(S) URL"
            )
        encoded = _encoded_public_url(raw)
        split, credential_bearing = _split_url_without_credentials(encoded)
        scheme = str(split.scheme).casefold()
        host = split.hostname
        if scheme not in {"http", "https"} or not host:
            raise ValueError("public source URL must use HTTP(S) and include a host")
        if any(
            character.isspace() or ord(character) < 32
            for character in split.netloc
        ):
            raise ValueError("public source URL contains invalid host characters")
        if any(character in "\"'<>\\\\" for character in host):
            raise ValueError("public source URL contains invalid host characters")
        if credential_bearing:
            raise CredentialBearingUrlError(
                "credential-bearing public source URL is not allowed"
            )
        port = split.port or (443 if scheme == "https" else 80)
    except CredentialBearingUrlError:
        raise
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid public source URL") from exc

    cleaned = urlunsplit((scheme, split.netloc, split.path, split.query, ""))
    return SafePublicUrlIdentity(
        url=cleaned,
        canonical_url=normalize_url(cleaned),
        origin=(scheme, host.casefold().rstrip("."), port),
    )


def redact_credential_url(url: str) -> str:
    """Return a persistence-safe URL without userinfo or secret values.

    Benign query parameters remain available for diagnostics.  Credential
    keys remain named, but their values are replaced so operators can see why
    a read was blocked without exposing the credential itself.
    """

    try:
        split = urlsplit(str(url).strip())
        host = split.hostname or ""
        if not host:
            return "[REDACTED_URL]"
        host_text = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if split.port is not None:
            host_text += f":{split.port}"

        def redact_parameters(value: str) -> str:
            parts = re.split(r"([&;])", value)
            for index in range(0, len(parts), 2):
                component = parts[index]
                raw_key, separator, _raw_value = component.partition("=")
                if separator and is_credential_query_key(unquote_plus(raw_key)):
                    parts[index] = f"{raw_key}=[REDACTED]"
            return "".join(parts)

        query = redact_parameters(split.query)
        fragment = redact_parameters(split.fragment)
        return urlunsplit((split.scheme, host_text, split.path, query, fragment))
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def scrub_credential_urls(text: str) -> str:
    """Redact credentials in every HTTP(S) URL embedded in free text."""

    return _URL_IN_TEXT_RE.sub(
        lambda match: redact_credential_url(match.group(0)),
        str(text),
    )


def today() -> str:
    """Current date as string."""
    return datetime.date.today().strftime("%d. %B %Y")


def normalize_url(url: str) -> str:
    """Normalize URL for better deduplication.

    Removes trailing slashes, fragments, and tracking parameters.
    """
    url = str(url).strip()
    url = url.rstrip(".,;:!?")
    while url.endswith("'") and url.count("'") % 2:
        url = url[:-1]
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    url = re.sub(r'#[^?]*$', '', url)
    if url.count('/') > 3:
        url = url.rstrip('/')
    url = re.sub(r'[?&](utm_[a-z]+|ref|source|fbclid|gclid)=[^&]*', '', url)
    url = url.rstrip('?')
    return url


def _scan_url_candidate(text: str, start: int) -> str:
    """Read one angle, Markdown, or bare URL beginning at ``start``."""

    angle_wrapped = start > 0 and text[start - 1] == "<"
    markdown_target = (
        start > 1
        and text[start - 1] == "("
        and text[start - 2] == "]"
    )
    if angle_wrapped:
        end = text.find(">", start)
        return text[start:] if end < 0 else text[start:end]

    allow_spaces = markdown_target
    parenthesis_depth = 0
    end = start
    while end < len(text):
        character = text[end]
        if character.isspace() and not allow_spaces:
            break
        if character in '<>"[]{}':
            break
        if character == "(":
            parenthesis_depth += 1
        elif character == ")":
            if parenthesis_depth == 0:
                break
            parenthesis_depth -= 1
        end += 1
    return text[start:end]


def _decoded_embedded_url_text(candidate: str) -> str:
    """Return the decoded second-URL suffix, or ``""`` when absent."""

    decoded = candidate
    for _round in range(_MAX_URL_DECODE_ROUNDS):
        next_value = unquote(decoded)
        if next_value == decoded:
            return ""
        decoded = next_value
        starts = list(_URL_START_RE.finditer(decoded))
        if len(starts) > 1:
            return decoded[starts[1].start():]
    return ""


def extract_urls(text: str, *, limit: int | None = None) -> list[str]:
    """Extract ordered public-URL candidates from prose.

    Angle links and Markdown destinations may contain legal spaces (for
    example in an OData expression); those characters are percent-encoded.
    Concatenated literal URLs are split.  When a second URL is percent-encoded
    inside a wrapper URL, only the decoded target is returned so the compound
    identity can never enter persistence.
    """

    if limit is not None and int(limit) <= 0:
        return []
    value = str(text or "")
    seen: set[str] = set()
    unique: list[str] = []
    pending: list[str] = []
    for match in _URL_START_RE.finditer(value):
        candidate = _scan_url_candidate(value, match.start()).strip()
        if not candidate:
            continue
        decoded_inner = _decoded_embedded_url_text(candidate)
        if decoded_inner:
            pending.extend(extract_urls(decoded_inner, limit=limit))
            continue
        direct_inner = _URL_START_RE.search(candidate, len(match.group(0)))
        if direct_inner is not None:
            candidate = candidate[: direct_inner.start()]
        pending.append(candidate)

    for candidate in pending:
        try:
            encoded = _encoded_public_url(candidate)
            split = urlsplit(encoded)
            if (
                split.scheme.casefold() not in {"http", "https"}
                or not split.hostname
            ):
                continue
            # Accessing port catches malformed netlocs such as ``:not-a-port``.
            _ = split.port
        except (TypeError, ValueError):
            continue
        normalized = normalize_url(encoded)
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
            if limit is not None and len(unique) >= max(0, int(limit)):
                break
    return unique


def sanitize_answer_links(answer: str, allowed_urls: set[str]) -> tuple[str, int]:
    """Remove non-allowed markdown links from the final answer."""
    if not answer or not allowed_urls:
        return answer, 0

    removed = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal removed
        label = m.group(1)
        url = m.group(2)
        normalized = normalize_url(url)
        if normalized in allowed_urls:
            return f"[{label}]({normalized})"
        removed += 1
        return label if re.search(r"[A-Za-z]", label) else ""

    sanitized = _MARKDOWN_LINK_RE.sub(_repl, answer)
    sanitized = _BROKEN_BARE_MARKDOWN_URL_RE.sub("", sanitized)
    return sanitized, removed


def count_allowed_links(answer: str, allowed_urls: set[str]) -> int:
    """Count unique allowed markdown links in the answer."""
    if not answer or not allowed_urls:
        return 0
    seen: set[str] = set()
    for m in _MARKDOWN_LINK_RE.finditer(answer):
        normalized = normalize_url(m.group(2))
        if normalized in allowed_urls:
            seen.add(normalized)
    return len(seen)


def domain_from_url(url: str) -> str:
    """Extract domain from URL (without www prefix)."""
    try:
        host = (urlparse(url).hostname or "").lower().strip()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


@functools.cache
def _public_suffix_extractor():  # type: ignore[no-untyped-def]
    """The offline public-suffix extractor, built on first use.

    ``tldextract`` (and the ``requests`` stack it imports) is loaded lazily:
    only a process that consolidates claims pays the import. The list is the
    snapshot bundled with the pinned release (``suffix_list_urls=()``,
    never fetched), parsed in memory (``cache_dir=None``: no lock file, no
    write under the user's home), and includes the PSL's private section so
    shared-hosting suffixes (``github.io``, ``blogspot.com``) separate their
    users' sites instead of collapsing them into one publisher.
    """
    import tldextract

    return tldextract.TLDExtract(
        suffix_list_urls=(),
        fallback_to_snapshot=True,
        cache_dir=None,
        include_psl_private_domains=True,
    )


def public_suffixes() -> frozenset[str]:
    """Every suffix of the bundled public suffix list (ICANN and private)."""
    return frozenset(_public_suffix_extractor().tlds)


@functools.lru_cache(maxsize=8192)
def registrable_domain(url: str) -> str:
    """Return the registrable domain (eTLD+1) of ``url``, lower-cased.

    ``foerderung.example.gv.at`` -> ``example.gv.at``; ``www.kfw.de`` ->
    ``kfw.de``; ``ecb.europa.eu`` -> ``europa.eu`` (the public suffix list
    treats ``europa.eu`` as one registration); ``alice.github.io`` ->
    ``alice.github.io`` (private suffix). Two subdomains of one registration
    are ONE publisher for independence counting. Falls back to
    :func:`domain_from_url` when the host has no public suffix (an IP, an
    intranet name).
    """
    host = domain_from_url(url)
    if not host:
        return ""
    parts = _public_suffix_extractor()(host)
    registered = str(parts.top_domain_under_public_suffix or "").lower()
    return registered or host


def matching_suffix(domain: str, candidates: Any) -> str:
    """Return the longest suffix of ``domain`` contained in ``candidates``.

    ``candidates`` is any container of lower-cased suffixes (a set, or a
    mapping whose keys are suffixes). ``"www.info.gov.hk"`` against
    ``{"gov.hk", "hk"}`` -> ``"gov.hk"``; the domain itself counts as its own
    suffix. ``""`` when nothing matches.
    """
    if not domain:
        return ""
    labels = domain.lower().strip().split(".")
    for start in range(len(labels)):
        suffix = ".".join(labels[start:])
        if suffix in candidates:
            return suffix
    return ""


_EDITOR_GUEST_ROUTE_TOKEN_RE = re.compile(
    r"(?i)(/s/|/v1/editor/share-links/)[^/?#\s\"'<>]+"
)


def _scrub_credentials(msg: str) -> str:
    """Common credential-scrubbing rules used by the log filter and error helper.

    Removes API keys, bearer tokens, AWS access keys, and credential values
    inside URL query parameters. URLs themselves are intentionally NOT replaced
    by ``[URL]`` — final answers, citation lists, and trace logs need to keep
    their links intact for debugging.
    """
    # Editor guest-link path segments are bearer credentials rather than
    # ordinary identifiers. Keep the route visible for diagnostics while
    # removing the complete token from access, proxy, and exception logs.
    msg = _EDITOR_GUEST_ROUTE_TOKEN_RE.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        msg,
    )
    # Per-URL credential redaction first so that the surrounding URL stays visible.
    msg = scrub_credential_urls(msg)
    # URL userinfo credentials (redis://:pw@..., postgresql+asyncpg://
    # user:pw@host/db) — connection strings travel inside driver
    # exceptions and would otherwise leak broker/database passwords.
    msg = re.sub(
        r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^@/\s\"'<>]*@",
        r"\1[REDACTED]@",
        msg,
    )
    msg = re.sub(r"(sk-|pplx-)[a-zA-Z0-9_\-]{16,}", "[KEY]", msg)
    msg = re.sub(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [REDACTED]", msg)
    msg = re.sub(r"AKIA[A-Z0-9]{16}", "[AWS_KEY]", msg)
    msg = re.sub(
        r"(?i)(aws_secret_access_key|aws_session_token)[=:]\s*\S+",
        r"\1=[REDACTED]",
        msg,
    )
    return msg


def sanitize_log_message(message: str | Exception) -> str:
    """Scrub credentials from a log message while keeping benign URLs intact.

    Designed for the centralized logging filter. Differs from
    :func:`sanitize_error` only in intent and naming — both currently share
    the same credential-scrubbing rules. Call this from log handlers; call
    ``sanitize_error`` from explicit error-formatting code paths.
    """
    return _scrub_credentials(str(message))


def sanitize_error(error: str | Exception) -> str:
    """Strip sensitive data from error messages.

    Use for explicit error stringification (HTTP responses, stderr prints,
    user-visible failure dialogs). Credentials are removed; URLs themselves
    are kept so support tickets carry the failing endpoint context.
    """
    return _scrub_credentials(str(error))
