"""Composition root: build the service/algorithm wiring for the HTTP app.

``build_container`` is the single place where providers, strategies,
settings, the algorithm registry, the auth provider, and the
application services are assembled into one :class:`AppContainer`
(Constructor-First: services never read the environment; this builder
hands them everything). Both ``create_app`` (via ``register_routes``)
and tests that wire routers manually go through here, so there is
exactly one wiring truth.
"""

from __future__ import annotations

import logging
import os
import uuid

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fastapi import Depends

from inqtrix import aio
from inqtrix.auth.api_key import CallableGateAuthProvider
from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.content.memory import MemoryFileRegistry
from inqtrix.capabilities import build_capability_registry
from inqtrix.services.file_service import FileService
from inqtrix.auth.principal import (
    COOKIE_SESSION_MODES,
    AuthProvider,
    NoneAuthProvider,
    Principal,
    UserContext,
)
from inqtrix.core.algorithms import AlgorithmRegistry
from inqtrix.core.context import RuntimeContext
from inqtrix.knowledge.algorithm import KnowledgeAlgorithm
from inqtrix.knowledge.profiles import KnowledgeStageCeiling
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.embeddings import LiteLLMEmbeddings
from inqtrix.research.web_research import DirectLlmAlgorithm, WebResearchAlgorithm
from inqtrix.server.execution import ExecutionLanes
from inqtrix.runs.memory_store import RunStore
from inqtrix.services.agent_context import AgentContextResolver
from inqtrix.services.chat_service import ChatService
from inqtrix.services.health_service import HealthService
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.services.run_service import RunService
from inqtrix.settings import Settings

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncEngine

    from inqtrix.content.ports import FileRegistry
    from inqtrix.providers.base import ProviderContext
    from inqtrix.runs.ports import RunStorePort
    from inqtrix.storage.object_store import ObjectStore
    from inqtrix.strategies import StrategyContext


def build_default_registry() -> AlgorithmRegistry:
    """Register the built-in algorithms in their presentation order.

    Registration order is contract-relevant: the mode-validation error
    message lists ids in this order
    (``"mode muss 'research' oder 'direct_llm' sein"``).
    """
    registry = AlgorithmRegistry()
    registry.register(WebResearchAlgorithm())
    registry.register(DirectLlmAlgorithm())
    return registry


@dataclass(frozen=True)
class PlatformPersistence:
    """Bundle of the persistence collaborators one backend wave shares.

    Attributes:
        permissions: Authorization chokepoint over the identity ports.
        file_registry: File-metadata repository.
        audit: Append-only audit sink (the identity backend in both
            modes — the memory store implements the port too).
        session_factory: Async session factory in postgres mode;
            ``None`` in memory mode.
        workspace_admin: The identity backend instance for the
            workspace bootstrap surface (create/list) — the same
            object that backs the permission ports, never a second
            store.
    """

    permissions: AuthorizationService
    file_registry: "FileRegistry"
    audit: Any
    session_factory: Any | None
    workspace_admin: Any = None
    prompt_templates: Any = None
    skills: Any = None
    user_event_store: Any = None


def build_platform_persistence_bundle(
    settings: Settings,
    engine: "AsyncEngine | None",
) -> PlatformPersistence:
    """Settings bridge for the platform persistence layer.

    ``memory`` (the default) wires the no-infrastructure backends:
    scoped principals start with zero memberships, file metadata is
    process-local. ``postgres`` builds the identity repositories and
    the file registry on the engine of the process.
    """
    if settings.storage.backend == "postgres":
        from inqtrix.storage.content_postgres import PostgresFileRegistry
        from inqtrix.storage.db import build_session_factory
        from inqtrix.storage.identity_postgres import PostgresIdentityBackend

        session_factory = build_session_factory(engine)
        backend = PostgresIdentityBackend(
            session_factory=session_factory,
            app_role=settings.storage.app_role,
            restrict_to_workspace_members=(
                settings.sharing.restrict_to_workspace_members
            ),
        )
        from inqtrix.storage.prompt_templates_postgres import (
            PostgresPromptTemplateRepository,
        )
        from inqtrix.storage.skills_postgres import PostgresSkillRepository
        from inqtrix.storage.user_events_postgres import PostgresUserEventStore

        return PlatformPersistence(
            permissions=AuthorizationService(
                members=backend,
                shares=backend,
                audit=backend,
                restrict_to_workspace_members=(
                    settings.sharing.restrict_to_workspace_members
                ),
                sharing_enabled=settings.sharing.enabled,
            ),
            file_registry=PostgresFileRegistry(
                session_factory=session_factory,
                app_role=settings.storage.app_role,
            ),
            audit=backend,
            session_factory=session_factory,
            workspace_admin=backend,
            prompt_templates=PostgresPromptTemplateRepository(
                session_factory=session_factory,
                app_role=settings.storage.app_role,
                restrict_to_workspace_members=(
                    settings.sharing.restrict_to_workspace_members
                ),
                sharing_enabled=settings.sharing.enabled,
            ),
            skills=PostgresSkillRepository(
                session_factory=session_factory,
                app_role=settings.storage.app_role,
                restrict_to_workspace_members=(
                    settings.sharing.restrict_to_workspace_members
                ),
                sharing_enabled=settings.sharing.enabled,
            ),
            user_event_store=PostgresUserEventStore(
                session_factory=session_factory,
                app_role=settings.storage.app_role,
            ),
        )
    from inqtrix.content.prompt_templates import (
        MemoryPromptTemplateRepository,
    )
    from inqtrix.content.skills import MemorySkillRepository
    from inqtrix.user_events import MemoryUserEventStore

    store = MemoryIdentityStore(
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
    )
    return PlatformPersistence(
        permissions=AuthorizationService(
            members=store,
            shares=store,
            audit=store,
            restrict_to_workspace_members=(
                settings.sharing.restrict_to_workspace_members
            ),
            sharing_enabled=settings.sharing.enabled,
        ),
        file_registry=MemoryFileRegistry(),
        audit=store,
        session_factory=None,
        workspace_admin=store,
        prompt_templates=MemoryPromptTemplateRepository(),
        skills=MemorySkillRepository(),
        user_event_store=MemoryUserEventStore(),
    )


def _require_valid_queue_storage(settings: Settings) -> None:
    """Reject a Valkey queue without the Postgres source of truth.

    Shared by :func:`build_run_store` and :func:`build_indexing_store`:
    both stream backends carry only dispatch messages, so a queue
    without a durable Postgres row is a contradiction that must fail at
    startup instead of degrading silently.
    """
    if settings.queue.backend != "valkey":
        return
    if settings.storage.backend != "postgres":
        raise RuntimeError(
            ("INQTRIX_QUEUE_BACKEND=valkey requires INQTRIX_STORAGE_BACKEND=postgres; "
            "the Postgres job row is authoritative, and the stream is only the dispatch"
            " channel.")
        )
    if not settings.queue.valkey_url.strip():
        raise RuntimeError(
            "INQTRIX_QUEUE_BACKEND=valkey requires INQTRIX_VALKEY_URL to be set."
        )


def build_run_store(settings: Settings, engine: "AsyncEngine | None" = None, *, checkpoint_cleanup: Callable[[str, str], None] | None = None) -> "RunStorePort":
    """Settings bridge for the run-store backend (env-coupled surface).

    Memory storage keeps the historical in-process store bit-for-bit.
    ``INQTRIX_STORAGE_BACKEND=postgres`` makes run records, events,
    and results durable (execution stays in-process);
    ``INQTRIX_QUEUE_BACKEND=valkey`` additionally dispatches execution
    to ``inqtrix-worker`` processes. Contradictory combinations fail
    at startup instead of degrading silently.
    """
    _require_valid_queue_storage(settings)
    if settings.storage.backend != "postgres":
        store = RunStore.from_settings(
            settings.server,
            audit_service_starts=settings.observability.audit_service_starts,
        )
        if checkpoint_cleanup is not None:
            store.bind_checkpoint_cleanup(checkpoint_cleanup)
        return store

    import os
    import socket

    from inqtrix.runs.postgres_store import PostgresRunStore

    from inqtrix.storage.db import build_session_factory
    from inqtrix.storage.identity_postgres import PostgresIdentityBackend

    audit = PostgresIdentityBackend(
        session_factory=build_session_factory(engine),
        app_role=settings.storage.app_role,
    )
    queue = None
    if settings.queue.backend == "valkey":
        from inqtrix.runs.valkey_queue import ValkeyRunQueue

        queue = ValkeyRunQueue(url=settings.queue.valkey_url)
    return PostgresRunStore(
        engine=engine,
        checkpoint_cleanup=checkpoint_cleanup,
        app_role=settings.storage.app_role,
        queue=queue,
        max_concurrent=(
            settings.server.run_max_concurrent
            or settings.server.max_concurrent
        ),
        max_queue_size=settings.server.run_queue_max_size,
        # The durable store keeps terminal reports for a generous retention
        # window (default 90 days), NOT the in-memory store's short replay TTL
        # (run_completed_ttl_seconds) -- so research reports survive reloads,
        # re-logins, and other devices instead of being pruned after minutes.
        completed_ttl_seconds=settings.server.run_durable_retention_seconds,
        incognito_ttl_seconds=settings.server.run_incognito_retention_seconds,
        worker_id=f"api-{socket.gethostname()}-{os.getpid()}",
        audit=audit,
        max_concurrent_per_user=settings.server.run_max_concurrent_per_user,
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
        audit_service_starts=settings.observability.audit_service_starts,
        # Explicit instead of inferred from ``queue is None``: the API
        # process owns orphan recovery exactly when it executes runs
        # in-process (see resolve_orphan_sweep for why the inference is
        # wrong for the worker constructor).
        recover_orphans=queue is None,
    )


