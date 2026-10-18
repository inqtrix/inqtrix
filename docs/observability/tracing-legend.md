# Tracing legend

This index connects observation types, content boundaries and the detailed
event catalogs. Attribute definitions live in
[`semconv.py`](../../src/inqtrix/observability/semconv.py); recording and content
handling live in [`otel.py`](../../src/inqtrix/observability/otel.py),
[`provider_tracing.py`](../../src/inqtrix/observability/provider_tracing.py) and
[`content.py`](../../src/inqtrix/observability/content.py).

## Observation levels

| Step | Langfuse observation | Contract |
|---|---|---|
| Native request, such as `POST /v1/runs` | Span | Request boundary and parent trace context. |
| Native request: name, user and executor work | Span | Named `{method} {http.route}` from the matched route template, or `{method}` alone when no route matched; the raw path never names it, because a path segment can be a bearer credential (a guest-link token) and the name becomes the trace name. `url.path` passes the shared credential sanitizer; a recorded exception keeps what an application log line keeps (type, stable code, frame locations), never its text, which can carry a guest-link token, a failed statement's parameters or a provider's answer. Carries the pseudonymous `langfuse.user.id` once the principal is resolved, the same reference as the log line's `user`. Work handed to an executor thread (editor assist, chat, text improvement, session title) runs as a child of this span, so the access log's `trace_id` leads to the whole call; under a sample rate below 1.0 that work is sampled with its request. |
| `inqtrix.run` execution segment | Agent | Run identity, mode and execution attempt; resumes can produce multiple segments in one trace. |
| `inqtrix.answer.publish` | Span | Publication of a finished answer: `inqtrix.answer.started`, byte-bounded `inqtrix.output_text.delta` events of at most 2 KB and `inqtrix.answer.ready`; `inqtrix.answer.bytes` is the answer's UTF-8 size. For an agent answer it includes the publisher's artifact writes. |
| `inqtrix.run.complete` | Span | The terminal write of a successful run segment: result payload and snapshot. |
| Research `classify`, `plan`, `search`, `evaluate`, `answer` | Span | Node/round, numeric snapshots and the decision/status attributes exported by `graph._set_node_span_attributes`. |
| Provider `chat` or `text_completion` | Generation | Purpose, effective model/effort, the forced tool choice of a role's turn (`inqtrix.request.tool_choice`, absent when the model may answer in text), usage, finish reason and the provider-call boundary. A response the provider stopped before it was finished (finish reason `content_filter`, `length`, `max_tokens` and the like) has level WARNING and a status message naming the finish reason. |
| Kernel tool call `execute_tool <name>` | Tool | One per call the kernel's tool node executes, per execution segment, keyed by `gen_ai.tool.call.id` (the timeline row's `invocation_id`); outcome in `inqtrix.outcome`. See [Tool spans](#tool-spans). |
| `inqtrix.session_title` | Span | Optional short-title work alongside a conversation. Its generation has purpose `session_title`, the dedicated Fast model and actual usage. The request records the final outcome after the title revision check and returns an optional `trace_id`. It is not a reasoning step of the answer. A timed-out HTTP waiter does not erase a provider completion or its quota booking. |
| Grounded `web_search` | Generation | Standard input/output show the actual query and provider answer. Source metadata retains provider provenance and annotation offsets. The search engine is the model identity; provider-reported search tokens contribute to trace usage. Missing prices are not zero-cost evidence. |
| Detailed lineage such as `claim_merge` or `evidence_verification_projection` | Forensic event | Full authorized audit payload and a content-minimized span-event projection; it is not an extra provider call. |

Tool requests and replies are part of the provider conversation. A tool named
in a message is not by itself another model call. In a kernel run, every call
the tool node executes has one tool observation per execution segment; a call
answered before dispatch (a rejected approval, a guarded or over-limit batch, a
role's typed result) has none — [Tool spans](#tool-spans) lists these cases.
Read the actual messages and observation hierarchy when following a
parent/child handover. A parent and its child can share one trace; sum each
generation only once.

Kernel tool-start events carry a display preview, not a second copy of the tool
arguments. For registered evidence selections the preview names the known source
or stored search question from the run's existing registry. It performs no new
retrieval and does not claim that the source was verified. Raw arguments remain in
the protected conversation/audit path. This separation keeps normal activity lines
readable without replacing the data used for forensic inspection.

Kernel context compaction emits one `inqtrix.agent.context.compacted` event
and its existing narration after the replacement model call succeeds. Sync and
async summary hooks use the same run-bound archive and task state. The summary
generation goes through the provider's existing content capture and retry
authority; middleware must not add an independent retry budget. Failure or
cancellation propagates without a successful compaction event.

Captured-text archives share the existing run-artifact lifecycle. One logical
source may contain several immutable storage parts; publishing those parts adds
no per-part event or connection. The read_canvas tool captures its selected original
offsets, fingerprint, fidelity and continuation in the existing tool conversation.
A bounded window is explicitly partial, never a claim that the full source was
read. Archive JSON preserves the decoded text while escaping boundary delimiters.
Legacy rendered archives remain labelled as such; they cannot attest fidelity to
their original input. Content still passes through the shared capture/redaction
path. Storage part sizes and read windows are not telemetry truncation limits.
Archive tool-start previews name the read or catalogue continuation in the
answer language, without identifiers or fingerprints. A displayed character
position is one-based; raw tool offsets remain zero-based in content capture.
Producing the preview performs no source read and does not claim success.
Knowledge and editor document reads preview the same way: the start of the
document, a one-based character position, or a further comment page; document
ids, versions and fingerprints stay in the captured tool arguments.

Model recovery stays inside the existing kernel model boundary. The run event
`inqtrix.agent.model.recovery` records reason, spent/shared attempts and archive
count; its narration explains the correction in the answer language. It creates
no tool execution or separate provider connection. Only actual provider calls
produce generation observations. Rejected generations carry
`inqtrix.model.failure_reason`, the original input, captured response when
available, and any reported token usage. Invalid tool JSON remains unchanged in
protected content capture and checkpointed correction feedback. Its feedback is
not a tool-completion event. Transport retries and model corrections consume one
per-invocation attempt/deadline budget; independent summary operations retain
their own provider budget. Archive failure prevents context replacement.
Retry logs and activity events use the same shared attempt coordinates. The
kernel binds its existing run-cancellation signal to provider retry waits.
`agent_context_projection` identifies the generation that reduces a complete
tool result after a context rejection or a conservative input-budget check. It uses the same model and the shared
remaining budget; this purpose distinguishes projection work from the answer
without inventing another tool execution. The checkpoint's kernel usage
aggregate includes these and proactive summary calls, so resuming a run cannot
discard their known spend. Message-level receipts remain the input for graphs
without the kernel aggregate.

Conversation-source tools use the kernel tool observation and activity channel;
the observation's output is the tool's reply text:
`search_context` captures the literal query, metadata snapshot, scanned ranges,
unavailable sources and continuation; `read_context` captures the selected
version and original range. A supplied cursor never proves earlier reading.
Search responses include exact `next_search` arguments. Published conversation
content is scanned before execution archives; recorded ranges, rather than
catalogue order or a no-match result, show what was actually inspected.
Neither operation creates a provider generation or a per-source event stream.
Knowledge and editor document reads use the same existing tool observations.
Their captured results name the representation, version, content fingerprint,
Unicode range, total characters and exact continuation arguments. Editor search
reports the scanned suffix and remaining matches, even when no match is returned;
comment pagination has its own continuation. These are product transfer windows,
not telemetry cuts. Content remains on the shared redaction/capture path, and
windowing creates no independent provider call, event stream or connection.
Every windowed reader also attaches a structured coverage record to its tool
message artifact (never to relayed text). The kernel folds these records into
a run-wide source coverage ledger, rebuilt from the checkpoint at every segment
start, and publishes it as `execution.source_coverage` in the run snapshot that
`tool.finished`, `phase.changed` and the run resource carry: per source version
the merged read ranges, scanned ranges, unread remainder, `complete` and
`continuation_open`. The advisory sufficiency judge receives the same ledger as
"Sources under reading"; its event `inqtrix.agent.sufficiency.judged` adds
`open_continuations` and `open_sources`. While a source the run started reading
has an unread remainder or an open search, catalogue or comment page, a
`covered` verdict names the remainder instead of ordering the model to stop.
Every message the harness itself places into the transcript (sufficiency
advice, post-mission advice, resolved skill inputs) begins with the harness-note
prefix, so a captured model input distinguishes runtime advice from user text.
An internal role run (a run with a work profile, such as the Deep review) is a
child run of the same kernel: its trace nests under the parent run like a
delegated research child and its run span carries `inqtrix.work_profile`. Its
model resolves under the role's own routing node and every model turn carries
that node as `inqtrix.purpose` (`agent_deep_review` for the review), so purpose
filters and the usage ledger find it. Its first captured model input is the
rendered typed handover ("ROLE ASSIGNMENT"). Its tool observations are the
ordinary windowed readers, and the advisory sufficiency judge (`kernel.judge`,
purpose `agent_sufficiency`) observes them as in every kernel run; the typed
result appears as one tool call named after the result model and executes
nothing. `execution.work_profile` names the role in every snapshot
of that run, and the stored result carries `work_result` (profile, status,
value, and `read_before_result` — the sources read before the result was
written) instead of an answer. On the parent, the role is one tool row
(`tool.started`/`tool.finished`, tool `review_role`) whose invocation id is the
role's origin key (`role-review`; a mission appends its review round,
`role-review:2`), and the run's outcome record carries how many cited
references the review had re-read (`reach`) and which points it could not
confirm (`lines`).
A memo section too large for one call is written the same way: a role run with
profile `section_writer` under the mission, one tool row (tool `section_role`)
whose invocation id is `section:<round>:<index>`, its model turns under purpose
`agent_synthesis` like the single call it replaces, and its reads of the
mission's evidence as `read_evidence` observations.
A mission's review takes the role only when its material exceeds the
single-call budget. Otherwise it is ONE generation with purpose
`agent_deep_review` inside the mission's own trace: the same rendered handover,
with the memo verbatim and the run's stored evidence behind it — every stored
text once, each source with its `basis_id`. The revision is a second generation
under the writer's node (`agent_synthesis`, or the answer node for a chat
deliverable) whose input carries only the bases the findings name. When the
revision applied an edit of a model finding, one more generation re-checks
those edits under purpose `agent_deep_review`, in the run's own trace in
both engines, also after a review by role. There is none when the revision
applied only server findings' edits, when every applied model edit is left
unchecked before the call (no segment, more than 12 segments, or new text
resting only on sources without stored source text), when the
run has no review call, or when the re-check does not fit its model's
context; each of the last three logs a warning. When the re-check holds at
least one edit, a second generation under the same purpose sends the same
input byte for byte and confirms it: an edit holds only where both do, so a
model answer that flips on the same request verifies nothing; there is no
second one when the first holds none. A run whose review was one call and
whose revision was re-checked therefore shows two or three generations under
that purpose, told apart by their `schema_name` (`DeepReviewVerdict` for the
review, `RevisionCheckVerdict` for the re-check and its confirmation). The
re-check's input carries each finding, the text its edit replaced, the
replacement, the segments the server lists for it, where it stands, and the
texts of the labels the replacement cites; its output is one row per segment
and a reason for the trace only. The server's decision per edit is not a
generation: an edit the re-check rejects is a warning naming the edit and its
code; every other edit left unchecked is counted per code in one warning that
names no edit, an edit the confirmation did not hold as `unconfirmed` with the
confirmation's code after a colon (none when the confirmation failed or
answered nothing usable).
The same producing readers attach `material_dependencies` to their tool
artifact: the knowledge documents (windows and hits) and editor documents (with
transferred comment ids) the result carried. The private transcript descriptor
records them as `materials`; a later `search_context`/`read_context` of that
transcript re-authorizes every material against the reader's current rights and
the source catalogue names a closed transcript with the reason
`material_unavailable`. Reading a document does not lock a transcript; raw
file and image inputs do.
Private checkpoint, compaction and recovery snapshots use the existing archive
storage, but are excluded from product artifact lists, exports and shared-result
reads. They preserve structured messages, tool calls and invalid arguments;
`checkpoint_messages` describes their coverage, not hidden provider reasoning or
unrecorded external files. Only server-owned checkpoint metadata supplies
trusted access receipts; historical copies read as source data confer no
authority. Receipt revalidation is an authorization check, not a new research step. Trace content still flows only through the shared
content policy; archive retention does not enable forensic capture implicitly.
Credential redaction of product search evidence is independent of telemetry
limits: it preserves complete answer/snippet text, valid annotation offsets and
usage metadata. Its raw provider envelope stays exclusively on the protected
trace path. Capture backstops must not shorten the product evidence beforehand.
The `context_budget` recovery reason denotes preflight projection; `context_window`
denotes a provider rejection. A projection whose own input cannot fit produces
no generation or retry charge. A failed archive write retains the checkpoint and
reports the failure instead of claiming a successful completed transcript.
A failed execution-cache purge warns with its error type and keeps the native
run anchor available for deletion retry. A deferred retention purge is not
reported as completed deletion; no additional provider observation is created.

