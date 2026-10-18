# Logging

> Files: `src/inqtrix/logging_config.py`, `src/inqtrix/runtime_logging.py`, `src/inqtrix/server/app.py`

## Scope

How Inqtrix configures the `inqtrix` logger, which environment variables control it, how secrets are redacted, and how uvicorn and FastAPI logs are mirrored into the same file when running the HTTP server.

## Logger topology

The library writes to `logging.getLogger("inqtrix")`. `configure_logging(...)` builds a dedicated handler pipeline that does **not** propagate to the root logger. Tests that want to capture Inqtrix logs must attach `caplog.handler` directly to the `inqtrix` logger.

## Configuration helpers

`inqtrix.logging_config` exposes these configuration helpers:

- `configure_logging(*, enabled, level, log_dir="logs", console=False, force=True, json_format=False) -> Path | None` — configures the `inqtrix` logger: optional file handler, optional console handler, secret-redaction filter. Pass `force=False` to skip reconfiguration when another caller already set up handlers. With `json_format=True` the console handler carries the full `level` (stdout is the canonical machine-readable sink); in text mode it stays at `WARNING`.
- `build_uvicorn_log_config(log_file: Path | str | None, *, web_level: str = "INFO", json_format: bool = False) -> dict[str, object]` — produces a `logging.config.dictConfig`-compatible dict that mirrors uvicorn's default stderr/stdout setup and additionally writes `uvicorn.error` and `uvicorn.access` into the same `log_file` as Inqtrix. Pass this to `uvicorn.run(app, log_config=...)`. **Pass the SAME `json_format` value as `configure_logging`** — uvicorn builds its handlers from its own dictConfig, so a mismatch mixes JSON and text lines on one stream.
- `read_logging_env() -> LoggingEnv` — reads every `INQTRIX_LOG_*` process variable with its default and returns them as a frozen `LoggingEnv`. These stay plain environment reads because logging must be configurable before `Settings()` validation can fail. The four bootstrap sites (`src/inqtrix/__main__.py`, `src/inqtrix/worker/__main__.py`, `create_app`, `src/inqtrix/server/stacks.py`) all read through it so they cannot drift apart.

Structured runtime events are emitted by `inqtrix.runtime_logging.emit_runtime_event(...)`. It is not a second logging system and it does not create a separate JSON file. Iteration events first enter the protected run audit; the fail-closed operational projection — only identifiers, lifecycle/status fields, models, counters, usage and timings — is then attached to the active span, never to the `inqtrix` logger, so it needs a recording span and a trace sink (`INQTRIX_TRACING=file` or `otlp`); the logger itself keeps the `RUN start:`/`RUN end:` lifecycle lines. Exact queries, provider prose, snippets, claim/evidence text, prompt views and URLs are not mirrored into the timestamped `logs/inqtrix_*.log` file. Independent of the profile, the node and task spans always carry the step's *decision* as plain attributes (stop reason, confidence, coverage, claim and source counts, contract status, evidence-budget cut counts) — identifiers, statuses and counters only, the same projection rule; the forensic events remain the content-bearing detail behind `OBSERVABILITY_PROFILE=forensic`.

Minimal library setup:

```python
from inqtrix import configure_logging

log_file = configure_logging(
    enabled=True,
    level="INFO",
    log_dir="logs",
    console=False,
)
```

Server bootstrap with uvicorn mirroring:

