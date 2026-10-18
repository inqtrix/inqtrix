# Writing a custom storage backend

## Scope

How to swap a persistence component for your own implementation — a blob store,
a run/queue store, a vector store, or the permissions/identity backend — without
forking the dispatch code. Covers the relevant port (ABC/Protocol) for each and
the composition-root seam that injects it. The built-in backends are selected by
env (`INQTRIX_OBJECT_STORE_BACKEND`, `INQTRIX_QUEUE_BACKEND`,
`INQTRIX_VECTOR_BACKEND`, `INQTRIX_STORAGE_BACKEND`); reach for this page only
when env selection is not enough. To swap authentication instead, see
[Writing a custom auth provider](auth-provider.md).

## The injection layers

Inqtrix has three composition entry points, layered from convenient to complete.
Inject at the **lowest layer that exposes the seam you need**:

| Entry point | Exposes | Use for |
|---|---|---|
| `create_app(...)` | `providers`, `strategies`, `auth_provider`, `object_store_impl` | The common swaps: LLM/search providers, auth, the blob store. |
| `register_routes(router, ...)` | the above + `run_store` | Also swapping the run/queue store. |
| `build_container(...)` | the above + `permissions`, `knowledge`, `workspace_admin`, `file_service`, `registry` | Deep swaps: the permission/identity backend, the vector store (via the knowledge context). |

An injected instance always wins over the env enum dispatch. The
`examples/webserver_stacks/*.py` scripts show composing the app this way.

## Object store (blob backend)

Implement the `ObjectStore` ABC
([`src/inqtrix/storage/object_store.py`](../../src/inqtrix/storage/object_store.py)) —
six methods:

```python
from pathlib import Path
from typing import Iterator
from inqtrix.storage.object_store import ObjectStore


class MyObjectStore(ObjectStore):
    def is_available(self) -> bool:
        ...  # read-only health probe; must not create buckets or write sentinels

    def put(self, key: str, source_path: Path) -> None:
        ...  # upload the file at source_path under key (overwrite)

    def exists(self, key: str) -> bool:
        ...  # True only for a complete object; raise ObjectStoreError when the
        # backend is unreachable, so an outage never looks like absence

    def stream(self, key: str) -> Iterator[bytes]:
        ...  # yield the bytes for key (raise ObjectStoreError if missing)

    def delete(self, key: str) -> None:
        ...  # missing keys are tolerated; the registry row is the source of truth

    def list_keys(self, prefix: str) -> Iterator[tuple[str, float]]:
        ...  # yield (key, last_modified_epoch) for the orphan sweep;
        # raise ObjectStoreError rather than yielding nothing on an outage
```

Inject it at the front door — it is wired into the `FileService` in every mode
that builds one (the memory/local default included; the storage backend only
changes the file-metadata registry, not whether the object store is used):

```python
from inqtrix.server import create_app

app = create_app(object_store_impl=MyObjectStore())
```

## Run / queue store

A custom run store (and, with it, your own queue/worker dispatch) implements the
run-store port and is injected at `register_routes`/`build_container` via
`run_store=`. The built-in memory and durable (Postgres + Valkey) stores are the
reference; a durable store owns an engine and a background loop and exposes
`close()`, which the app's lifespan calls on shutdown.

```python
from inqtrix.server.routes import create_router, register_routes

router = create_router()
container = register_routes(router, providers=..., strategies=...,
                            settings=settings, semaphore_factory=...,
                            run_store=MyRunStore())
```

## Vector store

The knowledge engine's vector store is supplied through the knowledge provider
context, injected at `build_container(knowledge=...)`. Implement the vector
store port your `KnowledgeProviderContext` references; the default is the
in-memory store, with Qdrant as the durable option. Knowledge stays off unless
the capability is enabled, so a custom vector store only matters when you run
the knowledge engine.

## Permissions / identity

The permission + identity backend (workspace membership, shares, audit) is
injected at `build_container(permissions=..., workspace_admin=...)`. The
built-in `MemoryIdentityStore` and the Postgres identity store are the
references; sharing is gated on a cookie-session mode plus this backend being
present, while quota is gated on a cookie-session mode alone.

## Rules

- Injected instance **wins** over env dispatch — but if you inject nothing, the
  env enum still applies (no silent fallback to a different backend).
- A store that owns resources (engine, sockets, threads) should expose
  `close()`/`aclose()`; the app lifespan disposes the run store and quota store
  on shutdown.
- Keep configuration in constructor arguments (Constructor-First); your
  composition root or an `examples/` script reads the environment.

## Related docs

- [Writing a custom auth provider](auth-provider.md) — the auth seam.
- [Platform components](../getting-started/platform-components.md) — which store each feature needs.
- [Writing a custom provider](provider.md) — LLM/search providers.
- [Deploy to production](../deployment/production-checklist.md) — TLS, secrets, backups, hardening.