Authentication rejection logs use English diagnostic templates and existing
domain-separated pseudonyms. Personal-access-token identifiers use the `res`
namespace even for unknown tokens; raw identifiers and secrets do not enter
these warnings. Public rejection responses retain their uniform status and
error type. This changes log content, without adding observations or model calls.
Object-cleanup warnings likewise use existing `ten` and `res` references for
tenant ids and object keys; their lookup and deletion semantics are unchanged.

Kernel tool completions retain their originating tool identity by invocation ID,
including middleware rejections whose reply omits the name. The same mapping is
restored from checkpointed calls when execution resumes. A client can settle an
older anonymous completion against its existing activity row; an anonymous
orphan does not establish that a search occurred. This uses the existing run
events and conversation capture, without adding observations or model calls.
Likewise, a completed turn alone does not establish that a quality review ran;
the completion recap reads it from the run's outcome record only, never from a
phase, a station or an activity.

User artifact edits and renames remain persisted and audited after a run ends.
Their notification reads the run through the caller's scope and sends a run
event only while the run is still live; completed-run views reconcile from REST.
The run store keeps its atomic terminal guard, including a race in which the
run finishes between that read and emission. Unexpected drops and notification
failures remain visible; successful late document edits do not routinely warn
about an attempt to append to a closed run log.

