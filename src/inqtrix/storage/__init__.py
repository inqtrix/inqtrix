"""Postgres persistence layer for the platform (identity schema first).

Activated by ``INQTRIX_STORAGE_BACKEND=postgres``; the package is
never imported in the default memory mode. Layout:

* :mod:`inqtrix.storage.db` — async engine/session construction and
  the tenant-scoped transaction helper (RLS context).
* :mod:`inqtrix.storage.identity_orm` — SQLAlchemy Core table
  definitions mirroring the migration DDL.
* :mod:`inqtrix.storage.identity_postgres` — Postgres-backed
  implementations of the permission-layer read ports and audit sink.
* :mod:`inqtrix.storage.migrate` — programmatic Alembic runner
  (``inqtrix-migrate`` console script).
* ``migrations/`` — Alembic environment and hand-written revisions
  (row-level security, roles, and grants are DDL no autogenerate
  could produce).
"""
