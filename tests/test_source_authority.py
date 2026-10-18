"""Source lifecycle fencing independent of deletion receipts."""

from __future__ import annotations

import pytest

from inqtrix.source_authority import (
    MemorySourceLifecycleAuthority,
    SourceLifecycleConflict,
    SourceScope,
)


def test_delete_epoch_is_idempotent_only_for_the_same_operation() -> None:
    authority = MemorySourceLifecycleAuthority()
    scope = SourceScope(
        tenant_id="tenant-a",
        source_id="asset:asset-a",
        owner_user_id=None,
        workspace_id="workspace-a",
    )
    write = authority.register_active(scope)

    first = authority.begin_delete(scope, operation_id="del-a")
    replay = authority.begin_delete(scope, operation_id="del-a")

    assert first == replay
    assert first.epoch == write.epoch + 1
    assert authority.get_deletion_permit(
        scope, operation_id="del-a"
    ) == first
    with pytest.raises(SourceLifecycleConflict):
        authority.begin_delete(scope, operation_id="del-b")
    with pytest.raises(SourceLifecycleConflict):
        with authority.active_write(
            scope,
            expected_epoch=write.epoch,
            create_if_missing=False,
        ):
            pass

    authority.complete_delete(first)
    assert authority.get_deletion_permit(
        scope, operation_id="del-a"
    ) == first
    with pytest.raises(SourceLifecycleConflict):
        authority.register_active(scope)


def test_equal_source_ids_in_different_scopes_do_not_share_a_fence() -> None:
    authority = MemorySourceLifecycleAuthority()
    first = SourceScope("tenant-a", "external:42", None, "workspace-a")
    second = SourceScope("tenant-a", "external:42", None, "workspace-b")
    authority.register_active(first)
    second_write = authority.register_active(second)

    authority.begin_delete(first, operation_id="del-a")

    with authority.active_write(
        second,
        expected_epoch=second_write.epoch,
        create_if_missing=False,
    ):
        pass
