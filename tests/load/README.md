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
`INQTRIX_E2E_BASE_URL` or `http://localhost:8080`. It creates four
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

## Run load

`run-load` is a different kind of load: many signed-in users drive Research,
Knowledge, Agent and direct chat runs with the requests the surface sends,
against a running stack whose models are the provider stand-in from
`mock-providers/`. It costs no model money and makes no capacity claim by itself;
it produces numbers that can be compared between two states of the stack. It is a
profile of the common orchestrator, and it starts nothing: the stack's lifecycle
belongs to the caller.

```bash
INQTRIX_WEB_PORT=8081 \
podman compose -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.loadtest.yaml \
  -f deploy/compose/compose.loadtest-providers.yaml \
  -f tests/load/run-load/compose.run-load.yaml \
  --env-file <secrets-file> --env-file <config-file> \
  --profile knowledge --profile s3 --profile workers --profile collaboration \
  up -d --build

INQTRIX_E2E_BASE_URL=http://localhost:8081 \
INQTRIX_RUN_LOAD_STAGE=smoke \
npm run verify:run-load
```

| Input | Purpose |
| --- | --- |
| `INQTRIX_E2E_BASE_URL` | The stack's public origin. Its port must be published by the named Compose project |
| `INQTRIX_E2E_ADMIN_EMAIL`, `INQTRIX_E2E_ADMIN_PASSWORD` | The owner who creates the temporary accounts; created first on an empty stack |
| `INQTRIX_E2E_USER_PASSWORD` | The password of the temporary accounts |
| `INQTRIX_RUN_LOAD_STAGE` | `smoke`, `l05`, `l1` or `l2`: 3, 25, 50 or 100 active root runs |
| `INQTRIX_RUN_LOAD_BASELINE` | Optional: an earlier report to hold this run against |
| `INQTRIX_RUN_LOAD_FAULT` | Optional: `worker-stop` sends one worker SIGTERM half way through the plateau, kills it after 10 s and starts it again 30 s later. Its runs must reach another worker through heartbeat silence, none may be lost. Needs at least two workers and no base report |
| `INQTRIX_RUN_LOAD_STALL_MS` | Optional diagnosis of the API and the workers: name what holds their event loop for this long, in a log line per stall, instead of the 2 s of normal operation. No comparison: the finer probe costs time itself |
| `INQTRIX_RUN_LOAD_PROJECT` | Compose project, default `inqtrix-loadtest` |

Stages, the scenario mix, the questions, the think times and every other number
are assumptions of the capacity model. They live in
`run-load/scenarios.json` and nowhere else.

A comparison holds only between runs of the same setup, which the report records:
the scenario file, the stand-in's latency and selection, the replicas per service
and the limits the operator's files set. So that each process carries about the
same load, `l05` runs with one API and one worker, `l1` with two of each
(`up -d --scale api=2 --scale worker=2`) and `l2` with four of each.

The Python gateway shares the requests between the API replicas. The nginx edge
(`-f deploy/compose/compose.web-nginx.yaml`) gives the same numbers within the scatter
of the runs, and the report does not tell the two apart.

The engine first proves the target. The base URL must be this machine and its
port must belong to the named project; the API and the workers must be wired to
the stand-in, which a check inside each container answers with one digit and no
value; the stand-in must be healthy except for the messages `scenarios.json`
accepts; and every sampler must read. Without that proof nothing is created, and
if a run finishes later while the stand-in saw no call, the load stops. A wrong
target would otherwise send the whole load to paid providers.

Then it creates one temporary account per tab and signs each in with at most eight
at a time. A few accounts get a small collection from `tests/eval/golden/corpus`. A
rest phase measures the stack with all of them signed in and no page open. Then the
tabs load the page, keep the user stream open and read the lists the page reads:
the quota every 25 seconds, a desk switch about every three minutes, and what the
user stream asks to refresh. After an idle phase, the number of active root
runs rises linearly to the stage's target and stays there as a closed loop: when
a run ends, another user without an active run starts the next one after a short
think time, with the scenario drawn from the mix by a fixed seed. Direct chats run
beside it as an open stream and count as no root. A person answers gates after a
think time, and one gate in ten stays open for two minutes; meanwhile the stream
of a parked run is closed and its events are polled every 15 seconds, as on the
surface. After the plateau no run starts any more, the rest may finish within the
decay, and what is still active then is cancelled and counts as not finished.
Finally a few research runs are cancelled at once on a quiet stack while their
searches are in flight, to time how soon those provider calls are gone and whether
the client hung up on them.

The samplers read from outside the application and only ever counters: Postgres
through one long-lived read-only session, the cgroup of every container of the
project, the broker's command count, the stand-in's statistics, and the probes of
the API and the workers where `compose.run-load.yaml` started them. A value that
cannot be read stays `not_measured`, never zero. Monotonic counters are read at
the phase boundaries, levels on their interval. The samplers are part of what they
measure: every reading is a container exec or a query.

