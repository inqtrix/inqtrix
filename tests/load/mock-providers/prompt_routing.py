"""Shared request identity for capture and replay in the provider stand-in.

The response schema distinguishes operations which deliberately share one
system role. Dates and the shared answer-language directive are framing,
not an operation identity. Unrecognised or colliding identities must remain
unmatched; a similar opening is not evidence of the same response contract.
"""

from __future__ import annotations

import json
from typing import Any

from inqtrix.prompt_language import LANGUAGE_NAMES_EN, date_line, directive_for

ROUTING_VERSION = 2
_DIRECTIVES = {directive_for(code) for code in (*LANGUAGE_NAMES_EN, None)}
_DATE_PREFIXES = (date_line("").removesuffix("."), "Heutiges Datum:")


def system_prompt(payload: Any) -> str:
    """Read a captured/incoming system message, or the first prompt without one."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        for role in ("system", "user"):
            for message in payload:
                if isinstance(message, dict) and message.get("role") == role:
                    return str(message.get("content") or "")
    return ""


def signature(prompt: str, length: int = 80, *, schema_name: str = "") -> str:
    """Return one exact key from the instruction opening and response schema.

    Strip only the application's known framing. A date embedded after the
    role sentence occupies the rest of that line; later instruction lines
    remain intact. The answer itself is never parsed to guess its schema.
    """
    lines = []
    for raw in str(prompt or "").splitlines():
        line = raw.strip()
        if line in _DIRECTIVES:
            continue
        for prefix in _DATE_PREFIXES:
            if prefix in line:
                line = line.partition(prefix)[0].strip()
                break
        if line:
            lines.append(line)
    opening = " ".join(" ".join(lines).split())[:length].lower()
    return json.dumps([schema_name, opening], ensure_ascii=False) if opening else ""


def request_schema_name(body: dict) -> str:
    """Read the schema identity exposed by the supported request formats."""
    named = ((body.get("response_format") or {}).get("json_schema") or {})
    if named.get("name"):
        return str(named["name"])
    # Native Anthropic carries the canonical schema title rather than a
    # separate response-format name.
    schema = (((body.get("output_config") or {}).get("format") or {}).get("schema") or {})
    return str(schema.get("title") or "")
