# Model cards

## Scope

How Inqtrix describes selectable LLMs to the UI: the curated **model-card
catalogue** (`src/inqtrix/model_cards.py`), how a provider offers concrete
models for direct selection (`selectable_models`), how a per-run model/effort
override reaches the wire, and how the catalogue is exposed to and rendered by
the React app (the model picker and the composer token meter). It does **not**
cover the algorithm's internal high/mid/fast tier routing — that stays in
[`../architecture/websearch/nodes.md`](../architecture/websearch/nodes.md); model cards are
an additive, optional layer on top.

## What a model card is

A *model card* is provider-neutral metadata for one logical model. One card per
model, regardless of how many providers serve it. The card carries the
UI-facing facts plus a flat `aliases` list of every provider-specific identifier
string that should resolve to it, so the same logical model resolves to one card
whether called via the Anthropic API (`claude-opus-4-8`), Amazon Bedrock
(`anthropic.claude-opus-4-8`, optionally region-prefixed `eu.`/`us.`/`apac.`/`ap.`
and version-suffixed `-v1`/`-v2` with an optional `:0` revision), or an Azure
deployment.

| Field | Behaviour |
|---|---|
| `id`, `display_name`, `vendor` | Identity. The UI always shows `display_name`, never a raw versioned id. |
| `category` (`high`/`mid`/`fast`) | Display grouping in the picker. Independent of latency and of node-tier routing. |
| `speed` | TEMPO tile label (`langsam`/`mittel`/`schnell`); decoupled from `category`. |
| `description` | Required one-line, user-facing summary of what the model is good for. Rendered under the name and in the hover-card. |
| `context_window_tokens`, `max_output_tokens` | Capacity. Drive the token meter (usable input ≈ window − reserved output − safety) and two server-side budgets: the Agent Desk kernel's compaction threshold is `context_window_tokens × INQTRIX_AGENT_KERNEL_CONTEXT_TRIGGER_FRACTION` (default `0.75`), floored at 128 000 tokens — a model with no card gets the bare floor — and knowledge contextualisation sizes its batches from the card's window and `max_output_tokens` before falling back to the provider values. |
| `reasoning_levels` | Effort tokens the model accepts (incl. `none` only if reasoning can be disabled). Empty ⇒ no effort control (e.g. Haiku), so the picker hides the reasoning selector. |
| `capabilities` | Chips in the hover-card (`reasoning`/`code`/`tool_use`/`vision`). |
| `input_modalities` | Accepted input modalities (e.g. `text`, `image`). Defaults to `["text"]`. |
| `pricing` | List $/MTok. The picker derives a `$`..`$$$$` tier + exact value, and the usage ledger prices every `chat`/`text_completion` call from it: `input_tokens/1e6 * input_per_mtok + output_tokens/1e6 * output_per_mtok`. |
| `aliases` | Every identifier that resolves to this card (matched after Bedrock region/version normalisation). |
| `knowledge_cutoff` | Coarse `YYYY-MM` knowledge cutoff, or `None`. Shown for context; never used as a default. |
| `source_url`, `last_verified` | Provenance — facts are curated, not guessed. |

## Offering models for direct selection (optional)

Every LLM provider accepts an **optional** `selectable_models` list. Empty (the
default) keeps the UI on the high/mid/fast tier picker — a web-search-only
library user needs no model configuration at all. When set, the UI offers those
concrete models, grouped by each card's `category`.

```python
from inqtrix import AnthropicLLM

# Direct API providers — Anthropic here; OpenAI direct goes through `LiteLLM`
# with `openai/`-prefixed ids: ship the latest models as defaults.
llm = AnthropicLLM(
    api_key=ANTHROPIC_API_KEY,
    default_model="claude-opus-4-8",
    tier_high_model="claude-opus-4-8",
    tier_mid_model="claude-sonnet-4-6",
    tier_fast_model="claude-haiku-4-5",
    context_window_tokens=1_000_000,
    selectable_models=["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
)
```

Bedrock uses region-prefixed ids; they normalise onto the same cards:

```python
BedrockLLM(
    region_name="eu-central-1",
    default_model="eu.anthropic.claude-opus-4-8-v1:0",
    selectable_models=[
        "eu.anthropic.claude-opus-4-8-v1:0",
        "eu.anthropic.claude-sonnet-4-6-v1:0",
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    ],
)
```

Azure deployment names are operator-chosen — **name each deployment after the
canonical model id** so the catalogue resolves it (see
[`../../examples/webserver_stacks/azure_foundry_web_search.py`](../../examples/webserver_stacks/azure_foundry_web_search.py)):

```python
AzureOpenAILLM(
    azure_endpoint=AZURE_ENDPOINT,
    default_model="gpt-5.4",
    selectable_models=["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"],
)
```

In webserver/env mode the same list comes from `INQTRIX_SELECTABLE_CHAT_MODELS`
(comma-separated model ids, default empty) rather than from a constructor.
`create_providers` forwards it as `selectable_models` only when it is non-empty —
which is exactly why an unset variable leaves the tier picker in place. See
[Settings and environment](settings-and-env.md) for the variable and
[Provider recipes](../getting-started/provider-recipes.md) for per-provider
examples.

## Per-run model + effort override

A request may pick a concrete model and reasoning effort via
`agent_overrides.model` and `agent_overrides.effort` (HTTP), mirrored by
`AgentSettings.model`/`effort` and `AgentConfig.model`/`effort`.

