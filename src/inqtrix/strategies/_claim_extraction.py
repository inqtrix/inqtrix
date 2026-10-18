"""Claim extraction strategy — extract structured claims from search text."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from openai import OpenAIError

from inqtrix.constants import CLAIM_EXTRACT_TIMEOUT
from inqtrix.domains import SOURCE_ROLES
from inqtrix.exceptions import (
    AgentModelCapacityError,
    AgentRateLimited,
    AgentStructuredOutputError,
    AgentTimeout,
    AnthropicAPIError,
    AzureFoundryWebSearchAPIError,
    AzureOpenAIAPIError,
    BedrockAPIError,
)
from inqtrix.json_helpers import parse_json_object
from inqtrix.prompts import build_claim_extraction_prompt
from inqtrix.providers.base import (
    INCOMPLETE_FINISH_REASONS,
    LLMProvider,
    _NonFatalNoticeMixin,
    _bounded_timeout,
    _check_deadline,
)
from inqtrix.urls import normalize_url

log = logging.getLogger("inqtrix")

_CLAIM_TYPES: set[str] = {"fact", "actor_claim", "forecast"}
_CLAIM_POLARITY: set[str] = {"affirmed", "negated"}

_CLAIM_PRIMARY_HINT_RE = re.compile(
    r"(\b\d{1,3}(?:[.,]\d+)?\s*(?:%|prozent|mrd|mio|million(?:en)?|milliard(?:en)?|euro)\b"
    r"|\b(gesetz|verordnung|richtlinie)\b|\b\u00a7\s*\d+\b|\bart\.?\s*\d+\b)",
    re.IGNORECASE,
)

_CLAIM_ACTOR_VERB_RE = re.compile(
    r"\b(sagte|sagt|warnte|warnt|forderte|fordert|lehnte|lehnt|schloss|schliesst|"
    r"erkl[a\u00e4]rte|erkl[a\u00e4]rt|kuendigte|k\u00fcndigte|kritisiert|kritisierte|nannte|"
    r"bezeichnete|wies|zurueck|zur\u00fcck)\b",
    re.IGNORECASE,
)

_CLAIM_EXTRACTION_SCHEMA_NAME = "inqtrix_claim_extraction_v2"
"""v2: the call also returns ``source_roles`` (one judgement per source of the card)."""


@dataclass(frozen=True, slots=True)
class ProviderCitationRef:
    """Provider-local inline citation reference for claim source binding.

    Attributes:
        ref: Inline marker used in the provider answer, for example ``"2"``
            for Perplexity ``[2]`` citations. Values are provider-local and
            only meaningful together with the query result that produced them.
        url: Canonical source URL associated with ``ref``. The extractor uses
            it as the deterministic target when the model returns
            ``provider_refs`` for a claim.
        title: Optional source title shown to the extraction model to make the
            reference map easier to inspect. It is not used for binding.
    """

    ref: str
    url: str
    title: str = ""


_CLAIM_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_text": {
                        "type": "string",
                        "description": "Atomic, question-relevant claim.",
                    },
                    "evidence_snippet": {
                        "type": "string",
                        "description": "Short supporting snippet from the source text.",
                    },
                    "claim_type": {
                        "type": "string",
                        "enum": ["fact", "actor_claim", "forecast"],
                    },
                    "polarity": {
                        "type": "string",
                        "enum": ["affirmed", "negated"],
                    },
                    "needs_primary": {
                        "type": "boolean",
                    },
                    "provider_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Provider inline citation refs that support this "
                            "claim, e.g. ['2', '3'] for text with [2][3]."
                        ),
                    },
                },
                "required": [
                    "claim_text",
                    "evidence_snippet",
                    "claim_type",
                    "polarity",
                    "needs_primary",
                    "provider_refs",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims", "source_roles"],
    "additionalProperties": False,
}
_CLAIM_EXTRACTION_SCHEMA["properties"]["source_roles"] = {
    "type": "array",
    "description": (
        "One judgement per source of the source card: who speaks "
        "(authority, the company itself, research, newsroom, interested "
        "party, aggregator, user-generated content) and the evidence for it."
    ),
    "items": {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "The ref of the source card."},
            "role": {"type": "string", "enum": list(SOURCE_ROLES)},
            "reason": {
                "type": "string",
                "description": "The evidence for the role, at most twelve words.",
            },
        },
        "required": ["ref", "role", "reason"],
        "additionalProperties": False,
    },
}

SOURCE_ROLE_REASON_CHARS = 160
"""Bound of a persisted role reason (the prompt asks for twelve words)."""


def _provider_ref_key(value: Any) -> str:
    """Return a normalized provider-ref key for inline citation matching."""
    raw = str(value or "").strip().lower()
    raw = raw.strip("[](){} ")
    if raw.startswith("web:"):
        raw = raw[4:].strip()
    return raw


def _provider_ref_aliases(ref: ProviderCitationRef) -> list[str]:
    """Return accepted aliases for one provider citation reference."""
    key = _provider_ref_key(ref.ref)
    if not key:
        return []
    aliases = [key]
    if key.isdigit():
        aliases.append(f"web:{key}")
    return aliases


def _source_refs_for_prompt(
    provider_refs: list[ProviderCitationRef],
    *,
    citation_cap: int,
    fixed_roles: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return the compact source-reference map shown to the extractor LLM.

    A source whose role the run already fixed carries ``role_fixed``; the
    prompt tells the model not to judge those rows again. A custom
    strategy's tier without a source role is named ``strategy:<tier>``.
    """
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in provider_refs:
        key = _provider_ref_key(ref.ref)
        url = normalize_url(ref.url)
        if not key or not url or key in seen:
            continue
        seen.add(key)
        row = {"ref": key, "url": url}
        if ref.title:
            row["title"] = " ".join(str(ref.title).split())[:180]
        fixed_role = (fixed_roles or {}).get(url)
        if fixed_role:
            row["role_fixed"] = fixed_role
        rows.append(row)
        if len(rows) >= citation_cap:
            break
    return rows


