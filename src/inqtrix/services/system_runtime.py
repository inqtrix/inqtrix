"""Sanitized runtime manifest for the instance-admin system view."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")

_RUNTIME_PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class RuntimeProbeResults:
    """Read-only backend availability checks for runtime/capability views."""

    object_store_available: bool | None = None
    queue_available: bool | None = None
    vector_store_available: bool | None = None
    queue_consumers: int | None = None
    queue_depth: int | None = None


def _checkpointer_pool_size(container: Any) -> int | None:
    """The agent checkpointer's effective pool ceiling, or None.

    Reads the handle (like the startup budget line), never the raw
    setting: a disabled agent, a missing extra, or the volatile escape
    has no psycopg pool, and publishing the configured knob there would
    name a pool that cannot exist.
    """
    handle = getattr(container, "agent_checkpointer", None)
    if handle is None or not getattr(handle, "durable", False):
        return None
    return int(handle.max_connections)


async def system_runtime_payload_checked(
    container: "AppContainer",
) -> dict[str, Any]:
    """Return the runtime payload with current backend availability probes."""
    probes = await runtime_probe_results(container)
    return system_runtime_payload(container, probes=probes)


def system_runtime_payload(
    container: "AppContainer",
    *,
    probes: RuntimeProbeResults | None = None,
) -> dict[str, Any]:
    """Return the deployment runtime shape without secrets or endpoints.

    Args:
        container: The already-wired application container. The payload is
            derived from the same settings and collaborators used by the
            routers and algorithms, so the admin UI describes what is
            configured to execute rather than re-inferring it on the client.
        probes: Optional availability booleans from read-only runtime
            checks. When omitted, the payload keeps its historical
            configuration-only semantics for pure unit call sites.

    Returns:
        A JSON-serializable manifest of backend categories only. It
        deliberately omits database URLs, object-store paths, bucket names,
        service endpoints, and credentials. Queue values describe the
        configured dispatch mode plus, when probed, the consumer-group
        attachment count and stream depth (the honest someone-is-attached
        signal); they still do not claim per-worker heartbeats.
    """
    settings = container.settings
    knowledge = _knowledge_payload(container, probes=probes)
    queue_backend = settings.queue.backend
    object_store = (
        container.object_store_backend
        if container.file_service is not None
        else "none"
    )
    object_store_available = (
        probes.object_store_available
        if probes is not None and probes.object_store_available is not None
        else container.file_service is not None
    )
    queue_available = (
        probes.queue_available
        if probes is not None and probes.queue_available is not None
        else True
    )

    return {
        "api": {
            "openapi": settings.server.enable_openapi,
            # Effective values, straight from the settings this process
            # runs with. Deliberately HERE and not in /v1/capabilities:
            # that endpoint is unauthenticated by design, and capacity
            # ceilings have no browser consumer -- unlike the timeouts
            # block, which the client needs for its own AbortController.
            # getattr-defensive like _observability_payload below, and for
            # the same documented reason: the capabilities route derives
            # its feature flags from THIS payload, and capability tests
            # pass settings doubles without these fields. None here never
            # reaches production -- real Settings always carry the fields.
            "chat_max_concurrent": getattr(
                settings.server, "max_concurrent", None
            ),
            "stream_reader_workers": getattr(
                settings.server, "stream_reader_workers", None
            ),
        },
        "files": {
            "blob_storage": _blob_storage_label(object_store),
            # The parser that prepares uploads, whether or not knowledge is
            # enabled; knowledge ingestion reads the same preparation.
            "document_parser": (
                settings.storage.document_parser
                if container.file_service is not None
                and container.file_service.text_extraction_available
                else "none"
            ),
            "enabled": container.file_service is not None,
            "max_file_bytes": (
                settings.storage.max_file_bytes
                if container.file_service is not None
                else None
            ),
            "object_store": object_store,
            "object_store_available": object_store_available,
        },
        "knowledge": knowledge,
        "runs": {
            "execution": (
                "worker_dispatch" if queue_backend == "valkey" else "in_process"
            ),
            "queue": queue_backend,
            "queue_available": queue_available,
            "queue_consumers": (
                probes.queue_consumers if probes is not None else None
            ),
            "queue_depth": (
                probes.queue_depth if probes is not None else None
            ),
            "store": (
                "postgres" if settings.storage.backend == "postgres" else "memory"
            ),
            "worker_dispatch": queue_backend == "valkey",
            # Admission limits of THIS api process. The "execution" field
            # above says which value governs how runs actually execute:
            # "in_process" means these are also the execution width, while
            # "worker_dispatch" means execution belongs to the worker
            # fleet, whose total capacity this process does not know and
            # therefore does not claim.
            "admission_max_concurrent": (
                getattr(settings.server, "run_max_concurrent", None)
                or getattr(settings.server, "max_concurrent", None)
            ),
            "queue_max_size": getattr(
                settings.server, "run_queue_max_size", None
            ),
        },
        "agents": {
            # EFFECTIVE, not configured: the value the psycopg pool was
            # actually built with, read from the HANDLE exactly like the
            # startup budget line -- one source, three displays
            # (INQTRIX_AGENT_CHECKPOINTER_POOL_SIZE). None when no
            # durable checkpointer exists (agent disabled, extra
            # missing, volatile mode): the panel row hides instead of
            # naming a pool that can never open. getattr-defensive for
            # capability-test doubles, degrading visibly to None.
            "checkpointer_pool_size": _checkpointer_pool_size(container),
        },
        "storage": {
            "backend": settings.storage.backend,
            "durable": settings.storage.backend == "postgres",
        },
        "observability": _observability_payload(settings),
    }


def _observability_payload(settings: Any) -> dict[str, Any]:
    """Tracing status: configured mode vs. effectively installed.

    ``tracing_active`` is the truth the admin needs when traces are
    missing: a non-off mode with a missing ``observability`` extra
    degrades loudly to no tracer — configured and effective then
    differ. No endpoints or credentials are exposed here. getattr-
    defensive throughout: capability/test containers pass settings
    doubles without the observability group, and telemetry status must
    never break the manifest they actually asked for.
    """
    from inqtrix.observability.otel import tracing_installed

    observability = getattr(settings, "observability", None)
    mode = str(getattr(observability, "tracing", "off") or "off")
    try:
        from inqtrix.observability.content import build_content_policy

        content_capture = build_content_policy(settings).configured_capture
    except Exception:  # noqa: BLE001 — partial settings double
        # Fail-safe, never fail-SILENT: in production this firing means
        # a real settings defect the operator must see.
        log.warning(
            "Content capture status unavailable because settings lack the complete "
            "observability group; reporting it as disabled.",
            exc_info=True,
        )
        content_capture = False
    return {
        "tracing": mode,
        "tracing_active": tracing_installed() if mode != "off" else False,
        "content_capture": content_capture,
        "sample_rate": float(
            getattr(observability, "trace_sample_rate", 1.0) or 0.0
        ),
        "spool": mode == "file",
        # None whenever NO cleanup job runs — including the documented
        # retention_days=0 opt-out. Rendering "0 days (cleanup job)"
        # would assert the opposite of reality (traces kept forever).
        # retention_enforced additionally tells the panel whether ANY
        # process actually runs the prune job: all three retention jobs
        # (trace/audit/ledger) live in the worker, so a worker-less
        # deployment keeps rows forever no matter what the days say.
        "retention_enforced": bool(
            getattr(getattr(settings, "queue", None), "backend", "")
            == "valkey"
        ),
        "retention_days": (
            int(getattr(observability, "trace_retention_days", 0) or 0)
            if mode == "otlp"
            and int(getattr(observability, "trace_retention_days", 0) or 0)
            > 0
            else None
        ),
        "ui_link_configured": bool(
            str(getattr(observability, "trace_ui_url", "") or "").strip()
        ),
    }


KNOWLEDGE_REASON_OPERATOR_DISABLED = "operator_disabled"
KNOWLEDGE_REASON_STORE_UNREACHABLE = "store_unreachable"


async def knowledge_availability(
    container: "AppContainer",
    *,
    vector_store_available: bool | None = None,
) -> tuple[bool, str | None]:
    """``(available, reason_code)`` of project knowledge, ONE derivation for
    the capability manifest and the run admission.

    Not configured (no knowledge service) is ``operator_disabled``;
    configured but the vector store does not answer its bounded probe is
    ``store_unreachable``. A caller that already holds a fresh probe result
    (the manifest probes every backend) passes it in; the admission of one
    knowledge request runs only the vector-store probe instead of the whole
    runtime fan-out.
    """
    if getattr(container, "knowledge_service", None) is None:
        return False, KNOWLEDGE_REASON_OPERATOR_DISABLED
    healthy = (
        vector_store_available
        if vector_store_available is not None
        else await _probe_vector_store(container)
    )
    if healthy:
        return True, None
    return False, KNOWLEDGE_REASON_STORE_UNREACHABLE


async def runtime_probe_results(container: "AppContainer") -> RuntimeProbeResults:
    """Probe optional backing services without exposing connection details."""
    object_store, queue_result, vector_store = await asyncio.gather(
        _probe_object_store(container),
        _probe_queue(container),
        _probe_vector_store(container),
    )
    queue_available, queue_info = queue_result
    return RuntimeProbeResults(
        object_store_available=object_store,
        queue_available=queue_available,
        vector_store_available=vector_store,
        queue_consumers=(
            queue_info.get("consumers") if queue_info is not None else None
        ),
        queue_depth=(
            queue_info.get("depth") if queue_info is not None else None
        ),
    )


def runtime_feature_overrides(
    runtime: dict[str, Any],
) -> dict[str, bool]:
    """Derive effective feature booleans from a runtime payload."""
    files = runtime.get("files") or {}
    knowledge = runtime.get("knowledge") or {}
    files_on = bool(files.get("enabled")) and bool(
        files.get("object_store_available", True)
    )
    knowledge_enabled = bool(knowledge.get("enabled"))
    knowledge_on = knowledge_enabled and bool(
        knowledge.get("vector_store_available", True)
    )
    return {
        # Document parsing is pure CPU work (MarkItDown) that belongs to the
        # files: it needs neither knowledge nor its vector store. Gating it on
        # either would turn uploads into the weaker client parse whenever
        # knowledge is off or its store briefly down (No-Silent-Fallbacks).
        "document_parser": files.get("document_parser") not in (None, "none"),
        "embedding_provider": knowledge_on
        and knowledge.get("embedding_provider") not in (None, "none"),
        "files": files_on,
        "hybrid_retrieval": knowledge_on
        and bool(knowledge.get("hybrid_retrieval")),
        "knowledge": knowledge_on,
        "reranker": knowledge_on and knowledge.get("reranker") not in (None, "none"),
    }


def _knowledge_payload(
    container: "AppContainer",
    *,
    probes: RuntimeProbeResults | None = None,
) -> dict[str, Any]:
    settings = container.settings
    service = container.knowledge_service
    if service is None:
        return {
            "contextual_retrieval": False,
            "cross_lingual_recommendation": "reranker",
            "default_top_k": None,
            "embedding_model": None,
            "embedding_provider": None,
            "enabled": False,
            "hybrid_retrieval": False,
            "reranker": "none",
            "sparse": None,
            "sparse_mode": "off",
            "sparse_language": None,
            "sparse_multilingual": False,
            "vector_store": "none",
            "vector_store_available": False,
        }

    context = service.knowledge
    vector_store_available = (
        probes.vector_store_available
        if probes is not None and probes.vector_store_available is not None
        else True
    )
    # The store's lexical-branch language is the single truth for both the
    # normalized mode (bm25/off) and the language code — derived once, never
    # re-asserted independently.
    sparse_language = getattr(context.store, "sparse_language", None)
    return {
        "contextual_retrieval": context.contextualizer is not None,
        # Keyword (BM25) retrieval is monolingual and never cross-lingual; the
        # cross-lingual lever is a multilingual cross-encoder reranker. Static
        # facts so clients can surface the limitation honestly (the per-run
        # query-vs-document mismatch would need a collection language).
        "cross_lingual_recommendation": "reranker",
        "default_top_k": context.default_top_k,
        "embedding_model": context.embeddings.default_model,
        "embedding_provider": settings.knowledge.embedding_provider,
        "enabled": True,
        "hybrid_retrieval": bool(getattr(context.store, "supports_hybrid", False))
        and vector_store_available,
        "reranker": (
            settings.knowledge.reranker_provider
            if context.reranker is not None
            else "none"
        ),
        "sparse": (
            settings.knowledge.sparse
            if settings.knowledge.vector_backend == "qdrant"
            else None
        ),
        "sparse_mode": "bm25" if sparse_language is not None else "off",
        "sparse_language": sparse_language,
        "sparse_multilingual": False,
        "vector_store": settings.knowledge.vector_backend,
        "vector_store_available": vector_store_available,
    }


def _blob_storage_label(object_store: str) -> str:
    if object_store == "local":
        return "volume"
    if object_store == "s3":
        return "s3"
    return object_store


async def _probe_object_store(container: "AppContainer") -> bool:
    service = getattr(container, "file_service", None)
    if service is None:
        return False

    async def probe() -> bool:
        checker = getattr(service, "object_store_available", None)
        if checker is None:
            return True
        return bool(await checker())

    return await _bounded_probe("object_store", probe)


async def _probe_queue(
    container: "AppContainer",
    *,
    include_info: bool = True,
) -> tuple[bool, dict[str, int] | None]:
    settings = container.settings
    if settings.queue.backend != "valkey":
        return True, None
    info: dict[str, int] | None = None

    def _probe() -> bool:
        nonlocal info
        if not _ping_valkey(settings.queue.valkey_url):
            return False
        if not include_info:
            # Readiness needs only the availability bit — skip the
            # group snapshot and its extra connection entirely.
            return True
        from inqtrix.runs.valkey_queue import ValkeyRunQueue

        queue = ValkeyRunQueue(url=settings.queue.valkey_url)
        try:
            info = queue.group_info()
        except Exception:  # noqa: BLE001 — liveness detail degrades to None
            info = None
        finally:
            # Probe clients are throwaway: close like _ping_valkey does,
            # or every runtime read leaks one broker connection.
            try:
                queue._client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        return True

    available = await _bounded_probe(
        "valkey_queue",
        lambda: asyncio.to_thread(_probe),
    )
    return available, info


async def _probe_vector_store(container: "AppContainer") -> bool:
    service = container.knowledge_service
    if service is None:
        return False
    store = service.knowledge.store
    checker = getattr(store, "is_available", None)
    if checker is None:
        return True

    async def probe() -> bool:
        result = checker()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    return await _bounded_probe("vector_store", probe)


async def readiness_payload(
    container: "AppContainer",
) -> tuple[int, dict[str, Any]]:
    """Build the ``/readyz`` payload and its HTTP status code.

    Readiness differs from ``/health`` (liveness, provider-only): a pod
    whose DATABASE or QUEUE is unreachable cannot serve requests and
    must leave the load-balancer rotation (503). A down VECTOR or object
    store degrades only its feature family, so the pod reports
    ``degraded`` but stays ready (200); capability discovery then disables
    the affected routes instead of turning a transient S3 outage into a
    whole-instance outage. Every probe is read-only and bounded
    (:data:`_RUNTIME_PROBE_TIMEOUT_SECONDS`), well below usual kubelet probe
    timeouts; memory backends stay zero-infrastructure.
    """
    (
        database_state,
        queue_result,
        vector_ok,
        object_store_ok,
    ) = await asyncio.gather(
        _probe_database_state(container),
        _probe_queue(container, include_info=False),
        _probe_vector_store_ready(container),
        _probe_object_store_ready(container),
    )
    database_ok = database_state in ("ok", "skipped")
    # _probe_queue returns (available, info); readiness consumes ONLY the
    # bool — treating the tuple itself as truth would report a dead
    # queue as ready.
    queue_ok, _queue_info = queue_result
    ready = database_ok and queue_ok
    optional_backends_ok = vector_ok and object_store_ok
    status = "ready" if ready and optional_backends_ok else (
        "degraded" if ready else "not_ready"
    )
    checks = {
        "database": _check_label(
            database_ok, skipped=container.settings.storage.backend != "postgres"
        ),
        "queue": _check_label(
            queue_ok, skipped=container.settings.queue.backend != "valkey"
        ),
        "vector_store": _check_label(
            vector_ok, skipped=container.knowledge_service is None
        ),
        "object_store": _check_label(
            object_store_ok,
            skipped=getattr(container, "file_service", None) is None,
        ),
    }
    return (200 if ready else 503), {
        "status": status,
        "checks": checks,
        # Additive detail for the product gate: ``checks.database`` keeps
        # its coarse ok/unavailable/skipped labels for orchestrators,
        # while this field says WHETHER an unavailable reading was a
        # confirmed contract violation or mere unreachability.
        "database_contract": database_state,
    }


def _check_label(ok: bool, *, skipped: bool) -> str:
    if skipped:
        return "skipped"
    return "ok" if ok else "unavailable"


async def _probe_database_state(container: "AppContainer") -> str:
    """Three-way database verdict: ``ok`` | ``unavailable`` | ``violation``.

    The distinction is load-bearing for the product gate: a probe that
    cannot REACH the database within its bound proves nothing about the
    schema/role contract, while a probe that connected and found a wrong
    contract is a confirmed break. Collapsing both into one bool once
    turned a single slow 2s probe under a load spike into a
    full-healthcheck-interval outage for every product route. Taxonomy
    matches the worker claim guard: unavailability pauses, only a
    confirmed (or unclassifiable) failure latches.
    """
    settings = container.settings
    if settings.storage.backend != "postgres":
        return "skipped"
    session_factory = container.session_factory
    if session_factory is None:
        # postgres declared but no factory wired — a composition bug,
        # permanent for this process, never a transient outage.
        log.warning(
            "Readiness: postgres storage backend has no session factory; the database "
            "probe reports a contract violation."
        )
        return "violation"
    from inqtrix.storage.runtime_contract import (
        DatabaseRuntimeContractError,
        DatabaseRuntimeUnavailableError,
        _is_database_runtime_unavailable,
        verify_database_runtime_contract,
    )

    try:
        await asyncio.wait_for(
            verify_database_runtime_contract(
                session_factory,
                app_role=settings.storage.app_role,
                login_policy=settings.storage.runtime_login_policy,
            ),
            timeout=_RUNTIME_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        log.warning(
            "Runtime availability probe failed for database: timed out "
            "after %.1fs; the probed backend logs its own detailed probe "
            "warning when it fails before this bound.",
            _RUNTIME_PROBE_TIMEOUT_SECONDS,
        )
        return "unavailable"
    except DatabaseRuntimeUnavailableError as exc:
        log.warning(
            "Runtime availability probe failed for database "
            "(transient, error_type=%s)",
            type(exc.__cause__ or exc).__name__,
        )
        return "unavailable"
    except DatabaseRuntimeContractError as exc:
        # error_type only: this module's log calls must never render
        # exception text (it can carry DSNs); the contract checker's own
        # output names the violated invariant.
        log.error(
            "Database runtime contract violated (error_type=%s).",
            type(exc).__name__,
        )
        return "violation"
    except Exception as exc:  # noqa: BLE001 - classified below, never silent.
        if _is_database_runtime_unavailable(exc):
            log.warning(
                "Runtime availability probe failed for database "
                "(transient, error_type=%s)",
                type(exc).__name__,
            )
            return "unavailable"
        # Unclassifiable failures latch like the worker guard: integrity
        # over availability when the probe itself cannot say why it died.
        log.error(
            "Database probe returned an unclassified error (error_type=%s); treating it"
            " as a contract violation.",
            type(exc).__name__,
        )
        return "violation"
    return "ok"


async def _probe_database(container: "AppContainer") -> bool:
    return await _probe_database_state(container) in {"ok", "skipped"}


async def database_runtime_contract_ready(container: "AppContainer") -> bool:
    """Return the hard database-contract state used by HTTP startup gates."""
    return await _probe_database(container)


async def _probe_vector_store_ready(container: "AppContainer") -> bool:
    # Readiness variant: NO knowledge service means the feature is off —
    # trivially ready (the admin runtime view reports False there
    # because it describes feature availability, not pod readiness).
    if container.knowledge_service is None:
        return True
    return await _probe_vector_store(container)


async def _probe_object_store_ready(container: "AppContainer") -> bool:
    """Treat a disabled file service as an intentionally skipped dependency."""
    if getattr(container, "file_service", None) is None:
        return True
    return await _probe_object_store(container)


async def _bounded_probe(
    name: str,
    probe: Callable[[], Awaitable[bool]],
) -> bool:
    try:
        return bool(
            await asyncio.wait_for(
                probe(),
                timeout=_RUNTIME_PROBE_TIMEOUT_SECONDS,
            )
        )
    except TimeoutError:
        # ``str(TimeoutError())`` is empty on Python >= 3.11, so name the
        # bound explicitly; the probed backend logs its own detailed cause
        # when it fails BEFORE this bound (e.g. the rate-limited
        # "S3 availability probe failed for bucket ..." warning).
        log.warning(
            "Runtime availability probe failed for %s: timed out after "
            "%.1fs; the probed backend logs its own detailed probe warning "
            "when it fails before this bound.",
            name,
            _RUNTIME_PROBE_TIMEOUT_SECONDS,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - status payload degrades visibly.
        log.warning(
            "Runtime availability probe failed for %s (error_type=%s)",
            name,
            type(exc).__name__,
        )
        return False


def _ping_valkey(url: str) -> bool:
    import valkey

    client = valkey.Valkey.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    try:
        return bool(client.ping())
    finally:
        client.close()