Both wire fields are strictly validated — the `agent_overrides` model sets
`extra="forbid"`, so a misspelt key is a 400 rather than a silent no-op: `model`
is a string of 1–200 characters, and `effort` must be one of `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`. Which of those a given model actually
accepts is the card's `reasoning_levels`; the corresponding library fields
(`AgentSettings.model`/`effort`, aliases `MODEL_OVERRIDE`/`EFFORT_OVERRIDE`, and
`AgentConfig.model`/`effort`) default to the empty string.

An explicit model short-circuits tier resolution in `describe_resolution` with
`model_source="explicit_request"` (visible in the forensic log). The override is
**scoped to the reasoning call sites**: the direct-chat answer, the
editor-assist endpoints, the knowledge-mode answer, the kernel's conversation
turn and final review, and a Mission's
`agent_plan`/`agent_synthesis`/`agent_deep_review`/`agent_answer`/`agent_answer_light` nodes; every
other call site keeps tier routing — the research graph's
`classify`/`claim_extract`/`plan`/`evaluate`/`answer` nodes, the knowledge
retrieval nodes (`knowledge_gate`, `knowledge_contextualize`,
`knowledge_decompose`, `knowledge_rerank`) and the agent assembly-line calls
(intake, discovery analyst, contradiction, sufficiency, file analysis,
patch, skill-point check, memory reflection) — because one model
cannot stand in for the classify/plan/evaluate/answer split. `search` is not an
LLM call site at all: `/health`'s `search_model` is reported by the search
provider, not by tier resolution.

Mission children inherit the parent's selected model/tier/effort, including
effective skill pins, for their planning, writing and review. Research children
keep their own routing. The kernel's automatic Deep effort increase is local
to its loop and revision; neither its final review nor a mission child treats
that increase as an explicit selection. Selecting a concrete model without an
effort uses the provider default for review. Selecting `none` explicitly disables
additional reasoning where the provider supports that setting.

## How the catalogue reaches the UI

`/health` returns `models_catalog` (one `{model_id, card}` entry per selectable
model; `card` is `null` for an unknown id) and `context_window_tokens`
(provider-level, `null` when unknown) as top-level keys. `/v1/stacks` carries the
same two keys one level down, per stack, at `stacks[].models.models_catalog` and
`stacks[].models.context_window_tokens`. The React picker
renders models grouped by `category` with an `i` hover-card (KONTEXT/TEMPO/KOSTEN
tiles + capability chips) and a model-dependent reasoning selector derived from
the card's `reasoning_levels`: one option per supported level, labelled
`Off`/`Min`/`Low`/`Med`/`High`/`XHigh`/`Max` for the effort tokens
`none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`. The first four levels render
as an inline segmented control and any remaining ones collapse into a `⋯`
overflow menu; levels are never folded into fixed buckets, and an empty
`reasoning_levels` hides the selector entirely. The composer token meter uses the
selected card's `context_window_tokens` as its capacity.

In webserver/env mode the provider-level `context_window_tokens` is not operator
configuration: `create_providers` resolves the card for `REASONING_MODEL` and
passes that card's `context_window_tokens` to the provider constructor. An
unknown `REASONING_MODEL` leaves it unset, which is what surfaces as `null` — and
without the derivation the editor assumed a 16k window and truncated report
context.

## Unknown model (no card on file)

If a selectable model has no matching card, nothing is fabricated:
`resolve_model_card` returns `None`, the `/health` entry has `card: null`,
the picker shows the raw id in an "Ohne Kategorie" group with a muted "Keine
Karte" badge (no hover stats, no reasoning selector), and the token meter keeps
estimating but reports "context window unknown" with no percentage. Sending is
still allowed.

The run is **unpriced**, though: `usage_cost_usd` returns `None` for a model
with no card — "not priceable, never free" — so its tokens are booked to
`UsageCostSummary.unpriced_input_tokens`/`unpriced_output_tokens` and the summary
reports `is_complete = False` instead of being folded into the cost figure. A
selectable model without a card therefore drops out of the cost report until the
card exists.

The fix is one entry in `MODEL_CARDS` — no code change.

## Adding a model

Append one `ModelCard` to `MODEL_CARDS` in `src/inqtrix/model_cards.py`. Nine
fields are required: `id`, `display_name`, `vendor`, `category`, `speed`,
`description`, `context_window_tokens`, `max_output_tokens` and `pricing` —
leaving out `description` or `speed` is a construction-time `ValidationError`,
not a cosmetic gap. The optional rest: `aliases` (include every provider id),
`reasoning_levels`, `capabilities`, `input_modalities` (defaults to `["text"]`),
`knowledge_cutoff`, and `source_url`/`last_verified`. No other code changes are
needed. Claude facts come from the Anthropic models overview; OpenAI/Google from
the respective provider docs.

## Related docs

- [LLM calls](../architecture/websearch/nodes.md) — node/tier routing the cards layer on top of.
- [Agent config](agent-config.md) — `model_tier`, `model`, `effort` fields.
- [Webserver mode](../deployment/webserver-mode.md) — `/health` and `/v1/stacks` fields, `agent_overrides`.
- [React UI](../deployment/react-ui.md) — the editor workspace, the Agent Desk
  composer, deployment paths and the runtime API boundary. The model picker and
  token meter are described on this page, not there.
