"""Deterministic report-quality building blocks shared by BOTH engines.

The mission phase machine (``synthesis.py``/``algorithm.py``) and the
cognitive kernel (``kernel/tools.py``) consume the SAME citation
validation, quote grounding, and evidence ranking — one definition,
never a per-engine copy. Everything here except
:func:`validate_and_repair_citations` is pure and LLM-free.

Quote verification is EXCERPT-BASED: quoted passages are checked
verbatim against the evidence texts stored at retrieval time (internal
chunks and web excerpts alike). Nothing is re-fetched and model memory
is never consulted, so a quote that only exists beyond the stored
excerpt window stays visibly unverified rather than silently trusted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Callable

from inqtrix.agents.evidence_context import INTERNAL_SOURCE_SCHEME
from inqtrix.agents.markdown import normalize_writer_markdown
from inqtrix.citation_labels import (
    AGENT_FAMILIES,
    cited_labels,
    malformed_citations,
)
from inqtrix.agents.phase_models import CitationRepairText
from inqtrix.agents.patterns._structured import structured_call
from inqtrix.domains import SOURCE_TIER_WEIGHTS
from inqtrix.observability import semconv
from inqtrix.observability.otel import add_span_event

if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider

_QUOTE = re.compile(
    r'"([^"]{20,400})"|„([^“]{20,400})“|“([^”]{20,400})”|'
    r'«([^»]{20,400})»|»([^«]{20,400})«'
)
_WORD = re.compile(r"[^\W_]+(?:['’\u2010-\u2015][^\W_]+)*")
log = logging.getLogger("inqtrix")


class CitationValidationFailed(RuntimeError):
    """Raised when one bounded repair cannot restore citation integrity."""

    def __init__(self, message: str, *, usage: dict[str, int]) -> None:
        super().__init__(message)
        self.usage = dict(usage)
        """Token usage of the rejected synthesis and repair calls."""


def _merge_usage(*values: dict[str, int]) -> dict[str, int]:
    """Add token usage from one synthesis call and its optional repair."""
    return {
        "prompt_tokens": sum(int(value.get("prompt_tokens", 0)) for value in values),
        "completion_tokens": sum(
            int(value.get("completion_tokens", 0)) for value in values
        ),
    }


def validate_and_repair_citations(
    llm: "LLMProvider",
    *,
    markdown: str,
    known_labels: list[str],
    usage: dict[str, int],
    system: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
    internal_reference_ids: list[str] | None = None,
    authored_by: str = "",
) -> tuple[str, dict[str, int]]:
    """Accept known, readable citations without introduced internal IDs,
    or repair once.

    A label outside *known_labels* and a citation bracket no reader can
    parse (:func:`malformed_citation_brackets`) both call for the repair;
    the repaired text passes the writer boundary before it is checked.
    ``authored_by`` is the text of this run a PERSON typed, built by
    :func:`user_authored_text`. An identifier the user wrote is theirs to
    repeat and is not a leak — see :func:`introduced_identifiers`.

    Raises:
        CitationValidationFailed: The single bounded repair round still
            left unknown labels, unreadable citation brackets or internal
            IDs (or produced no text) — the caller fails loudly instead of
            shipping invented citations.
    """
    from inqtrix.agents.prompts import build_agent_citation_repair_prompt

    references = [{"label": label} for label in known_labels]
    unknown = unknown_citation_labels(markdown, references)
    malformed = malformed_citation_brackets(markdown)
    leaked_ids = introduced_identifiers(
        markdown, internal_reference_ids or [], authored_by=authored_by
    )
    if not unknown and not malformed and not leaked_ids:
        return markdown, usage

    log.warning(
        "Agent synthesis needs bounded citation repair "
        "(unknown_labels=%d, malformed_brackets=%d, internal_ids=%d).",
        len(unknown),
        len(malformed),
        len(leaked_ids),
    )
    repair = structured_call(
        llm,
        prompt=build_agent_citation_repair_prompt(
            markdown,
            allowed_labels=known_labels,
            internal_reference_ids=leaked_ids,
            malformed_brackets=malformed,
        ),
        model_cls=CitationRepairText,
        node="agent_citation_repair",
        system=system,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
    )
    repaired = repair.value
    # The repair is writer text: it passes the writer boundary BEFORE it is
    # checked, so a group it wrote counts like the single brackets stored.
    repaired_markdown = (
        normalize_writer_markdown(repaired.markdown.strip())
        if isinstance(repaired, CitationRepairText)
        else ""
    )
    remaining = unknown_citation_labels(repaired_markdown, references)
    remaining_malformed = malformed_citation_brackets(repaired_markdown)
    remaining_ids = introduced_identifiers(
        repaired_markdown, internal_reference_ids or [], authored_by=authored_by
    )
    merged_usage = _merge_usage(usage, repair.usage)
    if not repaired_markdown or remaining or remaining_malformed or remaining_ids:
        raise CitationValidationFailed(
            "Citation repair did not produce a complete text with only known "
            f"labels (remaining={remaining or []}, "
            f"malformed={len(remaining_malformed)}, "
            f"internal_ids={len(remaining_ids)}, "
            f"empty_output={not bool(repaired_markdown)}).",
            usage=merged_usage,
        )
    return repaired_markdown, merged_usage


def embedded_reference_ids(markdown: str, reference_ids: list[str]) -> list[str]:
    """Find concrete registered identifiers in reader-facing prose.

    The caller supplies actual ledger identifiers. No identifier pattern,
    source-domain rule, or prose rewriting is involved. The primitive under
    :func:`introduced_identifiers`, which is what a writer guard asks: this
    one only answers whether the value stands in the text, not who put it
    there.
    """
    return sorted({value for value in reference_ids if value and value in markdown})


REFERENCE_RECORD_ID_FIELDS = ("reference_id", "revision_id", "generation_id")
"""The ids of the evidence RECORD itself, each a single value.

