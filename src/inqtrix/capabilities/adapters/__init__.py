"""Capability adapters.

The seam that translates the one capability contract into concrete tool
surfaces: the deepagents LangChain ``StructuredTool`` adapter and the MCP
tool adapter. Both iterate
:meth:`~inqtrix.capabilities.registry.CapabilityRegistry.manifest` /
``definitions`` — no adapter re-declares a tool list. Empty until those
adapters are implemented; kept as a package so the import path is stable.
"""
