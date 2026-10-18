# Retrieval profiles (mode=knowledge)

> Files: `src/inqtrix/knowledge/profiles.py`, `src/inqtrix/knowledge/algorithm.py`, `src/inqtrix/knowledge/decompose.py`, `src/inqtrix/providers/rerankers.py`

## Scope

A retrieval profile is the per-request switch for how much machinery a
knowledge question runs through. The request names ONE profile in
`knowledge_filters.profile`; the pipeline stages (rerank, sufficiency
gate, vocabulary-bridge rewrite, query decomposition, report-form
synthesis) are bundled behind it. The architecture and data flow of those stages are in [Knowledge engine](../architecture/knowledge-engine.md). A request without a profile behaves
exactly like the pre-profile pipeline (`standard`).

```text
Question
  │  (profile "auto": zero-cost heuristic picks schnell/standard/gruendlich)
  ▼
Resolve plan  =  requested profile ∩ operator ceiling (env)
  ▼
┌──────────────────── the agentic loop ────────────────────┐
│ retrieve (dense + BM25 hybrid) ─► [rerank] ─► evidence   │
│      ▲                                  │                │
│ rewrite the query           [gate: sufficient?]          │
│ (vocabulary bridge)              │ no, rounds left       │
│      └───────────────────────────┘                       │
└────────────────│ yes ────────────────────────────────────┘
                 ▼
   answer (quotes first), verify quotes deterministically
   `tief` additionally: decompose into sub-queries before the loop,
   interleave the per-aspect results, answer as a sectioned report.
```

## The profile matrix

Resolved values assume the shipped defaults (`INQTRIX_RERANK_CANDIDATE_DEPTH`
40, `INQTRIX_KNOWLEDGE_TOP_K` 8, `INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS` 3).

| Stage | `schnell` | `standard` (API default) | `gruendlich` | `tief` (UI default) | Effect of enabling it |
|---|---|---|---|---|---|
| Rerank | off | on (if configured) | on, depth ×1.5 (**60**) | on, depth ×2.0 (**80**) | Reorders a deeper candidate pool with a model that sees question and passage together, so it can tell "about this topic" from "answers this question". Buys top-1 precision on paraphrase queries; costs one rerank roundtrip that grows with depth. Hard ceiling 200. Inert when `INQTRIX_RERANKER_PROVIDER=none`. |
| Gate rewrite rounds | 0 (no gate call) | 1 | up to 2 | up to `INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS` (**3**) | Each round is one fast-tier judgement plus, when it proposes a rewrite, one more retrieval pass. Buys a second chance when the first query missed the corpus vocabulary; costs latency even when the first pass was already sufficient. Exits early when a rewrite returns no new chunks. |
| Vocabulary bridge | – | off | ON | ON | Switches the rewrite prompt to translate everyday phrasing into the documents' technical/official vocabulary. **No extra call** — it is a variant of the gate rewrite that already runs, so it is free wherever the gate runs at all. |
| Query decomposition | – | – | – | ON (one fast-tier call) | Splits a multi-aspect question into 2–4 self-contained sub-queries, each retrieved separately and merged round-robin. Buys coverage on "X, Y and Z?" questions where one strong aspect would otherwise crowd the others out of the top-k; costs one call plus one retrieval per sub-query. |
| Evidence width (`final_k_factor`) | ×1.0 | ×1.0 | ×1.0 | **×2.0** | `tief` is the only profile that widens the evidence reaching the answer instead of collapsing its decompose/gate fan-out back to `top_k`. Capped by `EVIDENCE_K_MAX` (40). |
| Answer form | compact | compact | compact | sectioned report | `tief` pins a four-section skeleton whose headings follow the answer language (`summary`, `key_points`, `detail_analysis`, `evidence_base` in `HEADINGS` of `prompt_language.py`: Kurzfazit / Kernaussagen / Detailanalyse / Quellenlage in German, Summary / Key points / Detailed analysis / Evidence base in English); every other profile lets the model structure longer answers freely. |
| Grounding (quote check) | ON | ON | ON | ON | Never a profile switch — see below. |
| Typical LLM calls | 1 | 2 | 2–4 | 3–7 | Exactly one high-tier call (the answer) per request — two when a quote-unverified grounding rejection triggers the one allowed answer regeneration, which resolves through the same high-tier `knowledge_answer` node; every other call is fast-tier. |

