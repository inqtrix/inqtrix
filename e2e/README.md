# Verification profiles for browser and system testing

Browser, live-system, fault, collaboration-load, and web-edge checks share one
orchestrator:

```bash
node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON \
  --experimental-strip-types \
  tests/verification/cli.ts \
  --profile ui-fixture
```

External profiles do not start the product stack. When no external fixture is
supplied, `system-smoke` and `fault-injection` provision Run-ID-bound browser
accounts and documents against the already running active gateway;
`fault-injection` additionally starts a loopback-only controller for the exact
Compose services selected through `--container-engine`. The collaboration
service must have been explicitly recreated with
`INQTRIX_COLLABORATION_VERIFICATION_FAULTS=1`; the default is disabled and the
service must be recreated without that variable after the fault run.
`load-smoke` creates
four Run-ID-bound temporary
accounts, one collaboration document, shares, and 20 short-lived leases
through the public product APIs. `load-soak` creates 24 temporary accounts plus
the owner, runs a 30-minute mixed workload with scoped Podman network shaping,
and verifies real feature activity and resource recovery. The isolated
`edge-conformance` profile builds
both real web targets and starts only run-labelled synthetic local containers.

This harness supplies reusable release evidence; it is not by itself an
Enterprise-ready claim. A real Safari smoke run, production-scale external
capacity, supported network-shaping profiles, and any unavailable live
security/recovery matrix remain visible external gates.

## Profiles and engines

The scenario inventory in
[`tests/verification/scenario-inventory.ts`](../tests/verification/scenario-inventory.ts)
is the single source for profile membership and required Playwright tags.

| Profile | Engine adapters | Purpose |
| --- | --- | --- |
| `ui-fixture` | `ui-fixture-playwright` | Deterministic browser fixtures without an external product stack |
| `system-smoke` | `collaboration-playwright`, `editor-system-live` | Visible multiuser browser paths, role matrix, comments, navigation, and layout |
| `agent-desk` | `agent-desk-live` | Real research conversation: follow-ups, synthesis with web disabled, a subsequent Canvas report, tool activity, clickable citations, browser hygiene and scoped cleanup |
| `agent-recovery` | `agent-desk-live` | Scoped SDK faults, real model correction/projection, typed failures, cancellation, and restored runtime configuration |
| `fault-injection` | `collaboration-playwright` | Revocation, permission downgrade, lost acknowledgement, sidecar restart, outages, private anchors, and protocol rejection |
| `load-smoke` | `collaboration-load` | Run-ID-bound 20-socket/five-writer workload with automatic temporary-resource provisioning |
| `load-soak` | `collaboration-load` | Resource-guarded 30-minute, 25-identity mixed workload with scoped network phases and real product APIs |
| `load-capacity` | `collaboration-load` | Fixed high-capacity, latency, lease-rotation, restart, and reconstruction workload |
| `edge-conformance` | `web-edge-containers` | Black-box parity of the packaged Python and nginx web edges |

The six engines remain focused adapters. Profiles own selection, preflight,
run identity, reporting, and cleanup instead of each engine claiming an
independent completion status.

## npm integration contract

The root `package.json` exposes these stable integration points:

```text
npm run verify:list
npm run verify:tooling
npm run verify:ui-fixture
npm run verify:system-smoke
npm run verify:agent-desk
npm run verify:agent-recovery
npm run verify:fault-injection -- --container-engine podman
npm run verify:load-smoke
npm run verify:load-soak
npm run verify:load-capacity -- --fixture /protected/load-fixture.json
npm run verify:edge -- --container-engine podman
# or: npm run verify:edge -- --container-engine docker
```

Their `scripts` entries are:

