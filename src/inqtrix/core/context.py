"""App-level runtime context and per-run execution context.

Two scopes, deliberately separated:

* :class:`RuntimeContext` is built once per application (or per
  ``ResearchAgent`` instance) and carries the default providers,
  strategies, settings, and the algorithm registry.
* :class:`RunContext` is built per request by an application service
  and carries everything one execution actually uses: the RESOLVED
  provider/strategy/settings bundle (multi-stack deployments swap
  these per request, which is why they cannot live on the app-level
  context), the verified principal, and the cancel/event seams.

Algorithms receive both and must read execution state from
``RunContext`` only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from queue import Queue
from typing import Any, Callable, TYPE_CHECKING

from inqtrix.core.events import EventSink

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal
    from inqtrix.core.algorithms import AlgorithmRegistry
    from inqtrix.providers.base import ProviderContext
    from inqtrix.settings import AgentSettings, Settings
    from inqtrix.strategies import StrategyContext


@dataclass(frozen=True)
class RuntimeContext:
    """Application-scoped wiring shared by all requests.

    Attributes:
        settings: The resolved root settings of the deployment.
        registry: The algorithm registry holding every executable mode.
        providers: The default provider bundle (single-stack identity;
            multi-stack requests resolve their own bundle into the
            :class:`RunContext`).
        strategies: The default strategy bundle, same caveat as
            ``providers``.
    """

    settings: "Settings"
    registry: "AlgorithmRegistry"
    providers: "ProviderContext"
    strategies: "StrategyContext"


@dataclass(frozen=True)
class RunContext:
    """Everything one algorithm execution needs, resolved per request.

    Attributes:
        providers: The provider bundle for THIS request (stack-aware).
        strategies: The strategy bundle for this request.
        agent_settings: Agent settings after stack, override, and mode
            resolution. Algorithms never re-apply overrides.
        principal: The verified request identity for HTTP-served
            executions (anonymous/static in the legacy modes). ``None``
            only for callers outside the HTTP surface (library mode,
            i.e. ``ResearchAgent``); streamed chat is HTTP-served and
            carries the resolved principal like every other registry
            dispatch.
        run_id: Native run id when executing under ``/v1/runs``;
            ``None`` for request/response protocols.
        workspace_id: Client-side project namespace persisted on a native
            run. Agent children inherit this value from their parent so
            authenticated event and result reads stay in the same namespace.
            ``None`` for request/response and library protocols.
        cancel_token: Threading event observed by the graph at node
            boundaries; ``None`` when the protocol offers no cancel.
        event_sink: Structured event emitter for native runs; ``None``
            for protocols without an event stream.
        progress_queue: Legacy coarse progress channel used by the
            streaming chat protocol; ``None`` elsewhere.
        token_budget: Optional HARD per-run LLM-token budget. ``0``
            (default) = off; when positive, the graph aborts at the
            next node boundary via *cancel_token* once cumulative
            tokens reach it (a graceful stop, the opt-in quota cap).
            Set only by the native-run path; request/response
            protocols leave it at ``0``.
        park: Optional park hook (``park(status: str) -> None``) — the
            run-store handle's ``wait`` threaded in by
            ``execute_run_request``. A segmented algorithm
            (workspace_agent) calls it to park the run in a waiting
            status and then RETURNS a parked result; ``None`` on
            surfaces that cannot park (chat completions), where an
            algorithm that needs interrupts must fail loudly instead of
            running without them.
    """

    providers: "ProviderContext"
    strategies: "StrategyContext"
    agent_settings: "AgentSettings"
    principal: "Principal | None" = None
    run_id: str | None = None
    workspace_id: str | None = None
    cancel_token: threading.Event | None = None
    event_sink: EventSink | None = None
    progress_queue: Queue | None = field(default=None, repr=False)
    token_budget: int = 0
    park: Any = None
    authority_check: Callable[[], None] | None = field(
        default=None, repr=False
    )
    """Live effective-actor check used at capability/tool safepoints."""
    stack_name: str = ""
    """Provider stack this execution was resolved on (``""`` = default).

    Agent child submissions thread it back into the resolver so a parent
    admitted on stack X never silently fans out on the default stack
    — the same inheritance rule as ``workspace_id``."""
