"""Submission and execution for durable knowledge-indexing operations.

The service submits both isolated collection-generation rebuilds and immutable
document-revision deltas to the same job store. Their worker callables share
the canonical knowledge preparation pipeline, check cancellation between
provider batches, emit durable progress, and book embedding-token usage only
for completed work. Read-side operations stay on the
:class:`~inqtrix.server.indexing.IndexingJobStore` and are called by the
router directly.

A reindex builds a complete shadow generation.  The active generation stays
readable while document-level checkpoints accumulate; concurrent source
changes are folded in as deltas, and one validated pointer switch publishes
the result.  Cancel or dependency pause never exposes a partial generation.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import logging
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any, Callable

from inqtrix.indexing_failures import (
    IndexingDependencyError,
    dependency_error_from_exception,
)
from inqtrix.knowledge.contextualize import (
    ContextualizationBatchCheckpoint,
    ContextualizationDependencyError,
    ContextualizationValidationError,
)
from inqtrix.knowledge.stores.ports import (
    DocumentNotFound,
    DocumentRevisionSuperseded,
    GenerationManifestChanged,
    GenerationValidationError,
    IndexGenerationSuperseded,
    KnowledgeCollection,
)
from inqtrix.quota.models import QuotaAdjustmentConflict, QuotaDimension
from inqtrix.sync_bridge import run_coro_sync

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal
    from inqtrix.indexing_auto_resume import SweepReport
    from inqtrix.source_authority import SourceScope
    from inqtrix.quota.models import QuotaSubject
    from inqtrix.server.indexing import IndexingJobHandle, IndexingJobStore
    from inqtrix.services.execution_dependency_authority import CollectionEditAuthorizer
    from inqtrix.services.knowledge_service import KnowledgeService
    from inqtrix.services.quota_service import QuotaService

log = logging.getLogger("inqtrix")

# Collection rebuilds use the same document concurrency as first ingestion.
# This keeps the visible "three active, remaining queued" contract consistent
# without creating another queue or a second indexing implementation.
_COLLECTION_DOCUMENT_CONCURRENCY = 3


class ReindexUnsupported(RuntimeError):
    """Raised when the active store cannot stage and publish a generation.

    A store without
    :meth:`~inqtrix.knowledge.stores.ports.KnowledgeStore.reembed_document`
    cannot back a shadow rebuild; the router maps this to HTTP 501
    so the missing capability is visible, never a silent no-op
    (capability-gated degradation, no silent fallback).
    """


class _IndexingCancellation(RuntimeError):
    """Internal control signal observed between provider batches."""


def _run_dependency_boundary(awaitable, *, vector_surface: bool = False):
    """Run one provider/store awaitable and preserve only proven outages."""

    try:
        return run_coro_sync(awaitable)
    except Exception as exc:
        dependency = dependency_error_from_exception(
            exc,
            vector_surface=vector_surface,
        )
        if dependency is not None:
            raise dependency from exc
        raise


def generation_embedding_adjustment_id(
    *,
    generation_id: str,
    document_id: str,
    revision_id: str | None,
    document_text: str,
    build_contract_hash: str,
) -> str:
    """Return the immutable idempotency key for one generation document.

    Legacy documents without a revision id use their canonical content hash.
    The final digest keeps the quota primary key bounded while still binding
    generation, document, revision/content, and the complete build contract.
    """

    revision_identity = revision_id or (
        "content:" + hashlib.sha256(document_text.encode("utf-8")).hexdigest()
    )
    identity = "\x1f".join(
        (
            generation_id,
            document_id,
            revision_identity,
            build_contract_hash,
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"knowledge-generation:{digest}:embedding-tokens"


def _embedding_receipt_checkpoint(
    *,
    adjustment_id: str,
    amount: int,
    input_count: int,
    input_sha256: str,
    revision_id: str,
    build_contract_hash: str,
) -> dict[str, Any]:
    """Private durable job facts proving the exact metered embed input."""

    return {
        "adjustment_id": adjustment_id,
        "amount": int(amount),
        "input_count": int(input_count),
        "input_sha256": input_sha256,
        "revision_id": revision_id,
        "build_contract_hash": build_contract_hash,
    }


def execute_document_revision_job(
    handle: "IndexingJobHandle",
    *,
    knowledge_service: "KnowledgeService",
    document_id: str,
    revision_id: str,
    quota_service: "QuotaService | None" = None,
    quota_subject: "QuotaSubject | None" = None,
    authority_check: Callable[[], None] | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Build one immutable revision and publish it through its CAS fence.

    Owns the ``indexing_documents`` metric for the primary ingest path
    in BOTH deployments (in-process job store and durable worker):
    exactly one ``completed`` OR ``failed`` sample per genuinely
    finished document — pauses, cancellations, and supersedes stay
    uncounted, and a post-completion terminal error never adds a
    second ``failed`` sample.
    """
    counted = {"completed": False}
    try:
        _run_document_revision_steps(
            handle,
            knowledge_service=knowledge_service,
            document_id=document_id,
            revision_id=revision_id,
            quota_service=quota_service,
            quota_subject=quota_subject,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
            counted=counted,
        )
    except (
        ContextualizationDependencyError,
        IndexingDependencyError,
        ContextualizationValidationError,
        GenerationValidationError,
    ):
        raise  # resumable pauses, not document failures
    except Exception:
        if not counted["completed"]:
            _count_indexed_document("failed")
        raise


