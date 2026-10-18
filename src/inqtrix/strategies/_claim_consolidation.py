"""Claim consolidation strategy — consolidate, filter, and format claim ledgers."""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from typing import Any, NamedTuple

from inqtrix.domains import GENERIC_QUERY_TERMS_DE, SOURCE_TIER_WEIGHTS, placement_kind
from inqtrix.prompts import default_claims_prompt_view
from inqtrix.runtime_logging import make_record_id
from inqtrix.scoring import SUPPORTED_STATUSES
from inqtrix.strategies._claim_values import (
    ClaimValues,
    claim_values,
    page_states_claim,
    signature_tokens,
    values_conflict,
)
from inqtrix.strategies._source_tiering import SourceTieringStrategy, quality_from_tiers
from inqtrix.text import NEGATION_TOKENS, STOPWORDS, norm_match_token, tokenize
from inqtrix.urls import domain_from_url, normalize_url, registrable_domain

_STATUS_WEIGHTS: dict[str, float] = {
    "verified": 1.0,
    "contested": 0.7,
    "unverified": 0.35,
}
_DEFAULT_TIER_WEIGHT = SOURCE_TIER_WEIGHTS.get("unknown", 0.35)

SYNDICATION_JACCARD = 0.8
"""Token-set Jaccard similarity from which two evidence snippets count as ONE
syndicated text (a wire story reprinted on two domains is one witness)."""

_SYNDICATION_MIN_TOKENS = 8
"""Snippets shorter than this are too small to call syndicated."""

_ISSUER_STOP_LABELS: frozenset[str] = frozenset({
    "www", "news", "blog", "info", "online", "web", "site", "home", "portal",
    "shop", "mail", "app", "the", "and", "der", "die", "das", "com", "net",
    "org", "media", "press", "presse", "daily", "times", "post", "journal",
})
"""Registrable-domain labels that name a format, not an issuer."""


def _snippet_tokens(text: str) -> set[str]:
    return {norm_match_token(token) for token in tokenize(text)}


def _syndicated(tokens: set[str], counted: list[set[str]]) -> bool:
    """Whether ``tokens`` near-duplicates any already counted snippet."""
    if len(tokens) < _SYNDICATION_MIN_TOKENS:
        return False
    for other in counted:
        if len(other) < _SYNDICATION_MIN_TOKENS:
            continue
        union = len(tokens | other)
        if union and len(tokens & other) / union >= SYNDICATION_JACCARD:
            return True
    return False


def issuer_label(url: str) -> str:
    """The registrable domain's first label as a matchable issuer token
    (``https://www.kfw.de/x`` -> ``kfw``, umlauts folded); ``""`` when the
    label names a format (``news``), is numeric or shorter than three chars."""
    registered = registrable_domain(url)
    label = norm_match_token(registered.split(".")[0]) if registered else ""
    if len(label) < 3 or label.isdigit() or label in _ISSUER_STOP_LABELS:
        return ""
    return label


def issuer_match(claim_text: str, urls: list[str]) -> str:
    """Return the issuer label a fact claim names itself (``""`` when none).

    A claim that names the publisher of its own source ("Die KfW foerdert …"
    from ``kfw.de``) is a statement of the issuer: the second deterministic
    primary basis next to a primary role, language- and country-neutral. The
    caller passes only the witnesses the run could not place (unknown
    tier); a false positive on a dictionary-word domain stays visible as
    ``primary_basis="issuer:<label>"`` and in the claim's ``status_reason``.
    """
    tokens = _snippet_tokens(claim_text)
    for url in urls:
        label = issuer_label(url)
        if label and label in tokens:
            return label
    return ""


def _mark_value_conflicts(members: list[tuple[ClaimValues, dict[str, Any]]]) -> None:
    """Contest every two claims of one signature whose values contradict.

    Both sides become ``contested`` and name each other: two sources that
    put different dates, percentages or amounts on the same statement are a
    conflict a reader must see, never two verified facts. Each side counts
    the other's supporting entries as its contradiction. Only claims with
    affirmed support take part; a negated claim contradicts nothing by its
    value.
    """
    for index, (values, claim) in enumerate(members):
        for other_values, other in members[index + 1:]:
            if claim["support_count"] < 1 or other["support_count"] < 1:
                continue
            if not values_conflict(values, other_values):
                continue
            for side, opposite in ((claim, other), (other, claim)):
                if side["status"] != "contested":
                    side["status"] = "contested"
                    side["status_reason"] = "conflicting values"
                    side["verification_basis"] = "contested"
                side["conflicting_claim_ids"].append(opposite["claim_id"])
                side["contradict_count"] += opposite["support_count"]
                side["contradicting_evidence_ids"] = list(dict.fromkeys([
                    *side["contradicting_evidence_ids"],
                    *opposite["supporting_evidence_ids"],
                ]))[:24]


