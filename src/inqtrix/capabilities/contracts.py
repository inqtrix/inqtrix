"""The capability contract: one shape consumed by three adapters.

A *capability* is a request/response service operation exposed to
agents (and later to MCP clients and thin REST routes) through one
uniform definition. Capabilities deliberately DO NOT contain business
logic — each wraps an existing application-service method and adds only
the typed envelope, the side-effect classification, and the injected
:class:`CapabilityContext`. They are distinct from
:class:`~inqtrix.core.algorithms.AlgorithmRegistry`, which stays the
sole registry for long-running graph engines; the capability layer
CALLS INTO services, it does not re-model algorithms.

The three fields that classify a capability — ``effect``,
``idempotent`` — map 1:1 onto the MCP tool-annotation vocabulary
(``readOnlyHint`` / ``destructiveHint`` / ``idempotentHint``), so the
later MCP adapter needs no second source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pydantic import BaseModel

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal, UserContext


class Effect(StrEnum):
    """A capability's side-effect class (mirrors MCP annotations).

    ``read`` is side-effect-free (``readOnlyHint=true``): the agent tool
    adapter may auto-approve and parallelise it. ``write`` mutates
    internal state and forces a human-in-the-loop gate. ``destructive``
    is an irreversible write (``destructiveHint=true``). Every capability
    declares its class so the adapters never have to infer risk.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class CapabilityContext:
    """Per-invocation execution context, built by adapters — NEVER the model.

    Identity, tenant, and visibility are injected here so the LLM can
    never choose them as tool arguments (the equivalent of LangChain's
    injected tool args). The capability handler receives this alongside
    the validated input model.

    Attributes:
        principal: The verified request identity. ``None`` only in
            library/test contexts that carry no HTTP principal; HTTP-served
            invocations always pass a real (possibly anonymous) principal.
        visible_to: The server-resolved membership context for data
            scoping (``None`` for unscoped anonymous/static principals,
            exactly as the services expect).
        workspace_id: The client UI namespace, passthrough only — never
            an authorization input.
        run_id: The parent agent run id for event/audit attribution
            (``None`` outside an agent run).
        knowledge_collection_ids: Immutable collection boundary pinned by
            the server when the run was admitted. ``None`` means that no run
            boundary applies (for example a direct MCP invocation); an empty
            set is an explicit boundary that permits no collection access.
        on_provider_retry: Optional platform observer for one visible provider
            retry. Capability handlers enrich the notice with operation input;
            they never invent retry policy themselves.
        question: The server-owned assignment text available to capability
            adapters as context, never as a model-supplied tool argument.
        search_provider: Request-resolved, authority-guarded search provider.
            When absent, a direct capability adapter uses its composition-time
            default. Models cannot set or replace this field.
    """

    principal: "Principal | None"
    visible_to: "UserContext | None" = None
    workspace_id: str | None = None
    run_id: str | None = None
    knowledge_collection_ids: frozenset[str] | None = None
    question: str = ""
    search_provider: Any | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    authority_check: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    on_provider_retry: Callable[[dict[str, object]], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    target_document_id: str | None = None

@dataclass(frozen=True)
class CapabilityDefinition:
    """One registered capability: its contract plus its handler.

    Attributes:
        id: Stable dotted identifier (``"knowledge.search"``); doubles
            as the MCP tool name and the manifest key.
        summary: One-sentence English description for discovery and the
            tool manifest. The richer LLM-facing tool description lives
            in the agent prompt layer, not here.
        input_model: Pydantic v2 model validating the invocation input;
            also the source of the tool's JSON schema.
        output_model: Pydantic v2 model the handler returns.
        effect: Side-effect classification (see :class:`Effect`).
        idempotent: Whether repeating the call with the same input is
            safe (maps to MCP ``idempotentHint``). ``read`` capabilities
            are idempotent by nature; the field is explicit so the
            adapters never infer it.
        handler: The async callable ``(input, context) -> output`` that
            wraps the existing service method. Registered at composition
            time with its service bound.
    """

    id: str
    summary: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    effect: Effect
    idempotent: bool
    handler: Callable[[BaseModel, CapabilityContext], Awaitable[BaseModel]]

    def manifest_entry(self) -> dict[str, object]:
        """Discovery-manifest projection (id, summary, effect, hints).

        Consumed identically by ``/v1/capabilities`` and every tool
        adapter, so the risk vocabulary has exactly one source.
        """
        return {
            "id": self.id,
            "summary": self.summary,
            "effect": self.effect.value,
            "read_only": self.effect is Effect.READ,
            "destructive": self.effect is Effect.DESTRUCTIVE,
            "idempotent": self.idempotent,
        }


class CapabilityError(Exception):
    """A capability failure carrying a stable code + user-facing message.

    Adapters translate this uniformly: the REST adapter to
    ``error_response(http_status, message, code)``; the tool adapter to
    a ``ToolException`` text. The ``code`` is stable across locales; the
    ``message`` may be localized (German user-facing strings).
    """

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
