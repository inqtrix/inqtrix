# Agent platform (Agent Desk)

> Files: `src/inqtrix/agents/`, `src/inqtrix/capabilities/`,
> `src/inqtrix/core/results.py`,
> `src/inqtrix/services/agent_control_service.py`,
> `src/inqtrix/server/routers/agent_runs.py`, migrations 0029/0030

## Scope

`mode=agent_kernel` runs the Agent Desk's agent, the cognitive kernel: a
tool-calling loop that answers directly, asks structured questions, searches
project knowledge and the web, reads and analyses documents, delegates
research and knowledge subtasks to child runs, writes canvas documents and,
when its Stufe asks for it, has its outputs reviewed once before it finishes
(see [Cognitive kernel](#cognitive-kernel-modeagent_kernel)). Everything
user-negotiable is persisted in Inqtrix control tables; run events are
signals, rows are the truth. Runs of the retired mission engine
(`mode=workspace_agent`) stay readable; see
[Older mission runs](#older-mission-runs).

Research extraction, report composition and Agent Desk writers share the
`evidence_fidelity_block()` contract in `inqtrix.prompts`. Conditions, scope and
time references must survive synthesis and revision. A rule or deadline does
not establish a current state, and source roles or valid citation labels do
not prove that the cited content supports the final sentence. The shared
instruction is present in the kernel's system prompt, the citation repair of
its chat answer, its review and its revision; it adds no model call or
semantic verifier.

When a research child's report is archived to keep the parent context small,
its evidence-review and partial-coverage notices stay in the tool message,
and so do the search notes of a knowledge child. These notices are part of
the result contract and remain visible independently of the truncated report
preview.

## Agent tiers

| Tier | Mode(s) | Control flow | Tools | Interrupts |
|---|---|---|---|---|
| S0 | `direct_llm` | code, one call | none | no |
| S1 | `research`, `knowledge` | bounded loop | web/RAG read-only | no |
| S2 | `agent_kernel` | checkpointed tool-calling loop | capabilities, children | yes |

The escalation doctrine is "smallest sufficient tier"; the UI sells
modes, never this taxonomy.

## Speed/depth Stufen (schnell | gruendlich | tief)

Orthogonal to the S0-S2 taxonomy above, the user-facing **Stufe** dial
selects how much work one Agent Desk run may do. All budgets live in ONE
frozen table (`agents/tier_policy.py`, `TIER_POLICIES`) that every
consumer reads: the kernel's search budgets and blocked tools, the child
research profile, the review gate, the form default of the kernel's first
message, and `/v1/capabilities`, which exposes the same table as
`agent.tiers` + `agent.default_tier`. What the table publishes, the kernel
enforces, with three named gaps: `clarification_rounds` is published from the
table, and the kernel enforces only its zero (no `ask_user` at all); it does
not count the rounds of a Stufe that allows some. The output form of `tief`
reaches the kernel only as a sentence of its first message, and the form a
run delivered is read from its result. `escalating` verification runs the
same review pass as `standard`.

| Stufe | Clarification | Web budget | Delegation (kernel) | Verification | Output |
|---|---|---|---|---|---|
| `schnell` | none: `ask_user` is removed, and the message asks for visibly named assumptions instead | exactly one `web_instant` and one `search_project_knowledge` (both enforced at the kernel tool chokepoint, counted per successful search; reading a document spends neither) | none; no document analysis (`analyze_document`) | citation labels only (no review) | chat answer; a document only on request |
| `gruendlich` (server default) | 1 round published | research children run profile `schnell` (1 round) | research and knowledge children (`run_web_research`, `delegate_batch` modes `research` and `knowledge`), document analysis (`analyze_document`) | the review pass | the kernel's routing: chat for a conversational answer, a canvas document for an artifact the user keeps or asks for |
| `tief` | 2 rounds published | research children run profile `compact` | research and knowledge children, document analysis | the same review pass (the kernel adds no measured quote findings of its own) | canvas document, unless the user chose a form |

The ladder answers "how much work may one turn start": one search at the
bottom, research children above it. The output column of `tief` reaches the
kernel through its message: a run without a chosen form (`auto` counts as
none) is told to write its result as a canvas document, and a form the user
chose wins. The kernel is prompt-driven, so the form a run delivered is read
from its result.

Admission is additive: `agent_overrides.agent_tier` (or the `AGENT_TIER`
env default) selects the Stufe; sending contradictory `depth` and
`agent_tier` in one request is a 400, while an explicit request `depth`
wins over a contradicting env-level tier (request beats env; the run
then keeps legacy depth semantics, logged visibly). `tief` bridges to
`depth=deep` internally so every existing depth consumer behaves
unchanged. The tier never influences model routing — model choice stays
exclusively with the model picker.

The Agent Desk composer preselects the Stufe the user picked last (the
account preference `last_agent_tier`, kept in the browser without a per-user
session) while `agent.tiers` still contains it, and `agent.default_tier`
otherwise; a Stufe picked on the page wins for that page. The composer always
submits the Stufe it shows, so the run carries an explicit `agent_tier`; the
server derives nothing from the stored pick.

A kernel research child runs the Stufe's child profile
(`web_child_profile`); a run without a Stufe gives a normal-depth child the
profile `compact` and a Deep child `deep`. The kernel tools a Stufe removes
are derived from the table (`blocked_kernel_tools`) and enforced at the
dispatch chokepoint:
`schnell` removes `ask_user`, `run_web_research`, `delegate_batch` and
`analyze_document` (and clamps the iteration ceiling); `gruendlich` and `tief`
remove nothing. `write_canvas` is never
removed — a document on request is possible in every Stufe. A run
without a Stufe (legacy `depth`) keeps every tool.

An explicit `response_form` (`chat`/`canvas`) on the request comes before
the Stufe's form default. The kernel states exactly one form rule in its
first message: the explicit choice when there is one, otherwise the
Stufe's default — never both, because two contradicting server
instructions in one message leave the choice to the model.

## Report quality pipeline

Deterministic quality building blocks of the kernel live in
`agents/report_quality.py`:

* **Citation validation** — cited `[K#]`/`[W#]` labels must exist in the
  evidence ledger. The kernel's `write_canvas` rejects unknown labels with
  a loud tool error (the kernel loop is its own repair round); a final chat
  answer has no next loop turn and gets exactly one bounded LLM repair call,
  after which only the citation syntax that is still unsupported is
  replaced with a visible marker in the answer's language
  (`[unbelegt: …]` / `[unsupported: …]`).
  Registered internal reference identifiers belong in reference metadata,
  not in reader-facing text. The same boundary checks concrete ledger IDs
  without recognizing identifier patterns or rewriting prose. Canvas writes
  reject such text before persistence; final review edits cannot introduce
  those IDs. The final chat answer reuses its bounded repair path, and an
  unusable internal-metadata repair is rejected with its usage retained.
* **Result requirement** — `POST /v1/runs` accepts `report_guidance` (typed
  text, max. 2000 chars) and `report_rule_ids` with the run request; the run
  is frozen under the composed requirement, and the kernel renders it as a
  requirement block of its first message. The Agent Desk composer sends
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
  rule pill attached before stays attached for the next task. The composer
  sends no typed requirement text; `report_guidance` comes from API clients.
  Deduplication, the server-set `report_rules` and the transcript binding
  are specified in [Runs API](../extending/runs-api.md#result-requirement).
  The kernel states the precedence under the requirement
  (`REQUIREMENT_PRECEDENCE` in `agents/prompts.py`): evidence labels stay
  even where a rule prescribes another citation style, the assignment,
  the typed free requirement and the user's clarification answers narrow
  a rule, the rule attached first wins a conflict between two rules and
  the chat answer names it, the requirement wins over a skill (attached
  or loaded), and a rule applies to the deliverable documents. No child
  inherits the requirement: research and knowledge children and role runs
  deliver to the run that holds it. The run event
  `inqtrix.agent.requirement.attached` and the run span's
  `inqtrix.requirement.*` attributes record the requirement a run starts
  under.
* **Run-wide tool grants** — a tool-gate approve accepts an additive
  `approval_scope` (`"once"` default, `"run"`). `"run"` is stored in
  the approval's `decision_payload` and folded at every segment start
  into the kernel's grant set: the gated tools of that gate stop
  gating for the rest of the run. One exception: a later batch of
  subtasks with a knowledge assignment asks again in a collection
  scope the user did not choose, unless the user granted the knowledge
  search itself for the run or gave the run-wide grant at a batch card
  that named the collections (see Policy gates).
  Grants act only in the balanced
  mode table (`strict` stays per-call by design), never cover
  `ALWAYS_GATED_TOOLS`, and surface on the execution snapshot as
  `tool_grants`. Because `decision_payload` is replay identity, an
  approve-once retry after an approve-run conflicts (409) exactly
  like a retry carrying different edited actions.

## Older mission runs

The mission engine (`mode=workspace_agent`) is retired: no server registers
it. Its earlier runs stay in the run store as they were written, and no
migration rewrites them; their plan and task rows (`run_plans`,
`run_plan_tasks`) stay as data. What stays readable:

* the run in `GET /v1/runs`, its summary and its question, with `mode` and
  `execution.effective_mode` `workspace_agent`;
* its stored event history (`GET /v1/runs/{run_id}/events`), unfiltered and
  in order, including the event types only the mission emitted (see
  [Runs API](../extending/runs-api.md#event-types-in-older-mission-runs)),
  and its children;
* its result (`GET /v1/runs/{run_id}/result`);
* its artifacts, the session memo shown in the canvas included, the sources
  behind them, and their export into an editor document;
* its approvals of every stored kind (`plan`, `replan`, `discovery`,
  `patch`) through `GET /v1/runs/{run_id}/approvals`, a plan-gate decision
  with its result requirement as parts, never as the composed text;
* the deletion of its session, which removes its plan and task rows with it.

What no longer works: a submission of `mode=workspace_agent` returns 400
with `reason: "workflow_retired"`; the plan and task routes
(`GET /v1/runs/{run_id}/plan`, `GET /v1/runs/{run_id}/tasks/{task_id}/result`,
`POST /v1/runs/{run_id}/tasks/{task_id}/cancel`) are gone; a decision on a
stored approval of another kind than `tool` answers 400 with
`reason: "workflow_retired"` and leaves the approval as it was (see
[Runs API](../extending/runs-api.md#native-runs)). A parked mission run
ends with `workflow_retired` when the process that resumes parked runs
starts: the queue worker at its start and then once a minute, without a
queue worker the API process once at its start.

A kernel turn that follows a mission turn in the same session builds its
session context from the stored rows of every earlier turn, the mission's
included, and `read_evidence` reads a mission origin's persisted evidence
artifact like any other (see *Tools* under
[Cognitive kernel](#cognitive-kernel-modeagent_kernel)). When the kernel's
review reads references from a completed mission run, it also reads that
run's latest stored plan: each task's stored document-analysis reading
becomes a reading record of the run diagnostics, and a task whose stored
result names an evidence limit (evidence-contract status, an evidence budget
cut or a search limit) becomes a diagnostic of its own (`scope:
"mission_task"`, keyed by `mission_run_id`; see *What the review reads and
checks* under [Deep mode](#deep-mode)).

## Feature gate

Agent runs are set up only when `INQTRIX_AGENT_ENABLED` is on,
the `agent` extra is installed, and checkpoints can survive:
`INQTRIX_STORAGE_BACKEND=postgres`, or the dev escape
`INQTRIX_AGENT_ALLOW_VOLATILE=true` (InMemorySaver, WARNING logged,
`agent.durable: false`). Only then does `/v1/capabilities` report
`features.agent_desk: true`; the kernel registers when its own gate passes
(see *Rollout* under [Cognitive kernel](#cognitive-kernel-modeagent_kernel)).
A submission of the retired `mode=workspace_agent` returns 400 with
`reason: "workflow_retired"` (see [Older mission runs](#older-mission-runs)).
Queue-mode parking is fenced: the
worker's handle parks with the claim fence and ACKs the dispatch message;
the resume re-enqueues a fresh message that any worker continues from the
persisted payload + checkpoint.

## Workflow contracts

A checkpoint resumes only into the graph shape that wrote it. Every
algorithm therefore names a `contract_version`, and every run is bound to its
mode and to the version that mode carried when the run was accepted (the
`runs.workflow_contract` column). A run claimed from the queue executes only
under its own contract: a run that never ran takes the worker's version; a
run with stored state from an older version, or of a retired mode, ends as
`cancelled` (`workflow_contract_changed` / `workflow_retired`) and loses its
checkpoint, and its active children are cancelled (`parent_cancelled`); a newer
version or a mode the worker has not registered stops that worker's claims
until the run is cancelled, ends or parks; the worker then claims again by
itself. Parked runs of the first kind end
at once when a worker starts and then once a minute, so nobody approves a run
that could no longer continue. Runs of the second kind an instance admin
cancels per mode after a rollback (`cancelled_by_admin`); a claim resolves the
pending cancel before it judges the binding, so such a run no longer stops the
worker that takes it over.

`tests/architecture/workflow_contracts.json` pins the checkpoint-relevant
shape of each workflow with a checkpointer (`agent_kernel` per autonomy
variant and work profile): graph nodes and edges, state
channels with their kind and snapshot frequency, every tool the graph can run
with a hash of its argument schema, and the interrupt kinds with the payload
keys they need. `tests/architecture/test_workflow_contract_pins.py` compares
the compiled graphs with that pin:

* **Break** — a changed node (a renamed middleware is one), edge, channel or
  interrupt kind, or a tool removed or given other arguments. Raise the
  workflow's `contract_version`, then re-pin.
* **Addition** — a new tool. A run parked with an open call resumes with it
  (`tests/harness/test_resume_with_added_tool.py`), so no increment; only
  re-pin.

Re-pin with `uv run python -m tests.architecture.workflow_pins`; it refuses a
break that keeps the pinned version. Prose in a tool's argument schema
(descriptions, titles, examples) is no part of the pin.

## Settings (`INQTRIX_AGENT_*`)

`enabled`, `max_parallel_queries_per_task` (3), `default_autonomy` (`balanced`),
`advanced_autonomy` (off), `allow_autonomous` (on),
`allow_volatile` (off). All enforced in code, never prompted. The settings of
the retired mission engine are retired settings: a set one warns, or stops
the start when it recorded a deliberate choice
([Settings and environment](../configuration/settings-and-env.md#agent-platform-agentplatformsettings)).
Model tiers: `agent_sufficiency` and `agent_skill_point_check` fast,
`agent_file_analysis` mid, `agent_kernel` and `agent_deep_review` high. A
run's `agent_overrides.model/model_tier/effort` (and a skill pin) reaches only
the thinking nodes, `agent_kernel` and `agent_deep_review`. The review runs
under the one node `agent_deep_review` and defaults to high. Sufficiency,
file analysis and the skill-point check retain their own tiers. The selected
settings are recorded before the kernel's automatic Deep effort increase and
passed to knowledge children and role runs; research children keep their own
routing. The increase stays local to the kernel loop and its revision.

The normal/deep kernel bases are `kernel_max_tool_calls` (30/60 through
the deep variant) and `kernel_max_iterations` (120/120: fourteen tool turns
plus the answer in both — deep buys more tool calls and the verification
pass, not more turns). Explicit extensions are bounded independently by
`kernel_max_tool_calls_extension_ceiling` (60/120) and
`kernel_max_iterations_extension_ceiling` (240/240), again with deep
variants. A configured ceiling below its base cannot shrink the base; the
effective ceiling is their maximum. `/v1/capabilities.agent.limits` publishes
these pairs together with the document-analysis bounds, the research round
limit, and the non-extendable token ceiling.
Every live Agent execution snapshot publishes measured usage and the
effective boundary in `execution.limits`; older servers omit the capability
block and the UI hides it rather than guessing.

The manifest also publishes the effective `schnell` kernel boundary (96
steps and no in-run extension at the defaults: the published search, one
knowledge/todo turn, one slack turn, plus eight read-only turns on evidence
the session already retains, canvas documents, attached reports and the
windows of a named document, so a follow-up reads before it answers and a
document can be read to its end)
instead of presenting the normal tier's larger allowance before submission.

## Interaction layer

* **Two permission modes.** The UI presents
  Standard/Auto (`/v1/capabilities` `agent.mode_presets`, mapped onto
  the unchanged wire vocabulary `balanced`/`autonomous`;
  `advanced_autonomy` republishes the legacy three-way control incl.
  `strict`; `allow_autonomous=false` takes Auto away from the publication,
  and `POST /v1/runs` then refuses it for an agent run). What each mode
  gates is published as `agent.permission_modes`, generated from the
  kernel's policy config (see *Policy gates* under
  [Cognitive kernel](#cognitive-kernel-modeagent_kernel)).
* **Structured clarifications.** A gate round carries 1-3 questions
  with 2-4 pickable options each (`ClarificationRecord.questions`,
  sanitized deterministic ids): `ask_user` asks one question per round,
  and the missing required inputs of a skill are asked in rounds of up to
  three, one question per input. Free text stays
  possible per question AND as a whole-round answer (`answer`), so older
  clients keep working. Answers land in `answers` (per-question map) and
  compose into the resume history through ONE shared function
  (`round_qa_lines`).
* **Chat-form deliverable.** `response_form` on the run request
  (`auto|chat|canvas`, agent-only) states the turn's deliverable form to
  the kernel (see the Stufen above): `chat` publishes the answer as the
  run-local `answer` artifact rendered inline in the transcript, `canvas`
  asks for the result as a canvas document (`write_canvas`) with a short
  chat note, and `auto` leaves the form to the surface an attached skill
  pins (see Skills, "Output form"), then to the Stufe's default and the
  kernel's routing.
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
* **Long-term memory.** Optional and personal. Accepted long-term memories
  live behind `AgentMemoryProvider` (first provider: self-hosted Mem0) and
  the Inqtrix `AgentMemoryService`. Anonymous/static principals cannot use
  it; namespaces are derived server-side from `(tenant_id,
  principal.user_id)`, and client owner fields are rejected. With the
  user's opt-in, a kernel run reads a non-evidentiary memory briefing at its
  start, and in the default candidate-only learning mode it stages
  `MemoryCandidate` rows from its final answer after a successful run; users
  accept, edit or reject them in Settings. Memory is never evidence and must
  not be cited.

Memory knobs: `memory_provider` (`none`/`mem0`), `memory_mode`
(`off`/`candidate_only`/`auto_safe`), `mem0_base_url`, `mem0_api_key`.
`auto_safe` is accepted for forward compatibility but currently reports
`effective_mode: candidate_only` with a degraded reason; it does not
auto-retain memories yet.

## Cognitive kernel (`mode=agent_kernel`)

The Agent Desk's one **orchestrator**: an LLM
tool-calling loop on LangChain's `create_agent`, assembled through the one
harness seam (`agents/harness.py::build_kernel_agent`) with the kernel's own
state (`agents/kernel/state.py`: `messages` as a delta channel that stores
writes and folds them with the public `add_messages`, a full snapshot every
50 steps), and with contract tests in
`tests/agents/test_harness_kernel_contract.py` and
`tests/harness/runtime/test_public_seams.py` as the upgrade gate. The mission
engine it replaced is retired (see [Older mission runs](#older-mission-runs)).

* **Two-layer architecture.** The kernel (`agents/kernel/`) is the
  conversational brain: it answers directly, asks structured questions
  (`ask_user` -> the structured clarification machinery, deterministic ids
  hashed from the checkpointed `tool_call_id`), searches, writes canvas
  deliverables, and DELEGATES research and knowledge subtasks to child runs
  (`run_web_research`, `delegate_batch`) and its review to a role run (see
  *Work profiles*).
  It is not a second retrieval implementation. `search_project_knowledge` is
  a thin orchestration adapter around the same `knowledge.search` capability
  and `KnowledgeService.search` used by the Knowledge Desk, and a knowledge
  child is a run of the knowledge mode itself. Dense/sparse fusion,
  mixed-model partition/rank fusion,
  reranking, authorization, active-revision filtering, degradation reporting,
  and canonical evidence projection therefore remain owned by the Knowledge
  subsystem. Likewise, the kernel's instant web search and its research
  children use the configured provider search and the same web-search ledger
  contract; neither creates a separate crawler or page-reader path.
* **Same platform seams.** Registry algorithm behind the normal
  RunService -> queue -> worker path; sync `graph.stream` only; shared
  checkpointer with `thread_id=run_id`; park/resume against control
  rows. The stream runs with `durability="sync"`: each superstep's
  checkpoint is stored before the next superstep starts, so the
  checkpoint holding a model step's tool calls is stored before any of
  those tools runs, and LangGraph's background checkpoint chain cannot
  exhaust its own thread pool (under the library default `"async"`, a run
  with many fast supersteps can wait on itself forever). Each superstep
  therefore waits for one checkpoint write; with Postgres that is a round
  trip through the checkpointer pool. Compiled graph variants are cached
  per `(autonomy, source_policy.web, source_policy.knowledge, max_tool_calls)` —
  `interrupt_on` and the enforced source tool
  surface are both compile-time; per-segment state travels through a
  ContextVar, so the compiled graphs are stateless and shared across
  users.
* **Checkpointing.** `PostgresSaver` (sync — worker threads) on its own
  psycopg pool with `thread_id=run_id`; its tables are library-owned
  (`saver.setup()`), NOT Alembic-managed. The checkpoint is ONLY a
  resumability cache: a run whose checkpoint is missing starts afresh only
  when its approvals, clarifications, artifacts and children are all read
  and all empty (`agents/checkpoint_guard.py`).
  Otherwise it fails loudly ("The checkpoint for this agent run was lost")
  while control and artifact data stay intact, and the assignment restarts
  as a new run. Terminal runs delete their checkpoint thread.
* **Effective actor and resume authority.** A run keeps its original owner,
  but every executable segment has an explicit `execution_actor_user_id` and
  scope ceiling. Approve, edit, or resume atomically records the deciding
  editor as the effective actor together with the control decision, run
  revision, and next status. The worker resolves that canonical user live and
  uses the same actor for quotas, audit, child runs, skills, knowledge, and
  tools. It never reconstructs or falls back to the owner's authority. A
  shared editor can therefore continue the run only with resources that editor
  may currently use.
* **Pinned dependencies and revocation safepoints.** An unscoped "all visible
  collections" request is normalized to concrete collection ids before
  execution, and every attached skill is pinned by id plus integer revision.
  The worker re-checks the actor, run edit authority, collection visibility,
  and skill revision before and after provider/search calls and at tool,
  knowledge, skill, child-run, segment, and final-publication boundaries. If
  access disappears, the run ends with `authorization_revoked`; it does not
  continue with a silently smaller evidence set. A run with
  `source_policy.knowledge` set to `disabled` has no tool that reaches a
  collection, so collection visibility is no dependency of its actor: a shared
  editor can continue it without access to the owner's collections. A remote
  call already in flight cannot be recalled, but its return value is discarded
  after the post-call check and is not persisted or emitted.
* **Scoping.** Every internal tool call is pinned to the current effective
  actor's live resource rights. Explicit collection ids are asserted; an
  omitted scope is normalized to that actor's visible collections rather than
  being rediscovered under the owner later. The shared Knowledge retrieval
  implementation partitions a mixed embedding-model scope by model, embeds
  once per group, and rank-fuses the group results before the common
  reranker. Neither the kernel nor its tool adapter selects only the default
  model.
* **Web query discipline.** The kernel's tool discipline carries
  `EXTERNAL_SEARCH_RULE`: the text a web tool sends out leaves verbatim to an
  external search provider and names only the public subject, never a
  project document, its title or heading, a collection, or anything only the
  project's documents say. Whether a project document itself is still
  current is no web question (`PROJECT_DOCUMENT_CURRENCY_RULE`). Limit: these
  are prompt rules; no deterministic check reads the query text before it
  leaves.
* **Policy gates.** `agents/kernel/policy.py` maps autonomy onto HITL
  gates: balanced gates `web_instant` (query verbatim in the approval
  payload), UN-scoped `search_project_knowledge` (a `when` predicate),
  and both child-run tools (`run_web_research`, `delegate_batch`). A grant of `delegate_batch` for the run covers a batch
  with a knowledge assignment only when the user picked the run's knowledge
  scope (attached collections, or the session switch for every visible
  collection), granted the knowledge search itself for the run, or gave the
  grant at the approval of a batch with a knowledge assignment whose card
  named the collections: over a scope the user did not pick, a batch granted
  at a card of research alone asks again, as the agent's own knowledge
  search does, and that search keeps asking for itself. The approval of a
  batch with a knowledge assignment names the collections. Strict also gates broad internal reads,
  while
  `read_evidence` consumes already registered evidence without a new approval;
  `propose_editor_patch` gates in EVERY mode, and no run-wide grant frees it.
  Its gate carries one predicate in every mode: a proposal to a document the
  run's last read found in collaboration mode asks nothing, because such a
  document accepts no agent proposal and the tool refuses the call without
  effect (see Editor read/propose discipline). A gate parks the
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
  An assignment of `delegate_batch` names its mode: `research` (a web
  research child), `knowledge` (a child of the knowledge run mode that
  answers one question from project knowledge). Each mode passes the check
  of the single tool it stands for (`run_web_research`,
  `search_project_knowledge`), so the batch itself is
  offered while the web or project knowledge is available. A knowledge child
  searches inside the collections of its parent with the knowledge profile of
  the parent's tier and the model the user chose for the run; a run with an
  explicitly empty collection boundary and a server without the knowledge run
  mode start none. An assignment that may not run is refused as a failed tool
  result before any child of the call is submitted. Of a research child's
  references only the cited ones become the run's evidence; a source the
  child examined without citing it stays in the child's report. Of a
  knowledge child the
  batch hands over the evidence its answer cites, and the notes of its search
  (a hit list a limit cut short says so). The knowledge documents the results
  of a batch's children name, cited or not, are materials of the run's
  transcript, like the hits of the agent's own search.
  `run_web_research` and the research assignments of `delegate_batch` take an
  optional `recency` (`day`, `week`, `month`, `year`): how far back from today
  the child is to prefer its sources. A missing, empty or null value names
  none, and the four are read whatever their case and surrounding blanks. A
  named window reaches the child as the `web_recency` of its run request,
  takes precedence over the window the child's own classifier would derive
  and goes to each of its searches, where a search provider that takes the
  hint puts it before the query; it is no filter on the results. Any other
  value, and a window on an assignment that is no research, is refused as a
  failed tool result before a child is submitted (in a batch: before any of
  them), which the model can correct. The approval of the call shows the
  window, or that a named one is not applied.
  `web_instant` takes an optional `answer_form` (`overview`, `table`,
  `timeline`): how this one search lays out what it found, which is not the
  answer contract of a research run. The default `overview`, a missing and a
  null value ask for nothing: the request to the search provider, the record
  of the search and its id are the ones of a search without the argument. A
  `table` or a `timeline` goes out as one fixed brief about the form of the
  answer, never as text of the model, and only to a search provider that names
  `answer_form` among its parameters; another provider searches as usual, and
  the first line of the tool result and the log say so. The record of the
  search keeps the form that was asked for and whether it was handed to the
  provider, the same question in another form is a search of its own, and a
  name outside the three runs no search. The approval of the call shows the
  requested form and offers the forms as a choice when the call is edited; it
  shows what the call asks for, not whether the search provider of the run
  takes a form.
  `read_evidence` indexes retained references/searches without a selector, or
  reads one `reference_id` or `query_id`. It returns available source excerpts,
  explicitly identified provider context and the producing search answers;
  it never fetches linked pages. Attached research reports import their ledger,
  and historical origins are loaded with the caller's visibility. An unreadable
  origin is reported, including when a local search projection already exists.
  The accompanying label/identifier catalog includes every registered reference,
  including when a large report body is moved to readable archive chunks.
  Reference registration, label translation and the catalog share the same set.
  Research origins supply their exported ledger; agent origins (kernel runs
  and stored mission runs) supply the same persisted evidence artifact used
  by Canvas provenance.
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
  card budget with output reserve. If reduction is needed, tool results give way
  to placeholders that name how to read them again, in the order the rule for
  answered results below protects them: answered results outside the newest three
  first, then answered ones among them, last results the model has not seen yet,
  whose placeholder names no evidence; the largest first in each, and a result
  without a reader in that call is passed over. A result that cannot be read
  again is archived in full first. This makes no model call and stores nothing
  in the checkpoint. An input that does not fit even then
  stops visibly with originals retained; it never falls back to prefix slicing.
  The same placeholders clear answered tool results from the model input before
  any context compaction summary, from 100,000 tokens or the compaction trigger,
  whichever is lower; the newest three results, the user's answers and loaded
  skills stay verbatim.
  Unknown model cards retain provider-driven context recovery.
  Public captured archives reject edits and renames with `archive_immutable` (HTTP 409).
  Ordinary Canvas documents keep their complete Markdown, revision and editing
  contract; archive-only parameters are rejected for these documents.
  The kernel's final response ends with the requested result rather than
  unsolicited follow-up offers or internal run rules. Missing project knowledge
  is mentioned only when the assignment requires it. Relevant limitations,
  necessary clarifications and explicitly requested next steps remain part of
  the response.
* **Evidence ledger.** References are deduplicated by the platform citation
  identity (`doc:{id}#{chunk}` / canonical URL) and get stable `K#`/`W#`
  labels in first-seen order. Exact retrieval fields
  (`source_text`/`excerpt`) remain source passages. Generated retrieval
  context is never exposed through this contract. Web evidence is the
  complete provider-grounded search result: exact query, coherent provider
  answer, provider snippets, and every returned link. Inqtrix does not fetch
  those linked pages. A URL tier is quality metadata and cannot discard an
  `unknown` result. Because one coherent provider answer may synthesize
  several links, the ledger records whether a citation has an explicit
  provider mapping, a provider snippet, or only source-list membership; it
  never invents a one-to-one passage mapping. Child research reports and
  their web-search ledgers reach the kernel without a second page-read gate.
  The kernel persists this ledger in the existing evidence artifact so
  park/resume and the evidence Canvas retain the exact query, answer, links,
  and stable `reference_id` labels. The ledger of a kernel run also holds
  what the run was handed: the references of earlier turns of its session
  and of the documents and reports it reads, so that their labels stay
  citable. An answer with citation labels lists exactly what it cites,
  whoever found it. An answer without one lists only what this run's own
  source tools gathered (search hits, cited passages, the passages of a
  document analysis, web searches, the evidence of a research or knowledge
  subtask it delegated), never a source only an earlier turn or a read
  document brought. The run stores which references those are with the
  ledger (`gathered_reference_ids` in the artifact payload), so the list is
  the same after park/resume. Agent-generated Markdown is normalized at
  write boundaries so currency dollars cannot open accidental KaTeX spans;
  fenced/inline code, URLs, block math, and recognized inline formulas
  remain byte-preserved.
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
* **Document analysis.** `analyze_document(document_ids, objective, fields)`
  has the server read whole project documents (at most
  `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_DOCUMENTS` per call, one to five
  fields) with the window sweep of `agents/document_analysis.py`: one
  structured call of the `agent_file_analysis` node per window, at most
  `INQTRIX_AGENT_MAX_PARALLEL_QUERIES_PER_TASK` at a time, every reported
  passage checked through `knowledge.document.cite`, and no call that joins
  the passages into an answer.
  The sweep reads each document window by window through
  `knowledge.document.read` (20,000 characters per window, 1,000 of them
  shared with the next: 4 windows for 66,389 characters, 32 for 602,381),
  pinned to the version and content hash of window 0; a change mid-sweep
  fails that document (`document_changed`). One call analyses at most
  `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS` windows; what a budget leaves
  is a named, unanalysed range. A window call sees the objective, the fields
  as numbered criteria and its own window, title and position inside the
  untrusted fence. An instruction in one window reaches only that window's
  extraction, never another window's reading or the choice of evidence, and
  it cannot change what is read, because the sweep's capabilities are fixed
  in code. The window's system prompt states the limits the server enforces
  on every quote (passages per window, quote length, minimum words), built
  from the same constants. A provider failure stops the calls not yet made:
  the failing window names `provider_error`, the windows after it
  `not_called_after_failure`, and what was analysed stays. A content refusal
  of one window (a terminal rejection whose structured code is
  `content_filter`, `provider_failure_contract.CONTENT_REFUSAL_CODES`) is
  that window's own reason, `content_refused`, and stops nothing.
  `knowledge.document.cite` maps each quote to evidence: it must be verbatim
  (the ONE normalizer, `knowledge/grounding.py`) in the window the model saw,
  inside one stored chunk overlapping that window, of the pinned content, and
  pass the evidence projector. A quote needs at least three words
  (`too_short`); one longer than the call carries is rejected before it
  (`too_long`); every other refusal keeps a named status (`not_in_window`,
  `spans_chunks`, `chunk_unverified`, `chunk_stale`), and the quotes travel in
  batches of at most 250. Every limit a reading can name is a row of
  `document_analysis.REASONS`: a *coverage* limit blocks completeness and
  absence, an *evidence* limit (a named passage that did not become evidence,
  passages beyond the per-window limit, a window that may hold more) blocks
  absence only, and `source_bound` marks the two limits of the stored document
  version itself (a page without a text layer, an unrecorded extraction) that
  every re-reading returns. An unknown reason blocks as much as its kind can.
  `reading_blockers()` lists the limits of a stored report and
  `reading_predicates()` derives `complete` and `absence_supported` from them,
  never from a stored flag. Per document, `text_read_to_end` (every character
  of the stored text analysed) is not the same as `document_complete()` (and
  nothing known missing from that text).
  The model reads a table: per document and
  field exactly one state, `passages` (the evidence labels of verified
  passages the reading named for the field; what the document says about the
  field stands in their text, and a passage can also show that the document
  does not state it), `not_found` (only after the document was read
  completely and every passage the reading named became evidence),
  `not_found_in_verified_passages` (read completely, but a named passage did
  not become evidence, a verified passage names no field, or a window may
  hold further passages) or `not_read`
  (with each reason; a limit of another document never changes a cell), then
  the verified section texts (twelve or 24,000 characters at most; the rest
  listed with their reference ids for `read_evidence`). A `passages` cell names
  the limits of its document's reading as reasons. The model says that a
  document does not state something only for `not_found`, or for a `passages`
  cell without such a limit, of a document read completely, whose passages do
  not state it. Which field a passage
  serves, the window model says; the server checks that the number names a
  field. No quote, claim or verdict of a window call reaches the model, the
  transcript or the archive: the tool's artifact keeps the reading record
  (documents with their reading facts, findings by id, section and evidence
  id, the windows granted, the cells) and binds every opened document as
  material. The windows of all analyses of a run share
  `INQTRIX_AGENT_DOCUMENT_ANALYSIS_MAX_WINDOWS_PER_RUN`, counted from the
  stored records at every segment start; a failed analysis keeps the windows
  it claimed. A call counts once as a use of project knowledge, and only when
  it read something. Each analysed document gets one live activity row
  (`document_analysis`, keyed by the call), sent for the first and every
  fifth processed window and when it settles. The Stufe `schnell` blocks the
  tool; a skill whose `allowed_tools` names `read_project_document` allows
  it; `strict` asks first and the card names the run's collections. What the
  server read is published as `execution.document_analyses`; the coverage
  ledger stays the record of what the model read, and an analysed range
  closes an open remainder only in the sufficiency judge's advice; the judge
  also reads for which field the reading named a cited section. The run's review reads each
  analysis as a reading record of the run itself (server facts, its objective
  and fields, never a quote or claim of a window call), and the review role may
  re-read every document an analysis opened. The kernel's system prompt tells
  the model to analyse when an assignment needs named facts out of whole
  project documents, and to read windows with `read_project_document` to
  follow a passage in its context, to render a document's course or where the
  analysis is not available; the tool's description adds that its evidence
  labels are cited as they are.
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
  (windows, search hits and the documents the children of a batch of
  subtasks answered from) and editor documents (with the comment ids
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
  re-read). Both name the document's `content_mode` in their result,
  outside the fenced text, and record it with the read in their tool
  message's artifact (`editor_read`: `document_id`, `revision`,
  `content_mode`); a resumed segment rebuilds the mode from that record
  only, when it matches the marker of the same message, never from the
  result text, so it judges a proposal the way the parked segment did. A
  read stored without the record leaves the mode unknown.
  `propose_editor_patch` is ENFORCED read-before-propose: it
  refuses unread targets, refuses any document other than the run's
  attached `document_id` target, refuses a document the last read found
  in collaboration mode (`editor.collaboration_document`, the text the
  read result already gave: such a document accepts no agent proposal,
  so the answer describes the change; no approval card, no call of the
  patch service, which refuses such a proposal as well), and pins the
  receipt revision as `expected_revision` — the propose path answers a
  moved document with the same `editor.patch_revision_conflict` (409)
  apply uses, so a proposal can never anchor against text its author has
  not seen. The refused call counts against the tool-call limit and shows
  as a failed tool row; the prompt and the read result keep the model
  from making it.
  Editor content is fenced: shared documents can
  carry other people's insertions — data, never instructions. Both read
  tools gate in `strict`; skills may allow them (`SKILL_ALLOWED_TOOLS`).
  Source tier is quality metadata rather than a domain admission list. An
  unknown publisher remains in the ledger and its complete provider-grounded
  answer reaches the model; the tier never silently removes the information.
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
  The history block keeps the user's own earlier questions outside as
  instructions still in force; every other stored text (answers,
  clarifications, notes) stands in a `session-history` fence, and a
  transcript an API client sends splits the same way into a
  `client-history` fence. A shortened question ends in a visible marker;
  where the run can read conversation sources, the marker names that
  question's original as `read_context(source_id=…)`.
  The kernel system prompt carries the two shared answer blocks from
  `prompt_language.py`: `OUTPUT_FORMAT_BLOCK` (what the renderer supports) and
  `WRITING_STYLE_BLOCK` (the house writing style of conversational answers).
* **Prompt language.** The kernel system prompt, every other prompt of a
  kernel run, the tool descriptions and every text a tool hands back to the
  model are English; the ANSWER language is a per-run fact, which the kernel
  resolves from the question. The directive for that language
  (`directive_for`) is the FIRST line of the kernel's user message and of
  every prompt that writes reader text: the review handover (in one call or
  for the role), the revision, the document-window calls and the memory
  reflection. Structured-output schema descriptions follow the same contract:
  they describe the expected fields in English and defer reader-facing text
  to the answer language rather than naming a fixed language.
  Server-written reader text (default titles such as the answer artifact's,
  the outcome record's summary and lines, transcript lines) comes from the
  i18n table in that language. External content reaches the
  model inside `<untrusted_content source="…">` fences (`untrusted_fence`,
  `inqtrix.untrusted_content`): source excerpts, search answers, documents,
  earlier outputs handed to a reviser or a memory reflection, the questions,
  option wording and
  assumptions of the run's clarification record, the older conversation a
  context compaction condenses and the summary it puts back. The delimiter is
  neutralized inside the payload in every spelling, so embedded text can
  neither close a fence nor forge one; titles and URLs that stand on the
  server's own lines outside a fence are neutralized the same way. What a
  fence means is stated once, in `UNTRUSTED_CONTENT_RULE`, and the provider
  layer appends it to the system instruction of every model call
  (`UntrustedContentRuleProvider`, applied by `instrument_providers`) — also
  to calls whose caller passes no system prompt, such as the sufficiency
  judge and the revision. It sits outside the tracing wrapper,
  so a generation's recorded system instructions show it. Text that directs
  the work is never fenced: after a context compaction the run's assignment
  (its first message and skill block), what the user typed in answer to the
  run's clarifying questions and the instructions of skills the model loaded
  stand verbatim before the fenced summary; the questions themselves and the
  wording of a chosen option are the model's own text and come back as the
  clarification record in its fence. The revision reads the user's own
  free-text answers outside the clarification record's fence. Fences and the
  rule stop a forged boundary and lower the chance
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
  registration together with the checkpointer rule, the agent runtime
  (LangChain) being installed, and `supports_tool_calls()` on the default provider
  (failed gates WARN). Capabilities publish `features.agent_kernel`, the
  missed gate's reason under `feature_status.agent_kernel`, and the
  effective `agent.default_mode` (`agent_kernel`, or `null` while the kernel
  gate fails). Setting the switch to `false` switches agent runs off: the
  desk stays and names `operator_disabled`; a deployment that switches off
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
both source families are available. The policy reaches the kernel's
compiled tool surface, its tool dispatch chokepoint, and delegated child
runs. Disabling a source removes its tools from the compiled graph and
rejects a violating tool call before it can contact the source.

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

The `tool_directives` field remains accepted as a kernel hint for
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
vector store did not answer the probe; for Qdrant that is a read-only call,
repeated once when it fails, both calls inside the probe's 2 s bound, and
every failed call logs a `WARNING` with its exception type). Clients must
feature-detect these entries: a disabled or unwired backend is an
unavailable control with an explanation, never a silent fallback. A run that
names collections explicitly (chip or `/wissen`) or a knowledge-desk ask is
refused at submission with HTTP 503 `knowledge_store_unavailable` while the
store is down; a run that merely could use knowledge is admitted and its
knowledge tool fails loudly with `knowledge.store_unavailable` instead of
falling back to another source.

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

The kernel projects one `execution` object into state-bearing run
snapshots, the terminal result state, and the public completed-result export:

`effective_mode`, `response_form`, `depth`, `model`,
`reasoning_effort`, effective `source_policy`, `consent_reason`, and
`tool_use_counts.web|knowledge`. The key set is total (empty optional strings
and zero counts are explicit), so a client never has to infer whether a source
was merely available or actually invoked. Successful source-tool calls advance
the counters; parked kernel segments restore them from the checkpoint. The
counters count uses of a source. The searches stand beside them as
`web_searches` and `knowledge_searches`; a stored mission run kept no count
of its knowledge searches alone and reads `null` there (not recorded). See
[Runs API](../extending/runs-api.md#run-summary) for the wire example.

## Skills

Skills are SERVER-ENFORCED policy objects, not a prompt category: the
entity (`content/skills.py::SkillRecord`, table `skill_templates`,
migration 0041 with the usual RLS recipe) bundles instructions,
clarification points, a deliverable hint, an approval requirement
(`requires_plan`), a tool whitelist, and the invocation rule under one
`/label`. CRUD lives at
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
  placeholder are allowed as context-only inputs. Before a skill's
  instructions activate, a fast-tier check (`SkillPointCheck`) decides
  which points the question/history already answer: missing REQUIRED
  points become options-chip clarification rounds (options map 1:1),
  missing optional points run on their `default_assumption` — visibly
  named in the run.
* **Admission.** The composer submits `skill_ids` (slash menu) plus
  `tool_directives` (hard kernel hints from the closed
  `AGENT_TOOL_DIRECTIVES` whitelist — unknown ids are a loud 400).
  The runs router admits skills through live owner-or-accepted-direct-share
  visibility (indistinct 404, `skills_max_attached` cap) and pins each skill's
  integer `revision` into the stored run request. Every worker segment resolves
  the effective actor and re-checks both current visibility and the exact
  pinned revision. The kernel fails closed with `authorization_revoked` if
  access, policy, or instructions changed before a later segment.
* **`requires_plan` matrix.** `always` keeps a run under the approval
  gates in every mode: an attached skill runs an autonomous run under the
  balanced gates (the only way a skill can force review), and loading such
  a skill mid-run in Auto is refused, because the gate set is compiled; `auto`
  follows the mode; `never` changes nothing in the kernel. The strictest
  activated skill wins; the patch gate (`propose_editor_patch`) is
  untouchable by skills, and so is the one call it lets pass without a
  card, the proposal to a collaboration document the tool refuses.
* **Model activation.** Attached skills inject as delimited
  USER-content blocks (marked as user content that cannot lift
  security or approval rules), but only after the kernel middleware has
  run the same `SkillPointCheck` and durably resolved every required input.
  Each frame names whose instructions it carries as a kind, never as a
  person: `the user's own skill` or `shared by another user`, decided by
  the access rule (owner id against the run's principal); a caller without
  a viewer keeps the neutral `user content`. The point check reads only
  words the user wrote: the assignment, their earlier lines and their
  answers to clarifications, never an earlier answer of the agent.
  Missing inputs park in deterministic options-chip batches before the first
  main-model call. Dynamic `load_skill` uses the same resolver and activates
  the skill/tool restrictions only after its required inputs are answered.
  Own `model_allowed` +
  autocomplete-eligible skills (ownerless ones in the modes without user
  accounts) disclose as one-liners under a
  deterministic character budget (`skills_disclosure_budget_chars`,
  visible overflow line). The kernel's `load_skill` tool activates a
  disclosed skill mid-run (gated in balanced/strict, free in
  autonomous); trusted `[skill_loaded:<id>@<revision>]` transcript
  markers re-arm the exact admitted revision on every resumed segment,
  so a tool limit acquired before a park survives park/resume without
  policy drift. Shared-in skills are
  structurally never model-activatable for the recipient, who can still
  attach them. Who may activate a skill stays the owner's choice: a
  recipient with an edit grant rewrites the skill, but a `PUT` of theirs
  that changes `invocation` is refused as a whole (400), in either
  direction, so an editor cannot make the owner's model load the editor's
  text unasked. A disclosed one-liner is one line: line breaks in a
  description or in `when_to_use` fold to spaces. An editor can still
  rewrite the text of a skill its owner already set to `model_allowed`;
  the record keeps no last writer.
* **Output form.** A skill's `deliverable` holds two kinds of value.
  `chat` and `canvas` name the surface the result lands on: they are the
  second level of the kernel's one output-form rule, stated once in the
  run's first message. The user's explicit `response_form` comes first,
  then the first attached skill that pins a surface, then the Stufe's
  default (a `tief` run writes a canvas document). A skill loaded mid-run
  cannot change the rule. `email` and `talking_points` name the form of
  the text: a format line in the skill's block, with the surface left to
  the same rule. An empty value leaves the form to the agent.
* **`allowed_tools` enforcement.** The union over activated skills
  narrows the run at the dispatch chokepoints: kernel tool bodies
  refuse blocked tools with a visible tool error. The kernel's
  `analyze_document` is allowed wherever `read_project_document` is
  (`SKILL_TOOL_IMPLIED_BY`); the closed list does not name it.
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
result, its prompt and its model-routing node; the built-in profile is
`review`.

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
  the run that commissioned it, through the same inheritance rule research
  and knowledge children use (`inherited_stufe`), so the thoroughness a user
  selected does not end at the handover.
  The Deep effort elevation stays local to the kernel's own reasoning loop —
  a role resolves under its own routing node, whose model and effort follow
  the user's selection.
* **Not a conversation source.** A role run holds nothing but re-read material:
  it is left out of the session's source catalogue (`search_context` /
  `read_context`) and of the session history. Its trace nests under the parent
  run; the desk shows it as one delegation row ("Prüfrolle (Unterauftrag)")
  joined to the child through the role's origin key.

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
  `INQTRIX_AGENT_KERNEL_MAX_ITERATIONS_DEEP` (120 = 14 sequential tool
  turns plus the answer; one tool turn and the answer turn each cost 8
  measured super-steps) — a raised, still operator-bounded decision point.
  Independently,
  all model-emitted tool calls count against a checkpoint-derived
  cumulative limit (30 normal, 60 deep); an overflowing multi-call batch
  is rejected before any tool executes. Explicit extension may raise the
  effective deep limits only to 240 steps and 120 tool calls by default.
* **Child profiles.** A research child runs a profile the server fixes,
  with no model-controlled profile argument: without a Stufe, `compact` for
  a normal run and `deep` for a Deep run; with a Stufe, the Stufe's child
  profile (`gruendlich` `schnell`, `tief` `compact`).
* **Verification pass.** Before finalizing, a run that owes a review (the
  `verify` level of its Stufe; without a Stufe the legacy rule `depth=deep`)
  has its outputs reviewed once, on the selected model and effort
  (`agent_deep_review` in the tier map, high tier by default). The Stufe
  decides WHETHER a run is reviewed; the size of the material decides HOW.
  Material whose estimated input stays within 32,000 tokens
  (`REVIEW_IN_PROMPT_BUDGET_TOKENS`) is reviewed in one structured call that
  carries the outputs and the run's whole stored evidence. Anything larger
  goes to the **review role** — a bounded child run of the same kernel under
  the work profile `review` (see *Work profiles* above). The role receives
  the complete effective assignment and the chat answer in its typed
  handover; every canvas created or updated by the run, the run's evidence
  ledger and the documents the run read travel as ADDRESSES, and the role
  reads them through the kernel's own windowed readers, so a review does not
  have to fit one context window. A host without the child-run seam has no
  role and reviews in one call as long as the model holds it; material that
  fits neither is a visible `deep_review_unavailable`, never a quiet pass.
  Output requirements never justify research. The review starts no
  research: it re-reads, it never acquires.
  While the role works, the parent parks slot-free (`waiting_for_children`)
  from its post-loop and is woken by the child's terminal write; the finished
  thread is re-entered without a model turn and finds the same child through
  its origin key (`role-review`).
  Source material, provider synthesis and internal measurements remain distinct;
  a source link or a single-source ratio is not proof of semantic correctness.
* **What the review reads and checks.** The handover carries the run's
  diagnostics as server facts, each with its `basis_id`: one reading record
  per source version the run read (read ranges, unread remainder, read
  completely), one per document analysis (built by ONE function,
  `kernel/evidence_read.document_analysis_diagnostic`: the objective and
  fields; per document what was read (`text_read_to_end`,
  `document_complete`, windows, unanalysed ranges, pages without a text
  layer, extraction status, verified passages, passages not taken over by
  status); the verified sources, both predicates and every blocker; no
  model text), the evidence contract of each research run the reviewed
  references come from, and the stored plan of a mission run they come from
  (see [Older mission runs](#older-mission-runs)). The trusted rubric says how a
  record counts: as read for exactly what it records and within the scope it
  states, proving no content beyond the sources it lists, a named limit
  staying open, and several records about the same material being no
  independent confirmations. The handover also carries the run's
  clarification record, read by server code from the stored clarification
  rounds, never from a transcript: oldest first, without the server's own
  limit-gate rows, a skill-point round counting like any other. Per question
  it holds the round, the question, the wording of the options the user
  chose, the user's free text and whether the question was answered; an
  unanswered question carries the assumption the run continued with where
  its round recorded one, an assumption and never an answer. The review
  reads the whole record as requirements to check against, in a fence of
  its own (`run-clarifications`) after the assignment's, since a fence
  inside the assignment's would be neutralized. The rubric checks
  procedural steps the assignment orders (ask first, read completely)
  against the server facts the handover carries, this record and the run
  diagnostics, never as text the outputs must contain.
  The rubric asks the fidelity questions of every cited statement: its force
  (a duty, an expectation, a permission, an example or indicator — an
  example is never a duty), its reservations, the intro of the list it
  stands in (an excerpt that begins inside a list leaves its force unknown,
  named as not checked), its links and quantifiers, its actors and reach,
  and absence. A statement of what the material does not cover is a factual
  claim about the documents: contradicted by material the review read, it
  is an `evidence_error` with that material as its basis; a reading limit
  alone makes no finding of it, and a claim the review cannot check is
  named as not checked. One list of these categories serves the review and
  the revision: the review renders them as checks with its own duties, the
  revision as the rules every changed sentence keeps.
  The verdict accounts for every success criterion by its number (a kernel
  run's review settles one criterion per attached library rule); a
  criterion it does not settle exactly once is open, never met. A finding
  quotes a verbatim, unique passage and names its basis
  (`source:<reference_id>`, `diagnostic:<run>`); a source basis counts only
  when the run holds it in its own ledger and the review had read it, and
  one unconfirmed basis opens that finding alone: it is shown as an open
  point and never applied. The outcome record carries how many cited
  references were re-read (`reach`) together with every point the review
  could not confirm (`lines`). A verdict the server cannot assess, such as
  one that negates a flag without naming a reason, makes the review
  `unavailable` in the outcome record, and the outputs stand as written.
* **Revision.** `deep_review_findings` exposes the located findings before
  the one revision call, which receives the evidence of exactly those bases
  and returns only a finding id and replacement text per edit. The revision
  replaces exactly the quoted passages, each with the citation run right
  after it (`Satz. [K12]`); everything else stays byte-identical, so a
  revised text is not reviewed a second time, and an edit that changes how
  the untouched text cites (an opened fence, a lone backtick) is named. All
  anchors resolve against the reviewed snapshot; overlaps, missing or
  ambiguous anchors, foreign grounds and foreign labels name their finding,
  and the others stay applicable. The citation rule depends on the
  finding's kind: an edit cites only its passage's labels and its basis's
  labels (in the target's label space, by reference identity), keeps each
  kept label on the sentence it stood on, and every new sentence of an
  `evidence_error`, `missing_information` or `output_requirement` fix
  carries a label of its basis; a `run_diagnostic` fix adds no label, and an
  output requirement without a basis changes only headings, order and
  deletions. A finding names in `remove_labels` the labels its fix removes
  and in `criterion_index` the success criterion it concerns; the server
  validates both. Valid replacements preserve all other text bytes and
  commit through the existing multi-canvas batch CAS; a canvas without an
  applied edit is not rewritten. An empty replacement of whole lines also
  removes one line end (the one after them, or at the end of the text the
  one before), so a deleted table row or list item leaves no blank line that
  would end its block; the re-check places every later replacement by the
  same spans. An unmet output requirement reads `revised` when every
  counted model finding that concerns it was applied, `verified` when the
  re-check also held every one of those edits, and open otherwise; neither
  state says the criterion is met.
* **Re-check of applied edits.** Each applied edit of a model finding is
  re-checked, and the server, not the model, decides it. The server lists
  where the edit changed its passage: both sides are read in headings,
  sentences and code blocks, each sentence split once more after a `;` or
  `:`, and compared by their form (heading, sentence, list item, table row or
  code), their words, the marks that say as much as a word (an order or
  approximation sign such as `<` or `≈` wherever it stands, `=` or `~`
  before a digit, a sign before a digit or before a currency sign or a
  second sign that a digit follows, a separator between digits, a percent,
  permille, currency or section sign) and their labels, so punctuation and
  emphasis make no segment while a changed word, label, such mark or form
  does (a sentence that becomes a list item is a segment); each run of
  differing units is one segment (`changed`, `added` or `removed`). It also
  says where the replacement stands in the delivered text: the heading above
  it, the intro of the list it stands in or holds, and the text on either
  side (a long line ends with `… [shortened]`). One structured call on the
  review node receives, fenced as data and each block after a line that
  says what it is, each finding's prose, the replaced text, the
  replacement, its segments, its placement and the texts of the labels the
  replacement cites (for a `run_diagnostic` finding also the reading record
  it rests on). It fills one row per segment: verbatim quotes from those
  texts first, then force, limits with their direction, actors and reach,
  reservations and links of the new text against the quoted basis, never
  against the replaced text; the kind the whole replacement in its place
  gives the content beside the kind the basis gives it; and what became of
  the replaced text (restated, asked for in the finding's own words, or not
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
  `too_large`, `check_unavailable`, `inconclusive` or `knowledge_only`), and the run's
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
  without a code. The model's reason stays in the trace.
* **Outcome.** Every final narration after a verdict is the outcome
  record's `summary`, built from the same review facts the record is sent
  from: an applied edit reads "überarbeitet, nicht nachgeprüft", and
  "behoben und nachgeprüft" only when the re-check held it; unchecked points
  and open criteria are named even when the verdict has no finding. When the
  revision leaves a counted finding unapplied, a `critic_revision_discarded`
  activity lists each named finding with its reason, and its text stands.
  The run then sends its outcome record (`inqtrix.agent.outcome`), built from
  its review: once per segment that leaves the kernel with a decision (the
  normal end directly before the checkpoint is deleted; a cancel at the
  model boundary or during the review; a graph error; any error after the
  loop; a limit's partial answer or cancel), never for a park and never for
  a role run, with every line through the run's reader guard. A Stufe that
  skips the review records it as `waived`; a kernel request without a Stufe
  that owes no review by its depth records `not_run`. The desk takes its
  words about the review from this record alone, never from a phase or an
  activity (see [React UI](../deployment/react-ui.md)). Payloads and
  references stay unchanged, so new sources must enter a canvas through its
  normal writing tool before review. A review that failed, delivered no
  typed result, delivered an invalid one or exhausted its budget preserves
  the existing outputs with a terminal `kernel_deep_review` narration that
  names the reason. Unusable edits, a revision that does not fit its context
  and CAS conflicts preserve them too: the summary counts the findings they
  named, and `critic_revision_discarded` gives each one's reason.
* **Prompt bias.** The deep user-message section biases research toward
  thorough research over instant search, and demands explicit assumptions.

## Dependencies

Optional extra `agent`: `langchain` and `langchain-core` (the kernel's
agent loop, assembled in `agents/harness.py`), `langgraph-checkpoint-postgres`,
`psycopg`. The agent checkpointer loads only LangGraph's safe types plus the
list `CHECKPOINT_TYPES` in `agents/checkpointing.py` (empty: the kernel keeps
plain data and LangChain messages), passed to the serializer directly, so it
holds whether or not `LANGGRAPH_STRICT_MSGPACK` is set; a value of another type
loads as raw data, is logged and counted, and stops a kernel run before its next
segment. The list is part of the workflow fingerprint. Install with
`uv sync --extra agent --extra knowledge-qdrant --extra queue-valkey`, or use
standard Python/pip with
`python -m pip install -e ".[agent,knowledge-qdrant,queue-valkey]"`.

## Related docs

- [Runs API](../extending/runs-api.md) — waiting statuses,
  agent event catalog, control endpoints
- [Knowledge engine](knowledge-engine.md) — the profiles knowledge
  children select
- [LLM calls](websearch/nodes.md) — per-node tiering