_STRUCTURE_REASONS: dict[str, str] = {
    "primary_source": "primary evidence present",
    "cross_checked": "independently corroborated",
    "quality_source": "backed by a high-quality source",
}


class _Unit(NamedTuple):
    """One URL an affirmed witness binds a claim to: the provider answer
    (``answer``, its query id) that bound it, the source's tier, and the
    source's own text when the provider returned it (``page_text``)."""

    url: str
    domain: str
    answer: str
    tier: str
    claim_text: str
    page_text: str


def _independent_witnesses(units: list[_Unit]) -> int:
    """How many independent witnesses *units* hold.

    Two witnesses are independent when they come from different provider
    answers AND sit on different registrable domains of a non-low source:
    one provider sentence citing two pages is one witness, and so are two
    answers citing one site. The count is the smaller of the number of
    answers and of domains among such units; at two, a pair differing in
    both always exists (if every pair shared an answer or a domain, a
    second answer and a second domain would still force a pair that shares
    neither).
    """
    usable = [unit for unit in units if unit.domain and unit.tier != "low"]
    return min(
        len({unit.answer for unit in usable}),
        len({unit.domain for unit in usable}),
    )


def _support_structure(
    units: list[_Unit],
    *,
    claim_type: str,
    claim_text: str,
    tier_reason_by_url: dict[str, str],
) -> tuple[str, str]:
    """The strongest support *units* give a claim, first match wins:
    ``primary_source`` (a primary role, or a fact claim naming the issuer of
    a source the run could not place), ``cross_checked`` (two independent
    witnesses), ``quality_source`` (a mainstream source), else ``""``.
    Returns it with the primary basis (``role``, ``issuer:<label>`` or
    ``""``)."""
    tiers = {unit.url: unit.tier for unit in units}
    primary_basis = "role" if "primary" in tiers.values() else ""
    if not primary_basis and claim_type == "fact":
        # A source the extractor judged as a newsroom, an interested party
        # or a forum is never "the issuer speaking" because it names itself.
        issuer = issuer_match(
            claim_text,
            [
                url for url, tier in tiers.items()
                if tier == "unknown"
                and placement_kind(tier_reason_by_url.get(url, ""))[0] == "unjudged"
            ],
        )
        if issuer:
            primary_basis = f"issuer:{issuer}"
    if primary_basis:
        return "primary_source", primary_basis
    if _independent_witnesses(units) >= 2:
        return "cross_checked", ""
    if "mainstream" in tiers.values():
        return "quality_source", ""
    return "", ""


def _checked_units(units: list[_Unit]) -> list[_Unit]:
    """The units whose source's own text states the claim
    (:func:`~inqtrix.strategies._claim_values.page_states_claim`); a page
    text that near-duplicates an already counted one (a wire story on two
    sites) counts once."""
    counted: list[set[str]] = []
    checked: list[_Unit] = []
    for unit in units:
        if not page_states_claim(unit.claim_text, unit.page_text):
            continue
        tokens = _snippet_tokens(unit.page_text)
        if _syndicated(tokens, counted):
            continue
        counted.append(tokens)
        checked.append(unit)
    return checked


_SUPPORT_BONUS_SCALE = math.log1p(10)
_PHASE2_MAX_PER_DOMAIN = 3

log = logging.getLogger("inqtrix")