Named apart from the rest because the review's reader-naming substitutes
exactly these (plus the document id it needs anyway to tell an internal
source from an external one): a search or origin id belongs to the run and
the search, which that naming resolves from their own records.
"""

REFERENCE_ID_FIELDS = REFERENCE_RECORD_ID_FIELDS + (
    "document_id",
    "chunk_id",
    "collection_id",
    "source_id",
    "query_id",
    "query_ids",
    "citation_id",
    "citation_ids",
    "source_run_id",
    "source_run_ids",
)
"""EVERY id field a reference dict of either engine may carry.

One definition, because two lists of "the ids a reference has" drift apart
in exactly the way that leaves a guard reading half of them. Every field
here has a producer, or it would be dead:

- ``reference_id`` — ``kernel/deps.py`` (``register_references``,
  ``hydrate_evidence``), and the mission's section handover;
- ``document_id``, ``chunk_index``, ``revision_id``, ``generation_id`` —
  the knowledge projector, through ``search_project_knowledge`` and
  ``knowledge/algorithm.py``;
- ``chunk_id``, ``collection_id`` — the SAME projector, but only along
  ``knowledge.document.cite`` -> ``document_analysis``, which appends the
  citation's evidence dict whole. Narrow, yet real: a mission that reads
  documents carries both, and a role reads them in the provenance;
- ``source_id``, ``query_id(s)``, ``citation_id(s)``, ``source_run_id(s)``
  — ``evidence.attach_web_search_lineage`` copies them from the search
  ledger onto every web reference.

The plural fields are lists, which is why every reader here flattens
instead of stringifying: ``str(["q1", "q2"])`` would be one garbage key.
"""

IDENTIFIER_FAMILIES = tuple(
    dict.fromkeys(
        field.removesuffix("s").removesuffix("_id").replace("_", " ")
        for field in REFERENCE_ID_FIELDS
    )
)
"""The id families a writing prompt may name, derived from the fields above.

A prompt that forbids identifier families is a promise, and a promise the
guard cannot keep is the same gap seen from the other side: it either
names a family nothing checks, or it leaves out one the guard does read
and the model takes the omission for permission. Deriving the wording
from the field list is what makes the two sides one edit — see
:data:`~inqtrix.agents.prompts.NO_MODEL_SOURCE_SECTIONS`, which builds its
sentence from this tuple.
"""

_EVIDENCE_LABEL_SHAPE = re.compile(
    r"\A(?:[A-Za-z]{1,2}\d{1,3}|(?:Quelle|Source)(?:\s*\d{1,3})?)\Z",
    re.IGNORECASE,
)
"""A value shaped like the label a reader is supposed to cite.

