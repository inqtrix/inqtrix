"""Explicit, insertion-ordered registry of agent capabilities.

Pattern-copy of :class:`~inqtrix.core.algorithms.AlgorithmRegistry`
(explicit registration, loud on duplicates, ordered manifest) — but a
SEPARATE type, never merged with it: algorithms are long-running graph
engines, capabilities are request/response service operations with
different lifecycles. Registration happens in the composition root
(``build_container``); there is deliberately no discovery or plugin
scanning here.
"""

from __future__ import annotations

from inqtrix.capabilities.contracts import (
    CapabilityContext,
    CapabilityDefinition,
    CapabilityError,
)
from pydantic import BaseModel, ValidationError


class UnknownCapability(KeyError):
    """Raised when a requested capability id is not registered."""


class CapabilityRegistry:
    """Insertion-ordered registry of :class:`CapabilityDefinition`.

    Insertion order is part of the contract: the discovery manifest
    lists capabilities in registration order, so the composition root
    controls presentation.
    """

    def __init__(self) -> None:
        self._items: dict[str, CapabilityDefinition] = {}

    def register(self, capability: CapabilityDefinition) -> None:
        """Add one capability; a duplicate id fails loudly.

        Raises:
            ValueError: When a capability with the same id is already
                registered — always a composition bug.
        """
        if capability.id in self._items:
            raise ValueError(f"Capability already registered: {capability.id}")
        self._items[capability.id] = capability

    def register_all(self, capabilities: list[CapabilityDefinition]) -> None:
        """Register a catalog builder's output in order."""
        for capability in capabilities:
            self.register(capability)

    def get(self, capability_id: str) -> CapabilityDefinition:
        """Resolve a capability id or fail with the available set.

        Raises:
            UnknownCapability: Listing the registered ids.
        """
        try:
            return self._items[capability_id]
        except KeyError as exc:
            available = ", ".join(self._items)
            raise UnknownCapability(
                f"Unknown capability {capability_id!r}. Available: {available}"
            ) from exc

    def ids(self) -> tuple[str, ...]:
        """Registered capability ids in registration order."""
        return tuple(self._items)

    def definitions(self) -> tuple[CapabilityDefinition, ...]:
        """Registered definitions in registration order."""
        return tuple(self._items.values())

    def manifest(self) -> list[dict[str, object]]:
        """Discovery-manifest entries in registration order."""
        return [item.manifest_entry() for item in self._items.values()]

    async def invoke(
        self,
        capability_id: str,
        payload: dict | BaseModel,
        context: CapabilityContext,
    ) -> BaseModel:
        """Validate *payload* against the capability's input model and run it.

        The single invocation seam every adapter uses. ``payload`` may
        be a raw dict (validated here) or an already-built input model.

        Raises:
            UnknownCapability: The id is not registered.
            CapabilityError: Input validation failed (code
                ``invalid_input``, HTTP 400) or the handler raised one.
        """
        definition = self.get(capability_id)
        if isinstance(payload, definition.input_model):
            model = payload
        else:
            raw = payload.model_dump() if isinstance(payload, BaseModel) else payload
            try:
                model = definition.input_model.model_validate(raw)
            except ValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {}
                loc = ".".join(str(part) for part in first.get("loc", ()))
                raise CapabilityError(
                    "invalid_input",
                    f'Invalid input for {capability_id}'
                    + (f": {loc}" if loc else ""),
                    http_status=400,
                ) from exc
        if context.authority_check is not None:
            context.authority_check()
        result = await definition.handler(model, context)
        if context.authority_check is not None:
            context.authority_check()
        return result
