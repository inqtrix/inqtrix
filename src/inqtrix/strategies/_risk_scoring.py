"""Risk scoring strategy — score risk and infer the query type."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod


log = logging.getLogger("inqtrix")


class RiskScoringStrategy(ABC):
    """Contract for question-level risk scoring and query-type inference.

    Risk scoring is one of the cheapest early-loop signals. The
    ``risk_score`` / ``high_risk`` flag is observational only -- it is
    surfaced in forensic events and ``/health`` and preserved across
    follow-up questions, but it does not select models or escalate
    anything (use the model tiers or a per-node model override for a
    stronger model). The query type is the classify fallback when the
    model call fails. The answer contract and the sub-question checklist
    come from the classify call itself (``ANSWER_CONTRACT`` /
    ``SUB_QUESTIONS``), and the evaluator judges their coverage — no
    keyword derivation or substring scan lives here.

    Implementations are expected to be deterministic and side-effect-
    free so the same question always produces the same risk score and
    query type. LLM-backed scorers are allowed but discouraged for cost
    reasons; the default :class:`KeywordRiskScorer` uses regex families
    exclusively.
    """

    @abstractmethod
    def score(self, question: str) -> int:
        """Return a bounded deterministic risk score for ``question``.

        Args:
            question: User question to score. Must tolerate empty
                input and return ``0`` rather than raising.

        Returns:
            Integer in ``[0, 4]`` -- four independent ``+1`` families, so
            four is the reachable maximum. Values
            ``>= AgentSettings.high_risk_score_threshold`` (default ``4``,
            i.e. all four families) flag the question as ``high_risk`` -- an
            observability signal only; it does not change model selection.
        """
        ...

    @abstractmethod
    def infer_query_type(self, question: str) -> str:
        """Infer a coarse query-type label that the planner specialises on.

        Args:
            question: User question. Tolerate empty input.

        Returns:
            One of ``"general"`` (default), ``"news"`` (recency-biased
            phrasing), or ``"academic"`` (paper / DOI / journal
            language). Implementations may add backend-specific
            labels but should keep the three core values stable so
            the planner's existing branches keep working.
        """
        ...


class KeywordRiskScorer(RiskScoringStrategy):
    """Deterministic regex-based default risk scorer.

    No LLM calls — uses regex families and token matching so classify,
    plan and search share stable heuristics that are cheap to
    evaluate and easy to regression-test. The regex families are
    German-language but topic-neutral (recency, quantification, normative
    phrasing, length); there is no domain-specific policy bias.

    Suitable for production as the default; subclass to replace the
    keyword tables, or implement :class:`RiskScoringStrategy`
    from scratch for other languages.
    """

    # ------------------------------------------------------------------ #
    # score
    # ------------------------------------------------------------------ #
    def score(self, question: str) -> int:
        """Compute a regex-based risk score capped at 10.

        Adds:
        - ``+1`` for current-affairs markers (``aktuell``, ``heute``,
          ``trend``, ``ausblick``).
        - ``+1`` for numeric / monetary mentions
          (``prozent``, ``mrd``, ``euro``, ``\\d+%``).
        - ``+1`` for normative phrasing
          (``soll``, ``geplant``, ``durchsetzbar``).
        - ``+1`` for very long questions (> 220 chars).

        Implements the contract from
        :meth:`RiskScoringStrategy.score`.

        Args:
            question: User question. Empty / ``None`` returns ``0``.

        Returns:
            Integer in ``[0, 4]``: one point each for recency, numeric,
            normative and length. The ``min(..., 10)`` below is a legacy
            ceiling that the four families can never reach.
        """
        q = (question or "").lower()
        score = 0

        if re.search(r"\b(aktuell|heute|neueste|zuletzt|diskussion|trend|ausblick|prognose)\b", q):
            score += 1
        if re.search(r"\b(prozent|mrd|mio|euro|\d+[%\u20ac]?)\b", q):
            score += 1
        if re.search(r"\b(soll|sollen|geplant|durchsetzbar|realistisch)\b", q):
            score += 1
        if len(question) > 220:
            score += 1

        return min(score, 10)

    # ------------------------------------------------------------------ #
    # infer_query_type
    # ------------------------------------------------------------------ #
    def infer_query_type(self, question: str) -> str:
        """Classify the question as ``academic``, ``news`` or ``general``.

        Implements the contract from
        :meth:`RiskScoringStrategy.infer_query_type`. Precedence:
        academic markers (``paper``, ``studie``, ``doi``, ``arxiv``,
        ``journal``, ``peer-review``) win over news markers
        (``heute``, ``aktuell``, ``debatte``); everything else maps
        to ``general``.

        Args:
            question: User question. Empty / ``None`` returns
                ``"general"``.

        Returns:
            One of ``"academic"``, ``"news"``, or ``"general"``.
        """
        q = (question or "").lower()
        if re.search(r"\b(paper|studie|study|doi|arxiv|journal|conference|peer-review)\b", q):
            return "academic"
        if re.search(r"\b(heute|aktuell|news|nachricht|meld|diskussion|debatte)\b", q):
            return "news"
        return "general"
