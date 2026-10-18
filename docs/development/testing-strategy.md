# Testing strategy

## Scope

This document explains the test layers that protect Inqtrix provider code and
cross-process product features, when each layer applies, and which release
gates remain manual. It covers offline unit/replay tests, PostgreSQL integration
tests, the editor collaboration matrix, and live qualification. It does not
replace the command-focused [Running tests](running-tests.md) page.

## Table of contents

- [The four layers](#the-four-layers)
- [Tool matrix per provider](#tool-matrix-per-provider)
- [Cassette layout and naming](#cassette-layout-and-naming)
- [Sanitization](#sanitization)
- [Markers and local gates](#markers-and-local-gates)
- [Recording new cassettes](#recording-new-cassettes)
- [Cookbook: adding a test for provider X](#cookbook-adding-a-test-for-provider-x)
- [Collaboration test matrix](#collaboration-test-matrix)
- [Known limitations](#known-limitations)

## The four layers

This diagram answers: "How much real runtime surface does each test layer
cover?" It is a testing pyramid: lower layers are faster and more isolated,
upper layers are closer to real providers and more expensive to maintain.

```mermaid
flowchart TD
    Unit["test layer: Unit<br/>MagicMock, fast"] --> Replay["test layer: Replay<br/>VCR + Stubber, offline"]
    Replay --> Smoke["asset layer: Parity<br/>tests/integration, inqtrix-parity CLI"]
    Smoke --> Live["manual layer: Live<br/>recording sessions"]
    Unit -. "path: tests/test_providers_*.py" .- Unit
    Replay -. "path: tests/replay/test_*_replay.py" .- Replay
```

Read the arrows as increasing integration depth, not as runtime execution
order. A provider change usually starts with unit tests and adds replay coverage
when the wire shape matters.

| Layer | Location | What it tests | When to add |
|---|---|---|---|
| **Unit** | `tests/test_providers_*.py` | Construction, parameter routing, response normalization, edge-case shapes (SSE strings, `model_dump` quirks). | Always — every new public method or branch deserves a unit test. |
| **Replay** | `tests/replay/test_*_replay.py` + `tests/fixtures/cassettes/<provider>/` (VCR) or `tests/fixtures/bedrock/` (Stubber JSON) | End-to-end provider behavior against realistic backend payloads with no network access. | When you change request/response wire shape, error mapping, or retry logic. |
| **Parity assets** | `tests/integration/` | Canonical parity questions and committed baselines (`baseline.json.migrated`, per-question `q*.json`). These are assets, not tests: `pytest tests/integration/` collects nothing, and the `inqtrix-parity` CLI is what reads them (`inqtrix-parity contract`). | When you change the agent loop or add a new provider permutation. |
| **Live** | Manual recording (`INQTRIX_RECORD_MODE=once`) | Real backend behavior for one-off cassette refreshes. | Whenever a backend's response shape changes upstream. |

There is **no automated live-provider layer in this provider pyramid**. Its
"live" layer is a maintainer-driven cassette-recording workflow (see
[Recording new cassettes](#recording-new-cassettes)). This is distinct from
the automated, production-like product verification profiles documented under
[Shared verification profiles](#shared-verification-profiles): those exercise
the deployed UI, API, sharing, and collaboration stack without calling real
third-party model/search providers.

## Tool matrix per provider

The HTTP transport dictates the mock library, not the provider's
purpose:

| Provider | Module | Transport | Replay tool |
|---|---|---|---|
| `LiteLLM` | `inqtrix.providers.litellm` | OpenAI SDK → httpx | vcrpy |
| `PerplexitySearch` | `inqtrix.providers.perplexity` | Perplexity SDK responses | unit stubs + debug dataflow script |
| `AzureOpenAILLM` | `inqtrix.providers.azure` | OpenAI SDK → httpx | vcrpy |
| `AzureFoundryWebSearch` | `inqtrix.providers.azure_web_search` | Azure AI Foundry / OpenAI SDK responses | unit stubs (`_parse_response` contract) |
| `AnthropicLLM` | `inqtrix.providers.anthropic` | `urllib.request.urlopen` | vcrpy (patches `http.client.HTTPConnection`) |
| `BedrockLLM` | `inqtrix.providers.bedrock` | `boto3.client("bedrock-runtime")` | `botocore.stub.Stubber` (VCR's boto3 integration is fragile; Moto does not yet support `bedrock-runtime`) |

**Single-library principle**: every replay test in this repo uses
either vcrpy + pytest-recording OR `botocore.stub.Stubber`. There is no
third mocking library. Sequence scenarios that would normally require
a side-effect mock (e.g. Anthropic 529 → success) are expressed
through multi-interaction cassettes; for Bedrock the same pattern
becomes successive `Stubber.add_response`/`add_client_error` calls.

## Cassette layout and naming

All cassette and stub data live under `tests/fixtures/`:

```text
tests/fixtures/
  __init__.py
  sanitize.py                    # request/response scrubbing hooks
  bedrock_responses.py           # JSON loader + ClientError builders
  bedrock/<scenario>.json        # Bedrock Converse response fixtures
  cassettes/
    anthropic/<test_name>.yaml
    azure_openai/<test_name>.yaml
    litellm/<test_name>.yaml
```

Conventions:

- **One cassette per test by default**. The cassette filename matches
  the test function (`test_complete_success_replay.yaml`) and
  pytest-recording resolves the path automatically via the
  `vcr_cassette_dir` fixture each module overrides.
- **Reuse via explicit decorator** when several tests would otherwise
  produce identical wire data: `@pytest.mark.vcr("test_complete_success_replay.yaml")`
  points the test at an existing cassette by name.
- **Hand-crafted cassettes are annotated** with a leading comment
  explaining their provenance: `# Hand-crafted from <provider> response
  shape on YYYY-MM-DD`. Re-record via `INQTRIX_RECORD_MODE=once` when
  the upstream schema drifts.
- **Cassette size**: keep individual files under 50 KB. Truncate long
  realistic payloads to a representative snippet — schema fidelity
  matters, byte fidelity does not.

## Sanitization

`tests/fixtures/sanitize.py` is the single source of truth. It is
wired into VCR via `vcr_config` in `tests/replay/conftest.py` and
exposes:

- `before_record_request` / `before_record_response` — VCR hooks that
  strip every header in `SANITIZED_HEADERS`, scrub query parameters
  in `SANITIZED_QUERY_KEYS`, and replace JSON body fields whose name
  matches `SECRET_BODY_KEYS`.
- `assert_cassette_clean(path)` — protective scan used by
  `tests/replay/test_sanitization.py`. Walks `tests/fixtures/`
  recursively, runs every entry of `_SECRET_PATTERNS` against each
  YAML/JSON file, and fails loudly on the first match.

If you introduce a new auth header or payload key carrying secrets,
extend `SANITIZED_HEADERS` / `SANITIZED_QUERY_KEYS` / `SECRET_BODY_KEYS`
**before** recording the cassette. The protective scan in
`tests/replay/test_sanitization.py::test_every_committed_cassette_passes_secret_scan`
must catch any oversight in the mandatory local gate.

## Markers and local gates

There are exactly **three** custom pytest markers, all registered in
`pyproject.toml` under `[tool.pytest.ini_options]`:

- `@pytest.mark.replay` — applied module-wide via
  `pytestmark = pytest.mark.replay` at the top of every file in
  `tests/replay/`. Selectable via `pytest -m replay` for fast targeted
  runs.
- `@pytest.mark.postgres` — tests needing a disposable Postgres via
  `INQTRIX_TEST_DATABASE_URL`; carried module-wide by the files in
  `tests/storage/`.
- `@pytest.mark.qdrant` — tests needing a disposable Qdrant via
  `INQTRIX_TEST_QDRANT_URL`; carried by the Qdrant-backed suites, among
  them `tests/test_qdrant_store.py`.

`tests/conftest.py` maps the last two markers to their URL variables: without
the variable the marked tests are skipped, and with
`INQTRIX_TEST_REQUIRE_INTEGRATION=1` the same situation becomes a session
error instead.

There is **no** `live` marker. Recording against real backends is a
manual maintainer workflow that uses the same tests (see next
section).

All replay cassettes / stubs are committed and offline; no env vars or network
access are required. The local invocations — full offline suite, single file,
`-k`, replay-only, and the `--collect-only -q` count for a release note — are
in [Running tests](running-tests.md#daily-commands).

GitHub Actions are intentionally inactive while release management is being
redesigned. No local pass should be described as a CI or release pass, and a
skipped PostgreSQL/browser/profile prerequisite must remain visible as
`blocked` or not run. The archived historical files are not templates to
reactivate; see [Release process](release-process.md).

## Recording new cassettes

Recording is the only operation that needs real API keys. The
recording workflow uses the same replay test as a recorder by setting
`INQTRIX_RECORD_MODE=once`; for the exact invocation with a real key see
[Running tests](running-tests.md#recording-a-replay-cassette).

What happens internally:

1. `tests/replay/conftest.py::vcr_config` reads the env var and sets
   `record_mode="once"`.
2. The test runs; VCR sees no cassette matching the test name and
   makes one real HTTP call.
3. The request and response pass through `before_record_request` /
   `before_record_response` from `tests/fixtures/sanitize.py`,
   stripping all known auth headers, query parameters, and JSON body
   fields.
4. VCR writes the sanitized YAML cassette to
   `tests/fixtures/cassettes/anthropic/test_complete_success_replay.yaml`.
5. Review the cassette diff manually. Run
   `pytest tests/replay/test_sanitization.py` to confirm the
   protective scan stays green.
6. Commit the cassette together with any test or sanitizer changes.

Allowed `INQTRIX_RECORD_MODE` values mirror VCR's record modes:

| Value | Behavior |
|---|---|
| `none` (default) | Replay-only; missing cassette fails the test immediately. |
| `once` | Record if cassette is missing; replay if it exists. **Recommended for adding new cassettes.** |
| `new_episodes` | Add new interactions to existing cassettes. |
| `all` | Re-record every interaction. **Only use when intentionally refreshing all cassettes for a provider.** |

Anything else (typos, capitalisation drift) silently falls back to
`none` so a misconfigured shell cannot accidentally enable recording
in a future automation environment.

## Cookbook: adding a test for provider X

1. **Inspect the provider's transport.** Read the relevant
   `src/inqtrix/providers/<x>.py`: does it use the OpenAI SDK
   (httpx → vcrpy), `urllib.request.urlopen` (also vcrpy), or
   `boto3` (`Stubber`)?
2. **Pick a scenario.** The minimum coverage per provider is five
   distinct cassettes/stubs: success + rate-limit + API error +
   one provider-specific edge case (empty response, structured output, SP
   auth, etc.) + one more variation. For Azure providers, cover all
   four auth modes (api_key, SP, custom token_provider, pre-built
   credential) — reuse cassettes via explicit `@pytest.mark.vcr("...")`
   when wire shape is identical.
3. **Add the cassette / stub fixture.** For VCR providers, create
   `tests/fixtures/cassettes/<provider>/<test_name>.yaml` matching
   the [layout above](#cassette-layout-and-naming). For Bedrock,
   add a JSON file under `tests/fixtures/bedrock/` and reference it
   from `tests/fixtures/bedrock_responses.py::load_bedrock_response`.
4. **Write the test.** Drop a new file
   `tests/replay/test_<provider>_replay.py` modelled on the existing
   `tests/replay/test_litellm_replay.py` (vcrpy) or
   `tests/replay/test_bedrock_replay.py` (Stubber). Set
   `pytestmark = pytest.mark.replay` and override `vcr_cassette_dir`
   per module.
5. **Verify the protective scan passes.** Run
   `pytest tests/replay/test_sanitization.py` after committing the
   cassette. If it flags a secret-pattern hit, sanitize the cassette
   by hand and extend `SANITIZED_HEADERS` / `SECRET_BODY_KEYS` so
   future recordings handle the same field automatically.
6. **Run the full suite.** `uv run pytest tests/ -v`, or
   `python -m pytest tests/ -v` in the pip-installed environment, should stay
   fully green.

## Retrieval evaluation (tests/eval/)

A fifth, quality-focused layer gates retrieval changes (embedding
models, store backends, chunking): a committed German golden set
(10 corpus documents, 50 labeled queries under
[`tests/eval/golden/`](../../tests/eval/golden)) graded with
document-level recall@k, MRR, and nDCG@5.

| Suite | Runs | Purpose |
|---|---|---|
| `test_metrics.py` | always (offline) | Metric math against hand-computed values; golden-set label integrity. |
| `test_retrieval_smoke.py` | always (offline) | The harness end to end with stub embeddings — proves ingestion/search/report wiring, NOT quality. |
| `test_retrieval_eval.py` | only with `INQTRIX_EVAL_EMBEDDING_BASE_URL` (+ `_API_KEY`, `_MODEL`), or with `INQTRIX_EVAL_EMBEDDING_PROVIDER=azure` plus `INQTRIX_EVAL_AZURE_ENDPOINT` and `INQTRIX_EVAL_AZURE_API_KEY` | Real-embedding quality against the committed baseline for that embedding model and golden-set tier in `tests/eval/baselines/`. |

Baseline workflow: baselines are keyed by embedding model AND golden-set
tier (`INQTRIX_EVAL_GOLDEN_SET`, default `base`; the registry in
`tests/eval/harness.py` also offers `hard`, `bsi`, `dora`, `dora_holdout`,
and `gquad`, and an unknown name fails loudly). The first gated run for a
given model/tier pair writes a JSON artifact (`tests/eval/artifacts/`,
gitignored) and skips with a pointer; establishing that baseline is a
deliberate commit of the aggregate metrics. Later runs fail when any gated
metric drops more than 0.05 below the baseline. Before switching retrieval
backends (memory to Qdrant hybrid) or locking an embedding model, run the
gated suite per candidate and compare artifacts.

`no_evidence` queries are excluded from retrieval metrics; they drive the
answer-side gate-honesty eval in
[`tests/eval/test_answer_eval.py`](../../tests/eval/test_answer_eval.py),
which runs the real `KnowledgeAlgorithm` with its gate and grades abstention
rate, false-refusal rate, citation rate, and gate telemetry against committed
answer baselines in `tests/eval/baselines/`. The harness has no claim-level
faithfulness judge; any judge model for answer-side metrics must be pinned,
because swapping it silently re-baselines every threshold.

## Agent-kernel evaluation (tests/eval/)

The agent tier grades the KERNEL as a whole — answer, citations,
routing, policy — through the real `/v1/runs` serving path, with
code-first graders and `pass^k` reliability (all-k-trials succeed,
stricter than any-of-k). Harness:
[`tests/eval/agent_kernel_harness.py`](../../tests/eval/agent_kernel_harness.py);
scenarios are versioned German assignments defined in
[`tests/eval/test_agent_kernel_eval.py`](../../tests/eval/test_agent_kernel_eval.py).

| Suite | Runs | Purpose |
|---|---|---|
| `test_agent_kernel_eval.py` (offline part) | always | Harness CONTRACT with scripted providers: collection (answer artifact, references, tools, gates, tokens), every code grader incl. a negative control, and the `pass^k` math. Deterministic; proves wiring, NOT model quality. |
| `test_agent_kernel_eval.py::test_agent_kernel_live_eval` | only with `INQTRIX_EVAL_AGENT_LIVE=1` (+ real provider env, `uv run --env-file .env`) | k=3 real trials per scenario against the configured providers; writes the reviewable artifact `tests/eval/artifacts/agent_kernel_live.json` and enforces the initial reliability floors. |

Grading is code-first by design: citation resolution against the run's
reference labels (plus the cited-only contract), tool-routing and
policy conformance (expected/forbidden tools, gate parked or not),
answer-artifact materialization, token/latency capture. An LLM judge
for subjective quality is a later, separately pinned addition — a
judge model swap silently re-baselines every threshold, so it never
enters the offline default suite.

Baseline workflow mirrors the retrieval tier: live runs write the
artifact for review; tightening the floors (or adding per-model
baselines under `tests/eval/baselines/`) is a deliberate commit.
Failure-driven growth: recurring live failures (park/resume, gate,
fallback-marker cases mined from real run history) become new
versioned scenarios — the eval set follows the product's actual weak
spots instead of a static benchmark.

## Collaboration test matrix

Editor collaboration crosses a shared schema package, two runtimes, a binary
gateway, PostgreSQL, three browser-serving paths, and role-aware UI. No single
mocked test proves the feature. Changes must select the layers whose public
contract they can break:

| Layer | Current location | Contract protected |
|---|---|---|
| Shared schema | `packages/editor-schema/tests/` | Markdown/Yjs round trips, secure UUID fallback, schema fingerprint, five suggestion kinds, reversible structure suggestions, the edit-mode guard for pending suggestions, decision bypass, and final/original projections. |
| Node coordinator | `apps/collaboration-server/tests/` | Config validation, leases, state loading, durable acknowledgements, restart-safe semantic sync no-ops, five-kind suggestion policy, the edit-lease rules for pending suggestions and their agreement with the editor's guard, the resolution of edit-lease updates after concurrent merges, bounded change summaries, AI publication and its placement onto a document others changed since its base (counter-cases and a seeded fuzz with an exact oracle), decisions, and failure close codes. |
| FastAPI service | `tests/test_editor_collaboration.py`, `tests/test_editor_patch_collaboration.py` | Session contracts, shared-comment permissions/lifecycle, mentions, private-note isolation, activity summaries, expected-sequence decisions, idempotency, and legacy Markdown isolation. |
| PostgreSQL | `tests/storage/test_editor_collaboration_postgres.py`, `tests/storage/test_editor_collaboration_migration.py` | RLS, sequence allocation, comment threads/messages/read cursors, hash/command idempotency, suggestions removed by an edit with their audit entries, instance fencing (fence lock mode, takeover ordering in both directions, renewal and takeover liveness), the lock order of deleting a document and revoking a guest link against updates and lease rotations, a guest unlock against a password or token rotation in either order, forced interleavings of a guest unlock, a guest session refresh, a guest lease issue and a guest comment mutation against revoking the link, rotating its password or token and (for the lease issue) deleting the document, in which the owner's change always commits without a deadlock, a revocation or rotation leaves no guest session or lease of the link live and admits no guest comment after it, and a deletion no lease of the document, a guest refresh against the same guest's lease issue or rotation, the revocation an introspection commits for a lease whose access is gone, an update under a long-expired lease that a lease issue sweeps, snapshots, retention, tombstones, additive migrations, and tenant isolation. |
| Binary transport | `tests/test_collaboration_gateway.py` | Origin policy, binary-only relay, bidirectional frame limits, upstream failure, and close-code propagation. |
| Deployment paths | `tests/test_collaboration_deployment.py`, `tests/web_gateway/`, `tests/test_helm_chart.py`, `tests/test_container_supply_chain.py` | Private Node topology, Compose profile, Python/nginx WebSocket forwarding, immutable images, and one-replica disabled-by-default Helm rendering. |
| Research Desk | `apps/research-desk/src/**/*.test.ts(x)` | HTTP-compatible UUIDs, jittered reconnect/manual retry without global auth reload, durable-ack state, role and comment-mode locking, shared/private comment separation, review presentation, inspector filtering/navigation, responsive toolbar geometry, and detached export/import rules. |

Run the offline gates after every collaboration milestone:

```bash
uv run ruff check src/
uv run pytest tests/ -v
# or: python -m pytest tests/ -v
npm ci
npm --workspace @inqtrix/editor-schema run typecheck
npm --workspace @inqtrix/editor-schema test
npm --workspace @inqtrix/collaboration-server run typecheck
npm --workspace @inqtrix/collaboration-server test
npm run ui:lint
npm run ui:typecheck
npm run ui:test
npm run ui:build
npm ls yjs
npm run verify:tooling
helm lint deploy/helm/inqtrix
helm template inqtrix deploy/helm/inqtrix -f deploy/helm/inqtrix/values-dev.yaml
```

`helm template` is the step that proves the chart's fail-fast guards; a bare
render exits 1. `helm lint` is a style and structure check only: since Helm 4 a
failing guard surfaces as `level=INFO msg="funcMap fail"` while the lint still
reports "0 chart(s) failed". A green lint is therefore not evidence.

`npm ls yjs` must resolve one compatible Yjs copy. Browser, shared schema, and
Node package versions must stay exact and coordinated; a duplicated Yjs or
mixed Tiptap schema is a visible verification blocker, not a warning.
`package-lock.json` is the sole JavaScript lock.

Set `INQTRIX_TEST_DATABASE_URL` to a disposable migrated PostgreSQL database
and `INQTRIX_TEST_QDRANT_URL` to a disposable Qdrant instance to exercise the
real store, the RLS tests, and the Qdrant-backed knowledge tests. The default
offline run may skip those tests, so an all-green offline run alone does not
prove sequence races, fencing, or tenant isolation. The proof is the second
run, which needs both URLs because it selects `postgres`- and `qdrant`-marked
tests:

```bash
INQTRIX_TEST_REQUIRE_INTEGRATION=1 uv run pytest tests/storage tests/test_qdrant_store.py
```

`INQTRIX_TEST_REQUIRE_INTEGRATION=1` turns a missing URL from a skip into a
session error, so this run cannot report green without the services.

### Shared verification profiles

Eight reusable technical engines serve eleven profiles because they prove
different things,
but one orchestrator owns profile selection, Run ID, preflight, reporting,
process cleanup, and the scenario inventory (`npm run verify:list` prints the
current inventory):

| Profile | Purpose | Required infrastructure | What it does not prove |
|---|---|---|---|
| `ui-fixture` | Deterministic React behavior without a backend | Local Node/browser toolchain | Authentication, persistence, real WebSockets |
| `system-smoke` | Visible multiuser workflows and document/role isolation | Production-like stack, distinct authenticated users, disposable fixture | Fault controls or sustained capacity |
| `fault-injection` | Revocation, downgrade, gateway/sidecar outage and restart, lost-ACK recovery, private-anchor isolation, protocol failure | Running canonical stack with the collaboration fault seam explicitly enabled, provisioning credentials, explicit Podman/Docker engine; or an authenticated external fault fixture | Capacity or provider correctness |
| `load-smoke` | Local WebSocket/Yjs visibility and durability with at least 20 sockets and five writers | Live collaboration stack plus admin and temporary-user passwords for automatic Run-ID-bound provisioning | Production capacity |
| `load-capacity` | Fixed 1,000-socket/100-writer capacity, restart, lease rotation, and reconstruction gate | Explicit production-like HTTPS fixture and budget | Browser UX/accessibility |
| `edge-conformance` | Black-box parity of the packaged Python and nginx web adapters | Explicit Podman or Docker engine; synthetic isolated backend | Authentication, persistence, browser UX, or production capacity |
| `owner-setup` | One visible first-run local-owner session through setup, initial persistence, user administration, and pointer logout | Externally prepared fresh stack, exactly one explicit `--browser` target | Multiuser collaboration, capacity, or fault controls |
| `agent-desk` | Kernel answer experience through the visible Agent Desk: run submission, tool activity, clickable citations, console/network hygiene, cleanup integrity | Live stack with configured model providers | Collaboration transport, capacity, or fault controls |
| `chat-prompt` | Chat and Prompt Library browser matrix: real completions, reload ordering, duplicated tabs, offline recovery, library CRUD in Chromium, Firefox, and WebKit | Live stack with configured model providers, all three browser engines | Collaboration transport, capacity, or fault controls |
| `load-soak` | Thirty-minute, 25-identity mixed collaboration soak with phased network shaping and resource recovery budgets | Live collaboration stack, provisioning credentials, explicit Podman engine | Production capacity |
| `load-ramp` | Resource-gated socket fan-out ladder (20/100/250/500/1000) from a capped identity pool; a controlled stop reports the highest rung reached | Live collaboration stack, provisioning credentials, explicit Podman engine | The production capacity gate, which it never discharges |

Use only the profile entrypoints. The command list, the mandatory
`--container-engine`/`--browser` flags, and the fault-seam recreation steps are
in [Running tests](running-tests.md#browser-system-fault-and-load-verification).

Every run receives an `inqv-…` Run ID and a redacted report under
`e2e/.results/verification/<run-id>/`. Missing users, documents, controls,
browsers, HTTPS, or capacity metadata block the relevant profile before an
engine starts; they are never converted to a skip/pass. Every created resource
must carry the Run ID, be registered immediately, and be removed after success,
failure, or interruption. A cleanup failure changes the overall result to
`cleanup_failed`.

`system-smoke` covers the real UI through isolated identities and exercises
Owner/Edit/Suggest/View, identical titles under different owners, a private
control document, share acceptance, permission downgrade/revocation, direct
unauthorized requests, comments, suggestions, presence, rapid document
switching, reload/navigation, reconnect, and responsive/focus behavior. It
must inspect browser console/network state and relevant service logs; an API
response alone is not sufficient.

`load-smoke` is the bounded local gate required for ordinary changes. The
stricter `load-capacity` profile is the fixed-capacity environment gate.
Record its environment and measurements; it is not implied by the local smoke
profile. Both profiles' socket, writer, observer, lease, latency, cookie, and
fixture contracts live in [`tests/load/README.md`](../../tests/load/README.md).

## Writing browser assertions that can fail

Browser checks in `e2e/` and `tests/verification/` assert against a live app,
which means they can also assert against the wrong screen. These rules exist
because each was violated in a real verification run and produced a confident
pass that a screenshot later refuted.

**Anchor first.** Prove the measurement is where it claims to be — authenticated,
app shell mounted, target view rendered — before asserting anything. A sign-in
screen must raise, not return an empty result. A text search for a control name
will happily match stray copy on a login page.

**Assert on the element that owns the state.** Read a status from the node that
renders it, not from `document.body.innerText`. Two components on one screen can
use the same word for different axes: the header badge reports project sync, the
editor badge reports the collaboration transport. A page-wide search returns
whichever appears first in the DOM.

**Probe the mechanism the code uses.** Check `disabled`/`aria-busy` only if the
implementation sets them. A component that signals busy by swapping an icon will
read as "no busy state" to a probe looking for attributes.

**Count confirmations, not attempts.** Input sent to a read-only surface is
discarded. Derive counts from what the server persisted or the peer received;
treat browser-side counters as claims.

**Review the images.** Screenshots are evidence only once a human or the agent
opens them. Truncated labels, contradictory indicators and wrong badges are
invisible to text assertions by construction.

## Known limitations

- **Bedrock-runtime mocking** (`botocore.stub.Stubber`, see the
  [tool matrix](#tool-matrix-per-provider)): stub responses must match the real
  Converse API shape (including required fields like `metrics` and
  `usage.totalTokens`); the Stubber's parameter validator will reject
  malformed fixtures at test-time.
- **Azure Foundry auth** is not exercised by any replay test — `tests/replay/`
  holds cassette suites for Anthropic, Azure OpenAI and LiteLLM, plus the
  Stubber-based Bedrock suite.
  Neither auth path needs periodic re-instantiation: the Entra path hands the
  OpenAI client the refreshing bearer-token provider returned by
  `AIProjectClient.get_openai_client()`
  (`src/inqtrix/providers/azure_web_search.py:181-200`), and the API-key path
  sends a static project key in the `api-key` header, which carries no token
  lifetime.
- **Retry sequences** are reflected as multi-interaction cassettes when
  needed (for example Anthropic 529-then-success). LLM providers backed by
  the OpenAI SDK disable hidden SDK retries and use Inqtrix-owned retry loops;
  replay tests can still call
  `provider._client.with_options(max_retries=0)` when a rate-limit or
  API-error scenario must replay exactly one interaction from its cassette
  (`tests/replay/test_litellm_replay.py`,
  `tests/replay/test_azure_openai_replay.py`).

## Related docs

- [Running tests](running-tests.md)
- [Editor collaboration architecture](../architecture/editor-collaboration.md)
- [Deploy editor collaboration](../deployment/editor-collaboration.md)
- [Local infrastructure](local-infrastructure.md)
- [Verification profiles for browser and system testing](../../e2e/README.md)
- [Collaboration load profiles](../../tests/load/README.md)
