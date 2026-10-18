# Report profiles

> Files: `src/inqtrix/report_profiles.py`, `src/inqtrix/agent.py`

## Scope

`ReportProfile` is the public switch for answer depth. It controls evidence context density, claim breadth, and answer section layout without changing provider wiring or provider token defaults. Three profiles ship out of the box.

## The three profiles

| Profile | Enum | Optimised for | Answer sections |
|---------|------|---------------|-----------------|
| Schnell | `ReportProfile.SCHNELL` | Quick lookups and agent child runs — one round, lowest latency | 2 (`summary`, `key_points`) |
| Compact | `ReportProfile.COMPACT` | Fast Q&A, chat UIs, cost control | 4 |
| Deep | `ReportProfile.DEEP` | Review-style reports, research briefings | 6 |

Answer length is not configured. `AnswerSectionSpec.length_guidance` is a soft qualitative hint phrased for the model and enforces no fixed token or word count (`report_profiles.py:52-53`); DEEP's longest section is explicitly steered by evidence depth rather than a word limit.

### Knobs per profile

The profile sets the following knobs on a round-by-round basis. The table
below mirrors `_COMPACT_TUNING` and `_DEEP_TUNING` in `report_profiles.py`;
`schnell` has no column because `_SCHNELL_TUNING` carries its own values, at
or below the COMPACT column on every knob (`max_rounds=1`,
`confidence_stop=6`, `answer_prompt_citations_max=40`,
`answer_claim_prompt_items=12`, `claim_input_char_limit=16000`,
`claim_citation_cap=6`, `claim_max_items=6`, `claim_source_url_cap=3`,
`claim_ledger_cap=200`, `materialize_max_total=16`,
`materialize_max_unverified=6`, `prompt_evidence_record_char_limit=1800`,
`prompt_evidence_total_char_budget=64000`, `evaluate_evidence_record_char_limit=900`,
`evaluate_evidence_total_char_budget=40000`, `min_report_eligible_evidence=2`;
`min_rounds`, `first_round_queries`, the four timeouts and `max_total_seconds`
match COMPACT).

| Knob | COMPACT | DEEP | Effect |
|---|---|---|---|
| `max_rounds` | 2 | 3 | Hard cap on research rounds. |
| `min_rounds` | 1 | 2 | Minimum completed rounds before early-stop is allowed. |
| `confidence_stop` | 7 | 7 | Evaluator-confidence threshold for the stop cascade. |
| `first_round_queries` | 6 | 6 | Broad search fan-out in Round 0. DEEP keeps the COMPACT breadth; its depth comes from the mandatory second round, the third-round headroom and the larger evidence budgets. |
| `max_total_seconds` | 3600 | 3600 | Active research-run deadline; profile depth does not change the timeout contract. |
| `reasoning_timeout` / `editor_assistant_timeout` / `claim_extract_timeout` / `search_timeout` | 600 | 600 | Logical operation budget; all attempts share this one deadline. |
| `claim_input_char_limit` | 24 000 | 48 000 | Maximum source text length (chars) forwarded to a single claim-extraction call. |
| `answer_prompt_citations_max` | 60 | 500 | Set per profile and settable via `AgentConfig`, but no component reads it -- the answer prompt's citation allowlist is `EvidenceOverview.allowed_urls`, so this value currently caps nothing. |
| `answer_claim_prompt_items` | 20 | 40 | Maximum consolidated claims rendered into the evaluator prompt's `CLAIM-LEDGER` block (`nodes.py:4883-4896`, inside `evaluate`). Despite the name, the answer prompt never reads it. |
| `claim_max_items` | 8 | 24 | Maximum claims extracted per source by the claim-extraction LLM (`nodes.py:3717`). SCHNELL uses 6. |
| `claim_citation_cap` | 8 | 20 | Maximum citations included with each extracted claim batch (`nodes.py:3658`, `nodes.py:3716`). |
| `claim_source_url_cap` | 4 | 6 | Maximum source URLs attached to a single consolidated claim (`nodes.py:3718`, `nodes.py:3964`). |
| `claim_ledger_cap` | 400 | 800 | Maximum entries kept in the consolidated claim ledger across the whole run; older entries are dropped from the front (`nodes.py:4140-4155`). |
| `materialize_max_total` | 24 | 48 | Maximum size of the compact `consolidated_claims` selection used for the evaluator's claim-list hint and public top claims. Verification, quality counters, cross-check targets and answer grounding use the complete assessment in `consolidated_claims_full`; this cap cannot change a claim's assessment. |
| `materialize_max_unverified` | 8 | 48 | Sub-cap on unverified claims inside `materialize_max_total`. |
| `prompt_evidence_total_char_budget` | 100 000 | 180 000 | Total character budget for the rendered evidence overview that ends up in every section's system prompt (and, in the compact per-source view, in the evaluate prompt). Overflow is thinned round-robin across the searches and becomes `EvidenceOverview.omitted_record_count` with a visible "HINWEIS: …" footer in the overview, a completeness note in the report's appendix and the `omitted_evidence_*` metrics -- never a silent cascade. |
| `prompt_evidence_record_char_limit` | 2 200 | 2 600 | Per-source-block budget inside the overview. A record that exceeds it keeps its header and metadata, then keeps content lines in their fixed build order (claims first, then evidence snippets and source passages) up to the first line that no longer fits; the rest is replaced by a visible `[...weitere Belege dieser Quelle wegen Budget gekuerzt]` marker. |
| `evaluate_evidence_total_char_budget` | 60 000 | 100 000 | Total budget of the evaluator's compact record view (header plus a few bounded claim lines per source, no snippets or passages). Sized so that every source the profile can collect fits, so the evaluator judges coverage and gaps over the whole ledger instead of an excerpt; overflow is thinned round-robin across the searches exactly like the writer's view. |
| `evaluate_evidence_record_char_limit` | 900 | 900 | Per-source cap inside the evaluator's compact view. |
| `min_report_eligible_evidence` | 3 | 8 | Minimum report-eligible records required before `evaluate()` accepts early-stop. **Hidden veto** in the stop cascade: applies even when utility-stop, plateau, or confidence-threshold has already fired. See [Stopping](../architecture/websearch/stopping.md). |

