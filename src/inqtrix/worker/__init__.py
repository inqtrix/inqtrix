"""Run-execution worker process (``python -m inqtrix.worker``).

Consumes the Valkey job stream, executes runs against the algorithm
registry, and writes records/events/results to the Postgres run store.
Activated by ``INQTRIX_QUEUE_BACKEND=valkey`` (which requires
``INQTRIX_STORAGE_BACKEND=postgres``); the zero-infrastructure default
deployment never starts a worker.
"""