class ClaimConsolidationStrategy(ABC):
    """Consolidate, filter, and format claim ledgers."""

    @abstractmethod
    def focus_stems_from_question(self, question: str) -> set[str]:
        """Build normalized focus stems from the user question.

        Args:
            question: Original user question.

        Returns:
            A set of normalized stems used to keep claims on-topic.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.focus_stems_from_question("Was kostet die GKV-Reform?")
            {"gkv", "reform", ...}
        """
        ...

    @abstractmethod
    def claim_matches_focus_stems(
        self,
        claim_text: str,
        focus_stems: set[str],
    ) -> bool:
        """Decide whether a claim is relevant to the question focus.

        Args:
            claim_text: Candidate claim text.
            focus_stems: Normalized stems produced from the question.

        Returns:
            ``True`` when the claim should remain in scope.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.claim_matches_focus_stems("Die Reform kostet mehr", {"reform"})
            True
        """
        ...

    @abstractmethod
    def claim_signature(self, text: str) -> str:
        """Create a stable signature for deduplicating claim text.

        Args:
            text: Raw claim text.

        Returns:
            A normalized signature used for grouping semantically similar
            claims.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.claim_signature("Der Beitrag steigt um 5 Prozent")
            'beitrag steigt'
        """
        ...

    @abstractmethod
    def consolidate(self, claim_ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge a raw claim ledger into consolidated claim groups.

        Args:
            claim_ledger: Raw claim entries gathered across rounds.

        Returns:
            Consolidated claim records with support/contradiction counts.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.consolidate([{"claim_text": "..."}])
            [{"claim_text": "...", "support_count": 1, ...}]
        """
        ...

    @abstractmethod
    def materialize(
        self,
        consolidated: list[dict[str, Any]],
        *,
        max_total: int = 24,
        max_unverified: int = 8,
    ) -> list[dict[str, Any]]:
        """Select the answer-facing subset of consolidated claims.

        Args:
            consolidated: Consolidated claim groups.
            max_total: Maximum number of claims to keep.
            max_unverified: Maximum number of unverified claims to keep.

        Returns:
            Materialized claims ready for prompts and exports.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.materialize(consolidated_claims, max_total=10)
            [{"claim_text": "..."}, ...]
        """
        ...

    @abstractmethod
    def quality_metrics(
        self,
        consolidated: list[dict[str, Any]],
    ) -> tuple[dict[str, int], float, int, int]:
        """Aggregate quality metrics for consolidated claims.

        Args:
            consolidated: Consolidated claim groups.

        Returns:
            Tuple of ``(status_counts, quality_score,
            needs_primary_total, needs_primary_supported)``.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.quality_metrics(consolidated_claims)
            ({"verified": 1, "attributed": 1, ...}, 0.8, 1, 1)
        """
        ...

    @abstractmethod
    def claims_prompt_view(
        self,
        consolidated: list[dict[str, Any]],
        max_items: int = 16,
    ) -> str:
        """Render consolidated claims into prompt-friendly text.

        Args:
            consolidated: Consolidated claim groups.
            max_items: Maximum number of claims to render.

        Returns:
            A textual summary that can be embedded into prompts.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.claims_prompt_view(consolidated_claims, max_items=5)
            '- Claim 1\\n- Claim 2'
        """
        ...

    @abstractmethod
    def select_answer_citations(
        self,
        consolidated: list[dict[str, Any]],
        all_citations: list[str],
        *,
        max_items: int,
        source_tiering: SourceTieringStrategy | None = None,
    ) -> list[str]:
        """Choose the citations that should be forwarded to answer synthesis.

        Args:
            consolidated: Consolidated claim groups.
            all_citations: Full citation list collected during research.
            max_items: Hard cap for citations included in the answer prompt.
            source_tiering: Optional tiering strategy used to bias ranking
                toward higher-quality domains. When omitted, the claim's
                pre-computed tier counts are used as fallback.

        Returns:
            Citation URLs ordered for answer synthesis.

        Raises:
            NotImplementedError: Implementations must provide the logic.

        Example:
            >>> strategy.select_answer_citations(consolidated_claims, citations, max_items=8)
            ['https://example.com/report']
        """
        ...


class DefaultClaimConsolidator(ClaimConsolidationStrategy):
    """Group claims by signature and derive answer-facing claim quality.

    The consolidator is the bridge between raw extracted claims and the
    smaller, quality-scored claim set used in evaluate and answer. It keeps
    polarity-aware support counts, classifies each group as verified,
    contested, or unverified, and then materializes a bounded subset for
    prompts and exports.
    """

    def __init__(self, source_tiering: SourceTieringStrategy) -> None:
        """Bind a source-tiering strategy to the consolidator.

        Args:
            source_tiering: Strategy used to compute per-claim
                source-tier counts (consumed inside :meth:`consolidate`
                to derive the verified / contested / unverified
                status). Reused as-is; the consolidator does not
                wrap or shadow it.
        """
        self._tiering = source_tiering

    # ------------------------------------------------------------------ #
    # focus_stems_from_question
    # ------------------------------------------------------------------ #
    def focus_stems_from_question(self, question: str) -> set[str]:
        """Derive prefix stems from the question for relevance filtering.

        Implements the contract from
        :meth:`ClaimConsolidationStrategy.focus_stems_from_question`.
        Removes generic / stopword tokens, then truncates remaining
        tokens to a 6-character prefix so morphological variants
        ("Zahnarzt" / "Zahnaerzte" / "Zahnbehandlung") collapse to a
        single stem. Adds a hand-tuned stem set for German health-
        policy keywords (``zahn*``, ``privat*``, ``gkv``,
        ``krankenkass*``) where the auto-stemming would otherwise
        miss obvious matches.

        Args:
            question: User question to analyse. May contain mixed
                case and punctuation; tokenised internally.

        Returns:
            Set of lower-cased stem strings, each at least 4
            characters. Empty set when the question has no usable
            tokens (very short / all stopwords).
        """
        stems: set[str] = set()
        q_norm = norm_match_token(question)

        for tok in tokenize(question):
            nt = norm_match_token(tok)
            if len(nt) < 4:
                continue
            if nt in STOPWORDS:
                continue
            if nt in GENERIC_QUERY_TERMS_DE:
                continue
            stems.add(nt[:6] if len(nt) > 6 else nt)

        if "zahn" in q_norm:
            stems.update({"zahn", "zahnar", "zahnaer", "zahnbe", "zahnlei"})
        if "privatis" in q_norm or "privat" in q_norm:
            stems.update({"privat", "privati", "privatis"})
        if "gkv" in q_norm or "krankenkass" in q_norm:
            stems.update({"gkv", "krankenk"})

        return {s for s in stems if len(s) >= 4}

    # ------------------------------------------------------------------ #
    # claim_matches_focus_stems
    # ------------------------------------------------------------------ #
    def claim_matches_focus_stems(self, claim_text: str, focus_stems: set[str]) -> bool:
        """Test whether at least one claim token starts with a focus stem.

        Implements the contract from
        :meth:`ClaimConsolidationStrategy.claim_matches_focus_stems`.
        Checks each claim token (length ``>= 4`` after normalisation)
        for a prefix match against the focus stem set.

        Args:
            claim_text: Claim text to test. Tokenised and normalised
                internally.
            focus_stems: Set of stems from
                :meth:`focus_stems_from_question`. An empty set is
                treated as "no filter" (every claim matches).

        Returns:
            ``True`` when at least one normalised claim token starts
            with a focus stem, or when ``focus_stems`` is empty.
            ``False`` otherwise.
        """
        if not focus_stems:
            return True
        for tok in tokenize(claim_text):
            nt = norm_match_token(tok)
            if len(nt) < 4:
                continue
            for st in focus_stems:
                if nt.startswith(st):
                    return True
        return False

    # ------------------------------------------------------------------ #
    # claim_signature
    # ------------------------------------------------------------------ #
    def claim_signature(self, text: str) -> str:
        """Compute a stable dedup signature for one claim string.

        Implements the contract from
        :meth:`ClaimConsolidationStrategy.claim_signature`. Leaves out the
        values the claim states (dates, percentages, amounts; see
        :mod:`inqtrix.strategies._claim_values`), drops stopwords,
        negation tokens and very short tokens, then keeps the first 16
        informative tokens joined by spaces. A token with a digit and an
        upper-case Roman numeral are identity ("Artikel 50", "Abs. 1a",
        "Kapitel II") and survive the length filter. Negation tokens are
        dropped intentionally so an affirmed and a contradicted version
        of the same claim share the signature and are tracked together
        via the polarity field instead; values are dropped so two
        claims that differ only in a value share it too and the
        consolidator can compare them.

        Args:
            text: Claim text to fingerprint. May contain mixed case
                and punctuation; tokenised internally.

        Returns:
            Lower-cased space-joined token string suitable as a dict
            key. Empty string when ``text`` has no usable tokens.
        """
        tokens = signature_tokens(text)
        cleaned = [
            token for token, identity in tokens
            if identity
            or (len(token) > 2 and token not in NEGATION_TOKENS and token not in STOPWORDS)
        ]
        if not cleaned:
            cleaned = [token for token, _ in tokens if token not in NEGATION_TOKENS]
        return " ".join(cleaned[:16]).strip()

    # ------------------------------------------------------------------ #
    # consolidate
    # ------------------------------------------------------------------ #
    def consolidate(self, claim_ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse ledger entries with the same normalized claim signature.

        Claims are first bucketed by signature, split by the values they
        state (:func:`~inqtrix.strategies._claim_values.claim_values`), and
        each split is merged into a single representative record with
        source-tier metadata and a derived status. ``verification_basis``
        is the support structure over all affirmed witnesses
        (``primary_source``, ``cross_checked``, ``quality_source``, or
        ``missing_primary_source``/``weak_evidence`` without support). The
        status says what was checked: ``verified`` when the source texts a
        provider returned state the claim on their own, ``attributed`` when
        only the provider's answer ties the claim to its sources (a tier is
        who speaks, never what a page says). Purely negated evidence is
        kept visible via ``contradict_count`` but supports nothing. Two
        splits of one signature whose values contradict each other
        (:func:`values_conflict`) are both contested and name each other in
        ``conflicting_claim_ids``.

        Contested claims sort first: they are few, and a reader must see
        both sides before any budget cuts the list.
        """
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in claim_ledger:
            sig = (
                str(claim.get("signature", "")).strip()
                or self.claim_signature(str(claim.get("claim_text", "")))
            )
            if not sig:
                continue
            groups[sig].append(claim)

        consolidated: list[dict[str, Any]] = []
        for sig, group in groups.items():
            # A value is part of what a claim says: within one signature,
            # claims with different values are different statements.
            subgroups: dict[str, tuple[ClaimValues, list[dict[str, Any]]]] = {}
            for claim in group:
                values = claim_values(str(claim.get("claim_text", "")))
                subgroups.setdefault(values.key, (values, []))[1].append(claim)
            members = [
                (values, self._merge_subgroup(sig, value_key, values, entries))
                for value_key, (values, entries) in subgroups.items()
            ]
            _mark_value_conflicts(members)
            consolidated.extend(record for _, record in members)

        order = {"contested": 0, "verified": 1, "attributed": 1, "unverified": 2}
        consolidated.sort(
            key=lambda c: (
                order.get(str(c.get("status", "unverified")), 3),
                -int(c.get("support_count", 0)),
                int(c.get("contradict_count", 0)),
            ),
        )
        return consolidated

    def _merge_subgroup(
        self,
        sig: str,
        value_key: str,
        values: ClaimValues,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Merge the ledger entries of one signature that state the same
        values into one consolidated claim with its support and status.

        Syndication runs here, inside the value subgroup: a reprint is the
        same text with the same values, never the other side of a value
        conflict."""
        claim_type = Counter(
            str(e.get("claim_type", "fact")) for e in entries
        ).most_common(1)[0][0]
        raw_needs_primary = any(bool(e.get("needs_primary", False)) for e in entries)
        needs_primary = raw_needs_primary and claim_type == "fact"
        contradict_count = sum(1 for e in entries if e.get("polarity") == "negated")

        source_urls: list[str] = []
        source_ids: list[str] = []
        citation_ids: list[str] = []
        citation_set: list[dict[str, Any]] = []
        supporting_evidence_ids: list[str] = []
        contradicting_evidence_ids: list[str] = []
        member_claim_ids: list[str] = []
        member_claim_texts: list[str] = []
        evidence_snippets: list[str] = []
        rounds_seen: list[int] = []
        supporting_domains: set[str] = set()
        tier_by_url: dict[str, str] = {}
        tier_reason_by_url: dict[str, str] = {}
        for e in entries:
            for raw_url, raw_tier in (e.get("source_tiers") or {}).items():
                n = normalize_url(str(raw_url))
                if n and raw_tier and n not in tier_by_url:
                    tier_by_url[n] = str(raw_tier)
            for raw_url, raw_reason in (e.get("source_tier_reasons") or {}).items():
                n = normalize_url(str(raw_url))
                if n and raw_reason and n not in tier_reason_by_url:
                    tier_reason_by_url[n] = str(raw_reason)

        def _tier_of(url: str) -> str:
            # The ledger's pinned role (structural floor or extractor
            # judgement) is the truth; the strategy only covers URLs the
            # run never registered.
            return tier_by_url.get(url) or self._tiering.tier_for_url(url)

        # Witnesses: an affirmed entry whose snippet near-duplicates an
        # already counted one is the same text again (a reprint), not a
        # second witness — it keeps its evidence id but adds no support
        # and no independent domain. Every URL a witness is bound to is one
        # unit of support: which provider answer bound the claim to which
        # source, and that source's own text when the provider returned it.
        counted_snippets: list[set[str]] = []
        support_count = syndicated_count = 0
        units: list[_Unit] = []
        for e in entries:
            witness = False
            if e.get("polarity") == "affirmed":
                tokens = _snippet_tokens(str(e.get("evidence_snippet", "")))
                if _syndicated(tokens, counted_snippets):
                    syndicated_count += 1
                else:
                    counted_snippets.append(tokens)
                    support_count += 1
                    witness = True
            raw_claim_id = str(e.get("raw_claim_id", "")).strip()
            if raw_claim_id and raw_claim_id not in member_claim_ids:
                member_claim_ids.append(raw_claim_id)
            claim_text = " ".join(str(e.get("claim_text", "")).strip().split())
            if claim_text and claim_text not in member_claim_texts:
                member_claim_texts.append(claim_text[:500])
            evidence = " ".join(
                str(e.get("evidence_snippet", "")).strip().split()
            )
            if evidence and evidence not in evidence_snippets:
                evidence_snippets.append(evidence[:500])
            try:
                rounds_seen.append(int(e.get("round", 0)))
            except (TypeError, ValueError):
                pass
            page_texts = e.get("source_texts") or {}
            for u in e.get("source_urls", []):
                n = normalize_url(str(u))
                if n and n not in source_urls:
                    source_urls.append(n)
                if n and witness:
                    domain = registrable_domain(n)
                    if domain:
                        supporting_domains.add(domain)
                    units.append(_Unit(
                        url=n,
                        domain=domain,
                        answer=str(e.get("query_id", "") or ""),
                        tier=_tier_of(n),
                        claim_text=str(e.get("claim_text", "")),
                        page_text=str(page_texts.get(n, "") or ""),
                    ))
            for source_id in e.get("source_ids", []) or []:
                source_id_str = str(source_id).strip()
                if source_id_str and source_id_str not in source_ids:
                    source_ids.append(source_id_str)
            for citation_id in e.get("citation_ids", []) or []:
                citation_id_str = str(citation_id).strip()
                if citation_id_str and citation_id_str not in citation_ids:
                    citation_ids.append(citation_id_str)
            for citation in e.get("citation_set", []) or []:
                if isinstance(citation, dict) and citation not in citation_set:
                    citation_set.append(dict(citation))
            for evidence_id in e.get("evidence_ids", []) or []:
                evidence_id_str = str(evidence_id).strip()
                if not evidence_id_str:
                    continue
                if e.get("polarity") == "negated":
                    if evidence_id_str not in contradicting_evidence_ids:
                        contradicting_evidence_ids.append(evidence_id_str)
                elif evidence_id_str not in supporting_evidence_ids:
                    supporting_evidence_ids.append(evidence_id_str)

        tier_counts, _ = quality_from_tiers(_tier_of(url) for url in source_urls)
        longest_text = max((str(e.get("claim_text", "")) for e in entries), key=len, default="")
        # The structure is read over ALL witnesses, whether or not a source
        # returned its own text, so the research loop sees the same support
        # from every provider. Page texts only decide what was checked.
        structure, primary_basis = _support_structure(
            units,
            claim_type=claim_type,
            claim_text=longest_text,
            tier_reason_by_url=tier_reason_by_url,
        )
        checked = _checked_units(units)
        checked_structure, _ = _support_structure(
            checked,
            claim_type=claim_type,
            claim_text=longest_text,
            tier_reason_by_url=tier_reason_by_url,
        )
        supporting_domain_count = len(supporting_domains)
        independent_support_count = _independent_witnesses(units)

        if support_count > 0 and contradict_count > 0:
            status = "contested"
            reason = "contradictory evidence"
            verification_basis = "contested"
        elif structure:
            # Verified only when the checked texts carry the claim on their
            # own; otherwise the provider's attribution is all there is.
            status = "verified" if checked_structure else "attributed"
            reason = "; ".join([
                (
                    f"issuer identifies itself ({primary_basis.removeprefix('issuer:')})"
                    if primary_basis.startswith("issuer:")
                    else _STRUCTURE_REASONS[structure]
                ),
                (
                    "checked against source text"
                    if checked_structure
                    else "attributed by the search provider"
                ),
            ])
            verification_basis = structure
        elif needs_primary:
            status = "unverified"
            reason = "no primary evidence"
            verification_basis = "missing_primary_source"
        else:
            status = "unverified"
            reason = "evidence too thin"
            verification_basis = "weak_evidence"

        representative = max(
            (str(e.get("claim_text", "")).strip() for e in entries),
            key=len,
            default="",
        )
        return {
            "claim_id": (
                make_record_id("claim", sig, value_key)
                if value_key
                else make_record_id("claim", sig)
            ),
            "signature": sig,
            "claim_text": representative,
            "claim_type": claim_type,
            "needs_primary": needs_primary,
            "support_count": support_count,
            "supporting_source_count": len(source_ids),
            "supporting_domain_count": supporting_domain_count,
            "independent_support_count": independent_support_count,
            "supporting_evidence_ids": supporting_evidence_ids[:24],
            "contradicting_evidence_ids": contradicting_evidence_ids[:24],
            "contradict_count": contradict_count,
            "status": status,
            "status_reason": reason,
            "verification_basis": verification_basis,
            "source_urls": source_urls[:8],
            "source_ids": source_ids[:8],
            "citation_ids": citation_ids[:16],
            "citation_set": citation_set[:16],
            "member_claim_ids": member_claim_ids[:24],
            "member_claim_texts": member_claim_texts[:12],
            "evidence_snippet": evidence_snippets[0] if evidence_snippets else "",
            "evidence_snippets": evidence_snippets[:5],
            "round_first_seen": min(rounds_seen) if rounds_seen else 0,
            "round_last_updated": max(rounds_seen) if rounds_seen else 0,
            "source_tier_counts": tier_counts,
            "primary_basis": primary_basis,
            "syndicated_count": syndicated_count,
            "values": [value.label() for value in (*values.values, *values.scope)],
            "checked_urls": list(dict.fromkeys(unit.url for unit in checked)),
            "conflicting_claim_ids": [],
        }

    # ------------------------------------------------------------------ #
    # materialize
    # ------------------------------------------------------------------ #
    def materialize(
        self,
        consolidated: list[dict[str, Any]],
        *,
        max_total: int = 24,
        max_unverified: int = 8,
    ) -> list[dict[str, Any]]:
        """Select the bounded claim subset forwarded to later nodes.

        Contested claims come first, then supported ones (verified and
        attributed alike, in the consolidation's order, so the selection is
        the same whether or not a provider returned source text): both are
        always preferred, and the few contested claims must never fall off
        behind the many supported ones (a value conflict has two sides, and
        a reader needs both). Unverified claims are only kept up to
        ``max_unverified`` and are biased toward groups that already have
        at least some quality-source backing.
        """
        if not consolidated:
            return []
        max_total = int(max_total or 0)
        max_unverified = int(max_unverified or 0)
        if max_total <= 0:
            return []

        supported: list[dict[str, Any]] = []
        contested: list[dict[str, Any]] = []
        unverified: list[dict[str, Any]] = []
        for c in consolidated:
            st = str(c.get("status", "unverified"))
            if st in SUPPORTED_STATUSES:
                supported.append(c)
            elif st == "contested":
                contested.append(c)
            else:
                unverified.append(c)

        if max_unverified <= 0:
            out = contested + supported
            return out[:max_total]

        unverified_high_tier: list[dict[str, Any]] = []
        unverified_unknown_only: list[dict[str, Any]] = []
        for c in unverified:
            tiers = c.get("source_tier_counts", {}) or {}
            has_any_quality = any(
                int(tiers.get(k, 0) or 0) > 0
                for k in ("primary", "mainstream", "stakeholder")
            )
            if has_any_quality:
                unverified_high_tier.append(c)
            else:
                unverified_unknown_only.append(c)

        kept_unverified = (unverified_high_tier + unverified_unknown_only)[:max_unverified]
        out = contested + supported + kept_unverified
        return out[:max_total]

    # ------------------------------------------------------------------ #
    # quality_metrics
    # ------------------------------------------------------------------ #
    def quality_metrics(
        self,
        consolidated: list[dict[str, Any]],
    ) -> tuple[dict[str, int], float, int, int]:
        """Compute claim-status counts and the aggregate claim quality score.

        The score weights supported claims by the depth of their support
        structure, whether a source text was checked or the provider only
        attributes the claim (the counts say which). Independent cross-checks
        carry full weight, a primary source stays strong, and a single
        quality source is useful but intentionally weaker than either.
        ``needs_primary_supported`` counts the claims that need a primary
        source and have support.
        """
        counts: dict[str, int] = {
            "verified": 0, "attributed": 0, "contested": 0, "unverified": 0,
        }
        needs_primary_total = 0
        needs_primary_supported = 0
        score = 0.0
        for claim in consolidated:
            status = str(claim.get("status", "unverified"))
            if status not in counts:
                status = "unverified"
            counts[status] += 1
            if status in SUPPORTED_STATUSES:
                basis = str(claim.get("verification_basis", "") or "")
                if basis == "cross_checked":
                    score += 1.0
                elif basis == "primary_source":
                    score += 0.9
                elif basis == "quality_source":
                    score += 0.7
                else:
                    score += 0.8
            elif status == "contested":
                score += 0.5
            if bool(claim.get("needs_primary", False)):
                needs_primary_total += 1
                if status in SUPPORTED_STATUSES:
                    needs_primary_supported += 1

        total = len(consolidated)
        if total == 0:
            return counts, 0.0, needs_primary_total, needs_primary_supported
        return counts, round(score / total, 3), needs_primary_total, needs_primary_supported

    # ------------------------------------------------------------------ #
    # claims_prompt_view
    # ------------------------------------------------------------------ #
    def claims_prompt_view(
        self,
        consolidated: list[dict[str, Any]],
        max_items: int = 16,
    ) -> str:
        """Render consolidated claims into the answer-prompt block.

        Thin delegate to
        :func:`~inqtrix.strategies._claim_consolidation.default_claims_prompt_view`
        — kept here so that custom subclasses can override the rendering
        without re-implementing dedup/consolidation. Implements the
        contract from
        :meth:`ClaimConsolidationStrategy.claims_prompt_view`.

        Args:
            consolidated: Output of :meth:`consolidate`. Caller is
                responsible for trimming to the desired claim
                population (e.g. via :meth:`materialize`) before
                rendering.
            max_items: Maximum number of claims rendered into the
                returned string. Defaults to ``16``; the answer
                composer typically passes
                ``ReportProfileTuning.answer_claim_prompt_items``.

        Returns:
            Multi-line string suitable for inlining into the answer
            prompt's ``Claims:`` section. Empty string when
            ``consolidated`` is empty.
        """
        return default_claims_prompt_view(consolidated, max_items=max_items)

    # ------------------------------------------------------------------ #
    # select_answer_citations
    # ------------------------------------------------------------------ #
    def select_answer_citations(
        self,
        consolidated: list[dict[str, Any]],
        all_citations: list[str],
        *,
        max_items: int,
        source_tiering: SourceTieringStrategy | None = None,
    ) -> list[str]:
        """Pick citation URLs for final answer synthesis.

        Ranking combines claim status (verified/contested/unverified) with
        source-tier weights so high-trust claims backed by mainstream or
        stakeholder domains can rank ahead of unverified claims that happen
        to cite a primary domain. Verified claims still dominate because
        ``status_weight`` is the primary multiplier; tier only re-orders
        within each status class. ``support_count`` adds a log-scaled bonus,
        ``contradict_count`` a bounded penalty.
        """
        max_items = max(0, int(max_items or 0))
        if max_items == 0:
            return []

        selected: list[str] = []
        seen: set[str] = set()

        def _add(url: str) -> None:
            n = normalize_url(str(url))
            if not n or n in seen:
                return
            seen.add(n)
            selected.append(n)

        def _tier_weight_for_url(url: str) -> float:
            if source_tiering is None:
                return _DEFAULT_TIER_WEIGHT
            tier = source_tiering.tier_for_url(url)
            return SOURCE_TIER_WEIGHTS.get(tier, _DEFAULT_TIER_WEIGHT)

        def _best_tier_weight(claim: dict[str, Any]) -> float:
            urls = [str(u) for u in (claim.get("source_urls") or []) if u]
            if source_tiering is not None and urls:
                return max(
                    (_tier_weight_for_url(u) for u in urls),
                    default=_DEFAULT_TIER_WEIGHT,
                )
            counts = claim.get("source_tier_counts") or {}
            best = 0.0
            for tier, count in counts.items():
                if int(count or 0) > 0:
                    w = SOURCE_TIER_WEIGHTS.get(str(tier), 0.0)
                    if w > best:
                        best = w
            return best or _DEFAULT_TIER_WEIGHT

        def _combined_score(claim: dict[str, Any]) -> float:
            status = str(claim.get("status", "unverified"))
            status_weight = _STATUS_WEIGHTS.get(status, _STATUS_WEIGHTS["unverified"])
            tier_weight = _best_tier_weight(claim)
            support = max(0, int(claim.get("support_count", 0) or 0))
            contradict = max(0, int(claim.get("contradict_count", 0) or 0))
            support_bonus = math.log1p(support) / _SUPPORT_BONUS_SCALE
            contradict_malus = min(0.3, contradict * 0.1)
            return (status_weight * tier_weight) + support_bonus - contradict_malus

        def _sorted_urls(claim: dict[str, Any]) -> list[str]:
            urls = [str(u) for u in (claim.get("source_urls") or []) if u]
            if source_tiering is None:
                return urls
            return sorted(urls, key=lambda u: -_tier_weight_for_url(u))

        seen_domains: set[str] = set()
        domain_counts: Counter[str] = Counter()

        def _add_with_domain(url: str) -> bool:
            n = normalize_url(str(url))
            if not n or n in seen:
                return False
            d = domain_from_url(n)
            if d and d in seen_domains:
                return False
            seen.add(n)
            if d:
                seen_domains.add(d)
                domain_counts[d] += 1
            selected.append(n)
            return True

        def _add_with_domain_cap(url: str) -> bool:
            n = normalize_url(str(url))
            if not n or n in seen:
                return False
            d = domain_from_url(n)
            if d and domain_counts[d] >= _PHASE2_MAX_PER_DOMAIN:
                return False
            seen.add(n)
            if d:
                domain_counts[d] += 1
            selected.append(n)
            return True

        if consolidated:
            ranked_claims = sorted(
                list(consolidated),
                key=lambda c: (
                    -_combined_score(c),
                    -int(c.get("support_count", 0) or 0),
                    int(c.get("contradict_count", 0) or 0),
                ),
            )

            # Phase 1 — Domain-Diversity: at most one URL per claim, and only
            # if the URL's domain has not been seen yet. Status-neutral; the
            # ranking via _combined_score already handles verified/unverified
            # priority (status_weight 1.0 / 0.7 / 0.35).
            for claim in ranked_claims:
                for u in _sorted_urls(claim):
                    if _add_with_domain(u):
                        if len(selected) >= max_items:
                            return selected[:max_items]
                        break

            # Phase 2 — Fill: top up with additional URLs from already-seen
            # domains, in claim-score order, but cap per-domain contribution
            # at _PHASE2_MAX_PER_DOMAIN to preserve diversity under heavy
            # concentration on a single authoritative source.
            for claim in ranked_claims:
                for u in _sorted_urls(claim):
                    _add_with_domain_cap(u)
                    if len(selected) >= max_items:
                        return selected[:max_items]

        # Phase 3 — Fallback: if the claim ledger was too sparse, supplement
        # from the raw all_citations pool. Honour the per-domain cap so
        # fallback cannot reintroduce dominance of a single domain.
        if len(selected) < min(15, max_items):
            fallback_candidates: list[tuple[float, int, str]] = []
            for index, u in enumerate(all_citations):
                normalized = normalize_url(str(u))
                if not normalized:
                    continue
                if source_tiering is not None:
                    tier = source_tiering.tier_for_url(normalized)
                    if tier == "low":
                        continue
                    weight = SOURCE_TIER_WEIGHTS.get(tier, _DEFAULT_TIER_WEIGHT)
                else:
                    weight = _DEFAULT_TIER_WEIGHT
                fallback_candidates.append((-weight, index, normalized))
            fallback_candidates.sort(key=lambda item: (item[0], item[1]))
            for _, _, u in fallback_candidates:
                _add_with_domain_cap(u)
                if len(selected) >= max_items:
                    break

        return selected[:max_items]
