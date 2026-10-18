"""THE one mapping point for trace attribute names (GenAI + Langfuse).

The OTel GenAI semantic conventions are still status *Development*
(pinned here against semantic-conventions v1.41, verified 2026-09-05),
and Langfuse maps a fixed keyword set with documented priorities. Both
contracts live in THIS module only — when either evolves, this file is
the single edit point; instrumentation code never spells attribute
strings itself.

Langfuse contract essentials (verified against the Langfuse ingestion
processor, server v3.224):

* ``langfuse.*`` keys always win over generic ones. Trace-level fields
  (name/user/session/tags) are taken ONLY from the root span or from
  explicit ``langfuse.*`` keys — and Inqtrix's run span is a CHILD of
  the API request context, so user/session/name MUST use the explicit
  keys (:data:`LANGFUSE_USER_ID`, :data:`LANGFUSE_SESSION_ID`,
  :data:`LANGFUSE_TRACE_NAME`).
* The observation type is derived from ``gen_ai.operation.name``
  (``chat``/``text_completion`` → generation, ``embeddings`` →
  embedding, ``invoke_agent`` → agent, ``execute_tool`` → tool);
  spans without a GenAI operation stay plain spans.
* Content belongs in the ``gen_ai.input.messages`` /
  ``gen_ai.output.messages`` / ``gen_ai.system_instructions`` SPAN
  attributes (JSON strings), never in span events.
* Usage and cost are read ONLY from ``gen_ai.usage.*`` (or the
  ``langfuse.observation.usage_details`` JSON); any other token
  attribute is invisible to the trace totals. Spans whose operation is
  ``invoke_agent``/``agent_step`` get model, usage and cost SKIPPED —
  a run root span therefore cannot carry the run total as usage;
  totals that must be queryable go to ``langfuse.trace.metadata.*``.
* ``langfuse.observation.type`` beats the operation mapping, so a
  grounded web search — a model generation on the provider side — is
  exported as a generation with its own usage.
* Only ``langfuse.trace.metadata.*`` / ``langfuse.observation.metadata.*``
  keys become top-level (filterable) metadata; every other raw span
  attribute lands under ``metadata.attributes`` (visible, not
  filterable). The purpose of a call is mirrored into the filterable
  namespace for that reason.
* Langfuse applies NO server-side truncation or masking in the OSS
  tier — the caller-side policy in
  :mod:`inqtrix.observability.content` is the only guard.
"""

from __future__ import annotations

INQTRIX_MODEL_FAILURE_REASON = "inqtrix.model.failure_reason"
"""Stable execution failure reason on a rejected model generation."""

# --- OTel GenAI semantic conventions (Development; v1.37+ shapes) ------
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
GEN_AI_USAGE_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"
LANGFUSE_OBSERVATION_USAGE_DETAILS = "langfuse.observation.usage_details"
INQTRIX_SEARCH_TOOL_CALL_COUNT = "inqtrix.search.tool_call_count"
INQTRIX_SCORE_PREFIX = "inqtrix.score."
INQTRIX_SCORES_READY = "inqtrix.scores_ready"
INQTRIX_SCORES_TRACE_ROOT = "inqtrix.scores_trace_root"


def score_key(name: str) -> str:
    """Attribute carrying an existing algorithm-produced score."""
    return f"{INQTRIX_SCORE_PREFIX}{name}"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
"""The provider's id of one tool call: the ``invocation_id`` of its timeline
row, so a row finds its tool observation."""
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"
TOOL_TYPE_FUNCTION = "function"
UNKNOWN_TOOL_NAME = "unknown_tool"
"""Stands for a tool name the model invented (no registered tool): such a
name is model output, so it reaches a trace only as captured content."""
ERROR_TYPE = "error.type"

# Operation values (drive the Langfuse observation type).
OPERATION_CHAT = "chat"
OPERATION_TEXT_COMPLETION = "text_completion"
OPERATION_EMBEDDINGS = "embeddings"
OPERATION_INVOKE_AGENT = "invoke_agent"
OPERATION_EXECUTE_TOOL = "execute_tool"


