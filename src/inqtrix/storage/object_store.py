"""Binary blob storage behind a Baukasten port (files feature).

The store holds opaque blobs under content-neutral keys
(``tenants/<tenant>/files/<uuid>``); every fact ABOUT a blob — owner,
filename, hash, permissions — lives in the file registry. The store is
never reachable by clients: Inqtrix checks permissions and streams the
bytes itself, so credentials and topology stay inside the backend.

Two implementations:

* :class:`LocalFSObjectStore` — directory tree on local disk, the
  zero-infrastructure default.
* :class:`S3ObjectStore` — AWS S3 or an S3-compatible endpoint via
  boto3. Path-style addressing and automatic bucket creation remain
  the compatibility defaults for the SeaweedFS development stack;
  managed services can instead use the SDK credential chain,
  provider-native endpoints, virtual-host addressing, and an
  externally provisioned bucket. boto3 is synchronous by design here
  — callers running on the event loop wrap calls in a thread (FastAPI
  does this for sync routes automatically).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import IO, Iterator, Literal

from inqtrix.urls import sanitize_log_message

log = logging.getLogger("inqtrix")

_STREAM_CHUNK_BYTES = 1024 * 1024

S3AddressingStyle = Literal["path", "auto", "virtual"]
S3BucketProvisioning = Literal["create_if_missing", "existing"]
S3ServerSideEncryption = Literal["none", "AES256", "aws:kms"]

_S3_ADDRESSING_STYLES = frozenset({"path", "auto", "virtual"})
_S3_BUCKET_PROVISIONING_MODES = frozenset(
    {"create_if_missing", "existing"}
)
_S3_SERVER_SIDE_ENCRYPTION_MODES = frozenset(
    {"none", "AES256", "aws:kms"}
)
_S3_TOTAL_MAX_ATTEMPTS = 4
_S3_PROBE_TIMEOUT_SECONDS = 0.75
_S3_PROBE_WARNING_INTERVAL_SECONDS = 60.0


class ObjectStoreError(RuntimeError):
    """Raised when a blob operation fails (missing key, backend down).

    Deliberately loud: a registry row whose blob cannot be served is
    an inconsistency operators must see, never an empty download.
    """


def _s3_operation_error(
    operation: str,
    *,
    target: str,
    cause: Exception,
) -> ObjectStoreError:
    """Build a diagnostic S3 error without leaking credential material."""
    safe_target = sanitize_log_message(target)
    safe_cause = sanitize_log_message(cause)
    return ObjectStoreError(
        f"S3 {operation} failed for {safe_target}: {safe_cause}"
    )


class ObjectStore(ABC):
    """Port for opaque blob storage keyed by caller-supplied strings."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether the backing blob service is reachable now.

        This is a read-only operator probe for health/capability surfaces.
        It must not create buckets, write sentinel objects, or expose
        endpoints/credentials; callers only receive a boolean.
        """

    @abstractmethod
    def put(self, key: str, source_path: Path) -> None:
        """Store the file at *source_path* under *key* (overwrite)."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether a complete object exists under *key*.

        Upload recovery uses this only with server-generated, operation-bound
        keys.  Implementations must not treat a partially written object as
        present and must distinguish absence (``False``) from an unavailable
        backing service (``ObjectStoreError``).  Treating every read failure as
        absence would turn an outage into a misleading request for re-upload.
        """

    @abstractmethod
    def stream(self, key: str) -> Iterator[bytes]:
        """Return a chunk iterator over the blob's bytes.

        Implementations open the blob EAGERLY — a missing key or an
        unreachable backend raises here, before any byte is handed
        out, so HTTP callers can still answer with a clean error
        status instead of aborting a started 200 response.

        Raises:
            ObjectStoreError: When the key does not exist or the
                backend is unreachable.
        """

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the blob. Missing keys are tolerated — the registry
        row is the source of truth and may outlive a crashed upload."""

    @abstractmethod
    def list_keys(self, prefix: str) -> Iterator[tuple[str, float]]:
        """Yield ``(key, last_modified_epoch)`` for blobs under *prefix*.

        Maintenance-only surface: the orphan sweep compares the physical
        inventory against the registry rows. Implementations yield
        page by page as the backend delivers entries and raise
        ``ObjectStoreError`` when the backend cannot be listed — an
        unreachable backend must never look like an empty store to a
        job that deletes based on the answer.
        """


def _iter_file(handle: IO[bytes]) -> Iterator[bytes]:
    """Yield chunks from an already-open handle, closing it at the end."""
    with handle:
        while chunk := handle.read(_STREAM_CHUNK_BYTES):
            yield chunk