def _resolve_provider_refs(
    raw_refs: Any,
    ref_to_url: dict[str, str],
    *,
    source_url_limit: int,
) -> tuple[list[str], list[str], list[str]]:
    """Resolve model-selected provider refs into internal source URLs.

    Returns:
        Tuple of ``(resolved_refs, source_urls, unknown_refs)``. The LLM is
        allowed to emit only provider-local refs; source URLs are derived here
        so downstream claim/evidence code keeps a single binding path.
    """
    resolved_refs: list[str] = []
    source_urls: list[str] = []
    unknown_refs: list[str] = []
    if not isinstance(raw_refs, list):
        return resolved_refs, source_urls, unknown_refs

    for raw_ref in raw_refs:
        ref_key = _provider_ref_key(raw_ref)
        if not ref_key or ref_key in resolved_refs or ref_key in unknown_refs:
            continue
        resolved_url = ref_to_url.get(ref_key)
        if not resolved_url:
            unknown_refs.append(ref_key)
            continue
        resolved_refs.append(ref_key)
        if resolved_url not in source_urls and len(source_urls) < source_url_limit:
            source_urls.append(resolved_url)
    return resolved_refs, source_urls, unknown_refs


# The citation that closes a passage of a search answer: a Markdown link
# "([domain](url))" as Azure writes it, or a numbered marker "[2]".
_CITATION_TOKEN_RE = re.compile(
    r"\(\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)\)|\[(?P<ref>\d{1,3})\]"
)
# A passage is a paragraph or a list item; providers cite at its end.
_PASSAGE_BREAK_RE = re.compile(r"\n\s*\n|\n(?=[ \t]*(?:[-*\u2022]|\d+[.)])\s)")
_MIN_PLACED_EXCERPT_CHARS = 12
PROVIDER_METADATA_HEADING = "Source metadata returned by the web search provider:"
"""Heading of the source list below the provider's answer in the extraction
text. Only the answer above it attributes statements; the list's numbers are
no citations."""


def _plain_with_positions(raw: str) -> tuple[str, list[int]]:
    """Lower-cased text without emphasis and citations, whitespace folded.

    ``positions[i]`` is the offset in ``raw`` of plain character ``i``, so a
    match in the plain text maps back to the raw passage.
    """
    citation_ends = {match.start(): match.end() for match in _CITATION_TOKEN_RE.finditer(raw)}
    chars: list[str] = []
    positions: list[int] = []
    index = 0
    after_space = True
    while index < len(raw):
        if index in citation_ends:
            index = citation_ends[index]
            continue
        char = raw[index]
        if char == "*":
            pass
        elif char.isspace():
            if not after_space:
                chars.append(" ")
                positions.append(index)
                after_space = True
        else:
            lowered = char.lower()
            chars.append(lowered if len(lowered) == 1 else char)
            positions.append(index)
            after_space = False
        index += 1
    return "".join(chars), positions


