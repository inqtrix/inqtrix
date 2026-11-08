"""The run domain: records, the in-memory stores and the durable backends.

The in-memory :class:`~inqtrix.runs.memory_store.RunStore` remains the
zero-infrastructure default. This package adds the opt-in durability
ladder:

* :mod:`inqtrix.runs.records` — statuses, records and handles every run
  store and its callers share.
* :mod:`inqtrix.runs.shared` — pure helpers (summary shape, event
  expansion) used by every backend so the wire format has exactly one
  source.
* :mod:`inqtrix.runs.postgres_store` — run records, events, and
  results in Postgres (``INQTRIX_STORAGE_BACKEND=postgres``);
  execution stays in-process until a queue backend is configured.
* :mod:`inqtrix.runs.valkey_queue` — Valkey-Streams job dispatch
  (``INQTRIX_QUEUE_BACKEND=valkey``) consumed by ``inqtrix-worker``
  processes (:mod:`inqtrix.worker`).
"""