Provider input/output totals include cache reads, cache writes and reasoning
tokens. Native Anthropic and Bedrock adapters add their separately reported
cache buckets to input; OpenAI-compatible totals already include them. Optional
details distinguish a reported zero from an unavailable count. Quotas and the
usage ledger retain these inclusive totals; the generation carries the detail.
No tokenization or additional model call is involved.
Providers declare `complete_uses_metadata` only when their text method delegates
to that same metadata call. The tracing wrapper retains its receipt before
returning text; configuration wrappers forward the declaration. Other custom
providers retain their own text method and accumulator-based usage accounting.

Generation spans retain the inclusive `gen_ai.usage.input_tokens` and
`gen_ai.usage.output_tokens` and optional cache/reasoning attributes. Their
`langfuse.observation.usage_details` projection uses disjoint buckets so a
receiver cannot count reasoning twice: `input` excludes recorded cache buckets,
`output` excludes recorded reasoning, and `total` is the unchanged sum. Missing
details remain absent. Invalid or inconsistent details are omitted with a
warning; they never change the provider's total.

Grounded-search token usage alone does not establish complete cost. A hosted
search agent can incur separate web-tool transaction charges. An application
search request, an internal tool call, and a billable transaction are different
measurements; never derive the latter two from citation or request counts.
Unpriced model tokens and unavailable tool charges must be named in cost reports.
When a hosted search provider reports its underlying model, the generation and
usage ledger use that model; the configured agent identity stays in
`inqtrix.search.engine`. `inqtrix.search.tool_call_count` counts reported web-tool
calls, not billable units. With content capture enabled, the complete available
provider response uses the same `inqtrix.response.raw` redaction and byte backstop
as LLM responses. A missing response is marked unavailable. The protected raw
envelope is not copied into evidence prose or the user-facing search ledger.