def _iter_s3_body(body, *, key: str) -> Iterator[bytes]:
    """Yield S3 chunks with sanitized failures, then close the body."""
    try:
        while chunk := body.read(_STREAM_CHUNK_BYTES):
            yield chunk
    except Exception as exc:
        raise _s3_operation_error(
            "download stream", target=f"key {key!r}", cause=exc
        ) from exc
    finally:
        try:
            body.close()
        except Exception as exc:
            log.warning(
                "S3 response-body close failed (error_type=%s)",
                type(exc).__name__,
            )


class LocalFSObjectStore(ObjectStore):
    """Directory-tree blob store (zero-infrastructure default).

    Args:
        root: Base directory; keys become paths below it. Created on
            first write. Key segments are server-generated (tenant id,
            UUID), never client-controlled — path traversal is
            structurally impossible, but the guard in :meth:`_path`
            keeps that invariant explicit.
    """

    def __init__(self, *, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ObjectStoreError(f"object key escapes the store root: {key!r}")
        return path

    def is_available(self) -> bool:
        """Return whether the local directory backend is usable."""
        target = self._root
        while not target.exists() and target != target.parent:
            target = target.parent
        return target.is_dir() and os.access(target, os.W_OK | os.X_OK)

    def put(self, key: str, source_path: Path) -> None:
        """Copy *source_path* into the tree (atomic via temp + rename)."""
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(target.suffix + ".part")
        shutil.copyfile(source_path, staging)
        staging.replace(target)

    def exists(self, key: str) -> bool:
        """Return whether the atomically published local object exists."""

        return self._path(key).is_file()

    def stream(self, key: str) -> Iterator[bytes]:
        """Open the blob eagerly, then return the chunk iterator."""
        path = self._path(key)
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise ObjectStoreError(f"blob not found in local store: {key!r}") from exc
        return _iter_file(handle)

    def delete(self, key: str) -> None:
        """Remove the blob file; a missing file is a tolerated no-op."""
        self._path(key).unlink(missing_ok=True)

    def list_keys(self, prefix: str) -> Iterator[tuple[str, float]]:
        """Walk the local tree under *prefix* and yield key + mtime."""
        base = self._path(prefix)
        if not base.exists():
            return
        if not base.is_dir():
            raise ObjectStoreError(
                f"list prefix is not a directory: {prefix!r}"
            )
        root = self._root.resolve()
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix == ".part":
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                raise ObjectStoreError(
                    f"local store listing failed at {path.name!r}"
                ) from exc
            yield path.relative_to(root).as_posix(), stat.st_mtime


class S3ObjectStore(ObjectStore):
    """Blob store for AWS S3 and compatible services.

    Args:
        bucket: Bucket for every blob.
        endpoint_url: Optional service endpoint, e.g.
            ``http://127.0.0.1:8333``. ``None`` delegates endpoint
            resolution to boto3 for native AWS deployments.
        access_key: Optional explicit access key. When all credential
            arguments are ``None``, boto3 resolves credentials through
            its standard provider chain (for example workload identity).
        secret_key: Optional explicit secret key.
        session_token: Optional session token for temporary credentials.
        region: Region name boto3 requires; self-hosted stores ignore
            the value.
        addressing_style: Bucket addressing strategy passed to botocore.
            ``path`` preserves compatibility with the bundled development
            store; managed services may use ``auto`` or ``virtual``.
        bucket_provisioning: ``create_if_missing`` preserves the bundled
            store behavior. ``existing`` requires an operator-provisioned
            bucket and never attempts ``CreateBucket``.
        ca_bundle: Optional CA bundle used to verify private S3 endpoints.
        server_side_encryption: Optional upload encryption header. ``none``
            sends no encryption or ACL arguments.
        kms_key_id: Optional KMS key identifier, valid only with
            ``server_side_encryption="aws:kms"``.

    Raises:
        ValueError: When an enum-like option is unsupported, explicit
            credentials are incomplete, or a KMS key is supplied without KMS
            encryption.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        session_token: str | None = None,
        region: str = "us-east-1",
        addressing_style: S3AddressingStyle = "path",
        bucket_provisioning: S3BucketProvisioning = "create_if_missing",
        ca_bundle: str | Path | None = None,
        server_side_encryption: S3ServerSideEncryption = "none",
        kms_key_id: str | None = None,
    ) -> None:
        if (access_key is None) != (secret_key is None):
            raise ValueError(
                "explicit S3 credentials require both access_key and secret_key"
            )
        if session_token is not None and access_key is None:
            raise ValueError(
                "an S3 session_token requires an explicit access/secret key pair"
            )
        if addressing_style not in _S3_ADDRESSING_STYLES:
            raise ValueError(
                f"unsupported S3 addressing style: {addressing_style!r}"
            )
        if bucket_provisioning not in _S3_BUCKET_PROVISIONING_MODES:
            raise ValueError(
                "unsupported S3 bucket provisioning mode: "
                f"{bucket_provisioning!r}"
            )
        if server_side_encryption not in _S3_SERVER_SIDE_ENCRYPTION_MODES:
            raise ValueError(
                "unsupported S3 server-side encryption mode: "
                f"{server_side_encryption!r}"
            )
        if kms_key_id is not None and server_side_encryption != "aws:kms":
            raise ValueError(
                "an S3 KMS key requires server_side_encryption='aws:kms'"
            )
        self._endpoint_url = endpoint_url
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._session_token = session_token
        self._region = region
        self._addressing_style = addressing_style
        self._bucket_provisioning = bucket_provisioning
        self._ca_bundle = str(ca_bundle) if ca_bundle is not None else None
        self._server_side_encryption = server_side_encryption
        self._kms_key_id = kms_key_id
        self._client = None
        self._probe_client = None
        self._bucket_ensured = False
        self._bucket_lock = threading.Lock()
        self._probe_warning_lock = threading.Lock()
        self._last_probe_warning = 0.0

    def _client_kwargs(self, *, probe: bool) -> dict[str, object]:
        """Build one credential/endpoint bundle with operation-specific IO policy."""
        from botocore.config import Config

        config_kwargs: dict[str, object] = {
            "retries": {
                "mode": "standard",
                "total_max_attempts": 1 if probe else _S3_TOTAL_MAX_ATTEMPTS,
            },
            "s3": {"addressing_style": self._addressing_style},
            "tcp_keepalive": True,
        }
        if probe:
            config_kwargs.update(
                connect_timeout=_S3_PROBE_TIMEOUT_SECONDS,
                read_timeout=_S3_PROBE_TIMEOUT_SECONDS,
            )
        client_kwargs: dict[str, object] = {
            "region_name": self._region,
            "config": Config(**config_kwargs),
        }
        if self._endpoint_url is not None:
            client_kwargs["endpoint_url"] = self._endpoint_url
        if self._access_key is not None:
            client_kwargs["aws_access_key_id"] = self._access_key
        if self._secret_key is not None:
            client_kwargs["aws_secret_access_key"] = self._secret_key
        if self._session_token is not None:
            client_kwargs["aws_session_token"] = self._session_token
        if self._ca_bundle is not None:
            client_kwargs["verify"] = self._ca_bundle
        return client_kwargs

    def _s3(self, *, probe: bool = False):
        """Lazily build normal and bounded-probe boto3 clients."""
        attribute = "_probe_client" if probe else "_client"
        client = getattr(self, attribute)
        if client is None:
            import boto3

            client = boto3.client("s3", **self._client_kwargs(probe=probe))
            setattr(self, attribute, client)
        return client

    def _warn_probe_failure(self, cause: Exception) -> None:
        """Emit a sanitized, rate-limited operator diagnostic for S3 probes."""
        now = time.monotonic()
        with self._probe_warning_lock:
            if (
                now - self._last_probe_warning
                < _S3_PROBE_WARNING_INTERVAL_SECONDS
            ):
                return
            self._last_probe_warning = now
        log.warning(
            "S3 availability probe failed (error_type=%s)",
            type(cause).__name__,
        )

    def is_available(self) -> bool:
        """Return whether the S3 endpoint is reachable without mutating it.

        A definite 404 counts as reachable only in ``create_if_missing``
        mode, where the first upload may create the bucket. Existing-bucket
        mode, authorization failures, transport errors, and ambiguous errors
        all report unavailable without mutating the service.
        """
        from botocore.exceptions import ClientError

        try:
            self._s3(probe=True).head_bucket(Bucket=self._bucket)
            return True
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            available = (
                status == 404
                and self._bucket_provisioning == "create_if_missing"
            )
            if not available:
                self._warn_probe_failure(exc)
            return available
        except Exception as exc:
            self._warn_probe_failure(exc)
            return False

    def ensure_bucket(self) -> None:
        """Verify or create the configured bucket (thread-safe).

        Existing-bucket mode never attempts creation. In managed
        environments this preserves least-privilege policies that grant
        object access but deny ``CreateBucket``. In create-if-missing mode,
        only a definite 404 triggers creation; auth failures, network errors,
        and everything else re-raise. Concurrent first uploads are
        serialized and racing creators tolerate already-exists answers.
        """
        from botocore.exceptions import ClientError

        with self._bucket_lock:
            if self._bucket_ensured:
                return
            try:
                client = self._s3()
            except Exception as exc:
                raise _s3_operation_error(
                    "client initialization",
                    target=f"bucket {self._bucket!r}",
                    cause=exc,
                ) from exc
            try:
                client.head_bucket(Bucket=self._bucket)
                self._bucket_ensured = True
                return
            except ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get(
                    "HTTPStatusCode"
                )
                if (
                    status != 404
                    or self._bucket_provisioning == "existing"
                ):
                    raise _s3_operation_error(
                        "head_bucket",
                        target=f"bucket {self._bucket!r}",
                        cause=exc,
                    ) from exc
                log.warning(
                    "object store bucket missing; creating it",
                )
            except Exception as exc:
                raise _s3_operation_error(
                    "head_bucket",
                    target=f"bucket {self._bucket!r}",
                    cause=exc,
                ) from exc
            create_kwargs: dict = {"Bucket": self._bucket}
            if self._region and self._region != "us-east-1":
                create_kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": self._region
                }
            try:
                client.create_bucket(**create_kwargs)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code == "BucketAlreadyExists":
                    try:
                        client.head_bucket(Bucket=self._bucket)
                    except Exception as head_exc:
                        raise _s3_operation_error(
                            "create_bucket ownership confirmation",
                            target=f"bucket {self._bucket!r}",
                            cause=head_exc,
                        ) from head_exc
                elif code != "BucketAlreadyOwnedByYou":
                    raise _s3_operation_error(
                        "create_bucket",
                        target=f"bucket {self._bucket!r}",
                        cause=exc,
                    ) from exc
            except Exception as exc:
                raise _s3_operation_error(
                    "create_bucket",
                    target=f"bucket {self._bucket!r}",
                    cause=exc,
                ) from exc
            self._bucket_ensured = True

    def put(self, key: str, source_path: Path) -> None:
        """Upload the file (multipart handled by boto3 transfer).

        The bucket is ensured once per process before the first
        upload — startup stays network-free, the first write fails
        loudly when the store is unreachable.
        """
        if not self._bucket_ensured:
            self.ensure_bucket()
        try:
            upload_args: dict[str, str] = {}
            if self._server_side_encryption != "none":
                upload_args["ServerSideEncryption"] = (
                    self._server_side_encryption
                )
            if self._kms_key_id is not None:
                upload_args["SSEKMSKeyId"] = self._kms_key_id
            if upload_args:
                self._s3().upload_file(
                    str(source_path),
                    self._bucket,
                    key,
                    ExtraArgs=upload_args,
                )
            else:
                self._s3().upload_file(str(source_path), self._bucket, key)
        except ObjectStoreError:
            raise
        except Exception as exc:
            raise _s3_operation_error(
                "upload", target=f"key {key!r}", cause=exc
            ) from exc

    def exists(self, key: str) -> bool:
        """Probe one object without downloading its body."""

        try:
            self._s3().head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as exc:
            try:
                from botocore.exceptions import ClientError

                if isinstance(exc, ClientError) and str(
                    exc.response.get("Error", {}).get("Code", "")
                ) in {"404", "NoSuchKey", "NotFound"}:
                    return False
            except ImportError:  # pragma: no cover - constructor needs boto3
                pass
            raise _s3_operation_error(
                "head_object", target=f"key {key!r}", cause=exc
            ) from exc

    def stream(self, key: str) -> Iterator[bytes]:
        """Fetch the object eagerly, then return the body iterator."""
        try:
            response = self._s3().get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
        except Exception as exc:
            raise _s3_operation_error(
                "download", target=f"key {key!r}", cause=exc
            ) from exc
        return _iter_s3_body(body, key=key)

    def delete(self, key: str) -> None:
        """Delete the object (S3 delete is idempotent by contract)."""
        try:
            self._s3().delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise _s3_operation_error(
                "delete", target=f"key {key!r}", cause=exc
            ) from exc

    def list_keys(self, prefix: str) -> Iterator[tuple[str, float]]:
        """Stream the bucket inventory under *prefix* via pagination."""
        try:
            paginator = self._s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self._bucket, Prefix=prefix
            ):
                for entry in page.get("Contents", ()):
                    yield entry["Key"], entry["LastModified"].timestamp()
        except Exception as exc:
            raise _s3_operation_error(
                "list", target=f"prefix {prefix!r}", cause=exc
            ) from exc
