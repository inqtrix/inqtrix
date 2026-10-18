# First knowledge answer

> Files: `src/inqtrix/server/routers/knowledge.py`, `src/inqtrix/server/routers/runs.py`, `src/inqtrix/knowledge/algorithm.py`, `src/inqtrix/knowledge/profiles.py`

## Scope

From a running server to a cited answer over your own documents: enable the knowledge engine, create a collection, ingest text, ask via `mode=knowledge`, watch the run on SSE, and read the result. The whole flow works on the zero-infrastructure setup from [First research run](first-research-run.md) — the `memory` vector backend needs no services (contents are lost on restart). For persistence and hybrid retrieval, add Qdrant per [Platform components](platform-components.md).

## Enable the knowledge engine

The engine is off by default: no knowledge routes are registered (they 404) and `mode=knowledge` is rejected with HTTP 400 naming the registered modes. Turn it on in `.env` and restart the server:

```dotenv
INQTRIX_KNOWLEDGE_ENABLED=true
```

Ingestion and search embed text through an OpenAI-compatible `/embeddings` endpoint. By default the embedding provider reuses `LITELLM_BASE_URL` and `LITELLM_API_KEY`, with `INQTRIX_EMBEDDING_MODEL` (default `text-embedding-3-small`) as the model for new collections; `INQTRIX_EMBEDDING_BASE_URL` / `INQTRIX_EMBEDDING_API_KEY` override the endpoint independently. Each collection stores its embedding model immutably at creation.

Confirm the feature is live:

```bash
curl http://localhost:5100/v1/capabilities
```

`features.knowledge` must be `true`; `knowledge.profiles` lists the retrieval profiles this deployment offers, including any operator-degraded stages.

When `INQTRIX_SERVER_API_KEY` is set, add `-H "Authorization: Bearer sk-..."` to every request below.

## Create a collection and ingest text

```bash
curl -X POST http://localhost:5100/v1/knowledge/collections \
    -H "Content-Type: application/json" \
    -d '{"name": "Handbuch"}'
```

Returns HTTP 201 with `id`, `name`, `embedding_model`, `embedding_dim`, `document_count`, `created_at`. An optional `embedding_model` field picks a model from the deployment's selectable set; omitted, the configured default applies. Use the returned `id` (here `kc_...`) below.

```bash
curl -X POST http://localhost:5100/v1/knowledge/collections/kc_.../document-revisions \
    -H "Content-Type: application/json" \
    -d '{
        "title": "Urlaubsregelung",
        "text": "Mitarbeitende haben Anspruch auf 30 Tage Urlaub pro Jahr. Resturlaub verfaellt am 31. Maerz des Folgejahres."
    }'
```

The durable endpoint answers HTTP 202 with stable `document_id`, immutable
`revision_id`, `job_id`, `events_url`, and the initial job state. Chunking,
optional contextualization, embedding, validation, and publication happen in
the server-owned indexing job; closing the browser does not cancel it. Observe
the job through its `events_url` or current summary:

```bash
curl -N http://localhost:5100/v1/knowledge/indexing-jobs/ix_.../events
curl http://localhost:5100/v1/knowledge/indexing-jobs/ix_...
```

`title` and `text` are required for this direct-text example. Normal Library,
Chat, and Editor file flows first create a bound asset through `/v1/files`.
After its server-side parser has produced the canonical extract, pass the
asset's stable id as `"asset_id"` instead of `"text"`; the asynchronous route
does not accept a raw `file_id`. This prevents the browser and the worker from
creating competing parse results. The compatibility endpoint
`POST /v1/knowledge/collections/{id}/documents` still accepts `text` or
`file_id`, submits the same revision job, waits for its terminal state, and
returns the historical HTTP 201 document shape.

A dependency timeout or invalid contextualization response pauses the new
revision as `paused_dependency` or `paused_validation`; the currently active
revision remains searchable. `POST .../resume` continues from the first
unfinished checkpoint. A dependency pause also resumes on its own: after one
minute, then after growing waits up to 15 minutes, at most
`INQTRIX_REINDEX_AUTO_RESUME_ATTEMPTS` (5) times in a row without progress; the
job's `checkpoint.auto_resume` shows the attempts, the next due time, and why a
streak stopped. A validation pause always waits for a person.
`POST .../resume-raw` is an explicit user decision to
build this revision without generated retrieval context. Neither case is
reported as a hidden partial success. The provider's content filter does not
pause a revision: a batch it blocks twice, or a request it refuses, is indexed
without retrieval context, and the document metadata counts those chunks in
`_chunk_context_omitted`.

