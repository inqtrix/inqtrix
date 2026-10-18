"""Application services between the HTTP routers and the algorithms.

Routers stay thin (parse, delegate, serialize); services own the
orchestration: stack resolution, per-request override/mode resolution,
payload caps,
run submission, and chat-completion execution. Every service receives
its collaborators via the constructor and is built
once in :func:`inqtrix.server.container.build_container`.
"""

from inqtrix.services.agent_context import (
    AgentContextResolver,
    ResolvedAgentContext,
    StackResolutionError,
)
from inqtrix.services.chat_service import ChatService
from inqtrix.services.health_service import HealthService
from inqtrix.services.run_service import RunService

__all__ = [
    "AgentContextResolver",
    "ChatService",
    "HealthService",
    "ResolvedAgentContext",
    "RunService",
    "StackResolutionError",
]
