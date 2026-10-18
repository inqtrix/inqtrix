"""Read-only discovery — the run's ``discovery`` phase.

Probes run DETERMINISTICALLY (code decides which, the LLM never steers
them) against the capability registry under a hard, server-side call
budget. The searches are the intake's content questions, verbatim, one
search each; nothing but that budget cuts them, and the cut is reported.
Raw probe texts stay in this module (context quarantine): the analyst
call sees a compressed digest — titles, collection names and excerpts,
never document or collection ids — inside an untrusted-content fence;
the caller gets only the structured
:class:`~inqtrix.agents.phase_models.DiscoveryResult` plus the count of
successful probes per source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from inqtrix.agents.intake import assignment_needs_web
from inqtrix.agents.patterns._structured import StructuredOutcome, structured_call
from inqtrix.agents.phase_models import AssignmentProfile, DiscoveryResult
from inqtrix.agents.prompts import (
    agent_analyst_system_prompt,
    build_agent_analyst_prompt,
)
from inqtrix.knowledge.retrieval_warnings import notice_severity
from inqtrix.text import PASSAGE_CUT_MARKER, shorten_visibly

if TYPE_CHECKING:
    from collections.abc import Sequence

    from inqtrix.agents.clarification import RunSettingsFacts
    from inqtrix.capabilities import CapabilityContext, CapabilityRegistry
    from inqtrix.providers.base import LLMProvider

log = logging.getLogger("inqtrix")

_PROBE_SNIPPET_CHARS = 400 + len(PASSAGE_CUT_MARKER)
"""Longest excerpt in the analyst digest (knowledge passages, web
snippets, failure texts): up to 400 characters of text, plus the
:data:`~inqtrix.text.PASSAGE_CUT_MARKER` a shortened one closes with."""


@dataclass
class ProbePlan:
    """The deterministic probe list (shown to the user in strict mode)."""

    probes: list[dict[str, Any]] = field(default_factory=list)
    requested_count: int = 0
    limit: int = 0
    left_out: list[str] = field(default_factory=list)
    """Search texts the budget cut before they ran, in plan order."""

    @property
    def searches(self) -> list[str]:
        """The knowledge search texts that run, in plan order."""
        return [
            str(probe["query"])
            for probe in self.probes
            if probe["kind"] == "knowledge.search"
        ]

    @property
    def omitted_count(self) -> int:
        """Number of probes visibly excluded by the server budget."""
        return max(0, self.requested_count - len(self.probes))

    def as_payload(self) -> list[dict[str, Any]]:
        """Wire shape for the discovery approval / discovery event."""
        return [dict(probe) for probe in self.probes]


def build_probe_plan(
    profile: AssignmentProfile | None,
    *,
    question: str,
    collection_ids: list[str],
    max_calls: int,
    web_preview_allowed: bool,
    knowledge_allowed: bool = True,
    clarified_context: str = "",
    user_chose_sources: bool = False,
) -> ProbePlan:
    """Assemble the deterministic probe list, budget-capped.

    Scope decision (deliberate, not an oversight): this builds three
    probe kinds — the collection listing (``knowledge.collections.
    list``), one ``knowledge.search`` per content question of the intake
    (``top_k=5``) and the ``web.search.instant`` preview. Every content
    question gets its search; only the budget cuts the list, and the
    cut stays visible as :attr:`ProbePlan.omitted_count`. The intake is
    asked for at most ``DISCOVERY_CONTENT_QUESTIONS_MAX`` questions, but
    nothing here enforces that number. Three further discovery sources
    are intentionally NOT probed here because each is either input-less
    or already covered:

    * Prior-run reuse would need a runs-read capability plus a
      question-similarity heuristic — a NEW catalog for a single
      consumer; within a session the prior context is already carried by
      the memo lineage (the session memo read at intake). It is a named
      follow-up, not a gap-fill.
    * Referenced files have no input: an agent run carries collection
      ids and one patch-target ``document_id``, never a list of
      @-mentioned individual files — a probe for them would have nothing
      to read.
    * Canvas/editor context is redundant: the canvas/memo context is
      read at intake (the session memo) and the editor patch-target
      document is read in the patch phase — probing it here would
      duplicate existing reads.
    """
    # Answered clarification rounds reach only the probes built from the
    # question itself: the fallback search below and the web preview.
    # Content questions run as the intake phrased them. Bounded so a long
    # free-text answer cannot blow up the query.
    context = clarified_context.strip()[:200]
    scoped_question = f"{question} — {context}" if context else question
    probes: list[dict[str, Any]] = []
    questions = _distinct_questions(profile.content_questions) if profile else []
    if knowledge_allowed:
        probes.append({"kind": "knowledge.collections.list", "query": ""})
        if not questions:
            # A success criterion never becomes a search; without content
            # questions the only honest search text is the assignment.
            log.warning(
                "Discovery has no content questions (%s); searching the "
                "assignment itself.",
                "intake failed" if profile is None else "intake wrote none",
            )
        for search in questions or [scoped_question]:
            probes.append(
                {
                    "kind": "knowledge.search",
                    "query": search,
                    "collection_ids": list(collection_ids),
                }
            )
    if web_preview_allowed and assignment_needs_web(
        profile, user_chose_sources=user_chose_sources
    ):
        probes.append(
            {"kind": "web.search.instant", "query": scoped_question}
        )
    requested_count = len(probes)
    left_out: list[str] = []
    if requested_count > max_calls:
        log.warning(
            "Discovery probe plan trimmed to the budget (%d -> %d).",
            len(probes),
            max_calls,
        )
        left_out = [
            str(probe["query"])
            for probe in probes[max_calls:]
            if probe["kind"] == "knowledge.search"
        ]
        probes = probes[:max_calls]
    return ProbePlan(
        probes=probes,
        requested_count=requested_count,
        limit=max_calls,
        left_out=left_out,
    )


def _distinct_questions(questions: list[str]) -> list[str]:
    """The intake's content questions as search texts: one line each,
    empty ones dropped, repeats (case-insensitive) searched once.

    A blank entry would run as a search for nothing and a repeat would
    spend budget on back-references only; both are logged, and a list that
    ends up empty counts as "intake wrote none".
    """
    distinct: list[str] = []
    seen: set[str] = set()
    for question in questions:
        text = " ".join(str(question).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            distinct.append(text)
    if len(distinct) != len(questions):
        log.warning(
            "Discovery dropped %d empty or repeated content questions "
            "(%d proposed).",
            len(questions) - len(distinct),
            len(questions),
        )
    return distinct


def execute_probes(
    plan: ProbePlan,
    *,
    registry: "CapabilityRegistry",
    capability_context: "CapabilityContext",
    invoke: Any = None,
) -> tuple[str, dict[str, int]]:
    """Run the probes; returns ``(digest_for_analyst, source_tool_counts)``.

    *invoke* overrides the capability invocation (test seam); defaults
    to ``registry.invoke``. Probe failures are collected VISIBLY into
    the digest — a discovery with dead probes still plans, but the
    analyst and the user see the holes. ``source_tool_counts`` counts
    the successful probes per source (``web``/``knowledge``).

    The digest names what the searches found the way a reader would:
    one ``Search k: <question>`` head per search, then each passage as
    ``[Sk.i] "<title>" · collection "<name>": <excerpt>``; the web preview
    gets its own ``Web preview: <query>`` head with ``[Wi] <url>:
    <snippet>`` lines. Every line of document or web text is collapsed to
    one line, so it cannot forge a head or a passage. The
    collection listing only supplies the names; the collections and
    their document counts reach the analyst as server facts in the run
    settings, outside the fence this digest travels in. A passage an
    earlier search already found is a back-reference, never a second
    copy; a cut excerpt says so.
    """
    import asyncio

    call = invoke or (
        lambda capability_id, payload: asyncio.run(
            registry.invoke(capability_id, payload, capability_context)
        )
    )
    lines: list[str] = []
    source_tool_counts = {"web": 0, "knowledge": 0}
    collection_names: dict[str, str] = {}
    seen: dict[tuple[str, int], str] = {}
    unnamed = 0
    search_number = 0
    for probe in plan.probes:
        kind = probe["kind"]
        try:
            if kind == "knowledge.collections.list":
                output = call("knowledge.collections.list", {})
                collection_names.update(
                    (str(item.get("id") or ""), _label(item.get("name")))
                    for item in _rows(output)
                )
            elif kind == "knowledge.search":
                search_number += 1
                # One line per head: a multi-line assignment searched as
                # the fallback must not spill lines of its own.
                head = " ".join(str(probe["query"]).split())
                lines.append(f"Search {search_number}: {head}")
                payload: dict[str, Any] = {
                    "query": probe["query"],
                    "top_k": 5,
                }
                if probe.get("collection_ids"):
                    payload["collection_ids"] = probe["collection_ids"]
                output = call("knowledge.search", payload)
                hits = _rows(output)[:5]
                for index, hit in enumerate(hits, start=1):
                    label = f"S{search_number}.{index}"
                    document_id = hit.get("document_id")
                    chunk_index = hit.get("chunk_index")
                    passage = (
                        (str(document_id), int(chunk_index))
                        if document_id and isinstance(chunk_index, int)
                        else None
                    )
                    if passage is not None and passage in seen:
                        lines.append(
                            f"[{label}] same passage as {seen[passage]}"
                        )
                        continue
                    if passage is not None:
                        seen[passage] = label
                    title = _label(hit.get("document_title"))
                    name = collection_names.get(
                        str(hit.get("collection_id") or "")
                    )
                    if not name:
                        unnamed += 1
                    # ``knowledge.search`` exposes only the canonical
                    # evidence projection. Retrieval ``text`` may contain
                    # generated context and is intentionally absent from
                    # the capability contract; using it here both emptied
                    # the analyst digest and invited callers to reintroduce
                    # the unsafe field.
                    lines.append(
                        f"[{label}] "
                        + (f'"{title}"' if title else "untitled document")
                        + " · "
                        + (
                            f'collection "{name}"'
                            if name
                            else "collection name unavailable"
                        )
                        + ": "
                        + shorten_visibly(
                            hit.get("excerpt"),
                            _PROBE_SNIPPET_CHARS,
                            marker=PASSAGE_CUT_MARKER,
                        )
                    )
                if not hits:
                    lines.append("No passages found.")
                for warning in _rows(output, key="warnings"):
                    # The search's own English message; the label says
                    # whether it limits what was found or only reports
                    # protection (a duplicate collapse).
                    code = str(warning.get("code") or "")
                    label = "DEGRADED" if notice_severity(code) == "limit" else "NOTE"
                    lines.append(
                        f"[{label} knowledge.search] "
                        + str(
                            warning.get("message")
                            or code
                            or "Incomplete retrieval execution."
                        )
                    )
            elif kind == "web.search.instant":
                lines.append(f"Web preview: {' '.join(str(probe['query']).split())}")
                output = call(
                    "web.search.instant",
                    {"query": probe["query"], "max_sources": 5},
                )
                sources = _rows(output, key="sources")[:5]
                for index, source in enumerate(sources, start=1):
                    # Web text is as untrusted as a document: one line,
                    # cut visibly, so it cannot forge a head or a passage.
                    lines.append(
                        f"[W{index}] {_label(source.get('url')) or 'no URL'}: "
                        + shorten_visibly(
                            source.get("snippet"),
                            _PROBE_SNIPPET_CHARS,
                            marker=PASSAGE_CUT_MARKER,
                        )
                    )
                if not sources:
                    lines.append("No web results found.")
            else:  # pragma: no cover - probe kinds are code-defined
                raise ValueError(f"unknown probe kind {kind!r}")
            source = "web" if kind.startswith("web.") else "knowledge"
            source_tool_counts[source] += 1
        except Exception as exc:  # noqa: BLE001 — probe failures stay visible
            log.warning(
                "Discovery probe %s failed (error_type=%s)",
                kind,
                type(exc).__name__,
            )
            lines.append(
                f"[FAILED {kind}] "
                + shorten_visibly(exc, _PROBE_SNIPPET_CHARS, marker=PASSAGE_CUT_MARKER)
            )
    if unnamed:
        log.warning(
            "Collection names unavailable in the discovery digest "
            "(passages=%d); the collection listing did not name them.",
            unnamed,
        )
    return "\n".join(lines) or "(no searches ran)", source_tool_counts


def _label(value: Any) -> str:
    """A title or collection name as one quotable line of digest text.

    Both are user-written: whitespace collapses, so a name cannot start a
    line of its own (a forged ``Search`` head), and double quotes become
    single ones, so the label cannot end its quotes early.
    """
    return " ".join(str(value or "").split()).replace('"', "'")


def _rows(output: Any, *, key: str = "results") -> list[dict[str, Any]]:
    """Rows of a capability output model or plain dict."""
    if hasattr(output, "model_dump"):
        output = output.model_dump()
    if isinstance(output, dict):
        value = output.get(key)
        if value is None and key == "results":
            # The knowledge.search capability names its rows ``hits``,
            # collections listing ``collections`` — cover the catalog's
            # actual output keys, never silently pretend emptiness.
            value = output.get("hits") or output.get("collections")
        return list(value or [])
    return []


def run_discovery_analysis(
    llm: "LLMProvider",
    *,
    question: str,
    probe_digest: str,
    plan: ProbePlan,
    settings: "RunSettingsFacts",
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    assumptions: "Sequence[dict[str, str]]" = (),
    history: str = "",
    language: str | None = None,
) -> StructuredOutcome:
    """The mid-tier analyst call; value is a DiscoveryResult.

    Args:
        plan: The probe plan that ran: which content questions were
            searched and which the budget left out are server facts the
            prompt states outside the digest's fence.
        settings: The run's fixed settings the prompt states.
        assumptions: Questions the run answered with an assumption
            instead of asking (never proposed again).
        history: Conversation plus ANSWERED clarification rounds. The
            analyst proposes ``questions_for_user`` — without the
            already-given answers it re-asks the intake round in new
            words (the observed duplicate-clarification bug).
    """
    return structured_call(
        llm,
        prompt=build_agent_analyst_prompt(
            question,
            probe_digest,
            plan.searches,
            left_out=plan.left_out,
            settings=settings,
            assumptions=assumptions,
            history=history,
            language=language,
        ),
        model_cls=DiscoveryResult,
        node="agent_discovery_analyst",
        system=agent_analyst_system_prompt(),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