def build_indexing_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the reindex-job-store backend.

    Mirrors :func:`build_run_store` exactly: memory storage keeps the
    in-process job store byte-for-byte; ``INQTRIX_STORAGE_BACKEND=postgres``
    makes reindex records and events durable (in-process execution);
    ``INQTRIX_QUEUE_BACKEND=valkey`` additionally dispatches re-embeds to
    ``inqtrix-worker`` processes via a SEPARATE reindex stream.
    """
    from inqtrix.runs.indexing_memory import IndexingJobStore

    _require_valid_queue_storage(settings)
    if settings.storage.backend != "postgres":
        return IndexingJobStore.from_settings(settings.knowledge)

    import os
    import socket

    from inqtrix.runs.indexing_auto_resume import AutoResumePolicy
    from inqtrix.runs.indexing_postgres import PostgresIndexingJobStore


    queue = None
    if settings.queue.backend == "valkey":
        from inqtrix.runs.indexing_queue import ValkeyIndexingQueue

        queue = ValkeyIndexingQueue(url=settings.queue.valkey_url)
    return PostgresIndexingJobStore(
        engine=engine,
        app_role=settings.storage.app_role,
        queue=queue,
        max_concurrent=settings.knowledge.reindex_max_concurrent,
        max_queue_size=settings.knowledge.reindex_queue_max_size,
        completed_ttl_seconds=settings.knowledge.reindex_completed_ttl_seconds,
        history_limit=settings.knowledge.reindex_history_limit,
        worker_id=f"api-{socket.gethostname()}-{os.getpid()}",
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
        # Explicit instead of inferred from ``queue is None`` (see the
        # run-store constructor above for the rationale).
        recover_orphans=queue is None,
        auto_resume=AutoResumePolicy(
            max_attempts=settings.knowledge.reindex_auto_resume_attempts
        ),
    )


def build_deletion_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Build the aggregate-deletion operation store.

    Memory deployments execute in-process. Postgres persists operation
    receipts and stage transitions; Valkey adds worker dispatch while the
    Postgres row remains authoritative.
    """

    from inqtrix.runs.deletion_operations import DeletionOperationStore

    _require_valid_queue_storage(settings)
    if settings.storage.backend != "postgres":
        return DeletionOperationStore()

    import os
    import socket

    from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore

    queue = None
    if settings.queue.backend == "valkey":
        from inqtrix.runs.deletion_queue import ValkeyDeletionQueue

        queue = ValkeyDeletionQueue(url=settings.queue.valkey_url)
    return PostgresDeletionOperationStore(
        engine=engine,
        app_role=settings.storage.app_role,
        queue=queue,
        max_concurrent=settings.server.deletion_max_concurrent,
        completed_ttl_seconds=(
            settings.server.deletion_receipt_retention_seconds
        ),
        dispatch_timeout_seconds=(
            settings.server.deletion_dispatch_timeout_seconds
        ),
        worker_id=f"api-{socket.gethostname()}-{os.getpid()}",
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
    )


def build_upload_store(settings: Settings, engine: "AsyncEngine | None" = None, *, asset_store: Any) -> Any:
    """Build the original-file upload operation ledger and durable outbox."""

    _require_valid_queue_storage(settings)
    if settings.storage.backend != "postgres":
        from inqtrix.runs.upload_operations import MemoryUploadOperationStore

        return MemoryUploadOperationStore(assets=asset_store)

    import os
    import socket

    from inqtrix.runs.upload_postgres import PostgresUploadOperationStore

    queue = None
    if settings.queue.backend == "valkey":
        from inqtrix.runs.upload_queue import ValkeyUploadQueue

        queue = ValkeyUploadQueue(url=settings.queue.valkey_url)
    return PostgresUploadOperationStore(
        engine=engine,
        app_role=settings.storage.app_role,
        queue=queue,
        worker_id=f"api-{socket.gethostname()}-{os.getpid()}",
    )


def build_chat_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the chat-history store backend.

    Mirrors the other tiered store builders: the in-memory store is the
    default (and the offline test backend); ``INQTRIX_STORAGE_BACKEND=
    postgres`` makes chat threads/groups/messages durable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.project.chat_memory import MemoryChatStore

        return MemoryChatStore()
    from inqtrix.project.chat_postgres import PostgresChatStore

    return PostgresChatStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_editor_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the editor-persistence store backend.

    Mirrors :func:`build_chat_store`: in-memory default (and offline test
    backend); ``INQTRIX_STORAGE_BACKEND=postgres`` makes editor documents/
    folders/comments durable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.project.editor_memory import MemoryEditorStore

        return MemoryEditorStore()
    from inqtrix.project.editor_postgres import PostgresEditorStore

    return PostgresEditorStore(
        engine=engine,
        app_role=settings.storage.app_role,
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
    )


def build_editor_patch_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the editor-patch store backend.

    Mirrors :func:`build_editor_store`: in-memory default (offline/test);
    ``INQTRIX_STORAGE_BACKEND=postgres`` makes proposed/decided patches
    durable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.project.editor_patch_memory import MemoryEditorPatchStore

        return MemoryEditorPatchStore()
    from inqtrix.storage.editor_patch_postgres import PostgresEditorPatchStore

    return PostgresEditorPatchStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_asset_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the file-asset-record store backend.

    Mirrors :func:`build_chat_store`/:func:`build_editor_store`: in-memory
    default (offline/test); ``INQTRIX_STORAGE_BACKEND=postgres`` makes the
    file-library records (sections/groups/assets + extracted text) durable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.project.asset_records_memory import MemoryAssetStore

        return MemoryAssetStore()
    from inqtrix.project.asset_records_postgres import PostgresAssetStore

    return PostgresAssetStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_knowledge_session_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the knowledge-session store backend (Wissensmodus).

    Mirrors :func:`build_asset_store`: in-memory default (offline/test);
    ``INQTRIX_STORAGE_BACKEND=postgres`` makes saved Ask sessions durable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.project.knowledge_sessions_memory import (
            MemoryKnowledgeSessionStore,
        )

        return MemoryKnowledgeSessionStore()
    from inqtrix.project.knowledge_sessions_postgres import (
        PostgresKnowledgeSessionStore,
    )

    return PostgresKnowledgeSessionStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_vector_index_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the vector-index-record store backend.

    Mirrors :func:`build_asset_store`: in-memory default (offline/test);
    ``INQTRIX_STORAGE_BACKEND=postgres`` makes the client vector-index
    records (file<->collection mapping + members + run history) durable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.project.vector_index_memory import MemoryVectorIndexStore

        return MemoryVectorIndexStore()
    from inqtrix.project.vector_index_postgres import PostgresVectorIndexStore

    return PostgresVectorIndexStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_account_preferences_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the account-preferences store backend.

    Mirrors :func:`build_vector_index_store`: in-memory default (offline/test);
    ``INQTRIX_STORAGE_BACKEND=postgres`` makes the per-user UI preferences
    (theme/locale/contrast/bubble tone) durable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.project.account_preferences_memory import (
            MemoryAccountPreferencesStore,
        )

        return MemoryAccountPreferencesStore()
    from inqtrix.project.account_preferences_postgres import (
        PostgresAccountPreferencesStore,
    )

    return PostgresAccountPreferencesStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_agent_memory_candidate_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for reviewable agent-memory candidates.

    Accepted memories live in the configured provider; candidates remain
    Inqtrix-owned so user approval state is queryable and auditable.
    """
    if settings.storage.backend != "postgres":
        from inqtrix.agents.memory_candidates_memory import (
            MemoryAgentMemoryCandidateStore,
        )

        return MemoryAgentMemoryCandidateStore()
    from inqtrix.storage.agent_memory_postgres import (
        PostgresAgentMemoryCandidateStore,
    )

    return PostgresAgentMemoryCandidateStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_agent_feedback_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for personal workspace-agent feedback history."""
    if settings.storage.backend != "postgres":
        from inqtrix.agents.memory_candidates_memory import (
            MemoryAgentFeedbackStore,
        )

        return MemoryAgentFeedbackStore()
    from inqtrix.storage.agent_memory_postgres import (
        PostgresAgentFeedbackStore,
    )

    return PostgresAgentFeedbackStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def build_agent_memory_provider(settings: Settings) -> Any:
    """Settings bridge for the optional long-term memory provider."""
    provider = settings.agent_platform.memory_provider
    if provider == "none" or settings.agent_platform.memory_mode == "off":
        return None
    if provider == "mem0":
        if not settings.agent_platform.mem0_base_url.strip():
            log.warning(
                "INQTRIX_AGENT_MEMORY_PROVIDER=mem0 configured without "
                "INQTRIX_MEM0_BASE_URL; memory is unavailable."
            )
            return None
        from inqtrix.agents.memory_mem0 import Mem0AgentMemoryProvider

        return Mem0AgentMemoryProvider(
            base_url=settings.agent_platform.mem0_base_url,
            api_key=settings.agent_platform.mem0_api_key,
        )
    raise RuntimeError(f"Unknown agent memory provider: {provider!r}")


