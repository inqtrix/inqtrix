# Runs API

> Files: `src/inqtrix/server/routers/runs.py`,
> `src/inqtrix/server/routers/agent_runs.py`, `src/inqtrix/runs/shared.py`,
> `src/inqtrix/runs/records.py`, `src/inqtrix/runs/memory_store.py`, `src/inqtrix/state.py`, `src/inqtrix/graph.py`,
> `src/inqtrix/agents/`

## Scope

Structured live events for native browser UIs that need progress cards, queue state, cancellation, and a final report fetch. This page covers the `/v1/runs` API. The OpenAI-compatible `/v1/chat/completions` endpoint uses OpenAI-style chunks and may additionally include Inqtrix diagnostics such as `inqtrix.model_resolution`; see [Web server mode](../deployment/webserver-mode.md).

## Endpoint model

### Conversation titles

`POST /v1/session-titles` generates one optional short label alongside the
answer. The body is `{question, language?, stack?, target?}`. `language` is the
UI-language hint; confident question-language detection takes precedence.
For a saved conversation, `target` is `{kind: "chat" | "agent", id, revision}`.
The target must belong to the caller and match a supplied UI namespace.
Omitting the target generates a label without saving a conversation.

The response contains `title`, `reason`, and, when a call completed, `model` and
actual `usage`. A saved target also returns `current` with `title`,
`title_revision`, and `title_source`. `title: null` names a skip, including
`disabled`, `no_fast_model`, `provider_unavailable`, `rate_limited`, `timeout`,
`invalid_title`, or `title_changed`. Quota denial uses the standard quota
envelope. The client keeps its provisional title and does not delay the answer.

The call requires `TIER_FAST_MODEL`, has an eight-second deadline, and accepts
at most six words and 60 characters. Invalid output is discarded, not repaired
or retried. Optional work has a separate two-thread executor and process-local
admission: six requests per user per minute, two concurrent calls. Shared quota
and deployment edge limits still apply. Reported usage is booked when a provider
thread finishes even after its HTTP waiter timed out; late labels are discarded.

Chat-thread and Agent-session metadata carry `title_revision` and `title_source`
(`initial`, `generated`, `manual`). New conversations start `initial`; existing
and imported titles default to `manual`. The revision increments in the same
row update as the title, independently of `updated_at`. Generation changes only
an initial title at the expected revision and preserves the activity/sort time.
A manual rename wins even when the model committed while the user was editing.
Choosing the identical provisional text still changes ownership to `manual`.

Metadata PUTs send the observed revision and intended source. Stale title echoes
cannot overwrite the stored title; other metadata retains its existing write
contract. `title_preserved` reports that outcome, and clients reconcile the
returned title independently of activity timestamps. A competing manual edit is
reported visibly, with the attempted name retained for an explicit retry.
Unresolved manual saves are recoverable in the same tab after reload; recovery
never resubmits over a newer title. Unversioned clients can change an untouched
legacy title once; later conflicting writes receive HTTP409
`session_title_conflict` and require a client reload.

Only an explicit first message creates a title intent, including a conversation
opened empty beforehand. Desk changes do not cancel it. Renaming, deletion and
account/project changes invalidate stale replies. Old conversations are not
mass-retitled. Incognito chat retains its temporary label without a title call.

### Native runs

Native runs are short-lived server resources addressed by an opaque `run_id`:

