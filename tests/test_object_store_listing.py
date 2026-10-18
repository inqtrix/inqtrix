"""Local object-store inventory listing (maintenance surface)."""

from __future__ import annotations

from pathlib import Path

from inqtrix.storage.object_store import LocalFSObjectStore


def test_local_listing_yields_keys_with_mtime_and_skips_partials(
    tmp_path: Path,
) -> None:
    root = tmp_path / "blobs"
    (root / "tenants" / "default" / "files").mkdir(parents=True)
    complete = root / "tenants" / "default" / "files" / "fl_a"
    complete.write_bytes(b"payload")
    (root / "tenants" / "default" / "files" / "fl_b.part").write_bytes(b"x")
    (root / "unrelated").mkdir()
    (root / "unrelated" / "note.txt").write_bytes(b"y")

    store = LocalFSObjectStore(root=root)
    listing = dict(store.list_keys("tenants/"))

    assert set(listing) == {"tenants/default/files/fl_a"}
    assert listing["tenants/default/files/fl_a"] == complete.stat().st_mtime


def test_local_listing_of_missing_prefix_is_empty(tmp_path: Path) -> None:
    store = LocalFSObjectStore(root=tmp_path / "blobs")
    assert list(store.list_keys("tenants/")) == []


def test_s3_listing_paginates_and_wraps_backend_failures() -> None:
    """The listing leg of a delete job: pagination, tz-aware timestamps,
    and a loud typed error instead of an empty-looking store."""
    import datetime as dt

    from inqtrix.storage.object_store import ObjectStoreError, S3ObjectStore

    stamp_a = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    stamp_b = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc)

    class _Paginator:
        def paginate(self, *, Bucket: str, Prefix: str):
            assert Bucket == "inqtrix-files"
            assert Prefix == "tenants/"
            yield {
                "Contents": [
                    {"Key": "tenants/default/files/fl_1", "LastModified": stamp_a}
                ]
            }
            yield {
                "Contents": [
                    {"Key": "tenants/default/files/fl_2", "LastModified": stamp_b}
                ]
            }

    class _Client:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return _Paginator()

    store = S3ObjectStore(bucket="inqtrix-files")
    store._s3 = lambda: _Client()  # type: ignore[method-assign]

    listing = list(store.list_keys("tenants/"))
    assert listing == [
        ("tenants/default/files/fl_1", stamp_a.timestamp()),
        ("tenants/default/files/fl_2", stamp_b.timestamp()),
    ]

    class _FailingPaginator:
        def paginate(self, **_kwargs):
            yield {"Contents": []}
            raise RuntimeError("bucket gone mid-listing")

    class _FailingClient:
        def get_paginator(self, name: str):
            return _FailingPaginator()

    store._s3 = lambda: _FailingClient()  # type: ignore[method-assign]
    import pytest

    with pytest.raises(ObjectStoreError):
        list(store.list_keys("tenants/"))