def execute_tool_span_name(name: str) -> str:
    """Name of the tool observation of one executed tool call."""
    return f"{OPERATION_EXECUTE_TOOL} {name}"


# --- Langfuse first-class keys (highest mapping priority) --------------
LANGFUSE_TRACE_NAME = "langfuse.trace.name"
LANGFUSE_USER_ID = "langfuse.user.id"
LANGFUSE_SESSION_ID = "langfuse.session.id"
LANGFUSE_OBSERVATION_TYPE = "langfuse.observation.type"
LANGFUSE_OBSERVATION_INPUT = "langfuse.observation.input"
LANGFUSE_OBSERVATION_OUTPUT = "langfuse.observation.output"
LANGFUSE_OBSERVATION_LEVEL = "langfuse.observation.level"
LANGFUSE_OBSERVATION_STATUS_MESSAGE = "langfuse.observation.status_message"
LANGFUSE_OBSERVATION_METADATA_PREFIX = "langfuse.observation.metadata."
LANGFUSE_TRACE_METADATA_PREFIX = "langfuse.trace.metadata."
OBSERVATION_TYPE_GENERATION = "generation"
OBSERVATION_LEVEL_WARNING = "WARNING"


def observation_metadata_key(name: str) -> str:
    """Filterable per-observation metadata key (``langfuse.observation.metadata.<name>``)."""
    return f"{LANGFUSE_OBSERVATION_METADATA_PREFIX}{name}"


def trace_metadata_key(name: str) -> str:
    """Filterable trace-level metadata key (``langfuse.trace.metadata.<name>``)."""
    return f"{LANGFUSE_TRACE_METADATA_PREFIX}{name}"