Grounding stays on everywhere: verification is deterministic (no extra LLM
call) and fail-closed, so it costs nothing per question and only the operator
can remove it. A malformed quote block or one unverifiable labelled quote
terminates the answer with a typed, visible cause; it never becomes a
plain-answer fallback. `schnell` saves the gate call, the rerank roundtrip,
and any second retrieval pass — that is the entire latency win.

`auto` routes per question with zero-cost heuristics and NEVER picks `tief`.
The rules are pure length and regex arithmetic evaluated without a provider
call, and the first match wins:

| Order | Test | Result | Reason string |
|---|---|---|---|
| 1 | A strong enumeration marker: `sowie`, `jeweils`, `bzw`, `ausserdem`/`außerdem`, `sowohl`, `vergleich…`, `unterschied…` | `gruendlich` | `strong_enumeration_marker` |
| 2 | Two or more `?` | `gruendlich` | `multiple_questions` |
| 3 | More than 240 characters | `gruendlich` | `long_question` |
| 4 | Two or more standalone `und` | `gruendlich` | `repeated_und` |
| 5 | Under 80 characters **and** at most one `?` | `schnell` | `short_simple` |
| 6 | Anything else | `standard` | `default` |

A plain `und` is deliberately not a marker on its own: ordinary German
compounds ("Sicherheits- und Risikomanagement") would otherwise route nearly
every question to `gruendlich`, which is why repeated `und` is counted
separately in rule 4. Because the widening rules run first, a short question
carrying two question marks resolves to `gruendlich`, not `schnell`.

The chosen profile and the reason string travel in the
`inqtrix.knowledge.profile.resolved` event as the telemetry on which a
later LLM-escalation decision will be grounded.

## Choosing a profile

| Situation | Profile | Why |
|---|---|---|
| A lookup phrased in the corpus's own wording ("Was steht in Artikel 17?") | `schnell` | Retrieval alone is usually already right and the gate would only confirm it. One LLM call. |
| Everyday phrasing against a corpus that uses the same everyday vocabulary | `standard` | One gate round catches the case where the first retrieval missed, without paying for a bridge or a fan-out. |
| Everyday phrasing against legal, regulatory, or technical documents | `gruendlich` | The vocabulary bridge targets exactly this failure: the user writes "wie lange behalten", the document says "Aufbewahrungsfrist". |
| One question covering several aspects, or a structured result is wanted | `tief` | Decomposition stops one aspect from crowding out the rest, and it is the only profile that both doubles the evidence width and structures the answer. |
| Mixed traffic where cost matters | `auto` | Picks between `schnell` / `standard` / `gruendlich` from question shape alone — no LLM call and no added latency. Never picks `tief`. |

**Two defaults, deliberately.** A request that names no profile runs
`standard`, and `/v1/capabilities` publishes `default_profile: "standard"`
accordingly. The Research Desk profile picker pre-selects **`tief`** whenever
the deployment offers it, so an interactive user starts at the deepest profile
while an API caller starts at the cheapest. Operators sizing token budgets
should assume `tief` for browser traffic and `standard` for integrations.

