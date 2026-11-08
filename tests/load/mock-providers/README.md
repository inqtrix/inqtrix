# Provider stand-in for load measurement

Capacity measurement needs many concurrent runs. Driving those against real
providers is expensive and rate-limited, and provider latency variance would
dominate the numbers being measured. This stand-in answers the LLM,
web-search, and embedding calls instead, so a measurement exercises the
application's own threads, connections, pools, and CPU while provider
behaviour stays a controlled, reproducible input.

## What makes it faithful

Two artifacts, both derived from traces of real runs rather than authored:

| Artifact | Contents | Version control |
| --- | --- | --- |
| `profiles/*.json` | per-operation p50/p95/max latency, median token counts, calls per run | tracked |
| `corpus/` | the captured provider answers and tool-call conversations, plus the routing signatures | **untracked** |

The corpus stays out of version control because it holds real questions,
answers, and source material, and because a routing signature is the opening
of a system instruction, which for agent work restates the user's own task.
Every operator regenerates it from their own runs.

Plain-text and structured replies are grouped by recorded purpose and response
schema. The schema separates, for example, an outline from a section even when
both use the same system role. Capture and replay share one normalization of
known language/date framing and require one exact instruction/schema key.
An ambiguous key stays unmatched; matching only part of an opening is not enough.
Regenerate a corpus when its routing format changes; `/healthz` reports an old
format as degraded instead of silently reusing it.

Latency is sampled per call from a lognormal fitted so its median is p50 and
its 95th percentile is p95, clamped at the observed maximum. A constant delay
would erase the spread that determines how long a thread is actually held.

## Fidelity limits

Search results remain synthetic. Current search generations do carry provider
answers, but their result/provenance formats differ across providers and are
not replayed by this stand-in. Search generations and older search spans both
contribute to search timing and inclusive token counts. A replayed extraction
is not semantically derived from the synthetic sources accompanying it.

Tool-calling operations replay as conversations. Calibration keeps the model
turns of one run and purpose in order, tool calls included. A request that
offers tools gets the turn at its position, counted by the assistant messages
it already holds, from the recording of the same assignment or else from one
whose tools this request offers. Identifiers in the recorded arguments are
rebound to ones the request holds, so `read_evidence` and `read_canvas` do real
work; a call whose identifier or tool the request lacks is dropped and counted
in `tool_calls_dropped`. Citations in a replayed text are pointed at evidence
labels the request knows, because a label from the recorded run would send the
caller down a citation repair no recorded run took. Tool results still depend on
synthetic sources, so a recorded review does not judge the text it sees. Workspace
agent children end as failed behind this stand-in, because their recorded outline
does not pass validation there, and the parent run carries on without them; that
is about three runs in five hundred. Embeddings remain synthetic,
with local generation time rather than a calibrated provider delay. `/healthz`
degradation, unmatched requests, synthetic replies or dropped tool calls must be
reviewed before treating any result as representative.

The measurand is resource use and call shape, not answer correctness. Compare
fan-out and wall-clock with real runs. A partial profile is not a replacement
for one covering the entire measured workload; keep old and new profiles
separate until their operation coverage has been reviewed.

## Regenerating the artifacts

Export traces of real runs, one JSON document per run, then calibrate:

```bash
uv run python tests/load/mock-providers/calibrate.py <trace-dir> \
  --out tests/load/mock-providers/profiles/azure-calibrated.json
```

Run calibration in the installed project environment, which supplies the shared
language directives. Traces come from `GET /v1/admin/runs/{run_id}/trace/export` as an instance
administrator; the run must have executed with tracing enabled. Cover every
operation a measurement will exercise — research, knowledge, and agent runs
each contribute their own steps. `calibrate.py` names any operation it found
timings for but no captured response, because an incomplete corpus that stayed
quiet would produce a measurement built on empty answers.

## Running it

The Compose override wires the whole stack to the stand-in and turns on the
Prometheus endpoints:

```bash
podman compose -f deploy/compose/compose.stack.yaml \
  -f deploy/compose/compose.loadtest.yaml \
  --env-file <secrets-file> --env-file <config-file> \
  --profile knowledge --profile s3 --profile workers \
  up -d --build
```

That override uses its own project name, so the measurement stack has its own
volumes and the daily stack keeps its data. Run the first-run owner setup once
against it.

Standalone, for development of the stand-in itself:

```bash
uv run python tests/load/mock-providers/mock_providers.py
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `INQTRIX_MOCK_PORT` | `9300` | Listen port |
| `INQTRIX_MOCK_PROFILE` | `profiles/azure-calibrated.json` | Timing profile |
| `INQTRIX_MOCK_CORPUS` | `corpus` | Response corpus directory |
| `INQTRIX_MOCK_EMBEDDING_DIM` | `3072` | Embedding width; must match the collections under measurement |
| `INQTRIX_MOCK_SEARCH_RESULTS` | `8` | Synthesised results per search |
| `INQTRIX_MOCK_SEED` | `1` | Base seed; the same seed reproduces the same delays |
| `INQTRIX_MOCK_LATENCY_SCALE` | `1.0` | Multiplies every delay |

`INQTRIX_MOCK_LATENCY_SCALE` below `1.0` shortens a harness rehearsal but
makes the run's wall clock incomparable to production, so a capacity result
must be produced at `1.0`.

## Endpoints

| Route | Serves |
| --- | --- |
| `POST /azure/openai/v1/chat/completions` | Azure OpenAI chat completions |
| `POST /v1/chat/completions` | OpenAI-compatible chat completions |
| `POST /azure/openai/v1/responses` | Azure OpenAI Responses API, for the models whose card names it (GPT-6, GPT-5.6); answered from the same catalogue as chat completions, with function calls and structured output. The latency profile stays the calibrated one, so a run measures the platform's Responses path, not the latency of those models |
| `POST /azure-embeddings/openai/deployments/{deployment}/embeddings` | Azure embeddings |
| `POST /v1/embeddings` | OpenAI-compatible embeddings |
| `POST /perplexity/v1/responses` | Perplexity search |
| `POST /foundry/openai/v1/responses` | Azure AI Foundry web search |
| `POST /anthropic/v1/messages` | Anthropic messages |
| `GET /healthz` | Readiness, loaded artifact counts, and any degraded artifact |
| `GET /admin/stats` | Per-operation call counts, introduced delay, unmatched instructions |

Every provider call in the paths above is non-streaming, which is what the
application requests, so no streaming surface is implemented.

## Reading the result

`GET /admin/stats` records the requests received by this stand-in; compare them
with the application's trace to establish coverage. Its counts alone cannot
rule out an additional call to a real provider. Counts per operation show the fan-out the run actually
produced. `in_flight` is the number of provider calls holding a connection right
now and `disconnected` counts clients that hung up first, which together time a
cancel down to the provider socket; `in_flight_per_operation` says which calls those are.
`evidence_errors` counts replayed documents the application refused for their citations: the run
continues without them, so their writes are missing from what it measures. Each entry in
`unmatched_instructions` means an instruction changed or was never recorded, and those calls ran
on neutral fallback timing rather than calibrated timing; `run-load` tolerates a small share of them. The stand-in logs a warning the first time it sees each
unrecognised or ambiguous identity, using a hash rather than copying prompt
content into logs; `GET /healthz` reports a missing profile or corpus
as `degraded` rather than serving quietly from defaults.
