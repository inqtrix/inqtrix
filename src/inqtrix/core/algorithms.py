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
    """

    id: str
    display_name: str

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
                composition bug, never something to resolve silently.
        """
        if algorithm.id in self._items:
            raise ValueError(f"Algorithm already registered: {algorithm.id}")
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