Section list per profile is defined inline. Every `AnswerSectionSpec`
carries a language-independent `key`; the heading the composer writes is
`heading(language)`, resolved through `HEADINGS` in
`src/inqtrix/prompt_language.py` for the run's answer language (German and
English tables; other languages fall back to English). Prompt instructions
and length guidance are English, like every prompt.

| Profile | Key | Heading (de) | Heading (en) | Flags |
|---|---|---|---|---|
| Schnell | `summary` | Kurzfazit | Summary | `write_last` |
| Schnell | `key_points` | Kernaussagen | Key points | `owns_evidence_limits` |
| Compact | `summary` | Kurzfazit | Summary | `write_last` |
| Compact | `key_points` | Kernaussagen | Key points | |
| Compact | `detail_analysis` | Detailanalyse | Detailed analysis | |
| Compact | `context_outlook` | Einordnung / Ausblick | Context and outlook | `owns_evidence_limits` |
| Deep | `executive_summary` | Executive Summary | Executive summary | `write_last` |
| Deep | `background` | Hintergrund / Kontext | Background and context | |
| Deep | `analysis` | Analyse | Analysis | |
| Deep | `perspectives` | Perspektiven / Positionen | Perspectives and positions | |
| Deep | `risks` | Risiken / Unsicherheiten | Risks and uncertainties | `owns_evidence_limits` |
| Deep | `conclusion` | Fazit / Ausblick | Conclusion and outlook | |

A custom plan (an extension hook) may use a literal heading as the key; an
unknown key is written verbatim in every language.

### `AnswerSectionSpec.write_last`

A section flagged `write_last=True` is rendered **after** all body sections, so it can see them via `report_so_far_summary` and `used_evidence_labels`.
See [Answer composition](../architecture/websearch/answer.md) for the exact mechanism, including the write-plan split and how the rolling body summary is built.

### Evidence limits and open questions

`AnswerSectionSpec.owns_evidence_limits` assigns the report's consolidated
disclosure of evidence limits to one section: `key_points` in Schnell,
`context_outlook` in Compact, and `risks` in Deep.
Other sections retain relevant qualifications inline. Ownership follows the
section plan, independently of heading text or language. A custom plan without
an explicit owner uses its last non-summary section, or its last section when
all sections are summaries. Multiple explicit owners are a configuration error.