```json
{
  "verify:orchestrator": "node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON --experimental-strip-types tests/verification/cli.ts",
  "verify:list": "npm run verify:orchestrator -- --list",
  "verify:tooling": "node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON --test --experimental-strip-types tests/verification/orchestrator.test.ts tests/verification/container-engine.test.ts tests/verification/repository-hygiene.test.ts tests/verification/fixtures/accounts.test.mjs tests/verification/fixtures/fault-control-server.test.mjs e2e/browser-observer.test.ts e2e/config.test.ts e2e/control.test.ts e2e/layout.test.ts e2e/protocol-session.test.ts e2e/scenario-reporter.test.ts e2e/transport-fingerprint.test.ts && node --test tests/load/collaboration-load.test.mjs && tsc -p e2e/tsconfig.json --noEmit",
  "verify:ui-fixture": "npm run verify:orchestrator -- --profile ui-fixture",
  "verify:system-smoke": "npm run verify:orchestrator -- --profile system-smoke",
  "verify:agent-desk": "npm run verify:orchestrator -- --profile agent-desk",
  "verify:fault-injection": "npm run verify:orchestrator -- --profile fault-injection",
  "verify:load-smoke": "npm run verify:orchestrator -- --profile load-smoke",
  "verify:load-soak": "npm run verify:orchestrator -- --profile load-soak --container-engine podman",
  "verify:load-capacity": "npm run verify:orchestrator -- --profile load-capacity",
  "verify:edge": "npm run verify:orchestrator -- --profile edge-conformance"
}
```

For diagnostics, the same orchestrator can be invoked directly:

```bash
node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON \
  --experimental-strip-types \
  tests/verification/cli.ts \
  --list

node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON \
  --experimental-strip-types \
  tests/verification/cli.ts \
  --profile system-smoke \
  --fixture /protected/e2e-fixture.json
```

Supported orchestrator controls are `--profile`, `--fixture`,
`--container-engine`, `--run-id`, `--preflight-only`, `--list`, and `--json`.
`--fixture` is rejected for `load-smoke` and `load-soak`; their private session
fixtures are generated inside the run directory and removed before completion.
`--container-engine` accepts only an explicit `podman` or `docker`; there is no
automatic selection or fallback. It is mandatory for generated
`fault-injection`, `load-soak`, and `edge-conformance`; load-soak specifically
requires Podman for namespace-scoped shaping. Playwright grep, project, reporter, worker,
and file overrides are intentionally not accepted by this entrypoint.

## Run lifecycle

The `agent-recovery` profile requires a healthy, idle canonical local Podman
stack and the normal protected tester credentials. Supply its original stack
environment-file paths through `INQTRIX_E2E_STACK_SECRETS_FILE` and
`INQTRIX_E2E_STACK_CONFIG_FILE`. These files are passed unchanged to Compose;
the verifier does not read their contents. The profile temporarily mounts a
Run-ID-scoped SDK fault fixture into the existing API and worker, and the
shared cleanup ledger restores their normal commands. Other requests continue
through the original SDK method. No alternative application stack is created.
The temporary seam requires an OpenAI-compatible `chat.completions` SDK;
the configured Azure stack uses this contract.
The conversation-originals scenario stores four visible turns, placing the
initial requirements beyond the history preview. Its final model is unmodified
and must choose the real search/read tools and recover the original values.
The bulk-handover scenario uses one explicitly enabled, synthetic search result
longer than 60,000 characters; the real answer model must see its late fact.
The fixture intercepts only that exact Run-ID-scoped synthetic search; all other
searches keep their configured provider. Together with the projection scenario,
these cases distinguish full transfers from budget-driven reductions.
The ordinary memo scenario carries no fault marker recognized by the SDK seam.
Set `INQTRIX_E2E_MANUAL_NORMAL_CASE=1` to submit that request through the in-app
browser: the same engine publishes its owned session and prompt in
`manual-normal-case.json`, waits for the matching native run, and registers it
in the existing cleanup ledger. Its memo needs both factual and visual review.
Set `INQTRIX_E2E_MANUAL_SOURCE_CASES=1` to submit the conversation-originals
and bulk-handover requests through that same in-app handover. Existing seeded
turns cannot satisfy the wait: it matches the current reviewed prompt marker.
Unexpected failures retain their owned session during the same bounded visual
review hold; `browser-failure.json` names the affected case and session for
live inspection before the common cleanup runs.
The manual submission wait polls once per second within the same bounded
deadline; a human review must not produce a continuous burst of history reads.
Runtime logs are retained before restoring the fixture containers so a
navigation failure can be correlated with the backend that actually served it.

