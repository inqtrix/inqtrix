"""Startup guard: per-replica-disk blobs must not span replicas (I.2).

``local`` object storage writes to one replica's filesystem; declaring
more than one replica via ``INQTRIX_REPLICA_COUNT`` turns every
cross-replica download into a silent 404. The container refuses that
combination loudly at composition time.
"""

from __future__ import annotations

import pytest

from inqtrix.server.container import build_object_store
from inqtrix.settings import Settings


def _settings(*, backend: str, replicas: int) -> Settings:
    settings = Settings()
    settings.storage.object_store_backend = backend  # type: ignore[assignment]
    settings.storage.replica_count = replicas
    return settings


def test_local_store_with_multiple_replicas_is_refused() -> None:
    with pytest.raises(RuntimeError, match="INQTRIX_REPLICA_COUNT=2"):
        build_object_store(_settings(backend="local", replicas=2))


def test_local_store_single_replica_stays_zero_infra(tmp_path) -> None:
    settings = _settings(backend="local", replicas=1)
    settings.storage.object_store_path = str(tmp_path)
    store = build_object_store(settings)
    assert store is not None


def test_s3_store_allows_multiple_replicas() -> None:
    settings = _settings(backend="s3", replicas=3)
    settings.storage.s3_endpoint_url = "http://127.0.0.1:9000"
    settings.storage.s3_access_key = "k"
    settings.storage.s3_secret_key = "s"
    store = build_object_store(settings)
    assert store is not None