Every writer receives the evaluator's status, explanation and uncovered
aspects. Verified claims do not imply that all requested questions are answered;
an insufficient evaluation or uncovered aspect still requires disclosure.
Complete evidence does not require a separate uncertainty block.

### Operator guidance

Operators typically change the profile **only**; the derived evidence and
prompt budgets should not be tuned individually because they were calibrated
together. LLM output-token defaults live on provider constructors
(`default_max_tokens`) or provider-specific model configuration, not in
report profiles.

In `DEEP`, the unverified materialisation sub-cap is set equal to the total materialisation cap -- deliberately; see [Evidence pipeline](../architecture/websearch/evidence.md) for the reason.

## Selecting a profile

### Library mode

```python
from inqtrix import AgentConfig, ReportProfile, ResearchAgent

agent = ResearchAgent(AgentConfig(report_profile=ReportProfile.DEEP))
```

Or via environment:

```bash
export REPORT_PROFILE=deep
```

`REPORT_PROFILE` is read by `AgentSettings` (field alias `REPORT_PROFILE`, no `INQTRIX_` prefix because the settings models use an empty `env_prefix`). It does not flow into `AgentConfig`: on the first `research()` call `ResearchAgent._build_settings` (`agent.py:796`) builds an `AgentSettings` from the environment and then lets only the fields you set explicitly on `AgentConfig` override it. This runs on every agent, including when you pass your own `llm` and `search` providers — only provider auto-creation is conditional on those being unset.

### Server mode

On the HTTP `/v1/chat/completions` endpoint, callers can flip the profile per request:

```json
{
  "model": "research-agent",
  "messages": [{"role": "user", "content": "..."}],
  "agent_overrides": {"report_profile": "deep"}
}
```

`"schnell"`, `"compact"` and `"deep"` are the accepted values. Unknown values return HTTP 400. See [Web server mode](../deployment/webserver-mode.md) for the full override whitelist.

## Profile-switch semantics

`apply_overrides()` treats the profile and any caller/operator-provided knob as explicit: when a request or an operator sets `report_profile`, profile-derived defaults fill only fields that neither side set directly. This keeps surgical overrides (e.g. `max_rounds=2` together with `report_profile=deep`) working the way the operator expects, while a pure profile switch can still move from `deep` back to `compact`. The pinned scenarios live in `tests/test_server_overrides.py`.

## Writing a custom profile

Report profiles are plain dataclasses in `report_profiles.py`. Adding a new profile requires:

1. Define the new `ReportProfile` enum value.
2. Register its `ReportProfileTuning` bundle in `report_profiles.py` alongside `_COMPACT_TUNING`, `_DEEP_TUNING` and `_SCHNELL_TUNING`.
3. Add a branch for it in `tuning_for_report_profile()` (`report_profiles.py:351`). Without this the new profile silently falls back to `_COMPACT_TUNING` — the function has no error path — and `settings_overrides_for_report_profile()` inherits the same fallback, so the whole preset cascade stays on COMPACT.
4. Add a test scenario in `tests/test_server_overrides.py` that pins the new profile together with at least one other overridden field.

`AgentSettings.with_report_profile_defaults(...)` (`settings.py:656`) needs no change: it resolves the bundle through `settings_overrides_for_report_profile()` and is generic over profiles. Touch it only if your profile adds a genuinely new derived field.

Keep profiles additive; do not remove or rename the existing ones without a deprecation cycle.

## Related docs

- [Agent config](agent-config.md)
- [Settings and env](settings-and-env.md)
- [Web server mode](../deployment/webserver-mode.md)
- [LLM calls](../architecture/websearch/nodes.md) -- section "Citation allowlist": which URLs an answer may cite, and the link sanitizer that strips the rest.
- [Answer composition](../architecture/websearch/answer.md) -- how the section list and char budgets feed the per-section LLM calls.
- [Evidence pipeline](../architecture/websearch/evidence.md) -- where `prompt_evidence_total_char_budget` and `prompt_evidence_record_char_limit` are consumed.
