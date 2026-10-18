"""Durable, source-exact vector cleanup manifests.

The plan contains identifiers only.  It can be checkpointed in a deletion
operation, replayed after a worker crash, and used to prove that the exact
physical points are gone even after canonical Knowledge rows are removed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from inqtrix.source_authority import SourceDeletionPermit, SourceScope


@dataclass(frozen=True)
class SourceCleanupTarget:
    collection_id: str
    document_id: str
    embedding_model: str
    chunk_ids: tuple[str, ...]
    point_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection_id": self.collection_id,
            "document_id": self.document_id,
            "embedding_model": self.embedding_model,
            "chunk_ids": list(self.chunk_ids),
            "point_ids": list(self.point_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceCleanupTarget":
        return cls(
            collection_id=str(value["collection_id"]),
            document_id=str(value["document_id"]),
            embedding_model=str(value["embedding_model"]),
            chunk_ids=tuple(str(item) for item in value.get("chunk_ids", [])),
            point_ids=tuple(str(item) for item in value.get("point_ids", [])),
        )


@dataclass(frozen=True)
class SourceCleanupPlan:
    """Immutable cleanup checkpoint bound to one deletion authority epoch."""

    scope: SourceScope
    authority_epoch: int
    operation_id: str
    targets: tuple[SourceCleanupTarget, ...]
    version: int = 1

    @property
    def document_count(self) -> int:
        return len(self.targets)

    @property
    def chunk_count(self) -> int:
        return sum(len(target.chunk_ids) for target in self.targets)

    @property
    def point_count(self) -> int:
        return sum(len(target.point_ids) for target in self.targets)

    def assert_permit(self, permit: SourceDeletionPermit) -> None:
        if (
            permit.scope != self.scope
            or permit.epoch != self.authority_epoch
            or permit.operation_id != self.operation_id
        ):
            raise ValueError("source cleanup plan does not match deletion permit")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scope": {
                "tenant_id": self.scope.tenant_id,
                "source_id": self.scope.source_id,
                "owner_user_id": (
                    str(self.scope.owner_user_id)
                    if self.scope.owner_user_id is not None
                    else None
                ),
                "workspace_id": self.scope.workspace_id,
            },
            "authority_epoch": self.authority_epoch,
            "operation_id": self.operation_id,
            "targets": [target.as_dict() for target in self.targets],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceCleanupPlan":
        version = int(value.get("version", 0))
        if version != 1:
            raise ValueError(f"unsupported source cleanup plan version: {version}")
        scope_value = value["scope"]
        if not isinstance(scope_value, dict):
            raise ValueError("source cleanup scope must be an object")
        owner_value = scope_value.get("owner_user_id")
        targets_value = value.get("targets", [])
        if not isinstance(targets_value, list):
            raise ValueError("source cleanup targets must be a list")
        plan = cls(
            scope=SourceScope(
                tenant_id=str(scope_value["tenant_id"]),
                source_id=str(scope_value["source_id"]),
                owner_user_id=(
                    uuid.UUID(str(owner_value)) if owner_value is not None else None
                ),
                workspace_id=(
                    str(scope_value["workspace_id"])
                    if scope_value.get("workspace_id") is not None
                    else None
                ),
            ),
            authority_epoch=int(value["authority_epoch"]),
            operation_id=str(value["operation_id"]),
            targets=tuple(
                SourceCleanupTarget.from_dict(item)
                for item in targets_value
                if isinstance(item, dict)
            ),
            version=version,
        )
        for target in plan.targets:
            if len(target.chunk_ids) != len(target.point_ids):
                raise ValueError("source cleanup chunk/point identifiers differ")
        return plan


def empty_source_cleanup_plan(
    permit: SourceDeletionPermit,
) -> SourceCleanupPlan:
    return SourceCleanupPlan(
        scope=permit.scope,
        authority_epoch=permit.epoch,
        operation_id=permit.operation_id,
        targets=(),
    )
