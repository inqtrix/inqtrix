"""Which places hand text to a person, and whose words each one carries.

A reader surface is a place where a run writes text a human then reads:
a narration line, an artifact row, a plan card, a task result, a
clarification round, the answer itself. Nothing in the code said which
places those were. So each one was guarded when somebody happened to
notice it, the ones nobody noticed carried model text unguarded, and no
test could tell the two apart — the question "is this surface covered?"
had no address to be asked at.

This module is that address, and it is DERIVED rather than written down.
:data:`READER_TEXT_ARGUMENTS` names the call and the argument that carry
reader text; a test reads every such call out of the source, builds the
same key :func:`reader_surface_key` builds, and holds the result against
:data:`READER_TEXT_AUTHOR`. A surface nobody classified is a surface
nobody asked the question about: who wrote this text, and what keeps an
internal identifier out of it.

The key carries the EXPRESSION, not just the place, so that changing what
a surface writes re-asks the question instead of inheriting the previous
answer. That is the whole point: a classification that survives a rewrite
of the thing it classifies is a label, not a guard.
"""

from __future__ import annotations

MODEL_TEXT_GUARDED = "model_text_guarded"
"""A model wrote this text, and the writer guard sees it before a reader.

The guard is the one in :mod:`inqtrix.agents.report_quality`
(``introduced_identifiers``/``added_identifiers`` over
``writer_identifiers``, with ``user_authored_text`` as the exemption).
There is exactly one, on purpose: a second mechanism would have to be
kept in agreement with the first, and the surfaces it did not cover
would be invisible again.
"""

RUNTIME_TEXT = "runtime_text"
"""This codebase wrote the text: a literal, or an :mod:`inqtrix.i18n` key.

No model contributed a fragment, so no identifier can arrive in it. A
template whose placeholders are filled with model text is NOT this — the
expression in the key names those fills, and a new one makes the key new.
"""

USER_TEXT = "user_text"
"""The user's own words, carried through unchanged.

Not guarded, and the reason belongs at the surface: an identifier a
person typed is theirs to keep, and rewriting their document's name
would be an edit nobody asked for.
"""

NOT_READ = "not_read"
"""Machinery: the value is stored, but no reader is ever shown it.

The weakest claim here, so it is the one the enumeration checks hardest,
and it carries the most weight: it is also the blanket exemption from
the fixed-language rule, because a string nobody reads has no language
to get wrong. Claimed wrongly, it legitimizes a German sentence on an
English reader's transcript with one word in a dictionary.

So it may be claimed only by an artifact write, and only for the kinds
in :data:`MACHINERY_ARTIFACT_KINDS`. A narration line, a plan card, a
clarification round and an answer exist in order to be read; there is no
version of those where the claim is true, and the enumeration refuses it
outright rather than looking for a reason it might hold.
"""

MACHINERY_ARTIFACT_KINDS = ("evidence_bundle", "context_archive")
"""The artifact kinds written for the machine and never shown as text.

The ledger both engines keep, and the kernel's compaction archive: rows
whose ``content_markdown`` is empty and whose payload is read back by
the run itself, never rendered as a document.

Named here rather than reused from elsewhere, because the obvious
existing tuple is the wrong one.
``NAMED_ARTIFACT_KINDS`` is about canvas documents that carry derived
FILE NAMES (``artifact_names``); ``answer`` and ``critic_report`` are
read by a person without being in it, so a surface writing one of those
could have claimed ``NOT_READ`` and passed. Held against
:data:`~inqtrix.agents.control_ports.ARTIFACT_KINDS` so a renamed or
retired kind fails here instead of quietly widening the exemption.
"""