**Reading the cost.** Only ingestion scales with corpus size. Every
per-question stage is bounded: `top_k` per (sub-)query, `final_k` evidence
entries (ceiling `EVIDENCE_K_MAX` = 40), a rerank pool bounded by
`INQTRIX_RERANK_CANDIDATE_DEPTH` × the profile factor (ceiling 200), and gate
rounds bounded by `INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS`. The worst case a single
request can reach is `tief` at the operator cap: one decompose call, four gate
calls (the opening judgement plus one per rewrite round), one answer call, and
at most one answer regeneration — five fast-tier calls and two high-tier calls,
because the regeneration resolves through the same high-tier `knowledge_answer`
node as the answer itself. Two further per-question calls sit outside the
profile and are not counted in that worst case: a conversational turn that
carries history pays one fast-tier contextualization call before retrieval, and
with `INQTRIX_RERANKER_PROVIDER=llm` every retrieval pass adds one fast-tier
rerank call — up to eight in a worst-case `tief` run (the question, up to four
sub-queries, and up to three rewrite retrievals). Which engine class runs each
stage, including the stages that are not model calls at all, is in
[Knowledge engine](../architecture/knowledge-engine.md#which-engine-owns-which-stage).

Per-stage token attribution is **not** available in the run result: `usage` is
a flat sum across contextualization, gate, decomposition, and answer, so a
per-stage breakdown has to come from traces rather than from the response.

## The two-level rule

Environment switches are the OPERATOR CEILING; the profile selects
within it:

* `INQTRIX_KNOWLEDGE_GATE=off` removes the gate from every profile.
* `INQTRIX_KNOWLEDGE_GROUNDING=off` removes quote verification
  everywhere.
* No reranker configured (`INQTRIX_RERANKER_PROVIDER=none`) removes
  the rerank stage everywhere.
* `INQTRIX_KNOWLEDGE_GATE_MAX_ROUNDS` (default 3, 1–5) caps the
  rewrite rounds of every profile; `tief` requests up to this cap.

A profile never enables what the ceiling forbids. Every clamp is
visible — `degraded_stages` appears in the `profile.resolved` event,
in `result_state.knowledge_profile`, and per profile in the
`/v1/capabilities` manifest (`knowledge.profiles[].degraded`). The UI
renders what would actually run; nothing degrades silently.

## Transport and validation

```json
POST /v1/runs   (or /v1/chat/completions)
{
  "mode": "knowledge",
  "question": "…",
  "knowledge_filters": {
    "collection_ids": ["kc_…"],
    "profile": "gruendlich",
    "top_k": 8,
    "final_k": 12
  }
}
```

Valid `profile` values: `schnell`, `standard`, `gruendlich`, `tief`,
`auto`. An unknown value fails the request with HTTP 400 naming the
valid set — a typo never silently runs a different profile.

`top_k` (per-query retrieval width) and `final_k` (the number of
evidence chunks that reach the answer) are orthogonal overrides under
every profile. Both are validated at the one resolver chokepoint and
fail with HTTP 400 when not a plain integer in range — `top_k` in
`1..50` (mirrors `INQTRIX_KNOWLEDGE_TOP_K`), `final_k` in
`1..EVIDENCE_K_MAX`. Without `final_k` the surfaced-evidence count is
`min(top_k * profile.final_k_factor, EVIDENCE_K_MAX)` (only `tief`
raises the factor above `1.0`); an explicit `final_k` pins it directly,
overriding the factor. The ceiling `EVIDENCE_K_MAX` and every profile's
`final_k_factor` are published in the `/v1/capabilities` manifest
(`knowledge.evidence_k_max`, `knowledge.profiles[].final_k_factor`) so a
client can render the effective `final_k` and bound its override.

Note: `final_k` can exceed `top_k`. The wider candidate pool comes from
the breadth of the run, not from `top_k` — query decomposition (`tief`)
fans out into sub-queries each retrieving `top_k`, the reranker selects
`final_k` from a far deeper candidate pool, and gate rewrite rounds add
more. For a profile without decomposition, the single retrieval is run
at `final_k` directly (so `top_k` only governs per-sub-query width).

## Stage notes

* **Vocabulary bridge** — the gate rewrite prompt variant that
  translates everyday phrasing into the documents' technical/official
  vocabulary (the measured DORA d20 failure class). It lives INSIDE
  the gate rewrite: there is exactly one place in the pipeline where
  queries are rewritten. Enabled in `gruendlich`/`tief`; promotion
  into `standard` is a deliberate re-baselining decision after the
  DORA answer eval shows gains without a false-refusal regression.
* **Query decomposition** (`knowledge/decompose.py`) — splits
  multi-aspect questions into 2–4 self-contained sub-queries (it
  splits, never reformulates — that distinction keeps it out of the
  rewrite location). Results merge round-robin so every aspect
  contributes to the top-k (the BSI b30 aggregation fix). Unparseable
  responses degrade to a no-op with the loud
  `_knowledge_decompose_fallback` marker.

## Evaluation

The answer eval (the only eval that exercises the gate) parametrizes over
`INQTRIX_EVAL_GOLDEN_SET` × `INQTRIX_EVAL_KNOWLEDGE_PROFILE` and keys baselines
`(model, tier, profile)` — tiers, thresholds, and the commands are in
[Knowledge engine](../architecture/knowledge-engine.md#evaluation).

## Related docs

- [Knowledge engine](../architecture/knowledge-engine.md) — what each stage does, why the pipeline has this shape, which class of engine runs each step, and how the engine is operated: collections, ingestion, the Wissen workspace, and the evaluation tiers.
- [Settings and environment](settings-and-env.md) — every `INQTRIX_KNOWLEDGE_*` / `INQTRIX_EMBEDDING_*` / `INQTRIX_RERANKER_*` variable that forms the operator ceiling.
- [LLM calls, model tiers, and reasoning effort](../architecture/websearch/nodes.md) — which tier resolves each `knowledge_*` call site.