Evidence reads reuse retained provider answers and source passages. Their tool
request/reply carries the selected reference or query and the available mapping
precision; reading retained material is not another search generation. Missing
retained material is reported as unavailable, not as an empty successful read.
The retained context can come from a Research result or an Agent evidence
artifact. Its original query and source-run identity remain intact in the
receiving prompt; this is material reuse, not a new provider invocation.
The session context also indexes run-scoped evidence from earlier visible,
completed chat turns. Metadata-index failures warn without discarding the
remaining conversation history; they do not create a search generation.
Imported report and child-result catalogs retain all registered label/identifier
pairs even when the body is offloaded. A declared complete catalog cannot be a
short preview. Internal identifiers in generated prose trigger the existing
tool-validation or bounded citation-repair path; raw messages show the rejected
text and correction. Normal clean output causes no additional model call.
Warnings report counts rather than copying internal identifiers into log lines.
Explicit evidence reads expose bounded, version-checked windows instead of
another archive digest. The captured tool conversation states the selection,
character range, total size and any continuation; the data portion is fenced.
A partial window is not a fully read document. Metadata pointers refer to named
material sections within the same selection. Judge transfer completeness from
these ranges and the actual messages, never from the number of tool calls.
Mission outline, section, review and revision generations also receive this retained
context through the shared evidence projection. Their captured prompts show
selected citation labels, source passages and each matching provider answer
once. A provider answer is explicitly distinct from an original source quote.
No separate retrieval span or search generation is invented for this local
projection; the ordinary content policy still controls prompt capture.
The mission review's captured input includes its actual assignment, the numbered
success criteria and, as run diagnostics with a `basis_id` and their method,
the persisted task states, source-operation counts with discovery
distinguished, the declared lexical Markdown word count, the known
contradictions and the concrete unverified quote passages. These measurements
allow execution and length findings to be checked instead of trusting a
model's count. They add context to the existing generation, not another model
call or a new source-verification score.
A review without a usable verdict, or one that contradicts itself, emits
`critic_unavailable` with its `reason` through the existing activity channel.
Its raw generation and usage remain recorded; no passing score or automatic
corrective generation is manufactured from it.
This run-scoped decision also reaches the parent of a delegated mission. The
completed-run recap names an unavailable review with the outcome record's
summary instead of inferring a usable check from having visited the critic
phase.
Final review distinguishes source material from run diagnostics and output
requirements. Findings identify their output and exact text anchor; revision
results describe applied or refused changes against the reviewed version. These
details explain a correction without treating a citation link as semantic proof.