READER_TEXT_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "upsert_artifact": ("title", "content_markdown"),
    "emit_narration": ("text",),
    "_emit_narration": ("text",),
    "emit_answer": ("answer",),
    "save_plan": ("plan", "tasks"),
    "transition_plan_task": ("result_summary",),
    "_transition_task": ("result_summary",),
    "build_clarification": ("questions", "default_assumption"),
}
"""The calls that put text in front of a reader, and which argument does it.

Every entry is a CHOKE POINT — a port a text must pass through to reach
a person — and that is the whole reason deriving from them is worth
anything: a surface can only be added by calling one of them.

- the durable artifact row (``upsert_artifact``) and the streamed answer
  (``RunHandle.emit_answer``), which are two transports of one text;
- the transcript narration (``narration.emit_narration``, the single
  emitter of ``NARRATION_EVENT``, shared by both engines);
- the plan version (``save_plan``) — its intent paragraph, and every
  task's title and objective;
- a task's result (``transition_plan_task``), which the task card shows;
- a clarification round (``build_clarification``) — the prompts and
  option labels a person is asked, written by a model in both engines.

The list was once two of these, with "a run has two channels" written
above it. It was not: a plan card and a task result carried model text
that nothing checked, and the same task title was masked in the
transcript and shown verbatim on the card. An enumeration that claims
completeness is worse than none — it makes what it misses invisible. So
what is in here is named one port at a time, and what is outside is
named too, below.

``_emit_narration`` and ``_transition_task`` are the mission's own entry
points, which bind the run state to a port once for all their call
sites. They are named here because those call sites are the surfaces; a
call from one declared entry point into another is a RELAY, and the
derivation skips it — otherwise fourteen distinct narration lines would
collapse into the one forwarding call inside the wrapper, and the list
would stop saying anything.

The SCOPE, stated so it cannot be mistaken for more: an activity row's
``detail`` and ``label`` are read by a person too and are NOT in here.
They are emitted through the generic ``ACTIVITY_EVENT`` payload, which
carries operational fields as well, so "the argument that is reader
text" is not a property of the call there — it would have to be decided
per payload key. Extending this list to them is a cut of its own. An
agent run's outcome record (``inqtrix.agent.outcome``) is outside for the
same reason: its ``lines`` carry the review's open points — criterion
texts, gaps and findings a model wrote — beside counts and codes, and
every line passes the run's reader guard in ``run_outcome.send_outcome``
before the record is sent (the mission's ``_guard_reader_text``, the
kernel's ``KernelDeps.guard_reader_text``).

What that leaves open, named rather than glossed. Most of what lands in
those two fields is message-table text or a title that passed the guard
where it was produced — the task title above is checked in
``_guarded_plan`` now, and the activity row inherits the checked one. The
exception is the discovery probe, which puts its search QUERY into
``detail``. That query is composed before the run has retrieved
anything: the ledger is empty at that point, so there is no identifier in
reach for it to carry, and a guard placed there would be a call that
cannot find anything rather than a check. It is written here because the
reason is the scope — if discovery ever probes after retrieval, the
reason is gone and this paragraph is where that shows.
"""

