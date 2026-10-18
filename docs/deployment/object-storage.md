# Object storage

## Scope

Inqtrix stores file metadata and authorization facts in PostgreSQL; the object
store holds only opaque file bytes under tenant-prefixed keys. The `s3` backend
supports native AWS S3 and S3-compatible APIs such as MinIO, SeaweedFS, R2,
Ceph, NooBaa and Nutanix Objects. It does not add Azure Blob or Google Cloud
Storage adapters.

## Choose one authentication model

The dotenv blocks below are complete target states. When starting from
`deploy/.env.stack.example`, edit or replace the existing canonical assignment
for each key; never paste a second assignment at the end of the file.

### Static or temporary credentials

Use this for bundled SeaweedFS/MinIO and providers that issue S3 keys —
including on-premise S3-compatible stores such as Nutanix Objects. Put the
topology in the visible stack config:

```dotenv
INQTRIX_OBJECT_STORE_BACKEND=s3
INQTRIX_S3_AUTH_MODE=static
INQTRIX_S3_ENDPOINT_URL=https://s3.example.com
INQTRIX_S3_BUCKET=inqtrix-files
INQTRIX_S3_REGION=us-east-1
INQTRIX_S3_ADDRESSING_STYLE=path
INQTRIX_S3_BUCKET_PROVISIONING=existing
```

Keep credentials in the selected stack secret file:

```dotenv
INQTRIX_S3_ACCESS_KEY=...
INQTRIX_S3_SECRET_KEY=...
# INQTRIX_S3_SESSION_TOKEN=...  # optional STS token
```

For the bundled `--profile s3`, Compose maps that pair to SeaweedFS'
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` contract. No generated JSON
credential file exists. The service refuses to start when either value is
missing, so it cannot silently run anonymously.

`deploy/.env.stack.example` already carries the bundled values, including
`http://seaweedfs:8333` and `create_if_missing` instead of `existing`.

### SDK default credential chain

Use this for EKS/ROSA workload identity, container credentials or instance
roles. Inqtrix passes no access, secret or session-token arguments to boto3, so
the SDK can select and refresh its standard provider:

```dotenv
INQTRIX_OBJECT_STORE_BACKEND=s3
INQTRIX_S3_AUTH_MODE=default
INQTRIX_S3_ENDPOINT_URL=
INQTRIX_S3_BUCKET=inqtrix-files
INQTRIX_S3_REGION=eu-central-1
INQTRIX_S3_ADDRESSING_STYLE=auto
INQTRIX_S3_BUCKET_PROVISIONING=existing
```

Do not set the Inqtrix static-key variables in `default` mode. The process
fails during configuration instead of letting stale keys shadow workload
identity. The provider order and web-identity/container refresh behavior are
defined by the [boto3 credential provider chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html).

## Endpoint, addressing and bucket lifecycle

`INQTRIX_S3_ENDPOINT_URL` is optional. Leave it empty for AWS-native endpoint
resolution; specify it for an S3-compatible service. Addressing may be `path`,
`auto` or `virtual`. `path` remains the compatibility default for bundled and
self-hosted stores; `auto` is recommended for native AWS.

`INQTRIX_S3_BUCKET_PROVISIONING` is an infrastructure contract:

- `existing` never calls `CreateBucket`. A missing bucket or a 403/404 probe is
  unavailable. Use this for production managed storage.
- `create_if_missing` creates a bucket only after a definite 404 and preserves
  the bundled SeaweedFS/MinIO behavior. The API/worker object-store identity
  must have `s3:CreateBucket` only in this mode.

Inqtrix never sets object ACLs, so buckets with S3 Object Ownership / Bucket
Owner Enforced remain supported.

## Minimum permissions

Scope permissions to the configured bucket and, where the provider supports
it, the `tenants/*` object prefix. The application needs:

- bucket probe/list permission (`s3:ListBucket`, used by `HeadBucket`);
- `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` for file lifecycle;
- `s3:AbortMultipartUpload` for failed multipart transfers;
- `s3:CreateBucket` only with `create_if_missing`.

AWS maps the exact API operations to IAM actions in its [S3 policy-action
reference](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html).
Do not grant public bucket access or broad account-level S3 administration.

## TLS and encryption

For a private S3-compatible CA, mount a readable PEM file and set
`INQTRIX_S3_CA_BUNDLE`. There is intentionally no switch that disables TLS
verification.

Uploads can request:

| Setting | Result |
|---|---|
| `INQTRIX_S3_SERVER_SIDE_ENCRYPTION=none` | No request header; a bucket-default encryption policy can still encrypt objects. |
| `AES256` | SSE-S3 upload header. |
| `aws:kms` | SSE-KMS upload header; optionally set `INQTRIX_S3_KMS_KEY_ID`. |

