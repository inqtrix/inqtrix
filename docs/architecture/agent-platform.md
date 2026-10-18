# Agent platform (Agent Desk)

> Files: `src/inqtrix/agents/`, `src/inqtrix/capabilities/`,
> `src/inqtrix/core/results.py`,
> `src/inqtrix/services/agent_control_service.py`,
> `src/inqtrix/server/routers/agent_runs.py`, migrations 0029/0030

## Scope

`mode=workspace_agent` runs a staged, human-supervised research agent: it
explores read-only, proposes an editable plan, waits for approval,
executes tasks (child research runs, instant web lookups, in-process
knowledge retrievals, whole-document analysis), merges evidence with
contradiction analysis, and synthesizes a cited memo artifact that a
critic reviews once. Everything user-negotiable is persisted in Inqtrix
control tables; run events are signals, rows are the truth.

Research extraction, report composition and Agent Desk writers share the
`evidence_fidelity_block()` contract in `inqtrix.prompts`. Conditions, scope and
time references must survive synthesis and revision. A rule or deadline does
not establish a current state, and source roles or valid citation labels do
not prove that the cited content supports the final sentence. The shared
instruction is present in memo synthesis, chat answers, the tool-calling
kernel and Deep revision; it adds no model call or semantic verifier.

When a research child's report is archived to keep the parent context small,
its evidence-review and partial-coverage notices stay in the tool message
alongside any memo status. These notices are part of the result contract and
remain visible independently of the truncated report preview.

## Agent tiers

| Tier | Mode(s) | Control flow | Tools | Interrupts |
|---|---|---|---|---|
| S0 | `direct_llm` | code, one call | none | no |
| S1 | `research`, `knowledge` | bounded loop | web/RAG read-only | no |
| S2 | `workspace_agent` | checkpointed phase machine | capabilities, children | yes |

The escalation doctrine is "smallest sufficient tier"; the UI sells
modes, never this taxonomy.

## Speed/depth Stufen (schnell | gruendlich | tief)

Orthogonal to the S0-S2 taxonomy above, the user-facing **Stufe** dial
selects how much work one Agent Desk run may do. All budgets live in ONE
frozen table (`agents/tier_policy.py`, `TIER_POLICIES`) that every
consumer reads — routing, clarification caps, the planner prompt, the
plan validator (`web_research_profile_ceiling`), child research
profiles, the plan-gate rule, synthesis form, and verification level.
Published equals enforced: `/v1/capabilities` exposes the same table as
`agent.tiers` + `agent.default_tier`.

| Stufe | Clarification | Plan gate | Web budget | Delegation (kernel) | Verification | Section reach | Output |
|---|---|---|---|---|---|---|---|
| `schnell` | never (intake questions become named assumptions) | only under `strict` autonomy | exactly one `web_instant` and one `search_project_knowledge` (both enforced at the kernel tool chokepoint); `web_research` rejected by the validator | none | citation labels only (critic skipped) | 20 register references | chat answer; a document only on request |
| `gruendlich` (default) | max. 1 round, at intake or after discovery | per autonomy | children default to profile `schnell` (1 round), ceiling `compact` | research children (`run_web_research`, `delegate_batch` mode `research`); no mission | critic (max. 1 revision) + advisory quote grounding | 60 register references | intake decides |
| `tief` | up to 2 rounds | per autonomy | children default `compact`, ceiling `deep` | missions (`run_deep_mission`, batch mode `deep_mission`) and research children | additionally: unverified web-cited quotes flip a passing critic verdict to `revise` once | 180 register references | canvas report |

The ladder answers "how much work may one turn start": one search, one
research child, one mission. `mission` is a column of the same table, so
the middle rung exists deterministically — a `gruendlich` run cannot fall
into a plan/waves/critic mission underneath one chat turn.

Admission is additive: `agent_overrides.agent_tier` (or the `AGENT_TIER`
env default) selects the Stufe; sending contradictory `depth` and
`agent_tier` in one request is a 400, while an explicit request `depth`
wins over a contradicting env-level tier (request beats env; the run
then keeps legacy depth semantics, logged visibly). `tief` bridges to
`depth=deep` internally so every existing depth consumer behaves
unchanged. The tier never influences model routing — model choice stays
exclusively with the model picker.

Engine scope: the per-task Suchtiefe ceiling applies to mission-engine
plans (the plan gate is where per-task profiles exist). Kernel-engine
(`agent_kernel`) child research always runs the tier's DEFAULT child
profile. The kernel tools a Stufe removes are derived from the table
(`blocked_kernel_tools`) and enforced at the dispatch chokepoint:
`schnell` removes `ask_user`, `run_web_research`, `run_deep_mission` and
`delegate_batch` (and clamps the iteration ceiling); `gruendlich` removes
`run_deep_mission` (a batch assignment in mode `deep_mission` is denied
through the same check); `tief` removes nothing. `write_canvas` is never
removed — a document on request is possible in every Stufe. A run
without a Stufe (legacy `depth`) keeps every tool.

An explicit `response_form` (`chat`/`canvas`) on the request comes before
the Stufe's form default in both engines. The mission machine applies the
full precedence in code (a patch assignment always writes to the canvas;
otherwise the explicit request, then a skill's pin, then the Stufe's
default — `schnell` chat, `tief` canvas — then the intake routing).
The kernel states exactly one form rule in its first message: the explicit
choice when there is one, otherwise the `schnell` or `gruendlich` default —
never both, because two contradicting server instructions in one message
leave the choice to the model.

## Report quality pipeline

Deterministic quality building blocks live in `agents/report_quality.py`
and are shared by BOTH engines (mission machine and kernel):

* **Citation validation** — cited `[K#]`/`[W#]` labels must exist in the
  evidence ledger; the mission synthesis gets exactly one bounded LLM
  repair round, the kernel's `write_canvas` rejects unknown labels with
  a loud tool error (the kernel loop is its own repair round).
  Registered internal reference identifiers belong in reference metadata,
  not in reader-facing text. The same boundary checks concrete ledger IDs
  without recognizing identifier patterns or rewriting prose. Canvas writes
  reject such text before persistence; final review edits cannot introduce
  those IDs. Synthesis and final chat reuse their bounded repair path, and an
  unusable internal-metadata repair is rejected with its usage retained.
* **Quote grounding** — quoted passages are verified verbatim against
  the STORED evidence texts (internal chunks and web excerpts alike) via
  the knowledge normalizer (`knowledge/grounding.py::quote_is_verbatim`).
  Verification is excerpt-based by design: nothing is re-fetched and
  model memory is never consulted, so unverifiable quotes stay visible
  in the critic facts instead of being trusted. The detector recognizes paired
  ASCII, German/English typographic double quotes and both guillemet directions
  for passages of 20–400 characters. Missing original text means unverified,
  not disproved; a provider synthesis does not become an original excerpt.
* **Evidence ranking** — above `synthesis_evidence_budget` references,
  the outline/answer PROMPT digest is capped to the top-ranked entries
  (source tier from `domains.py`, cross-task corroboration, excerpt
  presence — no LLM call). The citation ledger itself is never
  truncated, so every cited label still resolves.
* **Section evidence in two depths** — a memo section reads the
  references its outline entry preselected in full, and the rest of the
  run's ledger as one register line each. It may cite from both: the
  outline's selection decides what a section leads WITH, the register
  decides what it may reach FOR. It quotes verbatim only from what it read
  in full; from a register line it paraphrases and cites. An outline entry
  that preselected nothing therefore means "chose none", never "the run
  found none" — and when the run does hold evidence, the section reports
  itself as `section_without_preselection` (`status="degraded"`) so the
  outline's gap stays visible instead of being absorbed. How far a section
  may reach is a column of the Stufe table (`section_register`, published
  under `agent.tiers`): a Stufe that goes deeper reaches further. The
  register is ranked like the digest above, so a cut takes the
  lowest-ranked references; a cut logs a WARNING and raises the
  `inqtrix.truncation` span event with `limit_name=section_register`. A chat
  answer writes no sections and builds no register. `INQTRIX_AGENT_SYNTHESIS_SECTION_REGISTER_BUDGET`
  replaces every Stufe's width with the operator's number (and is
  published as such); `0` switches the register off.