Mission evidence processing records `sufficiency_skipped` through the existing
`inqtrix.agent.activity` channel when no failed or insufficient task outcome can
make the preliminary coverage judgement affect replanning. The reason
`no_unresolved_outcome` describes task status, not verified evidence coverage;
no generation or synthetic `covered` verdict is recorded for this branch.
The finished output still receives its normal content review.

Claim extraction presents one source card containing provider reference ids.
Its `citation_prompt_count` counts input citation URL entries represented by
that card; `citation_omitted_count` counts the remainder. A URL kept solely for
server-side binding validation is not reported as shown to the model.

On cooperative research cancellation, terminal usage and run-end diagnostics
use the interrupted node's current state, including usage already recorded
before the interruption. A missing final graph return must not reset those
values to the initial input. The state travels internally with the cancellation
exception; it is not copied wholesale into span attributes or used to derive
usage from telemetry. Provider-generation observations keep their own counts.

Search input/output and the existing `inqtrix.search.query/answer` attributes
use one redacted, bounded text value. Native fields encode that value as a
JSON string so literal text such as `null` remains text after decoding.
The encoded byte size determines the shared cap; a truncation event names
the native field that reached it. The additional display fields do not
add generations or change provider-reported token counts.

Captured messages and tool arguments share credential redaction with audit
logging, but do not inherit its short field previews. The content policy's
serialized byte backstop owns trace truncation and reports it. This keeps
ordinary-length queries, titles and tool text reconstructable in the trace.
Direct search calls are attributed to `web_instant`; enclosing task identity
is retained through the existing purpose context.

Model-resolution records distinguish the requested run tier from the effective
node tier. A high/mid run request can therefore remain visible while a fast node
still uses its assigned model and effort. Explicit per-node model pins keep their
precedence. Child research resolution comes from server configuration rather than
a planner-selected task tier; profile and source-policy inheritance are separate.
Mission critique and kernel final review resolve the selected model/tier/effort
through those same records. Mission children retain that selection in their
existing request overrides and emit their own node resolutions. The kernel's
automatic Deep effort increase is local to its loop and revision, not a selected
value to copy into final review or mission children. A generation's effective
model and effort must agree with its role's resolution; these records add no
provider call and require no content capture to make the routing visible.

