"""Provider/model circuit semantics for contextual indexing."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from inqtrix.contextualization_circuit import (
    CircuitAdmission,
    ContextualizationCircuitPermit,
    MemoryContextualizationCircuitBreaker,
)


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _permit(
    breaker: MemoryContextualizationCircuitBreaker,
    *,
    provider: str = "azure",
    model: str = "fast",
) -> ContextualizationCircuitPermit | None:
    return breaker.acquire_contextualization_circuit(
        provider_key=provider,
        model=model,
        cooldown_seconds=10,
        probe_lease_seconds=30,
    )


def test_transient_failure_opens_only_the_matching_provider_and_model() -> None:
    clock = MutableClock()
    breaker = MemoryContextualizationCircuitBreaker(clock=clock)
    first = _permit(breaker)
    assert first is not None

    breaker.record_contextualization_circuit_failure(
        first,
        error_type="contextualization_provider_timeout",
    )

    assert _permit(breaker) is None
    assert _permit(breaker, model="other-model") is not None
    assert _permit(breaker, provider="anthropic") is not None
    assert breaker.snapshot(provider_key="azure", model="fast")["state"] == "open"


def test_cooldown_grants_exactly_one_half_open_probe_concurrently() -> None:
    clock = MutableClock()
    breaker = MemoryContextualizationCircuitBreaker(clock=clock)
    first = _permit(breaker)
    assert first is not None
    breaker.record_contextualization_circuit_failure(
        first,
        error_type="contextualization_provider_unavailable",
    )
    clock.value += 10
    barrier = threading.Barrier(8)

    def acquire() -> ContextualizationCircuitPermit | None:
        barrier.wait()
        return _permit(breaker)

    with ThreadPoolExecutor(max_workers=8) as executor:
        permits = list(executor.map(lambda _index: acquire(), range(8)))

    granted = [permit for permit in permits if permit is not None]
    assert len(granted) == 1
    assert granted[0].probe_token is not None


def test_expired_probe_is_reclaimed_and_stale_tokens_are_fenced() -> None:
    clock = MutableClock()
    breaker = MemoryContextualizationCircuitBreaker(clock=clock)
    initial = _permit(breaker)
    assert initial is not None
    breaker.record_contextualization_circuit_failure(
        initial,
        error_type="contextualization_provider_timeout",
    )
    clock.value += 10
    crashed_probe = _permit(breaker)
    assert crashed_probe is not None
    assert crashed_probe.probe_token is not None
    assert _permit(breaker) is None

    clock.value += 30
    replacement = _permit(breaker)
    assert replacement is not None
    assert replacement.probe_token != crashed_probe.probe_token

    breaker.record_contextualization_circuit_success(crashed_probe)
    assert _permit(breaker) is None
    breaker.record_contextualization_circuit_failure(
        crashed_probe,
        error_type="contextualization_provider_timeout",
    )
    assert _permit(breaker) is None

    breaker.record_contextualization_circuit_success(replacement)
    assert _permit(breaker) is not None
    assert breaker.snapshot(provider_key="azure", model="fast")["state"] == "closed"


def test_failed_half_open_probe_reopens_for_a_fresh_cooldown() -> None:
    clock = MutableClock()
    breaker = MemoryContextualizationCircuitBreaker(clock=clock)
    initial = _permit(breaker)
    assert initial is not None
    breaker.record_contextualization_circuit_failure(
        initial,
        error_type="contextualization_provider_timeout",
    )
    clock.value += 10
    probe = _permit(breaker)
    assert probe is not None

    breaker.record_contextualization_circuit_failure(
        probe,
        error_type="contextualization_provider_rate_limited",
    )

    state = breaker.snapshot(provider_key="azure", model="fast")
    assert state is not None
    assert state["state"] == "open"
    assert state["consecutive_failures"] == 2
    assert state["cooldown_until"] == clock.value + 10
    assert _permit(breaker) is None


def test_the_view_admits_what_acquire_grants_through_a_whole_cycle() -> None:
    # The auto-resume sweep decides from the view what acquire would grant:
    # every call while closed, none while cooling or while a probe holds its
    # lease, one probe after either.
    clock = MutableClock()
    breaker = MemoryContextualizationCircuitBreaker(clock=clock)

    def admission() -> CircuitAdmission:
        view = breaker.contextualization_circuit_view(provider_key="azure", model="fast")
        return CircuitAdmission.ALL if view is None else view.admission(clock.value)

    assert admission() == CircuitAdmission.ALL
    first = _permit(breaker)
    assert first is not None
    breaker.record_contextualization_circuit_failure(
        first,
        error_type="contextualization_provider_timeout",
    )
    assert admission() == CircuitAdmission.NONE
    assert _permit(breaker) is None
    assert breaker.contextualization_circuit_view(provider_key="azure", model="other") is None

    clock.value += 10
    assert admission() == CircuitAdmission.ONE
    crashed_probe = _permit(breaker)
    assert crashed_probe is not None
    assert admission() == CircuitAdmission.NONE
    assert _permit(breaker) is None

    clock.value += 30
    assert admission() == CircuitAdmission.ONE
    replacement = _permit(breaker)
    assert replacement is not None
    breaker.record_contextualization_circuit_success(replacement)
    assert admission() == CircuitAdmission.ALL
    assert _permit(breaker) is not None
