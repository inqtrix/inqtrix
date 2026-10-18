"""Multi-stack hosting for the Inqtrix HTTP server.

Lets a single FastAPI process host multiple ``(providers, strategies,
agent_settings)`` triples side by side. UIs pick one per request via a
new ``body["stack"]`` top-level field, and a ``GET /v1/stacks``
discovery endpoint exposes the available bundles plus a cached health
flag so a frontend can render a selection box without
DDoSing the upstream providers.

Single-stack ``create_app(...)`` is unaffected — multi-stack lives in
this module behind its own factory ``create_multi_stack_app``. The
two factories share the routes, lifespan, and security helpers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from inqtrix.model_cards import build_models_catalog
from inqtrix.model_routing import describe_chat_model_options, describe_node_resolutions
from inqtrix.providers import instrument_providers
from inqtrix.providers.base import ProviderContext
from inqtrix.server.routes import create_router, register_routes
from inqtrix.auth.api_key import build_auth_provider
from inqtrix.server.security import make_cors_middleware_kwargs
from inqtrix.server.runs import RunStore
from inqtrix.services.health_service import provider_label, provider_ready
from inqtrix.settings import AgentSettings, Settings
from inqtrix.strategies import StrategyContext, create_default_strategies, resolve_claim_extract_model

log = logging.getLogger("inqtrix")


_STACK_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
_DISCOVERY_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class StackBundle:
    """A named bundle of providers / strategies / settings for multi-stack hosting.

    Attributes:
        providers: The :class:`ProviderContext` for this stack — the
            LLM and search providers that requests routed to this
            stack will use.
        strategies: Optional :class:`StrategyContext`. ``None`` (the
            default) makes the multi-stack factory derive defaults
            from ``providers.llm`` via
            :func:`inqtrix.strategies.create_default_strategies`.
        agent_settings: Optional per-stack :class:`AgentSettings` that
            overrides the global ``settings.agent`` for requests
            routed to this stack only. ``None`` falls back to the
            shared global.
        description: Free-text label shown in the ``/v1/stacks``
            discovery payload. Should be operator-friendly
            (e.g. ``"Bedrock Opus 4.6 + Perplexity Sonar Pro"``).
    """

    providers: ProviderContext
    strategies: StrategyContext | None = None
    agent_settings: AgentSettings | None = None
    description: str = ""


def _validate_stacks(
    stacks: dict[str, StackBundle], default_stack: str
) -> None:
    """Validate the stack registry. Raises ValueError on any inconsistency."""
    if not stacks:
        raise ValueError("create_multi_stack_app requires at least one stack")
    for name in stacks:
        if not _STACK_NAME_PATTERN.match(name):
            raise ValueError(
                f"Stack name {name!r} must match ^[a-z0-9_]+$ "
                "(lowercase letters, digits, underscore)."
            )
    if default_stack not in stacks:
        raise ValueError(
            f"default_stack {default_stack!r} is not in stacks "
            f"({sorted(stacks.keys())})"
        )


class _DiscoveryCache:
    """Time-bound cache for the ``/v1/stacks`` payload.

    ``is_available()`` may touch the network; callers (a frontend
    polling every second) must not be allowed to fan out into a
    provider-call storm. The cache holds the rendered payload for
    ``_DISCOVERY_CACHE_TTL_SECONDS`` and re-renders thereafter.
    """

    def __init__(self) -> None:
        self._payload: dict[str, Any] | None = None
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()

    def get(
        self,
        *,
        stacks: dict[str, StackBundle],
        default_stack: str,
        default_agent_settings: AgentSettings,
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if (
                self._payload is not None
                and (now - self._fetched_at) < _DISCOVERY_CACHE_TTL_SECONDS
            ):
                return self._payload
            self._payload = self._render(
                stacks, default_stack, default_agent_settings
            )
            self._fetched_at = now
            return self._payload

    @staticmethod
    def _render(
        stacks: dict[str, StackBundle],
        default_stack: str,
        default_agent_settings: AgentSettings,
    ) -> dict[str, Any]:
        rendered = []
        for name, bundle in stacks.items():
            rendered.append(
                {
                    "name": name,
                    "llm": provider_label(bundle.providers.llm),
                    "search": provider_label(bundle.providers.search),
                    "ready": provider_ready(bundle.providers.llm, label=provider_label(bundle.providers.llm))
                    and provider_ready(bundle.providers.search, label=provider_label(bundle.providers.search)),
                    "description": bundle.description,
                    "models": _stack_models_payload(bundle, default_agent_settings),
                }
            )
        return {"default": default_stack, "stacks": rendered}


def _stack_models_payload(
    bundle: StackBundle,
    default_agent_settings: AgentSettings,
) -> dict[str, Any]:
    """Per-stack model identifiers for ``GET /v1/stacks`` discovery output.

    Constructor-first configuration: each entry reports what the
    bundle's LLM and search providers were *built* with, not what the
    process-global ``Settings`` defaults claim. UIs use this payload to
    render an honest stack-selection box (model chips next to the stack
    name) without making an extra call against ``/health``.

    The ``node_models`` block reports, per call site, the model and reasoning
    effort the graph would route to (with ``model_source`` / ``effort_source``
    provenance) -- the same resolution used at runtime and on ``/health``, so a
    UI can show the per-node/tier models rather than only ``reasoning_model``
    and make a silent ``reasoning_model`` default visible (one shared
    resolution; nothing reported that the routing did not decide).

    Empty strings (and an empty ``node_models``) are returned when a provider
    exposes no public model attribute; consumers should treat that as
    "unknown / provider-default" and not as an error.
    """
    llm_models = getattr(bundle.providers.llm, "models", None)
    effective_agent_settings = bundle.agent_settings or default_agent_settings
    requested_tier = (effective_agent_settings.model_tier or "").strip() or None
    node_models = describe_node_resolutions(llm_models, requested_tier)

    def _from_llm(attr: str) -> str:
        if llm_models is None:
            return ""
        value = getattr(llm_models, attr, "")
        return value if isinstance(value, str) else ""

    # Read the standardized SearchProvider.search_model
    # property; the ABC default returns "<ClassName>(unknown)" for
    # subclasses that forget to override, which is intentionally loud.
    search_provider = bundle.providers.search
    search_model_value = getattr(search_provider, "search_model", "")
    search_model = (
        search_model_value if isinstance(search_model_value, str) and search_model_value
        else ""
    )

    return {
        "reasoning_model": _from_llm("reasoning_model"),
        "claim_extract_model": node_models.get("claim_extract", {}).get("model", ""),
        "classify_model": node_models.get("classify", {}).get("model", ""),
        "evaluate_model": node_models.get("evaluate", {}).get("model", ""),
        "search_model": search_model,
        "node_models": node_models,
        "chat_model_options": describe_chat_model_options(llm_models),
        "models_catalog": build_models_catalog(
            getattr(bundle.providers.llm, "selectable_models", []) or []
        ),
        "context_window_tokens": getattr(
            bundle.providers.llm, "context_window_tokens", None
        ),
    }


def create_multi_stack_app(
    *,
    settings: Settings,
    stacks: dict[str, StackBundle],
    default_stack: str,
) -> FastAPI:
    """Build a FastAPI app that hosts multiple Baukasten stacks.

    Validates that ``stacks`` is non-empty, ``default_stack`` is a key
    in it, and every stack name matches ``^[a-z0-9_]+$``.

    The resulting app exposes the unauthenticated discovery endpoint
    ``GET /v1/stacks`` on top of the standard surface (``/health``,
    ``/v1/models``, ``/v1/chat/completions``, ``/v1/test/run``). Each
    chat-completions request resolves the stack via
    ``body["stack"]``; missing → ``default_stack``; unknown →
    ``400 invalid_request_error`` with ``available_stacks`` hint.

    The opt-in security layers (TLS, Bearer-API-key, CORS) come from
    ``settings.server`` exactly as in single-stack ``create_app``;
    discovery stays unauthenticated by design (frontends
    need to read the stack list before they have the API key form
    rendered).

    Args:
        settings: Resolved Inqtrix :class:`Settings`. Provides the
            server-side concurrency limits and security configuration.
        stacks: Mapping ``{stack_name: StackBundle}``. Keys must
            match ``^[a-z0-9_]+$``.
        default_stack: The key used when a request omits the
            ``"stack"`` body field. Must exist in ``stacks``.

    Returns:
        A fully wired FastAPI instance with multi-stack routing in
        place. Lifespan logs the discovery defaults and per-stack
        readiness on startup.
    """
    _validate_stacks(stacks, default_stack)

    # Every stack's providers get the instrumentation of the single-stack
    # factory, and get it BEFORE the default strategies are derived from
    # them: the stacks are built by the caller, and the untrusted-content
    # rule and the traces are guarantees only if they do not depend on how
    # a provider was constructed. Idempotent for pre-wrapped providers.
    # Defaults are derived via the same factory the single-stack path uses.
    resolved_stacks: dict[str, StackBundle] = {}
    for name, bundle in stacks.items():
        providers = instrument_providers(bundle.providers, settings)
        strategies = bundle.strategies
        if strategies is None:
            agent_for_defaults = bundle.agent_settings or settings.agent
            strategies = create_default_strategies(
                agent_for_defaults,
                llm=providers.llm,
                claim_extract_model=resolve_claim_extract_model(
                    providers.llm,
                    fallback=settings.models.effective_claim_extract_model,
                ),
                claim_extract_timeout=agent_for_defaults.claim_extract_timeout,
            )
        resolved_stacks[name] = StackBundle(
            providers=providers,
            strategies=strategies,
            agent_settings=bundle.agent_settings,
            description=bundle.description,
        )

    # Configure logging as a last-resort default — see create_app for
    # the rationale behind ``force=False``. A multi-stack example that
    # set up its own ``configure_logging(...)`` keeps its handlers; the
    # silent default is only installed when nothing was configured yet.
    from inqtrix.logging_config import configure_logging, read_logging_env
    configure_logging(
        enabled=settings.agent.testing_mode,
        level="DEBUG" if settings.agent.testing_mode else "WARNING",
        console=True,
        force=False,
        json_format=read_logging_env().json_format,
    )

    _semaphore: asyncio.Semaphore | None = None

    def semaphore_factory() -> asyncio.Semaphore:
        nonlocal _semaphore
        if _semaphore is None:
            _semaphore = asyncio.Semaphore(settings.server.max_concurrent)
        return _semaphore

    run_store = RunStore.from_settings(settings.server)

    auth_provider = build_auth_provider(settings)
    cors_kwargs = make_cors_middleware_kwargs(settings.server)
    api_key_active = auth_provider.mode == "apikey"
    cors_active = cors_kwargs is not None

    discovery_cache = _DiscoveryCache()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "Inqtrix multi-stack server starting | stacks=%d | default=%s "
            "| max_concurrent=%d | run_max_concurrent=%d | run_queue_max_size=%d "
            "| run_completed_ttl_seconds=%d | api_key_gate=%s | cors=%s",
            len(resolved_stacks),
            default_stack,
            settings.server.max_concurrent,
            settings.server.run_max_concurrent or settings.server.max_concurrent,
            settings.server.run_queue_max_size,
            settings.server.run_completed_ttl_seconds,
            "on" if api_key_active else "off",
            "on" if cors_active else "off",
        )
        for name, bundle in resolved_stacks.items():
            llm_label = provider_label(bundle.providers.llm)
            search_label = provider_label(bundle.providers.search)
            log.info(
                "  stack=%s | llm=%s | search=%s | description=%s",
                name,
                llm_label,
                search_label,
                bundle.description or "(none)",
            )
        from inqtrix.services.system_runtime import (
            database_runtime_contract_ready,
        )

        database_ready = await database_runtime_contract_ready(
            _app.state.container
        )
        _app.state.database_contract_ready = database_ready
        if not database_ready:
            log.warning(
                "Database runtime contract is not ready; multi-stack "
                "business routes remain gated until /readyz succeeds."
            )
        if database_ready and settings.sharing.restrict_to_workspace_members:
            from inqtrix.services.workspace_administration import (
                ensure_workspace_share_reconciliation,
            )

            revoked = await ensure_workspace_share_reconciliation(
                _app,
                container.workspace_admin,
                tenant_id="default",
            )
            if revoked:
                log.warning(
                    "Workspace-Share-Reconciliation revoked %d invalid "
                    "active share(s) before multi-stack readiness.",
                    revoked,
                )
            else:
                log.info(
                    "Workspace-Share-Reconciliation completed without changes."
                )
        # Deliberately NOT gated on database_ready: a database blip during
        # boot would otherwise disable upload recovery for the whole
        # process lifetime. Each pass degrades to a WARNING on storage
        # errors and succeeds on its own once the database returns.
        upload_reconciler = getattr(container, "upload_reconciler", None)
        if upload_reconciler is not None:
            upload_reconciler.start()
        try:
            yield
        finally:
            log.info(
                "Inqtrix multi-stack server stopping | stacks=%d",
                len(resolved_stacks),
            )
            upload_reconciler = getattr(container, "upload_reconciler", None)
            if upload_reconciler is not None:
                upload_reconciler.close()
            collaboration_service = getattr(
                container, "editor_collaboration_service", None
            )
            if collaboration_service is not None:
                await collaboration_service.aclose()
            # Same release as the single-stack factory: request code still
            # unwinding above may hand work to a lane, so this goes last.
            lanes = getattr(container, "execution_lanes", None)
            if lanes is not None:
                lanes.close()

    app_router = create_router()

    # The default-stack providers/strategies/settings drive the legacy
    # single-stack code path inside register_routes; the actual
    # multi-stack resolution happens per-request via stacks_lookup.
    default_bundle = resolved_stacks[default_stack]
    default_agent_settings = (
        default_bundle.agent_settings
        if default_bundle.agent_settings is not None
        else settings.agent
    )

    container = register_routes(
        app_router,
        providers=default_bundle.providers,
        strategies=default_bundle.strategies or create_default_strategies(
            default_agent_settings,
            llm=default_bundle.providers.llm,
            claim_extract_model=resolve_claim_extract_model(
                default_bundle.providers.llm,
                fallback=settings.models.effective_claim_extract_model,
            ),
            claim_extract_timeout=default_agent_settings.claim_extract_timeout,
        ),
        settings=settings,
        semaphore_factory=semaphore_factory,
        auth_provider=auth_provider,
        stacks=resolved_stacks,
        default_stack=default_stack,
        run_store=run_store,
    )

    # The discovery route is unauthenticated by design.
    @app_router.get("/v1/stacks")
    def list_stacks() -> dict[str, Any]:
        return discovery_cache.get(
            stacks=resolved_stacks,
            default_stack=default_stack,
            default_agent_settings=settings.agent,
        )

    enable_openapi = settings.server.enable_openapi
    app = FastAPI(
        title="Inqtrix Research Agent (multi-stack)",
        docs_url="/docs" if enable_openapi else None,
        redoc_url="/redoc" if enable_openapi else None,
        openapi_url="/openapi.json" if enable_openapi else None,
        lifespan=_lifespan,
    )
    if cors_kwargs is not None:
        app.add_middleware(CORSMiddleware, **cors_kwargs)

    # Added LAST so Starlette places it OUTERMOST (see create_app).
    from inqtrix.server.request_context import RequestContextMiddleware

    app.add_middleware(RequestContextMiddleware)
    app.include_router(app_router)
    app.state.container = container

    from inqtrix.auth.principal_generation import (
        install_principal_generation_error_handler,
    )

    install_principal_generation_error_handler(app)

    from inqtrix.server.database_gate import install_database_contract_gate

    install_database_contract_gate(app, container=container)

    return app