Search sources retain `url`, `title`, `snippet`, `date`, `last_updated`,
`rank`, `origin` and `spans`, one `{start, end}` per place of the provider
answer that cites the source. Missing provider values remain missing; a
source without offsets has no spans. Span offsets refer to the original
provider answer before trace redaction or truncation, never to the linked
webpage.
They must not be treated as positions in an altered display string.
An empty per-source snippet does not mean the trace lost a webpage:
some search providers return a synthesis and citations without page bodies.

## Result requirement

A run started with a result requirement (attached library rules and typed
text, composed at submission) names it on every `inqtrix.run` segment span:
`inqtrix.requirement.rule_template_ids` and
`inqtrix.requirement.rule_revisions` list the distinct attached rules in
attachment order with the revision the run is frozen under (absent when only
text was typed), and `inqtrix.requirement.composed_chars` is the size of the
composed requirement. These are ids and counts, recorded under every content
policy. A rule's label and text are user content: they reach a trace only as
captured prompt content, in the first kernel user message or the mission's
writing prompts. The attributes describe the submitted requirement, which
the single mission a kernel run starts with `run_deep_mission` inherits, so
that child's segments carry the same ids; a plan or replan decision that
replaces a mission's requirement is recorded on its approval, not on the
span. The run event `inqtrix.agent.requirement.attached` is the transcript's
record of the same requirement and is no trace observation.

This category exists because a requirement shapes every writing prompt of a
run while it was visible only inside captured content: without the ids, a
trace recorded without content capture could not say which rules, at which
revision, a run worked under.

## Claim assessment and selection

`consolidated_claim_count`, verification counts and quality signals describe
the complete computed assessment. The `evidence_selection` forensic event and
the `claims` group of score snapshots separately expose
`materialized_claim_count`, the bounded selection for compact lists. This
distinction is necessary because presentation limits must not change the
assessment shown for the same evidence.

`claim_merge` records the complete assessment, and
`evidence_verification_projection` reports its projection onto evidence
records. The node snapshot and final result counters use that same population.
Neither a higher count nor a `verified` label proves semantic correctness of
the generated answer.

## Run scores

With `INQTRIX_TRACE_SCORES=true`, a configured Langfuse OTLP sink receives scores
after successful run-result publication. Existing Research assessments, Mission
Critic verdicts and Deep review counts/booleans are copied, never re-evaluated.
Unmeasured values are omitted. These are algorithm-produced signals, not
independent evidence of semantic correctness; a reviewer can be wrong.

Root-run scores attach to the trace. Child-run scores attach to that child's
execution observation so they do not replace the parent's assessment. Score IDs
derive from trace, execution span and score name; repeating an export updates the
same score. Parked, cancelled and failed segments do not publish final scores.
Deep fields describe the reviewer's submitted judgement, before applying edits.

The existing OpenTelemetry batch/flush lifecycle sends scores off the run thread.
Its bounded queue is a backstop, with SDK diagnostics routed into application
logs. Score failures warn and do not affect run completion; there is no durable
outbox or guarantee of delivery after a hard process exit. A non-Langfuse target
or missing credentials is reported at setup. Sampling governs spans and scores
together. File/local/off modes do not submit scores to a remote service.

## Content and failure boundaries

Content capture and destination are independent configuration choices. Prompts,
provider responses and tool payloads pass through the content handling path;
ordinary operational logging stays content-minimized. Truncation, omitted
records, sink failures and unavailable recording must be visible rather than
silently interpreted as empty evidence.

