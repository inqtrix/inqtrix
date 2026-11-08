"""Shared request identity for capture and replay in the provider stand-in.

The response schema distinguishes operations which deliberately share one
system role. Dates, the shared answer-language directive and the shared
security notice are framing, not an operation identity. A request whose system
message is only that notice is identified by its schema, or without one by the
opening of its user message. Unrecognised or colliding identities must remain
unmatched; a similar opening is not evidence of the same response contract.
"""

from __future__ import annotations

import json
import re
from typing import Any

from inqtrix.prompt_language import LANGUAGE_NAMES_EN, date_line, directive_for
from inqtrix.untrusted_content import UNTRUSTED_CONTENT_RULE

ROUTING_VERSION = 3
_DIRECTIVES = {directive_for(code) for code in (*LANGUAGE_NAMES_EN, None)}
_DATE_PREFIXES = (date_line("").removesuffix("."), "Heutiges Datum:")


def _is_only_framing(text: str) -> bool:
    return " ".join(text.split()) == " ".join(UNTRUSTED_CONTENT_RULE.split())


def system_prompt(payload: Any, *, schema_name: str = "") -> str:
    """Read the text that identifies a captured or incoming request.

    That is the system message, or the first prompt without one. A system
    message that holds only the shared security notice names no operation: the
    schema identifies the request then, or without one the user message.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        for role in ("system", "user"):
            for message in payload:
                if isinstance(message, dict) and message.get("role") == role:
                    text = str(message.get("content") or "")
                    if role == "system" and _is_only_framing(text):
                        if schema_name:
                            return ""
                        continue
                    return text
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
    return json.dumps([schema_name, opening], ensure_ascii=False) if opening or schema_name else ""


def assignment_of(user_text: str) -> str:
    """Return what a kernel run was asked to do; a role run states none of this kind."""
    head, marker, tail = str(user_text or "").rpartition("Assignment:")
    return tail.strip() if marker else ""


def tier_of(user_text: str) -> str:
    """Return the tier a kernel prompt states, because a tier decides what the run may do."""
    match = re.search(r"^Tier: (\w+)", str(user_text or ""), re.MULTILINE)
    return match.group(1) if match else ""


def request_schema_name(body: dict) -> str:
    """Read the schema identity exposed by the supported request formats."""
    named = ((body.get("response_format") or {}).get("json_schema") or {})
    if named.get("name"):
        return str(named["name"])
    # Native Anthropic carries the canonical schema title rather than a
    # separate response-format name.
    schema = (((body.get("output_config") or {}).get("format") or {}).get("schema") or {})
    return str(schema.get("title") or "")
