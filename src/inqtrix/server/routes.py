"""Route registration facade over the per-surface router modules.

This module is a thin composition step: ``register_routes`` builds
the :class:`~inqtrix.server.container.AppContainer` and includes the
split routers from :mod:`inqtrix.server.routers`. The legacy signature
(including the injected ``api_key_dependency`` seam used by tests and
the multi-stack factory) is preserved verbatim; ``auth_provider`` is
the additive successor seam.

Deliberately NOT exported: ``agent_run`` / ``guarded_stream`` module
globals. The engine seam is ``inqtrix.research.web_research.run_web_graph``
(and the chat router's ``guarded_stream``); a name here would let a
monkeypatch of it silently no-op instead of failing loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from fastapi import APIRouter

from inqtrix.auth.principal import COOKIE_SESSION_MODES
from inqtrix.server.container import build_container
from inqtrix.server.routers import (
    auth_config,
    capabilities,
    chat,
    editor,
    files,
    health,
    knowledge,
    runs,
    session_titles,
    sources,
    test,
    text,
)
from inqtrix.settings import Settings

if TYPE_CHECKING:
    from fastapi import Request

    from inqtrix.auth.principal import AuthProvider
    from inqtrix.providers.base import ProviderContext
    from inqtrix.server.container import AppContainer
    from inqtrix.server.runs import RunStore
    from inqtrix.storage.object_store import ObjectStore
    from inqtrix.strategies import StrategyContext


def create_router() -> APIRouter:
    """Create a fresh APIRouter instance (avoids module-level reuse)."""
    return APIRouter()


def register_routes(
    _router: APIRouter,
    *,
    providers: "ProviderContext",
    strategies: "StrategyContext",
    settings: Settings,
    semaphore_factory: Any,
    api_key_dependency: Callable[["Request"], None] | None = None,
    auth_provider: "AuthProvider | None" = None,
    stacks: dict[str, Any] | None = None,
    default_stack: str = "",
    run_store: "RunStore | None" = None,
    object_store_impl: "ObjectStore | None" = None,
) -> "AppContainer":
    """Bind all routes to *_router* with dependency injection.

    Returns the built container (additive — historical callers ignore
    the return value) so ``create_app`` can expose it on ``app.state``
    for lifecycle hooks like the durable run store's ``close()``.

    *semaphore_factory* is a callable that returns the
    :class:`asyncio.Semaphore` for concurrency limiting (lazy init
    because the event loop may not exist at import time).

    *api_key_dependency* is the legacy injected-gate seam: an optional
    FastAPI dependency callable (typically built by
    :func:`inqtrix.server.security.make_api_key_dependency`). When
    supplied (and no *auth_provider* is), requests on gated routes run
    the callable and resolve to the static operator principal.
    ``/health``, ``/v1/models`` and ``/v1/stacks`` deliberately remain
    unauthenticated so Kubernetes probes and discovery clients keep
    working without credentials.

    *auth_provider* is the successor seam: a full
    :class:`~inqtrix.auth.principal.AuthProvider` whose
    ``resolve_principal`` gates every protected route. It wins over
    *api_key_dependency* when both are supplied.

    *stacks* and *default_stack* are the multi-stack registry from
    :func:`inqtrix.server.stacks.create_multi_stack_app`. When
    *stacks* is non-None the routes resolve the per-request
    ``body["stack"]`` field and override providers/strategies/settings
    with that bundle. When *stacks* is None the single-stack path stays
    in effect (the *providers* / *strategies* / *settings* args are
    used as-is).
    """
    container = build_container(
        providers=providers,
        strategies=strategies,
        settings=settings,
        semaphore_factory=semaphore_factory,
        auth_provider=auth_provider,
        api_key_dependency=api_key_dependency,
        stacks=stacks,
        default_stack=default_stack,
        run_store=run_store,
        object_store_impl=object_store_impl,
    )

    _router.include_router(health.build_router(container))
    _router.include_router(capabilities.build_router(container))
    _router.include_router(auth_config.build_router(container))
    _router.include_router(text.build_router(container))
    _router.include_router(session_titles.build_router(container))
    _router.include_router(editor.build_router(container))
    _router.include_router(runs.build_router(container))
    if container.prompt_template_service is not None:
        from inqtrix.server.routers.prompt_templates import (
            build_router as build_prompt_templates_router,
        )

        _router.include_router(build_prompt_templates_router(container))
    if container.skill_service is not None:
        from inqtrix.server.routers.skills import (
            build_router as build_skills_router,
        )

        _router.include_router(build_skills_router(container))
    _router.include_router(test.build_router(container))
    _router.include_router(chat.build_router(container))
    if container.chat_history_service is not None:
        from inqtrix.server.routers.chat_history import (
            build_router as build_chat_history_router,
        )

        _router.include_router(build_chat_history_router(container))
    if container.editor_persistence_service is not None:
        from inqtrix.server.routers.editor_persistence import (
            build_router as build_editor_persistence_router,
        )

        _router.include_router(build_editor_persistence_router(container))
    if container.editor_patch_service is not None:
        from inqtrix.server.routers.editor_patches import (
            build_router as build_editor_patches_router,
        )

        _router.include_router(build_editor_patches_router(container))
    if container.editor_collaboration_service is not None:
        from inqtrix.server.collaboration_gateway import (
            build_router as build_collaboration_gateway_router,
        )
        from inqtrix.server.routers.editor_collaboration import (
            build_router as build_editor_collaboration_router,
        )
        from inqtrix.server.routers.internal_collaboration import (
            build_router as build_internal_collaboration_router,
        )

        _router.include_router(build_collaboration_gateway_router(container))
        _router.include_router(build_editor_collaboration_router(container))
        _router.include_router(build_internal_collaboration_router(container))
    if container.editor_guest_link_service is not None:
        from inqtrix.server.routers.editor_guest_links import (
            build_router as build_editor_guest_links_router,
        )

        _router.include_router(build_editor_guest_links_router(container))
    if container.asset_records_service is not None:
        from inqtrix.server.routers.asset_records import (
            build_router as build_asset_records_router,
        )

        _router.include_router(build_asset_records_router(container))
    if container.knowledge_sessions_service is not None:
        from inqtrix.server.routers.knowledge_sessions import (
            build_router as build_knowledge_sessions_router,
        )

        _router.include_router(build_knowledge_sessions_router(container))
    if container.agent_control_service is not None:
        from inqtrix.server.routers.agent_runs import (
            build_router as build_agent_runs_router,
        )

        _router.include_router(build_agent_runs_router(container))
    if container.agent_sessions_service is not None:
        from inqtrix.server.routers.agent_sessions import (
            build_router as build_agent_sessions_router,
        )

        _router.include_router(build_agent_sessions_router(container))
    if container.vector_index_service is not None:
        from inqtrix.server.routers.vector_indexes import (
            build_router as build_vector_indexes_router,
        )

        _router.include_router(build_vector_indexes_router(container))
    if container.account_preferences_service is not None:
        from inqtrix.server.routers.account_preferences import (
            build_router as build_account_preferences_router,
        )

        _router.include_router(build_account_preferences_router(container))
    if container.agent_memory_service is not None:
        from inqtrix.server.routers.agent_memory import (
            build_router as build_agent_memory_router,
        )

        _router.include_router(build_agent_memory_router(container))
    if container.file_service is not None:
        _router.include_router(files.build_router(container))
    if container.knowledge_service is not None:
        _router.include_router(knowledge.build_router(container))
        _router.include_router(sources.build_router(container))
        if container.indexing_service is not None:
            from inqtrix.server.routers import indexing

            _router.include_router(indexing.build_router(container))
    if container.auth_provider.mode in COOKIE_SESSION_MODES:
        from inqtrix.server.routers.auth import build_auth_router

        _router.include_router(
            build_auth_router(
                container.auth_provider,
                container.principal_dependency,
                audit=container.permission_service.audit_sink,
            )
        )
        if getattr(container.auth_provider, "users", None) is not None:
            from inqtrix.server.routers.admin import build_admin_router

            _router.include_router(
                build_admin_router(
                    container.auth_provider,
                    container.principal_dependency,
                    deletions=container.asset_deletion_service,
                )
            )
            from inqtrix.server.routers.admin_system import (
                build_router as build_admin_system_router,
            )

            _router.include_router(build_admin_system_router(container))
            from inqtrix.server.routers.audit_admin import (
                build_router as build_audit_admin_router,
            )

            _router.include_router(build_audit_admin_router(container))
            from inqtrix.server.routers.admin_trace import (
                build_router as build_admin_trace_router,
            )

            _router.include_router(build_admin_trace_router(container))
            if container.knowledge_service is not None:
                from inqtrix.server.routers.admin_knowledge import (
                    build_router as build_admin_knowledge_router,
                )

                _router.include_router(build_admin_knowledge_router(container))
            if container.workspace_admin is not None:
                from inqtrix.server.routers.admin_workspaces import (
                    build_router as build_admin_workspaces_router,
                )

                _router.include_router(
                    build_admin_workspaces_router(container)
                )
        if (
            getattr(container.auth_provider, "invitations", None) is not None
            and container.workspace_admin is not None
        ):
            from inqtrix.server.routers.workspaces import (
                build_router as build_workspaces_router,
            )

            _router.include_router(build_workspaces_router(container))
        if container.share_service is not None:
            from inqtrix.server.routers.shares import (
                build_router as build_shares_router,
            )
            from inqtrix.server.routers.users import (
                build_router as build_users_router,
            )

            _router.include_router(build_shares_router(container))
            _router.include_router(build_users_router(container))
        if container.user_event_store is not None:
            from inqtrix.server.routers.user_events import (
                build_router as build_user_events_router,
            )

            _router.include_router(build_user_events_router(container))
        if container.quota_service is not None:
            from inqtrix.server.routers.quota import (
                build_router as build_quota_router,
            )

            _router.include_router(build_quota_router(container))
        # Bound to the ledger, NOT to quotas: usage is recorded whether or
        # not the deployment meters anyone.
        from inqtrix.usage.recorder import active_usage_recorder

        if active_usage_recorder() is not None:
            from inqtrix.server.routers.usage import (
                build_router as build_usage_router,
            )

            _router.include_router(build_usage_router(container))
    return container