``K1``/``W3`` are what the ledger labels a source, ``Quelle 2`` what a
report reference falls back to. The knowledge projector files a hit under
``reference_id="K<rank>"`` internally and the HTTP search surface returns
that form, so a label-shaped value CAN reach an id field. Reading it as an
internal identifier would make the substring test fire on every correctly
cited ``[K1]`` — the same trap ``capabilities/catalog/knowledge.py`` names
where it pops that id off a citation. A label belongs in the text; this
filter is what says so once, for every guard site.
"""

_IDENTIFIER_NEIGHBOUR = r"[0-9A-Za-z_-]"
"""What continues an identifier rather than ending it."""


def reference_identifiers(ref: dict[str, Any]) -> frozenset[str]:
    """Every identifier ONE reference carries, without its reader label.

    The ledger id first: a mission reference carries no ``reference_id`` of
    its own, so a guard built from that field alone compared the prose
    against empty strings — and the id a role actually reads its
    preselection under is the derived one. Then every field of
    :data:`REFERENCE_ID_FIELDS`, lists flattened. A label-shaped value is
    dropped: the reader is supposed to write that one.
    """
    from inqtrix.agents.kernel.deps import KernelDeps

    # Only the reference id can hold a label: the knowledge projector
    # files a hit under "K<rank>", the HTTP search surface returns that
    # form, and the derived ledger id repeats a pre-set reference_id. Every
    # other field is taken as written - testing all of them would blind the
    # guard wherever a document or chunk id happens to be short, and a
    # filter meant for one field must not silently drop ids in ten others.
    values = [
        value for value in (KernelDeps.ledger_id(ref), ref.get("reference_id"))
        if isinstance(value, str) and not _label_shaped(value, ref)
    ]
    for field in REFERENCE_ID_FIELDS:
        if field == "reference_id":
            continue
        raw = ref.get(field)
        for item in raw if isinstance(raw, (list, tuple)) else [raw]:
            if isinstance(item, str):
                values.append(item)
    # The internal address is an identifier too, and the prompt has always
    # said so. It is not an id FIELD - it lives in ``url`` beside the web
    # addresses a reader may legitimately be shown - so it is read here
    # rather than added to REFERENCE_ID_FIELDS, whose producers a separate
    # guard pins. A web URL stays out: there the address is what a reader
    # would look up.
    url = str(ref.get("url") or "")
    if url.casefold().startswith(INTERNAL_SOURCE_SCHEME):
        values.append(url)
    return frozenset(value for raw in values if (value := raw.strip()))


def _label_shaped(value: str, ref: dict[str, Any]) -> bool:
    """Whether *value* is the label a reader is supposed to cite."""
    if not _EVIDENCE_LABEL_SHAPE.match(value.strip()):
        return False
    log.warning(
        "Reference %s is filed under the label-shaped id %s; it is read as a "
        "reader label, not as an internal identifier.",
        ref.get("document_id") or ref.get("url") or "?", value.strip(),
    )
    return True


def writer_identifiers(references: list[dict[str, Any]]) -> list[str]:
    """Every identifier the writer of *references* could have copied.

    The list form the writing calls hand to their citation check, sorted so
    a repair prompt and a failure message name the same ids in the same
    order across two calls of one run.
    """
    return sorted(
        {value for ref in references for value in reference_identifiers(ref)}
    )


USER_TURN_MARKERS = ("User: ", "Nutzer: ")
"""How a transcript marks a line the USER wrote."""

MODEL_TURN_MARKERS = ("Agent: ", "Assistent: ", "System: ", "Note: ")
"""How a transcript marks a line the user did NOT write."""

CLARIFICATION_MARKER = "Clarification: "
CLARIFICATION_ANSWER_MARKER = " — Answer: "
ASK_USER_ANSWER_MARKER = "User's answer: "
"""A clarification line carries both voices: the model asked, the user
answered. Only the answer half is the user's.