READER_TEXT_AUTHOR: dict[str, str] = {
    # -- the mission transcript -------------------------------------- #
    # Every line here passes ``algorithm._guard_reader_text``, which
    # sits inside the wrapper, so the classification answers only WHOSE
    # words these are - never whether somebody remembered the guard.
    "agents.algorithm:_emit_narration.text="
    "t(state, 'agent_limit_tokens_narration')": RUNTIME_TEXT,
    "agents.algorithm:_emit_narration.text="
    "settled_narration(settled, _named_settings(deps, state, facts), state)": (
        MODEL_TEXT_GUARDED
    ),
    "agents.algorithm:_emit_narration.text="
    "t_count(state, 'agent_clarify_skipped', len(dropped))": RUNTIME_TEXT,
    "agents.algorithm:_emit_narration.text="
    "not_asked_narration(entries, reason, state)": MODEL_TEXT_GUARDED,
    "agents.algorithm:_emit_narration.text="
    "not_asked_narration(entries, 'overflow', state)": MODEL_TEXT_GUARDED,
    "agents.algorithm:_emit_narration.text="
    "t_count(state, 'agent_discovery_limit_narration', plan.omitted_count, "
    "limit=plan.limit)": RUNTIME_TEXT,
    "agents.algorithm:_emit_narration.text="
    "discovery_narration(discovery if isinstance(discovery, DiscoveryResult) "
    "else None, language=state['language'])": MODEL_TEXT_GUARDED,
    "agents.algorithm:_emit_narration.text="
    "plan_narration(plan.summary_markdown, len(tasks), "
    "language=state['language'])": MODEL_TEXT_GUARDED,
    "agents.algorithm:_emit_narration.text="
    "t(state, 'agent_gate_rejected_draft_kept', subject=subject)": RUNTIME_TEXT,
    "agents.algorithm:_emit_narration.text="
    "t(state, 'agent_gate_rejected_no_research', subject=subject)": RUNTIME_TEXT,
    "agents.algorithm:_emit_narration.text="
    "task_narration(task.title if task else task_id, outcome.summary)": (
        MODEL_TEXT_GUARDED
    ),
    "agents.algorithm:_emit_narration.text="
    "synthesis_narration(outline.title, len(outline.sections), "
    "language=state['language'])": MODEL_TEXT_GUARDED,
    "agents.algorithm:_emit_narration.text="
    "section_narration(done['title'], language=state['language'])": (
        MODEL_TEXT_GUARDED
    ),
    "agents.algorithm:_emit_narration.text="
    "t(state, 'agent_answer_written')": RUNTIME_TEXT,
    # -- the kernel transcript --------------------------------------- #
    # The same channel from the other engine. Three of these five lines
    # carry model text - a clarification question, the deep review's own
    # prose, a child's report - and none of them had a guard before the
    # emitter became one for both engines.
    "agents.kernel.algorithm:emit_narration.text="
    "t({'language': resolve_answer_language(deps.question).code}, "
    "'agent_limit_tokens_narration')": RUNTIME_TEXT,
    "agents.kernel.algorithm:emit_narration.text="
    "t({'language': resolve_answer_language(deps.question).code}, "
    "'agent_memory_detail_unavailable')": RUNTIME_TEXT,
    "agents.kernel.algorithm:emit_narration.text=record.question": (
        MODEL_TEXT_GUARDED
    ),
    "agents.kernel.algorithm:emit_narration.text="
    "t(_review_words, 'kernel_review_prefix', text=text)": MODEL_TEXT_GUARDED,
    "agents.kernel.tools:emit_narration.text=text": MODEL_TEXT_GUARDED,
    # Three lines that spell the event name themselves. All three are
    # message-table text, so no model text reaches the reader through
    # them; they are listed here so that stays a checked property rather
    # than luck.
    "agents.kernel.middleware:emit_narration.text=narration_text": (
        RUNTIME_TEXT
    ),
    "agents.kernel.middleware:emit_narration.text="
    "t({'language': resolve_answer_language(deps.question).code}, "
    "'kernel_batchguard_ask_first' if ask_turn else "
    "'kernel_batchguard_bundled')": RUNTIME_TEXT,
    "agents.kernel.model_recovery:emit_narration.text="
    "t({'language': resolve_answer_language(self.deps.question).code}, "
    "f'kernel_recovery_{reason}')": RUNTIME_TEXT,
    # -- artifact rows ----------------------------------------------- #
    # The mission's evidence ledger and the kernel's: machinery, and the
    # reason stands at both call sites.
    "agents.algorithm:upsert_artifact.title='Agent evidence'": NOT_READ,
    "agents.algorithm:upsert_artifact.content_markdown=''": RUNTIME_TEXT,
    "agents.kernel.deps:upsert_artifact.title='Kernel evidence'": NOT_READ,
    "agents.kernel.deps:upsert_artifact.content_markdown=''": RUNTIME_TEXT,
    # The editor patch row is named after the document the user targeted.
    "agents.algorithm:upsert_artifact.title=document.title": USER_TEXT,
    # The review report is named from the message table; its body lives in
    # the payload, not in reader Markdown.
    "agents.algorithm:upsert_artifact.title=t(state, 'critic_report_title')": (
        RUNTIME_TEXT
    ),
    # The memo: the outline writes both its name and its headings, and
    # ``algorithm._repair_outline_headings`` is where they are checked.
    "agents.algorithm:upsert_artifact.title="
    "state.get('memo_title') or t(state, 'memo_title_default')": (
        MODEL_TEXT_GUARDED
    ),
    "agents.algorithm:upsert_artifact.content_markdown=content": (
        MODEL_TEXT_GUARDED
    ),
    # The kernel canvas: title and body are checked together in the tool.
    "agents.kernel.tools:upsert_artifact.title="
    "current.title if current is not None else title": MODEL_TEXT_GUARDED,
    "agents.kernel.tools:upsert_artifact.content_markdown="
    "normalized_markdown": MODEL_TEXT_GUARDED,
    # The answer row: named from the message table in the question's
    # language (the name is built once in ``publish``), body checked by
    # the producing algorithm before this transport is handed it.
    "services.agent_answer_publisher:upsert_artifact.title=title": (
        RUNTIME_TEXT
    ),
    "services.agent_answer_publisher:upsert_artifact.content_markdown=''": (
        RUNTIME_TEXT
    ),
    "services.agent_answer_publisher:upsert_artifact.content_markdown="
    "answer": MODEL_TEXT_GUARDED,
    # The stream the reader actually reads. Same local as the artifact
    # row above, one line later - the row is staged empty and finalized
    # while this streams - and the other engine's plain path is the same
    # text without a persistent publication. Named separately because a
    # transport is not a check: whichever of the two is removed, the
    # question has to be asked again about the one that stays.
    "services.agent_answer_publisher:emit_answer.answer=answer": (
        MODEL_TEXT_GUARDED
    ),
    "services.run_service:emit_answer.answer=answer": MODEL_TEXT_GUARDED,
    # -- the plan ----------------------------------------------------- #
    # Intent paragraph, task titles and objectives: planner-written, and
    # checked in ``algorithm._guarded_plan`` where the planner's output
    # becomes records, so the approval card, the replan gate, the plan
    # narration and every later listing all read the same checked text.
    "agents.algorithm:save_plan.plan=record": MODEL_TEXT_GUARDED,
    "agents.algorithm:save_plan.tasks=tasks": MODEL_TEXT_GUARDED,
    # -- a task's result ---------------------------------------------- #
    # What the task card shows when a subtask ends. Written by a model
    # that saw raw document ids in its search results, so this is the
    # line most likely to carry one; guarded before the word clip, on
    # both ways into the row - the run's own, and the service that folds
    # a child after the parent died.
    "agents.algorithm:_transition_task.result_summary=row_summary": (
        MODEL_TEXT_GUARDED
    ),
    "services.agent_control_service:transition_plan_task.result_summary="
    "summary": MODEL_TEXT_GUARDED,
    # Settling a cancelled plan writes the reason, from the message table.
    "agents.control_ports:transition_plan_task.result_summary="
    "pending_summary": RUNTIME_TEXT,
    "agents.control_ports:transition_plan_task.result_summary="
    "running_summary": RUNTIME_TEXT,
    "agents.control_ports:transition_plan_task.result_summary="
    "t(words, 'agent_task_settled_cancel_requested')": RUNTIME_TEXT,
    # -- a clarification round ---------------------------------------- #
    # The prompts and option labels a person is asked. Three call sites,
    # two engines, one shared projection over the question shape
    # (``clarification.guarded_questions``); each passes the guard ITS
    # run can bind.
    "agents.algorithm:build_clarification.questions="
    "guarded_questions(questions, lambda text: _guard_reader_text(state, "
    "text))": MODEL_TEXT_GUARDED,
    "agents.kernel.tools:build_clarification.questions="
    "guarded_questions(questions, deps.guard_reader_text)": MODEL_TEXT_GUARDED,
    # The assumption the run proceeds under if nobody answers. Written by
    # the model that asked, shown under the question.
    "agents.kernel.tools:build_clarification.default_assumption="
    "deps.guard_reader_text(default_assumption)": MODEL_TEXT_GUARDED,
    "agents.kernel.middleware:build_clarification.questions="
    "guarded_questions(questions, deps.guard_reader_text)": (
        MODEL_TEXT_GUARDED
    ),
}
"""Whose words every reader surface of this server carries.

Keyed by :func:`reader_surface_key`, and derived from the source rather
than maintained beside it: the test that reads these calls out of the
tree fails on a surface that is missing here, and on an entry here that
no longer matches any call.

Identical expressions in one module collapse to one entry on purpose -
the same expression gets the same answer, and two rows that must agree
are two rows that can disagree. Where an expression is a bare local name
and therefore says nothing on its own, the reason stands at the call
site, which is where a reader of the code is.
"""


def reader_surface_key(
    module: str, callee: str, argument: str, expression: str
) -> str:
    """The address of one reader surface.

    ``module`` is dotted and relative to ``inqtrix``; ``expression`` is
    the argument as :func:`ast.unparse` writes it. Built here rather than
    in the test so the map and the derivation cannot spell the same
    surface two ways.
    """
    return f"{module}:{callee}.{argument}={expression}"