An exception that escapes a span is recorded the way an application log line
records it: one exception event and ERROR status with its type, a stable code
and the frame locations, never its text. Exception text can carry a request
path with a guest-link token, a failed statement's parameters (document text,
e-mail addresses), a provider's answer or a tool's arguments. This holds for
every span Inqtrix opens, whatever the content policy; with content capture
enabled, a failed provider call's answer is recorded through the content
handling path instead (`inqtrix.response.raw`). As with the OpenTelemetry SDK's
own recording, only an `Exception` is recorded: a `KeyboardInterrupt` or a
closed generator ends the span without a record. A tool span classifies its
exceptions itself (see [Tool spans](#tool-spans)).

An incognito chat (`"incognito": true` on `/v1/chat/completions`) or
incognito knowledge ask (the same flag on `POST /v1/runs`) records no content
in any of its spans, whatever the content policy, also in the threads its
work is handed to: no prompt, answer, raw response or tool payload. Model,
token usage, duration and codes are still recorded, so usage is still counted.
Its `inqtrix.chat` or `inqtrix.run` span carries `inqtrix.incognito` and the
filterable trace metadata `incognito`, so a trace without content says why. A
worker reads the flag from the persisted request, so every segment of the run
withholds its content.

An unresolved answer-language warning reports the resolution state, without
copying any portion of the user's question into ordinary logs. The question
belongs to the protected generation input when content capture is enabled.

A generation's recorded system instructions are the ones the model received.
The provider layer appends the untrusted-content rule before the tracing layer
records the call, so every generation shows it — also one whose caller passed
no system prompt, where the rule is the whole instruction. A generation without
the rule is a call that did not pass the provider layer.

Use the protected audit and raw generation messages for content inspection.
The forensic span-event projection intentionally omits source prose. Its
field allowlists in `runtime_logging.py` are authoritative for that projection.

## Tool spans

Every call the kernel's tool node executes — conversational kernel and role
runs alike, the conversational kernel's built-in `write_todos` included — opens one `execute_tool <name>`
span, the Langfuse tool observation of that call. The kernel's tool-call
middleware opens it as its first middleware, so it encloses every layer that
can change a tool result; approval edits happen before dispatch, so it shows
the arguments that actually ran. It carries `gen_ai.tool.name`,
`gen_ai.tool.type` (`function`) and `gen_ai.tool.call.id`, which equals the
`invocation_id` of the call's `tool.started`/`tool.finished` rows: join a row
to its observation by that id. A name the model invented (no registered tool)
is model output, so the span name and `gen_ai.tool.name` say `unknown_tool`
and the invented name appears only in captured input.

With content capture on, the observation's input is the executed call's
arguments (for an invented name: the name and the arguments) and its output
is the ToolMessage text, both on the shared redaction and byte-cap path. Text
is written as a JSON string; arguments as their JSON object, or, when the cap
cuts them, as the JSON string of the cut rendering, with the
`inqtrix.truncation` event naming the attribute. A tool artifact is never
read. Without content capture the span carries identity and outcome only.

`inqtrix.outcome` uses the same key on the run span and on a tool span; the
words mean:

| Value | `execute_tool <name>` | `inqtrix.run` |
|---|---|---|
| `completed` | The call returned; no failure signal. | The segment finished the run. |
| `failed` | A visible failure the run survives: the reply starts with a tool failure prefix (`Tool unavailable:`, `Tool error`, `Tool blocked:`) or its ToolMessage status is `error` (an excluded built-in, an invented name, arguments that failed the tool schema, or a context tool that reported its sources unavailable). Level WARNING; `langfuse.observation.status_message` is the prefix or `error_status`, never the reply text. | Terminal, with ERROR status. |
| `parked` | The call interrupted (a question to the user, a child run, a skill input); the resumed execution is a new span with the same call id. | The segment parked. |
| `cancelled` | A cancellation raised during the call. | The run was cancelled. |
| `budget_exceeded` | The token budget stopped the run during the call. Level WARNING, status message `token_budget_exceeded`. | Not used; a budget stop is `failed`. |
| `error` | Any other exception: ERROR status with the exception's descriptor, `error.type` with its class name, and one projected exception event (type, stable code, frame locations, never its text). | Not used. |

A span without `inqtrix.outcome` is one of four cases, each stated:
`inqtrix.tool.result_unreadable=true` when the result carried no ToolMessage
for the call (one WARNING per process); a graph bubble-up other than an
interrupt, which passes unchanged (one WARNING per process); a
`BaseException` such as `KeyboardInterrupt`, which ends the span without a
classification; or a telemetry failure while classifying, which logs the
telemetry WARNING below. A telemetry failure while recording never fails the
call; it logs one WARNING per process. Opening the span (the shared operation-span
helper and its identity attributes) lies outside that guard and behaves like
every other operation span.

A failure text without one of the prefixes reads as `completed` (for example
"Knowledge store unavailable …", "Assignment N: Tool blocked: …" in a batch,
"Subtask not scheduled …" and the refusals of `write_canvas`,
`read_research_report` and `load_skill`). The span reads the same prefix
constant as the checkpoint recount of source-tool uses, which has the same
blind spot.

An interrupting call has more than one span with one call id: every
execution but the last ends `parked`. A `parked` span with no later span of
the same call id means the run ended while it waited (cancelled, expired or
revoked); "exactly one terminal span per row" holds for resumed runs only. A
sibling call of the same model turn executes once. A child run submitted from
a tool body carries the context of the tool span that submitted it, so every
segment of the child nests under that first, `parked` span; the resumed
execution, which finds the child instead of submitting another, sits under
the parent's next segment. Only kernel runs have these spans: a delegated
research or mission child shows its own engine's spans, and a role run's
tool spans sit under the role's own `inqtrix.run`.

These rows never pass the tool node and have no tool span: an approval
rejection, a child-batch guard answer, a rejected parallel `write_todos`, a
patched dangling call, a role's typed result call, a batch the tool-call
limit stopped before dispatch, and the `review_role`/`section_role`
delegation rows (the role has its own run span). Invalid provider arguments
produce neither row nor span.

Inside the tool observation, a capability call opens a plain span named
after the capability (`inqtrix.tool.capability`; a typed failure adds
`inqtrix.tool.failure_code`). It carries no arguments and no GenAI operation,
so it is not a second tool observation, and an exception crossing it is
recorded only on the tool span. The searched knowledge boundary is not
content and always rides the `knowledge.search` capability span:
`inqtrix.knowledge.scope` (`pinned` with `inqtrix.knowledge.collection_ids`
and `inqtrix.knowledge.collection_count`, or `unbounded` for an unscoped
principal searching its whole tenant — no ids, no count).

## Document analysis spans

A mission `file_analysis` task opens one `agent.document_analysis` span under
its `agent.task` span. Its window generations (`WindowExtraction`) and the
reduce generation (`DocumentAnalysisResult`) nest under it, all under the
`agent_file_analysis` node: one generation per window, so a 32-window
document shows 32 extraction generations whose inputs each hold exactly one
fenced window. The span carries counts only, never content, regardless of the
content-capture policy: `inqtrix.document.count`,
`inqtrix.document.windows_planned`, `inqtrix.document.windows_analysed`,
`inqtrix.document.quotes_cited`, `inqtrix.document.quotes_rejected`,
`inqtrix.document.pages_without_text` and
`inqtrix.document.extraction_unrecorded`. Planned above analysed means a
window budget, a failed extraction or a changed document left ranges
unread; the task result names each range with its reason.

## Collaboration service

The collaboration service records no Langfuse observations. Its operational
signals are the Prometheus families in
[metrics](metrics.md#collaboration-service-metrics) and structured log lines;
the durable record of a pending suggestion that an edit removed is one audit
row per suggestion, `editor.collaboration_suggestion.removed_by_edit`, with the
suggestion, patch, author and actor ids and no document content. When the
service resolves an edit lease's update after a concurrent merge, it counts
each rule in `inqtrix_collaboration_edit_merge_normalizations_total{rule}` and
logs `edit_merge_normalized` with the rules; a refusal that stands is counted
by reason and logged as `edit_merge_normalization_declined`, as a warning when
the read budget was spent. An update that writes a suggestion in another
person's name is the warning `edit_merge_foreign_record_refused`, which names
the room, the suggestion id and the collaboration lease id: the lease leads to
the writer, while a person's id or a session id never stands in the line (a
member's session id is the value of the session cookie).

## Change checklist

- Place a step at its actual execution level; reuse an existing observation
  when changing only the data it records.
- Define attribute names through `semconv.py` and keep source fields and
  derived counters explicit.
- Route captured content through `content.py`; keep identities and secrets
  out of ordinary logs and metric labels.
- Report recording failures and meaningful truncation. Keep metadata needed
  to interpret a result outside bulk-content truncation.
- Verify the raw messages, hierarchy, counters and rendered outcome together.
- Update this legend and the relevant detailed catalog when the contract
  changes.

The [iteration-log catalog](iteration-log.md#forensic-events) defines the
forensic event meanings. The [forensic cookbook](forensic-cookbook.md) covers
trace inspection and score snapshots; [logging](logging.md) and
[metrics](metrics.md) describe their respective destination contracts.
