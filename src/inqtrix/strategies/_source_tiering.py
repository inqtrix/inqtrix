"""Source tiering strategy — the structural floor under the model's roles.

The strategy assigns a URL a tier (``primary``, ``mainstream``,
``stakeholder``, ``unknown``, ``low``) from the URL alone and aggregates a
0.0–1.0 quality score over a batch of URLs. The default knows no sites: it
recognises registry-class suffix families (``.gov``, ``.gv.at``,
``.europa.eu``, ``.edu``, ``.ac.uk`` …) whose registration proves the role,
and reports everything else as ``unknown`` with ``tier_reason="unjudged"``.
The judgement of the remaining sources is the extractor's ``source_roles``
(pinned in the run's source registry, ``tier_reason="model:<role>"``); the
floor wins over the model, the model over ``unjudged``. Implementations
should be pure functions of the URL — no I/O, no per-call state. A tier
never admits or rejects provider evidence and never verifies a claim.
"""

from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from collections.abc import Iterable

from inqtrix.domains import (
    NO_DOMAIN_REASON,
    ROLE_TO_TIER,
    SOURCE_TIER_WEIGHTS,
    STRUCTURAL_LABEL_ROLES,
    STRUCTURAL_REASON_PREFIX,
    STRUCTURAL_REGISTRY_ROLES,
    STRUCTURAL_TLD_ROLES,
    UNJUDGED_REASON,
)
from inqtrix.urls import domain_from_url, matching_suffix, public_suffixes


class SourceTieringStrategy(ABC):
    """Contract for classifying citation URLs into source-quality tiers.

    The strategy is consumed in three places: the search node tags each
    citation as it arrives, the evaluate node uses the aggregate
    discovery-quality score as a stop-cascade input, and the answer node uses
    the per-URL tier to order references. Unknown classification must not
    discard a search result or prevent its provider-grounded content reaching
    synthesis. Implementations must be deterministic for a given URL and
    side-effect-free; the same URL must always classify to the same tier
    within one run.
    """

    @abstractmethod
    def tier_for_url(self, url: str) -> str:
        """Return the quality tier for a single URL.

        Args:
            url: Citation URL to classify. Implementations are
                expected to handle URLs that lack a scheme or that
                point at well-known aggregator hosts; malformed input
                must not raise — return ``"unknown"`` instead.

        Returns:
            One of ``"primary"`` (peer-reviewed / official), ``"mainstream"``
            (established media), ``"stakeholder"`` (industry / NGO /
            association), ``"unknown"`` (no classification), or
            ``"low"`` (known low-quality domains).
        """

    def quality_from_urls(self, urls: list[str]) -> tuple[dict[str, int], float]:
        """Aggregate tier counts and a weighted quality score over URLs.

        Args:
            urls: Citation URLs to score. May be empty; implementations
                must return zero counts and ``0.0`` for an empty list.

        Returns:
            Tuple ``(tier_counts, quality_score)``.
            ``tier_counts`` is a dict that contains all five tier keys
            (zero for absent tiers) for stable downstream consumption.
            ``quality_score`` is in ``[0.0, 1.0]`` — typically the
            weighted mean over per-URL tier weights from
            :data:`~inqtrix.domains.SOURCE_TIER_WEIGHTS` (``primary=1.0``,
            ``mainstream=0.8``, ``stakeholder=0.45``, ``unknown=0.35``,
            ``low=0.1`` by default). The run itself scores the tiers pinned
            in its source registry (:func:`quality_from_tiers`); this
            method classifies URLs the registry does not cover.
        """
        return quality_from_tiers(self.tier_for_url(url) for url in urls)

    def explain_url(self, url: str) -> dict[str, str]:
        """Explain how the strategy classified one URL.

        Custom strategies do not have to override this method. The
        default keeps the interface backwards-compatible by deriving a
        minimal explanation from :meth:`tier_for_url`.

        Args:
            url: Citation URL to classify.

        Returns:
            Mapping with ``domain``, ``tier``, and ``tier_reason`` keys.
        """
        tier = self.tier_for_url(url)
        return {
            "domain": domain_from_url(url),
            "tier": tier,
            "tier_reason": "strategy_default",
        }