* **Report guidance** — the plan-approval decision body accepts an
  additive `report_guidance` string (max. 2000 chars, stored in the
  approval's `decision_payload`); it renders as a user-requirements
  block in the outline, section, and answer prompts of that run.
  `POST /v1/runs` additionally accepts `report_guidance` and
  `report_rule_ids` with the run request. The Agent Desk composer sends
  `report_rule_ids` for the library rules picked with `@` (or `@rules:`):
  only rules visible for the agent surface, by their template id, each
  rule once, as an inline pill that holds for one task like a skill and is
  cleared only by an accepted submission. Before the submit the composer
  checks the published `max_rules` and `max_composed_chars` against each
  template's `composed_chars`, never by a composition of its own, and says
  so when the server publishes no ceiling or no rule cap. A rule it cannot
  attach is listed with its reason instead of being left out: the cap is
  reached, the rule is too long or over the budget, it is not synced yet,
  or it is a context pack with linked files. While a clarification is
  pending the rules are listed as blocked (the send answers it), and a
  rule pill attached before stays attached for the next task. Typed requirement text
  is set at the plan gate, which opens with the requirement in force.
  The approvals API returns the attached rules as parts, never their
  composed bodies; deduplication, the server-set `report_rules` and the
  transcript binding are specified in
  [Runs API](../extending/runs-api.md#result-requirement).
  Both engines state the same precedence under the requirement
  (`REQUIREMENT_PRECEDENCE` in `agents/prompts.py`): evidence labels stay
  even where a rule prescribes another citation style, the assignment,
  the typed free requirement and the user's clarification answers narrow
  a rule, the rule attached first wins a conflict between two rules and
  the chat answer names it, the requirement wins over a skill (attached
  or loaded), and a rule applies to the deliverable documents. Mission section writers read it with
  `SECTION_REQUIREMENT_SCOPE`: it describes the whole memo, whose structure
  the outline places. The single mission a kernel run starts with
  `run_deep_mission` inherits the requirement (text and rule parts) because
  its memo is the deliverable; research children, `delegate_batch` mission
  strands and role runs never do. A plan or replan gate opens with the
  requirement in force in its payload, and a decision keeps every rule
  already in force at its frozen revision and text; only added rules read
  the catalog. The run event `inqtrix.agent.requirement.attached` and the
  run span's `inqtrix.requirement.*` attributes record the requirement a
  run starts under.
* **Run-wide tool grants** — a tool-gate approve accepts an additive
  `approval_scope` (`"once"` default, `"run"`). `"run"` is stored in
  the approval's `decision_payload` and folded at every segment start
  into the kernel's grant set: the gated tools of that gate stop
  gating for the rest of the run. Grants act only in the balanced
  mode table (`strict` stays per-call by design), never cover
  `ALWAYS_GATED_TOOLS`, and surface on the execution snapshot as
  `tool_grants`. Because `decision_payload` is replay identity, an
  approve-once retry after an approve-run conflicts (409) exactly
  like a retry carrying a different edited plan.

## Execution model

The algorithm (`agents/algorithm.py`) is a LangGraph state machine whose
nodes are the phases below — deterministic control flow, LLM calls only
INSIDE phases through the shared structured-call helper. It executes in
run-store worker threads like every other algorithm and is registered
conditionally (see the feature gate).

```text
intake ─┬─ ask_user_first ─ clarify ─┐
        ├─ plan_now ─────────────────┤
        └─ discover ─ discovery ─ [clarify questions_for_user] ─┐
                                                                ▼
   plan (validate, 1 repair) ─ [plan approval] ─ execute waves
        ▲                                             │
        └── replan gate (≤2 additive rounds) ◄─ evidence merge
                                                      │
                     synthesize (memo sections) ─ critic (≤1 revision)
                                                      │
                       [patch ─ patch approval]* ─ finalize
```

\* Only when the assignment targets an editor document (`document_id`
on the run request): the agent proposes anchored edits through the ONE
editor-instruct pipeline and parks for the patch approval — ALWAYS, in
every autonomy mode (the write invariant). The agent never applies;
apply happens solely through `POST /v1/editor/patches/{id}:apply`.

* **Synthesis** (memo sections): the outline and every section prompt
  use the selected source passages and their retained provider-answer context
  (`INQTRIX_AGENT_SYNTHESIS_EVIDENCE_BUDGET` ranked sources). The pure projection
  in `agents/evidence_context.py` is shared with the kernel's evidence reader;
  it matches the retained origin, query and source URL without fetching pages.
  Each matching query answer appears once per prompt, separately labelled as
  provider synthesis rather than an original source passage. Sections receive
  only their selected references and matching query context; the same material
  reaches revisions. This can increase input volume but adds no model call.
  The outline's sections are written
  concurrently, up to `INQTRIX_AGENT_SYNTHESIS_MAX_PARALLEL_SECTIONS`
  (default 8). Each section is prompted from its outline entry and its
  own evidence digest and reads nothing a sibling writes; the memo is
  assembled, flushed and narrated in outline order, so the document
  still grows in reading order. Synthesis runs after the task waves, so
  it never adds to the wave worst case of the startup connection budget.
* **Interrupts**: approval/clarification nodes call LangGraph
  `interrupt()`; the algorithm then parks the run
  (`waiting_for_approval`/`waiting_for_input`) and returns. The
  decision endpoints record the decision and resume the run
  atomically; on re-entry the algorithm reads the decision from the
  control store and fast-forwards via `Command(resume=...)`. Node writes
  that precede an interrupt are idempotent (deterministic ids per
  `(run_id, kind, round)`), because interrupted nodes re-execute from
  their start on resume.
* **Effective actor and resume authority**: a run keeps its original owner,
  but every executable segment has an explicit `execution_actor_user_id` and
  scope ceiling. Approve, edit, or resume atomically records the deciding
  editor as the effective actor together with the control decision, run
  revision, and next status. The worker resolves that canonical user live and
  uses the same actor for quotas, audit, child runs, skills, knowledge, and
  tools. It never reconstructs or falls back to the owner's authority. A
  shared editor can therefore continue the run only with resources that editor
  may currently use.
* **Pinned dependencies and revocation safepoints**: an unscoped "all visible
  collections" request is normalized to concrete collection ids before
  execution, and every attached skill is pinned by id plus integer revision.
  The worker re-checks the actor, run edit authority, collection visibility,
  and skill revision before and after provider/search calls and at tool,
  knowledge, skill, child-run, segment, and final-publication boundaries. If
  access disappears, the run ends with `authorization_revoked`; it does not
  continue with a silently smaller evidence set. A remote call already in
  flight cannot be recalled, but its return value is discarded after the
  post-call check and is not persisted or emitted.
* **Children wait**: `web_research` tasks are SUBMITTED as child
  runs and awaited through the same interrupt machinery — the execute
  node submits one batch per wave (width `max_parallel_children`), then
  the `children_wait` node interrupts and the run parks slot-free as
  `waiting_for_children`. The run store's terminal choke point re-queues
  the parent inside the LAST child's terminal transaction (never lost),
  and the park itself re-probes in the same transaction (lost-wakeup
  self-heal). On resume, `children_wait` folds each child's terminal
  outcome from the run store — including the child's
  evidence-contract status, its capped confidence, its single-source
  share and its evidence-budget cut (`omitted_evidence_count` /
  `omitted_evidence_query_count`, `coverage = partial` when the child's
  report could not evaluate every source it had): a
  `needs_review`/`source_context_only` child is emitted as a
  `task.evidence_needs_review` activity, a partially covered child as
  `task.evidence_partial`. Both carry `evidence_limit=true`, never
  `fallback`: a limit of the evidence is a note, not a fallback path, so the
  task keeps its own status group; both are stated within the requested output structure
  by synthesis and shown to the critic as a precomputed fact, so a budget cut in
  a child is never read as "no sources found"; an explicitly classified transient
  provider/transport failure is resubmitted at most once with the unchanged
  operator-owned limits and the node parks again. Invalid input, policy,
  cancellation, authentication, and token-budget failures are never retried.
  A parent therefore never block-polls siblings out of
  the shared execution pool — an undersized pool serialises a wave
  instead of deadlocking it. Of a child's references only the cited ones
  (`cited`) become parent evidence; a source the child examined without
  citing it stays in the child's report — the same rule the kernel applies
  to a delegated research child's report.
* **Checkpointing**: `PostgresSaver` (sync — worker threads) with
  `thread_id=run_id`; tables are library-owned (`saver.setup()`), NOT
  Alembic-managed. The checkpoint is ONLY a resumability cache:
  wiping it fails in-flight runs LOUDLY ("Checkpoint ... verloren")
  while plans/artifacts stay intact and the assignment restarts as a new
  run. Terminal runs delete their checkpoint thread.
* **Autonomy**: `strict` approves the discovery probe list AND
  every plan delta; `balanced` (default) approves the initial plan,
  auto-applies replans of ≤2 new read-only tasks; `autonomous` skips
  plan interrupts (the plan is still persisted + visible). Write-effect
  capabilities (the editor patch flow) are gated in every mode.
* **Gate rejection is a clean finish, not a failure.** Rejecting the
  plan, replan or discovery approval completes the run normally with a
  deterministic German receipt as its chat answer (the reject `note` is
  echoed as a blockquote); a rejected replan that already synthesized a
  draft keeps that draft as the deliverable. The approval/plan rows keep
  the durable `rejected` status, `result_state` carries
  `plan_decision: "rejected"`, the approval resume payload includes the
  decider's `note`, and finalize skips memory-candidate staging. (Same
  contract as patch rejection, which already ended the run normally.)
* **Discovery probes:** deterministic and budget-capped through
  `discovery_max_tool_calls`. The wired set is
  `knowledge.collections.list`, one `knowledge.search` per content
  question, and the optional `web.search.instant` preview. The intake's
  `content_questions` are self-contained questions about the subject
  matter and run verbatim as the first searches; form and process
  requirements (citation style, reading completely, length) belong in its
  success criteria and never become a search. The intake is asked for at
  most `DISCOVERY_CONTENT_QUESTIONS_MAX` (4) questions — a request, not a
  limit: nothing caps the list except the call budget. An intake that
  failed or wrote no content question makes discovery search the
  assignment itself, with a WARNING. Discovery deliberately does not
  probe prior-run reuse because that requires a runs-read capability and a
  similarity policy; within a session, prior context already follows the memo
  lineage. It also does not probe referenced files because a run carries
  collection ids and at most one patch target rather than a list of mentioned
  files. Canvas and editor context are already covered by the intake memo and
  patch-phase document reads. See `agents/discovery.py`.
  The analyst reads what the searches found as one digest inside an
  `untrusted_content` fence (`discovery-results`), with the injection rule
  the provider layer appends to every system prompt: one `Search k: <question>` head per search, each
  passage as `[Sk.i] "<title>" · collection "<name>": <excerpt>` — titles
  and collection names, never document or collection ids. A passage an
  earlier search found again is a back-reference (`same passage as S1.2`).
  An excerpt longer than 440 characters keeps at most its first 400, cut at
  a word boundary and never inside a number, and ends in
  `[shortened here; the passage continues]`. An empty search reads
  `No passages found.` The web preview has its own `Web preview: <query>`
  head with `[Wi] <url>: <snippet>` lines; web snippets and failure texts
  are collapsed to one line and cut visibly like passages, so no text in
  the digest can forge a head or a passage. Blank and repeated content
  questions are dropped with a WARNING before any search runs. The
  collections and their document counts are server facts in the run
  settings outside the fence, and so is the list of searches that ran and
  the ones the budget left out (a run without a knowledge search says so
  instead of claiming its questions were searched). The analyst's prompt rules
  out two kinds of gap — which documents exist, and form requirements —
  and asks for gap descriptions and questions written for the user (plain sentences, no search vocabulary, no identifiers, most
  important gap first) — the discovery narration quotes the first gap.
  The planner reads the analyst's result inside its own fence
  (`discovery-state`) and carries the same injection rule; its prompt
  forbids tasks that inventory a collection's whole population (finding
  the documents that cover a subject stays allowed) and shares the
  intake's rule that criteria, queries and questions never demand
  internal identifiers.
  If the deterministic probe plan exceeds the cap, the omitted count is
  emitted as a visible limit event (`omitted`) plus a narration naming the
  cap; the run never presents the shortened plan as complete. The
  clarification and critic-replan caps use the same visible event vocabulary
  when they proceed with a stated assumption or an unresolved evidence gap.
  A replan round (round >= 1) whose delta cannot be validated is discarded
  visibly (`replan_invalid` activity) and the run continues with the previous
  plan version; only the first plan is terminal on validation failure. An
  empty critic replan (no new task, nothing skipped) keeps the memo the critic
  judged and ends the research visibly (`critic_research_exhausted`) instead of
  re-synthesizing unchanged evidence. Research that added no evidence ends the
  route too: the critic stores the identity of the run's evidence (the sorted
  `dedup_key`s of its references, `replan.evidence_identity`) with each
  decision, and when the round after a research decision finds the same
  identity — nothing new cited — it does not research again
  (`critic_research_exhausted`, reason `no_new_evidence`, "Nachrecherche
  beendet"). The measure is what the references cite: a document chunk
  (`doc:{document_id}#{chunk_index}`) or a normalized URL. A round that cites
  only chunks and URLs the run already cited counts as no progress; a re-read
  of a known document version that cites a chunk not cited before is
  progress. The memo's open points then say that the last research
  added no new evidence. Two more ceilings close the critic's
  research route the same visible way: a delegated mission (kernel child) opens
  at most ONE critic research round — the parent's deep-verify pass is its
  second check — and `mission_time_budget_seconds` (default 720 s of active
  time, gate waits excluded) ends further research once spent (`limit.reached`,
  kind `mission_time_budget`). Before a planner is called for another round,
  the review route and the replan gate ask one more question: can research in
  this run still reach unread material (`research_prospect.research_can_reach`,
  pure over facts the mission gathers)? It can through unread parts of
  documents already read (a re-read starts at window 1, so only a limit whose
  document fits the per-analysis window cap and the run's remaining windows
  counts; a complete reading of the same version without that limit voids
  it, and so does a complete reading of another version that stands later
  in the plan), documents
  a search found and no reading attempted, released collections no knowledge
  task searched, and the web when it is permitted and the assignment needs it.
  A plan without a free task slot closes every branch. Before the replan's
  plan version is saved, each of its new file analyses is searched and
  selected as execution would do it; one that would read nothing this run
  can still read (every found document already read, nothing found, or no
  window left) is dropped visibly (`replan_analysis_rejected`), a check
  that cannot run keeps the task visibly (`replan_analysis_unchecked`),
  and a replan whose every new task was dropped takes the empty-replan
  exit. Under `strict` autonomy the check searches nothing before the
  replan approval (a search waits for the user there, as the discovery
  probes do): each new analysis stays, visibly (`replan_analysis_unchecked`
  with `error_type` `consent_gate`), and execution applies the rule after
  the approval; a run without a window left still drops it, which needs no
  search. The replan's planner reads the criteria numbered as the review
  numbered them, the review's open criteria by those numbers (an unmet
  output requirement as a text defect), the branches the prospect opened,
  that no file analysis can read anything once the run's windows are spent,
  and every model or source text of the context fenced, each cut named.
  Each decision is its
  own `research_prospect` activity with the counts and flags it read; when no
  branch holds, it names the codes that closed them (`read_budget_exhausted`,
  `source_bound_limit`, `collection_unavailable`, `web_not_permitted`,
  `web_not_needed`), and `extraction_limit` whenever a document read to the
  end showed more passages than the analysis took over. On the review route a
  closed prospect ends research with reason `no_prospect` (or
  `plan_task_limit`); at the gate the round's evidence goes into the synthesis
  and no replan round is counted. A research round the review opened that
  ends before any task ran names why as well: `replan_invalid`,
  `replan_empty`, `no_gap`, `replan_rejected`. One `critic_research_exhausted`
  names every limit that applies (`reasons`) and the prospect's `codes`, and
  the run's outcome record (`inqtrix.agent.outcome`) counts the open
  criteria per limit and code; the field lists stand in
  [Runs API](../extending/runs-api.md). Whenever the route closes, the memo ends with the
  critic's classified factual gaps under the open-points heading of the memo's
  language (`heading_for("open_points", language)`: "Offene Punkte" / "Open
  points", joined to an existing closing section of that name) instead of
  another loop; unmet output requirements stay in the review/status path.
  The planner itself never plans verification, cross-check or falsification
  tasks: checking the result is the critic's job after the synthesis. The
  planner is told the task ceiling in the first plan and the remaining
  headroom (ceiling minus kept tasks minus the synthesis) in every replan
  prompt, so it never has to guess the arithmetic the validator applies.
* **Whether a run uses the web.** The source policy says whether the web
  MAY be searched; the assignment says whether it needs to be. The
  intake's `needs_web` (schema description and intake prompt share one
  sentence, `NEEDS_WEB_MEANING`) is read through one predicate,
  `assignment_needs_web` (`agents/intake.py`), by the discovery's web
  probe and by the planner in every round. A demand the code can read
  outranks the intake for both: a `web_research` directive, a web task of
  either kind in a plan version the user edited, or activated skills that
  allow only web tools. An answered clarification question about the
  sources outranks it for the planner alone, which is told that the answer
  decides: the answer is free text or a model-worded option, so no code
  reads whether it includes the web, and the discovery probe never sends
  text in that state. Without an intake reading the planner judges the
  need itself. When the web is open but not needed, the planner plans no
  web task — a new one is a repair error, while a web task carried over
  from an earlier version stays, since a replan cannot remove work that
  ran — and the plan carries a server-written assumption saying so, in
  every round until the user chooses the web; a plan version the user
  saves with a web task of their own drops it. A delegated mission's
  discovery sends no web probe: its
  question is a model's text written after reading project documents.
  Every model that has read project content and writes text a web tool
  sends out (the planner's web rule, the kernel's tool discipline) carries
  `EXTERNAL_SEARCH_RULE`: the text leaves verbatim to an external search
  provider and names only the public subject, never a project document,
  its title or heading, a collection, or anything only the project's
  documents say. Whether a project document itself is still current is no
  web question (`PROJECT_DOCUMENT_CURRENCY_RULE`, planner and kernel).
  Limit: these are prompt rules; no deterministic check reads the query
  text before it leaves.
