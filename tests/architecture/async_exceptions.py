"""Known async violations per module and rule: a ratchet that only shrinks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AsyncException:
    """How often a module breaks a rule, and why; the scanner names where."""

    module: str
    rule: str
    count: int
    reason: str


ASYNC_EXCEPTIONS: tuple[AsyncException, ...] = (
    AsyncException(
        "inqtrix.agent",
        "legacy_bridge",
        3,
        "the library facade drives the sync research graph until the graph runs async",
    ),
    AsyncException(
        "inqtrix.agents.algorithm",
        "legacy_bridge",
        56,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.agents.discovery",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.agents.kernel.compaction",
        "thread",
        1,
        "the async hook writes the archive on the short-work lane, whose threads may not wait for the loop",
    ),
    AsyncException(
        "inqtrix.agents.kernel.deps",
        "legacy_bridge",
        2,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.agents.kernel.middleware",
        "hook_pair",
        4,
        "the middleware has only sync hooks until the kernel runs async",
    ),
    AsyncException(
        "inqtrix.agents.kernel.model_recovery",
        "thread",
        2,
        "the async hook writes the archive on the short-work lane, whose threads may not wait for the loop",
    ),
    AsyncException(
        "inqtrix.agents.scheduler",
        "thread",
        2,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.capabilities.registry",
        "legacy_bridge",
        2,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.knowledge.algorithm",
        "legacy_bridge",
        2,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.knowledge.contextualize",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.knowledge.embedding_batches",
        "legacy_bridge",
        2,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.knowledge.stores.qdrant_store",
        "thread",
        40,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.nodes",
        "thread",
        2,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.providers.attempts",
        "legacy_bridge",
        1,
        "the sync face of an async provider call waits for the loop until its callers are async",
    ),
    AsyncException(
        "inqtrix.runs.durable_store",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.runs.durable_store",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.runs.shared",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.server.execution",
        "thread",
        3,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.metrics",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.server.routers.admin_trace",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.routers.editor",
        "thread",
        2,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.routers.indexing",
        "legacy_bridge",
        3,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.server.routers.indexing",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.routers.knowledge",
        "thread",
        3,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.routers.runs",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.server.routers.runs",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.routers.session_titles",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.routers.test",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.routers.text",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.server.streaming",
        "cancel",
        1,
        "a handler catches cancellation without raising it again",
    ),
    AsyncException(
        "inqtrix.server.streaming",
        "thread",
        2,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.services.agent_answer_publisher",
        "legacy_bridge",
        3,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.agent_control_service",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.agent_source_service",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.asset_deletion_service",
        "legacy_bridge",
        41,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.chat_service",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.services.execution_dependency_authority",
        "legacy_bridge",
        2,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.file_service",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.file_service",
        "thread",
        11,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.services.indexing_service",
        "legacy_bridge",
        8,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.indexing_service",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.services.knowledge_service",
        "legacy_bridge",
        9,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.quota_service",
        "legacy_bridge",
        4,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.system_runtime",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.services.upload_operation_service",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.user_erasure_aggregates",
        "legacy_bridge",
        2,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.services.user_erasure_executor",
        "legacy_bridge",
        4,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.storage.db",
        "cancel",
        1,
        "a handler catches cancellation without raising it again",
    ),
    AsyncException(
        "inqtrix.storage.object_orphan_sweep",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.user_events",
        "thread",
        1,
        "blocking work still runs on a thread instead of the one thread bridge",
    ),
    AsyncException(
        "inqtrix.worker.deletion_loop",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.worker.indexing_loop",
        "legacy_bridge",
        3,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.worker.loop",
        "legacy_bridge",
        2,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
    AsyncException(
        "inqtrix.worker.upload_loop",
        "legacy_bridge",
        1,
        "synchronous code still crosses the thread bridge until its engine is async",
    ),
)
