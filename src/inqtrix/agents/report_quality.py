"""Deterministic report-quality building blocks of the kernel.

Citation validation and the reader guard that keeps internal identifiers
out of reader text — one definition each. Everything here except :func:`validate_and_repair_citations`
is pure and LLM-free.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from inqtrix.agents.evidence_context import INTERNAL_SOURCE_SCHEME
from inqtrix.agents.session_context import USER_CONTINUATION_PREFIX
from inqtrix.untrusted_content import UNTRUSTED_FENCE_CLOSE, UNTRUSTED_FENCE_OPEN
from inqtrix.agents.markdown import normalize_writer_markdown
from inqtrix.citation_labels import (
    AGENT_FAMILIES,
    cited_labels,
    malformed_citations,
)
from inqtrix.agents.phase_models import CitationRepairText
from inqtrix.agents.patterns._structured import structured_call

if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider

log = logging.getLogger(__name__)


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
"""EVERY id field a reference dict may carry: of a kernel run, or of the
stored evidence of an older mission run the kernel reads.

One definition, because two lists of "the ids a reference has" drift apart
in exactly the way that leaves a guard reading half of them. Every field
here has a producer, or it would be dead:

- ``reference_id`` — ``kernel/deps.py`` (``register_references``,
  ``hydrate_evidence``);
- ``document_id``, ``chunk_index``, ``revision_id``, ``generation_id`` —
  the knowledge projector, through ``search_project_knowledge`` and
  ``knowledge/algorithm.py``;
- ``chunk_id``, ``collection_id`` — the SAME projector, but only along
  ``knowledge.document.cite`` -> ``document_analysis``, which appends the
  citation's evidence dict whole. Narrow, yet real: a run that analyses
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

    The ledger id first: a reference without a ``reference_id`` of its own
    (a stored mission reference) is filed under the derived id
    (``KernelDeps.ledger_id``), so a guard built from that field alone
    would compare the prose against empty strings. Then every field of
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
    # A web source has no internal id; its URL is not logged.
    log.warning(
        "Reference %s is filed under the label-shaped id %s; it is read as a "
        "reader label, not as an internal identifier.",
        ref.get("document_id") or "(web source)", value.strip(),
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


USER_TURN_MARKERS = ("User: ",)
"""How a transcript marks a line the USER wrote."""

MODEL_TURN_MARKERS = (
    "Agent: ", "Assistant: ", "System: ", "Tool: ", "Note: ", "[... ",
    UNTRUSTED_FENCE_OPEN, UNTRUSTED_FENCE_CLOSE,
)
"""How a transcript marks a line the user did NOT write: a model, system or
tool turn, a note, or the line that stands for left-out turns."""

CLARIFICATION_MARKER = "Clarification: "
CLARIFICATION_ANSWER_MARKER = " — Answer: "
ASK_USER_ANSWER_MARKER = "User's answer: "
"""A clarification line carries both voices: the model asked, the user
answered. Only the answer half is the user's.

These markers are written by :mod:`inqtrix.agents.session_context`
(``User:``/``Agent:``/``Clarification: … — Answer: …``/``Note:``),
:mod:`inqtrix.services.request_parsing` (``User:``/``Assistant:``/
``System:``/``Tool:``, every turn but the user's in a ``client-history``
fence, and ``[... n earlier messages omitted]``) and the
kernel's ``ask_user`` transcript
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
    """The lines of *history* the user wrote, continuations included.

    Between a fence's opening line and its closing line stands stored text
    in the wording of whoever wrote it: an earlier answer of the agent, a
    turn of a client-sent history, the record of a clarification. Nothing in
    there counts, whatever a line begins with - an answer that quotes a
    dialogue carries lines that open like the user's own turn. The fence
    defuses its delimiter inside the payload, so only the helper's own
    closing line ends it; a fence that never closes keeps the rest out,
    which is the failure this side can afford.
    """
    kept: list[str] = []
    mine = False
    fenced = False
    for line in history.splitlines():
        if fenced:
            fenced = not line.startswith(UNTRUSTED_FENCE_CLOSE)
        elif line.startswith(UNTRUSTED_FENCE_OPEN):
            fenced, mine = True, False
        elif line.startswith(USER_TURN_MARKERS):
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
            kept.append(line.removeprefix(USER_CONTINUATION_PREFIX))
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
    left to ask back. An answer is produced by a call this server can
    repeat, so a leak there goes to the bounded repair round. A narration
    line or an activity is assembled from what a finished step already
    produced: there is nobody left to re-ask, and one short line is not
    worth a model call of its own.

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
    ask. A canvas write can be refused back to the model that made it; a
    transcript line or an activity describes something that already
    happened, and the turn that produced it is over.

    The kernel binds this to what its run can see, under its evidence lock
    (``KernelDeps.guard_reader_text``). One mechanism: a second one would
    have to be kept in agreement with the first, and the surfaces it did
    not cover would be invisible again.
    """
    leaked = introduced_identifiers(text, identifiers, authored_by=authored_by)
    if not leaked:
        return text
    return mask_identifiers(text, leaked, marker=marker)


def cited_references(
    markdown: str, references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return known references actually cited by the rendered output.

    Order follows the canonical ledger, not textual citation order, so label
    identity stays stable across canvas writes and revisions.
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
    label: the kernel hands them back or marks them; a range is never
    expanded.
    """
    return malformed_citations(markdown, families)
