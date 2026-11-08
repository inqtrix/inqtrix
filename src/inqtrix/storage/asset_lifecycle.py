"""Shared lock identity for upload/deletion races on project assets.

The API and worker use separate database connections (and may use separate
processes), so an in-process mutex cannot protect the boundary.  Every durable
mutation that can create, finalise, or tombstone an asset acquires the same
transaction-scoped PostgreSQL advisory lock before inspecting either table.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def project_resource_lifecycle_lock_name(
    *,
    tenant_id: str,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
    resource_kind: str,
    resource_id: str,
) -> str:
    """Return the canonical owner-scoped lock identity for one resource."""

    return (
        "inqtrix-project-lifecycle:"
        f"{tenant_id}:{created_by_user_id or '-'}:{workspace_id or '-'}:"
        f"{resource_kind}:{resource_id}"
    )


async def lock_asset_lifecycle(
    session: "AsyncSession",
    *,
    tenant_id: str,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
    asset_id: str,
) -> None:
    """Serialize one asset's durable upload and deletion transactions."""

    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    project_resource_lifecycle_lock_name(
                        tenant_id=tenant_id,
                        created_by_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                        resource_kind="asset",
                        resource_id=asset_id,
                    ),
                    0,
                )
            )
        )
    )


async def lock_section_lifecycle(
    session: "AsyncSession",
    *,
    tenant_id: str,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
    section_id: str,
) -> None:
    """Serialize a section delete against child creation and movement."""

    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    project_resource_lifecycle_lock_name(
                        tenant_id=tenant_id,
                        created_by_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                        resource_kind="section",
                        resource_id=section_id,
                    ),
                    0,
                )
            )
        )
    )


async def lock_group_lifecycle(
    session: "AsyncSession",
    *,
    tenant_id: str,
    created_by_user_id: uuid.UUID | None,
    workspace_id: str | None,
    group_id: str,
) -> None:
    """Serialize a group receipt/delete against recreation and child moves."""

    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    project_resource_lifecycle_lock_name(
                        tenant_id=tenant_id,
                        created_by_user_id=created_by_user_id,
                        workspace_id=workspace_id,
                        resource_kind="group",
                        resource_id=group_id,
                    ),
                    0,
                )
            )
        )
    )
