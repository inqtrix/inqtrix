"""Agent capability layer: one contract, many adapters.

See :mod:`inqtrix.capabilities.contracts` for the contract and
:mod:`inqtrix.capabilities.registry` for the registry. The composition
root builds the registry via :func:`build_capability_registry`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from inqtrix.capabilities.catalog import (
    build_editor_capabilities,
    build_editor_patch_capabilities,
    build_knowledge_capabilities,
    build_web_capabilities,
)
from inqtrix.capabilities.contracts import (
    CapabilityContext,
    CapabilityDefinition,
    CapabilityError,
    Effect,
)
from inqtrix.capabilities.registry import CapabilityRegistry, UnknownCapability
from inqtrix.capabilities.catalog.context import build_context_capabilities

if TYPE_CHECKING:
    from inqtrix.providers.base import SearchProvider
    from inqtrix.services.editor_patch_service import EditorPatchService
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )
    from inqtrix.services.knowledge_service import KnowledgeService

__all__ = [
    "CapabilityContext",
    "CapabilityDefinition",
    "CapabilityError",
    "CapabilityRegistry",
    "Effect",
    "UnknownCapability",
    "build_capability_registry",
]


def build_capability_registry(
    *,
    knowledge_service: "KnowledgeService | None" = None,
    editor_service: "EditorPersistenceService | None" = None,
    search_provider: "SearchProvider | None" = None,
    editor_patch_service: "EditorPatchService | None" = None,
    context_source_service=None,
) -> CapabilityRegistry:
    """Assemble the capability registry from wired services.

    Each catalog is registered only when its service/provider is
    present (mirrors conditional router mounting), so a deployment
    without knowledge/editor/web contributes no capabilities of
    that kind — the manifest degrades visibly rather than lying. The
    patch write pair additionally requires *editor_patch_service*.
    """
    registry = CapabilityRegistry()
    if context_source_service is not None:
        registry.register_all(build_context_capabilities(context_source_service))
    if knowledge_service is not None:
        registry.register_all(build_knowledge_capabilities(knowledge_service))
    if editor_service is not None:
        registry.register_all(build_editor_capabilities(editor_service))
    if search_provider is not None:
        registry.register_all(build_web_capabilities(search_provider))
    if editor_patch_service is not None:
        registry.register_all(
            build_editor_patch_capabilities(editor_patch_service)
        )
    return registry