def build_agent_control_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the agent control store backend.

    Mirrors :func:`build_vector_index_store`: in-memory default
    (offline/test); ``INQTRIX_STORAGE_BACKEND=postgres`` makes plans,
    approvals, clarifications and artifacts durable (the approval and
    clarification decision writers run through the run store's session).
    """
    if settings.storage.backend != "postgres":
        from inqtrix.agents.control_memory import MemoryAgentControlStore

        return MemoryAgentControlStore()
    from inqtrix.storage.agent_control_postgres import (
        PostgresAgentControlStore,
    )

    return PostgresAgentControlStore(
        engine=engine,
        app_role=settings.storage.app_role,
        restrict_to_workspace_members=(
            settings.sharing.restrict_to_workspace_members
        ),
        sharing_enabled=settings.sharing.enabled,
    )


def build_agent_session_store(settings: Settings, engine: "AsyncEngine | None" = None) -> Any:
    """Settings bridge for the agent-sessions store backend."""
    if settings.storage.backend != "postgres":
        from inqtrix.project.agent_sessions_memory import (
            MemoryAgentSessionStore,
        )

        return MemoryAgentSessionStore()
    from inqtrix.project.agent_sessions_postgres import (
        PostgresAgentSessionStore,
    )

    return PostgresAgentSessionStore(
        engine=engine,
        app_role=settings.storage.app_role,
    )


def _run_share_resolvers(run_store: Any) -> tuple[Any, Any, Any]:
    """Owner, title and batch resolvers of shared runs over *run_store*."""

    async def owner(tenant_id: str, resource_id: str):
        return await run_store.aowner_user_id(resource_id)

    async def title(tenant_id: str, resource_id: str):
        return await run_store.atitle(resource_id)

    async def facts(tenant_id: str, resource_ids):
        return await run_store.ashare_facts(list(resource_ids))

    return owner, title, facts


def _require_shareable_object_store(settings: Settings) -> None:
    """Reject per-replica-disk blobs behind a load balancer.

    The ``local`` backend writes to THIS replica's filesystem: with more
    than one replica, a blob uploaded on replica A 404s on replica B —
    a silent data-availability split-brain. Refusing at startup mirrors
    :func:`_require_valid_queue_storage` (No Silent Fallbacks); the
    single-replica default stays zero-infrastructure.
    """
    storage = settings.storage
    if storage.object_store_backend == "local" and storage.replica_count > 1:
        raise RuntimeError(
            f'INQTRIX_OBJECT_STORE_BACKEND=local uses a per-replica disk and cannot be shared across INQTRIX_REPLICA_COUNT={storage.replica_count} replicas; use INQTRIX_OBJECT_STORE_BACKEND=s3 with an S3-compatible endpoint such as MinIO/SeaweedFS.'
        )


def build_object_store(settings: Settings) -> "ObjectStore":
    """Settings bridge for the blob store (env-coupled surface).

    ``local`` (default) needs nothing. ``s3`` accepts either explicit
    credentials or boto3's default credential chain; the
    :class:`~inqtrix.settings.StorageSettings` contract rejects incomplete
    combinations before this bridge runs. A configured private CA must be a
    readable file. ``local`` with more than one declared replica is rejected
    because blobs would be invisible across replicas.
    """
    from inqtrix.storage.object_store import LocalFSObjectStore, S3ObjectStore

    _require_shareable_object_store(settings)
    storage = settings.storage
    if storage.object_store_backend == "s3":
        if storage.s3_auth_mode == "static" and (
            not storage.s3_access_key or not storage.s3_secret_key
        ):
            raise RuntimeError(
                ("INQTRIX_S3_AUTH_MODE=static requires INQTRIX_S3_ACCESS_KEY and "
                "INQTRIX_S3_SECRET_KEY to be set.")
            )
        ca_bundle = storage.s3_ca_bundle or None
        if ca_bundle is not None:
            ca_path = Path(ca_bundle)
            if not ca_path.is_file() or not os.access(ca_path, os.R_OK):
                raise RuntimeError(
                    f'INQTRIX_S3_CA_BUNDLE must point to a readable file: {ca_path}'
                )
        return S3ObjectStore(
            bucket=storage.s3_bucket,
            endpoint_url=storage.s3_endpoint_url or None,
            access_key=(
                storage.s3_access_key
                if storage.s3_auth_mode == "static"
                else None
            ),
            secret_key=(
                storage.s3_secret_key
                if storage.s3_auth_mode == "static"
                else None
            ),
            session_token=(
                storage.s3_session_token or None
                if storage.s3_auth_mode == "static"
                else None
            ),
            region=storage.s3_region,
            addressing_style=storage.s3_addressing_style,
            bucket_provisioning=storage.s3_bucket_provisioning,
            ca_bundle=ca_bundle,
            server_side_encryption=storage.s3_server_side_encryption,
            kms_key_id=storage.s3_kms_key_id or None,
        )
    return LocalFSObjectStore(root=Path(storage.object_store_path))


def build_user_context_dependency(
    permissions: AuthorizationService,
    principal_dependency: Callable[..., Principal],
) -> Callable[..., Any]:
    """Build the FastAPI dependency resolving the request's user context.

    Composes with the principal dependency (FastAPI caches it
    per-request, so the auth gate still runs exactly once) and returns
    ``None`` for the legacy unscoped principals — routers pass the
    value straight into visibility-filtered store reads.
    """

    async def get_user_context(
        principal: Principal = Depends(principal_dependency),
    ) -> UserContext | None:
        return await permissions.resolve_user_context(principal)

    return get_user_context


def build_knowledge_context(
    settings: Settings,
    *,
    llm: Any | None = None,
    engine: "AsyncEngine | None" = None,
) -> KnowledgeProviderContext | None:
    """Settings bridge for the knowledge engine (env-coupled surface).

    Returns ``None`` when the engine is disabled — no embedding
    provider is constructed and no knowledge surface exists. The
    embedding endpoint falls back to the LiteLLM gateway configuration
    so a standard proxy deployment enables knowledge with a single
    flag.
    """
    if not settings.knowledge.enabled:
        for raw, setting in (
            (settings.knowledge.embedding_extra_headers, "INQTRIX_EMBEDDING_EXTRA_HEADERS"),
            (settings.knowledge.reranker_extra_headers, "INQTRIX_RERANKER_EXTRA_HEADERS"),
        ):
            if raw.strip():
                log.warning("CONFIG: %s is set but the knowledge engine is off; ignoring.", setting)
        return None
    from inqtrix.providers.headers import configured_headers

    embedding_headers = configured_headers(
        settings.knowledge.embedding_extra_headers,
        setting="INQTRIX_EMBEDDING_EXTRA_HEADERS",
        provider=f"{settings.knowledge.embedding_provider} embedding",
    )
    if settings.knowledge.embedding_provider == "azure":
        from inqtrix.providers.embeddings import AzureOpenAIEmbeddings

        if not settings.knowledge.embedding_azure_endpoint.strip():
            raise RuntimeError(
                ("INQTRIX_EMBEDDING_PROVIDER=azure requires an Azure endpoint "
                "(INQTRIX_EMBEDDING_AZURE_ENDPOINT or AZURE_AI_PROJECT_ENDPOINT).")
            )
        embeddings = AzureOpenAIEmbeddings(
            api_key=settings.knowledge.embedding_azure_api_key,
            azure_endpoint=settings.knowledge.embedding_azure_endpoint,
            api_version=settings.knowledge.embedding_azure_api_version,
            default_model=settings.knowledge.embedding_model,
            selectable_models=(
                settings.knowledge.selectable_embedding_model_list()
            ),
            timeout=settings.agent.reasoning_timeout,
            default_headers=embedding_headers,
        )
    else:
        embeddings = LiteLLMEmbeddings(
            api_key=(
                settings.knowledge.embedding_api_key.strip()
                or settings.server.litellm_api_key
            ),
            base_url=(
                settings.knowledge.embedding_base_url.strip()
                or settings.server.litellm_base_url
            ),
            default_model=settings.knowledge.embedding_model,
            selectable_models=(
                settings.knowledge.selectable_embedding_model_list()
            ),
            timeout=settings.agent.reasoning_timeout,
            default_headers=embedding_headers,
        )
    # Same instrumentation chokepoint as the LLM/search providers:
    # embedding batches and queries get gen_ai spans + duration; no-op
    # without the observability extra.
    from inqtrix.observability.content import build_content_policy
    from inqtrix.providers.tracing import instrument_embeddings

    embeddings = instrument_embeddings(
        embeddings, policy=build_content_policy(settings)
    )
    if settings.storage.backend == "postgres":
        # Postgres-canonical tier: collections/documents/chunks live
        # relationally (source of truth), the vectors in the vector index
        # (Qdrant when configured, in-process otherwise). It is awaited from
        # the HTTP loop and reached from the sync research graph and reindex
        # worker through aio.call_from_thread.
        from inqtrix.knowledge.stores.postgres_store import PostgresKnowledgeStore

        if settings.knowledge.vector_backend == "qdrant":
            from inqtrix.knowledge.stores.qdrant_store import QdrantVectorIndex

            vector_index: Any = QdrantVectorIndex(
                url=settings.knowledge.qdrant_url,
                api_key=settings.knowledge.qdrant_api_key,
                sparse=settings.knowledge.sparse,
            )
        else:
            from inqtrix.knowledge.stores.vector_index import MemoryVectorIndex

            vector_index = MemoryVectorIndex()
        store = PostgresKnowledgeStore(
            engine=engine,
            app_role=settings.storage.app_role,
            vector_index=vector_index,
            restrict_to_workspace_members=(
                settings.sharing.restrict_to_workspace_members
            ),
            sharing_enabled=settings.sharing.enabled,
        )
    elif settings.knowledge.vector_backend == "qdrant":
        from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore

        store = QdrantKnowledgeStore(
            url=settings.knowledge.qdrant_url,
            api_key=settings.knowledge.qdrant_api_key,
            sparse=settings.knowledge.sparse,
        )
    else:
        store = MemoryKnowledgeStore()

    reranker = None
    if settings.knowledge.reranker_provider == "cohere":
        from inqtrix.providers.rerankers import CohereRerank

        if (
            not settings.knowledge.reranker_base_url.strip()
            or not settings.knowledge.reranker_api_key
            or not settings.knowledge.reranker_model.strip()
        ):
            raise RuntimeError(
                ("INQTRIX_RERANKER_PROVIDER=cohere requires INQTRIX_RERANKER_BASE_URL, "
                "INQTRIX_RERANKER_API_KEY and INQTRIX_RERANKER_MODEL to be set.")
            )
        reranker = CohereRerank(
            api_key=settings.knowledge.reranker_api_key,
            base_url=settings.knowledge.reranker_base_url,
            default_model=settings.knowledge.reranker_model,
            timeout=settings.agent.reasoning_timeout,
            default_headers=configured_headers(
                settings.knowledge.reranker_extra_headers,
                setting="INQTRIX_RERANKER_EXTRA_HEADERS",
                provider="cohere rerank",
            ),
        )
    elif settings.knowledge.reranker_provider == "llm":
        if settings.knowledge.reranker_extra_headers.strip():
            log.warning(
                "CONFIG: INQTRIX_RERANKER_EXTRA_HEADERS is set but the llm reranker "
                "calls the LLM provider, which carries INQTRIX_LLM_EXTRA_HEADERS; ignoring."
            )
        from inqtrix.model_routing import resolve_model
        from inqtrix.providers.rerankers import LLMReranker

        if llm is None:
            raise RuntimeError(
                "INQTRIX_RERANKER_PROVIDER=llm requires a configured LLM provider."
            )
        provider_models = getattr(llm, "models", None)
        reranker = LLMReranker(
            llm,
            default_model=(
                (resolve_model("knowledge_rerank", provider_models, None) or "")
                if provider_models is not None
                else ""
            ),
            timeout=settings.agent.reasoning_timeout,
        )
    elif settings.knowledge.reranker_extra_headers.strip():
        log.warning(
            "CONFIG: INQTRIX_RERANKER_EXTRA_HEADERS is set but no reranker is configured "
            "(INQTRIX_RERANKER_PROVIDER=%s); ignoring.",
            settings.knowledge.reranker_provider,
        )

    if (
        settings.knowledge.vector_backend == "qdrant"
        and settings.knowledge.sparse != "off"
        and reranker is None
    ):
        # Eval-backed finding (golden set, text-embedding-3-large):
        # plain RRF fusion without a rerank stage degrades rank-1 on
        # paraphrase queries versus dense-only. Hybrid pays off for
        # exact/out-of-vocabulary terms and with a reranker on top.
        # Cross-lingual note: the BM25 branch is language-bound (monolingual),
        # so it contributes little when query and documents differ in language.
        # A multilingual cross-encoder reranker is the lever there — but it stays
        # OPTIONAL: `none` is the default and a valid choice, and a deployment
        # without a reranker keeps today's dense+BM25 path unchanged. The
        # recommended option is the `cohere` provider, which is a rerank-SCHEMA
        # adapter (not vendor-locked): native Cohere rerank-v3.5, Azure
        # serverless, or any compatible self-hosted endpoint. The `llm` provider
        # is a fallback whose multilingual quality depends on the configured LLM
        # and costs latency/tokens.
        log.warning(
            "Hybrid retrieval has no reranker: RRF can reduce top-1 precision for "
            "paraphrased questions, and the BM25 branch offers little help across "
            "languages (for example, a German question against English documents). "
            "Optionally configure a multilingual cross-encoder reranker (for example, "
            "INQTRIX_RERANKER_PROVIDER=cohere with rerank-v3.5 or a compatible "
            "endpoint), or explicitly keep reranking disabled. "
            "INQTRIX_KNOWLEDGE_SPARSE=off disables the sparse branch."
        )
    contextualizer = None
    if settings.knowledge.contextualize == "on":
        if llm is None:
            raise RuntimeError(
                ("INQTRIX_KNOWLEDGE_CONTEXTUALIZE=on requires an available LLM provider "
                "for ingestion.")
            )
        from inqtrix.knowledge.contextualize import LLMChunkContextualizer

        contextualizer = LLMChunkContextualizer(
            llm,
            timeout=settings.agent.reasoning_timeout,
            circuit_cooldown_seconds=(
                settings.knowledge.contextualization_circuit_cooldown_seconds
            ),
            circuit_probe_lease_seconds=(
                settings.knowledge.contextualization_circuit_probe_lease_seconds
            ),
        )

    return KnowledgeProviderContext(
        embeddings=embeddings,
        store=store,
        default_top_k=settings.knowledge.default_top_k,
        reranker=reranker,
        rerank_candidate_depth=settings.knowledge.rerank_candidate_depth,
        contextualizer=contextualizer,
    )


@dataclass(frozen=True)
class AppContainer:
    """Fully wired collaborators for the HTTP routers.

    Attributes:
        settings: Root settings of the deployment.
        providers: Default provider bundle.
        strategies: Default strategy bundle.
        registry: Algorithm registry (built-ins plus any extensions
            registered by the caller before route registration).
        runtime: App-level runtime context handed to algorithms.
        auth_provider: Active principal resolver.
        principal_dependency: FastAPI dependency yielding the request
            principal; attached to every gated route.
        permission_service: Authorization chokepoint (membership,
            shares, visibility). Memory-backed by default.
        user_context_dependency: FastAPI dependency yielding the
            server-resolved :class:`UserContext` for scoped principals
            and ``None`` for the legacy unscoped ones.
        resolver: Per-request stack/override/mode resolution.
        chat_service: Non-streaming chat execution.
        run_service: Native run submission.
        health_service: Health/model payload assembly.
        run_store: Run registry/queue (read-side surface) — the
            in-memory default or the durable Postgres backend, both
            behind :class:`~inqtrix.runs.ports.RunStorePort`.
        semaphore_factory: Lazy provider of the shared concurrency
            limiter (the event loop may not exist at wiring time).
        agent_checkpointer: Handle for the workspace agent's checkpoint
            store, or ``None`` when the agent is not registered. Carried
            here because it owns a pool on its own driver: no engine
            disposal reaches it, so whoever ends the container's life has
            to close it explicitly.
        execution_lanes: Named thread pools for blocking work, built
            from *settings* here, so a long AI call cannot leave an
            event stream or a short read without a thread. The caller
            that owns the container's lifetime closes them.
        knowledge_service: Knowledge collection/document/search service;
            ``None`` when the knowledge engine is disabled (no
            knowledge routes, no ``knowledge`` algorithm).
        stacks: Optional multi-stack registry for request resolution.
        default_stack: Default stack name when *stacks* is set.
    """

    settings: Settings
    providers: "ProviderContext"
    strategies: "StrategyContext"
    registry: AlgorithmRegistry
    runtime: RuntimeContext
    auth_provider: AuthProvider
    principal_dependency: Callable[..., Principal]
    permission_service: AuthorizationService
    user_context_dependency: Callable[..., Any]
    resolver: AgentContextResolver
    chat_service: ChatService
    run_service: RunService
    health_service: HealthService
    run_store: "RunStorePort"
    semaphore_factory: Callable[[], Any]
    execution_lanes: "ExecutionLanes"
    agent_checkpointer: Any = None
    kernel_gate_reason: str = ""
    """Which gate keeps ``mode=agent_kernel`` unregistered while the
    operator asked for it; empty when none does. The registration path is
    the only place that judges the gate, so the capability surface reports
    THIS verdict instead of deriving a second one."""
    knowledge_service: KnowledgeService | None = None
    file_service: FileService | None = None
    knowledge_ceiling: "KnowledgeStageCeiling | None" = None
    workspace_admin: Any = None
    share_service: Any = None
    prompt_template_service: Any = None
    skill_service: Any = None
    quota_service: Any = None
    indexing_service: Any = None
    chat_history_service: Any = None
    editor_persistence_service: Any = None
    editor_patch_service: Any = None
    """Editor-patch lifecycle (propose/apply/reject over the patch store
    pair); consumed by the patch router and the workspace-agent
    patch phase. ``None`` only in hand-built test containers."""
    editor_collaboration_service: Any = None
    """Optional Postgres-backed editor collaboration orchestration."""
    editor_guest_link_service: Any = None
    """Optional HTTPS-only account-less editor link orchestration."""
    agent_control_service: Any = None
    """Agent run control orchestration (plans/approvals/clarifications/
    artifacts); ``None`` only in hand-built test containers."""
    agent_sessions_service: Any = None
    """Agent-desk saved sessions (knowledge-sessions clone)."""
    agent_source_service: Any = None
    """Owner-authorized discovery and reads over saved conversation originals."""
    asset_records_service: Any = None
    knowledge_sessions_service: Any = None
    document_parser: Any = None
    vector_index_service: Any = None
    asset_deletion_service: Any = None
    upload_operation_service: Any = None
    upload_reconciler: Any = None
    indexing_auto_resumer: Any = None
    run_retention_sweep: Any = None
    account_preferences_service: Any = None
    user_event_store: Any = None
    session_factory: Any | None = None
    """Canonical session factory used by DB readiness checks."""
    agent_memory_service: Any = None
    capability_registry: Any = None
    run_user_lookup: Any = None
    """Actor directory that job bodies probe. The worker reads it instead of
    building its own — one definition, and the wiring cannot drift."""
    database_engine: Any = None
    """The one engine of this process. Whoever serves the process disposes it,
    last, after everything that uses it has stopped."""
    object_store_backend: str = "none"
    stacks: dict[str, Any] | None = None
    default_stack: str = ""


def build_container(
    *,
    providers: "ProviderContext",
    strategies: "StrategyContext",
    settings: Settings,
    semaphore_factory: Callable[[], Any],
    auth_provider: AuthProvider | None = None,
    api_key_dependency: Callable[["Request"], None] | None = None,
    stacks: dict[str, Any] | None = None,
    default_stack: str = "",
    run_store: "RunStorePort | None" = None,
    indexing_store: Any = None,
    deletion_store: Any = None,
    upload_store: Any = None,
    registry: AlgorithmRegistry | None = None,
    knowledge: KnowledgeProviderContext | None = None,
    permissions: AuthorizationService | None = None,
    file_service: FileService | None = None,
    workspace_admin: Any = None,
    object_store_impl: "ObjectStore | None" = None,
    document_parser: Any = None,
    database_engine: "AsyncEngine | None" = None,
    serves_http: bool = True,
) -> AppContainer:
    """Assemble the container from resolved collaborators.

    Auth resolution precedence (additive backwards compatibility):

    1. An injected *auth_provider* wins.
    2. A legacy injected *api_key_dependency* callable is adapted via
       :class:`~inqtrix.auth.api_key.CallableGateAuthProvider` —
       the historical ``register_routes(api_key_dependency=...)``
       seam keeps working unchanged.
    3. Otherwise the open-server :class:`NoneAuthProvider` applies
       (callers that want env-driven mode resolution build the
       provider via :func:`inqtrix.auth.api_key.build_auth_provider`
       and pass it in — ``create_app`` does exactly that).

    Without a *database_engine* the container takes the one an auth provider
    built beforehand, or builds the engine of the process itself; whoever serves
    the process finds it as ``database_engine``.

    ``serves_http`` is ``False`` for the queue worker: it has no auth provider,
    so what depends on the auth mode of the deployment is judged from settings.
    """
    if database_engine is None:
        from inqtrix.storage.db import resolve_process_engine

        database_engine = resolve_process_engine(auth_provider, settings.storage)
    if (
        settings.storage.backend == "postgres"
        and settings.storage.runtime_login_policy == "bundled_legacy"
    ):
        log.warning(
            "Database runtime login policy bundled_legacy is active. This "
            "compatibility boundary is supported only for the provided "
            "bundled PostgreSQL stack; managed/custom deployments must use "
            "a restricted runtime login."
        )
    if auth_provider is None:
        if api_key_dependency is not None:
            auth_provider = CallableGateAuthProvider(gate=api_key_dependency)
        else:
            auth_provider = NoneAuthProvider()

    active_knowledge = knowledge or build_knowledge_context(
        settings, llm=providers.llm, engine=database_engine
    )
    # An injected parser wins over the settings-derived ladder (the
    # Baukasten seam for a custom/stub parser, mirroring object_store_impl).
    # Preparing a file's text belongs to the files, not to the knowledge
    # engine: the parser exists whenever it is configured, reaches the file
    # service (uploads, /v1/files/{id}/text) and, with knowledge on, also
    # the knowledge service.
    if document_parser is None and settings.storage.document_parser == "markitdown":
        from inqtrix.knowledge.parsing import MarkItDownParser

        document_parser = MarkItDownParser()
    knowledge_service = None

    active_registry = registry or build_default_registry()
    knowledge_ceiling: KnowledgeStageCeiling | None = None
    if active_knowledge is not None:
        # ONE ceiling instance feeds both the algorithm and the
        # capabilities manifest — a second derivation would let the
        # two drift apart.
        knowledge_ceiling = KnowledgeStageCeiling(
            gate_available=settings.knowledge.gate == "on",
            grounding_available=settings.knowledge.grounding == "on",
            reranker_available=active_knowledge.reranker is not None,
            gate_max_rounds=settings.knowledge.gate_max_rounds,
            rerank_candidate_depth=settings.knowledge.rerank_candidate_depth,
        )
    if active_knowledge is not None and "knowledge" not in active_registry.ids():
        active_registry.register(
            KnowledgeAlgorithm(
                knowledge=active_knowledge,
                citation_base_url=settings.server.public_base_url,
                gate_enabled=settings.knowledge.gate == "on",
                grounding_enabled=settings.knowledge.grounding == "on",
                gate_max_rounds=settings.knowledge.gate_max_rounds,
            )
        )
    runtime = RuntimeContext(
        settings=settings,
        registry=active_registry,
        providers=providers,
        strategies=strategies,
    )
    resolver = AgentContextResolver(
        providers=providers,
        strategies=strategies,
        settings=settings,
        registry=active_registry,
        stacks=stacks,
        default_stack=default_stack,
    )
    from inqtrix.agents.checkpointing import build_checkpointer_handle, cleanup_run_checkpoint

    agent_checkpointer = build_checkpointer_handle(settings)
    def checkpoint_cleanup(run_id: str, mode: str) -> None:
        cleanup_run_checkpoint(agent_checkpointer, run_id, mode)
    # The initial durable-store retention sweep needs the same callback as
    # later explicit deletions. Injected worker stores bind before use here.
    active_run_store = run_store or build_run_store(
        settings, database_engine, checkpoint_cleanup=checkpoint_cleanup
    )
    active_run_store.bind_checkpoint_cleanup(checkpoint_cleanup)
    # Retention windows (the incognito one above all) hold while nobody
    # reads the store: the API process sweeps it on a clock in every tier,
    # on top of the cleanup every access runs. Without the queue the API
    # executes the runs; with it, API and workers share one database.
    from inqtrix.periodic_sweep import PeriodicSweep
    from inqtrix.runs.shared import RETENTION_SWEEP_SECONDS

    run_retention_sweep = PeriodicSweep(
        name="run-retention",
        label="Run retention",
        sweep=active_run_store.asweep,
        interval_seconds=RETENTION_SWEEP_SECONDS,
    )
    injected_file_service = file_service is not None
    needs_persistence = permissions is None or file_service is None
    active_object_store_backend = (
        "custom"
        if object_store_impl is not None or injected_file_service
        else settings.storage.object_store_backend
    )
    bundle = (
        build_platform_persistence_bundle(settings, database_engine)
        if needs_persistence
        else None
    )
    # An injected object store wins over the env enum dispatch — the
    # replacement seam for a custom blob backend (the other
    # stores ride run_store=/knowledge=/permissions=). Resolved only when
    # a FileService actually gets built here: with an injected
    # file_service the env dispatch must stay untouched (it may have no
    # blob configuration at all). The one resolved instance is shared
    # by everything that handles files.
    active_object_store = object_store_impl
    if needs_persistence:
        assert bundle is not None
        active_permissions = permissions or bundle.permissions
        if file_service is None:
            if active_object_store is None:
                active_object_store = build_object_store(settings)
            active_file_service = FileService(
                registry=bundle.file_registry,
                object_store=active_object_store,
                permissions=active_permissions,
                max_file_bytes=settings.storage.max_file_bytes,
                document_parser=document_parser,
            )
        else:
            active_file_service = file_service
    else:
        active_permissions = permissions
        active_file_service = file_service
    bind_pat_audit = getattr(auth_provider, "bind_pat_audit", None)
    if callable(bind_pat_audit):
        bind_pat_audit(active_permissions.audit_sink)
    active_workspace_admin = workspace_admin or (
        bundle.workspace_admin if bundle is not None else None
    )
    provider_users = getattr(auth_provider, "users", None)
    resource_invalidator = None
    if (
        active_workspace_admin is not None
        and bundle is not None
        and bundle.user_event_store is not None
    ):
        from inqtrix.user_events import ResourceInvalidator

        resource_invalidator = ResourceInvalidator(
            shares=active_workspace_admin,
            events=bundle.user_event_store,
        )
    if isinstance(active_workspace_admin, MemoryIdentityStore):
        active_workspace_admin.restrict_to_workspace_members = (
            settings.sharing.restrict_to_workspace_members
        )
        active_workspace_admin.sharing_enabled = settings.sharing.enabled
        if bundle is None or bundle.user_event_store is None:
            raise RuntimeError(
                "The in-memory identity backend requires a user-event store "
                "so atomic workspace/share effects cannot be dropped."
            )
        append_nowait = getattr(bundle.user_event_store, "append_nowait", None)
        if append_nowait is None:
            raise RuntimeError(
                "The in-memory identity backend requires a synchronous "
                "in-memory user-event sink."
            )
        active_admin_user_ids = getattr(
            provider_users, "active_admin_user_ids_nowait", None
        )
        active_workspace_admin.bind_user_event_sink(
            append_nowait,
            active_admin_user_ids=(
                active_admin_user_ids
                if callable(active_admin_user_ids)
                else None
            ),
        )
        if not active_workspace_admin.atomic_workspace_effects:
            raise RuntimeError(
                "The in-memory identity backend failed to bind atomic effects."
            )
    lifecycle = getattr(auth_provider, "lifecycle", None)
    bind_lifecycle_events = getattr(lifecycle, "bind_user_event_sink", None)
    if callable(bind_lifecycle_events):
        event_store = bundle.user_event_store if bundle is not None else None
        append_nowait = getattr(event_store, "append_nowait", None)
        if callable(append_nowait):
            bind_lifecycle_events(append_nowait)
    if (
        auth_provider.mode in COOKIE_SESSION_MODES
        and lifecycle is not None
        and not getattr(lifecycle, "atomic_effects", True)
    ):
        raise RuntimeError(
            "The scoped memory lifecycle requires the app user-event sink."
        )
    memory_authority: MemoryAuthorityCoordinator | None = None
    if (
        isinstance(active_workspace_admin, MemoryIdentityStore)
        and auth_provider.mode in COOKIE_SESSION_MODES
        and isinstance(provider_users, MemoryUserDirectory)
    ):
        memory_authority = MemoryAuthorityCoordinator()
        memory_authority.bind_users(provider_users)
        active_workspace_admin.bind_authority_coordinator(memory_authority)
        bind_lifecycle_authority = getattr(
            lifecycle, "bind_authority_coordinator", None
        )
        if callable(bind_lifecycle_authority):
            bind_lifecycle_authority(memory_authority)
    elif (
        isinstance(active_workspace_admin, MemoryIdentityStore)
        and auth_provider.mode in COOKIE_SESSION_MODES
        and lifecycle is not None
    ):
        raise RuntimeError(
            "The in-memory identity backend requires the canonical "
            "in-memory user directory."
        )
    if isinstance(active_run_store, RunStore):
        if not isinstance(active_workspace_admin, MemoryIdentityStore):
            raise RuntimeError(
                "The in-memory run store requires the in-memory identity "
                "backend for live share authorization."
            )
        active_run_store.bind_authorization(
            share_lookup=active_workspace_admin.permission_for_sync,
            share_workspace_check=active_workspace_admin.share_workspace_sync,
            resource_access_guard=(
                active_workspace_admin.resource_access_guard_sync
            ),
            restrict_to_workspace_members=(
                settings.sharing.restrict_to_workspace_members
            ),
        )
        if memory_authority is not None:
            active_run_store.bind_authority_coordinator(memory_authority)
    if (
        active_knowledge is not None
        and isinstance(active_knowledge.store, MemoryKnowledgeStore)
    ):
        if isinstance(active_workspace_admin, MemoryIdentityStore):
            active_knowledge.store.bind_authorization(
                resource_access_guard=(
                    active_workspace_admin.resource_access_guard_sync
                )
            )
            if memory_authority is not None:
                active_knowledge.store.bind_authority_coordinator(
                    memory_authority
                )
        elif auth_provider.mode in COOKIE_SESSION_MODES:
            raise RuntimeError(
                "The in-memory knowledge store requires the in-memory "
                "identity backend for atomic shared collection writes."
            )
    if active_knowledge is not None:
        knowledge_service = KnowledgeService(
            knowledge=active_knowledge,
            authorization=active_permissions,
            chunk_max_chars=settings.knowledge.chunk_max_chars,
            max_document_chars=settings.knowledge.max_document_chars,
            parser=document_parser,
            invalidator=resource_invalidator,
            generation_rollback_retention_seconds=(
                settings.knowledge.generation_rollback_retention_seconds
            ),
        )
    from inqtrix.services.prompt_template_service import (
        PromptTemplateService,
    )

    if bundle is not None and bundle.prompt_templates is not None:
        template_repository = bundle.prompt_templates
    else:
        # Injected-permissions composition (tests, embedders) gets the
        # in-process repository — visible here, never a silent skip.
        from inqtrix.content.prompt_templates import (
            MemoryPromptTemplateRepository,
        )

        template_repository = MemoryPromptTemplateRepository()
    bind_template_authority = getattr(
        template_repository, "bind_authority_coordinator", None
    )
    if memory_authority is not None and callable(bind_template_authority):
        bind_template_authority(memory_authority)
    prompt_template_service = PromptTemplateService(
        repository=template_repository,
        authorization=active_permissions,
        # Only the Postgres backend survives restarts; the capability
        # manifest must not invite browsers to sync against a store
        # that reads as "everything deleted" after a bounce.
        durable=settings.storage.backend == "postgres",
        invalidator=resource_invalidator,
    )
    from inqtrix.services.skill_service import SkillService

    if bundle is not None and bundle.skills is not None:
        skill_repository = bundle.skills
    else:
        from inqtrix.content.skills import MemorySkillRepository

        skill_repository = MemorySkillRepository()
    bind_skill_authority = getattr(
        skill_repository, "bind_authority_coordinator", None
    )
    if memory_authority is not None and callable(bind_skill_authority):
        bind_skill_authority(memory_authority)
    skill_service = SkillService(
        repository=skill_repository,
        authorization=active_permissions,
        durable=settings.storage.backend == "postgres",
        invalidator=resource_invalidator,
    )

    # One directory for every actor probe of a job body: the canonical one on
    # the engine, also where an injected auth provider brings its own, so the
    # security check reads the same universe as the requests.
    run_user_lookup = provider_users
    if bundle is not None and bundle.session_factory is not None:
        from inqtrix.storage.auth_postgres import PostgresUserDirectory

        run_user_lookup = PostgresUserDirectory(
            session_factory=bundle.session_factory,
            app_role=settings.storage.app_role,
        )
    # A serving API proves cookie authentication through its injected
    # provider. The queue worker deliberately has no HTTP auth provider, but
    # it loads the same deployment settings and owns a canonical Postgres user
    # directory. Treat that deployment mode and directory as the worker's
    # composition witness so enabling sharing or guest links cannot crash-loop
    # every worker replica.
    deployment_auth_mode = auth_provider.mode
    if not serves_http or settings.collaboration.enabled:
        from inqtrix.auth.principal import resolve_auth_mode

        deployment_auth_mode = resolve_auth_mode(
            settings.auth,
            settings.server,
            storage_backend=settings.storage.backend,
        )
    cookie_auth_available = auth_provider.mode in COOKIE_SESSION_MODES or (
        not serves_http
        and deployment_auth_mode in COOKIE_SESSION_MODES
    )
    sharing_users = provider_users
    if (
        sharing_users is None
        and not serves_http
        and deployment_auth_mode in COOKIE_SESSION_MODES
    ):
        sharing_users = run_user_lookup
    collaboration_store = None
    collaboration_users = None
    if settings.collaboration.enabled:
        if settings.storage.backend != "postgres":
            raise RuntimeError(
                "INQTRIX_COLLABORATION_ENABLED=true requires "
                "INQTRIX_STORAGE_BACKEND=postgres."
            )
        # The cookie-session requirement is a DEPLOYMENT property. The API
        # proves it through its active provider; the queue worker never
        # builds one (it serves no HTTP and composes with NoneAuthProvider),
        # so the env-resolved mode is the truthful witness there — without
        # it, enabling collaboration crash-looped every worker replica and
        # queued runs were never claimed.
        if (
            auth_provider.mode not in COOKIE_SESSION_MODES
            and deployment_auth_mode not in COOKIE_SESSION_MODES
        ):
            raise RuntimeError(
                "INQTRIX_COLLABORATION_ENABLED=true requires a cookie-session "
                "auth mode (local, oidc or gateway)."
            )
        if bundle is None or bundle.session_factory is None:
            raise RuntimeError(
                "Editor collaboration requires the canonical platform "
                "Postgres session factory."
            )
        if settings.collaboration.secret == settings.auth.session_secret:
            raise RuntimeError(
                "INQTRIX_COLLABORATION_SECRET must not reuse "
                "INQTRIX_SESSION_SECRET."
            )
        collaboration_users = provider_users
        if collaboration_users is None:
            # Non-serving processes fall back to the canonical directory, so
            # the projection consumer keeps working without an auth provider.
            from inqtrix.storage.auth_postgres import PostgresUserDirectory

            collaboration_users = PostgresUserDirectory(
                session_factory=bundle.session_factory,
                app_role=settings.storage.app_role,
            )
        from inqtrix.storage.editor_collaboration_postgres import (
            PostgresEditorCollaborationStore,
        )

        collaboration_store = PostgresEditorCollaborationStore(
            session_factory=bundle.session_factory,
            app_role=settings.storage.app_role,
            restrict_to_workspace_members=(
                settings.sharing.restrict_to_workspace_members
            ),
            sharing_enabled=settings.sharing.enabled,
            guest_links_enabled=settings.editor_guest_links.enabled,
        )
    # Editor persistence is constructed before sharing because editor
    # documents reuse the platform ShareService owner/title resolver maps.
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )

    editor_persistence_service = EditorPersistenceService(
        store=build_editor_store(settings, database_engine),
        durable=settings.storage.backend == "postgres",
        authorization=active_permissions,
        collaboration_store=collaboration_store,
        invalidator=resource_invalidator,
    )
    share_service = None
    # Sharing needs scoped principals + a user mirror — every cookie-session
    # mode qualifies (local, oidc, gateway), not just oidc. Serving processes use
    # their auth provider's directory; the non-serving worker uses the
    # canonical directory selected above. The single-operator
    # none/apikey modes remain excluded: their unscoped principal has no
    # identity to share with or as (use local for single-user-with-sharing).
    # Static principals remain unscoped; there is no implicit rescoping.
    if (
        settings.sharing.enabled
        and cookie_auth_available
        and active_workspace_admin is not None
        and sharing_users is not None
    ):
        from inqtrix.auth.shares import ShareService

        _run_owner, _run_title, _run_facts = _run_share_resolvers(active_run_store)

        async def _user_exists(tenant_id: str, user_id: uuid.UUID) -> bool:
            return await sharing_users.has_user_id(
                tenant_id=tenant_id, user_id=user_id
            )

        owner_resolvers: dict = {"run": _run_owner}
        # One batch resolver per owner resolver: the owner listing reads every
        # resource type with one query instead of one per shared resource.
        share_facts_resolvers: dict = {"run": _run_facts}
        unsupported_share_types: set[str] = set()
        # Title resolvers (recipient inbox + owner lifecycle listing) mirror the
        # owner resolvers one-for-one: same keys, owner-bypassing reads so a
        # pending recipient sees what they were offered — only titles/names
        # (run question, collection name, template title), never content or
        # any sensitive field.
        title_resolvers: dict = {"run": _run_title}
        collection_sharing_supported = bool(
            knowledge_service is not None
            and getattr(
                knowledge_service.knowledge.store,
                "supports_collection_sharing",
                False,
            )
        )
        if collection_sharing_supported and knowledge_service is not None:

            async def _collection(tenant_id: str, resource_id: str):
                from inqtrix.knowledge.stores.ports import CollectionNotFound

                try:
                    return await knowledge_service.knowledge.store.get_collection(
                        resource_id
                    )
                except CollectionNotFound:
                    return None

            async def _collection_owner(tenant_id: str, resource_id: str):
                collection = await _collection(tenant_id, resource_id)
                # Legacy collections (created_by_user_id None) have no
                # owner and need none — they are visible to everyone
                # already; None makes them unshareable (404).
                return collection.created_by_user_id if collection else None

            async def _collection_title(tenant_id: str, resource_id: str):
                collection = await _collection(tenant_id, resource_id)
                return collection.name if collection else None

            async def _collection_facts(tenant_id: str, resource_ids):
                return await knowledge_service.knowledge.store.collection_share_facts(
                    resource_ids
                )

            owner_resolvers["knowledge_collection"] = _collection_owner
            title_resolvers["knowledge_collection"] = _collection_title
            share_facts_resolvers["knowledge_collection"] = _collection_facts
        else:
            unsupported_share_types.add("knowledge_collection")

        async def _template_owner(tenant_id: str, resource_id: str):
            return await prompt_template_service.owner_user_id(
                tenant_id, resource_id
            )

        async def _template_title(tenant_id: str, resource_id: str):
            return await prompt_template_service.title(
                tenant_id, resource_id
            )

        async def _template_facts(tenant_id: str, resource_ids):
            return await prompt_template_service.share_facts(tenant_id, resource_ids)

        owner_resolvers["prompt_template"] = _template_owner
        title_resolvers["prompt_template"] = _template_title
        share_facts_resolvers["prompt_template"] = _template_facts

        async def _skill_owner(tenant_id: str, resource_id: str):
            return await skill_service.owner_user_id(tenant_id, resource_id)

        async def _skill_title(tenant_id: str, resource_id: str):
            return await skill_service.title(tenant_id, resource_id)

        async def _skill_facts(tenant_id: str, resource_ids):
            return await skill_service.share_facts(tenant_id, resource_ids)

        owner_resolvers["skill_template"] = _skill_owner
        title_resolvers["skill_template"] = _skill_title
        share_facts_resolvers["skill_template"] = _skill_facts

        async def _editor_document_owner(
            tenant_id: str, resource_id: str
        ):
            return await editor_persistence_service.share_owner_user_id(
                tenant_id, resource_id
            )

        async def _editor_document_title(
            tenant_id: str, resource_id: str
        ):
            return await editor_persistence_service.share_title(
                tenant_id, resource_id
            )

        async def _editor_document_facts(tenant_id: str, resource_ids):
            return await editor_persistence_service.share_facts(
                tenant_id, resource_ids
            )

        owner_resolvers["editor_document"] = _editor_document_owner
        title_resolvers["editor_document"] = _editor_document_title
        share_facts_resolvers["editor_document"] = _editor_document_facts

        share_service = ShareService(
            shares=active_workspace_admin,
            permissions=active_permissions,
            owner_resolvers=owner_resolvers,
            share_facts_resolvers=share_facts_resolvers,
            user_lookup=_user_exists,
            audit=active_workspace_admin,
            restrict_to_members=settings.sharing.restrict_to_workspace_members,
            title_resolvers=title_resolvers,
            invalidator=resource_invalidator,
            unsupported_resource_types=tuple(unsupported_share_types),
        )

    editor_guest_link_store = None
    if settings.editor_guest_links.enabled:
        from urllib.parse import urlsplit

        if not settings.sharing.enabled:
            raise RuntimeError(
                "INQTRIX_EDITOR_GUEST_LINKS_ENABLED=true requires "
                "INQTRIX_SHARING_ENABLED=true."
            )
        if not settings.collaboration.enabled:
            raise RuntimeError(
                "INQTRIX_EDITOR_GUEST_LINKS_ENABLED=true requires "
                "INQTRIX_COLLABORATION_ENABLED=true."
            )
        if settings.storage.backend != "postgres":
            raise RuntimeError(
                "INQTRIX_EDITOR_GUEST_LINKS_ENABLED=true requires "
                "INQTRIX_STORAGE_BACKEND=postgres."
            )
        if share_service is None:
            raise RuntimeError(
                "INQTRIX_EDITOR_GUEST_LINKS_ENABLED=true requires a cookie-session "
                "auth mode (local, oidc or gateway) and direct sharing."
            )
        if not settings.queue.valkey_url.strip():
            raise RuntimeError(
                "INQTRIX_EDITOR_GUEST_LINKS_ENABLED=true requires "
                "INQTRIX_VALKEY_URL for shared password throttling."
            )
        public_scheme = urlsplit(settings.server.public_base_url).scheme.lower()
        if public_scheme != "https":
            if not settings.editor_guest_links.allow_insecure_http:
                raise RuntimeError(
                    "INQTRIX_EDITOR_GUEST_LINKS_ENABLED=true requires an "
                    "HTTPS INQTRIX_PUBLIC_BASE_URL. For local development "
                    "over plain HTTP, explicitly opt in with "
                    "INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP=true "
                    "(guest tokens and passwords then cross the wire "
                    "unencrypted — never in production)."
                )
            if public_scheme != "http":
                # The opt-in never covers a missing/broken base URL: the
                # guest origin check and link generation both derive
                # from it, so an empty URL would 403 every guest.
                raise RuntimeError(
                    "INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP=true "
                    "still requires an absolute http(s) "
                    "INQTRIX_PUBLIC_BASE_URL."
                )
            log.warning(
                "Editor guest links use UNENCRYPTED HTTP "
                "(INQTRIX_EDITOR_GUEST_LINKS_ALLOW_INSECURE_HTTP): guest tokens, "
                "passwords and sessions are readable on the network path; never use "
                "this in production."
            )
        if settings.editor_guest_links.token_hmac_secret in {
            settings.auth.session_secret,
            settings.collaboration.secret,
        }:
            raise RuntimeError(
                "INQTRIX_EDITOR_GUEST_LINK_TOKEN_SECRET must not reuse "
                "INQTRIX_SESSION_SECRET or INQTRIX_COLLABORATION_SECRET."
            )
        assert bundle is not None
        assert bundle.session_factory is not None
        from inqtrix.storage.editor_guest_link_postgres import (
            PostgresEditorGuestLinkStore,
        )

        editor_guest_link_store = PostgresEditorGuestLinkStore(
            session_factory=bundle.session_factory,
            app_role=settings.storage.app_role,
        )
    # Quota service: only when enabled AND a multi-user mode
    # (local, oidc, gateway). The single-operator none/apikey/demo modes are
    # never metered — unscoped principals would bypass anyway, and not
    # constructing it keeps them byte-identical. Store mirrors the backend.
    quota_service = None
    if settings.quota.enabled and auth_provider.mode in COOKIE_SESSION_MODES:
        from inqtrix.services.quota_service import QuotaService

        if settings.storage.backend == "postgres":
            from inqtrix.storage.db import build_session_factory
            from inqtrix.storage.quota_postgres import PostgresQuotaStore

            quota_store: Any = PostgresQuotaStore(
                session_factory=build_session_factory(database_engine),
                app_role=settings.storage.app_role,
            )
        else:
            from inqtrix.quota.memory import MemoryQuotaStore

            quota_store = MemoryQuotaStore()
        quota_service = QuotaService(
            store=quota_store,
            settings=settings.quota,
        )
    # Background reindex (re-embed) jobs: only when the knowledge engine
    # is wired. The in-memory job store mirrors the run store; quota (when
    # on) meters the per-document embedding spend. None keeps deployments
    # without knowledge byte-identical (no reindex surface).
    indexing_service = None
    indexing_auto_resumer = None
    active_indexing_store = None
    if knowledge_service is not None:
        from inqtrix.services.execution_dependency_authority import (
            CollectionEditAuthorizer,
        )
        from inqtrix.services.indexing_service import IndexingService

        active_indexing_store = indexing_store or build_indexing_store(settings, database_engine)
        contextualizer = active_knowledge.contextualizer
        circuit = getattr(
            active_indexing_store,
            "contextualization_circuit",
            None,
        )
        bind_contextualization_circuit = getattr(
            contextualizer,
            "bind_circuit_breaker",
            None,
        )
        if (
            contextualizer is not None
            and callable(bind_contextualization_circuit)
        ):
            if circuit is None:
                raise RuntimeError(
                    "Contextualization requires an indexing-store circuit "
                    "authority."
                )
            bind_contextualization_circuit(circuit)
        bind_indexing_authority = getattr(
            active_indexing_store, "bind_authority_coordinator", None
        )
        if memory_authority is not None and callable(bind_indexing_authority):
            bind_indexing_authority(memory_authority)
        indexing_service = IndexingService(
            knowledge_service=knowledge_service,
            job_store=active_indexing_store,
            quota_service=quota_service,
            authority=CollectionEditAuthorizer(
                authorization=active_permissions,
                knowledge_service=knowledge_service,
                user_lookup=run_user_lookup,
            ),
        )
        # Dependency pauses resume on their own from this process in every
        # tier: the API's resume path is the one a person's click takes. The
        # indexing worker cannot drive it: its store claims queued work but
        # has no queue to dispatch a resumed job to.
        policy = getattr(active_indexing_store, "auto_resume_policy", None)
        if policy is not None and policy.enabled:
            from inqtrix.runs.indexing_auto_resume import SWEEP_INTERVAL_SECONDS

            indexing_auto_resumer = PeriodicSweep(
                name="indexing-auto-resume",
                label="Indexing auto-resume",
                sweep=partial(aio.run_legacy_segment, indexing_service.resume_due_pauses),
                interval_seconds=SWEEP_INTERVAL_SECONDS,
            )
    # Project-persistence tier: chat history becomes server-persistent
    # when a Postgres backend is present. Always wired (the memory store is
    # the offline/test tier); the `durable` flag — true only for Postgres —
    # gates the capability the frontend reads, so the volatile tier is never
    # advertised as a durable replacement for the local markdown project.
    from inqtrix.services.chat_history_service import ChatHistoryService

    chat_history_service = ChatHistoryService(
        store=build_chat_store(settings, database_engine),
        durable=settings.storage.backend == "postgres",
    )
    editor_collaboration_service = None
    if settings.collaboration.enabled:
        from inqtrix.services.collaboration_client import (
            CollaborationNodeClient,
        )
        from inqtrix.services.editor_collaboration_service import (
            EditorCollaborationService,
        )
        assert collaboration_store is not None
        assert collaboration_users is not None
        collaboration_node = CollaborationNodeClient(
            base_url=settings.collaboration.http_url,
            secret=settings.collaboration.secret,
        )
        editor_collaboration_service = EditorCollaborationService(
            store=collaboration_store,
            documents=editor_persistence_service,
            node=collaboration_node,
            settings=settings.collaboration,
            users=collaboration_users,
            guest_links=editor_guest_link_store,
        )
        editor_persistence_service.bind_collaboration_projector(
            editor_collaboration_service.flush_projection
        )
    editor_guest_link_service = None
    if settings.editor_guest_links.enabled:
        assert editor_guest_link_store is not None
        assert editor_collaboration_service is not None
        from inqtrix.auth.guest_ratelimit import (
            ValkeyGuestLinkRateLimiter,
        )
        from inqtrix.services.editor_guest_link_service import (
            EditorGuestLinkService,
        )

        editor_guest_link_service = EditorGuestLinkService(
            store=editor_guest_link_store,
            collaboration=editor_collaboration_service,
            settings=settings.editor_guest_links,
            public_base_url=settings.server.public_base_url,
            rate_limiter=ValkeyGuestLinkRateLimiter(
                url=settings.queue.valkey_url,
                max_attempts=5,
                window_seconds=5 * 60,
                lockout_seconds=15 * 60,
            ),
        )
    # Editor patches: the persisted proposal/decision lifecycle over
    # the documents above; collaboration patches delegate their Yjs mutation
    # to the optional serialized sidecar instead of writing Markdown.
    from inqtrix.services.editor_patch_service import EditorPatchService

    editor_patch_service = EditorPatchService(
        store=build_editor_patch_store(settings, database_engine),
        editor_persistence=editor_persistence_service,
        collaboration=editor_collaboration_service,
        audit=active_workspace_admin,
        durable=settings.storage.backend == "postgres",
    )
    from inqtrix.services.asset_records_service import AssetRecordsService

    asset_store = build_asset_store(settings, database_engine)
    vector_store = build_vector_index_store(settings, database_engine)
    shared_source_authority = None
    if settings.storage.backend != "postgres":
        from inqtrix.storage.source_authority import MemorySourceLifecycleAuthority

        shared_source_authority = (
            getattr(active_knowledge.store, "source_lifecycle_authority", None)
            if active_knowledge is not None
            else None
        ) or MemorySourceLifecycleAuthority()
        for source_consumer in (asset_store, vector_store):
            bind_source_authority = getattr(
                source_consumer, "bind_source_lifecycle_authority", None
            )
            if callable(bind_source_authority):
                bind_source_authority(shared_source_authority)
        if active_knowledge is not None:
            bind_knowledge_source_authority = getattr(
                active_knowledge.store,
                "bind_source_lifecycle_authority",
                None,
            )
            if callable(bind_knowledge_source_authority):
                bind_knowledge_source_authority(shared_source_authority)

    asset_records_service = AssetRecordsService(
        store=asset_store,
        durable=settings.storage.backend == "postgres",
    )
    active_deletion_store = deletion_store or build_deletion_store(settings, database_engine)
    if shared_source_authority is not None:
        bind_deletion_source_authority = getattr(
            active_deletion_store,
            "bind_source_lifecycle_authority",
            None,
        )
        if callable(bind_deletion_source_authority):
            bind_deletion_source_authority(shared_source_authority)
    # Memory tier: deletion index rows (asset.delete_*) land in the
    # in-memory audit trail via the coordinator, mirroring the
    # Postgres store's in-transaction writes.
    if memory_authority is not None:
        bind_deletion_audit = getattr(
            active_deletion_store, "bind_authority_coordinator", None
        )
        if callable(bind_deletion_audit):
            bind_deletion_audit(memory_authority)
    from inqtrix.services.knowledge_sessions_service import (
        KnowledgeSessionsService,
    )

    knowledge_sessions_service = KnowledgeSessionsService(
        store=build_knowledge_session_store(settings, database_engine),
        run_store=active_run_store,
        durable=settings.storage.backend == "postgres",
    )
    from inqtrix.services.vector_index_service import VectorIndexService

    vector_index_service = VectorIndexService(
        store=vector_store,
        durable=settings.storage.backend == "postgres",
    )
    from inqtrix.services.upload_operation_service import (
        UploadOperationService,
        UPLOAD_RECONCILE_SECONDS,
    )

    active_upload_store = upload_store or build_upload_store(
        settings, database_engine, asset_store=asset_store
    )
    upload_operation_service = UploadOperationService(
        operations=active_upload_store,
        files=active_file_service,
        assets=asset_records_service,
        quota=quota_service,
        max_attempts=settings.queue.worker_max_attempts,
        # file.uploaded index rows — one chokepoint for sync
        # and worker-deferred uploads alike. getattr-defensive: injected
        # permission doubles (enterprise seams) carry no sink, and
        # telemetry never imposes one.
        audit=getattr(active_permissions, "audit_sink", None),
    )
    upload_reconciler = None
    if settings.queue.backend != "valkey":
        from inqtrix.storage.migration_contract import SchemaHeadMismatch

        upload_reconciler = PeriodicSweep(
            name="upload-reconciler",
            label="Upload reconciler",
            sweep=upload_operation_service.reconcile,
            interval_seconds=UPLOAD_RECONCILE_SECONDS,
            finished_by=(SchemaHeadMismatch,),
        )
    from inqtrix.services.asset_deletion_service import AssetDeletionService

    asset_deletion_service = AssetDeletionService(
        assets=asset_records_service,
        operation_store=active_deletion_store,
        files=active_file_service,
        knowledge=knowledge_service,
        vector_indexes=vector_index_service,
        indexing_jobs=active_indexing_store,
        quota=quota_service,
        uploads=upload_operation_service,
    )
    if knowledge_service is not None:
        knowledge_service.bind_collection_deletion(
            active_check=active_deletion_store.ahas_collection_deletion
        )
        knowledge_service.bind_document_deletion(
            active_check=active_deletion_store.ahas_document_deletion
        )
    from inqtrix.services.account_preferences_service import (
        AccountPreferencesService,
    )

    account_preferences_service = AccountPreferencesService(
        store=build_account_preferences_store(settings, database_engine),
        durable=settings.storage.backend == "postgres",
    )
    from inqtrix.services.agent_memory_service import AgentMemoryService

    agent_memory_service = AgentMemoryService(
        candidate_store=build_agent_memory_candidate_store(settings, database_engine),
        feedback_store=build_agent_feedback_store(settings, database_engine),
        provider=build_agent_memory_provider(settings),
        provider_name=settings.agent_platform.memory_provider,
        mode=settings.agent_platform.memory_mode,
        durable=settings.storage.backend == "postgres",
        # Long-term memory is opt-in per user (privacy default OFF): the
        # service reads the flag from the account-preferences store.
        account_preferences=account_preferences_service.store,
    )
    # Agent control persistence: plans/approvals/clarifications/
    # artifacts, composed with the run store so an interrupt resolution
    # and the run's waiting->queued resume commit in ONE transaction;
    # audit through the platform sink.
    from inqtrix.services.agent_control_service import AgentControlService
    from inqtrix.services.agent_sessions_service import AgentSessionsService

    agent_control_store = build_agent_control_store(settings, database_engine)
    bind_control_authority = getattr(
        agent_control_store, "bind_authority_coordinator", None
    )
    bind_control_execution = getattr(
        agent_control_store, "bind_execution_guard", None
    )
    if memory_authority is not None and callable(bind_control_authority):
        bind_control_authority(memory_authority)
    if isinstance(active_run_store, RunStore) and callable(bind_control_execution):
        bind_control_execution(active_run_store.execution_control_guard)
    agent_control_service = AgentControlService(
        store=agent_control_store,
        run_store=active_run_store,
        audit=active_workspace_admin,
        editor_persistence=editor_persistence_service,
        # The ONE collection-visibility gate, so an edited plan's rag tasks
        # are visibility-checked at approval time, not only at task-run time.
        knowledge=knowledge_service,
        # Library rules attached at a plan gate resolve against the
        # DECIDING caller's own catalog (never against client text).
        prompt_templates=prompt_template_service,
        durable=settings.storage.backend == "postgres",
        max_plan_tasks=settings.agent_platform.max_plan_tasks,
    )
    agent_sessions_service = AgentSessionsService(
        store=build_agent_session_store(settings, database_engine),
        run_store=active_run_store,
        durable=settings.storage.backend == "postgres",
    )
    from inqtrix.services.execution_dependency_authority import (
        ExecutionDependencyAuthorizer,
    )

    run_service = RunService(
        registry=active_registry,
        runtime=runtime,
        run_store=active_run_store,
        quota_service=quota_service,
        answer_artifact_store=agent_control_store,
        dependency_authorizer=ExecutionDependencyAuthorizer(
            authorization=active_permissions,
            knowledge_service=knowledge_service,
            skill_service=skill_service,
            user_lookup=run_user_lookup,
        ),
    )
    from inqtrix.services.agent_source_service import AgentSourceService

    agent_source_service = AgentSourceService(
        sessions=agent_sessions_service, run_store=active_run_store,
        control=agent_control_store, knowledge=knowledge_service, skills=skill_service,
        editor=editor_persistence_service, prompt_templates=prompt_template_service,
    )
    capability_registry_instance = build_capability_registry(
        context_source_service=agent_source_service,
        knowledge_service=knowledge_service,
        editor_service=editor_persistence_service,
        # Web instant uses the default stack's search provider. The
        # registered capabilities are read-only discovery tools.
        search_provider=getattr(providers, "search", None),
        editor_patch_service=editor_patch_service,
    )
    # The workspace agent registers only with a real checkpointer
    # (Postgres) or the explicit volatile escape. A missing
    # gate keeps mode=workspace_agent a loud 400 listing available modes,
    # and /v1/capabilities reports features.workspace_agent=false.
    asset_deletion_service.bind_session_deletion(
        agent_sessions=agent_sessions_service,
        knowledge_sessions=knowledge_sessions_service,
        agent_checkpointer=agent_checkpointer,
    )
    _bind_user_erasure(
        settings,
        asset_deletion_service=asset_deletion_service,
        deletion_store=active_deletion_store,
        run_store=active_run_store,
        engine=database_engine,
    )
    if agent_checkpointer is not None and "workspace_agent" not in (
        active_registry.ids()
    ):
        from inqtrix.agents.algorithm import WorkspaceAgentAlgorithm

        active_registry.register(
            WorkspaceAgentAlgorithm(
                control_store=agent_control_service.store,
                run_service=run_service,
                resolver=resolver,
                capability_registry=capability_registry_instance,
                checkpointer=agent_checkpointer,
                platform=settings.agent_platform,
                permission_service=active_permissions,
                knowledge_service=knowledge_service,
                editor_patch_service=editor_patch_service,
                editor_persistence_service=editor_persistence_service,
                agent_memory_service=agent_memory_service,
                skill_service=skill_service,
            )
        )
    # Cognitive-kernel registration: on by default, switched off with
    # INQTRIX_AGENT_KERNEL_ENABLED=false, and additionally gated on the
    # same checkpointer rule plus native tool calling on the LLM — a
    # failed gate WARNS and NAMES itself instead of silently dropping the
    # mode, because "the kernel is off" and "the kernel could not start"
    # are different answers to an operator.
    kernel_gate_reason = ""
    if settings.agent_platform.kernel_enabled and "agent_kernel" not in (
        active_registry.ids()
    ):
        from inqtrix.agents.harness import runtime_available

        from inqtrix.providers.base import reported_capability

        # A provider may leave the tool-calling surface unimplemented —
        # the base class promises it keeps working prompt-only. Reading
        # the predicate through the shared helper keeps that promise
        # here too: an undeclared capability is a missed gate with the
        # WARNING below, never an AttributeError that takes the whole
        # container build, and with it every route, down.
        llm_tool_calls = reported_capability(
            providers.llm, "supports_tool_calls"
        )
        harness_ready = runtime_available()
        kernel_gate_reason = (
            "checkpointer_required"
            if agent_checkpointer is None
            else "tool_calls_required"
            if not llm_tool_calls
            else "harness_required"
            if not harness_ready
            else ""
        )
        if kernel_gate_reason:
            log.warning(
                "Agent kernel is enabled but cannot be registered (checkpointer=%s, "
                "tool_calls=%s, agent_runtime=%s); mode=agent_kernel remains disabled.",
                "ok" if agent_checkpointer is not None else "missing",
                "ok" if llm_tool_calls else "missing",
                "ok" if harness_ready else "missing",
            )
        else:
            from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm

            active_registry.register(
                KernelAgentAlgorithm(
                    control_store=agent_control_service.store,
                    checkpointer=agent_checkpointer,
                    platform=settings.agent_platform,
                    capability_registry=capability_registry_instance,
                    permission_service=active_permissions,
                    run_service=run_service,
                    resolver=resolver,
                    skill_service=skill_service,
                    agent_memory_service=agent_memory_service,
                    source_service=agent_source_service,
                )
            )
    from inqtrix.auth.principal_generation import bind_principal_generation

    principal_dependency = bind_principal_generation(
        auth_provider.build_principal_dependency()
    )

    return AppContainer(
        settings=settings,
        providers=providers,
        strategies=strategies,
        registry=active_registry,
        runtime=runtime,
        auth_provider=auth_provider,
        principal_dependency=principal_dependency,
        permission_service=active_permissions,
        user_context_dependency=build_user_context_dependency(
            active_permissions, principal_dependency
        ),
        resolver=resolver,
        chat_service=ChatService(registry=active_registry, runtime=runtime),
        run_service=run_service,
        health_service=HealthService(
            providers=providers,
            settings=settings,
            auth_provider=auth_provider,
            stacks=stacks,
            default_stack=default_stack,
        ),
        run_store=active_run_store,
        semaphore_factory=semaphore_factory,
        agent_checkpointer=agent_checkpointer,
        kernel_gate_reason=kernel_gate_reason,
        # Twice the admission ceiling: admission bounds how many AI calls
        # may start, not how many are still running. A disconnected stream
        # releases its slot while its thread finishes, and without headroom
        # those orphans would displace freshly admitted work.
        execution_lanes=ExecutionLanes(
            ai_workers=settings.server.max_concurrent * 2,
            stream_workers=settings.server.stream_reader_workers,
        ),
        knowledge_service=knowledge_service,
        file_service=active_file_service,
        knowledge_ceiling=knowledge_ceiling,
        workspace_admin=active_workspace_admin,
        share_service=share_service,
        prompt_template_service=prompt_template_service,
        skill_service=skill_service,
        quota_service=quota_service,
        indexing_service=indexing_service,
        chat_history_service=chat_history_service,
        editor_persistence_service=editor_persistence_service,
        editor_patch_service=editor_patch_service,
        editor_collaboration_service=editor_collaboration_service,
        editor_guest_link_service=editor_guest_link_service,
        asset_records_service=asset_records_service,
        knowledge_sessions_service=knowledge_sessions_service,
        agent_control_service=agent_control_service,
        agent_sessions_service=agent_sessions_service,
        agent_source_service=agent_source_service,
        document_parser=document_parser,
        vector_index_service=vector_index_service,
        asset_deletion_service=asset_deletion_service,
        upload_operation_service=upload_operation_service,
        upload_reconciler=upload_reconciler,
        indexing_auto_resumer=indexing_auto_resumer,
        run_retention_sweep=run_retention_sweep,
        account_preferences_service=account_preferences_service,
        user_event_store=(bundle.user_event_store if bundle is not None else None),
        session_factory=(bundle.session_factory if bundle is not None else None),
        database_engine=database_engine,
        agent_memory_service=agent_memory_service,
        capability_registry=capability_registry_instance,
        run_user_lookup=run_user_lookup,
        object_store_backend=active_object_store_backend,
        stacks=stacks,
        default_stack=default_stack,
    )


def _bind_user_erasure(
    settings: Any,
    *,
    asset_deletion_service: Any,
    deletion_store: Any,
    run_store: Any,
    engine: "AsyncEngine | None",
) -> None:
    """Wire account erasure onto the deletion machine (Postgres only).

    The erasure runs as one deletion operation per account: the deletion
    worker executes it, and it deletes the account's aggregates through
    the same machine and the run store before its rows go in batches.
    """
    from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore

    if settings.storage.backend != "postgres" or not isinstance(
        deletion_store, PostgresDeletionOperationStore
    ):
        return
    from inqtrix.services.user_erasure_aggregates import DeletionMachineAggregates
    from inqtrix.services.user_erasure_executor import UserErasure
    from inqtrix.storage.db import build_session_factory
    from inqtrix.storage.user_lifecycle_postgres import (
        PostgresUserLifecycleTransaction,
    )

    session_factory = build_session_factory(engine)
    app_role = settings.storage.app_role
    asset_deletion_service.bind_user_erasure(
        UserErasure(
            lifecycle=PostgresUserLifecycleTransaction(
                session_factory=session_factory, app_role=app_role
            ),
            session_factory=session_factory,
            app_role=app_role,
            aggregates=DeletionMachineAggregates(
                service=asset_deletion_service,
                store=deletion_store,
                run_store=run_store,
                session_factory=session_factory,
                app_role=app_role,
            ),
        )
    )