@functools.cache
def structural_suffix_roles() -> dict[str, str]:
    """Suffix -> role of the structural floor, derived once per process.

    Every second-level public suffix whose leading label is a registry-class
    label (:data:`STRUCTURAL_LABEL_ROLES`: ``gov.hk``, ``gob.ar``, ``ac.il``,
    ``edu.pl`` …), the reserved top-level domains
    (:data:`STRUCTURAL_TLD_ROLES`) and the explicit state registries the rule
    cannot derive (:data:`STRUCTURAL_REGISTRY_ROLES`).
    """
    roles: dict[str, str] = dict(STRUCTURAL_TLD_ROLES)
    for suffix in public_suffixes():
        if suffix.startswith(("*", "!")) or suffix.count(".") != 1:
            continue
        role = STRUCTURAL_LABEL_ROLES.get(suffix.split(".", 1)[0])
        if role:
            roles[suffix] = role
    roles.update(STRUCTURAL_REGISTRY_ROLES)
    return roles


def structural_role(domain: str) -> str:
    """Return the structural role of ``domain`` (``""`` when no suffix family matches)."""
    if not domain:
        return ""
    roles = structural_suffix_roles()
    suffix = matching_suffix(domain, roles)
    return roles[suffix] if suffix else ""


def quality_from_tiers(tiers: Iterable[str]) -> tuple[dict[str, int], float]:
    """Aggregate tier counts and the weighted quality score over tier labels.

    The same arithmetic as :meth:`SourceTieringStrategy.quality_from_urls`,
    applied to tiers the run already pinned (the source registry) instead of
    re-classifying URLs.
    """
    counts: dict[str, int] = {
        "primary": 0, "mainstream": 0, "stakeholder": 0, "unknown": 0, "low": 0,
    }
    score = 0.0
    total = 0
    for raw in tiers:
        tier = str(raw or "unknown")
        if tier not in counts:
            tier = "unknown"
        counts[tier] += 1
        score += SOURCE_TIER_WEIGHTS.get(tier, 0.35)
        total += 1
    if not total:
        return counts, 0.0
    return counts, round(score / total, 3)


class DefaultSourceTiering(SourceTieringStrategy):
    """The structural floor: registry-class suffixes, no site lists.

    ``.gov``/``gov.hk``/``gv.at``/``europa.eu``/``ac.uk``/… are ``primary``
    because the registry proves the role (a rule over the bundled public
    suffix list plus a bounded set of state registries); every other domain
    is ``unknown``/``unjudged`` until the extractor judges it. The floor never emits ``mainstream``,
    ``stakeholder`` or ``low`` — those are judgements about who speaks,
    which a suffix cannot prove.

    No I/O, no caching layer, no per-instance state — instances are
    cheap to construct and safe to share across runs.
    """

    def tier_for_url(self, url: str) -> str:
        """Return ``primary`` for a structural match, else ``unknown``."""
        role = structural_role(domain_from_url(url))
        return ROLE_TO_TIER[role] if role else "unknown"

    def explain_url(self, url: str) -> dict[str, str]:
        """Name the suffix family behind the tier (or ``unjudged``)."""
        domain = domain_from_url(url)
        if not domain:
            return {"domain": "", "tier": "unknown", "tier_reason": NO_DOMAIN_REASON}
        role = structural_role(domain)
        if role:
            return {
                "domain": domain,
                "tier": ROLE_TO_TIER[role],
                "tier_reason": f"{STRUCTURAL_REASON_PREFIX}{role}",
            }
        return {"domain": domain, "tier": "unknown", "tier_reason": UNJUDGED_REASON}
