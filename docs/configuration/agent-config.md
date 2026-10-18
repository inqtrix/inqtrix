# AgentConfig

> File: `src/inqtrix/agent.py`

## Scope

`AgentConfig` is the configuration object `ResearchAgent` takes at construction time, but not the only one it consumes: `AgentSettings` is read from `.env` and the process environment on the first `research()` / `stream()` call, then cached for the lifetime of the agent instance, and supplies every field the caller left unset, and a missing `llm` or `search` additionally pulls the `models` and `server` sections of `Settings` for provider auto-creation. `AgentConfig` covers agent behaviour (rounds, confidence, report profile), timeouts, cache settings, and optional provider/strategy injection. Server-only deployment settings stay on `ServerSettings` (see [Settings and env](settings-and-env.md)).

This page describes the Python library config in `src/inqtrix/agent.py`.
HTTP server mode uses the same agent defaults through `Settings` and optional
provider injection in `create_app(...)`.

Whether `AgentConfig` can select a different algorithm or graph topology is
answered in [Library API](../architecture/library-api.md) and
[Architecture overview](../architecture/overview.md).

## Minimal usage

```python
from inqtrix import AgentConfig, LiteLLM, ReportProfile

AgentConfig(
    llm=LiteLLM(api_key="...", default_model="gpt-4o"),
    report_profile=ReportProfile.DEEP,
    max_rounds=3,
    confidence_stop=7,
)
```

Model names live on provider constructors, not on `AgentConfig`. Leaving `llm=None` or `search=None` triggers auto-creation from environment variables on the first `research()` call.

## Configuration hierarchy

Precedence, highest wins:

1. Explicit scalar fields on `AgentConfig` — always override everything else.
2. `AgentSettings` (Pydantic, loaded from `.env` and process environment; see [Settings and env](settings-and-env.md)) — read on the agent's first run and cached for the instance's lifetime, and read regardless of whether providers are auto-created. Injecting `llm` and `search` explicitly does **not** cut the environment out; it only skips the additional `models` / `server` lookup that auto-creation needs.
3. The report-profile preset (`AgentSettings.with_report_profile_defaults`) — fills only the fields that neither the caller nor the environment set explicitly. `SCHNELL` yields `max_rounds=1`, `min_rounds=1`, `confidence_stop=6`, `first_round_queries=6`, `answer_prompt_citations_max=40`; `COMPACT` yields `2` / `1` / `7` / `6` / `60`; `DEEP` yields `4` / `2` / `8` / `8` / `500`.
4. Code defaults (`src/inqtrix/constants.py`): `max_rounds=2`, `min_rounds=1`, `confidence_stop=7`, `first_round_queries=6`.

## How fields affect the procedure

| Field family | Changes control flow? | Effect |
|---|---:|---|
| Providers (`llm`, `search`) | No topology change | Decide which external backends perform LLM and search calls. |
| Strategies | No topology change | Change policy inside nodes: tiering, extraction, consolidation, risk/aspects, stop heuristics. |
| Loop settings (`max_rounds`, `min_rounds`, `confidence_stop`, `first_round_queries`) | Yes | Change when the loop continues, stops, or how broad round 0 is. |
| Citation budget (`answer_prompt_citations_max`) | No | Inert: no code reads the field. The answer's citation allowlist is the set of URLs whose source blocks stayed visible under `prompt_evidence_total_char_budget`, and the link sanitizer strips every URL outside it. |
| Timeouts | No | Bound provider calls and whole-run wall time. |
| Model tiers + per-node model overrides | Indirect | Map each node to a high/mid/fast model and reasoning effort. Configured on the provider; see [LLM calls](../architecture/websearch/nodes.md). |
| Report profile | Indirect | Applies a bundle of answer-depth and evidence-retention defaults unless explicitly overridden. |

## Fields

### Providers and strategies

| Field | Type | Purpose |
|-------|------|---------|
| `llm` | `LLMProvider \| None` | Explicit LLM provider. `None` → auto-create from `Settings`. |
| `search` | `SearchProvider \| None` | Explicit search provider. Same auto-create behaviour. |
| `source_tiering` | `SourceTieringStrategy \| None` | Replace the default tiering (the structural floor of registry-class suffixes; roles otherwise come from the extractor). Where an organisation's trust list belongs. See [Source tiering](../architecture/websearch/evidence.md). |
| `claim_extraction` | `ClaimExtractionStrategy \| None` | Replace the LLM claim extractor. A custom extractor may also judge sources: implement `consume_source_roles()` (default: an empty list; sources not placed by the tiering strategy stay `unjudged`). Accept the optional `fixed_roles` mapping to see placements already fixed by the run: a source-role name or `strategy:<tier>` when a custom tier has no role. Return roles only for unfixed sources. |
| `claim_consolidation` | `ClaimConsolidationStrategy \| None` | Replace claim dedup / consolidation. |
| `risk_scoring` | `RiskScoringStrategy \| None` | Replace risk scoring and the classify fallback's query-type inference. |
| `stop_criteria` | `StopCriteriaStrategy \| None` | Replace the stop heuristic cascade. |

### Agent behaviour