These markers are written by :mod:`inqtrix.agents.session_context`
(``User:``/``Agent:``/``Clarification: … — Answer: …``/``Note:``),
:mod:`inqtrix.services.request_parsing` (``Nutzer:``/``Assistent:``/
``System:``) and the kernel's ``ask_user`` transcript
(``Question: …\nUser's answer: …``). They are repeated here because the
writer guard, not the producers, is where reading a model line as a user
line becomes a hole - and a test pins the two sides together so the
agreement cannot drift in silence.
"""


def user_authored_text(
    *plain: str, history: str = "", clarifications: Iterable[str] = ()
) -> str:
    """The text of this run a PERSON typed, and nothing else.

    The writer guard exempts an identifier the user wrote. That exemption is
    only as sound as this projection: a transcript mixes both voices, so
    handing one in whole would let an id the MODEL wrote - into its own
    clarification question, into the answer of an earlier turn - exempt
    every later copy of itself, for the rest of the session. The guard would
    then certify precisely what it exists to catch.

    *plain* blocks are wholly user-written (the question, the user's
    guidance, a passage they wrote themselves). *history* is a verbatim
    transcript, projected to its user lines. *clarifications* are
    ``ask_user`` blocks, projected to their answer halves.

    An unmarked line continues the turn above it, so a user's second
    paragraph stays and a model's does not. An unattributable line is
    dropped: the guard then asks for a repair it did not strictly need,
    which is the failure this side can afford.
    """
    parts = [text for text in plain if text]
    parts.extend(
        answer for block in clarifications
        if (answer := _after(str(block), ASK_USER_ANSWER_MARKER))
    )
    if history:
        parts.extend(_user_lines(history))
    return "\n".join(parts)


def _after(text: str, marker: str) -> str:
    """What follows the first *marker*, or nothing when it never stands."""
    return text.split(marker, 1)[1].strip() if marker in text else ""


def _user_lines(history: str) -> list[str]:
    """The lines of *history* the user wrote, continuations included."""
    kept: list[str] = []
    mine = False
    for line in history.splitlines():
        if line.startswith(USER_TURN_MARKERS):
            mine = True
            kept.append(line.split(": ", 1)[1])
        elif line.startswith(MODEL_TURN_MARKERS):
            mine = False
        elif line.startswith(CLARIFICATION_MARKER):
            # Both voices on one line: the model's question, the user's
            # answer. A continuation belongs to the answer.
            mine = CLARIFICATION_ANSWER_MARKER in line
            if mine:
                kept.append(_after(line, CLARIFICATION_ANSWER_MARKER))
        elif mine:
            kept.append(line)
    return [line for line in kept if line.strip()]


def _standalone(value: str) -> re.Pattern[str]:
    """*value* as its own token, so a longer id never answers for a shorter one."""
    return re.compile(
        rf"(?<!{_IDENTIFIER_NEIGHBOUR}){re.escape(value)}(?!{_IDENTIFIER_NEIGHBOUR})"
    )


def introduced_identifiers(
    text: str, identifiers: list[str], *, authored_by: str
) -> list[str]:
    """Internal identifiers in *text* that the USER did not write.

    A guard that only asks "does an id stand in the prose" cannot tell a
    leak from a quote: a user who names a document id in their question
    looks exactly like a model copying its evidence card. An id the user
    wrote is theirs to repeat, so it is not a leak however often the answer
    names it.

    *authored_by* must carry ONLY text a person typed. Feeding a model's
    own earlier output into it lets one leaked id license every later copy
    of itself - the guard would then certify what it is meant to catch.
    :func:`user_authored_text` is what builds that value; do not assemble it
    per call site.

    "Written by the user" means standing as its own token, not merely as a
    substring: ``doc-42`` inside ``mydoc-4212`` is a different id, and
    reading it as present would exempt the real one from the guard.

    This answers authorship only. Whether an id was ALREADY STANDING in a
    text being edited is a different question with a different answer -
    :func:`added_identifiers`.
    """
    embedded = embedded_reference_ids(text, identifiers)
    if not embedded or not authored_by:
        return embedded
    kept = []
    for value in embedded:
        if _standalone(value).search(authored_by):
            log.warning(
                "Writer guard: identifier %s stands in the user's own text "
                "and is not treated as a leak.", value,
            )
            continue
        kept.append(value)
    return kept


def added_identifiers(
    text: str, identifiers: list[str], *, replacing: str
) -> list[str]:
    """Internal identifiers *text* carries MORE OFTEN than *replacing* did.

    The edit question, and deliberately not a set: a document that already
    names an id once must stay editable, but one standing occurrence may
    not license ten new ones. Counting is what separates "left alone" from
    "copied in", and a set comparison cannot - it answers yes to both.

    Occurrences are counted as standalone tokens, the same reading
    :func:`introduced_identifiers` uses, so the two cannot disagree about
    what an occurrence is.
    """
    added = []
    for value in embedded_reference_ids(text, identifiers):
        pattern = _standalone(value)
        before = len(pattern.findall(replacing)) if replacing else 0
        if len(pattern.findall(text)) > before:
            added.append(value)
        elif before:
            log.warning(
                "Writer guard: identifier %s was already standing in the "
                "text being edited and is not treated as a leak.", value,
            )
    return added


def mask_identifiers(text: str, identifiers: list[str], *, marker: str) -> str:
    """Replace every identifier in *text* with a VISIBLE marker.

    The deterministic repair, for the reader surfaces that have no writer
    left to ask back. A memo section or an answer is produced by a call
    this server can repeat, so a leak there goes to the bounded repair
    round. A narration line, a plan card and a task result are assembled
    from what a finished phase already produced: there is nobody left to
    re-ask, and one short line is not worth a model call of its own.

    Dropping the text instead would be a silent cap - the reader loses a
    step of the run and learns nothing about why. So it is written with
    the identifier replaced, and *marker* is what says so. It comes
    from the caller because it is user-visible text and belongs in the
    run's answer language, while this module stays language-free.

    Occurrences are PLAIN OCCURRENCES, the same reading
    :func:`embedded_reference_ids` uses to find them. The two must read
    the word "present" alike or the repair silently does nothing: a
    detection by substring and a repair by standalone token disagree
    exactly where an identifier sits inside a longer word
    (``kd_1234_v2.pdf``), and there the guard would report a leak, this
    function would change nothing, and the line would reach the reader
    unrepaired with no trace in the log. A masked substring can leave a
    stump (``[X]_v2.pdf``); a stump is legible and honest, an unrepaired
    identifier is neither.

    Longest first, so an address that contains a document id is masked
    before the id inside it. A value that is then no longer found was
    already covered by that longer mask - the one case where finding
    nothing is the truth rather than a failure.
    """
    masked = text
    for value in sorted(identifiers, key=len, reverse=True):
        if not value or value not in masked:
            continue
        log.warning(
            "Reader surface: identifier %s is replaced with a visible "
            "marker before it reaches a reader.", value,
        )
        masked = masked.replace(value, marker)
    return masked


def guarded_reader_text(
    text: str,
    identifiers: list[str],
    *,
    authored_by: str,
    marker: str,
) -> str:
    """Detect and repair in ONE reading, for a text nobody can rewrite.

    The two halves belong together. Detection alone reports a leak that
    still reaches the reader; repair alone masks strings the user typed
    themselves. Holding them in one function is what keeps the two
    readings of "is this identifier present" from drifting apart - that
    drift is not hypothetical, it is what made ``mask_identifiers`` a
    silent no-op for an identifier sitting inside a longer word.

    Only the AUTHORSHIP question is asked, never "was it already
    standing": this is the repair for a surface with no writer left to
    ask. A memo section can be re-requested from the model that wrote it;
    a transcript line, a plan card and a task result describe something
    that already happened, and the turn that produced them is over.

    Each engine binds this to what ITS run can see - the mission to its
    phase state, the kernel to its evidence lock, the reconciling service
    to the child's own ledger. One mechanism, three bindings: a second
    mechanism would have to be kept in agreement with the first, and the
    surfaces it did not cover would be invisible again.
    """
    leaked = introduced_identifiers(text, identifiers, authored_by=authored_by)
    if not leaked:
        return text
    return mask_identifiers(text, leaked, marker=marker)


def report_word_count(markdown: str) -> int:
    """Lexical word count, using the editor's letter/number boundary rules.

    Count the supplied Markdown, including headings, citation labels and code.
    This is a declared measurement, not tokenization or a language-aware count
    of the rendered body alone. Apostrophes and typographic dashes join words.
    """
    return len(_WORD.findall(markdown))


def citation_coverage(markdown: str) -> dict[str, int]:
    """Deterministic citation stats the critic receives as facts."""
    paragraphs = [
        paragraph.strip()
        for paragraph in markdown.split("\n\n")
        if paragraph.strip() and not paragraph.lstrip().startswith("#")
    ]
    cited = sum(1 for paragraph in paragraphs if cited_labels(paragraph))
    return {
        "paragraphs": len(paragraphs),
        "cited_paragraphs": cited,
        "labels_used": len(cited_labels(markdown)),
    }


def cited_references(
    markdown: str, references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return known references actually cited by the rendered output.

    Order follows the canonical ledger, not textual citation order, so label
    identity stays stable across partial memo flushes and revisions.
    """
    used = set(cited_labels(markdown))
    return [
        dict(reference)
        for reference in references
        if str(reference.get("label") or "") in used
    ]


def unknown_citation_labels(
    markdown: str,
    references: list[dict[str, Any]],
    *,
    families: str = AGENT_FAMILIES,
) -> list[str]:
    """Return cited labels of *families* absent from the canonical evidence ledger.

    *families* are K/W by default; a revision reads K/W/E,
    :data:`~inqtrix.citation_labels.DELEGATED_FAMILIES`. Read with the one
    citation grammar: a label inside a grouped bracket or as link text is
    cited, so an invented one there is unknown as well.
    """
    known = {str(reference.get("label") or "") for reference in references}
    return sorted(set(cited_labels(markdown, families)) - known)


def malformed_citation_brackets(
    markdown: str, *, families: str = AGENT_FAMILIES
) -> list[str]:
    """Brackets of the labels of *families* no reader can parse: a range, a
    slash, a prose join.

    *families* are K/W by default; a revision reads K/W/E,
    :data:`~inqtrix.citation_labels.DELEGATED_FAMILIES`. Loud like an unknown
    label: the mission repairs them once, the kernel hands them back or marks
    them; a range is never expanded.
    """
    return malformed_citations(markdown, families)


def verify_quotes(
    markdown: str, references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Verbatim-verify quoted passages against ALL stored evidence texts.

    Internal chunks AND web excerpts count — a quote attributed to a web
    source is checked against the excerpt captured at retrieval time,
    never against a re-fetch or model memory.
    Uses :func:`inqtrix.knowledge.grounding.quote_is_verbatim` — the
    platform's ONE normalizer chain, shared by every quote check.
    ``check_grounding`` is not a substitute: that parser expects
    ``[K#]`` lines and an ``ANSWER:`` scaffold, so a memo quote would
    silently come back unverified.
    Returns ``[{"quote", "verified"}]``; unverified stays visible.
    """
    from inqtrix.knowledge.grounding import quote_is_verbatim

    evidence_texts = [
        text
        for ref in references
        if (text := str(ref.get("excerpt") or ref.get("source_text") or ""))
    ]
    quotes = [
        next(group for group in match.groups() if group is not None)
        for match in _QUOTE.finditer(markdown)
    ]
    if not quotes:
        return []
    # Missing original text is not the same as "nothing to verify".  A web
    # result may legitimately carry only a URL or provider summary; treating
    # that state as an empty check list made the strict/tief critic believe no
    # unverified quote existed.  Preserve every quote as an explicit failed
    # verification until canonical evidence is available.
    if not evidence_texts:
        return [{"quote": quote, "verified": False} for quote in quotes]
    return [
        {"quote": quote, "verified": quote_is_verbatim(quote, evidence_texts)}
        for quote in quotes
    ]


def quoted_passages(markdown: str) -> list[tuple[str, str]]:
    """Every quoted passage as ``(inner text, passage with its marks)``.

    An edit that turns a quote into a paraphrase has to own the quotation
    marks too, so an anchor is the passage WITH them.
    """
    return [
        (next(group for group in match.groups() if group is not None), match.group(0))
        for match in _QUOTE.finditer(markdown)
    ]


def unverified_web_quotes(
    markdown: str, quote_checks: list[dict[str, Any]]
) -> list[str]:
    """Unverified quotes standing in a web-cited ([W#]) paragraph.

    The deterministic escalation trigger of the ``tief`` tier: a quoted
    claim that leans on a web citation but could not be matched against
    any stored excerpt. Paragraph scope keeps the attribution honest
    without a claim-to-source model that the ledger does not carry.
    """
    unverified = {
        str(check.get("quote") or "")
        for check in quote_checks
        if not check.get("verified")
    }
    if not unverified:
        return []
    flagged: list[str] = []
    for paragraph in markdown.split("\n\n"):
        if not any(label.startswith("W") for label in cited_labels(paragraph)):
            continue
        for quote in unverified:
            if quote and quote in paragraph and quote not in flagged:
                flagged.append(quote)
    return flagged


SYNTHESIS_EVIDENCE_BUDGET_LIMIT = "synthesis_evidence_budget"
"""``limit_name`` of the truncation event the digest cap raises."""

SECTION_REGISTER_LIMIT = "section_register"
"""``limit_name`` of the truncation event the section register's cap raises.

Its own name, because the two caps answer different questions and are set by
different knobs: a reader who sees a register cut must not go looking for the
digest budget."""


def rank_evidence(
    references: list[dict[str, Any]],
    *,
    budget: int,
    tier_for_url: Callable[[str], str] | None = None,
    limit_name: str = SYNTHESIS_EVIDENCE_BUDGET_LIMIT,
) -> list[dict[str, Any]]:
    """Deterministic relevance selection for the synthesis PROMPT digests.

    Below or at ``budget`` the ledger passes through untouched (no
    behavior change for typical runs); above it the top-``budget``
    references are kept in a stable order. The score is built only from
    signals the ledger actually carries, never from inert fields:

    - source tier (``domains.py`` weights; internal document references
      count as ``primary`` — the user selected that corpus deliberately),
    - cross-task corroboration (how many independent tasks surfaced the
      same source),
    - whether a real excerpt exists (only those can ground quotes).

    This caps what the outline/answer PROMPT sees; the citation ledger
    itself is never truncated — labels stay resolvable end-to-end.
    No LLM call.
    """
    if budget <= 0 or len(references) <= budget:
        return references

    def score(ref: dict[str, Any]) -> float:
        if ref.get("document_id") is not None:
            tier_weight = SOURCE_TIER_WEIGHTS["primary"]
        else:
            tier = str(ref.get("tier") or "")
            if not tier and tier_for_url is not None:
                tier = tier_for_url(str(ref.get("url") or ""))
            tier_weight = SOURCE_TIER_WEIGHTS.get(tier, SOURCE_TIER_WEIGHTS["unknown"])
        corroboration = 0.3 * min(len(ref.get("tasks") or []) - 1, 3)
        has_excerpt = bool(
            str(ref.get("excerpt") or ref.get("source_text") or "").strip()
        )
        return tier_weight + max(corroboration, 0.0) + (0.25 if has_excerpt else 0.0)

    ordered = sorted(
        range(len(references)),
        key=lambda index: (-score(references[index]), index),
    )
    kept = sorted(ordered[:budget])
    # A cap the writer never sees is a silent cap: name it in the log and
    # on the active span (the same truncation event every capped value
    # raises), so a thin digest is findable in the trace.
    log.warning(
        "rank_evidence capped %s to %d of %d references.",
        limit_name,
        budget,
        len(references),
    )
    add_span_event(
        semconv.TRUNCATION_EVENT,
        {
            semconv.TRUNCATION_LIMIT_NAME: limit_name,
            semconv.TRUNCATION_ORIGINAL_SIZE: len(references),
            semconv.TRUNCATION_CAPPED_SIZE: budget,
        },
    )
    return [references[index] for index in kept]