def _run_document_revision_steps(
    handle: "IndexingJobHandle",
    *,
    knowledge_service: "KnowledgeService",
    document_id: str,
    revision_id: str,
    quota_service: "QuotaService | None" = None,
    quota_subject: "QuotaSubject | None" = None,
    authority_check: Callable[[], None] | None = None,
    actor_user_id: uuid.UUID | None = None,
    counted: dict[str, bool],
) -> None:
    handle.begin(1)
    if authority_check is not None:
        authority_check()
    if handle.cancelled:
        handle.cancel("client_requested_cancel")
        return

    def _cancel_check() -> None:
        if handle.cancelled:
            raise _IndexingCancellation("server-side indexing cancellation requested")

    try:
        checkpoint_reader = getattr(handle, "context_batch_checkpoints", None)
        stored_checkpoints = (
            checkpoint_reader(document_id) if callable(checkpoint_reader) else []
        )
        context_checkpoints = [
            ContextualizationBatchCheckpoint.from_dict(item)
            for item in stored_checkpoints
        ]
        checkpoint_writer = getattr(handle, "checkpoint_context_batch", None)

        def _phase(
            name: str, *, current_batch: int = 0, total_batches: int = 0
        ) -> None:
            phase_method = getattr(handle, "phase", None)
            if callable(phase_method):
                phase_method(
                    name,
                    current_batch=current_batch,
                    total_batches=total_batches,
                )

        _phase("contextualization")
        prepared = _run_dependency_boundary(
            knowledge_service.prepare_reserved_document_revision(
                document_id=document_id,
                revision_id=revision_id,
                actor_user_id=actor_user_id,
                on_context_batch=lambda current, total: _phase(
                    "contextualization",
                    current_batch=current,
                    total_batches=total,
                ),
                on_context_checkpoint=(
                    lambda checkpoint: (
                        checkpoint_writer(document_id, checkpoint.as_dict())
                        if callable(checkpoint_writer)
                        else None
                    )
                ),
                context_checkpoints=context_checkpoints,
                cancel_check=_cancel_check,
                on_embedding_started=lambda: _phase("embedding"),
                on_embedding_batch=lambda current, total: _phase(
                    "embedding",
                    current_batch=current,
                    total_batches=total,
                ),
                on_embedding_wait=lambda current, total: _phase(
                    "embedding_wait",
                    current_batch=current,
                    total_batches=total,
                ),
                contextualize=not bool(getattr(handle, "raw_by_user_choice", False)),
                authority_check=authority_check,
            ),
            vector_surface=True,
        )
    except _IndexingCancellation:
        handle.cancel("client_requested_cancel")
        return
    except DocumentRevisionSuperseded:
        handle.supersede("newer_revision_requested")
        return
    embedding_receipt = (
        prepared.embedded.work_receipt
        if prepared.embedded is not None
        else _run_dependency_boundary(
            knowledge_service.active_document_embedding_receipt(document_id),
            vector_surface=True,
        )
    )
    adjustment_id = f"knowledge-revision:{revision_id}:embedding-tokens"
    receipt_checkpoint: dict[str, Any] | None = None
    if quota_service is not None and quota_subject is not None:
        try:
            quota_service.record_blocking_once(
                quota_subject,
                QuotaDimension.EMBEDDING_TOKENS,
                embedding_receipt.amount,
                adjustment_id=adjustment_id,
            )
        except QuotaAdjustmentConflict as exc:
            log.error(
                "Embedding quota receipt for revision %s contradicts its "
                "immutable work identity (error_type=%s)",
                revision_id,
                type(exc).__name__,
            )
            handle.pause_validation(
                ("The quota receipt conflicts with the prepared document revision. The "
                "revision was not activated; the previous search version is unchanged.")
            )
            return
        except Exception as exc:  # noqa: BLE001 - durable pause preserves retryability
            log.warning(
                "Embedding quota receipt for revision %s could not be recorded "
                "(error_type=%s)",
                revision_id,
                type(exc).__name__,
            )
            handle.pause_dependency(
                ("The quota charge could not be confirmed. The prepared document "
                "revision was not activated; the previous search version is unchanged. "
                "The operation can resume when the dependency recovers."),
                error_type="quota_dependency_error",
            )
            return
        receipt_checkpoint = _embedding_receipt_checkpoint(
            adjustment_id=adjustment_id,
            amount=embedding_receipt.amount,
            input_count=embedding_receipt.input_count,
            input_sha256=embedding_receipt.input_sha256,
            revision_id=revision_id,
            build_contract_hash=prepared.reservation.build_contract_hash,
        )

    def _publish() -> Any:
        guard_factory = getattr(handle, "document_publication_guard", None)

        def _publication_guard() -> Any:
            return guard_factory(
                document_id=document_id,
                revision_id=revision_id,
            )

        return _run_dependency_boundary(
            knowledge_service.publish_prepared_document_revision(
                prepared,
                actor_user_id=actor_user_id,
                authority_check=authority_check,
                fence_job_id=getattr(handle, "fence_job_id", None),
                fence_attempt=getattr(handle, "fence_attempt", None),
                publication_guard=(
                    _publication_guard if callable(guard_factory) else None
                ),
            ),
            vector_surface=True,
        )

    try:
        document = _publish()
    except DocumentRevisionSuperseded:
        handle.supersede("newer_revision_requested")
        return
    except IndexGenerationSuperseded:
        durable_cancel_check = getattr(
            handle,
            "durable_cancel_requested",
            None,
        )
        if handle.cancelled or (
            callable(durable_cancel_check) and durable_cancel_check()
        ):
            handle.cancel("client_requested_cancel")
        else:
            # A reclaimed attempt may discover the new owner inside the
            # publication transaction. Its fenced terminal write is a no-op;
            # the new attempt remains the sole authority.
            handle.supersede("publication_fence_lost")
        return
    handle.document_completed(
        document.id,
        context_omitted_chunks=(
            prepared.embedded.context_omitted_chunks
            if prepared.embedded is not None
            else 0
        ),
    )
    counted["completed"] = True
    _count_indexed_document("completed")
    checkpoint_document = getattr(handle, "checkpoint_document", None)
    if callable(checkpoint_document):
        checkpoint_document(
            document.id,
            embedding_receipt=receipt_checkpoint,
        )
    handle.progress(completed_documents=1, current_document_title="")
    if bool(getattr(handle, "raw_by_user_choice", False)):
        handle.complete_raw_by_user_choice()
    else:
        handle.complete()


