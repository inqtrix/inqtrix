"""Read-only source operations inside the executing saved conversation."""

from inqtrix.agents.context_sources import (
    ContextReadInput, ContextSearchInput, ContextSearchOutput, ContextWindow,
    MaterialReadInput, MaterialWindow, material_window,
)
from inqtrix.agents.context_archive import ArchiveVersionConflict
from inqtrix.capabilities.contracts import CapabilityDefinition, CapabilityError, Effect


def read_material_window(text: str, version_prefix: str, payload: MaterialReadInput) -> tuple[str, MaterialWindow]:
    """The shared transfer envelope of the existing document capabilities."""
    try:
        return material_window(text, version_prefix, payload)
    except ArchiveVersionConflict as exc:
        raise CapabilityError("context.version_changed", str(exc), http_status=409) from exc
    except ValueError as exc:
        raise CapabilityError("context.invalid_read", str(exc)) from exc


def build_context_capabilities(service):
    async def read(payload, context):
        return await service.read(payload, context)

    async def search(payload, context):
        return await service.search(payload, context)

    return [
        CapabilityDefinition(
            id="context.read", summary="Read a versioned original range from this conversation.",
            input_model=ContextReadInput, output_model=ContextWindow,
            effect=Effect.READ, idempotent=True, handler=read,
        ),
        CapabilityDefinition(
            id="context.search", summary="Discover or search authorized conversation originals with explicit scan coverage.",
            input_model=ContextSearchInput, output_model=ContextSearchOutput,
            effect=Effect.READ, idempotent=True, handler=search,
        ),
    ]
