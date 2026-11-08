"""Extra HTTP headers an operator adds to every call of a provider.

``INQTRIX_LLM_EXTRA_HEADERS``, ``INQTRIX_SEARCH_EXTRA_HEADERS``,
``INQTRIX_EMBEDDING_EXTRA_HEADERS`` and ``INQTRIX_RERANKER_EXTRA_HEADERS`` hold
a JSON object of header names to text values, for example the subscription
header of an API gateway in front of the provider. The values may be secrets, so no
message here ever contains one; the names are not secret and may be logged.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping

log = logging.getLogger(__name__)

# RFC 9110 field name (token) and a value of visible ASCII, space or tab.
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_VALUE = re.compile(r"[\t\x20-\x7e]*")

RESERVED_HEADERS = frozenset(
    {
        # Authentication comes from the provider's key setting.
        "authorization",
        "proxy-authorization",
        "api-key",
        "x-api-key",
        # Framing belongs to the HTTP client.
        "host",
        "connection",
        "content-length",
        "content-type",
        "transfer-encoding",
    }
)
"""Header names an operator cannot set, compared without case."""


class _RepeatedName(Exception):
    """A JSON object that names one key twice (not a ``ValueError``: no JSON syntax error)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name


def _refuse_repeated_names(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for name, _ in pairs:
        if name in seen:
            raise _RepeatedName(name)
        seen.add(name)
    return dict(pairs)


def configured_headers(raw: str, *, setting: str, provider: str) -> dict[str, str]:
    """The checked headers of *setting* for a provider built at startup.

    The log names the headers, never their values; an invalid setting stops
    the start like a missing credential.

    Raises:
        RuntimeError: If the setting is not a valid object of headers.
    """
    try:
        headers = parse_extra_headers(raw, setting=setting)
    except ValueError as exc:
        log.warning("CONFIG: %s Failing loudly.", exc)
        raise RuntimeError(str(exc)) from None
    if headers:
        log.info(
            "CONFIG: %s adds %d header(s) to every %s call: %s",
            setting,
            len(headers),
            provider,
            ", ".join(headers),
        )
    return headers


def parse_extra_headers(raw: str, *, setting: str) -> dict[str, str]:
    """The checked headers of one setting; ``{}`` when the setting is empty.

    Args:
        raw: The setting's text, a JSON object of header names to text
            values.
        setting: The variable name, for the error message.

    Returns:
        The headers in the operator's spelling.

    Raises:
        ValueError: Naming *setting* and the problem, never a value.
    """
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw, object_pairs_hook=_refuse_repeated_names)
    except _RepeatedName as repeated:
        # JSON keeps only the last of two equal names; the first would vanish.
        named = repeated.name if _NAME.fullmatch(repeated.name) else "a header"
        raise ValueError(f"{setting} names {named} twice.") from None
    except ValueError:
        raise ValueError(
            f"{setting} is not valid JSON; expected an object of header names to text values."
        ) from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{setting} must be a JSON object of header names to text values.")
    return check_extra_headers(parsed, setting=setting)


def check_extra_headers(headers: Mapping[object, object], *, setting: str) -> dict[str, str]:
    """Refuse what a provider call must not carry, naming *setting*.

    Raises:
        ValueError: For an invalid header name, a value that is not text of
            visible ASCII, space or tab or that starts or ends with a space or
            tab, two names that differ only in case, or a reserved name
            (:data:`RESERVED_HEADERS`).
    """
    checked: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"{setting} contains a name that is not a valid HTTP header name.")
        folded = name.lower()
        if folded in RESERVED_HEADERS:
            raise ValueError(
                f"{setting} sets {name}, which the provider sets itself: authentication comes "
                "from the provider's key setting, framing from the HTTP client."
            )
        if folded in seen:
            raise ValueError(f"{setting} names {name} twice; header names ignore case.")
        if not isinstance(value, str) or not _VALUE.fullmatch(value):
            raise ValueError(
                f"{setting} gives {name} a value that is not text of visible ASCII "
                "characters, spaces or tabs."
            )
        if value != value.strip(" \t"):
            # An HTTP client refuses such a value on every request and quotes it.
            raise ValueError(
                f"{setting} gives {name} a value that starts or ends with a space or tab."
            )
        seen.add(folded)
        checked[name] = value
    return checked