A KMS key id is rejected unless the mode is `aws:kms`. On AWS, uploads need
`kms:GenerateDataKey`, downloads need `kms:Decrypt`, and multipart uploads may
need both; scope the KMS key policy to the workload role. See [AWS SSE-KMS
permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html#sse-kms-permissions).

## Helm

Non-secret S3 settings belong in `config`; access/secret/session-token values
belong in the app Secret. Workload identity is configured independently for API
and worker:

```yaml
config:
  INQTRIX_OBJECT_STORE_BACKEND: s3
  INQTRIX_S3_AUTH_MODE: default
  INQTRIX_S3_BUCKET: inqtrix-files
  INQTRIX_S3_REGION: eu-central-1
  INQTRIX_S3_ADDRESSING_STYLE: auto
  INQTRIX_S3_BUCKET_PROVISIONING: existing

serviceAccount:
  api:
    create: true
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/inqtrix-s3
    automountServiceAccountToken: true
  worker:
    create: true
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/inqtrix-s3
    automountServiceAccountToken: true
```

Web, Collaboration, migrations and Helm tests do not receive
the S3 Secret or cloud-identity ServiceAccount. API and worker may share the
same least-privilege S3 role.

An external S3-compatible store with static keys (self-hosted MinIO/Ceph or
on-premise Nutanix Objects) combines `config` for the non-secret settings, the
app Secret for the key pair, and `s3.caBundle` for a private CA — with
`s3.enabled` left `false` (no bundled MinIO):

```yaml
config:
  INQTRIX_OBJECT_STORE_BACKEND: s3
  INQTRIX_S3_AUTH_MODE: static
  INQTRIX_S3_ENDPOINT_URL: https://objects.example.internal
  INQTRIX_S3_BUCKET: inqtrix-files
  INQTRIX_S3_REGION: us-east-1
  INQTRIX_S3_ADDRESSING_STYLE: path
  INQTRIX_S3_BUCKET_PROVISIONING: existing

# INQTRIX_S3_ACCESS_KEY / INQTRIX_S3_SECRET_KEY go into the app Secret.

s3:
  enabled: false
  caBundle:
    existingConfigMap: inqtrix-object-store-ca
    key: ca.crt
```

For private PKI, reference a ConfigMap under `s3.caBundle`; the chart mounts the
selected key only into API/worker and derives `INQTRIX_S3_CA_BUNDLE` from the
mount path. `s3.caBundle` works independently of `s3.enabled`.

In Compose, set `INQTRIX_S3_CA_BUNDLE_HOST` to the absolute host path and keep
`INQTRIX_S3_CA_BUNDLE=/etc/inqtrix/object-store/ca.pem`. Only API and worker
receive that read-only mount; no container is given a TLS-disable escape hatch.

Bring-your-own manifests (custom charts/templates) must reproduce two chart
invariants: the `INQTRIX_S3_*` environment belongs on the API and worker
containers only (never web, Collaboration or the migration Job), and the CA
bundle must be mounted as a single read-only FILE (ConfigMap key via
`subPath`) with `INQTRIX_S3_CA_BUNDLE` pointing at that file path — not at a
directory.

## Availability behavior

`/readyz` reports `checks.object_store`. A temporary object-store failure makes
the API `degraded` with HTTP 200 and removes the effective files capability;
unrelated runs and administration remain reachable. File operations fail with
HTTP 503 and the stable code `object_store_unavailable`; raw SDK messages and
credentials are sanitized.

The readiness probe uses bounded sub-second connection and read timeouts, one
attempt, and a single shared in-flight probe. Repeated readiness requests
therefore cannot create an unbounded thread or retry storm during a provider
outage. Normal uploads/downloads retain the SDK's standard retry policy.

Triage when `checks.object_store` reports unavailable: the runtime log line
"Runtime availability probe failed for object_store" only names the probe
bound (for example "timed out after 2.0s"), and the separate, rate-limited
warning "S3 availability probe failed (error_type=...)" adds only the
exception class of the failed probe (for example `EndpointConnectionError`) —
neither line carries the bucket name, the endpoint or the SDK message.
Typical causes for S3-compatible stores: the endpoint needs path-style
addressing (`INQTRIX_S3_ADDRESSING_STYLE=path`), a private CA is not mounted
(`INQTRIX_S3_CA_BUNDLE`), the identity lacks `s3:ListBucket` (HeadBucket
returns 403, see [Minimum permissions](#minimum-permissions)), or the
endpoint scheme/port is wrong.

File deletion removes the object before committing the metadata/quota delete.
If S3 is unavailable, the API returns the stable 503 while the metadata and
stored-byte accounting remain intact, so a retry converges. If the later
metadata delete fails, retrying is still safe because object deletion is
idempotent.

The Helm smoke test is stricter: when S3 is configured, `helm test` requires a
successful object-store probe. This separates load-balancer availability from
deployment acceptance.

Release qualification may additionally run the opt-in managed smoke in
`tests/test_object_store.py`. It writes and removes only a unique test object,
and never creates or deletes infrastructure; the `INQTRIX_TEST_S3_MANAGED_*`
variables are listed in [Settings and environment](../configuration/settings-and-env.md).

## Related docs

- [Kubernetes](kubernetes.md) — Workload identity, Secret
  isolation and CA mounts.
- [Security model](security-model.md) — Least-privilege deployment
  boundaries.
- [Platform components](../getting-started/platform-components.md) — Object-store
  topology and bundled services.
- [Settings and environment](../configuration/settings-and-env.md) — Complete S3
  variable reference.
