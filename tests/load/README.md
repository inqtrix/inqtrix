# Collaboration load profiles

The load engine uses direct `ws` and Yjs protocol clients. It validates routed
rooms, authenticated access scope, complete Yjs sync, exact update hashes,
positive durable acknowledgement sequences, observer visibility, and
reconstruction.

The engine is not a separate quality claim. It is invoked through the common
verification orchestrator as `load-smoke`, `load-soak`, or `load-capacity`.

## Commands

The npm integration points are:

```text
npm run verify:load-smoke
npm run verify:load-soak
npm run verify:load-capacity -- --fixture /protected/load-fixture.json
npm run verify:tooling
```

The same profiles can be invoked directly for diagnostics:

```bash
node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON \
  --experimental-strip-types \
  tests/verification/cli.ts \
  --profile load-smoke

node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON \
  --experimental-strip-types \
  tests/verification/cli.ts \
  --profile load-soak \
  --container-engine podman

node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON \
  --experimental-strip-types \
  tests/verification/cli.ts \
  --profile load-capacity \
  --fixture /protected/load-fixture.json
```

Use `--preflight-only` to validate the local smoke credentials or external
capacity fixture without provisioning resources or opening sockets. Use the
orchestrator's `--list` command to inspect the scenario inventory.

## Load-smoke

The smoke engine defaults to:

- 20 sockets;
- 5 edit-capable writers;
- 3 non-writer observers;
- at least 1 second of sustained writes;
- at least 2 acknowledged rounds per writer;
- visible-update p95 below 250 ms;
- durable-ack p95 below 500 ms.

The public smoke profile requires `INQTRIX_E2E_ADMIN_EMAIL`,
`INQTRIX_E2E_ADMIN_PASSWORD`, and `INQTRIX_E2E_USER_PASSWORD`. Its base URL is
`INQTRIX_E2E_BASE_URL` or `http://127.0.0.1:8080`. It creates four
Run-ID-bound temporary users, one collaboration document, four accepted Edit
shares, and five leases per temporary identity through real product APIs. The
resulting 20-session fixture and its health probe are generated automatically;
passing `--fixture` is rejected.

The default HTTP base URL is only a local-development path. Set
`INQTRIX_OIDC_INSECURE_DEV_COOKIES=true` for that temporary plain-HTTP stack,
or use HTTPS and retain the hardened `Secure`/`__Host-` cookies. Never enable
the development cookie option in production.

The internal engine still supports an explicit API-probe opt-out for focused
protocol diagnostics, but the public profile adapter does not add weakening
flags.

## Load-soak

The local soak profile runs for 30 minutes with 25 distinct authenticated
identities: five Edit writers, five Suggest commenters, ten View readers, and
five View users that create staggered Research, Knowledge, Agent, and direct
Chat activity through the real product APIs. It creates 50 comment threads
with replies, long Unicode content, resolved threads, and explicit orphan
anchors while readers repeatedly reopen the document and comment listing.

Six fixed five-minute phases cover normal networking, 100 ms and 300 ms added
latency, 2 Mbit/s bandwidth, 1% packet loss, and final normalization. Shaping
is applied only inside the exact canonical collaboration container's Podman
network namespace. A loopback, bearer-protected, Run-ID-scoped controller
performs shaping and authenticated 60-second session reissue; it stores no
credential or lease token in the fixture or report.

The profile samples every canonical Compose service before each phase. It
refuses more load at 75% Podman-machine memory, 80% PostgreSQL connections, or
80% aggregate CPU per allocated vCPU. After a 30-second quiet period it fails
on an unplanned restart, more than 10% retained stack-memory growth, database
pressure, or Collaboration sockets that do not return to the baseline. The
five staggered feature activities are additionally capped at 5 USD and must be
fully priced by the usage ledger. `--container-engine podman` is mandatory;
the profile does not shape the host or other containers.

## Load-capacity

Capacity mode fixes the following values and rejects overrides:

- exactly 1,000 sockets and 100 edit-capable writers;
- exactly 20 non-writer observers;
- visible-update p95 strictly below 250 ms;
- durable-ACK p95 strictly below 500 ms;
- loaded API p95 degradation at or below 20% from baseline;
- at least 30 seconds of sustained writes;
- at least 10 durably acknowledged rounds from every writer;
- fresh 60-second lease reissue and scheduled rotation;
- ungraceful sidecar restart and exact reconstruction on fresh observers.

