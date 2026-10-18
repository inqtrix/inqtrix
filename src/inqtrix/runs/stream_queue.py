"""Generic Valkey-Streams job queue shared by every durable job kind.

The run worker and the reindex worker dispatch through structurally
identical Valkey streams: the Postgres row stays the source of truth,
the stream carries only ``(<entity_id>, tenant_id)`` dispatch messages,
a lost message is recoverable (the reconciler re-enqueues stale rows),
and a duplicate is harmless (the guarded claim makes redelivery
idempotent). The XADD/XREADGROUP/XAUTOCLAIM/XCLAIM-JUSTID/XACK+XDEL/
dead-letter mechanics are the same for both — the only differences are
the stream/group names and the payload's id field. Factoring that
mechanism here keeps it defined once: a thin
subclass per job kind fixes the stream names, the id field, and the
concrete dispatch dataclass.

Stream invariants (verified against the Valkey 9.x docs, 2026-06):

* the job stream is NEVER trimmed with MAXLEN — XAUTOCLAIM silently
  drops trimmed entries from the pending list, which would lose jobs;
  entries are XACKed and XDELed individually at a terminal state;
* the OWNING worker heartbeats its in-flight entries via
  ``XCLAIM JUSTID`` (resets idle time WITHOUT bumping the delivery
  counter — the counter is the dead-letter budget); other workers
  reclaim via ``XAUTOCLAIM`` once idle exceeds the claim threshold.
"""

from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar

log = logging.getLogger("inqtrix")

TJob = TypeVar("TJob")

_SOCKET_TIMEOUT_SECONDS = 30.0

# A consumer entry quiet for longer than this window counts as
# detached in group_info (entries are never auto-removed and worker
# names are boot-unique, so the raw count only grows).
_CONSUMER_RECENCY_MS = 60_000
"""Socket read timeout for the valkey client. MUST exceed the worker's
blocking-claim duration (``BaseWorkerLoop._CLAIM_BLOCK_MS`` = 5 s): a
blocking ``XREADGROUP ... BLOCK`` holds the socket open for the block
duration, and with ``socket_timeout`` left at the library default the
read deadline collapses onto the block window — the deadline fires
before the server's empty reply arrives, so EVERY idle claim spuriously
times out and the worker never picks up a job. An explicit timeout
larger than the block gives the read the headroom it needs; the value
is generous because only the blocking claim approaches it (every other
command is sub-millisecond). ``socket_keepalive`` keeps the long-lived
worker connection from being dropped while idle."""