Argument repair and context projection include real calls to the configured
model after fault injection. Other responses are explicitly synthetic protocol
fixtures: their reported tokens and calculated trace prices are not performance
or real-cost measurements. SDK call counts and request fingerprints are saved
alongside the ordinary report and screenshots.

Archive scenarios prepare captured text through the real store and direct the
first reads through the scoped SDK seam. The tail fact is absent from the first
window and from the request; only a later version-bound read makes it available
to the real answer generation. Catalogue continuation similarly reaches a source
beyond the first page. These scenarios establish storage and read integration,
not autonomous retrieval strategy quality. Inspect their actual tool content and
visible answers together before accepting them.

The cancellation case observes the native retry event, presses the visible
Stop control, and records monotonic time to terminal state and visible feedback.
Firefox and WebKit then read the same persisted recovery answer, typed failure,
and cancelled run through their own authenticated browser sessions. These are
cross-browser rendering checks, not additional provider fault executions.
The long failure message is also measured at an actual 390-pixel viewport in
Firefox and WebKit, with visible bounds and keyboard-operated step details.

The `agent-desk` profile uses real providers for an entire saved conversation:
three research questions, a corrected planning constraint followed by a synthesis
with web disabled, and a subsequent Canvas report from those earlier exchanges.
The first two questions ask for research in so many words and must start new
research. The third asks to check the current official guidance; it must
consult a source in that turn, either new research or a re-read of what the
conversation already researched (`read_evidence`/`read_context`), and both
follow-up answers must render at least one citation. An answer from the
model's memory alone meets neither.
Set `INQTRIX_E2E_MANUAL_CONVERSATION=1` to submit each question in the in-app
browser. The engine publishes its session, exact prompt and intended web control
state in `manual-conversation-turn.json` and registers the resulting native run
with the shared ledger. Each submission has a bounded five-minute wait.
`conversation-runs.json` and the per-turn event files retain the native run IDs,
artifact details and trace correlations for content review. Structural scenario
passes establish execution, source-tool use and persistence; they do not establish
factual correctness. Review the visible conversation and Canvas together with
the actual prompts, source results and writer output in Langfuse. In particular,
check that earlier conditions and evidence survive, and the corrected constraint
supersedes the original without changing unrelated facts.
Two further turns read an owned, indexed Knowledge document and a selected
Editor target. The originals are longer than ordinary text windows and contain
different final approvals. They are registered through the same resource ledger.
`document-originals.json` preserves the complete fictional material, identifiers
and hashes for same-run content review. The manual handover names the collection
or editor target to select through the native context menu. Questions contain no
round markers; the verifier keeps its own case identifiers outside the prompt.
Successful calls alone do not prove complete reading: inspect actual returned
ranges, subsequent model inputs and quoted content in the trace. The engine
additionally reads the run's published source coverage
(`snapshot.execution.source_coverage`) and requires the selected knowledge
original (R6) and editor working version (R7) to be read completely with no
open continuation, and the anchor search to have scanned the whole working
version. These are expectations of the two questions, not product rules.
An eighth turn then asks, without any document pin, for a verbatim passage of
the approved guide's archiving section that no published answer quotes (its
review steps and who names a deputy) and where in the conversation it came
from. Only the saved originals of the document turns hold that text (their
kernel transcripts and the archives of their offloaded tool results), and a
search excerpt is too short for a verbatim quote, so the engine requires that
this turn searched and read such an original (`search_context`/`read_context`
with a `transcript` or `archive` source of the knowledge or editor turn in the
published coverage) and opened no document again: the materials a run
transferred are part of the conversation, re-authorized against current
rights on every read. Both documents share the archiving section word for
word, so which of the two turns the answer names belongs to the content
review.
A question the published answers already cover would be answered from those
answers first (conversation originals precede transcripts in the search
order) and would not exercise this contract.
A ninth turn selects the collection again and asks for a page-by-page summary
of an uploaded PDF. The fixture brings that file in the way the product does:
default library section, upload reservation, bound multipart upload, durable
upload operation; the worker's canonical parse is the only producer of its
text, and knowledge ingestion reserves that text by `asset_id`. The fixture
requires the asset to name the parser revision that prepared it and the
knowledge document to carry the same extraction facts (three pages, page two
without text). The PDF's second page is a scan-like page without a text layer
and the pages do not number themselves, so only the extraction facts the
reader shows can reveal the gap. The engine requires the attachment to be read
completely; whether the answer names the unread page instead of inventing its
content is part of the content review. The library asset is a registered
resource of its own: its deletion removes the original file and the derived
knowledge document, and review retention keeps it with the conversation.
A tenth turn runs in the tier `tief` (the manual handover names the tier; in
the in-app browser it is chosen under "Tempo & Tiefe" in the run setup). Deep
ends with the review role — a bounded child run of the kernel under the work
profile `review`. The engine requires exactly one such child (`work_profile`,
origin key `role-review`), a stored typed verdict (`work_result`) with a
non-empty read basis that includes a document of the turn, no answer artifact
and no answer event on the role run, only profile tools in its tool events, one
`review_role` delegation row on the parent (and no role child where the
material fit one review call), an outcome record whose `review` is
`completed` with its `summary`, and no research in a turn whose documents are
both in the selected collection. The events of the role run are saved as
`events-R10-review-role.json`. Whether the verdict is right, what the role
re-read and what the revision changed belong to the content and trace review.
An eleventh turn runs on the mission engine with the permission "Auto" (the
manual handover names both; in the in-app browser they are chosen under
"Arbeitsweise" and "Berechtigung" in the run setup) and asks for a memo from
the selected collection without web. The mission shares the review pass with
the kernel. The engine requires an outcome record whose `review` is
`completed` with its `summary`, no `critic_unavailable` decision, a review role
child — `work_profile` `review`, origin key `role-review:<round>`, one
delegation row — for at most as many reviews as the record's `round` counts,
a stored `critic_report` of that round whose `transport` (`prompt` for the one
structured call, `role` for the review role) matches whether that round's role
child exists and whose verdict settles every planned success criterion by its
number, and a memo that cites only labels of its own references; an applied
revision must have raised the memo's revision and shown its findings. Whether the verdict is right and what an edit changed belong to
the content and trace review.