```python
from pathlib import Path
import uvicorn

from inqtrix.logging_config import build_uvicorn_log_config, configure_logging
from inqtrix.server.app import create_app

log_file = configure_logging(enabled=True, level="INFO", console=False, log_dir="logs")

uvicorn.run(
    create_app(),
    host="0.0.0.0",
    port=5100,
    log_config=build_uvicorn_log_config(log_file),
)
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `INQTRIX_LOG_ENABLED` | `false` | Master switch; when `false` the library only attaches a `NullHandler`. |
| `INQTRIX_LOG_LEVEL` | `INFO` | Any of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `INQTRIX_LOG_CONSOLE` | `false` | Mirror WARNING+ records from the `inqtrix` logger to stderr in addition to any file sink. |
| `INQTRIX_LOG_WEB_LEVEL` | `INFO` | Used by `build_uvicorn_log_config` for uvicorn/FastAPI logs. |
| `INQTRIX_LOG_INCLUDE_WEB` | `true` | Opt-out for uvicorn mirroring. |
| `OBSERVABILITY_PROFILE` | `summary` | `summary`, `debug`, or `forensic`. `forensic` produces source/citation/claim/answer lineage in the protected run audit and an operational, content-minimized log projection; `debug` is currently reserved for future mid-level detail. |
| `INQTRIX_LOG_FORMAT` | `text` | Line shape. `text` keeps the historical pipe format byte-identical; `json` renders one machine-readable object per line — for the `inqtrix` logger AND the mirrored uvicorn/FastAPI loggers. Authoritative definition: [Settings and environment variables](../configuration/settings-and-env.md#process-level-variables-outside-settings). |

The example webserver scripts (`examples/webserver_stacks/*.py`) read these variables, call `configure_logging(...)` once at startup, and pass `log_config=build_uvicorn_log_config(...)` to `uvicorn.run` so the timestamped file under `logs/` holds Inqtrix lifecycle lines, uvicorn startup/shutdown, and `uvicorn.access` request lines in one place.

## JSON log lines and correlation fields

With `INQTRIX_LOG_FORMAT=json`, every record becomes one JSON object with a
fixed envelope: `ts` (RFC3339 UTC), `level`, `logger`, `event`, `message`,
`thread`, plus `exc` for exceptions and the correlation fields bound by the
request/worker context: `request_id` (accepted from a valid incoming
`X-Request-ID` or generated, and echoed on every response), `run_id`, `user`
(the STABLE pseudonym `usr_<hex16>` — see `INQTRIX_PSEUDONYM_PEPPER`),
`workspace`, and `tenant`. Once tracing is enabled, `trace_id`/`span_id`
join the envelope so a log line and its trace correlate directly.

The `trace_id` of a request's log lines, the access log's included, is the
trace of its request span, and that trace holds the whole call: work the API
hands to an executor thread (editor assist, chat, text improvement, session
titles) carries the request's trace context across the hop and runs as a
child of the request span, with its own trace name kept through
`langfuse.trace.name`. The request span carries the same pseudonym as the
`user` field, as `langfuse.user.id`, once the principal is resolved; an
unscoped principal (anonymous or static key) carries none. Two consequences
follow from one trace per request: below a sample rate of `1.0`, the editor,
chat and title spans are sampled together with their request instead of on
their own, and a client that sends a `traceparent` places them under its own
trace id and sampling decision, as it already does for runs.

Redaction is format-independent: the same `_RedactSecretsFilter` runs in
front of both formatters, and structured field values are additionally
scrubbed. Follow one user across api and worker containers with:

```bash
docker compose logs api worker | jq -c 'select(.user == "usr_ab12cd34ef56aa77")'
```

In JSON mode the console sink carries the FULL `INQTRIX_LOG_LEVEL` (stdout is
the canonical machine-readable sink for container runtimes), so this works
without a file sink. In text mode the console keeps its historical
`WARNING`-and-above mirror.

## How the switches interact

There are three separate concerns:

- `INQTRIX_LOG_ENABLED` decides whether a persistent file under `logs/` is created.
- `INQTRIX_LOG_LEVEL` decides which records reach that file.
- `OBSERVABILITY_PROFILE` decides whether detailed forensic lineage events are produced at all. Only `forensic` enables `query_record`, `query_summary`, `source_record`, `provider_citation_record`, `evidence_record`, `claim_record`, `claim_merge`, `evidence_verification_projection`, `evidence_selection`, `answer_prompt_inputs`, `answer_section`, `answer_claim_binding`, `answer_sentence_audit`, and related events.

**Lineage events live on the trace, not in the log.** They are attached to the
span of the step that produced them, so the waterfall shows them in place
instead of in a separate stream that has to be joined by hand. Recording them
therefore needs a trace sink:

```bash
OBSERVABILITY_PROFILE=forensic       # how deep
INQTRIX_TRACING=file                 # where to  (or otlp)
```

`forensic` with `INQTRIX_TRACING=off` or `local`, or with a sample rate below
`1.0`, records only part of what was asked for — the process warns about both
combinations at startup. See [Forensic cookbook](forensic-cookbook.md#debugging-runs) for the
drill-down path.

The log keeps what it has always been good at: lifecycle lines, warnings,
fallback markers and the operational per-round trace, each carrying the
correlation fields so a log line and its trace join on `trace_id`.

Do not treat the container/file log as the durable evidence audit. It is intentionally insufficient to reconstruct private content. Authorized run artifacts (including `web_search_ledger`, result/artifact records, or `iteration_logs` in testing/parity mode) carry the reconstructable, redacted chain; compact INFO/DEBUG lines are only operational signposts that can be joined through stable IDs.

`TESTING_MODE=true` is separate from file logging. It exposes the HTTP `/v1/test/run` endpoint and attaches the sanitized `iteration_logs` list to test/parity results. With `OBSERVABILITY_PROFILE=forensic`, those exported iteration logs include the same forensic events even when file logging is disabled; never enable testing mode in production.

Common recipes:

```bash
# Persistent summary log: lifecycle, warnings, fallback markers.
INQTRIX_LOG_ENABLED=true
INQTRIX_LOG_LEVEL=INFO
```

```bash
# Provider-level debug detail: adds the SDK-side DEBUG lines (max_tokens
# auto-raise in the Anthropic/Bedrock adapters, stream disconnect watcher).
# The per-round `TRACE ...` lines of the research loop are already INFO, so
# the INFO recipe above already carries them; no forensic lineage either way.
INQTRIX_LOG_ENABLED=true
INQTRIX_LOG_LEVEL=DEBUG
```

```bash
# Forensic lineage (on the trace, see above) plus a persistent log file.
INQTRIX_LOG_ENABLED=true
INQTRIX_LOG_LEVEL=DEBUG
OBSERVABILITY_PROFILE=forensic
```

```bash
# Export sanitized iteration_logs for parity/testing tools.
TESTING_MODE=true
OBSERVABILITY_PROFILE=forensic
```

## Secret redaction

Every handler attached by `configure_logging(...)` includes `_RedactSecretsFilter` from `logging_config.py`. Structured events also pass through an allowlist-style serializer before the handler filter sees them. Together they provide two layers of protection:

- Do not emit blocked structured fields such as `headers`, `authorization`, `request_kwargs`, `request_body`, `raw_response`, `api_key`, `client_secret`, `password`, `secret`, or credential objects.
- Redact credential-bearing URL query parameters such as `api_key`, `token`, `sig`, `signature`, `client_secret`, and `password`. Iteration-event console projections omit every URL, including benign and private URLs.
- Redact bearer tokens, `sk-*`/`pplx-*` API-key-like strings, and AWS access/session tokens in nested dict/list payloads.
- Keep raw provider request bodies, headers, SDK responses, and client configuration out of the standard and forensic logs.
- Authorization-denial warnings never contain raw user, tenant, recipient, or
  resource identifiers. They expose only bounded categorical fields and
  domain-separated HMAC references (`actor_ref`, `tenant_ref`,
  `resource_ref`). With `INQTRIX_PSEUDONYM_PEPPER` configured, references are
  stable across processes and restarts; without it they deliberately fall
  back to a process-local key and startup warns once.
- Authentication warnings that need to correlate one browser session use the
  same helper with the `ses` namespace. A full session identifier, or any
  prefix sliced from it, is still bearer-derived credential material and must
  never be logged.
- Personal-access-token rejection warnings use the existing `res` namespace
  for the token identifier. Unknown identifiers, incorrect secrets, and
  revoked or expired credentials retain distinct diagnostics without printing
  the raw token id or secret. Their public response remains a uniform 401.
- Orphan-object cleanup diagnostics use `ten` references for tenant ids and
  `res` references for object keys. A key embeds its tenant and must not expose
  that identity through a formatted log message. The registry lookup and
  physical deletion still use the original identifiers.
- Durable `auth.logout` audit rows use the same `ses_<hex16>` namespace at
  their domain writer. The revoked browser credential itself belongs in
  neither `audit_log` nor its admin list and CSV/NDJSON exports. Historical
  rows are sanitized set-wise inside PostgreSQL with a domain-separated
  SHA-256 derivation; migration output and errors expose only counts.

The same filter is reused by the uvicorn mirror so access logs do not leak tokens.

Path segments that are bearer credentials (the token of a guest link in
`/s/<token>` and `/v1/editor/share-links/<token>`) are replaced by
`[REDACTED]` in every log line, the access log's request line included. The
request span follows the same rule: it is named after the matched route
template (`GET /v1/editor/share-links/{token}`), or after the method alone
when no route matched, never after the raw path, and its `url.path` attribute
passes the same sanitizer. An exception the request span records, like one
on any other span, keeps what an application log line keeps of it: its type,
a stable code and the frame locations, never its text, which can carry the
path, a failed statement's parameters (document text, e-mail addresses) or a
provider's answer. The metric label is the route template as well.

## Evidence lineage events

Forensic mode persists the following evidence-specific events in the protected audit. Their ordinary log projection contains only non-content operational fields:

- `query_summary`: per-query summary with extracted/kept claim counts,
  `claim_extraction_valid_empty`, and rendered evidence-context size. This is
  the fastest way to distinguish valid empty claims from parse/API `ALGO-FAIL`.
- `algorithm_failure`: visible core-path failure record. In forensic or deep
  report runs, blocking failures prevent normal final-report synthesis and
  produce a diagnostic answer instead.
- `evidence_selection`: carries the `evidence_depth_gap` diagnostic in its
  payload — it does NOT travel with `answer_prompt_inputs`.

This is a selection, not the catalogue: the complete event lists with their payload
fields live in [Iteration log](iteration-log.md) and [Forensic cookbook](forensic-cookbook.md).

For live provider triage after a run, capture a debug artifact directory with `scripts/live_prompt_flow_debug.py` (default `/private/tmp/inqtrix-live-debug-<timestamp>`, override with `--out-dir`) and point the reader at that directory:

```bash
# uv
uv run python scripts/debug_research_log.py /private/tmp/inqtrix-live-debug-<timestamp>

# standard Python/pip
python -m pip install -e .
python scripts/debug_research_log.py /private/tmp/inqtrix-live-debug-<timestamp>
```

The script prints run, search-call, EvidenceRecord, evidence-overview, LLM-call and answer statistics without echoing raw URLs or secrets. Its logfile mode — the default when the argument is omitted, and what a `logs/inqtrix_*.log` argument selects — expects `RUN metadata: {...}` and `ITERATION ...: {...}` lines, which the runtime does not write, so it reports zero events against a current log file.

## Fallback markers visibility

The no-silent-fallbacks rule requires every fallback path to emit all three of: a `log.warning(...)` line, a progress event on the user-visible queue, and a structured iteration-log marker. The following markers are the ones operators rely on to reconstruct a run:

- `_classify_fallback`, `_plan_fallback`, `_evaluate_fallback`
- `_confidence_parsed`, `_evidence_consistency_parsed`, `_evidence_sufficiency_parsed`
- `_claim_extraction_fallback` — claim extraction failed for at least one source. A full-round failure also emits an `algorithm_failure` event with `phase=claim_extraction`.
- `algorithm_report_blocked` — answer synthesis was blocked because a core evidence-path failure would otherwise produce a normal-looking report from unaudited source context.
- `_answer_fallback` plus `_answer_fallback_kind` / `_answer_fallback_reason` — answer synthesis took a fallback path (`timeout`, `no_fallback_model`, or `fallback_model_failed`). The answer body starts with a visible `> [!WARNING] Antwort-Synthese-Fallback aktiv` block so the degradation is also surfaced in the rendered output, not only in operator logs.
- `_stop_reason`
- `Run cancelled by client disconnect`

See [Iteration log](iteration-log.md) for the structured marker view and the [Forensic cookbook](forensic-cookbook.md#debugging-runs) for the typical recovery flows.

## Worker claim safety markers

The worker's durable-claim path emits two operator-relevant line families:

- `Claim-Contract-Sonde: N Laeufe, im Mittel X ms, zusammen Y s seit
  Prozessstart.` — cumulative cost of the full database-contract probe
  (schema revision, role shape, RLS policy function). The probe runs at
  worker start and periodically (coalesced, one shared guard across all
  loops). It does NOT run per claimed job: the per-job protection is the
  in-transaction schema fence below, so under a healthy process this
  counter grows slowly regardless of job throughput. A rapidly growing
  count or a rising mean indicates probe contention and is worth a look.
- `Schema-Kopf hat sich unter dem Prozess bewegt — keine weiteren Claims.`
  (log level ERROR) — the in-transaction fence (`assert_schema_head`,
  first statement of every durable-claim transaction across the run,
  indexing, deletion and upload stores) found the installed Alembic
  revision differing from the packaged head. The claim rolled back, the
  triggering queue entry stays unacked for an upgraded worker, and the
  shared claim guard latches shut: every loop of the process stops
  claiming. Recovery is deploying a worker image matching the installed
  schema (or completing the migration); the process does not resume
  claims on its own. This fence detects a COMPLETED cutover; a migration
  still in flight carries the old revision and is governed by the
  canonical migration workflow, which stops clients first.

## API product-gate markers (`/readyz`)

The API side of the same taxonomy: the readiness probe distinguishes a
confirmed contract violation from a merely unreachable database and logs
the product gate's decisions as three line families:

- `readyz: confirmed database contract violation; closing the business-route gate.`
  (log level ERROR, per probe poll while the violation persists) — the
  contract probe REACHED the database and found it violating the packaged
  schema contract. Product routes gate closed immediately; integrity over
  availability.
- `readyz: database probe unreachable for Ns; business-route gate
  retains its state (open|closed).` (log level WARNING) — the probe
  could not reach the database, which proves nothing about the contract.
  The gate keeps its last verdict instead of turning a transient blip
  into a full-healthcheck-interval outage.
- `readyz: database continuously unreachable for Ns; closing the
  business-route gate (integrity before availability).` (log level ERROR, once at
  the transition) — the keep-state window is bounded (120 s): after
  sustained unreachability the gate fails closed until a probe confirms
  a healthy contract again.

The gate's current verdict is also visible without log access: the
`/readyz` payload carries a `database_contract` field
(`ok | unavailable | violation | skipped`).

## The `force=False` rule for the server

`create_app(...)` and `create_multi_stack_app(...)` call `configure_logging(..., force=False)`. This preserves the example-script configuration when a user starts uvicorn from a script that already configured the `inqtrix` file handler. If you build your own bootstrap path, preserve that invariant so your file handler is not replaced by a later server default.

## Related docs

- [Iteration log](iteration-log.md)
- [Evidence pipeline](../architecture/websearch/evidence.md)
- [Forensic cookbook](forensic-cookbook.md)
- [Web server mode](../deployment/webserver-mode.md)
