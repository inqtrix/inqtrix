"""Run one claimed deletion operation: the fenced attempt itself.

The deletion worker claims an operation from its queue and runs it here; an
account erasure claims the aggregate deletions it registered and runs them
here too, inline, so both execute the same code under the same fence. The
attempt runs as the operation's creator -- the owner of the aggregate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.execution_failures import classify_execution_failure
from inqtrix.runs.deletion_operations import (
    DeletionAttemptSuperseded,
    DeletionJobHandle,
    DeletionStage,
)
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.runs.deletion_postgres import (
        ClaimedDeletionOperation,
        PostgresDeletionOperationStore,
    )
    from inqtrix.services.asset_deletion_service import AssetDeletionService

log = logging.getLogger("inqtrix")


class FencedDeletionJobHandle(DeletionJobHandle):
    def __init__(
        self,
        store: "PostgresDeletionOperationStore",
        operation_id: str,
        attempt: int,
    ) -> None:
        super().__init__(store, operation_id)
        self._attempt = attempt
        self.manages_asset_lifecycle = True

    def assert_current(self) -> None:
        if not self._store.is_attempt_current(
            self.operation_id, fence_attempt=self._attempt
        ):
            raise DeletionAttemptSuperseded(self.operation_id)

    def checkpoint_source_cleanup(
        self, asset_id: str, plan: dict[str, object]
    ) -> None:
        landed = self._store.checkpoint_source_cleanup(
            self.operation_id,
            asset_id=asset_id,
            plan=plan,
            fence_attempt=self._attempt,
        )
        if not landed:
            raise DeletionAttemptSuperseded(self.operation_id)

    def source_deletion_permit(self, scope):
        self.assert_current()
        return self._store.source_deletion_permit(
            self.operation_id,
            scope=scope,
            fence_attempt=self._attempt,
        )

    def progress(
        self,
        stage: DeletionStage,
        *,
        completed_items: int,
        total_items: int,
    ) -> None:
        landed = self._store.progress(
            self.operation_id,
            stage=stage,
            completed_items=completed_items,
            total_items=total_items,
            fence_attempt=self._attempt,
        )
        if not landed:
            raise DeletionAttemptSuperseded(self.operation_id)

    def complete(self) -> None:
        self.terminal_landed = self._store.complete(
            self.operation_id, fence_attempt=self._attempt
        )
        if not self.terminal_landed:
            raise DeletionAttemptSuperseded(self.operation_id)

    def fail(self, message: str, *, error_type: str = "server_error") -> None:
        self.terminal_landed = self._store.fail(
            self.operation_id,
            message,
            error_type=error_type,
            fence_attempt=self._attempt,
        )


def execute_claimed_deletion(
    store: "PostgresDeletionOperationStore",
    service: "AssetDeletionService",
    claimed: "ClaimedDeletionOperation",
) -> FencedDeletionJobHandle:
    """Run one claimed attempt to its terminal state, or until superseded.

    A failure lands as the operation's retryable ``delete_failed`` state
    with a sanitized message; a superseded attempt stops without writing.
    The returned handle says whether the terminal state landed.
    """
    handle = FencedDeletionJobHandle(store, claimed.operation_id, claimed.attempt)
    principal = Principal(
        user_id=claimed.created_by_user_id,
        kind=(
            "oidc_session"
            if claimed.created_by_user_id is not None
            else "anonymous"
        ),
        tenant_id=claimed.tenant_id,
        role=(
            "member"
            if claimed.created_by_user_id is not None
            else "owner"
        ),
    )
    visible_to = UserContext(
        principal=principal,
        workspace_ids=(
            (claimed.workspace_id,) if claimed.workspace_id else ()
        ),
    )
    try:
        service.execute(
            handle,
            manifest=claimed.manifest,
            vector_index_context=claimed.vector_index_context,
            knowledge_context=claimed.knowledge_context,
            session_context=claimed.session_context,
            target_kind=claimed.target_kind,
            target_id=claimed.target_id,
            principal=principal,
            visible_to=visible_to,
            workspace_id=claimed.workspace_id,
        )
    except DeletionAttemptSuperseded:
        log.info(
            "Worker deletion operation %s was superseded by a newer attempt.",
            claimed.operation_id,
        )
    except Exception as exc:  # noqa: BLE001 - terminal write is the answer
        log.error(
            "Worker deletion operation %s failed (error_type=%s)",
            claimed.operation_id,
            type(exc).__name__,
        )
        handle.fail(
            sanitize_error(exc),
            error_type=classify_execution_failure(exc),
        )
    return handle