| Route | Method | Purpose |
|---|---|---|
| `/v1/runs` | POST | Create a queued research run. Accepts either `question` or OpenAI-style `messages`. Agent runs additionally accept `response_form` (`"auto"`\|`"chat"`\|`"canvas"`) and `source_policy`; see below. A `mode: knowledge` ask accepts `"incognito": true` (only `true` or `false`, and never together with `session_id`; 400 otherwise): its spans carry no content whatever `INQTRIX_TRACE_CONTENT` says, and the run is deleted `RUN_INCOGNITO_RETENTION_SECONDS` after it ended. A `mode: knowledge` ask also accepts `"ui_language"` (`"de"` or `"en"`; 400 for any other value and for any other mode): the interface language, which the answer and every server text of the run use only where the question's own language is unclear; a question with a clear language is answered in it. Returns HTTP 202 with the run summary. `knowledge_filters` carries either explicit `collection_ids` (chips) or `scope: "all"` (the session knowledge switch: every collection the signed-in caller may see, resolved and pinned at submission; an unscoped principal keeps its tenant-wide view; 400 for any other `scope` value, and 400 for a `collection_ids` list with a non-string or empty entry — chips win over the switch, and the persisted filters then carry no `scope`); the run's execution snapshot reports the choice as `knowledge_scope` (`all`, `pinned`, `inherited`) plus `knowledge_collection_count`. A request that pins knowledge collections, uses `scope: "all"` or is a `mode: knowledge` ask is refused with HTTP 503 `knowledge_store_unavailable` (`reason`: `operator_disabled` or `store_unreachable`) while the knowledge store is not configured or does not answer the availability probe. |
| `/v1/runs` | GET | List visible queued, running, waiting, and retained terminal runs from the configured run store. |
| `/v1/runs/import` | POST | Persist a COMPLETED report carried in from a loaded project file — a terminal-run snapshot with no server-side execution, so it costs no run quota. `source_run_id` is required, but is provenance and an owner-scoped idempotency key only; the server always allocates the public `run_id`. `mode` names the kind of report and is `research` when the file leaves it out. Returns 200 with the run summary. |
| `/v1/runs/{run_id}` | GET | Fetch the current run summary. |
| `/v1/runs/{run_id}/events` | GET | Stream buffered and live events as Server-Sent Events. The server sends periodic SSE comment heartbeats (`: keepalive`) without allocating event sequences. The optional `?after=<sequence>` query parameter filters the REPLAY to events with a higher sequence (reconnect support); the live tail is unaffected. A non-integer value returns 400. With `?format=json` the SAME replay buffer returns immediately as a JSON page `{"object": "list", "data": [...], "terminal": bool}` instead of a stream — the polling fallback for clients behind SSE-buffering proxies. |
| `/v1/runs/{run_id}/children` | GET | List an agent run's direct child runs, newest first, as `{"object": "list", "data": [...]}`. Access is decided on the parent (a view share suffices); the children's own URLs stay owner-scoped. Standard runs return an empty list. |
| `/v1/runs/{run_id}/approvals` | GET | The run's approval requests of every stored kind, newest first. A current run's approvals are `kind: "tool"`; an older mission run's carry the kinds `plan`, `replan`, `discovery` and `patch`, and a decided plan gate's `decision_payload` names an attached result requirement by its parts and typed text only, never by the composed rule bodies; see [Result requirement](#result-requirement). |
| `/v1/runs/{run_id}/approvals/{approval_id}` | POST | Decide one tool approval: `{"decision": "approve"\|"reject"\|"edit", "note"?, "actions"?, "approval_scope"?}`. `edit` carries `actions`: exactly one action whose `args` replace the proposed ones, validated against the gated tool's input model; the tool itself cannot be swapped, and `actions` is allowed only with `edit`. `approval_scope` (`once` by default, or `run`) is allowed only with `approve`; `run` grants the gate's tools for the rest of the run ([Agent platform](../architecture/agent-platform.md#report-quality-pipeline)). A body that carries `plan`, `edited_tasks`, `report_guidance` or `report_rule_ids`, also as `null`, is refused with 400 `invalid_request_error` and the message `<field> is not part of an approval decision; send decision, note, actions or approval_scope.`. An unknown approval answers 404. A stored approval of another kind than `tool` (an older mission run's) answers 400 `{"error": {"message", "type": "invalid_request_error", "errors", "reason": "workflow_retired"}}`, also for a replay of the decision it stored, and stays as it is. The decision and the run's `waiting -> queued` resume commit atomically; replaying the SAME decision answers 200, a different one 409 `conflict`. Owner or edit share. |
| `/v1/runs/{run_id}/clarifications` | GET | The run's clarification questions, newest first. |
| `/v1/runs/{run_id}/clarifications/{clarification_id}` | POST | Answer one clarification with exactly one of `{"answer": ...}` (whole-round free text), `{"option_id": ...}` (legacy single option), or `{"answers": {question_id: {"option_ids": [...], "text": "..."}}}` for structured rounds — the map must resolve EVERY question of the round (partial maps 400). Resumes the run like a decision. Owner or edit share. |
| `/v1/runs/{run_id}/artifacts` | GET | Artifact metadata page (no bodies), `?kind&limit&cursor` keyset pagination. |
| `/v1/runs/{run_id}/artifacts/{artifact_id}` | GET | One artifact with body, refs, and revision history (`revisions[]`: `revision`, `created_by`, `created_at`, `status`); `?revision=` serves an older body. A revision's `status` is the artifact status its write carried: `writing` marks a write the agent has not finished (the empty answer artifact committed before its publication streams, and in an older mission run each streaming flush of its memo), which the canvas keeps for diffs but does not offer as a version to pick; `ready` marks a finished write or a user edit. |
| `/v1/runs/{run_id}/artifacts/{artifact_id}` | PUT | Optimistic user edit: `{"content_markdown", "expected_revision"}`. 409 `conflict` with `current_revision` on a revision miss; 409 with `locked_by: "agent"` while the agent streams (`status: "writing"`). Owner or edit share. |
| `/v1/runs/{run_id}/artifacts/{artifact_id}` | PATCH | Metadata-only rename: `{"title"}` (non-empty, at most 200 characters). The `revision` does NOT advance and the content history never records a rename. Returns `{"id", "revision", "title", "updated_by"}`; 409 `conflict` with `locked_by: "agent"` while the agent streams. Owner or edit share. |
| `/v1/runs/{run_id}/artifacts/{artifact_id}/export` | POST | Copy the artifact into a NEW editor document (`{"target": "editor_document", "title"?, "folder_id"?}`), 201 with the document. Not idempotent — every call creates a fresh copy. 502 when editor persistence is not wired. |
| `/v1/agent-sessions*`, `/v1/agent-session-groups*` | GET/PUT/DELETE | Saved agent-desk sessions — a structural clone of the `/v1/knowledge-sessions` surface (private per user, groups); unlike knowledge sessions, the list rows carry the small `items_json` settings body. |
| `/v1/agent/memory*` | GET/PATCH/POST/DELETE | Personal long-term agent memory: accepted memories, candidates, search (`GET /v1/agent/memory?q=`), clear, and feedback history. Auth derives ownership; client owner fields are rejected. |
| `/v1/agent/runs/{run_id}/feedback` | POST | Store personal run feedback (`positive`/`negative`/`neutral`, optional `reason`, optional owner-checked `memory_id`). Shared run access does not grant personal memory access. |
| `/v1/runs/{run_id}/result` | GET | Fetch the final `ResearchResult.to_export_payload()` report after completion. Agent Desk results include the same canonical `execution` block as the live snapshot; Knowledge results include the text-free `knowledge_profile`, `knowledge_gate`, `knowledge_grounding`, `knowledge_retrieval`, `knowledge_candidates`, and `knowledge_evidence_used` receipt reconstructed from the result and terminal snapshot. |
| `/v1/runs/{run_id}/cancel` | POST | Cancel a queued run or request cancellation for a running run. Cancelling an agent run cascades over its child runs. |
| `/v1/runs/{run_id}` | DELETE | Permanently delete a terminal run (owner-only); returns 409 while the run is still active. Revokes any shares on the run. To delete an active run, cancel it first, poll the summary until it turns terminal (its summary carries `cancel_requested: true` in the meantime), then delete — the reference web client automates exactly this flow. |

The opaque `run_id` identifies the job, while the server binds that job to its
tenant, workspace, owner, and optional shares. Clients address work by
`run_id`; possession of an id never grants access.

With `messages`, the last message is the run's question and the messages
before it are its history. The earlier `user` turns stay instructions in
force. Every other turn (`assistant`, `system`, `developer`, a tool result)
reaches the model as data in a `client-history` fence: a system message
inside a client-sent history describes the conversation, it does not
instruct the run. A history that exceeds its budget is cut visibly, and
every run of left-out messages stands as one line in its place.

### Agent source and execution request

The additive Agent Desk contract is:

```json
{
  "question": "Who won today's final?",
  "mode": "agent_kernel",
  "source_policy": {
    "web": "available",
    "knowledge": "disabled"
  },
  "response_form": "chat",
  "agent_overrides": {
    "depth": "normal",
    "model": "operator-published-model-id",
    "effort": "high"
  }
}
```

`source_policy.web` and `.knowledge` each accept `available` or `disabled`;
missing fields default to `available`, preserving old clients.
`source_policy` without an agent mode is rejected. Omission keeps both
sources available. The Agent Desk sends `web` as the
session's switch and `knowledge` as `available` only when the message
attached a collection. Inheritance to child runs and the full consent
model: [Agent platform](../architecture/agent-platform.md#source-policy).

### Result requirement

An agent run can carry a result requirement: how the deliverable has to
look. It is set on `POST /v1/runs` before the run starts.

| Field | Type | Meaning |
|---|---|---|
| `report_guidance` | string, at most `report_requirement.max_chars` characters | Text the user typed. |
| `report_rule_ids` | list of prompt-template ids | Library rules to attach, in attachment order. |

Only ids travel. The server resolves label, revision and body from the
caller's OWN visible templates; one id the caller cannot see refuses the
whole request with 400 (`Unknown or non-visible rule: …`). Ids are
trimmed and deduplicated in attachment order before they are counted or
resolved — the same rule listed twice is one rule — and the rule cap
(`report_requirement.max_rules`) counts the DISTINCT rules, so four ids
naming three rules are accepted and four distinct rules are refused with
400. A non-list or an empty id is a 400 of its own. Both fields apply
only to agent modes.

The server composes one requirement block: every rule as
`[Rule: <label>] … [End rule: <label>]` (a rule another user shared
with the caller opens with `[Rule: <label> (shared by another user)]`,
naming its source as a kind and never the person), the typed text last as
`[Free requirement] …`, blocks separated by one blank line. Labels are
folded to one line with their brackets — ASCII and fullwidth — turned
into parentheses. Inside a body or the typed text, a frame marker has
its opening bracket turned into `(` in every letter case, with an ASCII
or a fullwidth bracket, with whitespace, soft hyphens or invisible
format characters (zero-width space, joiners, word joiner, byte order
mark) in its gaps, and with straight, typographic (single or double),
low or single reversed quotes or a backtick around a skill label, so no
contributed text can open or close a frame in these spellings. Letters
from other scripts that look like Latin ones, a fullwidth colon, the
double reversed quote and other invisible format characters (for
example bidirectional controls) are not recognised. A
composition longer than
`report_requirement.max_composed_chars` is refused with 400 and its
length named; it is never shortened.

The composed text is frozen with the run: a template edited afterwards
changes nothing a run is already working under, and resume never
re-resolves. On the submit path the run's durable request also keeps the
parts it was composed from, `report_rules`:

```json
[{"template_id": "pt_…", "label": "briefing", "title": "Executive Briefing", "revision": 3}]
```

`label` is the rule's composer handle; `title` is the display title the rule had when it was attached,
folded to one line, and present only when the template has one. A part stored before titles were kept
carries none: readers fall back to `label`, nothing invents a title. A rule another user shared with the
caller also carries `"shared": true`, the marker its block opens with; an own rule carries no such key.

`report_rules` is server-set only, and so is the composed text the run
keeps as `report_requirement`. A `POST /v1/runs` body that carries
either is refused with 400 (`report_rules can only be set by the
server.`, `report_requirement can only be set by the server.`), exactly
like the internal role fields. The kernel binds its transcripts
to these revisions: a later run of the same conversation can read an
earlier run's transcript only while every rule it was bound to is still
visible to the reader at the same revision; an edited, deleted or no
longer shared rule closes the transcript (`unavailable`). The run's own
archives stay readable after a mid-run template edit, because the run is
frozen under its parts. A transcript whose descriptor names no rule
revisions — written by a server that did not record them, or by a run
whose durable request carries the composed text without `report_rules`
— is not rule-bound: its rule text stays readable to the session owner
after the rule is edited, deleted or unshared. Internal role runs (the
review) carry the requirement in their handover; they are no
conversation source, so a source id naming one is refused as
`unavailable`, exactly as the source catalogue leaves them out.

A stored plan-gate decision of an older mission run holds the composed
text the mission resumed under. The approvals API never returns it: such a
decision's `decision_payload` carries the parts and the typed text instead,

```json
{"report_requirement": {"free_text": "Audience: board.",
  "rules": [{"template_id": "pt_…", "label": "briefing", "title": "Executive Briefing", "revision": 3}]}}
```

so a rule deleted or unshared later is not readable through old
approvals (`rules` is empty when only text was typed). The composed
`report_guidance` string is never returned, also not by a stored decision
without `report_requirement`. A rule a plan gate kept carries the title it
was frozen with. The kernel reads earlier decisions of its conversation
through the same projection.

How the requirement ranks is stated to the kernel in fixed words: evidence
labels stay (a rule's own citation style comes next to a label, never
instead of it); the assignment, the typed free requirement and the user's
answers to clarifying questions in the run narrow a rule where they say so
explicitly; where two attached rules conflict, the one attached first wins
and the chat answer names the conflict; the requirement wins over a skill,
also one the kernel loads later; and a rule applies to every deliverable
document unless the assignment scopes it, and to the chat answer only when
that answer is the deliverable.
The run event `inqtrix.agent.requirement.attached` records the
requirement a run starts under, and the `inqtrix.run` span carries the
attached rule ids, their revisions and the composed size
([tracing legend](../observability/tracing-legend.md#result-requirement)).

**Child runs.** No child inherits the requirement: research and knowledge
children and role runs deliver to the run that holds it.

The capability manifest publishes the limits under
`agent.report_requirement`: `max_chars`, `max_rules` and
`max_composed_chars`. Every template on `/v1/prompt-templates` carries
`composed_chars`, the length of the block that template composes into
(label folded, body trimmed, markers defused — measured by the server's
own composition, in code points). A client pre-checks a selection by
adding the `composed_chars` of the attached rules, two characters for
each separator between blocks and, for typed text, the
`[Free requirement]` marker line plus the trimmed text; it never
re-implements the composition.

## Queue and lifecycle

`RUN_MAX_CONCURRENT` (default 100) caps active native provider work. Additional
`/v1/runs` jobs enter a bounded FIFO queue controlled by `RUN_QUEUE_MAX_SIZE`
(default 100). With the in-memory store, terminal runs remain fetchable for
`RUN_COMPLETED_TTL_SECONDS` (default 300). With the PostgreSQL run store,
terminal rows, their event history, and result payloads survive process
restarts and are retained for `RUN_DURABLE_RETENTION_SECONDS` (default 7776000,
90 days). An incognito knowledge ask leaves either store
`RUN_INCOGNITO_RETENTION_SECONDS` (default 900) after it ended, at the first
cleanup after that window: the API process runs one every minute, and every
store access in the API and the worker runs one as well.

```mermaid
stateDiagram
    [*] --> queued
    queued --> running: capacity available
    queued --> cancelled: cancel before start
    running --> completed: result stored
    running --> failed: worker exception
    running --> cancelled: cancel observed at node boundary
    running --> waiting: agent interrupt (approval/input)
    waiting --> queued: decision recorded, re-dispatch
    waiting --> cancelled: cancel, or waiting TTL expired
    completed --> [*]: TTL cleanup
    failed --> [*]: TTL cleanup
    cancelled --> [*]: TTL cleanup
```

The important transition is `queued -> running`: it is driven by the run store (in-memory by default; Postgres-backed with worker dispatch when the durable backends are configured), not by the client polling loop. Active jobs do not count against `RUN_QUEUE_MAX_SIZE`.

Agent runs add three non-terminal waiting statuses. `waiting_for_approval` and
`waiting_for_input` park the run (no execution slot held) until a HUMAN decision
resumes it back through `queued`. `waiting_for_children` parks it while its
child runs (research, knowledge or the review role) execute: the store itself re-queues the parent when the
LAST child reaches a terminal state (the wake happens inside the child's
terminal transaction, so it cannot be lost), and a park that lands after the
last child already finished self-heals back to `queued` immediately. Because
the parent holds no slot while waiting, a pool smaller than one batch of
children serialises the batch instead of deadlocking it. For every waiting status a
cancel ends the run (reason `cancelled_while_waiting`), or the waiting TTL
expires (default seven days) and the store auto-cancels it with the visible
reason `approval_timeout` (human waits) or `children_timeout` (children wait).
Waiting runs are excluded from orphan/stuck sweeps.

A run that ends other than by completing takes its live children along, in
the same transaction: a failed run (also one a restart or a lost worker
failed, or one whose execution authority is gone) cancels them with the
reason `parent_failed`, a cancelled run (also one whose wait timed out, or one
that stopped on its own) with `parent_cancelled`. Queued and waiting children
end at once, a running child is asked to stop. Cancelling a run asks its whole
tree to stop; a child that was already asked keeps its own request. A running
run that was asked to stop ends with the reason of the latest request
(`client_requested_cancel`, `cancelled_by_admin`, `parent_failed`,
`parent_cancelled`, `session_deleting`), also when it was about to park.

Every run is bound to its mode and to the workflow contract that mode
carried when the run was accepted. A worker runs it only under that
contract: a run that has not started yet takes the worker's contract, while
a run that already ran under an older contract (or whose mode was taken
out) ends as `cancelled` with the reason `workflow_contract_changed` (or
`workflow_retired`) and loses its stored agent state. A parked run of that
kind does not wait for its next claim: the worker ends it when it starts
and then once a minute, so nobody answers an approval for a run that can no
longer continue. Without a queue worker (`INQTRIX_QUEUE_BACKEND=memory`)
the API process ends such runs once, when it starts: a parked run becomes
outdated only through an update, and an update restarts the process (with
in-memory storage no parked run outlives a restart). A run the sweep ends
cancels its active children with the reason `parent_cancelled`, and a child
the sweep ends wakes its waiting parent as any ended child does. In that
mode no parked run of an earlier process can resume at all, because its work
was retained in that process: a run waiting for an approval or an answer
under the current contract stays parked after a restart until its wait
expires, and an answer to it is refused; a run waiting for its children is
woken when the restart fails them, finds no work to resume and ends as a
queued run that no process executes (`queued_timeout`, after two hours by
default). A run bound to a newer contract, or
of a mode the worker has not registered, waits for a worker that carries it;
a worker that cannot run it stops claiming runs until the run is cancelled,
ends or parks, and then claims again by itself.

Agent runs also carry additive summary keys — `kind` (`agent` or `agent_child`), `children_url`,
`parent_run_id`, `root_run_id`, `session_id`, `last_event_sequence` — emitted
only when non-default, so standard run summaries keep the exact historical key
set.

`last_event_sequence` is the run's event position at the moment the summary was
read, and it exists for clients that consume the summary **and** the event
stream at once. A summary is a snapshot of the run row while the stream keeps
moving, so the two can disagree, and only a position says which of them is
ahead. The decision endpoints make this the normal case rather than a race:
`POST /v1/runs/{id}/approvals/{id}` and `POST /v1/runs/{id}/clarifications/{id}`
answer with a summary read inside the transaction that re-queues the run, so it
reports `queued` while the worker picks the run up milliseconds later. A client
that writes that status over what it has already streamed shows a queue
position for a run that is working — for as long as it takes the next lifecycle
event to arrive, which can be minutes. Compare `last_event_sequence` against the
highest event `sequence` consumed and keep the streamed `status`, `phase`,
`queue_position` and `snapshot` when the summary is not ahead.

## Run summary

Every list/detail response returns the same summary shape:

```json
{
  "run_id": "run_abc123",
  "status": "running",
  "queue_position": null,
  "question": "What changed?",
  "stack": "default",
  "workspace_id": null,
  "mode": "research",
  "agent_overrides": {},
  "created_at": 1760000000.0,
  "started_at": 1760000001.0,
  "finished_at": null,
  "elapsed_seconds": 12.34,
  "snapshot": {
    "current_node": "search",
    "completed_rounds": 1,
    "active_round": 2,
    "max_rounds": 4,
    "total_queries": 6,
    "executed_queries": 5,
    "total_citations": 21,
    "total_sources": 18,
    "confidence": 7,
    "source_tier_counts": {
      "primary": 2,
      "mainstream": 11,
      "stakeholder": 3,
      "unknown": 2,
      "low": 0
    },
    "source_quality_score": 0.72,
    "claim_status_counts": {
      "verified": 1,
      "attributed": 3,
      "contested": 1,
      "unverified": 8
    },
    "claim_quality_score": 0.5,
    "evidence_record_count": 18,
    "consolidated_claim_count": 13,
    "aspect_coverage": 0.75,
    "evidence_consistency": 7,
    "evidence_sufficiency": 6,
    "done": false,
    "progress_estimate": 0.42,
    "last_message": "Durchsuche 3 Suchanfragen (Runde 2/4)..."
  },
  "error": null,
  "events_url": "/v1/runs/run_abc123/events",
  "result_url": "/v1/runs/run_abc123/result"
}
```

These 16 keys are always present. `workspace_id` is `null` for a run created outside a workspace, and `agent_overrides` is an empty object when the request set none — the keys themselves never disappear.

`mode` is the id of the algorithm that executed the run: `research` for the normal graph path, `direct_llm` for the explicit direct-provider chat mode, and — when their gates pass — `knowledge` or `agent_kernel`; earlier runs of the retired mission engine keep the mode `workspace_agent`. The registry accepts custom ids too, so clients must tolerate a `mode` they do not know. `snapshot` is a compact derived view from `AgentState`. It deliberately omits raw provider responses, full evidence ledgers, and secrets. The source and claim quality fields are live counters for UI cards and diagnostics; the final authoritative report payload still comes from `/result`.

Agent summaries additionally expose a `timing` object with
`total_seconds`, `active_seconds`, `waiting_seconds`, `queued_seconds`,
`segment_count`, `resume_count`, and `current_segment_id`. Standard run
summaries retain their historical exact key set.

Agent summaries and state-bearing agent events add one canonical
`snapshot.execution` block with an exact, algorithm-independent key set:

```json
{
  "execution": {
    "effective_mode": "agent_kernel",
    "response_form": "chat",
    "depth": "normal",
    "model": "operator-published-model-id",
    "reasoning_effort": "high",
    "source_policy": {
      "web": "available",
      "knowledge": "disabled"
    },
    "knowledge_scope": "inherited",
    "knowledge_collection_count": 0,
    "consent_reason": "explicit_directive",
    "tool_use_counts": {
      "web": 1,
      "knowledge": 0
    },
    "web_searches": 1,
    "knowledge_searches": 0,
    "limits": {
      "tool_calls": {
        "used": 4,
        "limit": 30,
        "ceiling": 60,
        "recoverable": true,
        "extendable": true
      }
    },
    "tool_grants": [],
    "document_analyses": []
  }
}
```

All keys are present for current agent runs. `limits` maps a stable limit id (`tool_calls`, `steps`, `tokens`; a stored mission run's snapshot also carries `discovery_tool_calls`, `plan_tasks`, `replan_rounds`, `clarification_rounds`) to the server-authored boundary facts `used`, `limit`, `ceiling`, `recoverable`, `extendable`, and an optional `reason`; the model never writes this block, and an engine without boundaries emits an empty object rather than dropping the key. `tool_grants` lists the tools the user approved run-wide (`approval_scope=run`), sorted, and stays an explicit empty list when nothing is granted. `document_analyses` lists, per document an `analyze_document` call of the run read without failure (a document that failed after it was opened, for example because it changed, is not listed), what the server read for it: `document_id`, `version`, `total_chars`, `complete`, `analysed_ranges`, `unanalysed` (`start`, `end`, `reason`), `windows_planned` and `windows_analysed`; it is an explicit empty list for a run that analysed nothing, and payloads written before the field read as empty. `source_coverage` stays the record of what the model itself read. `knowledge_scope` names whose knowledge boundary the run searched — `all` (the session knowledge switch: every collection the caller may see), `pinned` (attached collection chips) or `inherited` (no explicit choice) — and `knowledge_collection_count` the number of collections inside it; payloads written before these fields read as `null` / `0` (not recorded). `/v1/chat/completions` validates `knowledge_filters.scope` the same way and treats `all` like an omitted scope for `mode=knowledge` (every visible collection). Optional scalar values use the
empty string rather than disappearing. `effective_mode` is the engine that
executed: `agent_kernel`; earlier runs of the retired mission engine read
`workspace_agent`.
`consent_reason` is a stable machine token such as `explicit_directive`,
`strict_approval_required`, `strict_approval`, `strict_rejected`,
`autonomous_policy`, or `permission_policy`; clients should map known values
for display and preserve unknown future values.

`tool_use_counts` counts successful source-tool invocations, so zero means “not
used” while `source_policy.*="available"` means only “allowed to be selected”;
keep them as separate audit rows. A quick-web completion has exactly `web: 1`,
a rejected strict quick-web approval remains `web: 0`.

`tool_use_counts` counts uses of a source, not searches: a delegated
research is one web use there and no search, and reading a document is one
knowledge use. A batch of subtasks is one web use when it carries a research
assignment and one knowledge use when it carries a knowledge assignment,
however many of each it carries and whatever became of their children, as a
single delegated research counts. The search caps of a tier count searches only, one per
successful `web_instant` and one per successful `search_project_knowledge`,
so a document read spends none of them. `web_searches` (since v0.2.0.10)
counts the web searches the run's own provider calls executed, one per
successful instant search; a stored mission run counted its web discovery
probes and one per completed instant web task. A
research child reports the searches its provider executed as
`executed_queries` in its snapshot; `total_queries` there counts the planned
queries, which a deadline can stop before they run. A run that records no
searches (a role, a stored mission run) carries no `executed_queries`. A client adds the
run's `web_searches` and the `executed_queries` of its research children for
the searches a turn ran. Payloads written before the field read
`web_searches` as `null` (not recorded).

`knowledge_searches` (since v0.2.0.13) counts the searches of the knowledge
base a kernel run executed itself, one per successful
`search_project_knowledge`; a document read counts in
`tool_use_counts.knowledge` only, and so does a batch with a knowledge
assignment: the searches of its knowledge children are not added to the
parent's count. A stored mission run kept no count of its searches
alone and reads `null`, as does a payload written before the field: a
client then has only the counter of source uses, which takes a document
read like a search.

The completed `GET /v1/runs/{run_id}/result` payload copies this object to its
top-level `execution` field. Older stored results and non-agent algorithms omit
that field; clients should retain their existing compatibility view when it is
absent rather than inventing effective values.

## Event envelope

`GET /v1/runs/{run_id}/events` returns SSE frames where the event name matches `event.type`:

```text
event: inqtrix.node.started
data: {"type":"inqtrix.node.started","run_id":"run_abc123","sequence":3,"created_at":1760000002.0,"data":{"node":"plan","snapshot":{"current_node":"plan"}}}
```

Event payloads are sanitized through the same recursive drop-list used by runtime logging. Unknown event types are allowed so future UI events can be added without changing the log schema.

## Event types

| Event | When emitted | Important data |
|---|---|---|
| `inqtrix.run.queued` | Run record created or a parked run is admitted for another segment. | `status`, `queue_position`, optional `resumed`, `segment_id`, `segment_ordinal` |
| `inqtrix.run.started` | The first execution segment starts. | `status`, `snapshot`, `segment_id`, `segment_ordinal`, `reason` |
| `inqtrix.run.resumed` | A later execution segment actually starts. | `status`, `snapshot`, `segment_id`, `segment_ordinal`, `reason` |
| `inqtrix.run.snapshot` | A snapshot was updated for a state-bearing event. | `snapshot` |
| `inqtrix.progress.message` | Existing `emit_progress(...)` message fires. | `message`, `phase`, `severity`, `snapshot` |
| `inqtrix.node.model_resolution` | A node resolves its provider model and reasoning effort. | `node`, `model`, `tier`, `effort`, `verbosity` (the level a node that writes prose asks for, otherwise empty), `model_source`, `effort_source`, `requested_tier` |
| `inqtrix.node.started` | LangGraph enters a node. | `node`, `snapshot` |
| `inqtrix.node.finished` | Node returns normally. | `node`, `snapshot` |
| `inqtrix.node.failed` | Node raises. | `node`, `snapshot` |
| `inqtrix.answer.draft` | Since v0.2.0.10. A conversational kernel run whose review pass runs (the `verify` level of its Stufe, or the legacy rule without one) shows its answer before the review starts: once, after the draft passed the same citation check as the published answer. Runs without a review emit none. | `artifact_id` (the answer artifact the published answer will use), `draft_id`, `markdown` (the whole draft), `reference_labels` (the labels the draft cites) |
| `inqtrix.answer.started` | One final-answer publication begins after the empty durable answer artifact is committed as `writing`. | `artifact_id`, `publication_id`, `status=writing`, and additively since v0.2.0.8 `reference_labels` — the evidence labels this answer WILL cite, announced before the first delta so a client can render `[W1]` as its final linked form from the outset instead of rewriting the block when the references land (absent = no labels announced; a client must still accept labels it was not told about) |
| `inqtrix.output_text.delta` | Final Markdown is emitted in word-aligned chunks. | `artifact_id`, `publication_id`, UTF-8 byte `offset`, `delta` |
| `inqtrix.answer.ready` | The publication completed without a gap. | `artifact_id`, `publication_id`, `bytes`, `status=ready` |
| `inqtrix.answer.interrupted` | Publication stopped before completion. | `artifact_id`, `publication_id`, `offset`, `status=interrupted`, `stage` (`staging` / `streaming` / `finalizing` / `publishing_ready`) |
| `inqtrix.run.completed` | Result payload was stored. `metrics.prompt_tokens`/`completion_tokens` are run-cumulative across parked and resumed segments. | `metrics`, `result_url`, `snapshot` |
| `inqtrix.run.waiting` | Agent run parked itself for an approval, user input, or its running children. | `status` (`waiting_for_approval` / `waiting_for_input` / `waiting_for_children`), `snapshot` |
| `inqtrix.run.cancel_requested` | Cancellation was requested while running. | `status`, `reason` (`client_requested_cancel`, also for a running child when its parent's tree is cancelled; `cancelled_by_admin` when an instance admin cancelled runs this server cannot run; `parent_failed` / `parent_cancelled` for a running child of a run that failed / ended in another way than by cancelling its tree; `session_deleting` while its session is deleted) |
| `inqtrix.run.cancelled` | Run is terminal cancelled. | `status`, `reason` (`cancelled_while_waiting` / `approval_timeout` / `children_timeout` for parked runs; `workflow_contract_changed` / `workflow_retired` for a run its workflow binding ended, which also carries `bound_contract` and, when the worker carries the mode, `worker_contract`; `cancelled_by_admin` for a run an instance admin cancelled because this server cannot run its binding; `parent_failed` / `parent_cancelled` for each active child of a run that failed / ended in another way than by cancelling its tree, while a cancelled tree gives each child the reason of its own state (`cancelled_before_start`, `cancelled_while_waiting`, or after a request `client_requested_cancel`); `session_deleting` for a run of a deleted session; a run that was asked to stop ends with the reason of its latest request), `snapshot` |
| `inqtrix.run.failed` | Worker failed. | `status`, `error`, `snapshot` |

A resumed run first emits another `inqtrix.run.queued` event with
`resumed: true`; `inqtrix.run.resumed` follows only when the worker actually
starts that segment. `started_at` remains the first execution start. Agent-run
summaries expose the accumulated active, explicit-wait and resumed-queue time
in `timing`; worker reclaim does not create a false execution segment.

Knowledge runs additionally emit these typed retrieval events:

| Event | When emitted | Important data |
|---|---|---|
| `inqtrix.knowledge.retrieval.completed` | The shared retrieval implementation has produced the initial candidate set for the evidence budget. | `candidate_count`, `top_k`, `final_k`, `final_k_overridden`, `collection_document_count`, `degradations`, `warnings` |
| `inqtrix.knowledge.retrieval.degraded` | A technical vector boundary or stalled candidate page prevented the requested candidate pool from filling. Ordinary corpus exhaustion does not emit this event. | `reason`, `retrieval_mode`, `stage=vector_candidate_pool`, `requested_candidate_pool`, `returned_candidate_pool`, `final_top_k`, `returned_hits`, `final_evidence_complete`, optional `candidate_cap` |
| `inqtrix.knowledge.retrieval.warning` | Canonical hydration excluded one or more unsafe candidates. The event is a cumulative, text-free snapshot after the last retrieval, including decomposition and gate rewrites. | `code`, `reason`, `stage`, `count`, optional `recommended_action`; known codes include `chunks_require_reindex` and `chunks_pending_reconciliation` |
| `inqtrix.knowledge.answer.retry` | The first answer attempt contained quotes that failed verbatim verification; the single visible regeneration attempt is about to run. At most one per run, only for `quote_unverified` (format failures stay immediately terminal). Counter-only by design — quote texts never cross the event boundary. | `attempt` (always `2`), `quotes_total`, `quotes_unverified` |

Reconnect, reload, or export cannot turn a visible degradation into an ordinary
successful retrieval. A transport timeout, provider retry exhaustion, gate parse
fallback, or other quality downgrade likewise remains a typed event/result
marker or terminal failure; it is never hidden behind a completed event. In the
warning event, `count` is the number of exclusion observations across the run's
retrieval calls, not a distinct-chunk count. Why candidate-pool and
final-evidence completeness are separate facts, and where the receipts live:
[Knowledge engine](../architecture/knowledge-engine.md#retrieval-pipeline-question-to-cited-answer).

Agent control actions additionally emit signal events (rows are the truth,
clients reconcile via the GET endpoints above):

| Event | When emitted | Important data |
|---|---|---|
| `inqtrix.agent.approval.decided` | A user decided an approval. | `approval_id`, `status`, `decided_by_user_id` |
| `inqtrix.agent.clarification.answered` | A user answered a clarification. | `clarification_id` |
| `inqtrix.agent.limit.reached` | A deterministic tool/step/token ceiling was reached. Recoverable ceilings park the run instead of silently returning a partial answer. | `kind`, `used`, `limit`, `ceiling`, `extendable`, `state`; a recoverable park adds `clarification_id`, `next_limit` and the supported `choices` (`extend`/`partial`/`cancel`), a hard stop adds `recoverable` and `reason` |
| `inqtrix.agent.limit.decided` | The user explicitly chose a terminal partial answer or cancellation after a recoverable limit. | `clarification_id`, `kind`, `choice`, `state` |
| `inqtrix.agent.outcome` | A conversational `agent_kernel` run left its algorithm with a decision: once per algorithm decision, when the run completes, is cancelled while it executes, or fails inside its graph or after it; a redelivered segment can repeat it under the same `outcome_id`, readers take the last. A role run sends none. | `outcome_id` (`<run_id>:outcome`), `final`, `review`, `round`, `revision`, `accounting`, `basis`, `unavailable_reason` (only on an `unavailable` review), `unexplained` (only on an `inconsistent` one), `applied`, `verified` (how many applied findings the re-check held), `unverified_reasons` (per reason, how many applied findings the re-check left unverified), `reach` (`reread`, `cited`, `provider_only`), `codes`, `limits`, `open` (`by_code`, `by_kind`), `lines[]` (`kind`, `text`, `reasons`), `claims` (per output, its statements by state), `marks[]` (`output_id`, `quote`, `state`, `reason`, `note`), `summary`, `details` |
| `inqtrix.agent.artifact.created` | An agent artifact was committed for the first time. For an answer this precedes `answer.started` and exposes only an empty `writing` body. All agent-side emitters share one payload builder. | `artifact_id`, `kind`, `revision`, `title`, `status`, `updated_by`, `from_revision`, and additively since v0.2.0.8 the write's line delta `lines_added`/`lines_removed` (absent = honestly not counted, e.g. events from before the change or the size guard) |
| `inqtrix.agent.artifact.updated` | An artifact revision advanced: agent-side writes (kernel `write_canvas`, the deep-revision batch) emit the shared agent payload; a user content PUT emits a deliberately kind-less signal (`artifact_id`, `revision`, `updated_by` only — it must never render as a file chip); a user rename adds `title` and `renamed: true` at an UNCHANGED revision. | agent writes: same columns as `created`; user PUT: `artifact_id`, `revision`, `updated_by`; rename: + `title`, `renamed` |
| `inqtrix.agent.citation.validation` | A final kernel chat answer contained unknown citation labels or citation brackets no reader can parse (a range, a slash, a prose join) and completed its single bounded repair decision. | `status` (`repaired` / `degraded`), `unknown_labels`, `malformed_brackets`, `resolution` |
| `inqtrix.agent.requirement.attached` | Emitted once per agent run that starts with a result requirement, when its first segment seeds the run: the transcript's record of which library rules at which frozen revision the run works under. Not repeated when a later segment resumes the run. The typed text appears by its length only; `free_text_chars` is `null` — with a server WARNING — when the stored requirement does not split into its stored parts (a run queued before the parts were kept). `review` is `per_rule` when the run's review pass judges each attached rule on its own, one criterion per rule (a kernel run that owes a review and carries rules), `assignment` when the pass checks the result against the assignment the requirement is part of as a whole (typed text without rules), and `none` when its tier skips the review. A client that does not know a value shows none. Rule labels, titles and template ids persist with the run's events (an event shows a title over 180 characters shortened with `...`; the stored part keeps it whole): everyone who can read the run's events keeps seeing them after the template is edited, deleted or unshared; the event never carries rule text. | `rules[]` (`template_id`, `label`, `title` when the part has one, `revision`), `free_text_chars`, `review` |
| `inqtrix.agent.canvas_context.attached` | Emitted exactly once per kernel run that carried a canvas attachment — the durable transcript record of which comments rode the submission (run summaries deliberately never carry the canvas context). Comment text travels in full; the quote is a visibly shortened 120-char preview whose full text stays reachable via the document. | `artifact_id`, `revision`, `comments[]` (`comment`, `quote_preview`) |

The cognitive kernel (`mode=agent_kernel`) additionally
emits `inqtrix.agent.tool.started` (`tool`, `tool_call_id`, redacted
`args_preview`), `inqtrix.agent.tool.finished` (`tool`, `tool_call_id`,
`status`, `result_preview`, `result_call: true` on a role run's typed result
call — the call that delivers its verdict, no step of its work —,
`call_ordinal` (the call's position in the run's sequence of counted calls,
taken when its start is emitted; absent on the typed result call and on a call
that started in an earlier segment)
and additively since v0.2.0.8 a `snapshot`
carrying the complete canonical triple `{current_node, phase,
execution}` described under "Run summary" — built at the tool boundary,
so limit readouts advance per finished tool instead of only per phase
change; both the graph lane and the quick-web lane attach it),
`inqtrix.agent.todo.updated` (the
`write_todos` task list as `{content, status}` items), and
`phase.changed` (`execution` -> `done`) for timeline compatibility. A gate
parks the run after `inqtrix.agent.approval.requested` (`approval_id`,
`kind: "tool"` and the gate's payload with its proposed `actions`), and
`ask_user` or a skill's missing input after
`inqtrix.agent.clarification.requested` (`clarification_id`, `question`,
`options`, `question_count`). Since
v0.2.0.9 a `tool.started` row may carry `todo_index` (integer): the index
of the first `in_progress` item of the last reported todo list, so a
transcript can fold the row under the todo it served; absent before the
first list and when no item is in progress. Since v0.2.0.10 `args_preview`
holds reader words only: the call's query, question, assignment, objective,
instruction or title, an attached research report's title, or the server's
own words for an archive or document window; a call that names nothing but
identifiers previews empty, and the preview never carries JSON or an id. A
`read_canvas` row on a canvas document carries that document's `artifact_id`
as its own field instead, so a client names it by the title it knows; a read
of the run's own archive carries none. The
kernel emits NO intent narration since v0.2.0.9: the tool row
(`tool.started` with `args_preview`) already carries tool identity and
subject, derived from the accepted call; older runs still replay their
`kind="intent"` narrations (`kernel_<sha1[:8]>` ids), the kind stays valid. A successful `load_skill` call emits
`inqtrix.agent.skill.loaded` (`skill_id`, `label`) — the visible
counterpart of the tool-limit narrowing it applies. Whether a run is
reviewed follows the published `verify` level of its Stufe (`labels`
waives the pass; a request without a Stufe keeps the legacy rule
`depth=deep`), and the review reports as one narration with
`narration_id=kernel_deep_review` — "running", then the located finding
count while the revision runs, and after a verdict the run's outcome
record `summary` — plus an `inqtrix.node.model_resolution` event for the
`agent_deep_review` node. Besides completeness, support and
contradictions, the review asks the fidelity questions of every cited
statement: force (duty, expectation, permission, example or indicator),
reservations, the intro of a list, links and quantifiers, actors and
reach, and absence. A statement of what the material does not cover is a
factual claim about the documents: contradicted by material the review
read it is an `evidence_error`; a reading limit alone makes no finding of
it, and a claim the review cannot check is named as not checked. The revision keeps the same categories as writing
rules. Findings are located, applied or named one by one: when the
review returns findings, one `inqtrix.agent.activity` event
(`kind=deep_review_findings`, `scope=run`, `label`, `detail`, `findings[]`
with `quote`, `finding` and `target` — the flagged passage and what is
wrong with it) lists the located ones before the revision call. The
anchors, grounds and snapshot offsets that drive the edits stay
server-side. The revision returns only replacement text for each finding.
An edit cites only its passage's labels and its basis's labels in the
target output's label space, matched by reference identity; a kernel
canvas cannot take a source it does not cite, so such a finding is named.
A passage owns the citation run right after it (`Satz. [K12]`). Without a
chat finding no chat edit is generated. Untouched text stays verbatim, and
an edit that changes how it cites (an opened fence, a lone backtick) is
named; artifact changes use the existing atomic expected-revision check.
Unresolvable or overlapping anchors, invalid grounds, rule-breaking edits,
context limits and refused writes name that finding with its reason code;
they are visible outcomes, not successful corrections: the summary counts
them, and a `critic_revision_discarded` activity lists each named finding
with its reason.

Every `inqtrix.agent.phase.changed` snapshot carries the canonical
`execution` projection described above, so clients can distinguish the
source being available from the source actually being used.

The outcome record (`inqtrix.agent.outcome`) says how an agent run's review
ended and what stayed open: a conversational kernel run's (a kernel role
run sends none; a stored mission run's history holds the records its
mission sent, see [Event types in older mission runs](#event-types-in-older-mission-runs)).
`final` is the algorithm's own decision
(`completed`, `cancelled` or `failed`), not the run's terminal status: a
cancel that races the algorithm's return, or a failed answer publication
after it, still ends the run otherwise, so a client takes the terminal word
from `inqtrix.run.completed`/`failed`/`cancelled`. A run cancelled while it
was parked or waiting, and a run that failed before its graph started, has
no record. The fields:

- `review`: `completed`, `unavailable` (a review was due and produced no
  usable verdict), `waived` (the run's Stufe skips the review) or `not_run`
  (no review ran: the run ended before one, or a request without a Stufe
  owed none); `round` is the review round (`0` without a review). An
  `unavailable` review also carries `unavailable_reason`: `inconsistent`
  (the verdict negated a flag and named no reason for it), `failed`,
  `budget_exhausted`, or another code of a review that returned no verdict
  (`context`, `result_missing`, `schema_invalid`, `unavailable`). An
  `inconsistent` one names the flags it gave no reason for in `unexplained`
  (`complete`, `grounded`, `contradictions_named`), and the summary names
  them too. A negative flag counts as explained by an unconfirmed finding,
  an open or unsettled criterion or a point the review could not check, and
  a false `grounded` also by the basis alone when it is `knowledge` or
  `provider_context`.
- `basis`: what the review could check against, decided by the server:
  `evidence`, `knowledge` (the run holds no stored evidence) or
  `provider_context` (at least one reference is cited and every cited one
  carries the search provider's context alone, without stored source text).
  A `provider_context` review names that limit once, in the summary's head.
  The server decides the basis when it assesses a verdict: for a
  `completed` review and for an `unavailable` one whose reason is
  `inconsistent`. A record without an assessed verdict (`waived`, `not_run`,
  any other `unavailable` reason) carries `evidence` and states nothing by
  it.
- `revision`: `none`, `verified` (every finding's edit was applied and the
  re-check held each one), `unverified` (every finding's edit was applied
  and at least one was not held: the re-check rejected it, left it
  unchecked, did not confirm it or could not run), `partially_discarded`
  (some findings were applied, others named) or `discarded` (no edit was
  applied and the previous text stands). The text is not reviewed a second
  time; each applied model edit is checked, segment by segment, against the
  texts of the labels it cites, and the server settles it: it holds only
  when a second, identical check holds it too, and no settlement takes it
  back.
- `accounting`: `complete` only when a completed review settled every
  success criterion; a record without that fact reads `incomplete`. A
  record built from a review report written before the report carried its
  round, revision and accounting (a run resumed across an upgrade) reads
  `review` `completed` with `round` `0`, `revision` `none` and `accounting`
  `incomplete`; the server logs a warning when it builds one.
- `limits` and `codes` stay empty in a kernel run's record: the kernel has
  no research route. A stored mission run's record names in `limits` why
  research stopped although an information criterion stayed open and in
  `codes` why no further research could reach unread material (see
  [Event types in older mission runs](#event-types-in-older-mission-runs)).
- `applied`: how many findings the revision applied; `verified`: how many
  of them the re-check held; `unverified_reasons`: why it left the others
  unverified, counted per reason in this order and only where it occurs —
  `no_source_text` (the new text cites only sources without stored text),
  `format_only`, `too_large`, `check_unavailable` (the check could not
  run), `inconclusive` (it ran and settled nothing) and `knowledge_only`
  (the run holds no source, so the edit rests on general knowledge). A
  rejected edit has no reason, so the counts may sum to fewer. A record
  built from a review report written before its findings carried their
  reasons counts none; the server logs a warning when it builds one.
- `reach`: how many cited references the review re-read (`reread`) of how
  many the outputs cite (`cited`), and how many of the cited references
  rest on the search provider's context alone, with no stored source text
  (`provider_only`; the summary names them in one line, except on the basis
  `provider_context`, whose head already says so once). An evidence
  finding resting on nothing but such sources of its own passage asks for
  no revision: the passage stays as written and is marked
  (`provider_context_only`). A
  record built from a review report written before the report carried it
  has no `reach`; the server logs a warning when it builds one, and an older
  `reach` has no `provider_only`.
- `open.by_kind` counts the open points as `information`, `text_defect`,
  `finding`, `marked` (a passage that stays as written with its mark, never
  a finding the revision left unapplied) and `not_checked`, and as
  `revised` an unmet output requirement
  that at least one counted model finding concerns, every such finding
  applied, whether the re-check held those edits or not: its gap in the
  reviewed draft was worked on, which never shows the criterion met, since
  nothing checked the criterion again.
  An unmet criterion without its `state` reads open. `open.by_code` maps
  every limit and code to the number of open information criteria it
  applies to.
- `lines[]` are the open points in the reader's language, each with its
  `kind` and its `reasons`: for an information criterion the limits and
  codes it stayed open for, for a named finding its reason code, for a
  finding without a confirmed basis `basis_unconfirmed`, for a point the
  review could not check `not_checked` (once an edit of that review is
  applied, its line says the point was noted before the revision: it
  describes the reviewed draft, and the delivered text may no longer hold
  what it names), for a revised
  criterion `criterion_revised`, or `criterion_verified` when the re-check
  held every one of its edits (its line names the applied findings, says
  whether they were re-checked, and quotes no gap of the reviewed draft).
  A finding that marked its passage (`provider_context_only`) is a line of
  kind `marked`. A named finding's reason is one of `finding_invalid`,
  `basis_invalid`, `basis_outside_output`, `remove_labels_invalid`,
  `anchor_missing`, `anchor_ambiguous`, `anchor_overlap`,
  `provider_context_only`, `edit_not_single`, `edit_unchanged`,
  `edit_deletes`, `edit_shrinks`, `edit_rewrites_unchecked`,
  `edit_drops_figure`, `identifier_added`, `citation_malformed`,
  `label_not_citable`, `quote_label_dropped`, `label_moved`,
  `segment_without_basis_label`, `labels_whole_text`, `output_emptied`,
  `output_shrunk`, `revision_context`, `revision_failed`, `revision_empty`,
  `write_conflict`, `store_error`, `cancelled` or `revision_error`
  (`research_route` occurs only in records of stored mission runs).
  `provider_context_only` marks a passage: the finding rests on nothing but
  sources of that passage without stored source text, so no revision is
  asked for it, and it counts neither as applied nor as discarded. A fix
  resting only on such sources keeps every sentence of its passage word for
  word, moving a label or adding a sentence of its own
  (`edit_rewrites_unchecked` otherwise), and no fix drops a number of its
  passage (`edit_drops_figure`); a correction from general knowledge, in a
  run without sources, may change a number.
- `claims`: what the delivered statements rest on, per output (`chat` for
  the answer, otherwise the canvas's artifact id), as `total` and one count
  per state. A statement is a run of sentences up to the citation group
  that closes it, inside one paragraph, list item or table row; sentences
  after the last citation of their unit belong to none, a sentence with
  fewer than two content words lends its citations to the statement before
  it, a line naming a web address outside a link is a reference entry, and
  an output that cites nothing has no statements. Each statement has one
  state: `verified` (the stored text of a source it cites states it, as far
  as words can tell, and no edit the re-check did not hold wrote it),
  `source_text` (a cited source holds stored text that does not state it
  that way), `attributed` (its sources hold only the search provider's
  context) or `insufficient`. Counted on the texts as published (after the
  citation check); a review from general knowledge counts none, and a
  record without statements has no `claims`.
- `marks[]`: one entry per `insufficient` statement, in text order: its
  `output_id`, its passage as the delivered text holds it (`quote`,
  citations included, so a client finds it again by its words), its
  `state`, its `reason` — `finding` (a confirmed evidence finding no
  revision applied stands on it), `metadata_only` (its sources hold nothing
  but their metadata) or `restates` (it repeats a value, of the same kind,
  of the passage a finding disputes, not of the rest of the statement that
  passage stands in; a year alone does not count, nor does the date that
  only says when a disputed percentage or amount holds, and a statement
  with stored source text of its own keeps its state) — and its
  `note` in the reader's language. A client shows a mark only while it
  shows the record under that text, never once the user edited it.
- `summary`: the record in one sentence of the reader's language, built
  from its states and counts only, never from a line's text — for example
  "Geprüft · 2 Befunde überarbeitet, nicht nachgeprüft · 1 Befund nicht
  angewendet · 2 Punkte nicht geprüft · 1 Kriterium offen · 12
  von 12 zitierten Belegen nachgelesen". Only an applied edit the re-check
  held reads "behoben und nachgeprüft" ("fixed and re-checked"); every
  other applied edit reads "überarbeitet, nicht nachgeprüft" ("revised, not
  re-checked"), followed by the reasons in parentheses with their counts,
  for example "3 Befunde überarbeitet, nicht nachgeprüft (2 mit Belegen
  ohne gespeicherten Quelltext, 1 nur Formatierung)"; the other reasons
  read "zu umfangreich", "ohne mögliche Nachprüfung" and "ohne eindeutiges
  Prüfergebnis". "ohne Befund" appears only when nothing stayed open.
  A completed review opens with "Geprüft", with "Geprüft aus Modellwissen,
  ohne Quellen" on the basis `knowledge` and with "Gegen
  Anbieterauswertungen geprüft, Originalseiten nicht gelesen" on the basis
  `provider_context`. Without a completed review it reads "Ohne Prüfung
  (Stufe)", "Ohne Prüfung beendet" (`not_run`, `final` `completed`) or "Vor
  der Prüfung beendet"; an `unavailable` review reads by its
  `unavailable_reason`: "Prüfung gescheitert, Ergebnis ungeprüft"
  (`failed`), "Prüfung am Budget beendet, Ergebnis ungeprüft"
  (`budget_exhausted`), "Prüfurteil widersprüchlich: Vollständigkeit ohne
  Begründung" (`inconsistent`, naming every flag of `unexplained`) and
  "Prüfung nicht auswertbar, Ergebnis ungeprüft" for every other reason.
  A client that needs the state reads `review` and `unavailable_reason`,
  never the sentence. A record
  with `claims` reads as their count instead, summed over the outputs, each
  state only where it counts — for example "Geprüft · 24 Aussagen:
  3 verifiziert, 2 mit Quelltext, 15 vom Anbieter zugeschrieben, 4 nicht
  ausreichend belegt" —, and everything the sentence above would name
  stands in `details`.
- `details`: what a review that counted statements did besides, in the
  words the summary uses without the count ("ohne Befund · 4 von 4
  zitierten Belegen nachgelesen"); only on a record with `claims`.

A kernel run's record reads these fields from its own review: `round` is
`1` beside `review` `completed` or `unavailable` (a kernel run reviews once;
an owed review that could not run or gave no usable verdict counts) and
`0` beside `waived` and `not_run`, and a completed review reads `accounting`
`complete` unless it left an attached rule unsettled: a kernel run's review
settles one criterion per attached library rule (and no other success
criteria). An unmet rule is an open line that names the rule by the title it
was attached under and its frozen revision ("Kriterium offen: Regel
„Executive Briefing“ (Rev. 1) — …"), never by its prompt marker; a met rule
adds no line. A
kernel run parked for its review role sends the record from the segment that
resumes and decides, never from the park. A located finding whose revision a
cancel or an error stopped is named `cancelled` or `revision_error`; a
partial answer at a limit reads `review` `not_run`. The kernel keeps no
`critic_report` artifact.

After a revision applies at least one edit of a model finding, one more
structured call on the review node (`agent_deep_review`, schema
`RevisionCheckVerdict`) re-checks those edits; when every one of them is
left unchecked before the call (no segment, more than 12 segments, or new
text resting only on sources without stored source text, such as a web
source kept as the search provider's context), there is no call. The server lists the segments each edit changed (units
of headings, sentences and code blocks, a sentence split after `;` or `:`,
compared by their form (a sentence that becomes a list item, a heading or
a table row is a segment), their words, the marks that say as much as a
word (an order or approximation sign such as `<` or `≈` wherever it
stands, `=` or `~` before a digit, a sign before a digit or before a
currency sign or second sign a digit follows, a separator between digits,
a percent, permille, currency or section sign) and their labels) and where
its replacement stands (heading, list intro, the text on either side); each
finding's prose, the text its edit replaced, the replacement, the segments,
the placement and the texts of the labels the replacement cites travel
fenced as data. The call returns one check per finding (`findings[]`:
`finding_id`, `rows[]`, `fixes_named_defect` and a `reason` that stays in
the trace) with one row per segment (`rows[]`: `segment_id`, `basis`
quotes, `force`, `bound`, `actor_reach`, `reservations`, `links`,
`presents_as`, `basis_shows`, `already_in_replaced_text`, `before`,
`restated_in`, `finding_quote`); it gives no verdict. The server checks
every quote verbatim against its source text or the reading record the
finding rests on and settles each edit from a closed table: it holds
only when every row carries it, is rejected only for a deviation a
verified quote shows on a row that states a claim (`not_carried`, a duty
or deadline where the basis shows a weaker kind included), a dropped
condition, exception or qualifier (`reservation_dropped`) or a removal no
words of the finding ask for (`off_finding`), and otherwise stays
unchecked, among them every edit without a segment, of more than 12
segments, whose new text rests only on sources without stored source
text, or beyond 40 segments in one call. The re-check can only
confirm an edit, never take one back: when the call holds at least one
edit, a second call sends the same request byte for byte, and an edit
reads `recheck` `held` only where both calls hold it. Every other edit
stays applied and unverified, a rejected one included; its code reaches
the server log alone, never a finding's `reason`, a reader line or the
outcome record, and the text stays the one the revision composed. A
re-check that cannot run, or a confirmation that fails, leaves those
edits applied and `unverified`.

`inqtrix.agent.activity` is the compact live-status channel for
short-lived runtime facts (rendered as the one-line status readout). A
source operation adds the stable fields `scope`, `operation`, `status`
(`started`, `completed`, or `failed`), and optional `task_id`, `query`,
`purpose`, `current`, `total`, `metrics`, `attempt`, `error`, `fallback`,
and `evidence_limit`. In a kernel run that is a provider retry of a search
(`kind="searching"`, `scope="task"`, operation such as `web.search.instant`,
with `attempt` and a `retry` block) and the refusal of a research child a
Stufe does not allow (`operation="web.research"`, `status="failed"`, error
code `research_tier_blocked`). Events bracket the real operation rather
than announcing an entire batch before work starts. Additively since
v0.2.0.8 the kernel brackets every model turn as
`operation="agent.model.turn"` with the constant `activity_id="model-turn"`
(`kind="working"`, `scope="run"`), so clients upsert ONE live row per run
that flips `started` -> `completed` instead of stacking a row per turn, so
the model boundary — the kernel's longest event-free stretch — is visible.
A retried model call is a `working` row of its own, labelled with what the
call does. The kernel's `analyze_document` reads its documents window by
window as `operation="document_analysis"` (`kind="reading"`,
`scope="run"`), one row per document:
`activity_id="<tool_call_id>:document-analysis:<document_id>"`,
`document_id`, `detail` (the document title), `current` (windows
processed) and `total` (ALL windows planned for the document), `started`
for the first and every fifth processed window, and one terminal
`completed` (or `failed` for a document that could not be analysed or had
no window analysed) with `current` = windows analysed — so a budget stop
settles at, for example, 4 of 32.

The parent `inqtrix.agent.child.progress` projection carries `task_id` (the
origin key of the call that started the child), `child_run_id`,
`run_status`, `event_type`, the latest real `current_node`, a compact
snapshot, a sanitized message/error, metrics, attempt, and update time. It
carries the child's lifecycle, snapshot, node and progress events, the
knowledge child's retrieval and answer events (as bounded counters, never
quote or source text), and `inqtrix.agent.phase.changed`. A kernel ROLE
child (a review, a run with a `work_profile`) also crosses its settled tool
calls (`agent.tool.finished`) as `tool` (the wire name) and `tool_calls`
(the server's call counter from the event's snapshot) — only at its first
call and every fifth by the call's position (`call_ordinal`), so a batch of
parallel calls that settles with one shared counter crosses too; a step
without its position does not cross; never its typed result call, and never
the call's arguments or result preview. Every other event of a child (its
activity rows, its narration, its outcome record, `output_text.delta`)
stays in the child's own stream. A role child adds one row per five tool
calls (a review at its ceiling of 120 calls: about 25). An identity-only
child-start event has no current node and means "preparing"; clients must
never infer the terminal `answer` phase from a missing snapshot. The parent
stream powers the overview, while a selected child may replay its own run
stream for detailed diagnostics.

The `kind` values a kernel run's activity rows carry include `searching`,
`working`, `reading`, `memory_unavailable` (staging the run's memory
candidates failed), `deep_review_findings`, `deep_review_waived` (this run's
Stufe verifies nothing — `verify` carries the published level; a contract,
not a degradation), `deep_review_unavailable` (a review was owed and did not
run — `reason` and `transport` name why) and `critic_revision_discarded`
(the revision left at least one counted finding unapplied; the label says
whether it was discarded wholly or partly, the detail lists the named
findings with their reason). Whether a review passed or left points open is
stated by the outcome record: no activity reports either, and a client reads
`review`, `summary` and `lines` of `inqtrix.agent.outcome` instead of
inferring how a review ended from a phase or an activity.

`inqtrix.agent.narration` is the user-facing narration channel: short prose
lines derived deterministically from what the run already produced (a
finished child's report head, the review's progress and outcome, a limit, a
status) — never a raw chain of thought, and no extra LLM call. Payload:
`narration_id`, `kind` (`task` / `synthesis` / `limit` / `intent` /
`status`), `text`, `phase`, `final`. `narration_id` is STABLE per emission
site (e.g. `kernel_deep_review`, `n-child-<run_id>`): a re-executed step
re-emits the same id with a fresh sequence, and clients upsert by
`narration_id` instead of appending a duplicate line. The payload passes
a strict sanitizer allowlist (exactly the fields above). A kernel run
emits one `task` line per finished delegated child (`n-child-<run_id>`:
the assignment plus the report's head), so a run with research children
tells the reader what came back. Heads never start with a heading
marker; lines may carry inline Markdown (bold, code, links), which the
Agent Desk renders.

The rows of the internal review role (`review_role`) ride the
`tool.started`/`tool.finished` protocol. A successful row's `result_preview`
is a count in the run's language ("2 Befunde gemeldet" for the findings the
review reported, confirmed or not). A failed row's preview is an internal
diagnostic, not a reader text: an exception class name or a raw validation
message, visibly shortened at 240 characters. The Agent Desk does not
render `result_preview`; it is an event-API field.

The final answer body never uses the narration channel. The server first
persists an empty `writing` answer artifact, then emits `answer.started` and
the contiguous deltas. Immediately before `answer.ready`, the same artifact is
CAS-finalized with the complete Markdown and the exact reference list exported
in the run result. Clients render only contiguous deltas for the matching
publication id through the shared Markdown renderer and refetch the
authoritative artifact on `ready` or `interrupted`; they never infer a complete
answer from an artifact signal alone.

A reviewed kernel run shows its draft first. `inqtrix.answer.draft` carries
the whole draft in one event, stored like every other event, so a reload or a
`?format=json` read finds it again. The review reads the answer as the model
wrote it, and the published answer is the reviewed text after its own citation
check. Without a finding it is the draft's text, unless the check had to repair
a citation through a model call, which may word that repair differently the
second time. The draft is never stored as an
artifact revision: it is no editable or exportable answer, and the published
answer replaces it once `answer.ready` arrives. A client keeps the draft
visible until then, also across `answer.started`, and keeps it as an unchecked
draft when the run ends cancelled or failed without a published answer. A
segment that resumes after the review role parked the run does not repeat the
event. A redelivered segment that died before ordering its review may emit it
again under the same `draft_id`; the later event replaces the earlier one. A
draft whose citation check fails is not shown, and the log says so. The
in-memory development store keeps only the last `RUN_EVENT_BUFFER_SIZE` events
per run (default 200), so a long run there can lose the draft; the Postgres
store keeps every event of the run.

Decision/answer signals are emitted while the run is still parked, so they
PRECEDE the resumed `inqtrix.run.queued` event. They are optimistic: in the
rare race of two conflicting decisions, the loser's signal can remain in
the log although its decision was rejected with 409 — clients must treat
these events as refresh hints and read the approval state via GET. The event log of a run ends
with its terminal event: signals for already-finished runs (e.g. an
artifact edit after completion) are dropped with a warning — clients of
finished runs reconcile via the GET endpoints, never the closed stream.
Agent-side events (`artifact.created`, `tool.started`/`tool.finished`,
`phase.changed`) are emitted by the agent runtime itself.

User actions on agent runs are audited as `agent.approval_decided`,
`agent.clarification_answered`, `agent.artifact_edited`, and
`agent.artifact_exported` (audit `actor_type` stays `user`). The runtime's
OWN writes use `actor_type: agent` with the effective actor as
`actor_user_id`: a
document-targeted run records `editor.patch_proposed` when it proposes
edits, alongside the user's `editor.patch_applied` /
`editor.patch_rejected` on the decision. A document in collaboration mode
accepts no agent proposal: the run's patch tool refuses such a proposal
with `editor.collaboration_document`, so it writes no patch and no
`editor.patch_proposed`.

Terminal events are `inqtrix.run.completed`, `inqtrix.run.failed`, and `inqtrix.run.cancelled`. A browser can close its SSE connection after receiving one of those.

Background knowledge indexing operations use the same endpoint and event model
on a parallel surface: `GET
/v1/knowledge/indexing-jobs/{job_id}/events`. The operation summary identifies
`operation_kind` as `collection_generation` or `document_revision`; both use
the shared durable queue, checkpoint, cancellation, and replay contract, and
`GET /v1/knowledge/indexing-jobs` lists both unless `?operation_kind=` narrows
it. The
stream includes `inqtrix.index.{queued,started,progress,document_started,
document_progress,document_completed,raw_rebuild_requested,cancel_requested,
paused_dependency,paused_validation,resumed,completed,ready_raw_by_user_choice,
superseded,failed,cancelled}`. Progress snapshots contain real phases and
known counters rather than invented percentages. A dependency timeout or validation failure pauses
the operation visibly and leaves the active generation unchanged. Paused and
other non-terminal jobs are never failed by the terminal-history TTL or an
implicit maximum age. Continuing without contextualization requires the
explicit raw-build decision. Queue-backed recovery re-enqueues stale queued
rows and reclaims abandoned running/cancelling deliveries under a new attempt
fence. A no-queue restart reconstructs paused execution from the durable
operation identity before emitting `resumed`; an unsafe reconstruction returns
`resume_unavailable` without changing the status or checkpoint. In the durable
backend the jobs use a separate Valkey stream consumed by the same
`inqtrix-worker` — see [Web server mode](../deployment/webserver-mode.md).

Cancellation is a two-step lifecycle for running jobs. `POST
/v1/runs/{run_id}/cancel` returns the current summary, but a running summary can
still have `status="running"` because the cancel request is observed at the
run's next cancellation checkpoint. While the cancel is pending, the summary
additionally carries `cancel_requested: true` (emitted only in that state, so
historical summaries are unchanged). The intermediate
`inqtrix.run.cancel_requested` event tells clients that the request was
accepted; it is not terminal and should not move a card to the cancelled
bucket. Only `inqtrix.run.cancelled` or a summary with `status="cancelled"`
should do that. Queued jobs can skip the intermediate event and become
cancelled immediately.

Checkpoints are dense: besides the node boundaries, every provider retry
ladder checks before each attempt and during backoff sleeps, the search and
claim-extraction fan-outs abandon queued calls (visible as a warning progress
message plus the `cancel_abandoned_work` iteration-log marker with
`abandoned`/`in_flight`/`total` counts), and answer composition stops between
report sections. Typical time from cancel request to the terminal event is
therefore a few seconds; the residual worst case is the remainder of ONE
in-flight provider HTTP attempt (bounded by its transport timeout).

`inqtrix.progress.message` is the user-facing agent protocol. Native UIs should
prefer these messages for visible timelines because they match the terminal
progress wording (`Analysiere Frage...`, `Plane Suchanfragen...`, source and
statement quality summaries, report-section synthesis, warnings). Technical events such
as `inqtrix.run.snapshot`, `inqtrix.node.started`, `inqtrix.node.finished`, and
`inqtrix.output_text.delta` are still useful for state patches and diagnostics,
but should not be shown as primary user-facing steps. The progress event
`severity` is currently `info`, `warning`, `success`, or `error`; warnings cover
fallbacks, context-window notices, evidence limits of the report, and other
messages that should be highlighted compactly in cards without moving them into
duration or queue metadata. A step carries the severity its sender states; a
step that states none is `info`, whatever its wording. Among the steps that
send `warning` are the evidence-contract steps of a research answer (a cause of
`needs_review`, an answer from source context alone, a blocked report), the
failure of a research or chat-only run, a research or chat-only run stopped by a rate
limit, the two context-window notices, and the output-limit and cut-off
notices of a report section. The field always carries a value, so a client
needs no text matching of its own. Three messages mark a run's course for
programs and are no step: `done` (the end of a research run) and
`chat_only.start` / `chat_only.done` (the start and end of a chat without web
search). They carry the run's snapshot; a timeline leaves them out, as the
streamed chat completions and the library stream do. Native UIs may store an
optional normalized phase beside each visible event (`analysis`, `planning`,
`search`, `evaluation`, or `answer`) for compact phase visualizations, derived
from `data.phase`: every progress message carries it, with the graph node
that sent the message (`snapshot.current_node` holds the same node). It is
empty only for a message sent outside a node, such as the failure of a run
before its first node; such a message has no phase, and none is read from
its wording.

When a UI persists completed reports, it should keep the visible event records
as the report's agent protocol. In the React Research Desk project format those
records live in the completed run Markdown frontmatter under `events`; there is
no separate `agent_steps` field. Importers should tolerate older records without
`kind`, `severity`, or `phase` and default missing classification fields
defensively.

### Event types in older mission runs

The stored event history of a run of the retired mission engine
(`mode=workspace_agent`) is replayed as it was written: `GET
/v1/runs/{run_id}/events` filters by sequence only, never by type. Besides
the types above, such a history can contain types and values no current run
emits; a client keeps rendering them or tolerates them as unknown:

| Event | What it recorded | Important data |
|---|---|---|
| `inqtrix.agent.plan.proposed`, `inqtrix.agent.plan.revised` | A saved plan version: the first one, then each replanned one. | `plan_id`, `version`, `task_count`; a revision adds `reason` and `auto_approved` |
| `inqtrix.agent.task.started`, `inqtrix.agent.task.finished`, `inqtrix.agent.task.failed` | The lifecycle of one plan task. | `task_id`, `ordinal`, `tool_kind`, `title`; `started` adds `attempt`, the two terminal types add `status`, `child_run_id`, `result_summary` (at most 300 words, a longer one ends in `[…]`), `metrics` and, on a failure, `error` and `failure` |
| `inqtrix.agent.task.cancel_requested` | A user asked to cancel one plan task. | `task_id`, `status`, `child_run_id` |
| `inqtrix.agent.patch.proposed` | The mission proposed an editor patch; the always-gated patch approval followed. | `patch_id`, `document_id`, `artifact_id`, `edit_count` |
| `inqtrix.agent.artifact.edit_conflict` | A follow-up turn found the session memo edited by the user since it read it; the mission kept that text and appended its update. | `artifact_id`, `kind` |

Values of current types that only a mission wrote:

- `inqtrix.agent.approval.requested` of the kinds `plan`, `replan`,
  `discovery` and `patch`; a `replan` approval carries, next to
  `plan_id`/`plan_version`, the `reason` (`critic_research` or `replan`) and
  `new_tasks` (`task_id`, `ordinal`, `title`, `tool_kind`). These approvals
  stay readable and can no longer be decided (400 `workflow_retired`).
- `inqtrix.agent.limit.reached` with `kind` `clarification_rounds` (with
  `suppressed`, the number of questions the cap kept from being asked),
  `discovery_tool_calls`, `replan_rounds` or `mission_time_budget`.
- `inqtrix.agent.outcome` with `limits` (`no_new_evidence`,
  `replan_rounds`, `mission_time_budget`, `plan_task_limit`,
  `replan_invalid`, `replan_empty`, `no_gap`, `replan_rejected`) and `codes`
  (`read_budget_exhausted`, `source_bound_limit`, `collection_unavailable`,
  `web_not_permitted`, `web_not_needed`, `extraction_limit`), a `lines[]`
  entry of kind `limit` for `extraction_limit`, server findings (which the
  re-check never judged; `applied` did not count one that lost its overlap,
  `overlap_edit`) and the reason `research_route`. The mission built its
  record from its `critic_report` artifact (also `result_state.critic_report`),
  which stays readable through the artifacts API.
- `inqtrix.agent.activity` with `scope` `discovery` or `task` for the
  mission's searches and tasks (`knowledge.collections.list`,
  `knowledge.search`, `web.search.instant`, a mission `file_analysis` row
  with `activity_id="<task_id>:document-analysis:<document_id>"` and
  `scope="task"`), the notes `task.evidence_needs_review` and
  `task.evidence_partial` (`evidence_limit=true`; events stored before that
  flag carry `fallback=true` instead, which the Agent Desk reads as the
  evidence limit), and the kinds `memory`, `memory_candidate`,
  `critic_revise`, `critic_research`, `critic_research_exhausted`,
  `research_prospect`, `replan_analysis_rejected`,
  `replan_analysis_unchecked`, `document_version_unchecked`,
  `replan_context_incomplete`, `critic_unavailable`,
  `critic_web_quote_escalation`, `replan_invalid`, `sufficiency_skipped`,
  `section_evidence`, `section_without_preselection` and
  `section_role_unavailable`.
- `inqtrix.agent.narration` of the kinds `discovery` and `plan`, `task`
  lines per plan task (`n-task-<task_id>`, repeating the head of the
  `result_summary` its `task.finished` event carries) and `synthesis` lines
  per memo section (`n-section-<n>`).
- `inqtrix.agent.child.progress` rows a mission child projected onto a
  kernel parent: its `task.*` events (`ordinal`, `tool_kind`, `status`,
  `title`, `child_task_id`), its task searches, its narration lines, its
  decisions and its outcome summary.

The Agent Desk shows each task of a stored mission run as one text row
from these events alone; it requests neither the run's plan nor its task
rows.

## Example client flow

```bash
run_id=$(
  curl -s http://localhost:5100/v1/runs \
    -H "Content-Type: application/json" \
    -d '{"question":"Which providers meet the 2026 sovereignty requirements?"}' \
  | jq -r .run_id
)

curl -N "http://localhost:5100/v1/runs/${run_id}/events"
curl "http://localhost:5100/v1/runs/${run_id}/result"
```

For a React UI, create the run first, render the returned summary as a card,
stream `events_url`, patch the card with every `snapshot`, append visible
timeline rows only from user-facing progress and terminal/error events, then
fetch `result_url` after the terminal event. Completed runs should move to the
`completed` UI status only after the terminal event or a completed summary. The
Markdown report, `top_sources`, `references`, `top_claims`, final metrics,
`usage`, and optional Agent Desk `execution` should be attached to the same run record after
`GET /v1/runs/{run_id}/result` succeeds. The same visible timeline should remain
available from the completed report view so users can audit the agent's path
after the live run is gone.

When `INQTRIX_SERVER_API_KEY` is enabled, prefer a fetch-based SSE client because the browser `EventSource` API cannot attach an `Authorization` header. The endpoint already accepts `Authorization: Bearer ...`; the browser API is the limiting piece. Native `EventSource` is only appropriate when auth is off or a same-site proxy/cookie layer handles auth before the request reaches Inqtrix.

```js
const response = await fetch(`${baseUrl}${eventsUrl}`, {
  headers: { Authorization: `Bearer ${apiKey}` },
});

const reader = response.body
  .pipeThrough(new TextDecoderStream())
  .getReader();
let buffer = "";

for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += value;
  const frames = buffer.split("\n\n");
  buffer = frames.pop() ?? "";

  for (const frame of frames) {
    const event = frame.match(/^event: (.+)$/m)?.[1];
    const data = frame.match(/^data: (.+)$/m)?.[1];
    if (!event || !data) continue;
    handleRunEvent(event, JSON.parse(data));
  }
}
```

## Related docs

- [Web server mode](../deployment/webserver-mode.md)
- [Library API](../architecture/library-api.md)
- [Debugging runs](../observability/forensic-cookbook.md#debugging-runs)
