# Metrics (`/metrics`)

## Scope

This page describes the API Prometheus endpoint, the per-process worker
exporter, and the private metrics exposed by the optional editor
collaboration service. It covers scrape boundaries and bounded-cardinality
series, not alert thresholds or a complete monitoring stack.

Inqtrix can expose a Prometheus scrape endpoint for the API process. It is
**off by default** and mounts only when both are true:

- `INQTRIX_METRICS_ENABLED=true`, and
- the image was built with the optional `metrics` extra
  (`prometheus-client`). The bundled stack image
  ([`deploy/docker/Dockerfile.api`](../../deploy/docker/Dockerfile.api)) bakes
  it in; a custom build needs `--extra metrics`.

If the flag is set but the extra is missing, the server logs a WARNING and
leaves `/metrics` unmounted rather than failing to start.

## Access

When `INQTRIX_SERVER_API_KEY` is set, `/metrics` requires that same Bearer
token (constant-time compare, like the other gated routes). With no API key it
is unauthenticated — it is never exposed through the web ingress, so keep the
scrape path cluster-internal (a `NetworkPolicy`/ingress allowlist, or a
`PodMonitor`/`ServiceMonitor` scraping the pod directly).

The Helm chart wires this with one toggle:

```yaml
metrics:
  enabled: true          # sets INQTRIX_METRICS_ENABLED
  podAnnotations: true   # emits prometheus.io/{scrape,port,path} on the api pods
  workerPort: 9091       # sets INQTRIX_WORKER_METRICS_PORT + worker pod annotations (0 = off)
```

Set `podAnnotations: false` when you scrape via a `PodMonitor`/`ServiceMonitor`
CR instead of the classic annotation config.

An importable Grafana dashboard covering the HTTP, LLM/search,
runs/worker, and knowledge series lives under
[`deploy/grafana/`](../../deploy/grafana/README.md). It does not include the
collaboration-service series below, `inqtrix_run_capacity`, or
`inqtrix_stream_concurrent_viewers`.

## Series

All series are **bounded cardinality** — there are no run-id, subject, or
session labels, and HTTP paths are the matched route *template*
(`/v1/runs/{run_id}`), never the expanded id.

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `inqtrix_run_queue_depth` | gauge | — | Native runs waiting in the queue (`status=queued`). Read from the run store at scrape time. |
| `inqtrix_run_active` | gauge | — | Native runs currently executing (`status=running`). |
| `inqtrix_run_capacity` | gauge | — | In-process concurrent run capacity. Emitted **only** for the in-memory run store; with the durable (Postgres + worker) backend the worker fleet owns the slots, so this series is absent. |
| `inqtrix_run_admission_rejected_total` | counter | `reason` | Run/quota admissions rejected before entering the queue. `reason` is one of `queue_full` (global queue full), `per_user_limit` (per-user fairness cap), `quota` (monthly quota exhausted). |
| `inqtrix_http_requests_total` | counter | `method`, `handler`, `status` | HTTP requests by route template and status. |
| `inqtrix_http_request_duration_seconds` | histogram | `method`, `handler` | Request latency by route template. |