`run-load-report.json` in the run's report directory holds the rest, idle, ramp and
plateau values apart, the actual mix, transactions per provider call and per run net
of the idle rate, new connections per minute, connections, threads, anonymous
memory and CPU per container, slot occupancy and wait per lane, events written and
delivered per second, the delivery delay from an event's commit to its receipt, what
the streams lost or repeated, lock samples, the cancel time, the stand-in's coverage
and the probe values. The loop lag of a phase is the difference of two readings of a
fixed histogram, and a stall counts the wake-ups it swallowed, so a percentile is a
share of the time. `unit_costs` divides what differs between two phases by what
differs between them: a tab by comparing idle with rest, a running slot and an active
root by comparing the plateau with idle. `limits` shows the starting limits of the
capacity model next to the measured value; they are not judged until they are
confirmed. `load_matrix` names the axes of the load and where each number comes from.
The transactions per run are only reported: across three runs of the same code they
moved by 17 %, because the mix of short and long runs and what the window cuts off
move a count of runs far more than the code does, while the transactions per
provider call moved by about 3 %. A report holds counts, durations and route names;
one that holds a question, an address or a password is not written at all. The run
ends with one counted line:

```text
RUN-LOAD SUMMARY stage=smoke roots_started=... completed=... failed=... success_ratio=... tx_per_run_net=... tx_per_call_net=... conn_new_per_min=... sse_lag_p50_ms=... sse_lag_p95_ms=... residue=... unmatched_share=... synthesised_replies=... tool_calls_dropped=... cancelled=... unfinished=... events_lost=... events_duplicated=... rejected=... http_failures=... active_roots_median=... loop_lag_p99_ms=... latency_scale=...
```

How parallel the tools of a turn and the searches of a round really run cannot be
read from this load: the stand-in's delays are drawn, and the events of a turn are
written together whether its work ran side by side or not. `run-load/trace_timing.py`
reads it from the span timestamps of run trace exports of real runs.

| Scenario | Passes when |
| --- | --- |
| `run-load.provisioned` | The target is proven and every account, tab and collection exists |
| `run-load.workload-completed` | At least 90 % of the started root runs finished regularly; a plausibility check, no acceptance limit |
| `run-load.stand-in-covered` | At most 2 % of the provider calls had no recording to answer from |
| `run-load.events-complete` | Every run that reached its end received each of its events exactly once, at least one such run exists, and events arrived live and not only by polling |
| `run-load.load-applied` | The stage reached the stack: active roots near the target, every tab with its user stream, few rejections, failed requests and dropped streams, readable results, no early stop, and rest, idle and plateau no longer than planned; a defect that breaks a path would otherwise lower the load and improve every other number, and a host that sleeps through a phase leaves numbers that look fine |
| `run-load.report-written` | The report was written and read back |
| `run-load.no-residue` | No account of the run is listed any more, every table was read and no row of its accounts remains, and no cleanup step failed |
| `run-load.baseline-holds` | Only with `INQTRIX_RUN_LOAD_BASELINE`, otherwise `not_run` and missing from `--list` |

A base file is an earlier report, or `{"reports": [first, second, ...]}` to show how
far one state scatters by itself. Use several: four runs of the same code spread the
p95 delivery delay from 363 to 537 ms, and the first two of them lay only 4 % apart.
A report that was stopped early or has a red scenario is no base; being red only
against an older base does not count. The rules: more than 10 % more transactions
per provider call is red, counted net of the idle rate over the plateau, commits and
rollbacks together; more connections in a container of any application service than
the base had, plus the range across the base reports, is red; a median delivery
delay above the highest base by more than the tolerance is red, and inside the
tolerance a visible warning, the tolerance being the range across the base reports
and at least two histogram buckets; a p95 delivery delay above 1 second or an
event-loop lag p99 above 50 ms in the plateau is red; another stage, another setup
(scenario file, stand-in latency and selection, replicas) or a value that was not
measured is red as well. Against the base the p95 only warns, because in those four
runs it moved by half while the median moved by two buckets, and the same holds for
the p95 of submit, first event, lists and result.

The temporary accounts carry an address bound to the run by a digest. Before the
engine can create one, the adapter registers one ledger entry that erases exactly
those addresses, with at most eight at a time. The engine erases them itself after
a normal run and counts what remains through `/v1/admin/users`; the entry then
finds nothing. After an interruption the engine has four minutes to wind down, and
the entry does what is left. It tries a refused erasure again for up to two minutes,
because the product refuses while a run of the account is still stopping, and what
remains after that ends the run as `cleanup_failed`. The log inspection stays
`not_run` for this profile.
