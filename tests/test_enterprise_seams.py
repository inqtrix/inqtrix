"""Replacement seams on the composition root.

`create_app` exposes additive injection points so an integrator can swap a
component WITHOUT editing the env-driven dispatch in `build_auth_provider` /
`build_object_store`:

* ``auth_provider=`` — a custom :class:`AuthProvider` wins over
  ``INQTRIX_AUTH_MODE`` resolution;
* ``object_store_impl=`` — a custom blob backend wins over the
  ``INQTRIX_OBJECT_STORE_BACKEND`` enum dispatch.

(The other stores already ride existing build_container params: a custom
run/queue store via ``run_store=``, a custom vector store via the knowledge
context, custom permissions via ``permissions=``.)
"""

from __future__ import annotations

import asyncio
import uuid

from inqtrix.auth.api_key import build_auth_provider
from inqtrix.auth.principal import NoneAuthProvider, Principal, UserContext
from inqtrix.providers.base import ProviderContext
from inqtrix.server import create_app
from inqtrix.server.app import _placeholder_secret_fields
from inqtrix.server.container import (
    build_container,
    build_platform_persistence_bundle,
)
from inqtrix.storage.db import build_process_engine
from inqtrix.settings import (
    AuthSettings,
    EditorGuestLinkSettings,
    QueueSettings,
    QuotaSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM


def _memory_settings() -> Settings:
    return Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
        auth=AuthSettings(mode="none"),
    )


def _providers() -> ProviderContext:
    return ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch())


def test_create_app_uses_injected_auth_provider():
    # A specific provider instance passed in must be the one the container
    # resolves — env mode resolution is bypassed entirely.
    injected = build_auth_provider(Settings(auth=AuthSettings(mode="none")))
    app = create_app(
        settings=_memory_settings(),
        providers=_providers(),
        auth_provider=injected,
    )
    assert app.state.container.auth_provider is injected


def test_container_binds_authenticated_pat_lifecycle_to_canonical_audit():
    settings = Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            oidc_insecure_dev_cookies=True,
        ),
    )
    provider = build_auth_provider(settings)
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=provider,
    )

    audit = container.permission_service.audit_sink
    assert provider.pats is not None
    assert provider.pat_service is not None
    assert provider.pats._audit is audit
    assert provider.pat_service._audit is audit


def test_create_app_object_store_impl_is_wired_into_file_service():
    # The FileService is built in every mode that has a file registry (the
    # memory/local default included — the storage backend only changes the
    # metadata registry), so an injected object store is used verbatim there,
    # winning over the INQTRIX_OBJECT_STORE_BACKEND enum dispatch.
    sentinel = object()
    app = create_app(
        settings=_memory_settings(),
        providers=_providers(),
        object_store_impl=sentinel,  # type: ignore[arg-type]
    )
    file_service = app.state.container.file_service
    assert file_service is not None
    assert file_service._object_store is sentinel


def test_memory_container_wires_editor_delete_invalidation() -> None:
    """The composition root must not drop the volatile-store fallback."""
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_memory_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    owner_user_id = uuid.UUID("11111111-2222-4333-8444-555555555555")
    visible_to = UserContext(
        principal=Principal(
            user_id=owner_user_id,
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )
    )
    service = container.editor_persistence_service
    asyncio.run(
        service.save_document(
            id="ed_container_delete",
            title="Delete me",
            content_markdown="body",
            folder_id=None,
            source="blank",
            source_run_id=None,
            revision=1,
            diff_anchor_markdown=None,
            diff_anchor_updated_at=None,
            created_at=1.0,
            updated_at=1.0,
            caller_user_id=owner_user_id,
            workspace_id=None,
            visible_to=visible_to,
        )
    )

    asyncio.run(
        service.delete_document(
            "ed_container_delete",
            visible_to=visible_to,
        )
    )
    page = asyncio.run(
        container.user_event_store.page_after(
            tenant_id="default",
            target_user_id=owner_user_id,
            cursor=0,
        )
    )

    assert [
        (event.scope, event.resource_type, event.resource_id)
        for event in page.events
    ] == [
        (
            "editor_documents",
            "editor_document",
            "ed_container_delete",
        )
    ]