Pair the gauges (`queue_depth` vs `active`/`capacity`) with
`inqtrix_run_admission_rejected_total` to tell "saturated" apart from
"throttling one noisy tenant"; the ceiling behind each `reason` is in
[Settings and env](../configuration/settings-and-env.md#server-serversettings).

### Call metrics (API and worker)

The shared call-metric series are fed at single chokepoints — the LLM/search
provider wrappers, the retrieval pipeline, and the worker loops — so every
feature (research, kernel, knowledge, chat, editor, indexing) reports through
the same definitions. `model` is normalized through the model-card catalog
and `feature` comes from the request context, never from user input.

Two synthetic `model` values exist by design: `unknown` when a provider
reports no model id, and `other` once more than 50 DIFFERENT non-catalog
model names appear in one process. The second is the cardinality guard —
per-request model overrides are free-form strings, so without a cap one
client could mint unlimited series. It logs a WARNING when it engages;
`other` dominating a panel means either an unregistered model in regular
use (add a model card) or override traffic worth investigating.

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `inqtrix_llm_requests_total` | counter | `provider`, `model`, `operation`, `outcome` | Every LLM call. `operation` is `chat`, `text_completion` (also covers structured calls), or `embeddings`; `outcome` is one of `success`, `incomplete` (a billed response the provider stopped early, for example by its content filter or output limit), `timeout`, `cancelled`, `error`; any other value is counted as `error`, with one WARNING per process. The per-user usage ledger books an `incomplete` call as `success`, because its tokens were billed. |
| `inqtrix_llm_request_duration_seconds` | histogram | `provider`, `model`, `operation` | LLM call latency (buckets up to 600 s — the configured per-call ceiling). |
| `inqtrix_llm_tokens_total` | counter | `model`, `feature`, `token_type` | Prompt/completion tokens per feature — the aggregate twin of the per-user usage ledger. |
| `inqtrix_search_requests_total` / `inqtrix_search_request_duration_seconds` | counter / histogram | `provider`, `engine`, (`outcome`) | Web-search provider calls. |
| `inqtrix_retrieval_duration_seconds` | histogram | `step` | Knowledge retrieval stages (`hybrid_search`, `rerank`). |
| `inqtrix_run_duration_seconds` | histogram | `mode`, `outcome` | Worker execution **segments** per run mode — parked runs resume as fresh segments, so one deep run contributes several samples. Outcomes: `completed`, `failed`, `cancelled`, `parked`. Fenced-out attempts are not recorded (the winning attempt records the segment). Edge case: a park that the store resolves as an immediately-cancelled run still counts as `parked` — the segment did run up to the park attempt. |
| `inqtrix_run_queue_wait_seconds` | histogram | — | Time from enqueue to worker claim, native **runs only** and only for first deliveries — redelivered messages keep their original enqueue timestamp and would fold the prior attempt's runtime into the wait. |
| `inqtrix_stream_concurrent_viewers` | histogram | `job_kind` | Concurrent live event subscribers on ONE entity, observed each time a viewer joins (buckets 1–13). Deliberately carries no entity ids: the distribution alone answers whether multiple viewers per run occur at all — the evidence gate for a shared event poller. A histogram whose mass sits entirely at `le=1` means every viewer watches alone. |
| `inqtrix_worker_jobs_total` | counter | `loop`, `outcome` | Worker job terminations per loop (`runs`, `indexing`, `uploads`) and outcome (`terminal`, `parked`, `fenced`, `finalization_failed`); the uploads loop currently emits only `finalization_failed`. |
| `inqtrix_indexing_documents_total` | counter | `outcome` | Documents finishing an indexing pass (`completed`, `failed` — pauses/cancellations are not failures and stay uncounted). |
| `inqtrix_indexing_pauses_total` | counter | `status`, `source`, `kind` | Indexing jobs that paused (`paused_dependency`, `paused_validation`). `source` and `kind` are derived from the pause's error type into bounded sets: `contextualization`, `embedding`, `vector_store`, `quota`, `validation`, `other` and `circuit_open`, `rate_limited`, `timeout`, `unavailable`, `error`, `other`. |
| `inqtrix_indexing_auto_resumes_total` | counter | `outcome` | Automatic resumes of dependency pauses: `planned` (a pause got a due time), `resumed` (the sweep continued a job), `stopped` (a streak ended: attempts exhausted, access lost, or execution unavailable). |

### Worker exporter

Worker processes serve their **own** registry (no pushgateway): set
`INQTRIX_WORKER_METRICS_PORT` to a port >0 together with
`INQTRIX_METRICS_ENABLED=true` and each worker exposes
`http://0.0.0.0:<port>/metrics` for that process. Port `0` (default) keeps
the worker exporter off; a missing `metrics` extra or an unbindable port
logs one WARNING/ERROR and stays off — never a worker crash. Scrape every
worker replica as its own target.

Unlike the FastAPI endpoint, the worker port has **no bearer gate of its
own** — `INQTRIX_SERVER_API_KEY` does not apply here. Keep it strictly
cluster-internal (NetworkPolicy/scrape allowlist), like the collaboration
service's private endpoint below.

## Collaboration service metrics

The optional Node service always exposes Prometheus text at its private
`GET /metrics` endpoint. It is not routed through the public Python web
Service and has no bearer gate of its own, so restrict scraping to the private
service network. `INQTRIX_METRICS_ENABLED` controls the FastAPI endpoint only;
it does not mount or unmount the sidecar endpoint.

| Metric family | Meaning |
|---|---|
| `inqtrix_collaboration_active_connections`, `inqtrix_collaboration_rooms` | Current live transport and loaded-room counts. |
| `inqtrix_collaboration_document_queue_depth` | Serialized work waiting for one document/generation. |
| `inqtrix_collaboration_gate_wait_seconds`, `inqtrix_collaboration_update_validation_seconds`, `inqtrix_collaboration_update_persistence_seconds`, `inqtrix_collaboration_durable_ack_seconds`, `inqtrix_collaboration_update_total_seconds` | Latency of one client update, part by part: `gate_wait_seconds` is the wait in front of the room's serial queue, which a late writer in a burst pays and the queue-depth gauge cannot show; `update_validation_seconds` the schema and suggestion checks on a clone; `update_persistence_seconds` the internal persist request up to the database commit, including the lookup that resolves an unknown outcome; `durable_ack_seconds` the part after the commit, applying the committed update to the room and verifying it until the acknowledgement; `update_total_seconds` the whole chain the writer waits for, from entering the queue to the acknowledgement. |
| `inqtrix_collaboration_rejections_total`, `inqtrix_collaboration_websocket_rejections_total`, `inqtrix_collaboration_http_rejections_total` | Bounded rejection reasons for document, transport, and internal HTTP policy. `rejections_total` counts refused client frames by the service's own close reason they end with, without the internal API's reason a refused lease or access appends to it. `reason="suggestion_policy_violation"` counts updates the suggestion rules refused, `reason="invalid_schema"` updates the document schema, the suggestion metadata rules, or the change summary refused (the latter also logs the `change_summary_failed` warning with the room); each ends only the sender's editor, which keeps its local state as a backup. `reason="causal_gap"` counts updates that depended on changes the room did not hold; their client resends in order. `reason="restarting"` counts updates the per-document queue refused because their room had to be rebuilt from the store before they could be applied. `reason="rate_limited"` counts frames beyond the update allowance, a repeated request for the room state, client stateless messages beyond their budget, and token renewals a connection sends on its own beyond ten per ten seconds (these also log the `token_renewal_refused` warning). `reason="invalid_request"` counts refused client stateless payloads and frames of a message type no browser sends (the latter also log the `client_message_refused` warning with the type number). `websocket_rejections_total{reason="unauthorized_gateway"}` counts upgrade requests without the service bearer. |
| `inqtrix_collaboration_acknowledgements_undeliverable_total` | Durable acknowledgements for updates that were persisted and applied after their transport had begun closing, whether the client went away or the server closed it. The client receives them as duplicate acknowledgements after it reconnects. |
| `inqtrix_collaboration_persist_outcome_resolutions_total` | Persist requests with an unknown outcome, resolved by looking the update up by hash. `outcome="landed"` continued as committed. The other outcomes rebuilt the room from the store: `not_found` (the store answered and does not hold the hash), `lookup_failed` (the lookup itself failed) and `inconsistent` (the store answered with a row that is not this update). The `persist_outcome_resolved` warning names the room and the status and reason of the failed persist request and, for `lookup_failed`, of the failed lookup. |
| `inqtrix_collaboration_committed_updates_applied_by_coordinator_total` | Committed updates the coordinator applied itself because their connection turned read-only while they were persisting. |
| `inqtrix_collaboration_publication_rebase_total` | Stored AI suggestion publications that carried their base, by how the change was placed: `outcome="direct"` when nobody had changed the document since the base, `outcome="rebased"` when it was placed onto a document other writers had changed. `outcome="conflict"` counts refusals as `base_changed`, with `reason` `hunk_touched` (another writer changed the passage), `base_ambiguous` (a twin of the passage appeared next to it) or `base_too_far` (more than 200 block insertions and removals since the base, a changed block counting as one of each); the editor builds such a publication once more. A placement that would replace other content than the base held is refused as `internal_consistency` and logged as `internal_http_operation_failed`. |
| `inqtrix_collaboration_command_reconciliations_total` | Repeated server commands (a decision or an AI suggestion publication sent again under the same command id) whose committed update the room did not hold yet; the room was brought up to the store's state before the command was answered. The `command_replay_reconciled` warning names the sequence. |
| `inqtrix_collaboration_redundant_updates_acknowledged_total` | Updates without novel state whose hash no stored row carries, acknowledged at the room's durable sequence because the room already held every effect of them (another writer made the same change first, or the changes reached the store in another update). |
| `inqtrix_collaboration_formatting_cleanups_accepted_total` | Updates accepted together with the Yjs transaction that deletes formatting items their merge left redundant. |
| `inqtrix_collaboration_suggestions_removed_by_edit_total` | Pending suggestions an edit lease's update removed: the edit deleted what one proposed or left it in a form no pending suggestion can take ([what counts as removed](../architecture/editor-collaboration.md#editing-text-that-holds-suggestions)), or the resolution of the update after a concurrent merge removed one. Each is named in the update's row and written to the audit log as `editor.collaboration_suggestion.removed_by_edit`; a resent update the store already held is not counted again. |
| `inqtrix_collaboration_edit_merge_normalized_updates_total`, `inqtrix_collaboration_edit_merge_normalizations_total`, `inqtrix_collaboration_edit_merge_normalizations_declined_total`, `inqtrix_collaboration_edit_merge_normalization_seconds` | Resolution of an edit lease's update that the checks refused after its merge into the room. `normalized_updates_total` counts updates accepted with a resolution; `normalizations_total{rule}` counts what each rule changed: `inherited_mark`, `excluded_mark`, `resurrected_suggestion`, `empty_container` and `redundant_format`, and the two that remove a suggestion as a whole, `invalid_composition` and `altered_suggestion`, the last resort whose rate shows how often an edit could not keep another writer's suggestion. `normalizations_declined_total{reason}` counts refusals that stood: `no_rule`, `unsupported_suggestion`, `unsettled`, `still_refused`, `budget` (a spent read budget), and `forged_record` (a suggestion written in another person's name, logged as the `edit_merge_foreign_record_refused` warning with the room, the suggestion id and the collaboration lease id, never a person's or a session's id). Every other refusal that stood logs `edit_merge_normalization_declined` with its reason, as a warning for `budget`; an accepted resolution logs `edit_merge_normalized` with its rules. `normalization_seconds` is the time each attempt took. |
| `inqtrix_collaboration_known_suggestions_evicted_total`, `inqtrix_collaboration_edit_merge_unresolved_evicted_total` | Bounded room memory dropped oldest first: suggestions the room saw decided or removed (4,096 per room), whose outcome a copy written back by a stale view or an undo takes, and resolved rows whose persistence outcome stayed unknown (64 per room), from which a resent update is prepared. |
| `inqtrix_collaboration_view_sync_frames_answered_before_gate_total` | Sync frames of view connections whose every change the room already held, answered without waiting in the room's queue. |
| `inqtrix_collaboration_ingress_queued_frames`, `inqtrix_collaboration_ingress_queued_bytes`, `inqtrix_collaboration_ingress_deferred_frames`, `inqtrix_collaboration_ingress_deferred_discarded_total`, `inqtrix_collaboration_ingress_rejections_total` | Inbound frames per physical socket before Hocuspocus processes them. `ingress_queued_frames` counts the frames handed to Hocuspocus that hold a frame slot: every frame of a connection that is not open yet, and the document frames of an open one; `ingress_queued_bytes` also counts awareness frames and the frames waiting in the service. `ingress_deferred_frames` are document frames of open edit or suggest connections that wait because the frame limit is spent; `ingress_deferred_discarded_total` counts waiting frames dropped because their document connection or socket closed (their client resends them after it reconnects). `ingress_rejections_total` counts socket closes: `reason="rate_limited"` for send backpressure on any socket and for a frame or byte overflow of a connection that is not open or has view access, `reason="ingress_overflow"` when an open edit or suggest connection exceeds four times the frame limit or the byte limit. |
| `inqtrix_collaboration_outbound_queued_frames`, `inqtrix_collaboration_outbound_queued_bytes`, `inqtrix_collaboration_outbound_rejections_total` | Outbound frames per physical socket that are reserved but not yet written, including broadcasts held while their room applies a committed update. Each socket is bounded by `INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES`, `INQTRIX_COLLABORATION_MAX_QUEUED_BYTES`, and `INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES` of buffered socket data; a receiver that falls behind is closed with `4429` and counted as `outbound_rejections_total{reason="rate_limited"}`. |
| `inqtrix_collaboration_stateless_messages_ignored_total` | Client durability-reconcile requests, ignored because resent updates are acknowledged directly. Beyond ten per socket and ten seconds, or for any other client stateless payload, the socket closes (`rejections_total`). |
| `inqtrix_collaboration_event_loop_delay_seconds` | 99th percentile of the event-loop delay since the previous scrape. Every document of the service shares this loop, so a rising value delays every room. |
| `inqtrix_collaboration_instance_ready`, `inqtrix_collaboration_instance_epoch`, `inqtrix_collaboration_instance_renew_failures_total` | Single-writer fencing state. The failure counter counts every failed acquire or renewal attempt; a failed renewal is retried after one second and keeps the lease. Readiness becomes false one second before the active lease expires or when the database reports the instance fenced. |
| `inqtrix_collaboration_internal_api_seconds`, `inqtrix_collaboration_internal_api_errors_total`, `inqtrix_collaboration_http_request_seconds`, `inqtrix_collaboration_http_requests_total` | Node-to-FastAPI and FastAPI-to-Node operation health. |
| `inqtrix_collaboration_awareness_scratch_states_removed_total`, `inqtrix_collaboration_awareness_dropped_total`, `inqtrix_collaboration_awareness_coalesced_total` | Hocuspocus scratch-state normalization and intentionally sampled presence traffic. A drop in scratch-state removals after a dependency update is the signal to revalidate and remove the compatibility adapter. `awareness_coalesced_total` counts awareness states of an open connection replaced by a later state while an earlier one still waited to be processed. `awareness_dropped_total` counts every presence state or removal that was not applied, by `reason`: `rate_limited` (above the connection's awareness rate), `foreign_client_id` (a presence id bound to another participant in this room), `second_client_id` (a further id from a connection that already publishes one), `superseded_connection` (a state from a connection that authenticated before the current owner of the same participant's id; that connection is superseded), and `closed_connection` (a state drained after its connection left the room). Apart from `superseded_connection`, a drop never closes the connection. `foreign_client_id`, `second_client_id` and `superseded_connection` also log the `awareness_state_refused` warning, once per connection and reason. |
| `inqtrix_collaboration_connections_superseded_total` | Connections closed with code `4401` and reason `connection_superseded` because a newer connection of the same participant took over its presence id, or because it published that id after the newer connection had. Its frames still queued are drained without being applied or acknowledged; the newer connection resends what was not confirmed. |
| `inqtrix_collaboration_policy_poll_seconds`, `inqtrix_collaboration_policy_poll_errors_total`, `inqtrix_collaboration_policy_revalidations_total`, `inqtrix_collaboration_policy_revalidation_timeouts_total`, `inqtrix_collaboration_policy_resets_total`, `inqtrix_collaboration_policy_cursor` | Revocation-feed polling and connection revalidation. `policy_cursor` is the last `user_events` cursor the service has read. Alert on polling failures separately because readiness currently keys on the fencing lease. |
| `inqtrix_collaboration_lease_expirations_total` | Connections closed with `4401 invalid_lease` because their document lease expired without a renewal. The `connection_lease_expired` warning accompanies each. |
| `inqtrix_collaboration_snapshots_total`, `inqtrix_collaboration_snapshot_errors_total`, `inqtrix_collaboration_compaction_runs_total`, `inqtrix_collaboration_compaction_pruned_total` | Snapshot and retention maintenance outcomes. |
| `inqtrix_collaboration_snapshot_fallbacks_total` | Stored snapshots rejected while a room was rebuilt from the store (a room load or a command reconciliation), because the snapshot's state did not match its stored hash and state vector, the update tail replayed on top of it did not verify, or the resulting document failed validation; the rebuild then tried the older retained snapshot with its longer tail. The `snapshot_candidate_rejected` warning names the candidate and the reason. |
| `inqtrix_collaboration_snapshot_seconds` | Time from capturing a room for a snapshot until the store confirmed the stored snapshot. |

No series labels a user, room, document, token, comment, or update body. Use
document metadata (`persisted_sequence` and `projection_sequence`) for targeted
projection-lag diagnosis instead of adding document IDs to Prometheus.

## Readiness vs. liveness

`/metrics` is for scraping, not health checks. Kubernetes probes use:

- `/health` — liveness/startup (process up; unauthenticated).
- `/readyz` — readiness: the database contract probe and the queue `PING`
  must pass (503 when either is down); a dead vector store degrades to
  `200` with a `degraded` body, since research/chat/files still work. The
  body's `database_contract` field and the full gate semantics are in
  [Web server mode](../deployment/webserver-mode.md).

The collaboration process has separate private probes: `/health/live` means
the Node process is running, while `/health/ready` additionally requires its
active single-writer fencing lease.

## Related docs

- [Deploy editor collaboration](../deployment/editor-collaboration.md)
- [Logging](logging.md)
- [Security model](../deployment/security-model.md)
- [Runbooks](../deployment/runbooks.md)
