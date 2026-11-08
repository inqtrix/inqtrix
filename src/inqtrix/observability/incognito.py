"""Whether a run is incognito: the one fact tracing and the run stores share."""

from __future__ import annotations

from typing import Any, Mapping


def incognito_request(request_payload: Mapping[str, Any] | None) -> bool:
    """Whether a persisted run request is an incognito knowledge ask.

    The run service marks it in the replay body. Both run stores index the
    mark for their retention sweep, and the execution span withholds the
    run's content by it, in whichever process executes the run.
    """
    body = (request_payload or {}).get("body")
    return isinstance(body, Mapping) and body.get("incognito") is True