A turn that cannot be submitted or never ends stops the conversation. A
turn that completes but misses one of its expectations fails its scenario
(and with it the run) and is logged, while the later turns still run and are
judged on their own, so one early miss does not hide everything after it.

Firefox and WebKit then open the same persisted conversation and its
Canvas report through their own authenticated browser sessions. They verify the
revised planning facts and source-policy display without additional provider
calls. Review their saved screenshots together with the in-app live inspection.

Set `INQTRIX_E2E_RETAIN_REVIEW_DATA=1` for an explicitly requested handover of
the Agent-Desk conversation to its owner. Marked conversations, their runs and
associated review documents, knowledge collections or library assets remain
accessible after verification. Active runs still settle through the normal
cancellation path.
Authentication sessions, credentials, processes and unmarked technical fixtures
are cleaned normally. The cleanup ledger records `retained`, and the report
publishes its retained-resource count and status separately from `clean`.
The report schema is version 4. Without this opt-in, normal cleanup applies;
retention is not supported by the controlled recovery profile.

For in-app visual inspection before cleanup in either agent profile, set
`INQTRIX_E2E_VISUAL_REVIEW_SECONDS` to at most 600. Creating `visual-review.done`
inside that run's report directory releases the bounded hold early. It does not
mark any scenario as passed; UI content and traces still require review.

Every execution has a machine-readable `inqv-…` Run ID. The orchestrator:

1. validates the selected profile and scenario inventory;
2. runs every selected adapter preflight before starting any engine;
3. records child processes or exact run-labelled container resources
   immediately in a cleanup ledger;
4. executes adapters in the profile's declared order;
5. accepts product-resource registrations from a live child over a private IPC
   channel and acknowledges them only after the ledger has been flushed;
6. terminates registered processes on interruption or failure;
7. runs cleanup in reverse registration order;
8. writes the final report and cleanup state.

