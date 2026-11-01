"""Identity edge of the gateway mode: the API server's mode and readiness.

The header policy itself lives in :mod:`.headers`. This module adds what needs
state: the auth mode and the gateway's assertion, read from the API server's
``/api/auth/config`` rather than from this process's environment, so a
forgotten pass-through never silently keeps the old forwarding rule; the
throttled report of removed identity headers; and the reason this edge must
not become ready.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

import httpx

from .settings import IdentityEdgePolicy

log = logging.getLogger("inqtrix.web_gateway")

#: While the API server has not answered, its mode is asked again this often.
_MODE_RETRY_SECONDS = 5.0
#: Removed identity headers are reported at most this often.
_STRIP_REPORT_INTERVAL_SECONDS = 60.0
#: The assertions whose duties at the edge this module knows.
_KNOWN_ASSERTIONS = frozenset({"jwt", "header", "resolver"})


class IdentityEdge:
    """Mode, readiness and reporting of the identity edge.

    Args:
        policy: The header names and this process's trusted peers.
        client: The shared backend client; the mode comes from the API server.
        forwarded_allow_ips_set: Whether the retired ``FORWARDED_ALLOW_IPS``
            is set; the gateway mode refuses it.
        ask_mode: ``False`` never asks the API server, so the edge stays in
            the plain proxy's behaviour (tests of the proxy mechanics).
        clock: Monotonic clock (tests pass their own).
    """

    def __init__(
        self,
        policy: IdentityEdgePolicy,
        client: httpx.AsyncClient,
        *,
        forwarded_allow_ips_set: bool = False,
        ask_mode: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._client = client
        self._forwarded_allow_ips_set = forwarded_allow_ips_set
        self._ask_mode = ask_mode
        self._clock = clock
        self._mode: str | None = None
        self._assertion: str | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()
        self._last_report: float | None = None
        self._held_back = 0
        self._unready_reported = False

    def log_policy(self) -> None:
        """Write the start line: the policy, the header names and the peer count."""
        if not self.policy.header_names:
            policy = "none"
        elif self.policy.trusted_networks:
            policy = "strip_untrusted"
        else:
            policy = "keep_all"
        log.info(
            "Identity edge: identity_header_policy=%s headers=%s trusted_proxies=%d.",
            policy,
            ",".join(sorted(self.policy.header_names)) or "-",
            len(self.policy.trusted_networks),
        )

    async def gateway_mode(self) -> bool:
        """Whether the API server runs the gateway mode, once it has said so."""
        return await self._auth_mode() == "gateway"

    async def _auth_mode(self) -> str | None:
        if self._mode is not None or not self._ask_mode:
            return self._mode
        now = self._clock()
        if self._last_attempt is not None and now - self._last_attempt < _MODE_RETRY_SECONDS:
            return None
        async with self._lock:
            if self._mode is not None:
                return self._mode
            self._last_attempt = self._clock()
            try:
                response = await self._client.get("/api/auth/config")
                config = response.json() if response.status_code == 200 else {}
                mode = config.get("auth_mode")
                assertion = config.get("gateway_assertion")
            except (httpx.HTTPError, ValueError, AttributeError):
                mode, assertion = None, None
            if isinstance(mode, str) and mode:
                self._mode = mode
                self._assertion = assertion if isinstance(assertion, str) else None
            return self._mode

    def report_removed(self, header_names: Iterable[str], peer: str | None) -> None:
        """Report identity headers removed from a peer outside the list, by name only."""
        if not self.policy.strips(peer):
            return
        removed = sorted({name.lower() for name in header_names} & self.policy.header_names)
        if not removed:
            return
        now = self._clock()
        if self._last_report is not None and now - self._last_report < _STRIP_REPORT_INTERVAL_SECONDS:
            self._held_back += 1
            return
        log.warning(
            "Identity headers from a peer outside INQTRIX_GATEWAY_TRUSTED_PROXIES "
            "were removed (outcome=untrusted_source headers=%s; %d more since "
            "the last report).",
            ",".join(removed),
            self._held_back,
        )
        self._last_report = now
        self._held_back = 0

    async def unready_reason(self) -> str | None:
        """Why this edge must not become ready, or ``None``.

        Only the gateway mode makes demands: the edge needs the identity
        header names to remove them from untrusted peers, and the retired
        ``FORWARDED_ALLOW_IPS`` must not be set. The header assertion also
        needs this edge's peer list and the subject and proof header names,
        because there the shared secret alone would otherwise protect the
        edge; a resolver of its own needs the peer list, the proof header name
        and the names of the headers it reads. An assertion the edge does not
        know leaves its duties unknown.
        """
        if not await self.gateway_mode():
            return None
        problems = []
        if not self.policy.header_names:
            problems.append(
                "the identity header names (INQTRIX_GATEWAY_*_HEADER) are not set "
                "for the web gateway"
            )
        if self.policy.refused_header_problem:
            problems.append(self.policy.refused_header_problem)
        if self._assertion not in _KNOWN_ASSERTIONS:
            problems.append(
                "it does not report a known assertion (gateway_assertion), so the "
                "web gateway cannot tell its duties"
            )
        elif self._assertion == "header":
            if not self.policy.trusted_networks:
                problems.append(
                    "INQTRIX_GATEWAY_TRUSTED_PROXIES is empty for the web gateway, "
                    "which the header assertion requires"
                )
            if not (self.policy.subject_header and self.policy.proof_header):
                problems.append(
                    "the subject and proof header names (INQTRIX_GATEWAY_SUBJECT_HEADER, "
                    "INQTRIX_GATEWAY_PROOF_HEADER) are not set for the web gateway"
                )
        elif self._assertion == "resolver":
            if not self.policy.trusted_networks:
                problems.append(
                    "INQTRIX_GATEWAY_TRUSTED_PROXIES is empty for the web gateway, "
                    "which a resolver of its own requires"
                )
            if not self.policy.proof_header:
                problems.append(
                    "the proof header name (INQTRIX_GATEWAY_PROOF_HEADER) is not set "
                    "for the web gateway"
                )
            if self.policy.resolver_header_problem:
                problems.append(self.policy.resolver_header_problem)
            elif not self.policy.resolver_header_names:
                problems.append(
                    "the names of the headers the resolver reads "
                    "(INQTRIX_GATEWAY_RESOLVER_HEADERS) are not set for the web gateway"
                )
        if self._forwarded_allow_ips_set:
            problems.append(
                "FORWARDED_ALLOW_IPS is set; the web gateway reads X-Forwarded-For itself"
            )
        if not problems:
            return None
        reason = "The API server runs the gateway mode, but " + "; ".join(problems) + "."
        if not self._unready_reported:
            log.error("%s The web gateway stays unready.", reason)
            self._unready_reported = True
        return reason
