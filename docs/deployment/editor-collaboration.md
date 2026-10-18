# Deploy editor collaboration

## Scope

This page configures and operates the optional Node/Hocuspocus service, the
same-origin WebSocket gateway, PostgreSQL persistence, backup/restore,
retention, security limits, and the read-only kill switch. It covers the
bundled Compose and Helm paths plus host development. It does not describe
end-user review controls or claim horizontal availability or offline writes;
account-less guest links are a separate opt-in module documented in
[Settings and environment variables](../configuration/settings-and-env.md#editor-guest-links-editorguestlinksettings)
and are not covered here.

## Prerequisites

Collaboration is disabled by default. Enabling it requires all of the
following:

- `INQTRIX_STORAGE_BACKEND=postgres` migrated to the head this source tree
  ships. The head moves with the tree, so it is named in exactly one place —
  `SCHEMA_HEAD_REVISION` in `src/inqtrix/storage/migration_contract.py` — and
  readiness compares the installed revision against it. A number written into
  prose here would go stale with the next migration;
- cookie-session authentication: `INQTRIX_AUTH_MODE=local` or `oidc`;
- the canonical PostgreSQL session/user stores used by those auth modes;
- the private Node HTTP and WebSocket URLs;
- an independent `INQTRIX_COLLABORATION_SECRET` of at least 32 UTF-8 bytes,
  different from `INQTRIX_SESSION_SECRET`; and
- one and only one collaboration service replica.

When `INQTRIX_COLLABORATION_ENABLED=true`, an invalid storage/auth setup,
missing URL, weak/reused secret, or contradictory timing value stops startup.
The server does not silently disable the requested feature. With the flag left
false, the legacy editor and all non-collaboration features keep their normal
behavior.

## Network topology

Only the FastAPI gateway is browser-facing:

```text
Browser ws(s)://public-host/collaboration
  -> Vite development proxy or selected web adapter
     (the Python gateway unless another is explicitly selected)
  -> FastAPI /collaboration
  -> private ws://collaboration:1234/collaboration
```

FastAPI also calls private Node HTTP operations for conversion, projection,
suggestion publication, and decisions. Shared comments persist directly
through FastAPI and publish only content-free invalidation events to the
sidecar. Node calls FastAPI internal APIs for
lease introspection, state loading, durable updates, snapshots, policy events,
and compaction. Both directions use the collaboration bearer secret. Node has
no database URL, no host port, and no persistent volume.

The `/collaboration` prefix carries both the WebSocket upgrade and the HTTP
instance probe; a self-managed reverse proxy must pass both through.

The collaboration image installs its packages from the workspace lockfile at
image build time; no runtime invokes `npx` or downloads packages at startup.

## Configuration

The minimum host configuration is:

```dotenv
INQTRIX_STORAGE_BACKEND=postgres
INQTRIX_DATABASE_URL=postgresql+asyncpg://inqtrix:change-me@127.0.0.1:5432/inqtrix
INQTRIX_AUTH_MODE=local
INQTRIX_SESSION_SECRET=replace-with-an-independent-session-secret
INQTRIX_PAT_PEPPER=replace-with-an-independent-pat-pepper

INQTRIX_COLLABORATION_ENABLED=true
INQTRIX_COLLABORATION_HTTP_URL=http://127.0.0.1:1234
INQTRIX_COLLABORATION_WS_URL=ws://127.0.0.1:1234/collaboration
INQTRIX_COLLABORATION_SECRET=replace-with-at-least-32-random-characters
```

Start Node with a deliberately narrow environment containing only the private
API URL, collaboration secret, bind/port, and collaboration limits. Do not pass
the complete API `.env` into the Node process:

```bash
npm --workspace @inqtrix/collaboration-server run build
INQTRIX_API_INTERNAL_URL=http://127.0.0.1:5100 \
INQTRIX_COLLABORATION_SECRET="$INQTRIX_COLLABORATION_SECRET" \
INQTRIX_COLLABORATION_TENANT_ID=default \
npm --workspace @inqtrix/collaboration-server run start
```

The tenant value must match the deployment's canonical `Principal.tenant_id`.
Version 1 is intentionally single-tenant per deployment and therefore uses
`default` unless the whole deployment is configured for another canonical
tenant.

Every API and sidecar variable, default, and interaction is listed in
[Settings and environment variables](../configuration/settings-and-env.md#editor-collaboration-collaborationsettings).

## Compose

The `collaboration` profile adds only the private Node container. Set the
feature flag in the visible config and the independent collaboration secret in
the selected secret file, then start with the same ordered pair:

```bash
docker compose -f deploy/compose/compose.stack.yaml \
  --env-file deploy/.env.stack.secrets \
  --env-file deploy/.env.stack \
  --profile collaboration up -d --build
```

The matching `ps` and `logs` calls for the same ordered pair are in
[Runbooks](runbooks.md#status--logs).

The API service already receives the private URLs
`http://collaboration:1234` and
`ws://collaboration:1234/collaboration`; they are inert while the feature flag
is false. The Node container has a read-only root filesystem, drops Linux
capabilities, runs non-root, and publishes no host port.

The loopback stack defaults to HTTP. When a reverse proxy terminates TLS in
front of Compose, set both values in the stack environment and preserve the
public Host at that proxy:

```text
INQTRIX_PUBLIC_BASE_URL=https://desk.example
INQTRIX_EXTERNAL_SCHEME=https
```

The API consumes the full public origin; the web container consumes the scheme
and overwrites any client-supplied forwarding value. A mismatch fails
collaboration origin validation rather than falling back to HTTP.

Do not start the profile without `INQTRIX_COLLABORATION_ENABLED=true`, or set
the feature flag without starting Node. Both are visible misconfigurations:
the former starts an unused private service, while the latter reports the
configured capability as unavailable and collaboration operations fail.

## Helm

The chart defaults to `collaboration.enabled=false`. Build and publish
`inqtrix-collaboration` alongside the API and web images, store the bearer in a
Kubernetes Secret, and enable the component:

```yaml
image:
  collaboration:
    repository: registry.example.com/inqtrix-collaboration
    tag: "0.2.0"

collaboration:
  enabled: true
  secret:
    existingSecret: inqtrix-collaboration
    key: INQTRIX_COLLABORATION_SECRET
```

The Secret value must also be available to the API pod under the same key and
must differ from the session secret. Enabling the value renders a one-replica
Deployment with `Recreate`, a private ClusterIP Service, and no HPA, PDB, or
PVC. The chart derives the private HTTP/WS URLs and feature flag. An enabled
Ingress also derives `INQTRIX_PUBLIC_BASE_URL` and the web gateway's external scheme
from its host/TLS settings; an explicit `config.INQTRIX_PUBLIC_BASE_URL` keeps
precedence. Do not change the replica count: version 1 has a database fencing
lease for fail-safe replacement, not multi-replica fan-out. Size the service's
memory and CPU (`collaboration.resources`) with
[Collaboration service](kubernetes.md#collaboration-service); every change
restarts the single replica and briefly disconnects all editors.

## Readiness and verification

Node exposes private endpoints:

| Endpoint | Meaning |
|---|---|
| `GET /health/live` | Process is running. |
| `GET /health/ready` | Internal FastAPI is reachable and this instance holds an unexpired fencing lease. |
| `GET /metrics` | Sidecar Prometheus text metrics; keep private to the service network. |

The public capability response is the operator/user contract:

```bash
curl -s http://127.0.0.1:5100/v1/capabilities
```

Expect `collaboration.configured=true`,
`collaboration.service_available=true`, transport path `/collaboration`, and
mode `single_replica`. `configured=true` with `service_available=false` means
the API accepted its configuration but Node is not ready or reachable.

Release verification may also call the unauthenticated, content-free probe:

```bash
curl -s http://127.0.0.1:5100/collaboration/instance
```

A ready response has contract `inqtrix-collaboration-instance-v1`, service
`inqtrix-collaboration`, status `ready`, and the current non-empty
`instance_id` plus positive fencing `epoch`. FastAPI reads that identity from
PostgreSQL before and after checking the private Node readiness endpoint. A
missing, expired, changing, or unreachable data plane returns JSON status
`not_ready` with HTTP 503 and is never replaced by fixture/controller data.

## Backup and restore

There is no sidecar data volume to back up. A PostgreSQL backup contains all
collaboration document metadata, binary updates, verified snapshots, Markdown
projections, leases, attribution, and decision rows. Continue backing up the
object store for the rest of Inqtrix, but it is not the collaboration body
source of truth.

For a consistent collaboration backup, stop or drain the API and Node writer,
then take the ordinary PostgreSQL dump. Restore with matching application
binaries, schema/protocol versions, and migrations:

1. Stop API and collaboration processes.
2. Restore PostgreSQL using the normal [Runbooks](runbooks.md#restore)
   procedure.
3. Apply only migrations that belong to the restored application version.
4. Start FastAPI, then start exactly one Node instance.
5. Require Node readiness and `service_available=true`.
6. Open a document and verify that Node reconstructs it from the latest
   snapshot plus the remaining update tail.

Do not restore only `content_markdown` and attempt to recreate Yjs state. That
loses merge history and can duplicate content. A rollback across the
collaboration migration requires a matching pre-migration database backup and
old binaries; schema downgrade alone is not a safe recovery procedure.

## Retention and deletion

Snapshots are requested after 5 seconds of inactivity, 256 durable updates, or
a 1 MiB update tail by default. While writers are connected, each threshold
stores one snapshot; updates acknowledged while it is stored stay in the tail,
and a follow-up runs only when that tail reaches a threshold again. During
continuous typing a snapshot is also requested at least every 60 seconds. A
store request (inactivity, that 60-second bound, the last connection leaving a
room, an internal operation releasing it, a retry, or the service stopping)
waits for a snapshot that is already running and, when that snapshot captured
less than the tail persisted at the request, stores exactly one more, so the
stored state covers that tail before the room unloads or the service exits.

After a verified covering snapshot:

- binary update payloads may be pruned after 24 hours;
- attribution and decision metadata may be pruned after 90 days;
- only the latest two verified snapshots are retained; and
- tombstoned documents may be physically purged after 90 days.

Compaction is fenced and runs on an independent periodic timer in the active
Node instance, so retention does not depend on a document receiving another
edit or snapshot. Metadata retention is an operational history window, not a
complete version-restore system. Set longer periods before rollout when legal,
audit, or incident-response requirements demand them.

An eligible loaded room retries a failed snapshot autonomously with capped
exponential backoff. A snapshot answered with "document not found" (the
document was deleted or purged) is not retried: nothing is left to store, so
the room's remaining connections close with `access_revoked` and the room
unloads once it is empty.

## Kill switch and maintenance

Set `INQTRIX_COLLABORATION_ENABLED=false` and restart FastAPI to disable public
sessions, internal APIs, and the WebSocket gateway. Then stop Node. This is a
read-only kill switch, not a conversion or deletion:

- legacy Markdown documents continue to edit and autosave normally;
- collaboration documents retain their Yjs state in PostgreSQL;
- users can see only the last stored Markdown projection; and
- no client falls back to full-body Markdown writes or offline writes.

Re-enable only with the matching schema/protocol build and one healthy Node
instance. A planned Node restart closes sockets with `1012`; clients become
read-only and reconnect with capped exponential backoff. After repeated
failures the status surface exposes a manual reconnect that rotates the lease
and resynchronizes the room without a page reload.

## Security and limits

- Terminate TLS at the public proxy so browsers use `wss://`.
- Keep `INQTRIX_PUBLIC_BASE_URL`, the public proxy host, and its TLS mode
  aligned. FastAPI accepts forwarded same-origin only when browser Origin,
  sanitized forwarded proto/host, and that configured public origin all match.
- Keep Node and `/metrics` private; expose only the selected web adapter (the
  Python gateway, unless another is explicitly selected) and FastAPI's
  same-origin gateway.
- The public instance probe is deliberately unauthenticated and `no-store`; it
  exposes only fixed contract labels plus readiness, process instance ID, and
  fencing epoch. It contains no lease token, user, room, document, or content.
- Allow additional browser origins only through
  `INQTRIX_COLLABORATION_ALLOWED_ORIGINS`; CORS settings do not replace the
  WebSocket Origin check.
- Rotate the collaboration secret by restarting API and Node together. Never
  put it in a URL, ConfigMap, log, metric label, or frontend build.
- Frame, document, session, rate, queue, and backpressure limits all apply by
  default; see [Settings and environment variables](../configuration/settings-and-env.md#editor-collaboration-collaborationsettings)
  for the variables and their values, and [Editor collaboration architecture](../architecture/editor-collaboration.md#close-codes)
  for the close codes.
- Account disable, share revoke/downgrade, generation change, and session
  invalidation propagate through the existing `user_events` feed. A policy
  polling failure emits a warning and metric; current readiness reflects the
  instance fencing lease, so alert on that metric separately. Short lease
  rotation still rechecks live access and bounds stale authorization.
- A failed fencing-lease renewal (timeout, unreachable API, server error) does
  not end the lease: the service keeps its rooms open, retries every second,
  and closes all sockets with `4503 instance_lease_lost` only one second before
  the stored lease expires or as soon as the database reports the instance
  fenced (`409 instance_fenced`, on a renewal or on any other request). Every
  write transaction checks the lease and its epoch in the database, so a
  replacement instance never writes beside this one. A renewal does not wait
  for writes in flight; a replacement instance waits only for the old
  instance's writes that the API accepted before the lease expired.

The sidecar metric families and their label-cardinality guarantee are listed in
[Metrics](../observability/metrics.md#collaboration-service-metrics).

## Refused updates and stored content

The collaboration service refuses an update its schema or suggestion rules do
not admit and ends only the sending editor's session for that document; the
room and the other writers continue. The editor applies the same rules before
it sends and refuses such a change locally with a reason, so a refusal by the
service is the exception. When one happens, the writer sees a bar that keeps
the local state as a backup, offers it as a Markdown download, and reloads the
document from the service's state on request. The operator signals are
`inqtrix_collaboration_rejections_total` with
`reason="suggestion_policy_violation"` or `reason="invalid_schema"`, and the
`change_summary_failed` warning when the change summary refused an update.
Concurrent edits of the same place can still lead to such a refusal; see
[Known limitations](../architecture/editor-collaboration.md#known-limitations).

An update from a writer with edit access that the checks refuse only because
it met another writer's concurrent change is resolved in the writer's own row
where the service has a rule for the conflict, instead of being refused
([What the service accepts from a suggestion](../architecture/editor-collaboration.md#what-the-service-accepts-from-a-suggestion)).
`inqtrix_collaboration_edit_merge_normalized_updates_total` counts such
updates, and `inqtrix_collaboration_edit_merge_normalizations_declined_total`
the refusals that stood. Its `reason="forged_record"`, together with the
`edit_merge_foreign_record_refused` warning, marks an update that wrote a
suggestion in another person's name; an unmodified editor can send one when it
writes back a suggestion the room no longer remembers, for example by an undo
after the service loaded the room anew. Every pending suggestion an edit
removes is written to the audit log as
`editor.collaboration_suggestion.removed_by_edit`, naming the suggestion, its
patch, its author and the writer; the entry follows the audit log's own
retention (`INQTRIX_AUDIT_RETENTION_DAYS`), not the retention of the
collaboration metadata in [Retention and deletion](#retention-and-deletion).

### A stored suggestion that cannot be applied

The applicability rule for structure suggestions judges new updates; it does
not rewrite stored content. A document can therefore hold a structure
suggestion that can no longer be applied, for example a code-block proposal on
the paragraph of a list item, when that content was written without the rule
or by a modified client. Such a document stays open for live editing and keeps
accepting direct edits, but every new suggestion in it is refused for its
sender with `4409 invalid_schema`, because the change summary cannot project
the document; the `change_summary_failed` warning names the room. An owner or a
user with edit access clears the state by rejecting that suggestion in the
editor's list of open changes. Suggestions are accepted again afterwards.

### Validation rules and stored documents

Stored collaboration documents and snapshots carry the fingerprint of the
schema rules they were validated with. The fingerprint also names the exact
versions of the editor libraries (Tiptap, its Yjs binding and Yjs), so an
upgrade of one of them moves it too. When those rules or libraries change in
a way that keeps the meaning of every document the editor writes, a migration
re-stamps the stored fingerprint; without the re-stamp every stored document
would be refused on load. The collaboration service built from the same tree
and the migration therefore roll out together: a service of the previous
build refuses the re-stamped documents in the same way. A re-stamp does not make a document valid that the current
rules refuse. A document that holds a text alignment other than the offered
ones (`left`, `center`, `right`, `justify`) or an attribute named `__proto__`
can only come from a modified client; the service refuses to load it with
`invalid_schema`, so it cannot be opened for live editing.

## Unsupported deployment shapes

Version 1 does not support multiple Hocuspocus replicas, Redis/Valkey fan-out,
offline writes, or a direct browser-to-Node route. Account-less guest links are
not a collaboration deployment shape of their own: they are a separate opt-in
module (`INQTRIX_EDITOR_GUEST_LINKS_ENABLED`) that requires this collaboration
service, an independent token secret, Valkey, and an HTTPS public base URL, and
it is configured in
[Settings and environment variables](../configuration/settings-and-env.md#editor-guest-links-editorguestlinksettings).
The Redis extension would distribute live state but would not replace
PostgreSQL persistence. Do not add a second replica until a separately designed
HA mode defines fan-out, leader/fencing behavior, rolling schema compatibility,
and load-tested failure handling.

## Related docs

- [Editor collaboration architecture](../architecture/editor-collaboration.md)
- [Runbooks](runbooks.md)
- [Security model](security-model.md)