Reports live under:

```text
e2e/.results/verification/<run-id>/report.json
e2e/.results/verification/<run-id>/cleanup-ledger.json
```

Both files use mode `0600`. The report schema is version 4 and records the
Inqtrix version read from `src/inqtrix/__init__.py`, the profile, selected
engines, scenario records, source revision and dirty-state flag, explicit
status values, preflight results, adapter exit metadata, and cleanup state.
The CLI summary prints the same Inqtrix version. Scenario records use exactly:

- `passed`: the selected engine explicitly reported that scenario as passed;
- `failed`: that scenario ran and failed, or the engine failed before it could
  identify a more precise first failure;
- `blocked`: the owning engine failed preflight and never started;
- `not_run`: the profile selected the scenario, but execution never reached it;
- `not_applicable`: the scenario belongs to another profile.

An exit code of zero without complete explicit scenario results is converted to
an execution failure. Missing results are never inferred as passed, and
scenarios after an early failure remain `not_run`.

Reports never contain
environment dumps, command arguments, fixtures, browser storage state,
credentials, account emails, user/owner/recipient identifiers, cookies, CSRF
values, leases, or tokens. Cleanup-ledger labels describe only the resource
kind and current-run scope, never its product identifier. Sensitive keys,
bearer values, guest-link values, URL secrets, and sensitive environment values
are redacted before persistence. Child stdout and stderr also pass through the
same bounded line redactor before reaching the operator terminal.

Final statuses are unambiguous:

- `preflight_passed`: `--preflight-only` completed and no engine ran;
- `blocked`: a prerequisite failed before execution;
- `passed`: every selected adapter passed and cleanup completed;
- `failed`: an adapter failed;
- `cleanup_failed`: cleanup failed, regardless of the engine result;
- `interrupted`: the run received an interrupt or termination signal.

Exit codes are `0` for `passed`/`preflight_passed`, `2` for `blocked`, `3` for
`cleanup_failed`, `130` for `interrupted`, and `1` for execution failure.

## Web-edge container engine

The opt-in edge profile builds `web-python` and `web-nginx` from the same
`deploy/docker/Dockerfile.web`, starts a synthetic private backend and one
isolated network, and exercises both adapters through random loopback ports.
It covers SPA/static cache behavior, SSE, duplicate cookies, hop-by-hop header
removal, chunked body limits, binary WebSockets, backend recovery,
non-root/read-only execution, and normalized guest/share-link security and log
redaction.

Python removes dynamically nominated request hop fields. Stock nginx has no
generic directive for removing an arbitrary field named by `Connection`, so it
fails such malformed requests closed with HTTP 400; known protocol options
continue normally and fixed hop fields are stripped. The shared security
contract is that a nominated or fixed hop field never reaches the next hop.
An adapter may independently emit `Connection` for its own downstream
HTTP/1.1 link, so conformance distinguishes that from observable upstream
fields such as `Keep-Alive` and `Trailer`.
The trusted application upstream must not emit an arbitrary response field
nominated by `Connection`: Python removes such fields defensively, while stock
nginx can only remove the fixed protocol fields without an additional header
filter module.

No environment or secret file is read. Container commands use argv arrays and
their bounded output remains in memory; raw container logs are neither printed
nor persisted. Containers, network, and final run image tags are registered
before creation and removed in reverse order. A final label query requires
zero residual resources. Missing engine selection or an unreachable selected
engine is `blocked` with exit code 2, never a passing skip.

## UI fixture engine