| Field | Default | Purpose |
|-------|---------|---------|
| `report_profile` | `ReportProfile.COMPACT` | Answer depth: `SCHNELL` runs exactly one research round for the lowest latency; `COMPACT` keeps the concise format; `DEEP` raises evidence context and answer budgets. See [Report profiles](report-profiles.md). |
| `max_rounds` | `2` | Maximum research-loop iterations. |
| `min_rounds` | `1` | Minimum research rounds before an already-triggered stop may be accepted; earlier stops are suppressed unless `max_rounds` is reached. |
| `confidence_stop` | `7` | Stop threshold for `final_confidence` in evaluate. See [Stopping](../architecture/websearch/stopping.md). |
| `first_round_queries` | `6` | Query count for round 0. Round 1+ uses `max(6, first_round_queries - 2)` so planning slots and executed search calls stay aligned. |
| `answer_prompt_citations_max` | `60` | Inert: no code reads it. What actually bounds the answer's citations is the evidence overview's character budget (`prompt_evidence_total_char_budget`: 64 000 SCHNELL / 100 000 COMPACT / 180 000 DEEP) — only URLs whose source block fits it stay citable — plus the link sanitizer that removes every other URL from the body. |
| `required_context_window_tokens` | `128_000` | Minimum model context-window size in tokens required for DEEP / forensic runs. A known smaller window blocks normal report synthesis; an unknown window produces a visible capacity warning. |
| `max_question_length` | `60_000` | Validation guardrail on user input. Generous because the chat composer inlines attached file content into the message. |

### Timeouts

| Field | Default (s) | Purpose |
|-------|-------------|---------|
| `max_total_seconds` | `3600` | Outer active-run budget. See [Timeouts and errors](../observability/forensic-cookbook.md#timeouts-and-errors). |
| `reasoning_timeout` | `600` | Logical reasoning-operation budget, shared by at most three attempts. |
| `editor_assistant_timeout` | `600` | Logical editor-operation budget, independent from research reasoning. |
| `search_timeout` | `600` | Logical search-operation budget, shared by at most three attempts. |
| `claim_extract_timeout` | `600` | Logical claim-extraction budget, shared by at most three attempts. |

### Risk scoring

| Field | Default | Purpose |
|-------|---------|---------|
| `high_risk_score_threshold` | `4` | Risk score at and above which a question is flagged `high_risk`. The flag is an observability signal only (forensic events, `/health`, follow-up preservation); it does not change model selection and drives no query/answer heuristic. Use model tiers or a per-node model override to put demanding questions on a stronger model — see [LLM calls](../architecture/websearch/nodes.md). |

### Model tier, model, and effort

| Field | Default | Purpose |
|-------|---------|---------|
| `model_tier` | `""` | Per-run model-tier selection (`high` / `mid` / `fast`); replaces mid/high default assignments. Fast nodes keep their tier and effort; explicit per-node model overrides still win. Tier models are configured on the provider (constructor args / env), not on `AgentConfig` — see [LLM calls](../architecture/websearch/nodes.md). |
| `model` | `""` | Explicit model id for the direct-chat answer (the UI model picker choosing a concrete model instead of a tier). A non-empty value bypasses tier routing for the direct-chat call only; the research pipeline keeps tier routing. Pair with `effort`. |
| `effort` | `""` | Reasoning effort for the direct-chat answer, paired with `model` (`none` / `low` / `medium` / `high` / `xhigh` / `max`, model-dependent). Empty inherits the provider default. |

### Search cache

| Field | Default | Purpose |
|-------|---------|---------|
| `search_cache_maxsize` | `256` | LRU capacity. |
| `search_cache_ttl` | `3600` | Entry lifetime in seconds. |

### Convenience

| Field | Default | Purpose |
|-------|---------|---------|
| `testing_mode` | `False` | Exposes `/v1/test/run` in server mode and enables top-level `iteration_logs` export in test/parity payloads. |
| `observability_profile` | `"summary"` | Detail level of the protected run audit: `summary` keeps the compact INFO/DEBUG behaviour, `debug` is reserved for future mid-level diagnostics, `forensic` emits provider-neutral source, citation, claim, stop, and answer lineage into the protected audit. Ordinary logs receive only its content-minimized projection. |

## Common setups

### A: Compact default

```python
agent = ResearchAgent()  # env-based, compact profile
```

### B: Strict budget

```python
agent = ResearchAgent(AgentConfig(
    llm=LiteLLM(api_key="sk-...", default_model="gpt-4o-mini"),
    search=PerplexitySearch(api_key="..."),
    max_rounds=2,
    confidence_stop=7,
    max_total_seconds=120,
    reasoning_timeout=45,
    claim_extract_timeout=30,
))
```

Two setups live on other pages: a deep report with explicitly constructed
providers in
[Library mode](../deployment/library-mode.md#option-b-explicit-providers-baukasten-pattern),
and a full custom-strategy implementation in
[Source tiering](../architecture/websearch/evidence.md#2-replace-the-strategy).

## Per-request overrides (HTTP only)

Server callers can override a whitelisted subset of fields per request via `body["agent_overrides"]`:

- `max_rounds`, `min_rounds`
- `confidence_stop`
- `report_profile`
- `depth`, `agent_tier`
- `max_total_seconds`
- `first_round_queries`
- `skip_search`
- `model_tier`, `model`, `effort`

Provider selection and strategy slots are intentionally **not** overridable per request. See [Web server mode](../deployment/webserver-mode.md).

Example:

```json
{
  "model": "research-agent",
  "messages": [{"role": "user", "content": "Explain Retrieval Augmented Generation"}],
  "agent_overrides": {
    "report_profile": "compact",
    "skip_search": true
  }
}
```

`skip_search=true` bypasses the plan/search/evaluate loop for that request and produces an uncited direct LLM answer. It remains available for compatibility with existing callers. New HTTP/API clients should prefer the top-level `mode` field: `mode="direct_llm"` for direct provider chat, or `mode="research"` to force the full graph even when a server default has `skip_search=true`.

## Related docs

- [Settings and env](settings-and-env.md)
- [Report profiles](report-profiles.md)
- [Providers overview](providers.md)