Capacity mode rejects insecure TLS, API-probe opt-out, shortened observation
windows, and capacity, duration, round, or latency overrides. The API probe must
use HTTPS `/health`; every WebSocket must use WSS on exact path
`/collaboration`; the instance probe must use HTTPS at
`/collaboration/instance` on the same public origin.

## Fixture contract

The following operator-provided fixture contract applies to `load-capacity`.
Generate the version-2 fixture immediately before a run from real
collaboration-session responses and do not commit it. Capacity requires
`api_probe`, `instance_probe`, `restart_control`, and `session_reissue`. On
POSIX systems the orchestrator rejects a fixture that is accessible by group or
other users; use `chmod 600 /protected/load-fixture.json`.

Minimal structural example:

```json
{
  "version": 2,
  "base_url": "https://app.example.test",
  "api_probe": {
    "contract": "inqtrix-health-v1",
    "url": "/health"
  },
  "instance_probe": {
    "contract": "inqtrix-collaboration-instance-v1",
    "url": "/collaboration/instance"
  },
  "restart_control": {
    "base_url": "https://control.example.test",
    "authorization_env": "INQTRIX_LOAD_RESTART_TOKEN",
    "restart_path": "/v1/test/collaboration/restart"
  },
  "session_reissue": {
    "authorization_env": "INQTRIX_LOAD_REISSUE_TOKEN",
    "contract": "inqtrix-collaboration-session-reissue-v1",
    "lease_ttl_seconds": 60,
    "url": "https://control.example.test/v1/test/collaboration/sessions/reissue"
  },
  "sessions": []
}
```

Each session is the collaboration-session API response plus a unique opaque
`reissue_id`. Required fields are `room`, `lease_token`, `expires_at`,
`refresh_after`, `websocket_path` or `websocket_url`, `access`,
`initial_write_mode`, `protocol_version`, `schema_version`, and `user.id`.

The health probe must return the `inqtrix-health-v1` FastAPI schema as JSON.
HTML, redirects, generic JSON, and malformed payloads fail. The instance probe
must return `inqtrix-collaboration-instance-v1`, `Cache-Control: no-store`, a
non-empty instance identity, and a positive epoch.

The restart controller must terminate the sidecar without graceful close
frames and return only after a replacement is ready. The runner independently
checks the public instance probe before and after restart; controller-provided
before/after identity claims are not accepted as evidence.

The session-reissue controller obtains fresh sessions through the authenticated
product API. It must not fabricate leases or accept permanent login
credentials. Changed room, user, access, schema, protocol, or WebSocket path;
reused tokens; invalid refresh timing; and stale leases fail.
Rotation concurrency consists of independent per-client pipelines: each fresh
session is validated and synchronized to its existing socket immediately,
without waiting for other clients in the same concurrency group.

## Cleanup and secret hygiene

The load engine closes every original and fresh-observer socket and stops the
rotation supervisor in `finally`. Its child process is also registered in the
common cleanup ledger, so interruption triggers termination and reverse-order
cleanup.

For generated `load-smoke` and `load-soak`, the parent additionally records the
temporary fixture, account sessions, temporary users, shares, documents, and
soak-owned product resources. The
fixture lives only below
`e2e/.results/verification/<run-id>/.cleanup-secrets/` with mode `0600` and is
removed after the child exits. The final report and cleanup labels contain
neither credentials, account identifiers, session data, lease tokens, nor the
fixture path.

The orchestrator report contains only scenario IDs, status, timing, exit
metadata, and cleanup state. It does not persist command arguments, fixture
paths, sessions, URLs, headers, document content, authorization values, lease
tokens, cookies, or environment dumps. Generated lease/session/token fixtures
under `tests/load/` remain ignored; the checked-in example is nonfunctional.

Before a successful child exit, the load engine atomically writes explicit
per-scenario results to the orchestrator-owned sidecar. Smoke reports protocol,
durability, and reconstruction separately; capacity reports latency, lease
rotation, and restart/reconstruction separately. The parent accepts only known,
unique scenario IDs with `passed` or `failed`. If the runner fails before a
scenario is reached, that missing result remains `not_run`; it is never inferred
as passed. Soak reports its identity matrix, comments/navigation, network
phases, durability, feature activity, and resource recovery independently.
