"""Deprecated import location for the per-request override surface.

The override request model and its application logic moved to
:mod:`inqtrix.services.overrides` when the service layer was
introduced: the only orchestration consumer
(:class:`~inqtrix.services.agent_context.AgentContextResolver`) lives
in ``services/``, and keeping the module under ``server/`` made the
service layer reach back into the server package (a layering cycle).

This shim keeps every existing ``inqtrix.server.overrides`` import
working unchanged (backwards compatibility is additive); new code
imports from :mod:`inqtrix.services.overrides`.
"""

from inqtrix.services.overrides import (
    AgentOverridesRequest,
    apply_overrides,
    parse_overrides_payload,
)

__all__ = [
    "AgentOverridesRequest",
    "apply_overrides",
    "parse_overrides_payload",
]