* **Tools per task**: normal Mission plans express each independent
  web evidence question as one `web_instant` task. That task contains exactly
  one self-contained natural-language question and performs exactly one
  grounded `SearchProvider.search()` through the capability registry; tasks
  in the same dependency wave may execute concurrently. `web_research` is
  available unless the Stufe forbids it (`schnell`) and
  spawns one CHILD research run (`kind=agent_child`): its listed strings are
  joint guidance questions for that child, not literal provider calls. The
  server selects the child profile from the Stufe (`agents/tier_policy.py`):
  `gruendlich` defaults to `schnell` with ceiling `compact`, `tief` defaults to
  `compact` with ceiling `deep`. A delegated child (kernel to mission or
  research, mission to research) inherits the parent's `agent_tier`, so one
  Stufe governs every level of a run tree. A request that sends no `agent_tier`
  keeps the legacy depth path and gets `compact` for a normal-depth child and
  `deep` for Deep, so the Research Desk's `schnell` profile is also the default
  `gruendlich` child grain. Per-task model-generated token/time/child budgets are
  not execution authority; deployment quotas and bounded provider/child
  transport contracts are. A reached boundary remains a typed task/run state
  and visible evidence gap; it is never rewritten as a successful complete
  result or a hidden lower-quality mode.
  `rag_query` runs the knowledge algorithm
  IN-PROCESS with its retrieval profile (`schnell`/`standard`/
  `gruendlich`/`tief`); `file_analysis` reads the documents its questions
  find COMPLETELY (see "Whole-document analysis" below).