def store_supports_reembed(knowledge_service: "KnowledgeService") -> bool:
    """Whether the store can re-embed behind a mutation boundary."""
    store = knowledge_service.knowledge.store
    return (
        callable(getattr(store, "reembed_document", None))
        and bool(getattr(store, "supports_safe_reindex", True))
        and callable(getattr(store, "activate_generation", None))
        and callable(getattr(store, "discard_generation", None))
    )


def execute_reindex_job(
    handle: "IndexingJobHandle",
    *,
    knowledge_service: "KnowledgeService",
    collection_id: str,
    embedding_model: str,
    generation_id: str | None = None,
    quota_service: "QuotaService | None" = None,
    quota_subject: "QuotaSubject | None" = None,
    authority_check: Callable[[], None] | None = None,
    actor_user_id: uuid.UUID | None = None,
    max_parallel_documents: int = _COLLECTION_DOCUMENT_CONCURRENCY,
) -> None:
    """Re-embed every document in one collection, emitting progress.

    The single execution body shared by the in-process dispatch thread:
    it snapshots the collection manifest, stages each document through the
    same pipeline as first-time ingestion, folds concurrent source changes in
    as deltas, validates the final manifest, and atomically publishes the
    complete generation. Exceptions propagate to the store worker, which owns
    the pause or failure path.

    Args:
        handle: Store-backed job handle (progress, complete, cancel).
        collection_id: The collection whose documents are re-embedded.
        embedding_model: The collection's immutable embedding model id.
        quota_service: Optional usage meter. When wired, the embedded
            text of each document is booked against ``EMBEDDING_TOKENS``
            as it completes (incremental, so a cancelled run only counts
            what it actually embedded). ``None`` for unmetered
            deployments.
        quota_subject: Explicit quota account, reconstructed from the job's
            canonical user UUID and tenant — token accounting fires
            regardless of which thread executes the job.
    """
    store = knowledge_service.knowledge.store
    if authority_check is not None:
        authority_check()
    # The worker execution body is synchronous; the knowledge store/service
    # are async. Bridge each call to completion on this worker thread.
    documents = run_coro_sync(store.list_documents(collection_id))
    if generation_id is None:
        generation_id = f"gen_{uuid.uuid4().hex[:20]}"
    collection = run_coro_sync(store.get_collection(collection_id))
    raw_by_user_choice = bool(getattr(handle, "raw_by_user_choice", False))
    build_contract_hash = knowledge_service.build_contract_hash(
        collection,
        contextualize=not raw_by_user_choice,
    )
    requested_manifest = {
        document.id: document.active_revision_id or "" for document in documents
    }
    generation = run_coro_sync(
        knowledge_service.begin_generation(
            collection_id=collection_id,
            generation_id=generation_id,
            build_contract_hash=build_contract_hash,
            manifest=requested_manifest,
            actor_user_id=actor_user_id,
        )
    )
    # A resume must compare against the manifest captured when this immutable
    # generation began. Replacing it with a fresh source snapshot could make a
    # completed-document checkpoint skip a newer revision that was never
    # embedded into this generation.
    staged_manifest = dict(generation.manifest)
    _run_dependency_boundary(
        knowledge_service.prune_expired_generations(collection_id=collection_id),
        vector_surface=True,
    )
    completed_ids = getattr(handle, "completed_document_ids", frozenset())
    confirmed_receipts: dict[str, dict[str, Any]] = {}
    confirmed_receipts_lock = threading.Lock()
    progress_lock = threading.Lock()
    reported_completed = len(completed_ids)

    def _confirm_quota_receipt(
        document,
        *,
        work_receipt=None,
        persisted: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Durably confirm usage before any completion checkpoint/publication."""

        if quota_service is None or quota_subject is None:
            return None
        adjustment_id = generation_embedding_adjustment_id(
            generation_id=generation_id,
            document_id=document.id,
            revision_id=document.active_revision_id,
            document_text=document.text,
            build_contract_hash=build_contract_hash,
        )
        revision_id = document.active_revision_id or ""
        if persisted is not None:
            try:
                checkpoint = _embedding_receipt_checkpoint(
                    adjustment_id=str(persisted["adjustment_id"]),
                    amount=int(persisted["amount"]),
                    input_count=int(persisted["input_count"]),
                    input_sha256=str(persisted["input_sha256"]),
                    revision_id=str(persisted["revision_id"]),
                    build_contract_hash=str(persisted["build_contract_hash"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GenerationValidationError(
                    "generation checkpoint has an invalid embedding receipt"
                ) from exc
            if (
                checkpoint["adjustment_id"] != adjustment_id
                or checkpoint["revision_id"] != revision_id
                or checkpoint["build_contract_hash"] != build_contract_hash
            ):
                raise GenerationValidationError(
                    "generation checkpoint embedding receipt contradicts its "
                    "immutable document contract"
                )
        elif work_receipt is not None:
            checkpoint = _embedding_receipt_checkpoint(
                adjustment_id=adjustment_id,
                amount=work_receipt.amount,
                input_count=work_receipt.input_count,
                input_sha256=work_receipt.input_sha256,
                revision_id=revision_id,
                build_contract_hash=build_contract_hash,
            )
        else:
            raise GenerationValidationError(
                "generation publication lacks an exact embedding receipt"
            )

        if work_receipt is not None and (
            checkpoint["amount"] != work_receipt.amount
            or checkpoint["input_count"] != work_receipt.input_count
            or checkpoint["input_sha256"] != work_receipt.input_sha256
        ):
            raise GenerationValidationError(
                "replayed embedding work contradicts its durable receipt"
            )
        # A document is normally confirmed by one worker only. The lock also
        # makes that invariant safe during a concurrent rebuild replay.
        with confirmed_receipts_lock:
            confirmed = confirmed_receipts.get(document.id)
            if (
                confirmed is not None
                and confirmed["adjustment_id"] == checkpoint["adjustment_id"]
            ):
                if confirmed != checkpoint:
                    raise GenerationValidationError(
                        "generation produced contradictory embedding receipts"
                    )
                return checkpoint
            try:
                quota_service.record_blocking_once(
                    quota_subject,
                    QuotaDimension.EMBEDDING_TOKENS,
                    checkpoint["amount"],
                    adjustment_id=adjustment_id,
                )
            except QuotaAdjustmentConflict as exc:
                raise GenerationValidationError(
                    "quota receipt contradicts the immutable generation work"
                ) from exc
            except Exception as exc:
                raise IndexingDependencyError(
                    ("The quota charge for the prepared generation could not be "
                    "confirmed. The active search generation is unchanged; the "
                    "operation can resume."),
                    error_type="quota_dependency_error",
                ) from exc
            confirmed_receipts[document.id] = checkpoint
        return checkpoint

    def _phase(name: str, *, current_batch: int = 0, total_batches: int = 0) -> None:
        phase_method = getattr(handle, "phase", None)
        if callable(phase_method):
            phase_method(
                name,
                current_batch=current_batch,
                total_batches=total_batches,
            )

    handle.begin(len(documents))

    def _aggregate_progress(current_document_title: str = "") -> None:
        """Emit monotonic aggregate progress while document workers overlap."""

        nonlocal reported_completed
        with progress_lock:
            reported_completed = max(
                reported_completed,
                len(getattr(handle, "completed_document_ids", frozenset())),
            )
            handle.progress(
                completed_documents=reported_completed,
                current_document_title=current_document_title,
            )

    def _process_document(
        listed_document,
        *,
        reuse_checkpoint: bool,
        record_completion: bool = True,
        stop_event: threading.Event | None = None,
    ) -> bool:
        if authority_check is not None:
            authority_check()
        if handle.cancelled or (stop_event is not None and stop_event.is_set()):
            return False
        try:
            # Enumeration fixes only the work list. The canonical document is
            # reloaded immediately before embedding so a preceding mutation
            # that committed before the maintenance boundary cannot feed stale
            # title/text/metadata into this pass.
            document = run_coro_sync(store.get_document(listed_document.id))
            if authority_check is not None:
                authority_check()
            document_started = getattr(handle, "document_started", None)
            if callable(document_started):
                document_started(document.id)
            _aggregate_progress(document.title)

            def _document_phase(
                name: str,
                *,
                current_batch: int = 0,
                total_batches: int = 0,
            ) -> None:
                document_progress = getattr(handle, "document_progress", None)
                if callable(document_progress):
                    document_progress(
                        document.id,
                        name,
                        current_batch=current_batch,
                        total_batches=total_batches,
                    )
                else:
                    _phase(
                        name,
                        current_batch=current_batch,
                        total_batches=total_batches,
                    )

            def _cancel_check() -> None:
                if handle.cancelled or (
                    stop_event is not None and stop_event.is_set()
                ):
                    raise _IndexingCancellation(
                        "server-side indexing cancellation requested"
                    )

            try:
                _document_phase("contextualization")
                checkpoint_reader = getattr(handle, "context_batch_checkpoints", None)
                stored_checkpoints = (
                    checkpoint_reader(document.id)
                    if callable(checkpoint_reader) and reuse_checkpoint
                    else []
                )
                context_checkpoints = [
                    ContextualizationBatchCheckpoint.from_dict(item)
                    for item in stored_checkpoints
                ]
                checkpoint_writer = getattr(handle, "checkpoint_context_batch", None)
                reembedded = _run_dependency_boundary(
                    knowledge_service.reembed_document_with_receipt(
                        document=document,
                        embedding_model=embedding_model,
                        generation_id=generation_id,
                        fence_job_id=getattr(handle, "fence_job_id", None),
                        fence_attempt=getattr(handle, "fence_attempt", None),
                        on_context_batch=lambda current, total: _document_phase(
                            "contextualization",
                            current_batch=current,
                            total_batches=total,
                        ),
                        on_context_checkpoint=(
                            lambda checkpoint: (
                                checkpoint_writer(document.id, checkpoint.as_dict())
                                if callable(checkpoint_writer)
                                else None
                            )
                        ),
                        context_checkpoints=context_checkpoints,
                        cancel_check=_cancel_check,
                        on_embedding_started=lambda: _document_phase("embedding"),
                        on_embedding_batch=lambda current, total: _document_phase(
                            "embedding",
                            current_batch=current,
                            total_batches=total,
                        ),
                        on_embedding_wait=lambda current, total: _document_phase(
                            "embedding_wait",
                            current_batch=current,
                            total_batches=total,
                        ),
                        authority_check=authority_check,
                        actor_user_id=actor_user_id,
                        contextualize=not raw_by_user_choice,
                    ),
                    vector_surface=True,
                )
            except _IndexingCancellation:
                return False
        except DocumentNotFound:
            # Deleted between enumeration and re-embed: visible, not
            # fatal — the remaining documents still get re-embedded.
            log.warning(
                "Reindex %s: document %s vanished mid-run; skipping",
                collection_id,
                listed_document.id,
            )
            return True
        # Provider work alone is not a completed durable unit. The receipt
        # must commit first so a checkpoint can never authorize publication
        # of unmetered work. Repeating either side after a crash is safe: the
        # immutable adjustment id suppresses a second charge.
        receipt_checkpoint = _confirm_quota_receipt(
            document,
            work_receipt=reembedded.work_receipt,
        )
        if record_completion:
            handle.document_completed(
                document.id,
                context_omitted_chunks=reembedded.context_omitted_chunks,
            )
            _count_indexed_document("completed")
            checkpoint = getattr(handle, "checkpoint_document", None)
            if callable(checkpoint):
                checkpoint(
                    document.id,
                    embedding_receipt=receipt_checkpoint,
                )
            _aggregate_progress(document.title)
        return True

    def _process_documents_bounded(
        candidates: list[Any],
        *,
        reuse_checkpoint: bool,
        record_completion: bool = True,
    ) -> bool:
        """Run document pipelines concurrently without prestarting the queue."""

        if not candidates:
            return True
        concurrency = max(1, min(int(max_parallel_documents), len(candidates)))
        stop_event = threading.Event()
        candidate_iter = iter(candidates)
        first_error: Exception | None = None

        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="inqtrix-index-document",
        ) as executor:
            in_flight = {}

            def _submit_next() -> bool:
                if stop_event.is_set() or handle.cancelled:
                    stop_event.set()
                    return False
                try:
                    candidate = next(candidate_iter)
                except StopIteration:
                    return False
                # Snapshot the submitting thread's contextvars so the
                # document pipeline's LLM calls keep feature="indexing"
                # (pool threads start with empty context).
                submit_context = contextvars.copy_context()
                future = executor.submit(
                    submit_context.run,
                    functools.partial(
                        _process_document,
                        candidate,
                        reuse_checkpoint=reuse_checkpoint,
                        record_completion=record_completion,
                        stop_event=stop_event,
                    ),
                )
                in_flight[future] = candidate
                return True

            for _ in range(concurrency):
                if not _submit_next():
                    break

            while in_flight:
                finished, _pending = wait(
                    tuple(in_flight),
                    return_when=FIRST_COMPLETED,
                )
                for future in finished:
                    in_flight.pop(future, None)
                    try:
                        completed = future.result()
                    except Exception as exc:  # propagate after peers stop safely
                        if not isinstance(
                            exc,
                            (
                                ContextualizationDependencyError,
                                IndexingDependencyError,
                                ContextualizationValidationError,
                                GenerationValidationError,
                                IndexGenerationSuperseded,
                            ),
                        ):
                            # Pauses/supersedes are resumable, not
                            # document failures — they must not mint
                            # failed samples the resumed pass would
                            # pair with a completed one.
                            _count_indexed_document("failed")
                        if first_error is None:
                            first_error = exc
                        stop_event.set()
                    else:
                        if not completed:
                            stop_event.set()
                    if not stop_event.is_set():
                        _submit_next()

        if first_error is not None:
            raise first_error
        return not stop_event.is_set() and not handle.cancelled

    pending_documents: list[Any] = []
    for listed_document in documents:
        if listed_document.id in completed_ids:
            try:
                current_document = run_coro_sync(store.get_document(listed_document.id))
            except DocumentNotFound:
                continue
            current_revision = current_document.active_revision_id or ""
            if staged_manifest.get(current_document.id) == current_revision:
                receipt_reader = getattr(handle, "embedding_receipt", None)
                persisted_receipt = (
                    receipt_reader(current_document.id)
                    if callable(receipt_reader)
                    else None
                )
                if quota_service is None or quota_subject is None:
                    continue
                if persisted_receipt is not None:
                    _confirm_quota_receipt(
                        current_document,
                        persisted=persisted_receipt,
                    )
                    continue
                # A checkpoint without exact accounting facts is not a
                # publication proof. Re-run the canonical pipeline so usage is
                # derived from real provider inputs, never from raw text.
                pending_documents.append(current_document)
            continue
        pending_documents.append(listed_document)
    if not _process_documents_bounded(
        pending_documents,
        reuse_checkpoint=True,
    ):
        handle.cancel("client_requested_cancel")
        return

    # Optimistic snapshot+delta publication.  There is no arbitrary retry or
    # deadline: each source change is folded into the shadow generation, and a
    # continuously changing collection remains visibly in validation until it
    # reaches a quiet CAS point or the user cancels.
    validation_repair_attempted = False
    while True:
        if authority_check is not None:
            authority_check()
        if handle.cancelled:
            handle.cancel("client_requested_cancel")
            return
        current_documents = run_coro_sync(store.list_documents(collection_id))
        current_manifest = {
            document.id: document.active_revision_id or ""
            for document in current_documents
        }
        removed_ids = set(staged_manifest) - set(current_manifest)
        for document_id in sorted(removed_ids):
            _run_dependency_boundary(
                knowledge_service.remove_document_from_generation(
                    collection_id=collection_id,
                    document_id=document_id,
                    generation_id=generation_id,
                ),
                vector_surface=True,
            )
        changed_documents = [
            document
            for document in current_documents
            if staged_manifest.get(document.id) != (document.active_revision_id or "")
        ]
        if changed_documents or removed_ids:
            _phase("delta")
        if not _process_documents_bounded(
            changed_documents,
            reuse_checkpoint=False,
        ):
            handle.cancel("client_requested_cancel")
            return
        staged_manifest = current_manifest
        _phase("validation")
        # A receipt confirmation is an explicit publication precondition, not
        # merely an earlier best-effort side effect. This repairs checkpoints
        # created by an older attempt and remains idempotent during validation
        # repair or worker redelivery.
        for document in current_documents:
            persisted_receipt = confirmed_receipts.get(document.id)
            if persisted_receipt is None:
                receipt_reader = getattr(handle, "embedding_receipt", None)
                persisted_receipt = (
                    receipt_reader(document.id) if callable(receipt_reader) else None
                )
            _confirm_quota_receipt(
                document,
                persisted=persisted_receipt,
            )
        try:
            _run_dependency_boundary(
                knowledge_service.activate_generation(
                    collection_id=collection_id,
                    generation_id=generation_id,
                    expected_document_ids=list(staged_manifest),
                    expected_manifest=staged_manifest,
                    build_contract_hash=build_contract_hash,
                    fence_job_id=getattr(handle, "fence_job_id", None),
                    fence_attempt=getattr(handle, "fence_attempt", None),
                    actor_user_id=actor_user_id,
                ),
                vector_surface=True,
            )
        except GenerationManifestChanged:
            continue
        except GenerationValidationError:
            if validation_repair_attempted:
                raise
            validation_repair_attempted = True
            _phase("validation")
            log.warning(
                "Generation %s failed publication validation; rebuilding its "
                "staged documents once before pausing",
                generation_id,
                extra={
                    "event": "knowledge.generation.validation_repair",
                    "collection_id": collection_id,
                    "generation_id": generation_id,
                },
            )
            if not _process_documents_bounded(
                current_documents,
                reuse_checkpoint=True,
                record_completion=False,
            ):
                handle.cancel("client_requested_cancel")
                return
            continue
        break
    _phase("publication")
    handle.progress(completed_documents=len(staged_manifest), current_document_title="")
    if raw_by_user_choice:
        complete_raw = getattr(handle, "complete_raw_by_user_choice", None)
        if not callable(complete_raw):
            raise RuntimeError(
                "indexing store lacks explicit raw-choice completion contract"
            )
        complete_raw()
    else:
        handle.complete()


def execute_indexing_operation(
    handle: "IndexingJobHandle",
    *,
    knowledge_service: "KnowledgeService",
    operation_kind: str,
    collection_id: str,
    embedding_model: str,
    generation_id: str | None,
    document_id: str | None,
    revision_id: str | None,
    quota_service: "QuotaService | None" = None,
    quota_subject: "QuotaSubject | None" = None,
    authority_check: Callable[[], None] | None = None,
    actor_user_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Execute one canonical operation kind through the shared job bodies.

    API-local execution, no-queue resume reconstruction, and Valkey workers
    all use this dispatcher. Missing durable identity is a hard error before
    provider work starts; callers keep a paused row paused when validating a
    resume specification.

    This is also the ONE place that binds the usage-ledger subject for
    indexing: binding it in the Valkey worker alone would make the
    consumption history depend on the deployment topology (in-process
    indexing books quota but no ledger rows).
    """

    from inqtrix.observability.context import (
        bind_feature,
        bind_usage_subject,
        clear_usage_subject,
        reset_feature,
    )

    feature_token = bind_feature("indexing")
    # Identity for the ledger comes from the ACTOR, never from the quota
    # service: quotas are an unrelated, default-OFF feature, and gating
    # the ledger on them would make consumption history depend on a
    # billing toggle. quota_subject stays a fallback for callers that
    # only carry it.
    ledger_tenant = tenant_id or (
        quota_subject.tenant_id if quota_subject is not None else None
    )
    ledger_user = actor_user_id or (
        quota_subject.user_id if quota_subject is not None else None
    )
    bind_usage_subject(ledger_tenant, ledger_user, workspace_id)
    try:
        _dispatch_indexing_operation(
            handle,
            knowledge_service=knowledge_service,
            operation_kind=operation_kind,
            collection_id=collection_id,
            embedding_model=embedding_model,
            generation_id=generation_id,
            document_id=document_id,
            revision_id=revision_id,
            quota_service=quota_service,
            quota_subject=quota_subject,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
        )
    finally:
        reset_feature(feature_token)
        clear_usage_subject()


def _dispatch_indexing_operation(
    handle: "IndexingJobHandle",
    *,
    knowledge_service: "KnowledgeService",
    operation_kind: str,
    collection_id: str,
    embedding_model: str,
    generation_id: str | None,
    document_id: str | None,
    revision_id: str | None,
    quota_service: "QuotaService | None" = None,
    quota_subject: "QuotaSubject | None" = None,
    authority_check: Callable[[], None] | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    from inqtrix.server.indexing import IndexingOperationKind

    kind = IndexingOperationKind(operation_kind)
    if kind == IndexingOperationKind.DOCUMENT_REVISION:
        if not document_id or not revision_id:
            raise ValueError(
                "document revision job lacks reserved document/revision identity"
            )
        execute_document_revision_job(
            handle,
            knowledge_service=knowledge_service,
            document_id=document_id,
            revision_id=revision_id,
            quota_service=quota_service,
            quota_subject=quota_subject,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
        )
        return
    if not generation_id:
        raise ValueError("collection generation job lacks generation identity")
    execute_reindex_job(
        handle,
        knowledge_service=knowledge_service,
        collection_id=collection_id,
        embedding_model=embedding_model,
        generation_id=generation_id,
        quota_service=quota_service,
        quota_subject=quota_subject,
        authority_check=authority_check,
        actor_user_id=actor_user_id,
    )


class IndexingService:
    """Submit background collection generations and document revisions.

    Args:
        knowledge_service: The collection/document service whose store
            and re-embed pipeline the worker drives.
        job_store: The configured indexing registry/queue that owns dispatch,
            events, fencing, and retention.
        quota_service: Optional usage meter threaded into the worker for
            incremental embedding-token accounting.
    """

    def __init__(
        self,
        *,
        knowledge_service: "KnowledgeService",
        job_store: "IndexingJobStore",
        quota_service: "QuotaService | None" = None,
        authority: "CollectionEditAuthorizer | None" = None,
    ) -> None:
        self._knowledge_service = knowledge_service
        self._job_store = job_store
        self._quota_service = quota_service
        self._authority = authority
        knowledge_service.bind_collection_maintenance(
            active_check=job_store.has_active_job,
        )

    @property
    def job_store(self) -> "IndexingJobStore":
        """The underlying job store (read-side surface for the router)."""
        return self._job_store

    @property
    def authority(self) -> "CollectionEditAuthorizer | None":
        """Live collection-edit checker shared with queue workers."""
        return self._authority

    def resume(
        self,
        job_id: str,
        *,
        principal: "Principal | None",
        raw_by_user_choice: bool = False,
        automatic: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Resume a paused job with canonically reconstructed execution work.

        ``automatic`` marks the auto-resume sweep's call: the store resumes
        only a due dependency pause and counts the attempt.

        Queue-backed deployments execute from the same persisted fields in the
        worker. In a Postgres/no-queue deployment this rebinds a closure lost
        during an API restart before the paused-to-queued CAS. Invalid or
        incomplete identity raises a typed conflict and leaves the checkpoint
        untouched.
        """

        from inqtrix.auth.principal import Principal
        from inqtrix.quota.models import QuotaSubject
        from inqtrix.server.indexing import (
            IndexingOperationKind,
            IndexingResumeUnavailable,
        )

        current = self._job_store.get(job_id)
        if current.get("status") not in {
            "paused_dependency",
            "paused_validation",
        }:
            return current
        spec_reader = getattr(self._job_store, "execution_spec", None)
        if not callable(spec_reader):
            raise IndexingResumeUnavailable(
                ("The paused indexing job cannot be safely reconstructed from this "
                "storage backend. The checkpoint is unchanged.")
            )
        spec = spec_reader(job_id)
        has_user = spec.created_by_user_id is not None
        has_tenant = spec.created_by_tenant_id is not None
        if has_user != has_tenant:
            raise IndexingResumeUnavailable(
                ("The paused indexing job has no complete execution identity. The "
                "checkpoint is unchanged.")
            )
        execution_principal: Principal | None = None
        quota_subject: QuotaSubject | None = None
        actor_user_id: uuid.UUID | None = None
        if has_user:
            if not spec.created_by_tenant_id:
                raise IndexingResumeUnavailable(
                    ("The paused indexing job has no complete execution identity. The "
                    "checkpoint is unchanged.")
                )
            actor_user_id = uuid.UUID(str(spec.created_by_user_id))
            execution_principal = Principal(
                user_id=actor_user_id,
                kind="oidc_session",
                tenant_id=spec.created_by_tenant_id,
                role="member",
            )
            if self._quota_service is not None:
                quota_subject = QuotaSubject(
                    tenant_id=spec.created_by_tenant_id,
                    user_id=actor_user_id,
                )

        kind = IndexingOperationKind(spec.operation_kind)
        if kind == IndexingOperationKind.DOCUMENT_REVISION:
            if not spec.document_id or not spec.revision_id:
                raise IndexingResumeUnavailable(
                    ("The paused document revision lacks its canonical document or "
                    "revision ID. The checkpoint is unchanged.")
                )
        elif not spec.generation_id:
            raise IndexingResumeUnavailable(
                ("The paused collection indexing job lacks its canonical generation. The"
                " checkpoint is unchanged.")
            )

        if raw_by_user_choice and kind == IndexingOperationKind.COLLECTION_GENERATION:
            run_coro_sync(
                self._knowledge_service.reset_generation_for_raw_choice(
                    collection_id=spec.collection_id,
                    generation_id=str(spec.generation_id),
                )
            )

        def _check_authority() -> None:
            if self._authority is not None:
                self._authority.check(
                    spec.collection_id,
                    execution_principal,
                )

        def _work(handle: "IndexingJobHandle") -> None:
            try:
                execute_indexing_operation(
                    handle,
                    knowledge_service=self._knowledge_service,
                    operation_kind=kind.value,
                    collection_id=spec.collection_id,
                    embedding_model=spec.embedding_model,
                    generation_id=spec.generation_id,
                    document_id=spec.document_id,
                    revision_id=spec.revision_id,
                    quota_service=self._quota_service,
                    quota_subject=quota_subject,
                    authority_check=_check_authority,
                    actor_user_id=actor_user_id,
                    tenant_id=(
                        execution_principal.tenant_id
                        if execution_principal is not None
                        else None
                    ),
                )
            except ContextualizationDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except IndexingDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except ContextualizationValidationError as exc:
                handle.pause_validation(str(exc))

        cleanup: Callable[[], None] | None = None
        if kind == IndexingOperationKind.COLLECTION_GENERATION:

            def _cleanup() -> None:
                run_coro_sync(
                    self._knowledge_service.discard_generation(
                        collection_id=spec.collection_id,
                        generation_id=str(spec.generation_id),
                        actor_user_id=actor_user_id,
                    )
                )

            cleanup = _cleanup

        if raw_by_user_choice:
            return self._job_store.resume_raw_by_user_choice(
                job_id,
                actor_user_id=(principal.user_id if principal is not None else None),
                work=_work,
                cleanup=cleanup,
            )
        return self._job_store.resume(
            job_id,
            actor_user_id=(principal.user_id if principal is not None else None),
            work=_work,
            cleanup=cleanup,
            automatic=automatic,
            now=now,
        )

    def resume_due_pauses(self, *, now: float | None = None) -> "SweepReport":
        """Resume the dependency pauses whose automatic resume is due.

        Each job continues through :meth:`resume` as its creator, the path a
        person's click takes: rebound locally where this process executes the
        work, queued for a worker where one does.
        """
        from inqtrix.indexing_auto_resume import sweep_due_resumes

        moment = time.time() if now is None else now
        return sweep_due_resumes(
            self._job_store.due_auto_resumes(now=moment),
            now=moment,
            policy=self._job_store.auto_resume_policy,
            resume=lambda job_id: self.resume(
                job_id, principal=None, automatic=True, now=moment
            ),
            stop=self._job_store.stop_auto_resume,
            circuit=self._knowledge_service.contextualization_circuit_view,
        )

    def submit(
        self,
        *,
        collection: KnowledgeCollection,
        index_id: str | None = None,
        workspace_id: str | None = None,
        principal: "Principal | None" = None,
    ) -> dict[str, Any]:
        """Queue one reindex job for *collection* and return its summary.

        Access to the collection is the router's concern (checked before
        this call); this method only wires the work closure and the
        canonical quota attribution.

        Raises:
            ReindexUnsupported: The active vector store cannot re-embed
                in place.
            inqtrix.server.indexing.IndexingJobConflict: The collection
                already has an active collection-generation job (mapped to
                HTTP 409). Document-revision deltas do not occupy that slot.
            inqtrix.server.indexing.IndexingQueueFull: The waiting queue
                is full (mapped to HTTP 429).
        """
        if not store_supports_reembed(self._knowledge_service):
            raise ReindexUnsupported(
                "the active knowledge store cannot safely serialize reindexing"
            )
        quota_subject = (
            self._quota_service.subject_for(principal)
            if self._quota_service is not None
            else None
        )
        collection_id = collection.id
        embedding_model = collection.embedding_model
        generation_id = f"gen_{uuid.uuid4().hex[:20]}"

        def _check_authority() -> None:
            if self._authority is not None:
                self._authority.check(collection_id, principal)

        def _work(handle: "IndexingJobHandle") -> None:
            try:
                execute_indexing_operation(
                    handle,
                    knowledge_service=self._knowledge_service,
                    operation_kind="collection_generation",
                    collection_id=collection_id,
                    embedding_model=embedding_model,
                    generation_id=generation_id,
                    document_id=None,
                    revision_id=None,
                    quota_service=self._quota_service,
                    quota_subject=quota_subject,
                    authority_check=_check_authority,
                    actor_user_id=(
                        principal.user_id if principal is not None else None
                    ),
                    tenant_id=(
                        principal.tenant_id if principal is not None else None
                    ),
                    workspace_id=workspace_id,
                )
            except ContextualizationDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except IndexingDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except ContextualizationValidationError as exc:
                handle.pause_validation(str(exc))

        def _cleanup() -> None:
            run_coro_sync(
                self._knowledge_service.discard_generation(
                    collection_id=collection_id,
                    generation_id=generation_id,
                    actor_user_id=(
                        principal.user_id if principal is not None else None
                    ),
                )
            )

        return self._job_store.submit(
            collection_id=collection_id,
            collection_name=collection.name,
            embedding_model=embedding_model,
            work=_work,
            cleanup=_cleanup,
            generation_id=generation_id,
            index_id=index_id,
            workspace_id=workspace_id,
            created_by_user_id=principal.user_id if principal is not None else None,
            created_by_tenant_id=(
                principal.tenant_id if principal is not None else None
            ),
        )

    async def submit_document_revision(
        self,
        *,
        collection: KnowledgeCollection,
        title: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        page_texts: list[str] | None = None,
        workspace_id: str | None = None,
        principal: "Principal | None" = None,
        visible_to: Any = None,
        source_scope: "SourceScope | None" = None,
    ) -> dict[str, Any]:
        """Reserve immutable source intent, then enqueue its provider work."""
        reservation = await self._knowledge_service.reserve_document_revision(
            collection_id=collection.id,
            title=title,
            text=text,
            metadata=metadata,
            page_texts=page_texts,
            visible_to=visible_to,
            source_scope=source_scope,
        )
        quota_subject = (
            self._quota_service.subject_for(principal)
            if self._quota_service is not None
            else None
        )

        def _check_authority() -> None:
            if self._authority is not None:
                self._authority.check(collection.id, principal)

        def _work(handle: "IndexingJobHandle") -> None:
            try:
                execute_indexing_operation(
                    handle,
                    knowledge_service=self._knowledge_service,
                    operation_kind="document_revision",
                    collection_id=collection.id,
                    embedding_model=collection.embedding_model,
                    generation_id=None,
                    document_id=reservation.document_id,
                    revision_id=reservation.revision_id,
                    quota_service=self._quota_service,
                    quota_subject=quota_subject,
                    authority_check=_check_authority,
                    actor_user_id=(
                        principal.user_id if principal is not None else None
                    ),
                    tenant_id=(
                        principal.tenant_id if principal is not None else None
                    ),
                    workspace_id=workspace_id,
                )
            except ContextualizationDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except IndexingDependencyError as exc:
                handle.pause_dependency(str(exc), error_type=exc.error_type)
            except ContextualizationValidationError as exc:
                handle.pause_validation(str(exc))

        from inqtrix.server.indexing import IndexingOperationKind

        # Durable stores expose a deliberately synchronous bridge to their
        # private database loop. Running that bridge on the request loop can
        # deadlock concurrent reservations: one request waits synchronously
        # for the job-store collection lock while another request still needs
        # the request loop to finish and release that same lock.
        return await asyncio.to_thread(
            self._job_store.submit,
            collection_id=collection.id,
            collection_name=collection.name,
            embedding_model=collection.embedding_model,
            operation_kind=IndexingOperationKind.DOCUMENT_REVISION,
            document_id=reservation.document_id,
            revision_id=reservation.revision_id,
            work=_work,
            workspace_id=workspace_id,
            created_by_user_id=(principal.user_id if principal is not None else None),
            created_by_tenant_id=(
                principal.tenant_id if principal is not None else None
            ),
        )


def _count_indexed_document(outcome: str) -> None:
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is not None:
        metrics.count_indexed_documents(outcome=outcome)
