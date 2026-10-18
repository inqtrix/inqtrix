"""Capability catalog builders.

Each builder takes an already-wired application service (or provider)
and returns the :class:`~inqtrix.capabilities.contracts.CapabilityDefinition`
list it owns. The composition root calls only the builders whose service
is present — the same conditional-registration pattern as router
mounting, so a disabled surface contributes no capabilities.
"""

from inqtrix.capabilities.catalog.editor import (
    build_editor_capabilities,
    build_editor_patch_capabilities,
)
from inqtrix.capabilities.catalog.knowledge import build_knowledge_capabilities
from inqtrix.capabilities.catalog.web import build_web_capabilities

__all__ = [
    "build_editor_capabilities",
    "build_editor_patch_capabilities",
    "build_knowledge_capabilities",
    "build_web_capabilities",
]