* **Whole-document analysis (`file_analysis`).** When completeness within a
  document is the requirement, no model has a reading decision left to make:
  the server knows every window. `agents/document_analysis.py` therefore
  runs a server-driven window sweep, engine-neutral behind `AnalysisPorts`:
  - *Selection.* The task's questions run `knowledge.search`; the hits only
    SELECT documents (best rank, then hit count) and never become evidence.
    At most `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_DOCUMENTS` documents are
    read; further found documents are named on the task, and the report
    counts the documents of the searched collections, so a reader learns how
    many the searches never found. Tasks of the same round share no
    readings: each reads the best-ranked documents ITS searches find, so the
    planner is told to give one task the questions for documents that belong
    together (one task per document re-reads what another task's searches
    also find). A later round's analysis does not read again a document
    version an earlier round read as far as a re-read reaches: the same rows
    and window rule as the research prospect
    (`research_prospect.version_reach`, `select_documents`), keyed by
    `(document_id, version)` against the current version window 0 would
    read (an unreadable version keeps the document as unread, visibly).
    Which tasks count as earlier is recorded with the round's plan version
    (`tasks_before_round`), never read from whichever sibling finished
    first. Reachable re-reads are read first, while their windows fit the
    analysis's budget; the skipped documents are named on the task as
    already read and block the absence claim like a document not selected.
    A document no earlier reading could open (each failed before its first
    window, with the store's code) is not tried again either, and is named
    as not readable, never as read.
    An analysis whose every found document was already read completes
    without reading and says so.
  - *Windows.* A pure window plan (20,000 characters, 1,000 overlap: 4
    windows for 66,389 characters, 32 for 602,381) is read sequentially on
    the task thread through `knowledge.document.read`, pinned to the version
    and content hash of window 0 — a change mid-sweep fails that document
    (`document_changed`). A task analyses at most
    `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS` windows, a mission at most
    `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS_PER_RUN` across waves,
    replans and parks; what a budget leaves is a named, unanalysed range.
  - *Map.* One tool-less structured call per window (`agent_file_analysis`,
    up to `INQTRIX_AGENT_MAX_PARALLEL_QUERIES_PER_TASK` at once) sees the
    objective, the task's questions as numbered criteria and ITS window —
    title and position inside the untrusted fence. An instruction in one
    window reaches that window's extraction and, as a verified finding, the
    reduce — never another window's reading, the choice of evidence or a
    later prompt; it cannot change what is read, because the sweep's
    capabilities are fixed in code. The window's system prompt states the
    limits the server enforces on every quote (passages per window, quote
    length, minimum words), built from the same constants. A provider failure
    stops the calls not yet made: the failing window names `provider_error`,
    the windows after it `not_called_after_failure`; what was analysed stays.
    A content refusal of one window (a terminal rejection — in practice
    HTTP 400 — whose structured code is `content_filter`,
    `provider_failure_contract.CONTENT_REFUSAL_CODES`) is
    that window's own reason, `content_refused`, and stops nothing: it is
    bound to the window's text, not to the provider. When a failure leaves no
    window analysed, the task fails with the provider's reason and keeps its
    report and window booking. It is not retried as a task: the provider
    layer already spent its shared attempt budget, and a task retry would
    stack a second retry layer on top. A task cancel stops the reading at
    the next window.
  - *Verify.* `knowledge.document.cite` maps each quote to evidence: it must
    be verbatim (the ONE normalizer, `knowledge/grounding.py`) in the window
    the model saw, inside one stored chunk overlapping that window, of the
    pinned content, and pass the evidence projector. A quote needs at least
    three words to prove anything (`too_short`); one longer than the call
    carries is rejected before it (`too_long`). Everything else keeps a
    named status (`not_in_window`, `spans_chunks`, `chunk_unverified`,
    `chunk_stale`) and is counted on the task; the calls travel in batches of
    at most 250 quotes.
  - *Reduce.* One call joins the VERIFIED findings into an answer and
    per-criterion verdicts. The server keeps only verdicts it can stand
    behind: "answered" needs a verified passage behind it ("no verified
    passage" otherwise), and "not addressed" is kept only when nothing
    blocks an absence; the downgrade names its cause — "not found in the
    analysed parts" when part of a document was not analysed, "not found in
    the verified passages" when passages the reading named did not become
    evidence. A failing reduce leaves the evidence standing.
  The task's evidence is exactly the cited chunks. **One table, one pair of
  functions.** Every limit a reading can name is a row of
  `document_analysis.REASONS`: a *coverage* limit (a range not analysed and
  why, a failed document, a document without text, a found document not
  selected, a page without a text layer, an unrecorded extraction) blocks
  completeness and absence; an *evidence* limit (a named passage that did
  not become evidence — `not_in_window`, `too_long`, `too_short`,
  `spans_chunks`, `chunk_unverified`, `chunk_stale` — passages beyond the
  per-window limit, a window that named the most passages it may or reported
  more, a stopped analysis) blocks absence only. `source_bound` marks the
  two limits of the stored document version itself (page without a text
  layer, extraction not recorded) that every re-reading returns. An unknown
  reason blocks as much as its kind can. `reading_blockers()` lists the
  limits of a stored report and `reading_predicates()` derives `complete`
  and `absence_supported` from them — the one source every reader asks,
  never a stored flag. Per document, `text_read_to_end` (every character of
  the stored text analysed) is not the same as `document_complete()` (and
  nothing known missing from that text).
  **What later steps learn.** The task's summary and reader lines are
  server-rendered: `analysis_facts()` says what was read ("read completely",
  or "stored text read to its end, but not shown to hold the whole
  content"), `analysis_notes()` what was not established, one true sentence
  per limit class (a quote too long to check is "not checked", never "not
  verbatim"), with real singular/plural forms. The outline, every section
  and the chat answer receive these lines (facts and limits); the activity
  row shows the limits; every note names the subtask by its plan title. The
  review of both engines receives each analysis as its own reading record
  (see Review). The model's summary, claims and open points appear on the
  task card alone. Coverage proves what was read, not what was understood.
  A task counts as covering its discovery gaps when every coverage limit it
  has is `source_bound` (reading again cannot close it); a part not read or
  a document not selected keeps the gap open. A completely read negative is
  `insufficient_evidence`. A replan reads the reading facts of earlier
  analyses (fenced, with the rule that a limit of the stored document
  returns on every re-reading) instead of their summaries. A balanced replan
  that adds a file analysis shows its plan (it reads whole documents, one
  model call per window).
* **Task-row truth.** Plan task rows transition idempotently from `pending` to
  `running` and then to `completed`, `failed`, `insufficient_evidence`,
  `skipped`, or `cancelled`; an in-flight synchronous provider call first
  records `cancel_requested`. The row stores the delegated child id and bounded
  result summary; `result_payload.answer_markdown` stores the complete task
  output without a character/word cutoff. The overview reads only the summary,
  while `GET /v1/runs/{run_id}/tasks/{task_id}/result` lazy-loads the full
  Markdown, references, metrics, and error. Events remain the chronological
  signal. A reconnect or terminal-run reload therefore reconciles status from
  control rows before replaying event detail. `POST
  /v1/runs/{run_id}/tasks/{task_id}/cancel` stops a pending task immediately,
  delegates to the existing child-run cancellation path when applicable, and
  suppresses retry/result commit for an in-flight instant operation without
  claiming that its synchronous network request was hard-killed.
* **Scoping**: every internal tool call is pinned to the current effective
  actor's live resource rights. Explicit collection ids are asserted; an
  omitted scope is normalized to that actor's visible collections rather than
  being rediscovered under the owner later. The shared Knowledge retrieval
  implementation partitions a mixed embedding-model scope by model, embeds
  once per group, and rank-fuses the group results before the common reranker.
  Neither orchestrator nor its tool adapter selects only the default model.
* **Evidence**: dedup by the platform citation identity
  (`doc:{id}#{chunk}` / canonical URL) and assign stable `K#`/`W#` labels in
  first-seen order. Exact retrieval fields (`source_text`/`excerpt`) remain
  source passages. Generated retrieval context is never exposed through this
  contract. Web evidence is the complete provider-grounded search result:
  exact query, coherent provider answer, provider snippets, and every returned
  link. Inqtrix does not fetch those linked pages. A URL tier is quality
  metadata and cannot discard an `unknown` result. Because one coherent
  provider answer may synthesize several links, the ledger records whether a
  citation has an explicit provider mapping, a provider snippet, or only
  source-list membership; it never invents a one-to-one passage mapping.
  Child research reports and their web-search ledgers reach the parent
  orchestrator without a second page-read gate. The kernel persists this
  ledger in the existing evidence artifact so park/resume and the evidence
  Canvas retain the exact query, answer, links, and stable `reference_id`
  labels.
* **Synthesis**: outline first, then one call per section; every section
  flushes into the memo artifact (revision++, `artifact.updated`), so
  the canvas streams section-wise without a delta protocol. Quotes
  are verbatim-verified through the knowledge grounding normalizer;
  unverified quotes stay visible, never block. A follow-up turn in the
  same session reads the ONE session memo (`(session_id,'memo')`) at
  intake — including any edit the user made after the previous turn — so
  the outline CONTINUES it ("latest revision wins") and each flush
  CAS-guards against that revision. If the user edited it concurrently the
  guarded write raises the same conflict as a user PUT: the agent keeps
  the user's text, appends its update, and emits `artifact.edit_conflict`
  rather than overwriting (the agent's symmetric half of the
  optimistic-concurrency contract). There is no `context_artifact_ids`
  wire field: the session key is the single source for the memo, so an
  explicit id list would be unconsumed surface.
  Agent-generated Markdown is normalized at generation/write boundaries so
  currency dollars cannot open accidental KaTeX spans. Fenced/inline code,
  URLs, block math, and recognized inline formulas remain byte-preserved;
  raw `grounded_support` provenance is deliberately not presentation-escaped.
  The outline's structured schema limits reference choices to the supplied
  evidence labels; without references only an empty label list is valid.
  Invalid or empty outlines fail through the structured-output error contract
  instead of inventing a replacement outline. Explicit section counts, order
  and titles take precedence over default organization; open questions fit
  within that structure. A remaining critic note adds no structural heading.
  Section writing accepts only the selected reference labels. An unknown
  output label triggers exactly one complete-text repair call;
  empty or still-invalid repaired output fails loudly and is never persisted.
  The run's clarifications reach the outline and every section in either
  transport, read once per synthesis from the stored clarification rounds,
  and stand right under the assignment they refine, ahead of the one
  output-requirements section, whose rank order they leave alone. The
  questions, the wording of the chosen options and the assumptions are the
  assistant's words: they stand inside the `run-clarifications` fence, under
  the server's frame that the user chose an option and the assistant worded
  it. The user's own free text follows the fence, outside every fence, as
  the user's requirement, by the number of its question. In a section's
  assignment, in either transport, one server line closes the block: the
  clarifications concern the whole memo, the outline already placed what
  they ask of its structure (a section of its own, the order, an opening or
  closing part), and the section follows them only where they concern its
  own content or form, never adding a part the outline gave to another
  section. The outline and the revision read the block without that line.
  The estimate that picks a section's transport measures that same prompt,
  and a run that asked nothing renders no block.
* **Evidence checks**: the contradiction prefilter excludes equal texts after
  whitespace normalization without removing claims or changing negation,
  numbers, case or punctuation. The preliminary coverage model runs only for
  failed or insufficient task outcomes, using the same status predicate as the
  replan gate. Otherwise `sufficiency_skipped` records why no model was called;
  successful tasks are not treated as a semantic evidence-coverage verdict.
* **Review**: the mission and the kernel's Deep mode share ONE review pass
  (`agents/kernel/review_pass.py`): a typed handover in, a typed verdict out,
  anchored edits instead of a rewrite. The mission hands over its assignment
  (question, output requirements, non-citable memory context), its success
  criteria, the current memo and its own measurements as run diagnostics, each
  with a `basis_id` and its method: citation coverage and a lexical word count,
  quote verification with the unverified passages, the known contradictions with
  both positions, the persisted plan/task states and the evidence notes of its
  children, and one reading record per document analysis
  (`diagnostic:<run>:<task>:document_analysis`, built by ONE function,
  `kernel/evidence_read.document_analysis_diagnostic`, for both engines): the
  task's plan title, objective and questions, per document what was read
  (`text_read_to_end`, `document_complete`, windows, unanalysed ranges, pages
  without a text layer, extraction status, verified passages, passages not
  taken over by status), the verified sources, how many documents the
  searches found out of the scope, both predicates and every blocker —
  server facts only, no model text. The trusted rubric says how it counts:
  as read for exactly what it records and within the scope it states,
  proving no content beyond the sources it lists, a named limit staying open,
  and several records about the same material being no independent
  confirmations.
  The text measurements also list verified sources the memo cites nowhere.
  An analysis appears only as its record, never again as an evidence note.
  Word boundaries follow the editor's letter/number rules, with
  internal apostrophes and typographic dashes joined; the count includes
  headings, labels and code and is neither a token estimate nor a measurement of
  the rendered body text. Execution criteria are checked against the task
  records, output requirements against the memo, factual claims against the
  evidence. Completed tasks establish execution, not semantic correctness.
  Source-operation counts include discovery, which is also identified
  separately; a delegated Research task counts as one operation, not its
  internal queries.
  Both engines also hand the review the run's clarification record, read by
  server code from the stored clarification rounds, never from a
  transcript: oldest first, without the server's own limit-gate rows, a
  skill-point round counting like any other. Per question it holds the
  round, the question, the wording of the options the user chose, the
  user's free text and whether the question was answered; an unanswered
  question carries the assumption the run continued with where its round
  recorded one, an assumption and never an answer. The review reads the
  whole record as requirements to check against, in a fence of its own
  (`run-clarifications`) after the assignment's, since a fence inside the
  assignment's would be neutralized. The rubric checks procedural steps the
  assignment orders (ask first, read completely) against the server facts
  the handover carries, this record and the run diagnostics, never as text
  the outputs must contain. The revision reads the record in the writers'
  form (see **Synthesis**), right after the assignment's fence.
  Two transports carry the same contract, chosen by measured need: material
  whose estimated input stays within a fixed budget is reviewed in one
  structured call that carries the outputs and the run's whole stored evidence;
  anything larger goes to the review role, a bounded kernel child run that
  re-reads the mission's stored evidence ledger and the memo for itself while
  the mission parks slot-free (`waiting_for_children`). A server without the
  kernel engine has no role and uses the one call as long as the model holds
  it; material that fits neither is a visible `critic_unavailable`, never a
  quiet pass. Every research round gets a review of its own.
  The rubric asks the fidelity questions of every cited statement in both
  transports: its force (a duty, an expectation, a permission, an example or
  indicator — an example is never a duty), its reservations, the intro of the
  list it stands in (an excerpt that begins inside a list leaves its force
  unknown, named as not checked), its links and quantifiers, its actors and
  reach, and absence. A statement of what the material does not cover is a
  factual claim about the documents: contradicted by material the review
  read, it is an `evidence_error` with that material as its basis; a reading
  limit alone makes no finding of it, and a claim the review cannot check is
  named as not checked. One list of these categories
  serves the review and the revision: the review renders them as checks
  with its own duties, the revision as the rules every changed sentence
  keeps. The writers' shared fidelity block stays their own.
  The verdict accounts for every success criterion by its number; a criterion
  it does not settle exactly once is open, never met. A finding quotes a
  verbatim, unique passage and names its basis; a source basis counts only when
  the run holds it in its own ledger and the review had read it, and one
  unconfirmed basis opens that finding alone. Each confirmed finding ends
  applied or named with its reason code: one that cannot be located, and one
  whose edit breaks a rule, is named alone and takes no other finding with it.
  The revision replaces exactly the quoted passages, each with the citation
  run right after it (`Satz. [K12]`); everything else stays byte-identical,
  so a revised text is not reviewed a second time, and an edit that changes
  how the untouched text cites (an opened fence, a lone backtick) is named.
  Instead, each applied edit of a model finding is re-checked, and the
  server, not the model, decides it. The server lists where the edit
  changed its passage: both sides are read in headings, sentences and code
  blocks, each sentence split once more after a `;` or `:`, and compared by
  their form (heading, sentence, list item, table row or code), their words,
  the marks that say as much as a word (an order or approximation sign
  such as `<` or `≈` wherever it stands, `=` or `~` before a digit, a sign
  before a digit or before a currency sign or a second sign that a digit
  follows, a separator between digits, a percent, permille, currency or
  section sign) and their labels, so punctuation and emphasis make no
  segment while a changed word, label, such mark or form does (a sentence
  that becomes a list item is a segment); each run of differing units is
  one segment (`changed`, `added` or `removed`). It also says where the
  replacement stands in the delivered text: the heading above it, the intro
  of the list it stands in or holds, and the text on either side (a long
  line ends with `… [shortened]`). One structured call on the review node
  receives, fenced as data and each block after a line that says what it
  is, each finding's prose, the replaced text, the replacement, its
  segments, its placement and the texts of the labels the replacement cites
  (for a `run_diagnostic` finding also the reading record it rests on). It
  fills one row per segment: verbatim quotes from those texts first, then
  force, limits with their direction, actors and reach, reservations and
  links of the new text against the quoted basis, never against the
  replaced text; the kind the whole replacement in its place gives the
  content beside the kind the basis gives it; and what became of the
  replaced text (restated, asked for in the finding's own words, or not
  asked for). The finding and the placement are never a basis. The server
  reads a quote's label without its whitespace and one pair of brackets
  (`[K5]` is `K5`), checks every quote verbatim (at least three words, a
  reading-record field and its value at least two), and accepts an open
  list item only for quotes from the list a source text begins inside: the
  lines (split at line feeds) from its start that open with an enumeration
  mark or in lowercase, up to one that opens with neither after a blank
  line (whitespace only) or after a line that closes a sentence with a full
  stop, which closing quotes or brackets and one footnote mark (`[3]`) or
  glued footnote number of up to three digits may follow; a last item that
  ends otherwise (with `;`, or with anything else after its full stop)
  keeps the sentence on the next line in the list unless a blank line
  follows it. It checks where a quote stands, not which item a row
  speaks of, so the choice of quote, an open item and `same` together can
  carry a hold. It settles each edit from a closed table: it holds only
  when every row carries it; it is rejected only for a deviation a
  verified quote shows on a row that states a claim, a duty or deadline
  where the basis shows a weaker kind included (`not_carried`), a dropped
  condition, exception or qualifier (`reservation_dropped`), or a removal
  no words of the finding ask for (`off_finding`); everything else stays
  unchecked: a defective or missing row, an unclear answer, any other pair
  of kinds, a row that states no claim, a deviation the replaced text
  already had, new text without a verified quote (such as a heading that
  cites no source), a fix the check does not call whole, an edit without
  a segment, an edit of more than 12 segments, an edit whose new text
  rests only on sources without stored source text (a web source kept as
  the search provider's context; no quote under its label can match, so
  such an edit goes to no call), and the edits beyond 40 segments in one
  call. The re-check can only confirm an edit, never take
  one back. When the first call holds at least one edit, a second call
  sends the same request byte for byte, since one answer of a model can
  flip on the same request, and an edit holds (`recheck` `held`) only
  where both calls hold it; there is no second call when the first holds
  none. Every other edit stays applied and unverified, a rejected one
  included: its code reaches the server log alone, never a reader line,
  and the outputs stay the ones the revision composed. Every unverified
  edit but a rejected one carries the reason its code reads as in its
  finding record (`recheck_reason`: `no_source_text`, `format_only`,
  `too_large`, `check_unavailable` or `inconclusive`), and the run's
  outcome summary counts those reasons in reader words. A
  re-check that cannot run (no review call, a context that does not fit,
  a failed first call or one without a usable verdict) leaves every
  applied edit applied and unverified, with its own warning. Otherwise a
  rejection by the first call is a warning that names the edit and its
  code, and one warning counts every other edit left unchecked per code
  and names none, an edit the first call held and the confirmation did
  not as `unconfirmed` with the confirmation's code after a colon; a
  confirmation that fails or answers nothing usable adds a warning of its
  own, and the edits the first call held are counted as `unconfirmed`
  without a code. The model's reason stays in the trace. Server findings
  never reach it.
  The citation rule depends on the finding's kind: an edit cites only its
  passage's labels and its basis's labels (in the target's label space, by
  reference identity), keeps each kept label on the sentence it stood on, and
  every new sentence of an `evidence_error`, `missing_information` or
  `output_requirement` fix carries a label of its basis; a `run_diagnostic`
  fix adds no label, and an output requirement without a basis changes only
  headings, order and deletions. A finding names in `remove_labels` the labels
  its fix removes and in `criterion_index` the success criterion it concerns;
  the server validates both. When the last round's revision leaves a counted
  finding unapplied, `critic_revision_discarded` lists each named finding
  with its reason and its text stands; findings of the research route and
  server findings that lost an overlap stand with their reason in
  `open_lines` and `findings`. On the `tief` Stufe an
  unverified quote in a web-cited paragraph joins the review as a measured
  server finding, anchored including its quotation marks; server findings
  stand behind the model's and lose every overlap, and in the last round a
  losing one is measured again on the delivered text. The decision lands as a
  `critic_report` artifact and in `result_state`, together with the verdict,
  its `round`, the `revision` (`none`, `verified`, `unverified`,
  `partially_discarded` or `discarded`), the `accounting` (`complete` when
  every criterion was settled), the `open_lines` with their reasons, the
  per-finding `findings` (each applied one with its `recheck`, an
  unverified one with its `recheck_reason`),
  the `research_limits` and `prospect_codes` that closed research, and the
  transport.
  Unmet criteria carry `index` (the criterion's number in the review's list),
  `text`, `kind`, `gap` and `state`: `information` for a factual evidence
  gap, `output_requirement` for format, length or structure. An unmet output
  requirement is `revised` when at least one counted model finding concerns
  it and every such finding was applied, `verified` when the re-check also
  held every one of those edits, otherwise `open`; its line then names the
  applied findings, says whether their edits were re-checked (a `revised`
  line says they were not checked again), and quotes no gap of the reviewed
  draft, which the delivered text may no longer hold. Neither state says
  the criterion is met. An
  information criterion stays `open`, since an applied edit reaches no
  unread material. The report also carries the review's `reach`: how many
  cited references it re-read of how many the outputs cite. Only information
  gaps may request research or populate the memo's factual open-points section.
  Output requirements belong to the text: they are revised or stay open points
  of the run's outcome record. Unclassified
  retained criteria remain visibly unclassified and cannot initiate a research
  round on checkpoint resume. Run diagnostics are judged against their supplied
  values, not against external citation labels.
  A passing verdict must contain neither findings nor uncovered criteria.
  An inconsistent or otherwise unusable report is rejected visibly as
  `critic_unavailable`; it cannot accept the output, revise it or reuse a stale
  research route. The current text and the consumed usage remain intact, without
  another automatic model call.
* **Long-term memory**: optional and personal. Run memory remains the
  control/artifact system and session memory remains the session memo.
  Accepted long-term memories live behind `AgentMemoryProvider` (first
  provider: self-hosted Mem0) and the Inqtrix `AgentMemoryService`.
  Anonymous/static principals cannot use it; namespaces are derived
  server-side from `(tenant_id, principal.user_id)`, and client owner fields
  are rejected. The default learning mode is candidate-only: finalize
  can stage `MemoryCandidate` rows, but users accept/edit/reject them in
  Settings. Memory is never evidence and must not be cited.
* **Patch**: document-targeted assignments validate the target at
  INTAKE (an invisible or mistyped document fails in seconds, before any
  research spend); the patch phase re-loads it with the effective actor's
  visibility, proposes edits via the shared instruct pipeline (memo as
  reference material, the editor-assistant timeout window), persists ONE
  `editor_patches` row (the artifact row carries only `{patch_id}`),
  and interrupts with approval `kind='patch'`. Rejecting the gate
  also rejects the patch row and ends the run normally — the memo
  remains the deliverable; `result_state` records `patch_decision`.
  Proposal failures (timeout, parse failure, oversized document) are
  HARD run failures, never a silent "no changes needed".

## Feature gate

The algorithm registers only when checkpoints can survive:
`INQTRIX_STORAGE_BACKEND=postgres`, or the dev escape
`INQTRIX_AGENT_ALLOW_VOLATILE=true` (InMemorySaver, WARNING logged,
`workspace_agent_durable: false`). Otherwise `mode=workspace_agent` is
the loud 400 listing available modes and `/v1/capabilities` reports
`features.workspace_agent: false`. Queue-mode parking is fenced: the
worker's handle parks with the claim fence and ACKs the dispatch message;
the resume re-enqueues a fresh message that any worker continues from the
persisted payload + checkpoint.

## Settings (`INQTRIX_AGENT_*`)

`enabled`, `max_parallel_children` (8), `max_parallel_queries_per_task` (3),
`discovery_max_tool_calls` (15),
`max_plan_tasks` (8), `max_replan_rounds` (2), `mission_time_budget_seconds` (720),
`max_clarification_rounds` (2), `default_autonomy` (`balanced`),
`allow_web_discovery_preview` (on), `advanced_autonomy` (off),
`allow_volatile` (off). All enforced in code, never prompted. Model
tiers: `agent_intake`/`agent_sufficiency` fast,
`agent_discovery_analyst`/`agent_contradiction`/`agent_file_analysis`/
`agent_answer_light` mid, `agent_plan`/`agent_synthesis`/`agent_answer`/`agent_deep_review`
high. A run's `agent_overrides.model/model_tier/effort` (and a skill pin)
reaches only the thinking nodes — `agent_plan`, `agent_synthesis`,
`agent_answer`, `agent_answer_light` and `agent_deep_review`, plus `agent_kernel`
in the kernel segment. The review of both engines runs under the one node
`agent_deep_review` and defaults to high.
Intake, sufficiency, contradiction, discovery, file analysis and other helper
roles retain their own tiers. The selected settings are recorded before the
kernel's automatic Deep effort increase and passed to mission children, for
both single and batched dispatch. The increase stays local to the kernel loop
and its revision. A plan task selects
its tool, permitted research profile and recency, not a model tier. Child research
runs use the server's node routing. New plan schemas reject `params.model_tier`;
editing a persisted plan discards that field before validation without discarding
other unknown fields. An already stored task cannot override its child's model
routing through that parameter.

The normal/deep kernel bases are `kernel_max_tool_calls` (30/60 through
the deep variant) and `kernel_max_iterations` (121/121: fourteen tool turns
plus the answer in both — deep buys more tool calls and the verification
pass, not more turns). Explicit extensions are bounded independently by
`kernel_max_tool_calls_extension_ceiling` (60/120) and
`kernel_max_iterations_extension_ceiling` (241/241), again with deep
variants. A configured ceiling below its base cannot shrink the base; the
effective ceiling is their maximum. `/v1/capabilities.agent.limits` publishes
these pairs together with mission discovery/plan/replan/clarification/child
limits, the research round limit, and the non-extendable token ceiling.
Every live Agent execution snapshot publishes measured usage and the
effective boundary in `execution.limits`; older servers omit the capability
block and the UI hides it rather than guessing.

The manifest also publishes the effective `schnell` kernel boundary (57
steps and no in-run extension at the defaults: the published search, one
knowledge/todo turn, one slack turn, plus three read-only turns on evidence
the session already retains, canvas documents and attached reports — a
follow-up in a session with earlier searches reads before it answers)
instead of presenting the normal tier's larger allowance before submission.

## Interaction layer

* **Two permission modes.** The UI presents
  Standard/Auto (`/v1/capabilities` `agent.mode_presets`, mapped onto
  the unchanged wire vocabulary `balanced`/`autonomous`;
  `advanced_autonomy` republishes the legacy three-way control incl.
  `strict`). In Standard the approved plan is the ONLY web-search
  consent surface — plan tasks carry their verbatim queries (shown
  inline in the approval tray), a replan that adds anything but a
  knowledge query re-gates — only `rag_query` auto-approves
  (`AUTO_APPROVABLE_REPLAN_TOOLS` in `agents/replan.py`); web tasks and
  whole-document analyses show their plan — and the discovery web preview
  runs only in `autonomous`, only when the assignment needs the web, and
  only for a question the user wrote (not in a delegated mission).
* **Structured clarifications.** A gate round carries 1-3 questions
  with 2-4 pickable options each (`ClarificationRecord.questions`,
  sanitized deterministic ids); free text stays possible per question
  AND as a whole-round answer (`answer`), so older clients keep
  working. Answers land in `answers` (per-question map) and compose
  into the resume history through ONE shared function
  (`round_qa_lines`).
* **Which questions a round asks.** The intake and the discovery analyst
  propose questions with a closed `subject` (`knowledge_scope`,
  `sources`, `output_form`, `assignment`) and a `default_assumption`;
  both fields are internal and never reach the stored round or
  `/clarifications`. Both prompts state the run's fixed settings
  (`RunSettingsFacts`: knowledge scope with collection names as labels
  and document counts, never ids; web on or off; a pinned output form;
  tier and round cap; in Auto, ask only when no reasonable default
  assumption exists). Code decides, not the prompt: the clarify node
  drops a question whose subject a setting answers (`knowledge_scope`
  under pinned or all collections, a boundary of at most one collection
  or knowledge off; `sources` when at most one source is available;
  `output_form` only under a pinned form — a form the intake inferred is
  open; `assignment` never) and quotes each one with its setting in one
  transcript line; only a pinned boundary is called "selected", an
  inherited one is named as what the run was released. It then drops
  what an earlier round asked or the run already assumed (both lists are
  checkpointed state, so a resumed segment filters exactly like its
  first pass), appends pending skill points, and asks nothing when
  nothing is left. The settings section states the rounds LEFT, so the
  analyst after a used-up intake round knows a question would not be
  asked. The second round exists only when the analyst's
  `questions_for_user` (or a pending skill point) is non-empty — a
  `blocking` gap is research the plan must close, not a question.
* **Assumptions instead of questions.** A question the round cap
  (`effective_clarification_rounds`: the operator cap, tightened by the
  tier), the tier's zero-round cap or the per-round overflow keeps from
  being asked is named in one transcript line with its default
  assumption, and recorded as `clarification_assumptions`
  (`{prompt, assumption, reason}`). At the cap, `limit.reached`
  (`kind=clarification_rounds`) carries `suppressed`, the number of such
  questions, and fires only when a question survived the filters. The
  assumptions reach the discovery analyst and the planner in a fenced
  block marked as proposed defaults — never as the user's answer — and stand
  verbatim in the first plan version's assumptions, where the plan
  approval shows them; a question without a proposed assumption stands
  there as an open question. The snapshot's `limits.clarification_rounds`,
  the event and `agent.tiers` publish the same effective cap.
* **Chat-form deliverable.** `response_form` on the run request
  (`auto|chat|canvas`, agent-only) or the intake profile decides the
  turn's deliverable: `chat` writes a run-local `answer` artifact
  rendered inline in the transcript (one `write_chat_answer` call —
  no outline loop; model routing uses the approved plan contract:
  any web task selects the high tier even when retrieval returned no
  references, while a plan without web tasks selects
  `agent_answer_light`/mid), `canvas` keeps the session-memo
  path unchanged. A patch assignment always uses canvas.
* **Canvas attachment.** `canvas_context` on the run request
  (agent-kernel only, rejected loudly everywhere else including the
  quick-web lane) carries the open canvas document (`artifact_id` +
  `revision`) and queued selection comments
  (`{artifact_id, revision, quote, quote_before, quote_after, comment}`,
  max. 20). It is a DEDICATED field — never serialized into `question`
  (the question column is clipped at persistence and reaches share-inbox
  titles) — validated strictly with visible bounds (over-limit content
  is rejected with the limit named, never truncated), persisted in the
  worker replay body, and injected into the kernel user message directly
  before the assignment. Trust split: the user's comment text is an
  instruction; the quoted document excerpts are fenced as untrusted data
  (`quelle="canvas-auszug"`). Snapshot semantics: frozen with the first
  segment's checkpointed user message — later canvas edits never rewrite
  it. Child runs do not inherit it.
* **Session context.** Every follow-up composes session metadata
  server-side from durable rows (`list_session_runs` + clarifications +
  approvals + result answers + `list_session_artifacts` registry). An
  explicit `request.history` replaces only the conversational history;
  the artifact registry, last effective output form, and prior canonical
  evidence count are still reconstructed. The references behind that count
  include run-scoped evidence bundles from earlier visible, completed session
  turns, including ordinary chat answers without a Canvas document. Their
  metadata is paged separately and does not create entries in the file index;
  full search answers are loaded only by the authorized evidence reader.
  Duplicate sources keep their earlier labels while missing lineage is filled.
  These references
  are registered as the follow-up turn's known evidence before its first
  model turn, labels preserved: an answer that cites "[W3]" from the
  previous turn keeps the citation instead of seeing it marked
  unsupported, and the turn need not search again for a source the
  session already holds. The deterministic trim policy
  drops older turns first and visibly truncates an oversized newest turn.
* **Event transport fallback (T).** `GET /v1/runs/{id}/events?format=json
  [&after=N]` returns the SAME replay buffer as an immediate JSON page
  (`{data, terminal}`); the frontend channel degrades from SSE to this
  poller (and back) with a visible hint when proxies buffer SSE.

Memory knobs: `memory_provider` (`none`/`mem0`), `memory_mode`
(`off`/`candidate_only`/`auto_safe`), `mem0_base_url`, `mem0_api_key`.
`auto_safe` is accepted for forward compatibility but currently reports
`effective_mode: candidate_only` with a degraded reason; it does not
auto-retain memories yet.

## Cognitive kernel (`mode=agent_kernel`)

A second **orchestrator** registered next to the phase machine: an LLM
tool-calling loop on deepagents, assembled through the one harness seam
(`agents/harness.py::build_kernel_agent`, pin `deepagents>=0.7.13,<0.8`
with contract tests in `tests/agents/test_harness_kernel_contract.py`
as the upgrade gate). The phase machine stays registered — it is the
kernel's `run_deep_mission` child capability, and parked missions must
resume under their own algorithm.

* **Two-layer architecture.** The kernel (`agents/kernel/`) is the
  conversational brain: it answers directly, asks structured questions
  (`ask_user` -> the structured clarification machinery, deterministic ids
  hashed from the checkpointed `tool_call_id`), searches, writes canvas
  deliverables, and DELEGATES multi-strand researched deliverables to
  the phase machine as child runs.
  It is not a second retrieval implementation. `search_project_knowledge` and
  Mission `rag_query` are thin orchestration adapters around the same
  `knowledge.search` capability and `KnowledgeService.search` used by the
  Knowledge Desk. Dense/sparse fusion, mixed-model partition/rank fusion,
  reranking, authorization, active-revision filtering, degradation reporting,
  and canonical evidence projection therefore remain owned by the Knowledge
  subsystem. Likewise, kernel web research and mission research share the
  configured provider search and the same web-search ledger contract; neither
  creates a separate crawler or page-reader path.
* **Same platform seams.** Registry algorithm behind the normal
  RunService -> queue -> worker path; sync `graph.stream` only; shared
  checkpointer with `thread_id=run_id`; park/resume against control
  rows. Compiled graph variants are cached per `(autonomy,
  source_policy.web, source_policy.knowledge, max_tool_calls)` —
  `interrupt_on` and the enforced source tool
  surface are both compile-time; per-segment state travels through a
  ContextVar, so the compiled graphs are stateless and shared across
  users.
* **Policy gates.** `agents/kernel/policy.py` maps autonomy onto HITL
  gates: balanced gates `web_instant` (query verbatim in the approval
  payload), UN-scoped `search_project_knowledge` (a `when` predicate),
  and all three child-run tools (`run_web_research`, `run_deep_mission`,
  `delegate_batch`); strict also gates broad internal reads, while
  `read_evidence` consumes already registered evidence without a new approval;
  `propose_editor_patch` gates in EVERY mode. A gate parks the
  run as a `kind="tool"` approval whose id derives from the
  checkpoint-stable interrupt id; decisions map onto the HITL resume
  contract (approve/reject fan out over the batch, edit replaces the
  args of exactly one action, tool never swappable).
  Edited arguments are validated against the selected tool's real Pydantic
  input model; unknown fields and type errors are rejected. Tool name and
  stored action id are immutable. Identity, tenant, run, workspace, and
  authorization context are never editable model arguments — the server
  injects them and rechecks the target resource at execution.
* **Tools.** All wrappers over the capability registry or
  platform services, identity injected via `CapabilityContext` (never a
  model-controlled argument). Denials and outages return as VISIBLE
  tool results the prompt obliges the model to acknowledge.
  `write_canvas` writes session-scoped `deliverable` artifacts with
  optimistic concurrency (`expected_revision`); child-run tools park
  slot-free (`waiting_for_children`) and re-find their child via
  `origin_key=tool_call_id` on resume re-execution (persisted in the
  replay payload, no schema change).
  `read_evidence` indexes retained references/searches without a selector, or
  reads one `reference_id` or `query_id`. It returns available source excerpts,
  explicitly identified provider context and the producing search answers;
  it never fetches linked pages. Attached research reports import their ledger,
  and historical origins are loaded with the caller's visibility. An unreadable
  origin is reported, including when a local search projection already exists.
  The accompanying label/identifier catalog includes every registered reference,
  including when a large report body is moved to readable archive chunks.
  Reference registration, label translation and the catalog share the same set.
  Research origins supply their exported ledger; Agent and Mission origins
  supply the same persisted evidence artifact used by Canvas provenance.
  Completed child handovers use this shared reader too. The caller authorizes the origin
  run before reading its artifact; no second evidence store is maintained.
  Explicit reads put retained text before provenance and do not recursively
  offload the requested material. A reply contains at most 20,000 material
  characters and states its range, total size and continuation arguments.
  `offset` counts Unicode code points; a nonzero offset requires the returned
  `content_hash`. Every continuation reauthorizes the origin and compares the
  rendered selection's fingerprint. Changed material requires a fresh read;
  fragments from different versions must not be combined. Large text fields
  appear once, with metadata pointers to their named material sections. The
  stored evidence projection is unchanged. Reading consumes the normal
  tool allowance, not a web-search allowance. No autonomy mode requests renewed
  consent for this registered selection; origin visibility and tool restrictions
  are still enforced on every read.
  Run-local captured text uses the existing `context_archive` artifact kind.
  The control store commits all immutable parts together and treats an identical
  write as a replay. It stores the captured text exactly, without whitespace
  trimming or Markdown repair, and keeps no duplicate body in mutable revision
  history. Source and part fingerprints, original code-point coordinates and
  the complete part count describe one logical source. The source remains under
  the originating run's access and retention lifecycle.
  `read_canvas` reads these sources in bounded windows with `offset`,
  `content_hash` and `max_chars`. An explicit nonzero offset requires the
  fingerprint; a direct part reference can start at that part. The response
  identifies partial reads and supplies executable continuation arguments.
  JSON escaping preserves the exact decoded text without letting source text
  close the untrusted-content boundary. Missing or corrupt parts fail visibly.
  The bare run-archive prefix lists logical sources through `cursor` and `limit`.
  Its cursor binds the entire metadata snapshot, including legacy rows, rather
  than using an allocation-time identifier as a progress marker. A changed
  snapshot requires restarting the list. Text bodies are not loaded for this
  catalogue; range reads fetch only overlapping parts. Existing rendered
  archives stay readable with an explicit lower-fidelity designation.
  The owner can discover earlier questions, user decisions, published answers,
  Canvas revisions and recorded kernel transcripts through `search_context` and
  read selected originals through `read_context`. These use the existing run,
  decision and artifact stores, not a second conversation database. Model inputs
  cannot select a caller, workspace or session. Every read verifies the current
  session, source owner, version and retained dependency grants. A result share
  grants no private transcript access. Source receipts are checkpointed separately
  from message compaction and rechecked at model boundaries and on resume.
  Literal search scans at most 100,000 new characters and returns at most 20 hits
  per call, with executable continuations, exact match positions and explicit
  unavailable sources. Metadata pages contain at most 50 entries. Completeness
  describes the inspected source set and ranges, never unseen earlier pages.
  Metadata discovery uses batched queries and does not load archive bodies;
  source-grant validation uses bounded batches without one connection per source.
  These bounds do not constitute a measured cluster capacity guarantee.
  Private JSON snapshots preserve actual checkpoint messages and tool calls
  before checkpoint deletion; failed capture keeps the checkpoint. Compaction
  and recovery originals use the same private archive contract. Product artifact
  lists, single-artifact reads, exports and edits exclude these private records.
  Missing older transcripts are labelled unavailable. File/editor-dependent raw
  transcripts require current source-specific release and deletion adapters;
  without those adapters, cross-run retrieval refuses the raw snapshot explicitly.
  Saved Agent sessions retain their agent roots and children until session
  deletion, including while its deletion saga retries. Explicit native run
  deletion also removes execution checkpoints while the owner/terminal gate is
  held, before deleting the run anchor. Cleanup failure returns a retryable
  refusal and preserves that anchor. Terminal retention sweeps keep candidates
  whose checkpoint cleanup failed and retry them later. The generic run TTL still
  applies to ordinary runs. Aggregate session deletion removes these originals.
  Eager archival never shortens a tool result. Model-input planning considers
  complete messages, system input and tool schemas against a conservative model
  card budget with output reserve. If reduction is needed, it archives the full
  input message first and requests a task-specific projection. This shares the
  existing provider attempt/deadline budget. A projection that cannot itself fit
  stops visibly with originals retained; it never falls back to prefix slicing.
  Unknown model cards retain provider-driven context recovery.
  Public captured archives reject edits and renames with `archive_immutable` (HTTP 409).
  Ordinary Canvas documents keep their complete Markdown, revision and editing
  contract; archive-only parameters are rejected for these documents.
  The kernel's final response ends with the requested result rather than
  unsolicited follow-up offers or internal run rules. Missing project knowledge
  is mentioned only when the assignment requires it. Relevant limitations,
  necessary clarifications and explicitly requested next steps remain part of
  the response.
* **Document read windows.** `read_project_document` and
  `read_editor_document` transfer one bounded original range with its
  representation, version, content fingerprint, total size and complete
  `next_read` arguments. Offsets count Unicode code points; the end is
  exclusive. A continuation requires the returned version and fingerprint;
  a changed source is rejected instead of combining different versions.
  The stored document remains complete. These windows bound model transfers;
  the existing persistence getters still load the document body.
  Editor comments have an independent keyset continuation. Subsequent text
  windows do not repeat comments. Comment pages reflect current visible
  comments and do not claim an immutable snapshot of the entire collection.
* **Citing read text.** A document window carries no evidence label. The
  kernel turns passages of a window it read into evidence with
  `cite_document_passages`: the server checks each passage through
  `knowledge.document.cite` (verbatim in that window, held by one stored
  section of the same document version) and registers an accepted passage
  under the identity of a search hit on that section, so both paths share one
  label. Each refused passage names its reason (too short, not verbatim,
  across a section boundary, no verified section, another version). Citing is
  allowed exactly where reading is, only within ranges the run's coverage
  ledger records as transferred, and spends no source budget.
* **Source coverage contract.** Every windowed reader (`read_context`,
  `read_canvas` archive windows and catalogue pages, `read_project_document`,
  `read_editor_document`, `search_editor_document`, `search_context`,
  `read_evidence`, whole canvas reads) attaches a coverage record to its
  checkpointed tool artifact. The kernel derives a run-wide ledger from those
  artifacts (rebuilt from the checkpoint at segment start, advanced by the
  update stream) and publishes it as `execution.source_coverage`: per source
  version the merged read ranges, scanned ranges, unread remainder,
  `complete` and `continuation_open`. The advisory sufficiency judge sees the
  ledger as "Sources under reading" and, while a source the run started
  reading has an unread remainder or an open page, a `covered` verdict names
  the remainder instead of ordering the model to stop; thresholds, the
  judgement cap and the tool budget are unchanged. The kernel's review
  handover carries the same ledger as run diagnostics, one per source version
  the run read (read ranges, unread remainder, read completely), for the
  chat answer and the canvases the run itself wrote; a delegated run's memo
  keeps its own reading record. Every message the harness
  places into the transcript itself carries the harness-note prefix explained
  in the system prompt, so the model never attributes runtime advice to the
  user; user steering receives its own distinguishable framing.
* **Materials in the conversation corpus.** The knowledge documents
  (windows and search hits) and editor documents (with the comment ids
  actually transferred) a run took into its transcript are named in the
  private transcript descriptor as `materials`, collected only from the
  checkpointed artifacts of the producing tools. A later run of the same
  conversation may search and read that transcript through
  `search_context`/`read_context` once every material passes the reader's
  CURRENT rights: the knowledge document must still be visible and its
  collection inside the run's knowledge boundary, the editor document must
  still be readable and every transferred comment must still exist in the
  reader's comment scope. A deleted or hidden document, a revoked share or a
  deleted transferred comment closes the transcript (`material_unavailable`
  in the source catalogue); a changed document version does not, because the
  archived text is a historical snapshot of material that still exists. Raw
  file and image inputs keep the conservative lock until their intake path
  is bound; descriptors written before this contract keep their lock. A
  guessed id or a text inside a document never creates a dependency.
* **Canonical file text.** A file's text has one producer and one stored
  representation. `extract_file` is the only place that turns file bytes into
  text (parser text plus per-page texts for PDFs); the upload worker calls it
  once and persists the result on the library asset (`prepared_*` fields),
  named by file digest, text digest, parser id and the parser revision that
  made it. A preparation stored before the revision was recorded keeps
  "unknown": the parser installed at read time says nothing about an older
  text, so the revision is never back-filled. Every reader goes through one
  function. Knowledge ingestion by `asset_id` and
  `GET /v1/files/{id}/text` read that stored preparation and
  re-verify it on each call: the reader's access to the file, the file
  digest and text digest against the stored identity, and the asset's
  deletion lifecycle. A mismatch is a visible integrity error, never a
  silent second parse; a file outside the library has no preparation and is
  parsed on read, which its representation name says
  (`parsed_file_text` instead of `prepared_file_text`). An upload that
  completed while no parser was configured is prepared from its stored
  original, once, when a process with a parser runs its next reconcile pass
  (API or worker); a recorded parse failure is never queued again.
  What an extraction did not capture belongs to the representation. For a
  paged source the facts name the page count and the pages without a text
  layer (scans, full-page images). Both ingestion paths write them as the
  server-owned `extraction` metadata of the knowledge document (a
  client-supplied key of that name is dropped), `knowledge.document.read`
  passes them on, and the kernel's window note states them for every
  material reader: parser and revision, "text only, images and scans are
  not read", and either all pages, the numbered pages that produced no text,
  or "unknown" when no page information exists. Unknown never reads as
  complete, and a file-derived document without recorded facts says so
  ("extraction coverage was not recorded") instead of saying nothing
  (`DocumentReadOutput.extraction_status`: `recorded | not_recorded | none`). The kernel itself opens no raw files; files reach it as
  knowledge documents, and typed file and image inputs remain locked until
  their intake path exists.
* **Source navigation.** A conversation's originals are scanned in a fixed
  order (questions, published artifacts, results, archives, kernel
  transcripts last) under a bounded page budget, so the text a tool returned
  in a late turn can sit many pages away. Three properties keep it reachable
  without changing that order or budget. Every run-bound source title names
  the turn it belongs to (an excerpt of that run's question), because seven
  sources titled "Kernel transcript" or "Antwort" cannot be told apart. An
  incomplete search names the sources it has not reached yet
  (`unscanned_sources`, bounded, with `unscanned_total`), in scan order and
  never one the response already touched or one the reader cannot open, so
  the remainder has an address. And the tool contract says where earlier tool
  output lives (the turn's kernel transcript) and that a known turn is
  searched directly by `source_id` instead of paging through everything
  before it. This follows the same rule as windowed reads: a limited result
  always says where the rest is.
* **Editor read/propose discipline.** Two read tools —
  `read_editor_document` (a versioned, fenced text window) and
  `search_editor_document` (whitespace-tolerant search that returns
  BYTE-TRUE original-markdown `find`/quote candidates the server
  resolver matches literally, with exact continuation arguments for further
  matches) — mint durable read receipts
  `{document_id: revision}` (marker first line, rebuilt at segment
  start with a producing-tool check; a lost receipt only forces a
  re-read). `propose_editor_patch` is ENFORCED read-before-propose: it
  refuses unread targets, refuses any document other than the run's
  attached `document_id` target, and pins the receipt revision as
  `expected_revision` — the propose path answers a moved document with
  the same `editor.patch_revision_conflict` (409) apply uses, so a
  proposal can never anchor against text its author has not seen.
  Editor content is fenced: shared documents can
  carry other people's insertions — data, never instructions. Both read
  tools gate in `strict`; skills may allow them (`SKILL_ALLOWED_TOOLS`).
  Source tier is quality metadata rather than a domain admission list. An
  unknown publisher remains in the ledger and its complete provider-grounded
  answer reaches synthesis; the tier can influence ordering, cross-check
  planning, and confidence, but never silently remove the information.
* **Transparent limits.** Kernel tool-call and graph-step limits are
  checkpoint-safe decision gates. An overflowing tool batch is rejected in
  full before any tool executes; reaching either boundary creates one durable
  clarification and parks the existing run as `waiting_for_input`. The user
  may extend to the published operator ceiling, accept an explicitly labelled
  non-generative partial receipt, or cancel. An extension folds into the same
  checkpointed run and retains prior tool/token usage; it does not create a
  second lifecycle or reset counters. LangGraph's per-invocation
  `recursion_limit` is reduced by the checkpoint's cumulative step coordinate,
  so approval and clarification resumes cannot acquire a fresh step budget.
  The `schnell` step/tool ceiling is intentionally not extendable because an
  extension would contradict that tier's latency contract.
* **Token boundary.** The model-call boundary checks the cancel event and the
  run-cumulative token budget (prior segments recovered from checkpointed
  `usage_metadata`). A token ceiling is a typed, visible operator stop and is
  not offered as extendable: provider usage from the failing graph node is not
  yet an exactly-once checkpointed continuation coordinate. Presenting the
  same three choices here would risk replaying paid work or undercounting it.
  The Agent Desk therefore labels token limits separately from recoverable
  tool/step gates.
* **Continuity + events.** The per-run user message carries the
  session context (history block, artifact registry for CAS updates,
  response-form hints) — the system prompt stays compile-time static.
  The kernel system prompt carries the two shared answer blocks from
  `prompt_language.py`: `OUTPUT_FORMAT_BLOCK` (what the renderer supports) and
  `WRITING_STYLE_BLOCK` (the house writing style of conversational answers);
  the mission's chat answer carries them too, memo sections follow their
  outline and template instead.
* **Prompt language.** Every mission phase prompt, the kernel system prompt,
  the tool descriptions and every text a tool hands back to the model are
  English; the ANSWER language is a per-run fact. The mission resolves it once
  in the intake segment (`resolve_answer_language`: the intake's language
  classifier, then the text heuristic, otherwise `unresolved` with a WARNING —
  never a silent default) and keeps it as `language` / `language_source` in
  the phase state. A resumed segment restores that saved value, including
  an unresolved value, into its dependencies so document analysis and
  activity evidence notes use the same language as synthesis. Only legacy segments
  without a saved language use text detection. The kernel resolves its
  language from the question. The directive
  for that language (`directive_for`) is the FIRST line of every writing
  prompt (outline, sections, revision, chat answer, critic, deep review and
  revision, document-window and reduce calls, memory reflection) and of
  the kernel's user
  message. Structured-output schema descriptions follow the same contract:
  they describe the expected fields in English and defer reader-facing text
  to the answer language rather than naming a fixed language.
  A plan task's retrieval profile applies only to `web_research` and
  `rag_query`; other tool kinds use `null`. Strict output schemas require
  the key while allowing that null value. Schema guidance and validation
  feedback state this explicitly; invalid values are rejected, never silently
  stripped from a generated or user-edited plan.
  Server-written memo text (the open-points block, task evidence
  notes, default titles such as the answer artifact or the critic report)
  comes from the i18n table in that language. External content reaches the
  model inside `<untrusted_content source="…">` fences (`untrusted_fence`,
  `inqtrix.untrusted_content`): source excerpts, search answers, documents,
  earlier outputs handed to a reviser or a memory reflection, the answers and
  failure reasons of finished tasks in a replan, the gaps the review and the
  sufficiency check name for it (the review's open criteria themselves stand
  there only by their numbers), the questions, option wording and
  assumptions of the run's clarification record, the older conversation a
  context compaction condenses and the summary it puts back. The delimiter is
  neutralized inside the payload in every spelling, so embedded text can
  neither close a fence nor forge one; titles and URLs that stand on the
  server's own lines outside a fence are neutralized the same way. What a
  fence means is stated once, in `UNTRUSTED_CONTENT_RULE`, and the provider
  layer appends it to the system instruction of every model call
  (`UntrustedContentRuleProvider`, applied by `instrument_providers`) — also
  to calls whose caller passes no system prompt, such as the sufficiency and
  contradiction judges and the revision. It sits outside the tracing wrapper,
  so a generation's recorded system instructions show it. Text that directs
  the work is never fenced: after a context compaction the run's assignment
  (its first message and skill block) and the user's answers to clarifying
  questions stand verbatim before the fenced summary, a plan's section
  focus stays the section writer's work order, and a writer or the revision
  reads the user's own free-text answers outside the clarification record's
  fence. Fences and the rule stop a forged boundary and lower the chance
  that an instruction inside source text is followed; they do not make
  natural-language injection impossible.
  Every visible source-tool failure starts with one of
  `SOURCE_TOOL_FAILURE_PREFIXES` (`Tool unavailable:`, `Tool error`,
  `Tool blocked:`) — the one contract the checkpoint rehydration counts by.
  The update stream emits `tool.started/finished` (redacted previews),
  `todo.updated`, deterministic `narration` (content-hash ids), and
  `phase.changed` for timeline compatibility.
  Run SSE independently re-resolves the credential and current run access
  immediately before each data frame. Revoking a viewer closes only that
  viewer's stream; revoking or downgrading the effective actor also terminates
  execution at the next safepoint. Reconnect after revoke cannot replay older
  frames to the former recipient.
* **Rollout.** `INQTRIX_AGENT_KERNEL_ENABLED` (default on) gates
  registration together with the checkpointer rule, deepagents
  availability, and `supports_tool_calls()` on the default provider
  (failed gates WARN). Capabilities publish `features.agent_kernel`, the
  missed gate's reason under `feature_status.agent_kernel`, and the
  effective `agent.default_mode` (`INQTRIX_AGENT_DEFAULT_MODE` falls back
  to `workspace_agent` while the kernel gate fails). Setting the switch to
  `false` runs the phase machine instead; a deployment that switches back
  while kernel runs are parked or queued ends those runs at the mode
  resolver, because their mode no longer exists in the registry.

## Source policy

Source availability is a server-enforced run policy, independent of whether
the model ultimately chooses a tool. Agent-native `POST /v1/runs` requests may
add:

```json
{
  "source_policy": {
    "web": "available",
    "knowledge": "disabled"
  }
}
```

`source_policy.web` and `source_policy.knowledge` accept `available` or
`disabled`; omitting the block preserves the historical behaviour in which
both source families are available. The policy reaches both agent engines,
their planner/tool dispatch chokepoints, and delegated child runs. Disabling a
source removes its tasks from planning and rejects a violating tool/task
before it can contact the source.

The Agent Desk derives the policy from two visible switches and the
message: `web` is the session's web switch (on by default, persisted in the
session's existing `items_json`; the composer's `/web` command switches it
on — a no-op when it already is, and never a second depth control, depth
is the tier's job); `knowledge` is `available` when the message attached a
collection (`@collection`, which pins the scope to that chip) or when the
session's knowledge switch is on (`/wissen`, the glyph beside the web
glyph, persisted in `items_json.knowledge_switch`, submitted as
`knowledge_filters.scope = "all"`), and `disabled` otherwise. There is no
per-message execution route: the tier decides how deep a run goes.

The `tool_directives` field remains accepted as a planner/kernel hint for
compatible clients.

Policy precedence is fixed and restrictive: deployment availability,
identity/visibility, strict approval, and write protection first; activated
skill policy second; the session source policy third; automatic tool choice
last. A lower layer can narrow what the agent may do but cannot reopen a
source or side effect denied above it.

`/v1/capabilities` publishes `agent.source_controls[]` (`id`, `default`,
`available`, `reason_code`). `available` holds only when the capability is
registered, the service is configured AND the live probe answers; otherwise
`reason_code` names the failing leg — `operator_disabled` (not configured,
e.g. no `knowledge` profile) or `store_unreachable` (configured, but the
vector store did not answer the probe). Clients must feature-detect these
entries: a disabled or unwired backend is an unavailable control with an
explanation, never a silent fallback. A run that names collections
explicitly (chip or `/wissen`) or a knowledge-desk ask is refused at
submission with HTTP 503 `knowledge_store_unavailable` while the store is
down; a run that merely could use knowledge is admitted and its knowledge
tool fails loudly with `knowledge.store_unavailable` instead of falling
back to another source.

The composer's knowledge switch (`/wissen`, or the knowledge glyph next to
the web glyph) submits `knowledge_filters.scope = "all"`: the server resolves
it to every collection the signed-in caller may see and pins that set like
explicit chips (`explicit = true`, so the balanced gate treats it as the
user's own choice); an unscoped principal (auth mode `none`/`apikey`) keeps
the tenant-wide view its runs always had. A request carrying both chips and
the switch is pinned to its chips and persisted without `scope`; a chip list
with a non-string or empty entry is refused with 400 rather than read as
"no chips". Delegated children inherit the boundary together with its
scope label, so parent and child report the same provenance. `/v1/capabilities` advertises the wire form as
`agent.knowledge_scope_all`. The execution snapshot reports
`knowledge_scope` (`all` | `pinned` | `inherited`) and
`knowledge_collection_count` so the transcript can say which boundary a run
searched, and every `knowledge.search` capability span (a plain child of the
kernel tool observation) carries it as plain attributes:
`inqtrix.knowledge.scope` is `pinned`, with the resolved collection ids and
their count (`inqtrix.knowledge.collection_ids`,
`inqtrix.knowledge.collection_count`), or `unbounded` for an unscoped
principal, without ids or count. A trace shows the searched boundary even
when queries are not captured.

Both agent engines project the same `execution` object into state-bearing run
snapshots, the terminal result state, and the public completed-result export:

`effective_mode`, `response_form`, `depth`, `model`,
`reasoning_effort`, effective `source_policy`, `consent_reason`, and
`tool_use_counts.web|knowledge`. The key set is total (empty optional strings
and zero counts are explicit), so a client never has to infer whether a source
was merely available or actually invoked. Successful source-tool calls advance
the counters; parked kernel segments restore them from the checkpoint. See
[Runs API](../extending/runs-api.md#run-summary) for the wire example.

## Skills

Skills are SERVER-ENFORCED policy objects, not a prompt category: the
entity (`content/skills.py::SkillRecord`, table `skill_templates`,
migration 0041 with the usual RLS recipe) bundles instructions,
clarification points, a deliverable hint, a plan requirement, a tool
whitelist, and the invocation rule under one `/label`. CRUD lives at
`/v1/skills` (mandatory integer `expected_revision`, 409 on a stale write,
sharing through
`resource_type="skill_template"` with consent); SKILL.md round-trips
through `content/skill_markdown.py` (agentskills.io-compatible, policy
fields under the `x-inqtrix` frontmatter key), exposed as
`GET /v1/skills/{id}/markdown` and `POST /v1/skills/import` — the
import parser maps only the file shape, the service validator stays
the single policy gate.

* **Placeholder coupling.** Every `{{name}}` in the instructions must
  have a same-named clarification point (loud 400); points without a
  placeholder are allowed as context-only inputs. At intake a
  fast-tier check (`SkillPointCheck`) decides which points the
  question/history already answer: missing REQUIRED points join the
  options-chip clarification round (options map 1:1), missing
  optional points run on their `default_assumption` — visibly named
  in the run.
* **Admission.** The composer submits `skill_ids` (slash menu) plus
  `tool_directives` (hard planner/kernel hints from the closed
  `AGENT_TOOL_DIRECTIVES` whitelist — unknown ids are a loud 400).
  The runs router admits skills through live owner-or-accepted-direct-share
  visibility (indistinct 404, `skills_max_attached` cap) and pins each skill's
  integer `revision` into the stored run request. Every worker segment resolves
  the effective actor and re-checks both current visibility and the exact
  pinned revision. Both engines fail closed with `authorization_revoked` if
  access, policy, or instructions changed before a later segment.
* **`requires_plan` matrix.** `always` forces the plan gate in every
  mode (including Auto — the only way a skill can force review);
  `never` frees only the mission-weight default; `auto` follows the
  mode. The strictest activated skill wins; the web-search re-consent gate and
  the patch gate are untouchable by skills.
* **Model activation.** Attached skills inject as delimited
  USER-content blocks (marked as user content that cannot lift
  security or approval rules), but only after the kernel middleware has
  run the same `SkillPointCheck` and durably resolved every required input.
  Missing inputs park in deterministic options-chip batches before the first
  main-model call. Dynamic `load_skill` uses the same resolver and activates
  the skill/tool restrictions only after its required inputs are answered.
  Own `model_allowed` +
  autocomplete-eligible skills disclose as one-liners under a
  deterministic character budget (`skills_disclosure_budget_chars`,
  visible overflow line). The kernel's `load_skill` tool activates a
  disclosed skill mid-run (gated in balanced/strict, free in
  autonomous); trusted `[skill_geladen:<id>@<revision>]` transcript
  markers re-arm the exact admitted revision on every resumed segment,
  so a tool limit acquired before a park survives park/resume without
  policy drift. Shared-in skills are
  structurally never model-activatable for the recipient.
* **`allowed_tools` enforcement.** The union over activated skills
  narrows the run at the dispatch chokepoints: kernel tool bodies
  refuse blocked tools with a visible tool error, and the phase
  machine's planner receives blocked task kinds as repair errors
  (`TASK_KIND_KERNEL_TOOLS`: a task kind is allowed when the union holds
  every kernel tool its execution amounts to — a file analysis needs
  `search_project_knowledge` AND `read_project_document`).
* **Capabilities.** `features.skills` plus
  `agent.skills {max_attached, disclosure_budget_chars}`; the skill
  LIST itself comes only from the authenticated `GET /v1/skills`
  (capabilities are unauthenticated).

Settings: `INQTRIX_AGENT_SKILLS_MAX_ATTACHED` (3),
`INQTRIX_AGENT_SKILLS_DISCLOSURE_BUDGET_CHARS` (4000).

## Work profiles (internal roles)

A specialised model role that has to LOOK THINGS UP is a bounded run of the
same kernel, never a second agent loop. A work profile
(`agents/kernel/work_profiles.py`) names the role's tool surface, its typed
result, its prompt and its model-routing node; the built-in profiles are
`review` and `section_writer`.

* **The profile belongs to the server.** `RunRequest.work_profile` and
  `RunRequest.handover` are set by server code on the child submission seam
  only. `POST /v1/runs` refuses both fields with HTTP 400 — refused, not
  ignored. The profile is persisted in the replay body, published as
  `execution.work_profile`, exposed on run summaries as `work_profile`, and
  part of the compiled graph's cache key. An unknown profile name fails the run
  instead of running it as a conversation; a profile naming a tool the kernel
  does not have fails at kernel construction.
* **One visibility, two layers.** A tool outside the profile is absent from the
  role's prompt (compile time: profile ∩ source policy) AND refused at the
  dispatch chokepoint (`KernelDeps.require_tool_allowed`). A profile never widens
  a right. Roles have no delegation, no writing, no user question and no HITL
  gate.
* **A role re-reads, it never acquires.** The handover releases exactly what the
  parent run already reached: the sources in its coverage ledger that it read
  (`knowledge_document`, `editor_document`, `conversation_source`, `canvas`), its
  output canvases and its evidence ledger (`evidence_run_id`, re-authorised
  against the run's visibility at every segment start). That run is the parent
  the role works for and is parked on it while the role reads, so its own
  search answers are read like the role's own ledger — never re-authorised as a
  finished foreign origin, which it cannot be yet. Every reader checks its
  TARGET against that list (`KernelDeps.require_released`), so an allowed reader
  pointed at a foreign document is refused like a foreign tool. Because a role
  can reach nothing its parent did not reach under the user's own approvals, it
  needs none of its own — in every permission mode, `strict` included.
* **Typed handover in.** `WorkHandover` carries goal, the parent's effective
  assignment, the run's clarification record (`clarifications`, oldest first,
  empty when the run asked nothing, rendered in a fence of its own after the
  assignment's), the outputs under work (chat verbatim, canvases by
  address), the released sources, how many references lie behind the
  evidence address (a count, never a copy — it is what lets the SIZE of the
  subject travel with the address), compact run diagnostics, known gaps, the
  expected result and the answer language. ONE function renders it into the
  role's first model input (`render_handover`); the role is isolated — no
  session history, no memory briefing, no collection catalogue. It keeps the
  kernel's cognition: the advisory sufficiency judge observes its reads with
  the role's goal as the criterion, so an unread remainder of a released
  source is named to the role before it writes its result.
* **Typed result or defined failure out.** The role ends with its result model
  through the framework's structured final response
  (`ToolStrategy(..., handle_errors=False)`). The framework binds every model
  turn of a role to call a tool, and the kernel's chat bridge sends that
  obligation to the provider as `tool_choice="required"` (the trace records it
  as `inqtrix.request.tool_choice`), so a role cannot close with prose instead
  of its result call. A role graph carries no planner (`write_todos`): the
  planner's prompt asks for a closing text answer. A model or gateway that does
  not support a forced tool choice rejects the role's first model call, visibly;
  such a model cannot serve roles. A schema violation is repaired by
  the kernel's bounded model recovery exactly like rejected tool arguments (same
  feedback, same attempt budget); exhausted, it is `schema_invalid`. Free text
  instead of the result is `result_missing`; an exhausted tool or step budget is
  `budget_exhausted` — a role never parks at the user. The run stores
  `work_result = {profile, status, value, detail, read_before_result}` and
  publishes NO answer: no answer artifact, no answer events, no memory staging,
  no transcript archive, no review of its own.
* **Proof of what was read.** `read_before_result` lists the sources read BEFORE
  the turn that delivered the result — reads requested in the same turn ran
  afterwards and do not count. The role's coverage ledger is published like any
  run's (`execution.source_coverage`).
* **Budget.** A role's tool allowance is DERIVED from the size of its subject,
  because its work is linear in what it re-reads:
  `kernel_role_tool_calls_base + kernel_role_tool_calls_per_source x sources`,
  where `sources` counts the outputs under work, the references behind the
  evidence address (`WorkHandover.evidence_references`) and the released
  sources, each exactly once. It is never below the operator's normal
  allowance (`INQTRIX_AGENT_KERNEL_MAX_TOOL_CALLS`, so no role gets less than
  a run without a derived budget) and never above the allowance of the Stufe
  that commissioned it (so no role costs more than the run that ordered it).
  The step budget is priced from that allowance; neither extends. The
  defaults are a starting estimate, not a measured distribution.
  Delivering the result
  executes nothing and spends no tool budget. The token ceiling is the
  operator's run cap, as for every child.
* **Price class.** A role inherits the Stufe (or, without one, the depth) of
  the run that commissioned it, through the same inheritance rule mission and
  research children use (`inherited_stufe`). Before that, a review
  commissioned by a `tief` run started at the server's default depth: the
  thoroughness a user selected reached the parent and stopped at the handover.
  The Deep effort elevation stays local to the kernel's own reasoning loop —
  a role resolves under its own routing node, whose model and effort follow
  the user's selection.
* **Not a conversation source.** A role run holds nothing but re-read material:
  it is left out of the session's source catalogue (`search_context` /
  `read_context`) and of the session history. Its trace nests under the parent
  run; the desk shows it as one delegation row ("Prüfrolle (Unterauftrag)")
  joined to the child through the role's origin key.


### Section writer: a memo section too large for one call

A mission writes each memo section in one structured call. When a section's
work order would exceed `SECTION_IN_PROMPT_BUDGET_TOKENS` (16,000 estimated
tokens), it goes to the `section_writer` role instead: a bounded kernel child
with `read_evidence` as its only reader and `SectionText` as its typed result,
routed on the same `agent_synthesis` node as the single call. ONE function
decides the transport (`synthesis.choose_section_transport`) from the size of
the prompt the single call would carry. The bound comes from measured fixed
costs — about 2,133 tokens once for a structured call against about 5,309 per
model turn for a role that reads before it writes. With `f` the share of its
material a role actually loads, the role wins above `8,485 / (1 - f)` tokens:
the bound holds for a role that loads at least about half, a role that loads
less would win earlier, and one that loads everything wins on fitting the
material, never on cost. Measured sections sit at a median of about 1,300 and a
90th percentile of about 6,500 tokens, so the role serves the tail only. A
server that runs no kernel writes every section in one call and logs a WARNING
for each one that should have gone to the role.

* **One work order, two transports.** The role's `WorkHandover.brief` is built
  by the same builder as the single call's prompt; the only difference is that
  the evidence the section leads with arrives as addresses — each line carries
  the `reference_id` the role's ledger files it under — instead of in full.
  The run's clarifications travel in the brief in the writers' form, as in
  the single call; the handover's own record is a judging role's and stays
  empty.
* **What a section may cite never depends on its transport.** The role's prose
  goes through the same closing stage as a single-call section
  (`synthesis.finish_section`: normalisation and the citation check with its one
  repair), with the same citable labels.
* **Submit all, park once.** Every role of a synthesis round is submitted
  before the mission parks, on one `{"kind": "children"}` interrupt in its own
  `synthesis_wait` node — the same slot-free wait as research children and the
  review role. The round's outline, its sections written in one call and the
  transport of each section are carried across the park in the declared
  checkpoint fields `synthesis_progress` and `synthesis_pending`; the outline is
  never written twice, and a single-call section is paid for in the segment
  that wrote it.
* **A park is not a crash.** The synthesis task is still `running` on the
  return from the park, exactly as after a crash in the middle of writing. Only
  a committed node return sets `synthesis_pending` for the current round, so
  the return continues, while a crash before the round's first park stays
  fail-closed (`synthesis_execution_interrupted`) — nothing then tells which
  sections reached the memo. A crash AFTER a wake starts from the checkpoint the
  park committed, which does tell: the synthesis resumes from that progress,
  the outline and every section written before the park are not repeated, and
  only what the lost segment did after the wake is done again. Every way out of
  the node other than a new park leaves the flag down, so a synthesis the
  control store already records as completed is restored and routed onward,
  never back to the wait node.
* **The memo grows as a prefix of the outline.** A section still in a role's
  hands stops the fold; written sections behind it wait in the progress and
  follow in order once it returns. The memo never stands in the order the
  sections happened to finish.
* **A failed role is never a quiet gap.** A role that cannot deliver its
  typed result leaves its section to one call, with a WARNING and the activity
  `section_role_unavailable` (`status="degraded"`); only when the material
  does not fit one call either does the synthesis fail, as it did before.

## Deep mode

Thoroughness is ORTHOGONAL to the permission mode: `agent_overrides.depth`
(`normal` | `deep`, default `normal`, published as `agent.depth_modes` +
`agent.default_depth`) selects deterministic budget-plus-verification —
never an unbounded loop:

* **Effort.** The kernel node runs on `high` reasoning effort unless the
  request or a skill pin already chose one (precedence: explicit request
  effort > skill pin > deep > tier map). The tier stays with the tier
  map (`agent_kernel` is high-tier already).
* **Budgets.** The kernel `recursion_limit` rises to
  `INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_DEEP` (121 = 14 sequential tool
  turns plus the answer; one tool turn costs 8 measured super-steps, the
  answer turn 9) — a raised, still operator-bounded decision point.
  Independently,
  all model-emitted tool calls count against a checkpoint-derived
  cumulative limit (30 normal, 60 deep); an overflowing multi-call batch
  is rejected before any tool executes. Explicit extension may raise the
  effective deep limits only to 241 steps and 120 tool calls by default.
* **Child profiles.** Normal kernel `run_web_research` children are forced
  to COMPACT; Deep children and every deep phase-machine child research run
  are forced onto the DEEP report profile
  (deterministic, with no model-controlled profile argument);
  a deep kernel run delegates `run_deep_mission` children with
  `depth=deep`, so the mission's own children inherit it.
* **Verification pass.** Before finalizing, the run hands its outputs to the
  **review role** — a bounded child run of the same kernel under the work
  profile `review` (see *Work profiles* above), on the selected model and effort
  (`agent_deep_review` in the tier map, high tier by default). The role receives
  the complete effective assignment and the chat answer in its typed handover;
  every canvas created or updated by the run, the memo a delegated mission child
  wrote into the session, the run's evidence ledger and the documents the run
  read travel as ADDRESSES, and the role reads them through the kernel's own
  windowed readers. A review therefore does not have to fit one context window
  and is never skipped for size. That memo is the deliverable of a delegated
  mission: the kernel tool result names it (title, artifact id, revision). The
  post-mission advisory asks the kernel to read the memo and retained evidence
  before researching a concrete factual gap within existing permissions and
  limits. Output requirements never justify research. The review starts no
  research: the role re-reads, it never acquires.
  The parent parks slot-free (`waiting_for_children`) from its post-loop and is
  woken by the child's terminal write; the finished thread is re-entered without
  a model turn and finds the same child through its origin key (`role-review`).
  Source material, provider synthesis and internal measurements remain distinct;
  a source link or a single-source ratio is not proof of semantic correctness.
  Findings name their kind, exact unique passage and supporting basis ids
  (`source:<reference_id>`, `diagnostic:<run>`). Only a basis the role READ
  before it wrote its verdict carries a finding: a finding with an unread source
  basis is shown as an open point and never applied, and the outcome record
  carries how many cited references were re-read (`reach`) together with every
  point the role could not confirm (`lines`). `deep_review_findings` exposes the located findings
  before the one revision call, which receives the evidence of exactly those
  bases and returns only a finding id and replacement text per edit. Untouched
  output is not regenerated. All anchors resolve against the reviewed snapshot;
  overlaps, missing or ambiguous anchors, foreign grounds and foreign labels
  name their finding, and the others stay applicable. Valid replacements
  preserve all other text bytes and commit through the existing multi-canvas
  batch CAS; a canvas without an applied edit is not rewritten. An empty
  replacement of whole lines also removes one line end (the one after them, or
  at the end of the text the one before), so a deleted table row or list item
  leaves no blank line that would end its block; the re-check places every
  later replacement by the same spans. Every final
  narration after a verdict is the outcome record's `summary`, built from
  the same review facts the record is sent from: an applied edit reads
  "überarbeitet, nicht nachgeprüft", and "behoben und nachgeprüft" only when
  the re-check held it; unchecked points and open criteria are named even
  when the verdict has no finding. The
  same `critic_revision_discarded` activity as a mission's lists each named
  finding with its reason. The run then sends the same outcome record
  (`inqtrix.agent.outcome`) as a mission, built from its review in the
  mission's review-report shape: once per segment that leaves the kernel
  with a decision (the normal end directly before the checkpoint is
  deleted; a cancel at the model boundary or during the review; a graph
  error; any error after the loop; a limit's partial answer or cancel),
  never for a park and never for a role run, with every line through the
  run's reader guard. A Stufe that skips the review records it as
  `waived`, in both engines; a kernel request without a Stufe that owes no
  review by its depth records `not_run`. The desk takes its words about the
  review from this record alone, never from a phase or an activity (see
  [React UI](../deployment/react-ui.md)). Payloads and references stay
  unchanged, so new sources must enter a canvas through its normal writing tool
  before review. A role that failed, delivered no typed result, delivered an
  invalid one or exhausted its budget preserves the existing outputs with a
  terminal `kernel_deep_review` narration that names the reason. Unusable
  edits, a revision that does not fit its context and CAS conflicts preserve
  them too: the summary counts the findings they named, and
  `critic_revision_discarded` gives each one's reason.
* **Prompt bias.** The deep user-message section biases delegation
  toward `run_deep_mission` and thorough research over instant search,
  and demands explicit assumptions. The phase machine keeps its critic
  as the built-in verification pass — Deep there means DEEP research
  children, not a second reviewer.

## Dependencies

Optional extra `agent`: `deepagents` (isolated behind
`agents/harness.py`; pulls `langchain-anthropic` and
`langchain-google-genai` unconditionally — see THIRD_PARTY_NOTICES),
`langchain`, `langgraph-checkpoint-postgres`, `psycopg`. Deployments
should set `LANGGRAPH_STRICT_MSGPACK=true`. Install with
`uv sync --extra agent --extra knowledge-qdrant --extra queue-valkey`, or use
standard Python/pip with
`python -m pip install -e ".[agent,knowledge-qdrant,queue-valkey]"`.

## Related docs

- [Runs API](../extending/runs-api.md) — waiting statuses,
  agent event catalog, control endpoints
- [Knowledge engine](knowledge-engine.md) — the profiles rag tasks
  select
- [LLM calls](websearch/nodes.md) — per-node tiering
