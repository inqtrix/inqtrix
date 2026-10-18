"""Unit tests for the object-store implementations.

LocalFS runs offline against tmp_path; the S3 implementation has a
gated integration suite (``INQTRIX_TEST_S3_ENDPOINT``) against the
SeaweedFS dev stack — see ``docs/development/local-infrastructure.md``.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from inqtrix.storage.object_store import (
    LocalFSObjectStore,
    ObjectStoreError,
    S3BucketProvisioning,
    S3ObjectStore,
    S3ServerSideEncryption,
)


def write_source(tmp_path: Path, content: bytes) -> Path:
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    return source


# ------------------------------------------------------------------ #
# LocalFS (offline)
# ------------------------------------------------------------------ #


def test_localfs_roundtrip_and_delete(tmp_path):
    store = LocalFSObjectStore(root=tmp_path / "store")
    source = write_source(tmp_path, b"blob-bytes" * 100)

    store.put("tenants/default/files/fl_1", source)
    streamed = b"".join(store.stream("tenants/default/files/fl_1"))
    assert streamed == b"blob-bytes" * 100

    store.delete("tenants/default/files/fl_1")
    with pytest.raises(ObjectStoreError, match="not found"):
        list(store.stream("tenants/default/files/fl_1"))
    # Deleting again stays a tolerated no-op.
    store.delete("tenants/default/files/fl_1")


def test_localfs_put_overwrites_atomically(tmp_path):
    store = LocalFSObjectStore(root=tmp_path / "store")
    store.put("k", write_source(tmp_path, b"first"))
    store.put("k", write_source(tmp_path, b"second"))
    assert b"".join(store.stream("k")) == b"second"
    # No stray .part staging files left behind.
    assert not list((tmp_path / "store").rglob("*.part"))


def test_localfs_rejects_keys_escaping_the_root(tmp_path):
    store = LocalFSObjectStore(root=tmp_path / "store")
    with pytest.raises(ObjectStoreError, match="escapes"):
        list(store.stream("../outside"))
    with pytest.raises(ObjectStoreError, match="escapes"):
        store.put("../../etc/passwd", write_source(tmp_path, b"x"))


# ------------------------------------------------------------------ #
# S3 / SeaweedFS (gated)
# ------------------------------------------------------------------ #

S3_ENDPOINT = os.environ.get("INQTRIX_TEST_S3_ENDPOINT", "")
MANAGED_S3_BUCKET = os.environ.get("INQTRIX_TEST_S3_MANAGED_BUCKET", "")

s3_gated = pytest.mark.skipif(
    not S3_ENDPOINT,
    reason="INQTRIX_TEST_S3_ENDPOINT not set (SeaweedFS integration)",
)


@pytest.fixture()
def s3_store() -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url=S3_ENDPOINT,
        bucket="inqtrix-files-test",
        access_key=os.environ.get(
            "INQTRIX_TEST_S3_ACCESS_KEY", "inqtrix-dev-access"
        ),
        secret_key=os.environ.get(
            "INQTRIX_TEST_S3_SECRET_KEY", "inqtrix-dev-secret"
        ),
    )


@s3_gated
def test_s3_roundtrip_and_delete(tmp_path, s3_store):
    key = f"tenants/default/files/fl_{uuid.uuid4().hex}"
    payload = b"seaweed-blob" * 1000
    s3_store.put(key, write_source(tmp_path, payload))
    try:
        assert b"".join(s3_store.stream(key)) == payload
    finally:
        s3_store.delete(key)
    with pytest.raises(ObjectStoreError):
        list(s3_store.stream(key))


@s3_gated
def test_s3_missing_key_raises_loudly(s3_store):
    with pytest.raises(ObjectStoreError, match="download failed"):
        list(s3_store.stream("tenants/default/files/fl_does_not_exist"))


@pytest.mark.skipif(
    not MANAGED_S3_BUCKET,
    reason="INQTRIX_TEST_S3_MANAGED_BUCKET not set (managed S3 smoke)",
)
def test_managed_s3_default_chain_existing_bucket_roundtrip(tmp_path) -> None:
    """Optional smoke for workload/default credentials and existing buckets."""
    endpoint = os.environ.get("INQTRIX_TEST_S3_MANAGED_ENDPOINT_URL", "").strip()
    store = S3ObjectStore(
        endpoint_url=endpoint or None,
        bucket=MANAGED_S3_BUCKET,
        region=os.environ.get("INQTRIX_TEST_S3_MANAGED_REGION", "us-east-1"),
        addressing_style="auto",
        bucket_provisioning="existing",
    )
    key = f"tenants/default/files/managed-smoke-{uuid.uuid4().hex}"
    payload = b"inqtrix-managed-s3-smoke"

    store.put(key, write_source(tmp_path, payload))
    try:
        assert b"".join(store.stream(key)) == payload
    finally:
        store.delete(key)


# ------------------------------------------------------------------ #
# S3 offline (botocore Stubber — no network)
# ------------------------------------------------------------------ #


def make_stubbed_store(
    *, bucket_provisioning: S3BucketProvisioning = "create_if_missing"
) -> tuple[S3ObjectStore, Any]:
    from botocore.stub import Stubber

    store = S3ObjectStore(
        endpoint_url="http://stub.invalid",
        bucket="inqtrix-files",
        access_key="stub-access",
        secret_key="stub-secret",
        bucket_provisioning=bucket_provisioning,
    )
    client = store._s3()
    store._probe_client = client
    stubber = Stubber(client)
    return store, stubber


def test_s3_default_chain_omits_endpoint_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3

    captured: dict[str, object] = {}
    client = object()

    def capture_client(service_name: str, **kwargs: object) -> object:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return client

    monkeypatch.setattr(boto3, "client", capture_client)

    store = S3ObjectStore(
        bucket="managed-bucket",
        region="eu-central-1",
        addressing_style="auto",
        bucket_provisioning="existing",
    )

    assert store._s3() is client
    assert captured["service_name"] == "s3"
    assert captured["region_name"] == "eu-central-1"
    assert "endpoint_url" not in captured
    assert "aws_access_key_id" not in captured
    assert "aws_secret_access_key" not in captured
    assert "aws_session_token" not in captured
    assert "verify" not in captured

    config = captured["config"]
    assert config.s3 == {"addressing_style": "auto"}
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": 4,
    }
    assert config.tcp_keepalive is True


def test_s3_static_temporary_credentials_endpoint_and_ca_are_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import boto3

    captured: dict[str, object] = {}
    client = object()

    def capture_client(service_name: str, **kwargs: object) -> object:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return client

    monkeypatch.setattr(boto3, "client", capture_client)
    ca_bundle = tmp_path / "private-ca.pem"

    store = S3ObjectStore(
        endpoint_url="https://s3.internal.example",
        bucket="managed-bucket",
        access_key="temporary-access",
        secret_key="temporary-secret",
        session_token="temporary-token",
        region="eu-west-1",
        addressing_style="virtual",
        bucket_provisioning="existing",
        ca_bundle=ca_bundle,
    )

    assert store._s3() is client
    assert captured["endpoint_url"] == "https://s3.internal.example"
    assert captured["aws_access_key_id"] == "temporary-access"
    assert captured["aws_secret_access_key"] == "temporary-secret"
    assert captured["aws_session_token"] == "temporary-token"
    assert captured["verify"] == str(ca_bundle)
    assert captured["config"].s3 == {"addressing_style": "virtual"}


def test_s3_probe_client_has_bounded_io_and_no_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3

    captured: dict[str, object] = {}

    def capture_client(service_name: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", capture_client)
    store = S3ObjectStore(
        bucket="managed-bucket",
        access_key="access",
        secret_key="secret",
    )

    store._s3(probe=True)

    config = captured["config"]
    assert config.connect_timeout == 0.75
    assert config.read_timeout == 0.75
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": 1,
    }


def test_s3_stream_raises_eagerly_on_missing_key_offline():
    store, stubber = make_stubbed_store()
    stubber.add_client_error(
        "get_object", service_error_code="NoSuchKey", http_status_code=404
    )
    with stubber:
        # The error must surface at stream() call time, BEFORE any
        # iteration — that is what keeps the stable HTTP 503 path alive.
        with pytest.raises(ObjectStoreError, match="download failed"):
            store.stream("tenants/default/files/fl_missing")


def test_s3_ensure_bucket_creates_only_on_definite_404_offline(tmp_path):
    store, stubber = make_stubbed_store()
    stubber.add_client_error(
        "head_bucket", service_error_code="404", http_status_code=404
    )
    stubber.add_response("create_bucket", {}, {"Bucket": "inqtrix-files"})
    with stubber:
        store.ensure_bucket()
    assert store._bucket_ensured is True

    # A non-404 head failure (auth, network) must re-raise, never
    # silently fall through to create_bucket.
    failing_store, failing_stubber = make_stubbed_store()
    failing_stubber.add_client_error(
        "head_bucket", service_error_code="403", http_status_code=403
    )
    with failing_stubber:
        with pytest.raises(ObjectStoreError, match="head_bucket failed"):
            failing_store.ensure_bucket()
    assert failing_store._bucket_ensured is False


def test_s3_bucket_already_exists_requires_access_confirmation() -> None:
    store, stubber = make_stubbed_store()
    stubber.add_client_error(
        "head_bucket", service_error_code="404", http_status_code=404
    )
    stubber.add_client_error(
        "create_bucket",
        service_error_code="BucketAlreadyExists",
        http_status_code=409,
        expected_params={"Bucket": "inqtrix-files"},
    )
    stubber.add_client_error(
        "head_bucket", service_error_code="403", http_status_code=403
    )

    with stubber:
        with pytest.raises(ObjectStoreError, match="ownership confirmation"):
            store.ensure_bucket()

    assert store._bucket_ensured is False


def test_s3_existing_bucket_mode_never_attempts_creation_offline() -> None:
    store, stubber = make_stubbed_store(bucket_provisioning="existing")
    stubber.add_client_error(
        "head_bucket", service_error_code="404", http_status_code=404
    )

    with stubber:
        with pytest.raises(ObjectStoreError, match="head_bucket failed"):
            store.ensure_bucket()

    assert store._bucket_ensured is False


@pytest.mark.parametrize(
    ("status", "bucket_provisioning", "expected"),
    [
        (404, "create_if_missing", True),
        (404, "existing", False),
        (403, "create_if_missing", False),
        (403, "existing", False),
    ],
)
def test_s3_availability_respects_bucket_provisioning_mode(
    status: int,
    bucket_provisioning: S3BucketProvisioning,
    expected: bool,
) -> None:
    store, stubber = make_stubbed_store(
        bucket_provisioning=bucket_provisioning
    )
    stubber.add_client_error(
        "head_bucket",
        service_error_code=str(status),
        http_status_code=status,
    )

    with stubber:
        assert store.is_available() is expected


@pytest.mark.parametrize(
    ("encryption", "kms_key_id", "expected_kwargs"),
    [
        ("none", None, {}),
        (
            "AES256",
            None,
            {"ExtraArgs": {"ServerSideEncryption": "AES256"}},
        ),
        (
            "aws:kms",
            "arn:aws:kms:eu-central-1:123456789012:key/test",
            {
                "ExtraArgs": {
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": (
                        "arn:aws:kms:eu-central-1:123456789012:key/test"
                    ),
                }
            },
        ),
    ],
)
def test_s3_upload_uses_only_configured_encryption_headers(
    tmp_path: Path,
    encryption: S3ServerSideEncryption,
    kms_key_id: str | None,
    expected_kwargs: dict[str, object],
) -> None:
    store = S3ObjectStore(
        endpoint_url="http://stub.invalid",
        bucket="inqtrix-files",
        access_key="stub-access",
        secret_key="stub-secret",
        server_side_encryption=encryption,
        kms_key_id=kms_key_id,
    )
    client = Mock()
    store._client = client
    store._bucket_ensured = True
    source = write_source(tmp_path, b"encrypted-blob")

    store.put("tenants/default/files/fl_1", source)

    client.upload_file.assert_called_once_with(
        str(source),
        "inqtrix-files",
        "tenants/default/files/fl_1",
        **expected_kwargs,
    )


def test_s3_operation_errors_redact_credentials_and_preserve_cause(
    tmp_path: Path,
) -> None:
    store = S3ObjectStore(bucket="inqtrix-files")
    client = Mock()
    cause = RuntimeError(
        "request to https://user:raw-password@s3.internal.example failed "
        "aws_session_token=raw-session-token"
    )
    client.upload_file.side_effect = cause
    store._client = client
    store._bucket_ensured = True

    with pytest.raises(ObjectStoreError, match="S3 upload failed") as raised:
        store.put("tenants/default/files/fl_1", write_source(tmp_path, b"x"))

    message = str(raised.value)
    assert "raw-password" not in message
    assert "raw-session-token" not in message
    assert "[REDACTED]" in message
    assert raised.value.__cause__ is cause


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"addressing_style": "unsupported"}, "addressing style"),
        ({"bucket_provisioning": "unsupported"}, "provisioning mode"),
        ({"server_side_encryption": "unsupported"}, "encryption mode"),
        ({"kms_key_id": "key-without-kms"}, "KMS key"),
        ({"access_key": "access-only"}, "both access_key and secret_key"),
        ({"secret_key": "secret-only"}, "both access_key and secret_key"),
        ({"session_token": "token-only"}, "requires an explicit"),
    ],
)
def test_s3_rejects_unsupported_constructor_combinations(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        S3ObjectStore(bucket="inqtrix-files", **kwargs)


def test_s3_delete_failure_raises_loudly_offline():
    store, stubber = make_stubbed_store()
    stubber.add_client_error(
        "delete_object", service_error_code="InternalError", http_status_code=500
    )
    with stubber:
        with pytest.raises(ObjectStoreError, match="delete failed"):
            store.delete("tenants/default/files/fl_1")
