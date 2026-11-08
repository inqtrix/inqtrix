"""The AgentAlgorithm contract and the AlgorithmRegistry.

The registry replaces mode branching: callers resolve the requested
mode to a registered algorithm and invoke it through one uniform
``run(request, runtime=..., context=...)`` signature. Registration is
explicit at composition time (``build_container`` /
``register_routes``) — there is deliberately no discovery, entry-point
scanning, or plugin loading here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from inqtrix.core.context import RunContext, RuntimeContext
    from inqtrix.core.results import AgentResult, RunRequest


class AlgorithmId(StrEnum):
    """Canonical ids of the built-in algorithms.

    The registry accepts arbitrary string ids (custom algorithms are a
    supported extension point); this enum names the built-ins so call
    sites and tests reference them without string literals. Values
    double as the public ``mode`` strings on the HTTP surface.
    """

    RESEARCH = "research"
    DIRECT_LLM = "direct_llm"


class UnknownAlgorithm(ValueError):
    """Raised when a requested algorithm id is not registered."""


@runtime_checkable
class AgentAlgorithm(Protocol):
    """One executable mode of the platform.

    Implementations are stateless with respect to individual runs:
    every per-request fact arrives via the
    :class:`~inqtrix.core.context.RunContext`. The same instance may
    serve concurrent runs as long as the wrapped engine supports it.

    Attributes:
        id: Stable identifier; doubles as the public ``mode`` value.
        display_name: Human-readable name for capability manifests.
        contract_version: Version of the workflow contract, at least 1. Every
            run of this mode is stamped with it on acceptance; a change that
            breaks resuming a stored run raises it.
    """

    id: str
    display_name: str
    contract_version: int

    def capabilities(self) -> dict:
        """Describe what this algorithm needs and produces.

        Returns:
            A JSON-serializable dict consumed by the capability
            manifest (e.g. ``requires``, ``streams_events``,
            ``produces``). Keys are additive; consumers must ignore
            unknown keys.
        """
        ...

    def run(
        self,
        request: "RunRequest",
        *,
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> "AgentResult":
        """Execute one request to completion (blocking).

        Args:
            request: The normalized execution request.
            runtime: App-level wiring (settings, registry, defaults).
            context: The per-request execution bundle (resolved
                providers/strategies/settings, principal, cancel and
                event seams).

        Returns:
            The algorithm outcome, including the raw provider-shaped
            result dict the serialization layer consumes.
        """
        ...


class AlgorithmRegistry:
    """Explicit, insertion-ordered registry of executable algorithms.

    Insertion order is part of the contract: operator-facing error
    messages and capability manifests list algorithms in registration
    order, so the composition root controls the presentation order.
    """

    def __init__(self) -> None:
        self._items: dict[str, AgentAlgorithm] = {}

    def register(self, algorithm: AgentAlgorithm) -> None:
        """Add one algorithm; duplicate ids fail loudly.

        Args:
            algorithm: The algorithm instance to register.

        Raises:
            ValueError: When an algorithm with the same id is already
                registered — duplicate registration is always a
                composition bug, never something to resolve silently —
                or when it names no workflow contract of at least 1, which
                every run of its mode is stamped with.
        """
        if algorithm.id in self._items:
            raise ValueError(f"Algorithm already registered: {algorithm.id}")
        contract_version = getattr(algorithm, "contract_version", None)
        # An exact type check: ``True`` is an int, but no version.
        if type(contract_version) is not int or contract_version < 1:
            raise ValueError(f"Algorithm {algorithm.id} names no workflow contract")
        self._items[algorithm.id] = algorithm

    def get(self, algorithm_id: str) -> AgentAlgorithm:
        """Resolve an algorithm id or fail with the available set.

        Args:
            algorithm_id: The requested ``mode`` value.

        Returns:
            The registered algorithm.

        Raises:
            UnknownAlgorithm: Listing the registered ids so the caller
                can build a precise operator/client-facing message.
        """
        try:
            return self._items[algorithm_id]
        except KeyError as exc:
            available = ", ".join(self._items)
            raise UnknownAlgorithm(
                f"Unknown algorithm {algorithm_id!r}. Available: {available}"
            ) from exc

    def ids(self) -> tuple[str, ...]:
        """Registered algorithm ids in registration order."""
        return tuple(self._items)

    def workflow_contracts(self) -> dict[str, int]:
        """The workflow contract each registered mode carries.

        A claim compares a run's binding with this map; a mode missing from
        it is one this process cannot run.
        """
        return {
            algorithm_id: algorithm.contract_version
            for algorithm_id, algorithm in self._items.items()
        }

    def manifest(self) -> list[dict]:
        """Capability manifest entries in registration order."""
        return [
            {
                "id": algorithm.id,
                "display_name": algorithm.display_name,
                **algorithm.capabilities(),
            }
            for algorithm in self._items.values()
        ]