_PG_URL = "postgresql+asyncpg://u:p@localhost:5432/db"
"""A dummy asyncpg URL: create_async_engine is lazy, so no connection is ever
opened — only the pool CLASS is exercised."""


def _pg_settings() -> Settings:
    return Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(
            backend="postgres",
            database_url=_PG_URL,
        ),
        auth=AuthSettings(mode="none"),
    )


def _bundle_engine(bundle):
    return bundle.session_factory.kw["bind"]


def _permission_engine(container):
    # The permission service's identity backend is the store that crashed on
    # agent resume; look at ITS engine, not just the bundle.
    return container.permission_service._members._session_factory.kw["bind"]


def test_the_platform_bundle_sits_on_the_engine_it_is_given():
    settings = _pg_settings()
    engine = build_process_engine(settings.storage)
    bundle = build_platform_persistence_bundle(settings, engine)
    assert _bundle_engine(bundle) is engine


def test_build_container_hands_its_engine_to_the_permission_service():
    settings = _pg_settings()
    engine = build_process_engine(settings.storage)
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        database_engine=engine,
    )
    assert container.database_engine is engine
    assert _permission_engine(container) is engine


def test_build_container_takes_the_engine_an_auth_provider_brought():
    # The documented library path builds the provider first and hands it to
    # create_app. Its stores already sit on the engine of the process.
    settings = _pg_settings()
    settings.auth = AuthSettings(
        mode="local", session_secret="s" * 32, pat_pepper="p" * 32
    )
    provider = build_auth_provider(settings)
    assert provider.database_engine is not None
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=provider,
    )
    assert container.database_engine is provider.database_engine
    assert _permission_engine(container) is provider.database_engine
    assert _directory_engine(provider.users) is provider.database_engine


def _run_authorizer(container):
    return container.run_service._dependency_authorizer


def _run_permission_engine(container):
    authorization = _run_authorizer(container)._authorization
    return authorization._members._session_factory.kw["bind"]


def _directory_engine(directory):
    return directory._session_factory.kw["bind"]


def test_requests_and_job_bodies_authorize_on_one_engine():
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    assert _run_authorizer(container)._authorization is container.permission_service
    assert _run_permission_engine(container) is container.database_engine


def test_admission_and_recording_share_one_quota_store_on_the_engine():
    settings = _pg_settings()
    settings.auth = AuthSettings(
        mode="local", session_secret="s" * 32, pat_pepper="p" * 32
    )
    settings.quota = QuotaSettings(enabled=True)
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=build_auth_provider(settings),
    )
    service = container.quota_service
    assert service is not None
    assert service._store._session_factory.kw["bind"] is container.database_engine


def test_the_transcript_reads_the_rule_catalog_of_the_requests():
    # The transcript reader re-checks bound library rules from job bodies.
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    run_catalog = container.agent_source_service.prompt_templates
    assert run_catalog is container.prompt_template_service
    assert _directory_engine(run_catalog._repository) is container.database_engine


def test_job_bodies_probe_the_canonical_directory_also_next_to_a_provider():
    # auth_provider.users backs login. The actor probe of a job body keeps its
    # own directory object on the same engine, as in every other process.
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_collaboration_settings("local"),
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    lookup = _run_authorizer(container)._user_lookup
    assert lookup is not None
    assert _directory_engine(lookup) is container.database_engine
    assert lookup is not getattr(container.auth_provider, "users", None)
    assert container.run_user_lookup is lookup


def test_a_provider_of_its_own_does_not_cost_job_bodies_their_directory():
    # An injected auth provider without a user directory is a documented seam.
    # Every run with a user would otherwise end as "no live user lookup".
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=NoneAuthProvider(),
    )
    assert getattr(container.auth_provider, "users", None) is None
    assert _run_authorizer(container)._user_lookup is container.run_user_lookup
    assert _directory_engine(container.run_user_lookup) is container.database_engine


