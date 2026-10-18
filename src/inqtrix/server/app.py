"""FastAPI application factory with optional Baukasten injection and lifespan."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError

from inqtrix.auth.api_key import build_auth_provider
from inqtrix.providers import create_providers
from inqtrix.providers.base import ProviderContext
from inqtrix.server.routes import create_router, register_routes
from inqtrix.server.security import make_cors_middleware_kwargs
from inqtrix.services.health_service import provider_label, provider_ready
from inqtrix.settings import Settings
from inqtrix.strategies import StrategyContext, create_default_strategies, resolve_claim_extract_model

if TYPE_CHECKING:
    from inqtrix.auth.principal import AuthProvider
    from inqtrix.storage.object_store import ObjectStore

log = logging.getLogger("inqtrix")


def db_integrity_response(request: Request, exc: IntegrityError):
    """Map a DB constraint violation to a typed 4xx, or re-raise it.

    The app-wide Baukasten backstop (registered in :func:`create_app`) so
    a concurrent-write race in ANY store degrades gracefully and visibly
    instead of as an opaque 500 with a stack trace — without a
    flickenteppich of per-route try/except. Deliberately NARROW: only the
    two client-attributable SQLSTATEs are mapped; every other
    ``IntegrityError`` (check-constraint 23514, not-null 23502, a
    serialization failure, or a genuine data bug) is RE-RAISED so it
    still surfaces as a 500 with its traceback (No Silent Fallbacks —
    masking those behind a polite 409 would hide real faults). Routes
    that already catch their own domain conflicts (editor patches,
    artifact CAS, share) never let the ``IntegrityError`` escape and so
    never reach here. asyncpg exposes the SQLSTATE on ``exc.orig`` as
    ``.sqlstate`` (``.pgcode`` on the psycopg checkpointer path).
    """
    from inqtrix.services.request_parsing import error_response

    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    log.warning(
        "Database integrity violation sqlstate=%s method=%s",
        code,
        request.method,
    )
    if code == "23505":  # unique_violation
        return error_response(
            409,
            "The record already exists or violates a uniqueness constraint.",
            "conflict",
        )
    if code == "23503":  # foreign_key_violation
        return error_response(
            400,
            "A referenced record is missing or invalid.",
            "invalid_request_error",
        )
    raise exc


_PLACEHOLDER_SECRET_MARKER = "CHANGE_ME"


def _placeholder_secret_fields(settings: Settings) -> list[str]:
    """Env names of secret settings still holding a ``CHANGE_ME`` placeholder.

    Returns the variable NAMES only — never the value. Lets ``create_app``
    warn loudly at startup so a placeholder secret (the values shipped in
    ``deploy/.env.stack.example``) cannot be deployed silently
    (no silent fallbacks).
    """
    candidates = {
        "INQTRIX_SESSION_SECRET": settings.auth.session_secret,
        "INQTRIX_PAT_PEPPER": settings.auth.pat_pepper,
        "INQTRIX_PSEUDONYM_PEPPER": settings.auth.pseudonym_pepper,
        "INQTRIX_SERVER_API_KEY": settings.server.api_key,
        "INQTRIX_DATABASE_URL": settings.storage.database_url,
        "INQTRIX_OIDC_CLIENT_SECRET": settings.auth.oidc_client_secret,
    }
    return [
        name
        for name, value in candidates.items()
        if isinstance(value, str)
        and _PLACEHOLDER_SECRET_MARKER in value.upper()
    ]


def create_app(
    *,
    settings: Settings | None = None,
    providers: ProviderContext | None = None,
    strategies: StrategyContext | None = None,
    auth_provider: "AuthProvider | None" = None,
    object_store_impl: "ObjectStore | None" = None,
) -> FastAPI:
    """Build the Inqtrix FastAPI app with optional Baukasten injection.

    Resolution order (precedence high → low):

    1. ``providers`` injected (Baukasten mode) — when supplied,
       ``create_providers(...)`` is not called for the LLM/search slot,
       and the caller is fully responsible for provider construction.
       This is the path used by the ``examples/webserver_stacks/*.py``
       scripts.
    2. Explicit ``settings`` without ``providers`` — env-var mode against
       the supplied settings.
    3. Pure env-var configuration via ``Settings()`` as the final fallback.

    ``strategies`` is optional in every mode; when ``None``,
    :func:`inqtrix.strategies.create_default_strategies` is invoked with
    the resolved LLM provider so existing default heuristics stay intact.

    Args:
        settings: Pre-built :class:`Settings`. When ``None``, env
            resolution via :class:`Settings` applies.
        providers: Pre-built :class:`ProviderContext`. When supplied,
            the injected providers are used verbatim (Baukasten injection).
        strategies: Pre-built :class:`StrategyContext`. When ``None``,
            defaults are derived from the resolved LLM provider.

    Returns:
        A fully-wired :class:`FastAPI` instance with all routes
        registered, an ASGI ``lifespan`` context attached for startup
        health-probe and shutdown logging, and the OpenAPI schema
        intentionally disabled (``docs_url=None``).

    Example:
        Library / env-only mode (current default ``python -m inqtrix``)::

            from inqtrix.server import create_app
            app = create_app()

        Baukasten injection from a webserver-stack example::

            from inqtrix import LiteLLM, PerplexitySearch
            from inqtrix.providers.base import ProviderContext
            from inqtrix.server import create_app
            from inqtrix.settings import Settings

            providers = ProviderContext(
                llm=LiteLLM(api_key="...", default_model="gpt-4o"),
                search=PerplexitySearch(api_key="..."),
            )
            app = create_app(settings=Settings(), providers=providers)
    """
    if providers is not None:
        if settings is None:
            settings = Settings()
    elif settings is not None:
        pass
    else:
        settings = Settings()

    # Configure logging as a last-resort default. The webserver-stack
    # examples (and any caller that already ran ``configure_logging``
    # before invoking ``create_app``) keep their handlers — passing
    # ``force=False`` makes this call a no-op when a real handler is
    # already attached (see ``logging_config.is_configured``). Without
    # this guard the previous unconditional reset silently dropped every
    # INFO-level marker (``_classify_fallback``, ``Round 1``, ...) that
    # keeps every fallback visible in the log.
    from inqtrix.logging_config import configure_logging, read_logging_env
    configure_logging(
        enabled=settings.agent.testing_mode,
        level="DEBUG" if settings.agent.testing_mode else "WARNING",
        console=True,
        force=False,
        json_format=read_logging_env().json_format,
    )

    # Install the instance-wide pseudonym key so every subject reference
    # in logs (and later traces/audit correlation) is stable across the
    # API server, the workers, and restarts. An empty pepper keeps the
    # historical per-process references and logs one WARNING.
    from inqtrix.auth.log_redaction import configure_stable_pseudonyms
    configure_stable_pseudonyms(settings.auth.pseudonym_pepper)

    # Tracing (INQTRIX_TRACING): off by default; local/file/otlp install
    # the process-global tracer provider (idempotent across create_app
    # calls; a missing extra degrades loudly to off).
    from inqtrix.observability.otel import setup_tracing
    setup_tracing(settings, service_role="api")

    # Resolve providers — injected wins over env-driven defaults.
    if providers is None:
        providers = create_providers(settings)
    else:
        # Injected providers get the SAME instrumentation as built ones:
        # observability that depends on how a provider was constructed
        # is observability an operator cannot rely on. The wrappers are
        # idempotent, so an already-wrapped context passes through.
        from inqtrix.providers import instrument_providers

        providers = instrument_providers(providers, settings)

    # Resolve strategies — injected wins, otherwise defaults from LLM.
    # The claim_extract_model is resolved Constructor-First from the provider's
    # own models attribute; the global settings.models
    # serves only as a last-resort fallback for providers without one.
    if strategies is None:
        strategies = create_default_strategies(
            settings.agent,
            llm=providers.llm,
            claim_extract_model=resolve_claim_extract_model(
                providers.llm,
                fallback=settings.models.effective_claim_extract_model,
            ),
            claim_extract_timeout=settings.agent.claim_extract_timeout,
        )

    # Lazy semaphore (event loop may not exist yet)
    _semaphore: asyncio.Semaphore | None = None

    def semaphore_factory() -> asyncio.Semaphore:
        nonlocal _semaphore
        if _semaphore is None:
            _semaphore = asyncio.Semaphore(settings.server.max_concurrent)
        return _semaphore


    # Run-store selection (memory default, durable opt-in) happens in
    # the container's build_run_store bridge so the Postgres backends
    # share one engine; nothing is built here.

    # Resolve opt-in security layers (all default to disabled). The auth
    # provider honours INQTRIX_AUTH_MODE with explicit-wins semantics and
    # raises at startup on contradictory configuration instead of guessing.
    # An injected auth provider wins over env-driven mode resolution — the
    # replacement seam for a custom AuthProvider (no need to edit the
    # build_auth_provider dispatch). See how-to/writing-a-custom-auth-provider.
    auth_provider = auth_provider or build_auth_provider(settings)
    # Loudly flag any secret still left at its CHANGE_ME placeholder — a
    # placeholder secret is a silent insecurity, so it must be visible at
    # startup. Names only, never the value.
    placeholder_secrets = _placeholder_secret_fields(settings)
    if placeholder_secrets:
        log.warning(
            "INSECURE: %d secret(s) still hold a CHANGE_ME placeholder value "
            "(%s) — set real secrets before production.",
            len(placeholder_secrets),
            ", ".join(placeholder_secrets),
        )
    cors_kwargs = make_cors_middleware_kwargs(settings.server)
    api_key_active = auth_provider.mode == "apikey"
    cors_active = cors_kwargs is not None

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Startup — log resolved provider identity, reachability and the
        # behavioural envelope so operators can confirm the deployment
        # took effect without grepping a separate log file.
        # Also emit a terminal-visible banner about the active logging
        # configuration so operators see at a glance whether file
        # logging is enabled and where the log file lives. Printed
        # directly to stderr (not through the inqtrix logger) so the
        # banner stays visible even when file logging is disabled or
        # the logger is silent.
        from inqtrix.logging_config import print_logging_banner
        logging_state = print_logging_banner()
        log.info(
            "Logging status | file_enabled=%s | log_file=%s | level=%s "
            "| console=%s | web_mirrored=%s",
            logging_state["file_enabled"],
            logging_state["file_path"] or "-",
            logging_state["level"],
            logging_state["console_enabled"],
            logging_state["web_mirrored"],
        )

        llm_label = provider_label(providers.llm)
        search_label = provider_label(providers.search)
        llm_ready = provider_ready(providers.llm, label=llm_label)
        search_ready = provider_ready(providers.search, label=search_label)
        log.info(
            "Inqtrix server starting | llm=%s ready=%s | search=%s ready=%s "
            "| report_profile=%s | max_concurrent=%d | run_max_concurrent=%d "
            "| run_queue_max_size=%d | run_completed_ttl_seconds=%d "
            "| api_key_gate=%s | auth_mode=%s | cors=%s",
            llm_label,
            llm_ready,
            search_label,
            search_ready,
            settings.agent.report_profile,
            settings.server.max_concurrent,
            settings.server.run_max_concurrent or settings.server.max_concurrent,
            settings.server.run_queue_max_size,
            settings.server.run_completed_ttl_seconds,
            "on" if api_key_active else "off",
            auth_provider.mode,
            "on" if cors_active else "off",
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
                "Database runtime contract is not ready; business routes "
                "remain gated until /readyz verifies the migrated schema "
                "and restricted application role."
            )
        if settings.storage.backend == "postgres":
            # The engines have been built by now, so the count comes from
            # what exists rather than from a list kept by hand.
            from inqtrix.storage.connection_budget import (
                report_connection_budget,
            )

            container = getattr(_app.state, "container", None)
            checkpointer = getattr(container, "agent_checkpointer", None)
            await report_connection_budget(
                    database_url=settings.storage.database_url,
                process_label="API process",
                pool_size=settings.storage.pool_size,
                pool_max_overflow=settings.storage.pool_max_overflow,
                # The agent checkpointer speaks a different driver and keeps
                # its own pool, so no engine count can see it.
                extra_connections=(
                    checkpointer.max_connections if checkpointer is not None else 0
                ),
                extra_label="agent checkpointer",
                # Run threads drive a NullPool bundle: one connection per
                # operation, none while a run waits on its provider. They
                # hold nothing at rest, so no pool count sees them -- but a
                # synchronised burst can ask for one per in-flight run, and
                # that is the number the server has to survive.
                transient_peak=(
                    settings.server.run_max_concurrent
                    or settings.server.max_concurrent
                ),
                transient_label="run threads, NullPool",
                transient_knob="RUN_MAX_CONCURRENT",
            )
        if (
            settings.server.run_max_concurrent_per_user is not None
            and settings.server.run_max_concurrent_per_user
            < settings.agent_platform.max_parallel_children
        ):
            # Visible constraint (No Silent Fallbacks): a per-user cap
            # below one agent wave's width means a single agent tree's
            # children cannot all be admitted — the wave truncates to
            # proceed-and-mark holes. Loud at startup, not discovered as
            # mysteriously incomplete agent runs.
            log.warning(
                "RUN_MAX_CONCURRENT_PER_USER=%d is smaller than "
                "INQTRIX_AGENT_MAX_PARALLEL_CHILDREN=%d; a single agent wave exceeds "
                "the per-user budget and will be limited. Set the cap to at least the "
                "wave width.",
                settings.server.run_max_concurrent_per_user,
                settings.agent_platform.max_parallel_children,
            )
        from inqtrix.agents.plan_validation import MAX_PLAN_TASKS_DEFAULT

        # A wave is never wider than the plan the validator accepts, so
        # the raw knob overstates the peak whenever it exceeds that
        # ceiling. Warning on the overstated number would cry wolf —
        # and a warning nobody believes is worse than none.
        effective_wave = min(
            settings.agent_platform.max_parallel_children,
            MAX_PLAN_TASKS_DEFAULT,
        )
        peak_agent_calls = (
            effective_wave
            * settings.agent_platform.max_parallel_queries_per_task
        )
        run_lane = (
            settings.server.run_max_concurrent
            or settings.server.max_concurrent
        )
        if peak_agent_calls > run_lane:
            # Visible constraint (No Silent Fallbacks): one run's widest
            # wave, each task running its sub-queries concurrently, can
            # demand more model calls than the lane admits. The excess
            # does not fail — it queues, and the run simply takes longer
            # for a reason nothing on any surface states. Loud at
            # startup, not discovered as a mysteriously slow agent.
            log.warning(
                "Effective wave width %d (plan limit) x "
                "INQTRIX_AGENT_MAX_PARALLEL_QUERIES_PER_TASK=%d = %d concurrent model "
                "calls from one run, but the lane admits only %d; waves queue. Increase"
                " MAX_CONCURRENT or reduce query concurrency.",
                effective_wave,
                settings.agent_platform.max_parallel_queries_per_task,
                peak_agent_calls,
                run_lane,
            )
        if database_ready and settings.sharing.restrict_to_workspace_members:
            from inqtrix.services.workspace_administration import (
                ensure_workspace_share_reconciliation,
            )

            startup_container = getattr(_app.state, "container", None)
            workspace_admin = getattr(
                startup_container, "workspace_admin", None
            )
            revoked = await ensure_workspace_share_reconciliation(
                _app,
                workspace_admin,
                tenant_id="default",
            )
            if revoked:
                log.warning(
                    "Workspace-Share-Reconciliation revoked %d invalid "
                    "active share(s) before readiness.",
                    revoked,
                )
            else:
                log.info(
                    "Workspace-Share-Reconciliation completed without changes."
                )
        startup_container = getattr(_app.state, "container", None)
        upload_reconciler = getattr(
            startup_container, "upload_reconciler", None
        )
        # Deliberately NOT gated on database_ready: a database blip during
        # boot would otherwise disable upload recovery for the whole
        # process lifetime. Each pass degrades to a WARNING on storage
        # errors and succeeds on its own once the database returns.
        # Accepted trade-off: unlike the queue-mode worker, which fences
        # every claim transaction on the schema head and re-runs the full
        # database contract probe on a coalesced interval, a reconciler
        # pass may resume previously accepted uploads while the boot
        # contract check still fails against a DML-compatible schema.
        if upload_reconciler is not None:
            upload_reconciler.start()
        # Same reasoning for dependency pauses: each sweep degrades to a
        # WARNING and the next one follows.
        indexing_auto_resumer = getattr(
            startup_container, "indexing_auto_resumer", None
        )
        if indexing_auto_resumer is not None:
            indexing_auto_resumer.start()
        # And for the run store's retention sweep, which keeps the
        # incognito window while nobody reads the store.
        run_retention_sweep = getattr(
            startup_container, "run_retention_sweep", None
        )
        if run_retention_sweep is not None:
            run_retention_sweep.start()
        try:
            yield
        finally:
            log.info(
                "Inqtrix server stopping | llm=%s | search=%s",
                llm_label,
                search_label,
            )
            # Flush the last span batch before the process dies — the
            # documented span-loss window of BatchSpanProcessor.
            from inqtrix.observability.otel import shutdown_tracing

            shutdown_tracing()
            container = getattr(_app.state, "container", None)
            # The retention sweep uses the run store: stop it before the store.
            run_retention_sweep = getattr(container, "run_retention_sweep", None)
            if run_retention_sweep is not None:
                run_retention_sweep.close()
            # Durable run stores own an engine and a background loop
            # thread; the memory store has no close() and is skipped.
            run_store = getattr(container, "run_store", None)
            if run_store is not None and hasattr(run_store, "close"):
                run_store.close()
            # The sweep uses the indexing store: stop it before the store.
            indexing_auto_resumer = getattr(container, "indexing_auto_resumer", None)
            if indexing_auto_resumer is not None:
                indexing_auto_resumer.close()
            indexing_service = getattr(container, "indexing_service", None)
            indexing_store = getattr(indexing_service, "job_store", None)
            if indexing_store is not None and hasattr(indexing_store, "close"):
                indexing_store.close()
            deletion_service = getattr(
                container, "asset_deletion_service", None
            )
            deletion_store = getattr(
                deletion_service, "operation_store", None
            )
            if deletion_store is not None and hasattr(deletion_store, "close"):
                deletion_store.close()
            upload_reconciler = getattr(container, "upload_reconciler", None)
            if upload_reconciler is not None:
                upload_reconciler.close()
            upload_service = getattr(container, "upload_operation_service", None)
            upload_store = getattr(upload_service, "operations", None)
            if upload_store is not None and hasattr(upload_store, "close"):
                upload_store.close()
            # The Postgres quota store owns a NullPool engine; dispose it
            # on the live loop here (record_blocking's throwaway loops
            # cannot). Memory store / disabled quota -> no-op.
            quota_service = getattr(container, "quota_service", None)
            if quota_service is not None:
                await quota_service.aclose()
            # Usage-ledger recorder: flush the remaining rows, then
            # dispose its NullPool engine on the live loop.
            from inqtrix.usage.recorder import (
                active_usage_recorder,
                set_active_usage_recorder,
            )

            usage_recorder = active_usage_recorder()
            if usage_recorder is not None:
                usage_recorder.close()
                set_active_usage_recorder(None)
                aclose_store = getattr(usage_recorder.store, "aclose", None)
                if callable(aclose_store):
                    await aclose_store()
            # The Postgres-canonical knowledge store owns its own NullPool
            # engine (loop-agnostic); dispose it on the live loop here.
            # Memory/Qdrant stores have no aclose -> guarded no-op.
            knowledge_service = getattr(container, "knowledge_service", None)
            knowledge = getattr(knowledge_service, "knowledge", None)
            store = getattr(knowledge, "store", None)
            if store is not None and hasattr(store, "aclose"):
                await store.aclose()
            # The Postgres chat-history store owns its own NullPool engine;
            # dispose it on the live loop here. Memory store -> no-op.
            chat_history_service = getattr(
                container, "chat_history_service", None
            )
            chat_store = getattr(chat_history_service, "store", None)
            if chat_store is not None and hasattr(chat_store, "aclose"):
                await chat_store.aclose()
            # The Postgres editor store owns its own NullPool engine too.
            editor_service = getattr(
                container, "editor_persistence_service", None
            )
            editor_store = getattr(editor_service, "store", None)
            if editor_store is not None and hasattr(editor_store, "aclose"):
                await editor_store.aclose()
            # The Postgres editor-patch store owns its own NullPool engine too.
            editor_patch_service = getattr(
                container, "editor_patch_service", None
            )
            editor_patch_store = getattr(editor_patch_service, "store", None)
            if editor_patch_store is not None and hasattr(
                editor_patch_store, "aclose"
            ):
                await editor_patch_store.aclose()
            # The Postgres asset-record store owns its own NullPool engine too.
            asset_service = getattr(container, "asset_records_service", None)
            asset_store = getattr(asset_service, "store", None)
            if asset_store is not None and hasattr(asset_store, "aclose"):
                await asset_store.aclose()
            # The Postgres vector-index store owns its own NullPool engine too.
            vector_index_service = getattr(container, "vector_index_service", None)
            vector_index_store = getattr(vector_index_service, "store", None)
            if vector_index_store is not None and hasattr(vector_index_store, "aclose"):
                await vector_index_store.aclose()
            # The Postgres account-preferences store owns its own NullPool engine.
            account_prefs_service = getattr(container, "account_preferences_service", None)
            account_prefs_store = getattr(account_prefs_service, "store", None)
            if account_prefs_store is not None and hasattr(account_prefs_store, "aclose"):
                await account_prefs_store.aclose()
            # The Postgres knowledge-session store owns its own NullPool engine.
            knowledge_sessions_service = getattr(container, "knowledge_sessions_service", None)
            knowledge_sessions_store = getattr(knowledge_sessions_service, "store", None)
            if knowledge_sessions_store is not None and hasattr(knowledge_sessions_store, "aclose"):
                await knowledge_sessions_store.aclose()
            collaboration_service = getattr(
                container, "editor_collaboration_service", None
            )
            if collaboration_service is not None:
                await collaboration_service.aclose()
            # Last: request code still unwinding above may hand work to a
            # lane, and a closed executor would reject it. Nothing here
            # depends on the lanes, so releasing them at the end is free.
            # The agent checkpointer holds its own psycopg pool, which no
            # engine disposal reaches.
            checkpointer = getattr(container, "agent_checkpointer", None)
            if checkpointer is not None:
                checkpointer.close()
            lanes = getattr(container, "execution_lanes", None)
            if lanes is not None:
                lanes.close()

    # Usage ledger: the provider wrappers feed llm_usage rows through the
    # process recorder; the lifespan finally closes it. Installed BEFORE the
    # routes, because the usage read surface resolves the recorder while it
    # is being built — a dependency created after its consumer silently
    # leaves that surface unmounted.
    from inqtrix.usage.recorder import install_usage_recorder

    install_usage_recorder(settings)

    # Fresh router per create_app() call to avoid duplicate route handlers
    app_router = create_router()

    container = register_routes(
        app_router,
        providers=providers,
        strategies=strategies,
        settings=settings,
        semaphore_factory=semaphore_factory,
        auth_provider=auth_provider,
        object_store_impl=object_store_impl,
    )

    enable_openapi = settings.server.enable_openapi
    app = FastAPI(
        title="Inqtrix Research Agent",
        docs_url="/docs" if enable_openapi else None,
        redoc_url="/redoc" if enable_openapi else None,
        openapi_url="/openapi.json" if enable_openapi else None,
        lifespan=_lifespan,
    )
    if cors_kwargs is not None:
        app.add_middleware(CORSMiddleware, **cors_kwargs)

    # Added LAST so Starlette places it OUTERMOST: the request id must
    # wrap every other middleware and error path, and the context reset
    # must be the final thing that runs. The proxy-hops policy is the
    # SAME one the login throttle trusts (XFF from the right).
    from inqtrix.server.request_context import RequestContextMiddleware

    app.add_middleware(
        RequestContextMiddleware,
        trusted_proxy_hops=settings.auth.trusted_proxy_hops,
    )

    @app.exception_handler(IntegrityError)
    async def _db_integrity_handler(
        request: Request, exc: IntegrityError
    ):
        return db_integrity_response(request, exc)

    app.include_router(app_router)
    app.state.container = container

    from inqtrix.auth.principal_generation import (
        install_principal_generation_error_handler,
    )

    install_principal_generation_error_handler(app)

    from inqtrix.server.database_gate import install_database_contract_gate

    install_database_contract_gate(app, container=container)

    from inqtrix.server.metrics import setup_metrics

    setup_metrics(app, container=container, settings=settings)

    return app