def _closing_citation_refs(
    excerpt: str,
    text: str,
    *,
    url_to_ref: dict[str, str],
    card_refs: set[str],
) -> list[str] | None:
    """Refs of the citation that closes the passage where ``excerpt`` stands.

    ``None`` when the text does not settle it: the excerpt is too short,
    absent or found more than once, nothing cites its passage after it, or
    a citation does not resolve to the source map.
    """
    needle = _plain_with_positions(excerpt)[0].strip()
    if len(needle) < _MIN_PLACED_EXCERPT_CHARS:
        return None
    answer = text.split(PROVIDER_METADATA_HEADING, 1)[0]
    placements: list[tuple[str, int]] = []
    for passage in _PASSAGE_BREAK_RE.split(answer):
        plain, positions = _plain_with_positions(passage)
        start = plain.find(needle)
        while start >= 0:
            placements.append((passage, positions[start + len(needle) - 1] + 1))
            start = plain.find(needle, start + 1)
    if len(placements) != 1:
        return None
    passage, end = placements[0]
    refs: list[str] = []
    position = end
    for match in _CITATION_TOKEN_RE.finditer(passage, end):
        if refs and passage[position:match.start()].strip():
            break
        url = match.group("url")
        ref = url_to_ref.get(normalize_url(url)) if url else match.group("ref")
        if ref is None or ref not in card_refs:
            return None
        if ref not in refs:
            refs.append(ref)
        position = match.end()
    return refs or None