def test_the_worker_has_a_user_lookup_without_an_auth_provider():
    # The worker composes WITHOUT an auth provider, so auth_provider.users is
    # None there. The actor probe still has to work.
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        serves_http=False,
    )
    lookup = _run_authorizer(container)._user_lookup
    assert getattr(container.auth_provider, "users", None) is None
    assert lookup is not None
    assert _directory_engine(lookup) is container.database_engine


def test_memory_backend_run_lane_shares_the_request_persistence():
    # A second memory bundle would be an EMPTY parallel identity universe:
    # every scoped run would resolve against zero memberships. Identity, not
    # just equality.
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
            auth=AuthSettings(mode="none"),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    authorizer = _run_authorizer(container)
    assert authorizer._authorization is container.permission_service
    assert authorizer._skill_service is container.skill_service


def test_injected_permissions_are_not_shadowed_by_a_run_lane():
    # With injected persistence, building a second object from settings anyway
    # would authorize runs against a different universe than the request path
    # and break the Enterprise seam.
    sentinel = object()
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        permissions=sentinel,
        file_service=object(),
    )
    assert _run_authorizer(container)._authorization is sentinel


def test_partially_injected_permissions_govern_the_run_lane():
    # Injection wins PER OBJECT: permissions injected alone (file_service
    # still settings-built) must govern the run lane too — a settings-built
    # permissions twin would silently authorize runs against a different
    # universe than the requests. The actor directory stays on the engine.
    sentinel = object()
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        permissions=sentinel,
    )
    authorizer = _run_authorizer(container)
    assert authorizer._authorization is sentinel
    assert _directory_engine(authorizer._user_lookup) is container.database_engine


def test_injected_file_service_skips_object_store_construction(monkeypatch):
    # An injected file_service means the env blob dispatch must stay
    # untouched — the deployment may have no object-store configuration at
    # all, and reaching it anyway would crash startup or create resources
    # nobody asked for.
    def _boom(settings):
        raise AssertionError(
            "build_object_store must not run with an injected file_service"
        )

    import inqtrix.server.container as container_module

    monkeypatch.setattr(container_module, "build_object_store", _boom)
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_settings(),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        file_service=object(),
    )
    # Job bodies still authorize against the settings-built permissions (only
    # file_service was injected).
    assert _run_permission_engine(container) is container.database_engine


def test_placeholder_secret_scan_flags_change_me_values():
    # A secret left at its deploy/.env.stack.example CHANGE_ME placeholder is
    # reported by name so create_app can warn loudly at startup (the guarantee
    # documented in docs/deployment/production-checklist.md). The value is never echoed.
    settings = Settings(
        auth=AuthSettings(
            session_secret="CHANGE_ME_SESSION_SECRET",
            pat_pepper="real-pepper-value-not-a-placeholder",
        ),
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    flagged = _placeholder_secret_fields(settings)
    assert "INQTRIX_SESSION_SECRET" in flagged
    assert "INQTRIX_PAT_PEPPER" not in flagged


def _pg_collaboration_settings(auth_mode: str) -> Settings:
    from inqtrix.settings import CollaborationSettings

    return Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        ),
        auth=AuthSettings(
            mode=auth_mode,
            session_secret="s" * 40,
            pat_pepper="p" * 40,
        ),
        collaboration=CollaborationSettings(
            enabled=True,
            http_url="http://collaboration:1234",
            ws_url="ws://collaboration:1234/collaboration",
            secret="c" * 40,
        ),
    )


def _pg_guest_link_settings(
    auth_mode: str,
    *,
    public_base_url: str = "https://inqtrix.example",
    allow_insecure_http: bool = False,
) -> Settings:
    from inqtrix.settings import CollaborationSettings

    return Settings(
        server=ServerSettings(public_base_url=public_base_url),
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        ),
        auth=AuthSettings(
            mode=auth_mode,
            session_secret="s" * 40,
            pat_pepper="p" * 40,
        ),
        queue=QueueSettings(valkey_url="redis://valkey:6379/0"),
        collaboration=CollaborationSettings(
            enabled=True,
            http_url="http://collaboration:1234",
            ws_url="ws://collaboration:1234/collaboration",
            secret="c" * 40,
        ),
        editor_guest_links=EditorGuestLinkSettings(
            enabled=True,
            token_hmac_secret="g" * 40,
            allow_insecure_http=allow_insecure_http,
        ),
    )


