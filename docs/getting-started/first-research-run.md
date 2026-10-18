# First research run

> Files: `src/inqtrix/__main__.py`, `src/inqtrix/settings.py`, `apps/research-desk/vite.config.ts`

## Scope

The shortest viable path from a freshly installed repo to a real research answer — server, library, or browser UI. Everything on this page runs with the zero-infrastructure defaults: in-memory storage, in-memory queue, knowledge engine off, no containers.

## Prerequisites

- Editable install working (see [Installation](installation.md)).
- A local `.env` with at least one LLM key and one search key. Copy the template:

  ```bash
  cp .env.example .env
  # edit .env
  ```

- For other provider combinations (Anthropic, Azure, Bedrock, Azure Foundry search), see [Provider recipes](provider-recipes.md) — they are selectable purely via `.env`.

## The minimal `.env`

The env-only server auto-creates a `LiteLLM` provider (any OpenAI-compatible gateway) and a `PerplexitySearch` provider. Five variables are enough:

```dotenv
# .env
LITELLM_BASE_URL=http://localhost:4000/v1
LITELLM_API_KEY=sk-...
REASONING_MODEL=gpt-4o
SEARCH_MODEL=perplexity-sonar-pro-agent
PERPLEXITY_API_KEY=pplx-...
```

No infrastructure variables are needed: `INQTRIX_STORAGE_BACKEND` and `INQTRIX_QUEUE_BACKEND` default to `memory`, and the knowledge engine is off by default. To run a different stack (Anthropic, Azure, Bedrock, Azure Foundry search), set `INQTRIX_LLM_PROVIDER` / `INQTRIX_SEARCH_PROVIDER` — copy-paste `.env` recipes are in [Provider recipes](provider-recipes.md). The Python-wired variants still live in [`examples/webserver_stacks/`](../../examples/webserver_stacks) for library mode.

## Path A: HTTP server

```bash
# uv
uv run python -m inqtrix

# or, after `python -m pip install -e .`
python -m inqtrix
```

The server listens on port 5100. In a second shell:

```bash
curl -N http://localhost:5100/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "research-agent",
        "messages": [
            {"role": "user", "content": "Was ist der aktuelle Stand der GKV-Reform?"}
        ],
        "stream": true
    }'
```

The response is an OpenAI-compatible SSE stream: progress chunks first, then answer chunks, terminated by `data: [DONE]`. Pass `"include_progress": false` for answer-only SSE.

See [Web server mode](../deployment/webserver-mode.md) for authentication, per-request overrides, multi-stack serving, and SSE details.

## Path B: Library

```python
from inqtrix import ResearchAgent

agent = ResearchAgent()
result = agent.research("Was ist der aktuelle Stand der GKV-Reform?")

print(result.answer)
print(f"Confidence: {result.metrics.confidence}/10")
print(f"Sources: {result.metrics.total_citations}")
print(f"Rounds: {result.metrics.rounds}")
```

Run the script with `uv run python main.py`, or with `python main.py` after a
normal pip installation. The same `.env` feeds the auto-created providers. See
[Library mode](../deployment/library-mode.md) for the explicit-providers
variant and streaming.

## Path C: Research Desk UI

With the server from Path A running, start the React frontend (Node 22.12+ and
npm 10.9+, see [Installation](installation.md)):

```bash
# from the repository root
npm run ui:dev
# -> http://127.0.0.1:5173
```

The Vite dev server proxies `/health`, `/v1`, and `/api` to `http://localhost:5100`, so browser fetches stay same-origin. The composer creates native `/v1/runs` jobs and streams their events live. Point the proxy at a non-default backend with `VITE_INQTRIX_API_BASE_URL` — see [`apps/research-desk/README.md`](../../apps/research-desk/README.md).

To explore the UI without any backend or API keys, open Settings (Einstellungen), section Preferences, and switch on demo mode: it loads a realistic sample workspace for presentations. Toggling it replaces the current browser workspace, so running searches and unsaved changes are lost.

## What a good answer looks like

A healthy run prints the final Markdown answer followed by a stats footer:

```text
---
*18 Quellen recherchiert · 9 Suchen · 3 Runden · 45s · Confidence 8/10*
```

If you see confidence pinned at 8 with open sub-questions, that is the coverage cap (see [Sub-question coverage](../architecture/websearch/stopping.md#sub-question-coverage)): it fires only above 8 and clamps to exactly 8. A run that sits at 6 or 7 usually means no guardrail fired at all: the evaluator's value is written through unchanged, and every clamp is conditional — the low-tier-source majority and the two-or-more-contested-claims guardrails only engage above 7, the missing-citations and unbacked-evidence caps only above 6. Look for a reason tag in the iteration log before assuming a rule held the value down. If confidence stays at 1–4 across rounds, the default `compact` profile simply runs out its two rounds: falsification can only arm in the final evaluate — with no further planning round left to act on it — and stagnation is suppressed in exactly that case, so raise `MAX_ROUNDS` or set `REPORT_PROFILE=deep` to give either mechanism room to work (see [Falsification mode](../architecture/websearch/stopping.md#falsification-mode)).

## What to do when the answer looks wrong

- Turn on logging: `INQTRIX_LOG_ENABLED=true`, `INQTRIX_LOG_LEVEL=INFO`. Use `OBSERVABILITY_PROFILE=forensic` for protected source/citation/claim/answer audit lineage; `INQTRIX_LOG_LEVEL=DEBUG` mirrors only its content-minimized IDs, states and metrics into the ordinary log. See [Logging](../observability/logging.md).
- Read the iteration log for the run (testing mode) — the markers explain every decision. See [Iteration log](../observability/iteration-log.md).
- Look for provider errors in the log (`AnthropicAPIError`, `AzureOpenAIAPIError`). Perplexity raises no error class of its own: it logs `Perplexity-WebSearch fehlgeschlagen (status=…, type=…)`, records a non-fatal notice and returns an empty result. See [Debugging runs](../observability/forensic-cookbook.md#debugging-runs).
- Re-run with a different stack (for example `examples/webserver_stacks/bedrock_perplexity.py`) to isolate whether the problem is provider-specific.

## Next steps

- [Stack quickstart](stack-quickstart.md) — run the whole stack with one `docker compose up`.
- [Platform components](platform-components.md) — Postgres, object store, Qdrant, workers, OIDC: do you need them?
- [First knowledge answer](first-knowledge-answer.md) — cited answers over your own documents.
- [Web server mode](../deployment/webserver-mode.md) — the OpenAI-compatible HTTP surface.

## Related docs

- [Overview](overview.md)
- [Installation](installation.md)
- [Providers overview](../configuration/providers.md)
