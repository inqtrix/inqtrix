"""The open-file limit of the gateway process.

Every descriptor the gateway holds counts against its soft limit: two per
relayed stream (browser and backend), two per collaboration WebSocket, one per
idle browser connection it keeps alive, and the listening sockets and files.
Only the streams have a ceiling (``INQTRIX_MAX_UPSTREAM_CONNECTIONS``, 4096 by
default), so the gateway does not guess a need: before it serves it raises its
soft limit to the hard limit the runtime grants, as other servers do, and
worker processes inherit it. Container runtimes start the process with
different limits: Podman with 524,288, newer Docker runtimes with a soft limit
of 1024 below a high hard limit.
"""

from __future__ import annotations

import logging
import resource

log = logging.getLogger("inqtrix.web_gateway")

_HEADROOM = 256
"""Descriptors beyond the streams: listening sockets, static files, logs."""


def ensure_open_file_limit(max_upstream_connections: int) -> None:
    """Raise this process's soft open-file limit to its hard limit.

    Without a hard limit (``RLIM_INFINITY``) the soft limit rises to what the
    stream ceiling alone needs. When the hard limit is below that need, or the
    raise is refused, the gateway still starts and logs a WARNING naming the
    need and what to change; at load it would run out of descriptors before
    its ceiling.
    """
    needed = 2 * max_upstream_connections + _HEADROOM
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        return
    target = needed if hard == resource.RLIM_INFINITY else hard
    if target > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (OSError, ValueError):
            log.warning(
                "The open-file limit stays at %d: raising it to %d was refused. "
                "%d upstream connections alone need %d. Raise the limit of the "
                "container or lower INQTRIX_MAX_UPSTREAM_CONNECTIONS.",
                soft,
                target,
                max_upstream_connections,
                needed,
            )
            return
        log.info("Raised the open-file limit from %d to %d.", soft, target)
    else:
        target = soft
    if target < needed:
        log.warning(
            "The open-file limit is %d, below the %d that %d upstream "
            "connections alone need; under load the gateway runs out of file "
            "descriptors. Raise the hard limit of the container or lower "
            "INQTRIX_MAX_UPSTREAM_CONNECTIONS.",
            target,
            needed,
            max_upstream_connections,
        )