# --- Inqtrix namespace (own attributes, stable across backends) --------
INQTRIX_RUN_ID = "inqtrix.run_id"
INQTRIX_TENANT = "inqtrix.tenant"
INQTRIX_WORKSPACE = "inqtrix.workspace"
INQTRIX_ATTEMPT = "inqtrix.attempt"
INQTRIX_MODE = "inqtrix.mode"
INQTRIX_STACK = "inqtrix.stack"
INQTRIX_WORK_PROFILE = "inqtrix.work_profile"
INQTRIX_REQUIREMENT_RULE_TEMPLATE_IDS = "inqtrix.requirement.rule_template_ids"
INQTRIX_REQUIREMENT_RULE_REVISIONS = "inqtrix.requirement.rule_revisions"
INQTRIX_REQUIREMENT_COMPOSED_CHARS = "inqtrix.requirement.composed_chars"
"""The submit-time result requirement on the run span: the attached
library rules as template ids with the revisions the run is frozen under
(same order), and the size of the composed requirement. Opaque ids and
counts only — a rule's label and text are user content and reach a trace
solely as captured prompt content. A plan-gate decision that replaces the
requirement of a mission is recorded on its approval, not here."""
INQTRIX_OUTCOME = "inqtrix.outcome"
# Outcome values. ``completed``, ``parked`` and ``cancelled`` mean the same on
# the run span and on a tool span. ``failed`` depends on the span: terminal
# with ERROR on a run, a visible tool failure the run survives (WARNING) on a
# tool span.
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_PARKED = "parked"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_BUDGET_EXCEEDED = "budget_exceeded"
OUTCOME_ERROR = "error"
STATUS_MESSAGE_ERROR_STATUS = "error_status"
"""Status message of a tool result whose ToolMessage status is ``error``."""
STATUS_MESSAGE_TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
INQTRIX_STREAMED = "inqtrix.streamed"
INQTRIX_INCOGNITO = "inqtrix.incognito"
"""Set on the root span of an incognito request: its spans carry no content
on purpose (metadata, usage and codes only), whatever the content policy.
Mirrored into the filterable trace metadata."""
INQTRIX_PURPOSE = "inqtrix.purpose"
"""The call site that made an LLM/search/embedding call (``agent_plan``,
``agent_synthesis``, ``agent_kernel``, ``classify``, ``claim_extract``, ``session_title``,
...). Bound through :func:`inqtrix.observability.context.bind_purpose`
at the few provider-boundary sites, read by the tracing wrappers, and
mirrored into the filterable Langfuse metadata namespace."""
INQTRIX_TASK_ID = "inqtrix.task_id"
INQTRIX_TASK_KIND = "inqtrix.task_kind"
INQTRIX_STEP = "inqtrix.step"
INQTRIX_TOOL_CAPABILITY = "inqtrix.tool.capability"
INQTRIX_TOOL_FAILURE_CODE = "inqtrix.tool.failure_code"
INQTRIX_TOOL_RESULT_UNREADABLE = "inqtrix.tool.result_unreadable"
"""Set on a tool span whose result carried no ToolMessage for its call, so
the span records no outcome instead of a guessed one."""
INQTRIX_KNOWLEDGE_COLLECTION_IDS = "inqtrix.knowledge.collection_ids"
INQTRIX_KNOWLEDGE_COLLECTION_COUNT = "inqtrix.knowledge.collection_count"
INQTRIX_KNOWLEDGE_SCOPE = "inqtrix.knowledge.scope"
"""The boundary one ``knowledge.search`` actually ran against: ``scope``
is ``pinned`` (a concrete collection set, listed as ids with their count)
or ``unbounded`` (an unscoped principal searching its whole tenant — no
ids, no count, never a misleading zero). Opaque ids, not content — set on
the capability span under the tool observation regardless of the
content-capture policy, so a trace shows WHICH collections a run searched
even when queries are not captured."""
INQTRIX_JUDGE_MARKER = "inqtrix.judge.marker"
INQTRIX_STOP_REASON = "inqtrix.stop_reason"
INQTRIX_ANSWER_CONTRACT = "inqtrix.answer_contract"
INQTRIX_EVIDENCE_CONTRACT_STATUS = "inqtrix.evidence_contract_status"
INQTRIX_ANSWER_CONTRACT_SOURCE = "inqtrix.answer_contract_source"
# UTF-8 size of the answer a run publishes (on the ``inqtrix.answer.publish`` span).
INQTRIX_ANSWER_BYTES = "inqtrix.answer.bytes"
INQTRIX_ASPECTS_SOURCE = "inqtrix.aspects_source"
INQTRIX_EVENT_MODE = "inqtrix.event_mode"
INQTRIX_EVALUATE_STATUS = "inqtrix.evaluate_status"
INQTRIX_COVERAGE_SOURCE = "inqtrix.coverage_source"
INQTRIX_SOURCE_ROLES_PREFIX = "inqtrix.source_roles."
"""Prefix of the per-node counters of source placement: ``strategy``
(placed by the configured tiering strategy), ``model`` (judged by the
extraction call), ``unjudged`` and ``conflicts``."""
INQTRIX_EVIDENCE_RENDERED_COUNT = "inqtrix.evidence.rendered_count"
INQTRIX_EVIDENCE_OMITTED_COUNT = "inqtrix.evidence.omitted_count"
INQTRIX_EVIDENCE_OMITTED_QUERY_COUNT = "inqtrix.evidence.omitted_query_count"
INQTRIX_EVIDENCE_BUDGET_CHARS = "inqtrix.evidence.budget_chars"
EVIDENCE_OMITTED_EVENT = "inqtrix.evidence.omitted"
"""Span event raised whenever the evidence overview drops report-eligible
records for budget reasons — the trace-side twin of the prompt footer."""
SPAN_AGENT_TASK = "agent.task"
SPAN_DOCUMENT_ANALYSIS = "agent.document_analysis"
"""One whole-document analysis (window sweep) inside a task span; its window
generations and reduce call nest under it."""
INQTRIX_DOCUMENT_COUNT = "inqtrix.document.count"
INQTRIX_DOCUMENT_WINDOWS_PLANNED = "inqtrix.document.windows_planned"
INQTRIX_DOCUMENT_WINDOWS_ANALYSED = "inqtrix.document.windows_analysed"
INQTRIX_DOCUMENT_QUOTES_CITED = "inqtrix.document.quotes_cited"
INQTRIX_DOCUMENT_QUOTES_REJECTED = "inqtrix.document.quotes_rejected"
INQTRIX_DOCUMENT_PAGES_WITHOUT_TEXT = "inqtrix.document.pages_without_text"
INQTRIX_DOCUMENT_EXTRACTION_UNRECORDED = "inqtrix.document.extraction_unrecorded"
"""Counts only, never content: set regardless of the content-capture policy,
so a trace shows how much of each document was analysed and cited."""
HTTP_REQUEST_METHOD = "http.request.method"
HTTP_RESPONSE_STATUS_CODE = "http.response.status_code"
HTTP_ROUTE = "http.route"
"""The matched route TEMPLATE (``/v1/runs/{run_id}``), never the raw path.
The request span is named after it, so a credential carried in a path
segment (a guest-link token) never becomes a trace name."""
URL_PATH = "url.path"
"""The request path after the shared credential sanitizer."""
EXCEPTION_MESSAGE = "exception.message"
EXCEPTION_STACKTRACE = "exception.stacktrace"
"""Overrides for the SDK's own exception event on the request span: its text
can repeat the request path or spell out a failed statement's parameters, so
both carry the logs' projection of the exception (type, stable code, frame
locations) instead."""
INQTRIX_REQUEST_ID = "inqtrix.request_id"
INQTRIX_NODE = "inqtrix.node"
INQTRIX_RAW_UNAVAILABLE = "inqtrix.raw_unavailable"
"""Set when a call path structurally cannot expose the provider's
raw payload (bare-text complete()) — so an absent raw response is
distinguishable from a provider that returned nothing."""
INQTRIX_USAGE_UNAVAILABLE = "inqtrix.usage_unavailable"
"""Set when a bare-text complete() runs without a state accumulator, the
only channel through which that path learns its token usage. Absent usage
attributes let the trace backend infer token counts from the message text,
which then disagrees with the ledger; this marker keeps the gap visible
instead of exporting a fabricated zero."""
INQTRIX_ROUND = "inqtrix.round"
INQTRIX_REASONING_EFFORT = "inqtrix.request.reasoning_effort"
INQTRIX_TOOL_CHOICE = "inqtrix.request.tool_choice"
"""The obligation a chat turn was sent with (``required`` for a role's
turns, which must end with their typed result call); absent when the
model was free to answer in text."""
INQTRIX_SCHEMA_NAME = "inqtrix.request.schema_name"
INQTRIX_RESPONSE_RAW = "inqtrix.response.raw"
INQTRIX_TOOL_CALL_COUNT = "inqtrix.response.tool_call_count"
INQTRIX_SEARCH_PROVIDER = "inqtrix.search.provider"
INQTRIX_SEARCH_ENGINE = "inqtrix.search.engine"
INQTRIX_SEARCH_QUERY = "inqtrix.search.query"
INQTRIX_SEARCH_MODE = "inqtrix.search.mode"
INQTRIX_SEARCH_RECENCY = "inqtrix.search.recency_filter"
INQTRIX_SEARCH_DOMAIN_FILTER_COUNT = "inqtrix.search.domain_filter_count"
INQTRIX_SEARCH_SOURCE_COUNT = "inqtrix.search.source_count"
INQTRIX_SEARCH_ANSWER_LENGTH = "inqtrix.search.answer_length"
INQTRIX_SEARCH_SOURCES = "inqtrix.search.sources"
INQTRIX_SEARCH_ANSWER = "inqtrix.search.answer"
INQTRIX_EMBED_TEXT_COUNT = "inqtrix.embeddings.text_count"

# Truncation visibility: every capped value adds
# this span event so accidental caps are systematically findable.
TRUNCATION_EVENT = "inqtrix.truncation"
TRUNCATION_ORIGINAL_SIZE = "original_size"
TRUNCATION_CAPPED_SIZE = "capped_size"
TRUNCATION_LIMIT_NAME = "limit_name"
