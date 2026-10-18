"""Shared circuit-breaker contract for ingestion-time contextualization.

The breaker is keyed by tenant, provider and resolved model.  PostgreSQL
deployments persist the state through the durable indexing store so every API
and worker replica observes the same open/half-open decision.  The memory
implementation below provides the same transition semantics under one lock
for the in-process deployment tier.

An open circuit never turns work into a success and never publishes raw
chunks.  It prevents another provider request and lets the indexing lifecycle
surface a typed ``paused_dependency`` state.  After the configured cooldown,
exactly one caller receives a leased half-open probe.  A matching successful
probe closes the circuit; a failed or abandoned probe reopens it (an expired
lease may be taken over after a worker crash).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Callable
from typing import Protocol, runtime_checkable

log = logging.getLogger("inqtrix")


class ContextualizationCircuitState(StrEnum):
    """Durable states for one provider/model dependency."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitAdmission(StrEnum):
    """How many provider calls a circuit admits right now."""

    ALL = "all"
    ONE = "one"
    NONE = "none"


def circuit_admission(
    state: ContextualizationCircuitState | str | None,
    *,
    cooldown_until: float | None,
    probe_lease_until: float | None,
    now: float,
) -> CircuitAdmission:
    """The one admission rule every breaker and the auto-resume sweep share.

    A closed (or never opened) circuit admits every call. An open circuit
    admits none until its cooldown ends, a half-open circuit none while a
    probe holds the lease; after that exactly one call may probe.
    """
    if state is None or state == ContextualizationCircuitState.CLOSED:
        return CircuitAdmission.ALL
    if state == ContextualizationCircuitState.OPEN and now < float(cooldown_until or 0):
        return CircuitAdmission.NONE
    if (
        state == ContextualizationCircuitState.HALF_OPEN
        and probe_lease_until is not None
        and now < float(probe_lease_until)
    ):
        return CircuitAdmission.NONE
    return CircuitAdmission.ONE


@dataclass(frozen=True)
class ContextualizationCircuitView:
    """Read-only state of one provider/model circuit."""

    state: ContextualizationCircuitState
    cooldown_until: float
    probe_lease_until: float | None = None

    def admission(self, now: float) -> CircuitAdmission:
        return circuit_admission(
            self.state,
            cooldown_until=self.cooldown_until,
            probe_lease_until=self.probe_lease_until,
            now=now,
        )


@dataclass(frozen=True)
class ContextualizationCircuitPermit:
    """Permission for one provider call.

    ``probe_token`` is present only for the single half-open probe.  Success
    and failure writes carrying a stale token are ignored, so a crashed or
    reclaimed worker cannot close a circuit now owned by another probe.
    """

    provider_key: str
    model: str
    cooldown_seconds: float
    probe_lease_seconds: float
    probe_token: str | None = None


@runtime_checkable
class ContextualizationCircuitBreaker(Protocol):
    """Provider/model circuit authority used by the contextualizer."""

    def acquire_contextualization_circuit(
        self,
        *,
        provider_key: str,
        model: str,
        cooldown_seconds: float,
        probe_lease_seconds: float,
    ) -> ContextualizationCircuitPermit | None:
        """Return one call permit, or ``None`` while the circuit is open."""
        ...

    def record_contextualization_circuit_success(
        self,
        permit: ContextualizationCircuitPermit,
    ) -> None:
        """Close a half-open circuit when ``permit`` still owns its lease."""
        ...

    def record_contextualization_circuit_failure(
        self,
        permit: ContextualizationCircuitPermit,
        *,
        error_type: str,
    ) -> None:
        """Open the circuit after a transient provider failure."""
        ...

    def contextualization_circuit_view(
        self,
        *,
        provider_key: str,
        model: str,
    ) -> ContextualizationCircuitView | None:
        """Current state of one circuit, ``None`` when it never opened."""
        ...


@dataclass
class _MemoryCircuitRecord:
    state: ContextualizationCircuitState
    consecutive_failures: int
    cooldown_until: float
    probe_token: str | None = None
    probe_lease_until: float | None = None
    last_error_type: str | None = None