def test_worker_shape_container_builds_with_collaboration_enabled():
    # The queue worker composes WITHOUT an auth provider (it serves no HTTP,
    # so build_container falls back to NoneAuthProvider). The collaboration
    # gate must judge the DEPLOYMENT auth mode from settings there. Otherwise
    # enabling collaboration in the shared stack env crash-loops every worker
    # replica and queued runs are never claimed. The projection consumer gets
    # the canonical user directory from the platform bundle instead of the
    # absent provider.
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_collaboration_settings("local"),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        serves_http=False,
    )
    assert container.editor_collaboration_service is not None


def test_worker_shape_container_builds_with_guest_links_enabled():
    # Guest links depend on ShareService. The worker has no HTTP auth provider,
    # so it must use the deployment's cookie-auth mode and its canonical
    # user directory instead of crash-looping on a false "direct
    # sharing unavailable" diagnosis.
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=_pg_guest_link_settings("local"),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        serves_http=False,
    )
    assert getattr(container.auth_provider, "users", None) is None
    assert container.share_service is not None
    assert container.editor_guest_link_service is not None


def test_the_worker_judges_the_auth_mode_from_settings_without_collaboration():
    # Direct sharing depends on the deployment's cookie-auth mode as well, and
    # nothing but the settings can tell the worker what that mode is.
    settings = _pg_settings()
    settings.auth = AuthSettings(
        mode="local", session_secret="s" * 32, pat_pepper="p" * 32
    )
    container = build_container(
        providers=_providers(),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        serves_http=False,
    )
    assert container.share_service is not None


def test_worker_guest_links_still_fail_closed_without_cookie_auth():
    import pytest

    with pytest.raises(RuntimeError, match="cookie-session auth mode"):
        build_container(
            providers=_providers(),
            strategies=None,
            settings=_pg_guest_link_settings("none"),
            semaphore_factory=lambda: asyncio.Semaphore(1),
            serves_http=False,
        )


def test_guest_links_fail_closed_on_http_base_url_and_name_the_escape():
    # The HTTPS boot guard, pinned directly for the first time: plain
    # http fails loudly AND the message names the explicit dev opt-in
    # so the operator sees the way out in the same line.
    import pytest

    with pytest.raises(
        RuntimeError, match="ALLOW_INSECURE_HTTP"
    ):
        build_container(
            providers=_providers(),
            strategies=None,
            settings=_pg_guest_link_settings(
                "local", public_base_url="http://inqtrix.example"
            ),
            semaphore_factory=lambda: asyncio.Semaphore(1),
            serves_http=False,
        )


def test_guest_links_http_opt_in_builds_and_warns(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        container = build_container(
            providers=_providers(),
            strategies=None,
            settings=_pg_guest_link_settings(
                "local",
                public_base_url="http://inqtrix.example",
                allow_insecure_http=True,
            ),
            semaphore_factory=lambda: asyncio.Semaphore(1),
            serves_http=False,
        )
    assert container.editor_guest_link_service is not None
    assert any(
        "UNENCRYPTED HTTP" in record.getMessage()
        for record in caplog.records
    )


def test_guest_links_http_opt_in_still_requires_absolute_base_url():
    # The opt-in loosens the SCHEME, never the presence: the guest
    # origin check and link generation derive from the base URL.
    import pytest

    with pytest.raises(RuntimeError, match="absolute http"):
        build_container(
            providers=_providers(),
            strategies=None,
            settings=_pg_guest_link_settings(
                "local", public_base_url="", allow_insecure_http=True
            ),
            semaphore_factory=lambda: asyncio.Semaphore(1),
            serves_http=False,
        )


def test_collaboration_still_fails_closed_without_cookie_auth():
    # The API misconfiguration (collaboration on, deployment auth mode none)
    # keeps the loud fail-closed startup error.
    import pytest

    with pytest.raises(RuntimeError, match="cookie-session auth mode"):
        build_container(
            providers=_providers(),
            strategies=None,
            settings=_pg_collaboration_settings("none"),
            semaphore_factory=lambda: asyncio.Semaphore(1),
        )
