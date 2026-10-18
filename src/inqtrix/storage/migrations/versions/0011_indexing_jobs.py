"""Durable reindex-job schema: indexing_jobs and indexing_job_events.

Revision ID: 0011_indexing_jobs
Revises: 0010_knowledge

Creates the durable reindex-job tables from their metadata snapshot and
applies the established security layering: DML grants for ``inqtrix_app``
and ENABLE + FORCE row-level security with the fail-closed tenant policy
(InitPlan ``(SELECT ...)`` wrapper), identical to ``0003_runs_durability``
and ``0010_knowledge``. The status CHECK constraint mirrors
:class:`~inqtrix.server.indexing.IndexingJobStatus`; the lifecycle
ordering lives only in that application enum.

This is the durable twin of the in-memory reindex-job store: a background
re-embed becomes a row a worker process can claim, so it survives a server
restart, not just closing the browser. Document VECTORS live in Qdrant;
canonical text lives in ``knowledge_documents`` — these tables hold only
job state and the event log.
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON

indexing_metadata = MetaData()

indexing_jobs = Table(
    "indexing_jobs",
    indexing_metadata,
    Column("job_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # References ``knowledge_collections.id`` logically, but with no hard
    # FK: that table lives in a different MetaData, and a cross-metadata
    # FK breaks ``create_all`` (it cannot resolve the referent). The run
    # schema is likewise foreign-key-free across domains. An orphan job
    # for a deleted collection is harmless — terminal ones TTL out, an
    # active one re-embeds zero documents and completes.
    Column("collection_id", Text, nullable=False),
    Column("collection_name", Text, nullable=False, server_default=text("''")),
    Column("embedding_model", Text, nullable=False, server_default=text("''")),
    Column("index_id", Text, nullable=True),
    Column(
        "status",
        Text,
        nullable=False,
        server_default=text("'queued'"),
    ),
    Column("workspace_id", Text, nullable=True),
    Column("created_by_sub", Text, nullable=True),
    Column("created_by_tenant_id", Text, nullable=True),
    Column("total_documents", Integer, nullable=False, server_default=text("0")),
    Column(
        "completed_documents", Integer, nullable=False, server_default=text("0")
    ),
    Column(
        "current_document_title", Text, nullable=False, server_default=text("''")
    ),
    Column("error", JSON, nullable=True),
    Column(
        "cancel_requested",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column("claimed_by", Text, nullable=True),
    Column("attempt", Integer, nullable=False, server_default=text("0")),
    Column("event_seq", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Float, nullable=False),
    Column("started_at", Float, nullable=True),
    Column("finished_at", Float, nullable=True),
    Index(
        "ix_indexing_jobs_collection_created", "collection_id", "created_at"
    ),
    Index("ix_indexing_jobs_tenant_status", "tenant_id", "status"),
    # One active reindex per collection, enforced at the database so two
    # processes cannot race two re-embed passes (IndexingJobConflict).
    Index(
        "uq_indexing_jobs_active_collection",
        "collection_id",
        unique=True,
        postgresql_where=text("status IN ('queued', 'running')"),
    ),
)
"""Durable reindex-job records — the source of truth once
``INQTRIX_STORAGE_BACKEND=postgres`` is active. The status CHECK
constraint is added in migration 0011 (values from
:class:`~inqtrix.server.indexing.IndexingJobStatus`); the progress
snapshot is derived from the count columns, not stored, so it cannot
drift from the wire shape."""

indexing_job_events = Table(
    "indexing_job_events",
    indexing_metadata,
    Column(
        "job_id",
        Text,
        ForeignKey("indexing_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("sequence", Integer, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("type", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("data", JSON, nullable=False, server_default=text("'{}'")),
)
"""Per-job event log in emission order; ``(job_id, sequence)`` is the
primary key and ``sequence`` is allocated from
``indexing_jobs.event_seq`` so SSE replay reproduces the in-memory
stream byte-compatibly."""

revision = "0011_indexing_jobs"
down_revision = "0010_knowledge"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
# Order matters for FK creation/drop: parents before children.
_TABLES = ("indexing_jobs", "indexing_job_events")

_STATUS_VALUES = (
    "'queued', 'running', 'completed', 'failed', 'cancelled', 'expired'"
)


def upgrade() -> None:
    bind = op.get_bind()
    indexing_metadata.create_all(bind=bind)

    op.execute(
        "ALTER TABLE indexing_jobs ADD CONSTRAINT ck_indexing_jobs_status "
        f"CHECK (status IN ({_STATUS_VALUES}))"
    )
    for table in _TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
                WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexing_metadata.drop_all(bind=bind)