Optionally verify retrieval before involving an LLM — `POST /v1/knowledge/search` is the synchronous debugging surface:

```bash
curl -X POST http://localhost:5100/v1/knowledge/search \
    -H "Content-Type: application/json" \
    -d '{"query": "Wie viele Urlaubstage habe ich?", "collection_ids": ["kc_..."], "top_k": 5}'
```

## Ask via a native run

`POST /v1/runs` with `mode=knowledge` queues an answer run against your collections. The retrieval profile (`schnell` | `standard` | `gruendlich` | `tief` | `auto`) selects how much machinery runs per question — see [Retrieval profiles](../configuration/knowledge-profiles.md); an unknown value fails with HTTP 400.

```bash
curl -X POST http://localhost:5100/v1/runs \
    -H "Content-Type: application/json" \
    -d '{
        "mode": "knowledge",
        "question": "Wie viele Urlaubstage habe ich und wann verfaellt Resturlaub?",
        "knowledge_filters": {
            "collection_ids": ["kc_..."],
            "profile": "standard",
            "top_k": 8
        }
    }'
```

The response is HTTP 202 with the public run summary: `run_id`, `status` (`queued`), `events_url`, `result_url`, plus question/mode/timing fields.

## Watch the run and read the result

Stream the run's structured events as SSE (each frame is `event: <type>` plus a `data:` JSON line; the stream ends after a terminal event):

```bash
curl -N http://localhost:5100/v1/runs/run_.../events
```

| Event | Meaning |
|-------|---------|
| `inqtrix.run.queued` / `inqtrix.run.snapshot` | Lifecycle and state-patch events. |
| `inqtrix.knowledge.profile.resolved` | Which profile actually runs, with `degraded_stages`. |
| `inqtrix.knowledge.retrieval.completed` | Retrieval pass finished, candidate counts. |
| `inqtrix.knowledge.gate.evaluated` | Sufficiency-gate verdict per round. |
| `inqtrix.knowledge.answer.retry` | The single visible answer regeneration after unverified quotes (`attempt`, `quotes_total`, `quotes_unverified`); at most one per run, never silent, never carrying quote text. |
| `inqtrix.knowledge.grounding.checked` | Typed quote-verification verdict, bounded repair flag, and verified/total counts; it never carries quote or source text. |
| `inqtrix.run.completed` / `failed` / `cancelled` | Terminal; `completed` carries the `result_url`. |

Then fetch the result:

```bash
curl http://localhost:5100/v1/runs/run_.../result
```

For a completed run, the payload carries `answer` (Markdown with `[K1]`-style citation labels), `usage`, and the flat top-level keys `references` (label, title, URL per cited chunk), `knowledge_gate`, `knowledge_grounding`, and `knowledge_profile`. Reference URLs use the internal `inqtrix://documents/<id>` scheme by default — the export pipeline normalizes URLs and strips fragments, so the chunk index does not survive in the URL (it is still carried by the separate `chunk_index` field). Set `INQTRIX_PUBLIC_BASE_URL` to turn them into clickable `<base>/v1/sources/<id>?chunk=<n>` links, where the chunk index travels as a query parameter and is preserved. If grounding cannot parse the required sections or verify every labelled quote, the run is `failed` with a stable grounding error type and safe message; a completion the provider cut short or left empty fails as `knowledge_answer_incomplete` instead. No result endpoint publishes the rejected completion. On the in-memory backend, terminal runs stay fetchable only for `RUN_COMPLETED_TTL_SECONDS` (default 300); the Postgres backend persists them.

## The same flow in the UI

In the Research Desk ([First research run](first-research-run.md)), the Datenbank workspace manages collections: create a Sammlung, then add text or uploaded files to it. The Wissen workspace asks questions against selected collections with a retrieval-profile picker; citations open the document viewer on the ingested text. Both surfaces are capability-gated — they appear only when `/v1/capabilities` reports the knowledge engine.

## Next steps

- [Knowledge engine](../architecture/knowledge-engine.md#the-retrieval-model-in-one-pass) — what actually happened between your question and the cited answer: why two searches run, what fuses them, and which stage runs on which kind of engine.
- [Retrieval profiles](../configuration/knowledge-profiles.md) — what each profile does, when to pick which, and how operator ceilings clamp it.
- [Platform components](platform-components.md) — Qdrant persistence, durable runs, workers.
- [Runs API](../extending/runs-api.md) — the complete SSE event contract.

## Related docs

- [First research run](first-research-run.md)
- [Web server mode](../deployment/webserver-mode.md)
- [Settings and env](../configuration/settings-and-env.md)