class StreamJobQueue(Generic[TJob]):
    """Synchronous Valkey-Streams queue (valkey-py client).

    Args:
        stream: Job stream key.
        dead_stream: Dead-letter stream key.
        group: Consumer group name.
        entity_field: Payload field carrying the durable id (e.g.
            ``run_id`` or ``job_id``) — also the attribute name read off
            the concrete dispatch dataclass when dead-lettering.
        url: Valkey connection URL (``redis://`` scheme).
        consumer: Consumer name within the worker group — the worker id.
            May stay empty for enqueue-only callers (the API process
            never reads the stream).
        client: Pre-built client (tests inject ``fakeredis`` here);
            ``None`` builds one from *url*.
    """

    def __init__(
        self,
        *,
        stream: str,
        dead_stream: str,
        group: str,
        entity_field: str,
        url: str = "",
        consumer: str = "",
        client: Any | None = None,
    ) -> None:
        if client is None:
            import valkey

            client = valkey.Valkey.from_url(
                url,
                decode_responses=True,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_keepalive=True,
            )
        self._client = client
        self._consumer = consumer
        self._stream = stream
        self._dead_stream = dead_stream
        self._group = group
        self._entity_field = entity_field

    # -- subclass hook ---------------------------------------------------- #

    def _make_job(
        self,
        *,
        message_id: str,
        entity_id: str,
        tenant_id: str,
        delivery_count: int,
    ) -> TJob:
        """Build the concrete dispatch dataclass for this job kind."""
        raise NotImplementedError

    def group_info(self) -> dict[str, int] | None:
        """Consumer-group liveness snapshot for the runtime manifest.

        Returns ``{'consumers': n, 'pending': m, 'depth': k}`` for this
        queue's worker group — the honest someone-is-attached signal a
        stuck-queued diagnosis needs — or ``None`` while the stream or
        group does not exist yet (no worker ever attached).
        """
        try:
            groups = self._client.xinfo_groups(self._stream)
            for group in groups:
                if group.get("name") != self._group:
                    continue
                # Consumer entries are never auto-removed and worker
                # names are boot-unique, so the raw group count only
                # ever grows. Filter by idle time: an entry quiet for
                # longer than the recency window is a dead husk, not an
                # attached worker.
                active = 0
                for consumer in self._client.xinfo_consumers(
                    self._stream, self._group
                ):
                    idle_ms = int(consumer.get("idle", 0) or 0)
                    if idle_ms < _CONSUMER_RECENCY_MS:
                        active += 1
                return {
                    "consumers": active,
                    "pending": int(group.get("pending", 0) or 0),
                    "depth": int(self._client.xlen(self._stream)),
                }
        except Exception:  # noqa: BLE001 — degrade to None, never raise
            return None
        return None

    # -- producer side ---------------------------------------------------- #

    def _enqueue(self, *, entity_id: str, tenant_id: str) -> None:
        """Append one dispatch message; subclasses expose a typed alias."""
        self._client.xadd(
            self._stream,
            {self._entity_field: entity_id, "tenant_id": tenant_id},
        )

    # -- consumer side ---------------------------------------------------- #

    def ensure_group(self) -> None:
        """Create the consumer group, tolerating prior creation."""
        try:
            self._client.xgroup_create(
                self._stream, self._group, id="$", mkstream=True
            )
        except Exception as exc:  # noqa: BLE001 — only BUSYGROUP is benign
            if "BUSYGROUP" not in str(exc):
                raise

    def claim_pending(self) -> list[TJob]:
        """Drain this consumer's own pending entries (crash recovery)."""
        response = self._client.xreadgroup(
            self._group, self._consumer, {self._stream: "0"}
        )
        return self._jobs_from_read(response)

    def claim_new(self, *, block_ms: int, count: int = 1) -> list[TJob]:
        """Claim never-delivered messages, blocking up to *block_ms*."""
        response = self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=count,
            block=block_ms,
        )
        return self._jobs_from_read(response)

    def reclaim(self, *, min_idle_ms: int, count: int = 10) -> list[TJob]:
        """Take over entries whose owner stopped heartbeating.

        Uses the full XAUTOCLAIM form (not JUSTID) so the delivery
        counter increments — the counter is the dead-letter budget.
        The loop terminates on the ``0-0`` cursor OR when an iteration
        yields nothing new — claiming resets idle time, so a low
        threshold could otherwise re-match the same entries forever.
        """
        jobs: list[TJob] = []
        seen: set[str] = set()
        cursor = "0-0"
        while True:
            response = self._client.xautoclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=min_idle_ms,
                start_id=cursor,
                count=count,
            )
            cursor, entries = response[0], response[1]
            fresh = [
                (message_id, fields)
                for message_id, fields in entries
                if message_id not in seen
            ]
            for message_id, fields in fresh:
                seen.add(message_id)
                jobs.append(self._job(message_id, fields))
            if cursor == "0-0" or not fresh:
                return jobs

    def heartbeat(self, message_ids: list[str]) -> None:
        """Reset idle time on in-flight entries WITHOUT a retry bump.

        ``JUSTID`` is load-bearing: a plain XCLAIM would increment the
        delivery counter and eventually dead-letter healthy long jobs.
        """
        if not message_ids:
            return
        self._client.xclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=0,
            message_ids=message_ids,
            justid=True,
        )

    def ack(self, message_id: str) -> None:
        """Acknowledge and delete one finished dispatch message.

        Call order is load-bearing: the terminal state must be
        committed to Postgres FIRST — a crash between commit and ack
        causes one redelivery that the job-row state machine absorbs.
        """
        self._client.xack(self._stream, self._group, message_id)
        self._client.xdel(self._stream, message_id)

    def dead_letter(self, job: TJob, *, reason: str) -> None:
        """Move one poisoned job to the dead-letter stream and ack it."""
        entity_id = getattr(job, self._entity_field)
        self._client.xadd(
            self._dead_stream,
            {
                self._entity_field: entity_id,
                "tenant_id": job.tenant_id,  # type: ignore[attr-defined]
                "original_id": job.message_id,  # type: ignore[attr-defined]
                "delivery_count": str(job.delivery_count),  # type: ignore[attr-defined]
                "reason": reason,
            },
        )
        log.warning(
            "Job %s moved to the dead-letter stream after %d delivery attempts (%s).",
            entity_id,
            job.delivery_count,  # type: ignore[attr-defined]
            reason,
        )
        self.ack(job.message_id)  # type: ignore[attr-defined]

    # -- helpers ---------------------------------------------------------- #

    def _jobs_from_read(self, response: Any) -> list[TJob]:
        jobs: list[TJob] = []
        for _stream, entries in response or []:
            for message_id, fields in entries:
                if fields is None:
                    # A trimmed/deleted entry still pending: nothing to
                    # execute — drop it from the PEL visibly.
                    log.warning(
                        "Job stream entry %s discarded because it has no payload.",
                        message_id,
                    )
                    self._client.xack(self._stream, self._group, message_id)
                    continue
                jobs.append(self._job(message_id, fields))
        return jobs

    def _job(self, message_id: str, fields: dict[str, str]) -> TJob:
        tenant_id = fields.get("tenant_id", "")
        if not tenant_id:
            log.warning(
                "Job stream entry %s has no tenant_id; using the default tenant.",
                message_id,
            )
            tenant_id = "default"
        return self._make_job(
            message_id=message_id,
            entity_id=fields[self._entity_field],
            tenant_id=tenant_id,
            delivery_count=self._delivery_count(message_id),
        )

    def _delivery_count(self, message_id: str) -> int:
        pending = self._client.xpending_range(
            self._stream,
            self._group,
            min=message_id,
            max=message_id,
            count=1,
        )
        if not pending:
            return 1
        entry = pending[0]
        delivered = (
            entry.get("times_delivered")
            if isinstance(entry, dict)
            else getattr(entry, "times_delivered", 1)
        )
        return int(delivered or 1)