class MemoryContextualizationCircuitBreaker:
    """Lock-serialized circuit state for the non-durable memory tier."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], _MemoryCircuitRecord] = {}

    def acquire_contextualization_circuit(
        self,
        *,
        provider_key: str,
        model: str,
        cooldown_seconds: float,
        probe_lease_seconds: float,
    ) -> ContextualizationCircuitPermit | None:
        now = float(self._clock())
        key = (_require_key(provider_key, "provider_key"), _require_key(model, "model"))
        cooldown = _require_positive(cooldown_seconds, "cooldown_seconds")
        lease = _require_positive(probe_lease_seconds, "probe_lease_seconds")
        with self._lock:
            record = self._records.get(key)
            admission = circuit_admission(
                None if record is None else record.state,
                cooldown_until=None if record is None else record.cooldown_until,
                probe_lease_until=None if record is None else record.probe_lease_until,
                now=now,
            )
            if admission == CircuitAdmission.ALL:
                return ContextualizationCircuitPermit(
                    provider_key=key[0],
                    model=key[1],
                    cooldown_seconds=cooldown,
                    probe_lease_seconds=lease,
                )
            if admission == CircuitAdmission.NONE:
                return None

            # Cooldown elapsed, or a worker died while owning the half-open
            # lease.  The store lock grants exactly one replacement probe.
            probe_token = uuid.uuid4().hex
            record.state = ContextualizationCircuitState.HALF_OPEN
            record.probe_token = probe_token
            record.probe_lease_until = now + lease
            log.info(
                "Contextualization circuit entered half-open",
                extra={
                    "event": "knowledge.contextualization.circuit.half_open",
                    "provider": key[0],
                    "model": key[1],
                    "probe_lease_seconds": lease,
                },
            )
            return ContextualizationCircuitPermit(
                provider_key=key[0],
                model=key[1],
                cooldown_seconds=cooldown,
                probe_lease_seconds=lease,
                probe_token=probe_token,
            )

    def record_contextualization_circuit_success(
        self,
        permit: ContextualizationCircuitPermit,
    ) -> None:
        if permit.probe_token is None:
            return
        key = (permit.provider_key, permit.model)
        with self._lock:
            record = self._records.get(key)
            if (
                record is None
                or record.state != ContextualizationCircuitState.HALF_OPEN
                or record.probe_token != permit.probe_token
            ):
                return
            record.state = ContextualizationCircuitState.CLOSED
            record.consecutive_failures = 0
            record.cooldown_until = 0.0
            record.probe_token = None
            record.probe_lease_until = None
            record.last_error_type = None
            log.info(
                "Contextualization circuit closed after successful probe",
                extra={
                    "event": "knowledge.contextualization.circuit.closed",
                    "provider": permit.provider_key,
                    "model": permit.model,
                },
            )

    def record_contextualization_circuit_failure(
        self,
        permit: ContextualizationCircuitPermit,
        *,
        error_type: str,
    ) -> None:
        now = float(self._clock())
        key = (permit.provider_key, permit.model)
        with self._lock:
            current = self._records.get(key)
            if permit.probe_token is not None and (
                current is None
                or current.state != ContextualizationCircuitState.HALF_OPEN
                or current.probe_token != permit.probe_token
            ):
                return
            failures = (current.consecutive_failures if current else 0) + 1
            self._records[key] = _MemoryCircuitRecord(
                state=ContextualizationCircuitState.OPEN,
                consecutive_failures=failures,
                cooldown_until=now + permit.cooldown_seconds,
                last_error_type=str(error_type),
            )
            log.warning(
                "Contextualization circuit opened after transient failure",
                extra={
                    "event": "knowledge.contextualization.circuit.opened",
                    "provider": permit.provider_key,
                    "model": permit.model,
                    "cooldown_seconds": permit.cooldown_seconds,
                    "failure_count": failures,
                    "error_type": str(error_type),
                },
            )

    def snapshot(self, *, provider_key: str, model: str) -> dict[str, object] | None:
        """Return a test/diagnostic snapshot without exposing source content."""

        with self._lock:
            record = self._records.get((provider_key, model))
            if record is None:
                return None
            return {
                "state": record.state.value,
                "consecutive_failures": record.consecutive_failures,
                "cooldown_until": record.cooldown_until,
                "probe_token": record.probe_token,
                "probe_lease_until": record.probe_lease_until,
                "last_error_type": record.last_error_type,
            }

    def contextualization_circuit_view(
        self,
        *,
        provider_key: str,
        model: str,
    ) -> ContextualizationCircuitView | None:
        with self._lock:
            record = self._records.get((provider_key, model))
            if record is None:
                return None
            return ContextualizationCircuitView(
                state=record.state,
                cooldown_until=record.cooldown_until,
                probe_lease_until=record.probe_lease_until,
            )


def _require_key(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _require_positive(value: float, name: str) -> float:
    normalized = float(value)
    if normalized <= 0:
        raise ValueError(f"{name} must be positive")
    return normalized