`apps/research-desk/playwright.frontend.config.ts` runs the specs in
`apps/research-desk/browser-tests/`, among them
`editorCollaborationLifecycle.spec.ts` and `editorBodyChanges.spec.ts`, against
local Vite fixtures. It uses npm to resolve Vite and writes Playwright
artifacts beneath the current Run ID. It requires no application account or
external service. The collaboration specs (`editorCollaborationReconnect.spec.ts`
and `editorCollaborationSending.spec.ts`) start the real collaboration sidecar
inside the Playwright worker, with a store double behind it, and carry each
page's socket to it frame by frame, so they can assert which updates a page
sent, which the service acknowledged, and what the store holds. The page-exit
scenarios connect through a local relay instead, so the frames a page writes
while it unloads are the engine's own, and the page starts each reload itself
(`e2e/leave-question.ts`): Playwright's own reload in Firefox runs none of the
page's `beforeunload` handlers and keeps its sockets open until `pagehide`,
which a reload the page starts does not. Every scenario must pass in desktop
Chromium, Firefox, and Playwright WebKit unless the inventory declares it for
Chromium alone. The input method composition across a reconnect is such a
scenario: it is driven through the Chromium DevTools protocol, and the other
engines skip it.

## Collaboration Playwright engine

The external-stack scenarios live at
`e2e/scenarios/collaboration.system.spec.ts`. `playwright.config.ts` selects the
profile inventory and runs each selected scenario across all three configured
transports with:

- Chromium desktop and mobile;
- Firefox desktop;
- Playwright WebKit desktop.

The genuine IME scenario uses Chromium's CDP composition API and is declared
Chromium-only in the inventory. It is removed from Firefox/WebKit selection,
not skipped at runtime. Playwright WebKit is engine coverage and does not
replace the mandatory real-Safari smoke gate for a supported Safari release.

Without `--fixture`, `system-smoke` creates its own active-gateway fixture and
`fault-injection` does the same when an explicit container engine is selected.
The generated fault controller binds only to `127.0.0.1`, requires both an
ephemeral bearer and the exact Run ID, and can target only the run's declared
documents and the label-verified `web` and `collaboration` services of the
canonical Compose project. It exposes no product API. The sidecar installs its
deterministic fault-file and signal handling only when
`INQTRIX_COLLABORATION_VERIFICATION_FAULTS=1`; unset or `0` means no active
fault seam, and every other value is rejected at startup.

For explicit three-transport coverage, copy `e2e/fixture.example.json` outside
version control and pass its path with `--fixture` or
`INQTRIX_E2E_FIXTURE`. Storage-state files under `e2e/.auth/`
remain ignored and, on POSIX systems, must not be accessible by group or other
users (`chmod 600 …`).

All strict profiles require:

- distinct declared owner and collaborator UUIDs and browser storage-state
  files; the live session endpoint must confirm that both states resolve to
  those two different identities;
- disposable direct-edit, revocation, and suggestion documents;
- Vite, nginx, and Python-gateway URLs on distinct credential-free origins
  (`fixture.transports.python-gateway` or
  `INQTRIX_E2E_PYTHON_GATEWAY_BASE_URL`);
- observable transport identity at every endpoint.

`system-smoke` additionally requires the concurrent-edit and detached-transfer
documents.
`fault-injection` additionally requires the downgrade, reconciliation,
sidecar-outage, public-gateway-outage, protocol, and private-anchor documents
plus authenticated fixture controls. Generated fixtures create these resources,
seed each actor's private comment anchors, and generate the private AI proposal
through the visible UI with a deterministic response limited to that one
provider boundary.

Unavailable strict prerequisites are `blocked`; they are never converted into
skipped success. The scenario reporter also fails if a selected required
scenario is absent or skipped at runtime.

## Live editor system engine

`e2e/engines/editor-system-live.mjs` runs the account-level multiuser matrix. It
uses `INQTRIX_VERIFICATION_RUN_ID` for document IDs and artifact paths, opens
isolated browser contexts, and exercises sharing, permissions, suggestions,
comments, guest links when enabled, navigation, and responsive layout.

Required or optional environment variables:

```text
INQTRIX_E2E_ADMIN_EMAIL          required; no seed-account default
INQTRIX_E2E_TESTER_EMAIL         required; must identify a different account
INQTRIX_E2E_ADMIN_PASSWORD       required; no password default
INQTRIX_E2E_USER_PASSWORD        required; no fallback to the admin password
INQTRIX_E2E_BASE_URL             optional; defaults to http://127.0.0.1:8080
PLAYWRIGHT_EXECUTABLE_PATH       optional; Playwright resolves its browser otherwise
INQTRIX_E2E_REQUIRE_GUEST_LINKS  optional strict capability requirement
```