def _parse_source_roles(
    raw_roles: Any,
    ref_to_url: dict[str, str],
    *,
    card_urls: set[str],
    fixed_urls: set[str],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Validate the extractor's ``source_roles`` against the source card.

    Returns the accepted judgements (first judgement per URL wins inside
    one call; a source whose role is fixed on the card is not judged again)
    and the counters the search node reports: judged, invalid (unknown ref
    or role outside the vocabulary), missing (sources SHOWN on the card,
    not fixed, that the model did not judge — never the citations the cap
    kept off the card) and truncated reasons.
    """
    judged: dict[str, dict[str, str]] = {}
    invalid = truncated = 0
    for item in raw_roles if isinstance(raw_roles, list) else []:
        if not isinstance(item, dict):
            invalid += 1
            continue
        url = ref_to_url.get(_provider_ref_key(item.get("ref", "")), "")
        role = str(item.get("role", "") or "").strip().lower()
        if not url or role not in SOURCE_ROLES:
            invalid += 1
            continue
        if url in judged or url in fixed_urls:
            continue
        reason = " ".join(str(item.get("reason", "") or "").split())
        if len(reason) > SOURCE_ROLE_REASON_CHARS:
            truncated += 1
            reason = reason[:SOURCE_ROLE_REASON_CHARS]
        judged[url] = {"url": url, "role": role, "reason": reason}
    judgeable = {url for url in card_urls if url and url not in fixed_urls}
    counters = {
        "source_role_count": len(judged),
        "source_role_invalid_count": invalid,
        "source_role_missing_count": len(judgeable - set(judged)),
        "source_role_reason_truncated_count": truncated,
    }
    if truncated:
        log.warning(
            "Source roles: %d reason(s) truncated to %d characters (the prompt requests"
            " at most twelve words)",
            truncated,
            SOURCE_ROLE_REASON_CHARS,
        )
    return list(judged.values()), counters


class ClaimExtractionStrategy(ABC):
    """Contract for extracting structured claims from raw search text.

    A claim extractor turns the textual output of one search hit into
    a bounded list of normalised claim dicts that the consolidation
    strategy can later deduplicate and verify. Implementations return
    empty claims for per-source failures so the search node can aggregate
    and surface them, but they must make incomplete / unparsable
    extraction explicit via the non-fatal notice channel rather than
    treating it as a successful empty result.
    """

    @abstractmethod
    def extract(
        self,
        text: str,
        citations: list[str],
        question: str,
        *,
        deadline: float | None = None,
        provider_refs: list[ProviderCitationRef] | None = None,
        text_char_limit: int = 7000,
        citation_cap: int = 8,
        max_claims: int = 8,
        source_url_limit: int = 4,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Extract normalised claims from one search result.

        Args:
            text: Raw search-result text to analyse. Empty / whitespace
                input must short-circuit to an empty result; large
                inputs must be trimmed by the implementation
                (``text_char_limit``).
            citations: Citation URLs that may be resolved from provider
                refs. This is an internal allow-list; the LLM must not
                emit URLs as the binding source.
            question: Original user question. Forwarded into the
                extraction prompt so the model can focus on
                question-relevant claims rather than every fact in
                the source text.
            deadline: Optional absolute monotonic deadline for the
                whole agent run. Implementations should clamp any
                per-call timeout to the remaining budget and raise
                :class:`AgentTimeout` when the budget is exhausted.
            provider_refs: Optional provider-local inline citation map.
                Perplexity uses numeric refs such as ``"2"`` for answer
                citations like ``[2]``. Inqtrix resolves these refs to
                internal source URLs deterministically.
            text_char_limit: Maximum number of characters from
                ``text`` forwarded to the LLM. Defaults to ``7000``
                (COMPACT profile); raise via the report-profile
                tuning bundle. Capped to ``>= 1000`` to prevent
                pathological tiny inputs.
            citation_cap: Maximum number of provider references included
                in the prompt's source card. Bounds prompt size when a
                single search returns many references.
            max_claims: Maximum number of claims returned. The
                implementation may over-extract internally and then
                truncate to this cap. Capped to ``>= 1``.
            source_url_limit: Maximum number of source URLs attached
                to a single claim. Bounds the per-claim payload size
                when many citations support the same statement.
            model: Optional best-effort routing hint. The search graph
                passes the model it resolved for the ``claim_extract`` call
                site (the fast tier by default) so a model-aware extractor
                can stay uniform with the other nodes. Implementations MAY
                ignore it and use their own model. ``None`` means "no hint".
            reasoning_effort: Optional best-effort reasoning-effort hint
                paired with ``model`` (``""`` inherit / ``"none"`` off /
                graded). Implementations that do not reason MAY ignore it.
                The graph only forwards these two kwargs to extractors whose
                ``extract`` actually accepts them, so a custom strategy on the
                older signature keeps working unchanged.

        Returns:
            Tuple ``(claims, prompt_tokens, completion_tokens)``.
            ``claims`` is a list of dicts each with keys
            ``claim_text`` (string), ``claim_type`` (one of
            ``fact``/``actor_claim``/``forecast``), ``polarity`` (one
            of ``affirmed``/``negated``), ``needs_primary`` (bool),
            ``provider_refs`` (validated inline source refs),
            ``source_urls`` (internal URLs resolved from ``provider_refs``),
            ``binding_status`` (``"bound"`` or ``"unbound"``), optional
            ``evidence_snippet`` (short text excerpt that directly supports
            ``claim_text``). The claim carries no model-invented date: the
            search node dates it from the bound citation's ``source_date``
            (or ``unknown``). Token counts
            come from the underlying LLM call; ``0`` when no call ran.

        Raises:
            AgentRateLimited: When the LLM provider escalates a fatal
                rate limit. Other failure modes (timeouts, parse
                errors, provider API errors) return an empty claim list
                and surface the cause via the non-fatal notice mixin so
                the search node can record an ``ALGO-FAIL`` when the
                algorithmic claim path is unusable for the round.
        """

    def consume_source_roles(self) -> list[dict[str, str]]:
        """Return and clear the source roles the last ``extract()`` judged.

        Each entry is ``{"url", "role", "reason"}`` with ``role`` one of
        :data:`~inqtrix.domains.SOURCE_ROLES` and ``url`` an entry of the
        citation allow-list. The search node calls this right after
        ``extract()`` in the same worker thread and pins the judgement in
        the run's source registry. A strategy that does not judge sources
        returns an empty list — every source then stays ``unjudged``
        (visible in the registry and on the trace), never silently
        ``primary``.
        """
        return []


class LLMClaimExtractor(_NonFatalNoticeMixin, ClaimExtractionStrategy):
    """LLM-backed claim extractor used during the search node.

    Converts free-form search-result text into a bounded list of
    structured claims by prompting the configured ``claim_extract_model``.
    Validates each claim against the schema (allowed types, polarity),
    downgrades obvious actor attributions from ``fact`` to
    ``actor_claim``, infers ``needs_primary`` from regex hints when
    the model omitted it, and resolves provider-local refs to internal
    source URLs so the algorithm has a single binding path.

    Failure handling is visible but per-source local: parse errors,
    provider token-limit stops, timeouts and provider API errors yield
    an empty claim list plus an ``ALGO-FAIL`` notice that the search node
    aggregates. Only :class:`AgentRateLimited` propagates so the agent
    can abort consistently on hard rate-limit conditions.
    """

    def __init__(
        self,
        llm: LLMProvider | None,
        claim_extract_model: str,
        claim_extract_timeout: int = CLAIM_EXTRACT_TIMEOUT,
    ) -> None:
        """Bind the underlying LLM and per-call defaults to the extractor.

        Args:
            llm: LLM provider that will perform the extraction calls.
                When ``None``, every :meth:`extract` call short-
                circuits to an empty result and stores a non-fatal
                notice — used by tests and by the env-driven path
                when no LLM provider could be auto-created.
            claim_extract_model: Model identifier passed to the provider's
                ``complete_with_metadata`` call. In env-driven mode
                this is the resolved value of
                ``ModelSettings.effective_claim_extract_model``; in
                Baukasten mode the caller picks an explicit model
                name. Empty string is accepted but typically results
                in the provider falling back to its own default.
            claim_extract_timeout: Per-call timeout (seconds) before
                deadline clamping. Default ``60`` mirrors the
                ``AgentSettings.claim_extract_timeout`` default; tighten
                for faster failure on slow upstreams, raise for
                models with long warmup.

        Example:
            >>> from inqtrix.strategies import LLMClaimExtractor
            >>> extractor = LLMClaimExtractor(llm=None, claim_extract_model="")
            >>> extractor.extract("", [], "question")
            ([], 0, 0)
        """
        self._llm = llm
        self._claim_extract_model = claim_extract_model
        self._claim_extract_timeout = claim_extract_timeout

    def _set_slot(self, name: str, value: Any) -> None:
        """Store a per-call value on the current worker thread (one slot per channel)."""
        setattr(self._notice_state(), name, value)

    def _consume_slot(self, name: str) -> Any:
        """Return and clear a per-call value of the current worker thread."""
        state = self._notice_state()
        value = getattr(state, name, None)
        if hasattr(state, name):
            delattr(state, name)
        return value

    def _set_extraction_metadata(self, metadata: dict[str, Any]) -> None:
        """Store per-call extraction metadata for the current worker thread."""
        self._set_slot("extraction_metadata", dict(metadata))

    def _set_source_roles(self, roles: list[dict[str, str]]) -> None:
        """Store the roles the current call judged for the current worker thread."""
        self._set_slot("source_roles", [dict(role) for role in roles])

    def consume_source_roles(self) -> list[dict[str, str]]:
        """Return and clear the role judgements of the last ``extract()``."""
        roles = self._consume_slot("source_roles")
        return [dict(role) for role in roles] if isinstance(roles, list) else []

    def consume_extraction_metadata(self) -> dict[str, Any]:
        """Return and clear per-call extraction metadata for observability.

        The search node calls this immediately after ``extract()`` in the
        same worker thread, mirroring ``consume_nonfatal_notice()``. The
        metadata is intentionally diagnostic only; the algorithm's control
        flow still depends on extracted claims and non-fatal notices.
        """
        metadata = self._consume_slot("extraction_metadata")
        return dict(metadata) if isinstance(metadata, dict) else {}

    def _structured_output_supported(self, model: str) -> bool:
        """Return whether the LLM can handle the claim schema for *model*.

        Checks structured-output support for the model that will actually run
        this call (the graph-resolved fast-tier model, or the standalone
        default), not a fixed construction-time model.
        """
        checker = getattr(self._llm, "supports_structured_output", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(model=model))
        except TypeError:
            return bool(checker())

    # ------------------------------------------------------------------ #
    # extract
    # ------------------------------------------------------------------ #
    def extract(
        self,
        text: str,
        citations: list[str],
        question: str,
        *,
        deadline: float | None = None,
        provider_refs: list[ProviderCitationRef] | None = None,
        text_char_limit: int = 7000,
        citation_cap: int = 8,
        max_claims: int = 8,
        source_url_limit: int = 4,
        model: str | None = None,
        reasoning_effort: str | None = None,
        fixed_roles: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Run claim extraction for one search result.

        The method validates claim types and polarity, downgrades obvious
        speaker attributions from ``fact`` to ``actor_claim``, infers
        ``needs_primary`` when the model omitted it, and keeps only URLs from
        the provided citation allow-list.

        Args:
            model: The model to run this call on. The search-graph caller
                passes the fast-tier model resolved by ``_resolve_node_llm``
                so claim extraction follows the same routing as every other
                node. When ``None`` (standalone callers), the constructor's
                ``claim_extract_model`` applies.
            reasoning_effort: Per-call reasoning effort, forwarded to the
                provider. ``""`` inherits the provider default, ``"none"``
                forces reasoning off, a graded level turns it on. The fast
                tier's effort flows in here; there is no separate suppression
                path.
            fixed_roles: URL -> role of sources the run already placed (the
                structural floor, an earlier judgement), or ``strategy:<tier>``
                for a custom placement without a role; they are shown on
                the card as fixed and the model is not asked to judge them
                again.

        Returns:
            Tuple of ``(claims, prompt_tokens, completion_tokens)``.

        Raises:
            AgentRateLimited: Propagated so the agent can abort consistently on
                hard rate-limit conditions.
            AgentTimeout: Raised before the call when the absolute deadline is
                already exhausted.
        """
        self._clear_nonfatal_notice()
        self._consume_slot("extraction_metadata")
        self._consume_slot("source_roles")
        if not text.strip():
            return [], 0, 0
        if self._llm is None:
            self._set_extraction_metadata({
                "claim_extraction_mode": "unavailable",
                "claim_extraction_schema": "",
                "claim_extraction_structured_supported": False,
            })
            self._set_nonfatal_notice(
                "ALGO-FAIL claim_extraction: no LLM configured; no structured claims emitted."
            )
            return [], 0, 0
        if deadline is not None:
            _check_deadline(deadline)

        text_char_limit = max(1000, int(text_char_limit or 7000))
        citation_cap = max(1, int(citation_cap or 8))
        max_claims = max(1, int(max_claims or 8))
        source_url_limit = max(1, int(source_url_limit or 4))
        use_model = model or self._claim_extract_model

        normalized_citations = [normalize_url(u) for u in (citations or []) if u]
        known_urls = set(normalized_citations)
        fixed = {normalize_url(u): r for u, r in (fixed_roles or {}).items() if normalize_url(u)}
        source_refs = _source_refs_for_prompt(
            list(provider_refs or []),
            citation_cap=citation_cap,
            fixed_roles=fixed,
        )
        card_urls = {str(row.get("url", "")) for row in source_refs}
        ref_to_url: dict[str, str] = {}
        for ref in provider_refs or []:
            url = normalize_url(ref.url)
            if not url or (known_urls and url not in known_urls):
                continue
            for alias in _provider_ref_aliases(ref):
                ref_to_url.setdefault(alias, url)

        prompt = (
            f"{build_claim_extraction_prompt(max_claims=max_claims)}\n"
            f"Question:\n{(question or '').strip()}\n\n"
            f"Source map:\n{json.dumps(source_refs, ensure_ascii=False)}\n\n"
            f"Text:\n{text[:text_char_limit]}"
        )

        structured_supported = self._structured_output_supported(use_model)
        extraction_mode = (
            "structured_output" if structured_supported else "legacy_text_json"
        )
        extraction_metadata: dict[str, Any] = {
            "claim_extraction_mode": extraction_mode,
            "claim_extraction_schema": (
                _CLAIM_EXTRACTION_SCHEMA_NAME if structured_supported else ""
            ),
            "claim_extraction_structured_supported": structured_supported,
            "input_char_count": len(text),
            "input_prompt_char_count": min(len(text), text_char_limit),
            "input_omitted_chars": max(0, len(text) - text_char_limit),
            "citation_count": len(normalized_citations),
            # Count input URL entries represented by the source card, not a
            # second URL list. Provider refs determine the card's contents.
            "citation_prompt_count": sum(url in card_urls for url in normalized_citations),
            "citation_omitted_count": sum(url not in card_urls for url in normalized_citations),
        }
        self._set_extraction_metadata(extraction_metadata)

        try:
            call_timeout = _bounded_timeout(self._claim_extract_timeout, deadline)

            if structured_supported:
                response = self._llm.complete_structured(
                    prompt,
                    schema=_CLAIM_EXTRACTION_SCHEMA,
                    schema_name=_CLAIM_EXTRACTION_SCHEMA_NAME,
                    schema_description=(
                        "Extract question-relevant factual claims from one search result."
                    ),
                    model=use_model,
                    reasoning_effort=reasoning_effort,
                    timeout=call_timeout,
                    deadline=deadline,
                )
            else:
                response = self._llm.complete_with_metadata(
                    prompt,
                    model=use_model,
                    reasoning_effort=reasoning_effort,
                    timeout=call_timeout,
                    deadline=deadline,
                )

            raw = response.content or ""
            finish_reason = str(getattr(response, "finish_reason", "") or "").strip().lower()
            request_max_tokens = int(getattr(response, "request_max_tokens", 0) or 0)
            request_max_tokens_label: int | str = request_max_tokens or "provider-default"
            if finish_reason in INCOMPLETE_FINISH_REASONS:
                log.warning(
                    "ALGO-FAIL claim_extraction token-limited or non-standard stop "
                    "(model=%s, finish_reason=%s, request_max_tokens=%s, completion_tokens=%s)",
                    use_model,
                    finish_reason,
                    request_max_tokens_label,
                    response.completion_tokens,
                )
                self._set_nonfatal_notice(
                    "ALGO-FAIL claim_extraction: "
                    f"{use_model} stopped with finish_reason={finish_reason} "
                    f"(request_max_tokens={request_max_tokens_label}); "
                    "no structured claims emitted."
                )
                return [], response.prompt_tokens, response.completion_tokens
            if structured_supported:
                parsed = getattr(response, "parsed", None)
                if not isinstance(parsed, dict):
                    log.warning(
                        "ALGO-FAIL claim_extraction structured schema mismatch "
                        "(model=%s, request_max_tokens=%s, completion_tokens=%s)",
                        use_model,
                        request_max_tokens_label,
                        response.completion_tokens,
                    )
                    self._set_nonfatal_notice(
                        "ALGO-FAIL claim_extraction: "
                        f"{use_model} returned structured output "
                        "without a JSON object; no structured claims emitted."
                    )
                    return [], response.prompt_tokens, response.completion_tokens
            else:
                parsed = parse_json_object(raw, fallback={"__parse_failed": True})

            if parsed.get("__parse_failed"):
                log.warning(
                    "ALGO-FAIL claim_extraction invalid JSON "
                    "(model=%s, request_max_tokens=%s, completion_tokens=%s)",
                    use_model,
                    request_max_tokens_label,
                    response.completion_tokens,
                )
                self._set_nonfatal_notice(
                    "ALGO-FAIL claim_extraction: "
                    f"{use_model} returned invalid or incomplete JSON "
                    f"(request_max_tokens={request_max_tokens_label}); "
                    "no structured claims emitted."
                )
                return [], response.prompt_tokens, response.completion_tokens
            raw_claims = parsed.get("claims", [])
            if not isinstance(raw_claims, list):
                log.warning(
                    "ALGO-FAIL claim_extraction missing claims list "
                    "(model=%s, mode=%s, request_max_tokens=%s, completion_tokens=%s)",
                    use_model,
                    extraction_mode,
                    request_max_tokens_label,
                    response.completion_tokens,
                )
                self._set_nonfatal_notice(
                    "ALGO-FAIL claim_extraction: "
                    f"{use_model} returned no claims list "
                    f"(mode={extraction_mode}); no structured claims emitted."
                )
                return [], response.prompt_tokens, response.completion_tokens
            extraction_metadata["claim_extraction_raw_claim_count"] = len(raw_claims)
            self._set_extraction_metadata(extraction_metadata)
            claims: list[dict[str, Any]] = []
            unknown_provider_ref_count = 0
            unbound_claim_count = 0
            rebound_count = 0
            unchecked_count = 0
            url_to_ref = {
                str(row["url"]): str(row["ref"])
                for row in source_refs
                if str(row["ref"]) in ref_to_url
            }
            card_refs = set(url_to_ref.values())

            for item in raw_claims[: max(max_claims * 2, max_claims)]:
                if not isinstance(item, dict):
                    continue
                claim_text = str(item.get("claim_text", "")).strip()
                if len(claim_text) < 12:
                    continue
                evidence_snippet = " ".join(
                    str(item.get("evidence_snippet", "")).strip().split()
                )[:500].strip()

                claim_type = str(item.get("claim_type", "fact")).strip().lower()
                if claim_type not in _CLAIM_TYPES:
                    claim_type = "fact"
                if claim_type == "fact" and _CLAIM_ACTOR_VERB_RE.search(claim_text):
                    claim_type = "actor_claim"

                polarity = str(item.get("polarity", "affirmed")).strip().lower()
                if polarity not in _CLAIM_POLARITY:
                    polarity = "affirmed"

                raw_needs_primary = item.get("needs_primary", None)
                if claim_type != "fact":
                    needs_primary = False
                elif isinstance(raw_needs_primary, bool):
                    needs_primary = raw_needs_primary
                else:
                    needs_primary = bool(_CLAIM_PRIMARY_HINT_RE.search(claim_text))

                provider_ref_values, source_urls, unknown_refs = _resolve_provider_refs(
                    item.get("provider_refs", []),
                    ref_to_url,
                    source_url_limit=source_url_limit,
                )
                if unknown_refs:
                    unknown_provider_ref_count += len(unknown_refs)
                    log.warning("Claim cited unknown provider refs: %s", unknown_refs)
                # The provider's text settles the attribution where the
                # excerpt stands once and a citation closes its passage: a
                # model choice foreign to that citation is rebound.
                closing_refs = _closing_citation_refs(
                    evidence_snippet,
                    text[:text_char_limit],
                    url_to_ref=url_to_ref,
                    card_refs=card_refs,
                )
                if closing_refs is None:
                    unchecked_count += 1
                elif not set(provider_ref_values) & set(closing_refs):
                    provider_ref_values = closing_refs
                    source_urls = list(dict.fromkeys(ref_to_url[ref] for ref in closing_refs))
                    rebound_count += 1
                if not source_urls:
                    unbound_claim_count += 1

                normalized_claim = {
                    "claim_text": claim_text,
                    "claim_type": claim_type,
                    "polarity": polarity,
                    "needs_primary": needs_primary,
                    "source_urls": source_urls[:source_url_limit],
                    "binding_status": "bound" if source_urls else "unbound",
                }
                normalized_claim["provider_refs"] = provider_ref_values[:source_url_limit]
                if evidence_snippet:
                    normalized_claim["evidence_snippet"] = evidence_snippet
                claims.append(normalized_claim)

            returned_claims = claims[:max_claims]
            source_roles, role_counters = _parse_source_roles(
                parsed.get("source_roles", []),
                ref_to_url,
                card_urls=card_urls,
                fixed_urls={url for url in card_urls if url in fixed},
            )
            self._set_source_roles(source_roles)
            extraction_metadata.update(role_counters)
            extraction_metadata["claim_extraction_normalized_claim_count"] = len(
                returned_claims
            )
            extraction_metadata["claim_extraction_filtered_claim_count"] = max(
                0,
                len(raw_claims[: max(max_claims * 2, max_claims)]) - len(returned_claims),
            )
            extraction_metadata["unknown_provider_ref_count"] = unknown_provider_ref_count
            extraction_metadata["unbound_claim_count"] = unbound_claim_count
            extraction_metadata["provider_refs_rebound"] = rebound_count
            extraction_metadata["provider_refs_unchecked"] = unchecked_count
            if rebound_count:
                log.warning(
                    "Claim extraction rebound %d of %d claims to the citation that "
                    "closes their passage in the provider text (model=%s)",
                    rebound_count,
                    len(claims),
                    use_model,
                )
            self._set_extraction_metadata(extraction_metadata)
            return returned_claims, response.prompt_tokens, response.completion_tokens

        except AgentRateLimited:
            raise
        except AgentStructuredOutputError as exc:
            exc_message = str(exc)[:200]
            log.warning(
                "ALGO-FAIL claim_extraction structured_output "
                "(model=%s, error_type=%s)",
                use_model,
                type(exc).__name__,
            )
            self._set_nonfatal_notice(
                "ALGO-FAIL claim_extraction: "
                f"{use_model} returned invalid structured output "
                f"({exc_message}); no structured claims emitted."
            )
            return [], 0, 0
        except NotImplementedError as exc:
            exc_message = str(exc)[:200]
            log.warning(
                "ALGO-FAIL claim_extraction structured_output_missing "
                "(model=%s, error_type=%s)",
                use_model,
                type(exc).__name__,
            )
            self._set_nonfatal_notice(
                "ALGO-FAIL claim_extraction: "
                f"{use_model} advertised structured output but "
                "did not implement it; no structured claims emitted."
            )
            return [], 0, 0
        except AgentModelCapacityError as exc:
            exc_message = str(exc)[:200]
            log.warning(
                "ALGO-FAIL claim_extraction model_capacity "
                "(model=%s, error_type=%s)",
                use_model,
                type(exc).__name__,
            )
            self._set_nonfatal_notice(
                "ALGO-FAIL model_capacity: "
                f"{use_model} cannot satisfy claim extraction "
                f"token capacity ({exc_message}); no structured claims emitted."
            )
            return [], 0, 0
        except (
            OpenAIError,
            AgentTimeout,
            AnthropicAPIError,
            BedrockAPIError,
            AzureOpenAIAPIError,
            AzureFoundryWebSearchAPIError,
        ) as exc:
            exc_label = type(exc).__name__
            exc_message = str(exc)[:200]
            log.warning(
                "ALGO-FAIL claim_extraction provider error "
                "(model=%s, error_type=%s)",
                use_model,
                exc_label,
            )
            self._set_nonfatal_notice(
                "ALGO-FAIL claim_extraction: "
                f"{use_model} failed ({exc_label}: {exc_message}); "
                "no structured claims emitted."
            )
            return [], 0, 0