The engine closes pages and contexts, deletes run documents, erases temporary
users, logs out normal account sessions, and closes the browser in `finally`.
Account, document, share/link, and session lifecycle responsibilities live in
the shared modules under `tests/verification/fixtures/`; the engine file keeps
the scenario interactions.

Product cleanup is durable from the parent orchestrator's perspective:

- documents are registered before creation and are deleted by their owner;
- shares and guest links are registered immediately after creation and are
  cleaned by deleting their Run-ID-bound document;
- temporary-user emails are deterministically Run-ID-bound, and each user is
  erased after the run through `DELETE /v1/admin/users/{id}`; its cleanup entry
  completes only once the erasure finished, read without a workspace because an
  account erasure belongs to none;
- each authenticated browser context first registers an exact-session cleanup
  target with the parent and then writes its transient `0600` Playwright
  storage-state file under `.cleanup-secrets` before continuing.

## Local collaboration load smoke

`load-smoke` uses the same product-resource IPC and cleanup ledger as the live
system engine. It needs `INQTRIX_E2E_ADMIN_EMAIL`,
`INQTRIX_E2E_ADMIN_PASSWORD`, and `INQTRIX_E2E_USER_PASSWORD`;
`INQTRIX_E2E_BASE_URL` defaults to `http://127.0.0.1:8080`. The owner creates
four deterministic temporary users, shares one Run-ID-bound collaboration
document with Edit access, and issues exactly five leases per temporary
identity. The 20 sessions are ordered across identities so the first five
writer sockets do not all represent one user.

Use HTTPS for the normal hardened cookie contract. A deliberately plain-HTTP
loopback/LAN test must set `INQTRIX_OIDC_INSECURE_DEV_COOKIES=true` in that
temporary test environment; otherwise the login response can succeed while a
standalone API test client correctly refuses to replay the `Secure` cookie.
Never carry this development escape hatch into production.

The generated version-2 fixture contains only protocol fields required by the
load engine. It is atomically written with mode `0600` below the run's private
`.cleanup-secrets` directory, never copied into the report, and deleted after
the child exits. Accounts, sessions, shares, and the document are registered
before or immediately after creation, so parent cleanup still applies after
failure or interruption. `load-capacity` remains operator-provisioned because
its 1,000 sessions, HTTPS control plane, restart control, and lease-reissue
contract are environment-specific.

`load-soak` uses the same lifecycle with 25 distinct identities. Five writers
produce paced durable Yjs updates, five Suggest users create 50 mixed comment
threads, ten readers navigate repeatedly, and five users execute staggered
Research, Knowledge, Agent, and Chat activity. Six five-minute phases apply
normal, delayed, bandwidth-limited, lossy, and normalized network conditions
only to the selected collaboration container. Resource headroom is checked
before every phase; a post-quiet sample proves bounded memory, sockets,
database connections, and restart counts. Provider activity is limited to five
small workflows and a 5 USD usage-ledger budget.

On normal completion the engine performs these actions in `finally` and marks
the records complete. If the child fails, is interrupted, or is killed with
`SIGKILL`, the still-running parent replays the persisted ledger in reverse
order. There is no broad `ed_inqv-*` or `inqv-*@example.invalid` stale sweep,
because that could delete another concurrent verification run.
The harness deliberately exposes no automatic prior-run cleanup mode. If the
parent orchestrator itself is killed and leaves resources behind, an operator
must select that exact Run ID and clean it through the product's owner/admin
APIs; guessing by a shared prefix is not accepted.

Load behavior belongs exclusively to the two load profiles.

## Fault-control contract

Fault controls are authenticated `POST` operations whose URLs, response bodies,
and bearer values are never written to reports. They provide distinct
operations for lost durable acknowledgement, collaboration-sidecar outage,
public FastAPI gateway outage, status, restore, and sidecar restart.

The scenarios require observable effects rather than trusting controller
claims: browser close codes and decoded protocol state, unchanged projections
during outage, independent health behavior, exact-once reconciliation, and
successful recovery. A control response must expose its operation state and
fault layer without returning secrets.
