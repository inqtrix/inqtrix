"""Source vocabulary: the role a source plays, its tier label and weight,
the structural floor of registry-class suffixes and the generic query terms.

There are no site lists here. A source's role is judged once per run by the
claim-extraction call (``source_roles``) and pinned in the run's source
registry; the structural floor is a rule over the bundled public suffix list
(registry-class suffix families whose registration itself proves the role)
plus a bounded set of state registries the rule cannot derive.
Organisation-specific trust lists belong in a custom
:class:`~inqtrix.strategies.SourceTieringStrategy`, never in this module.
"""

from __future__ import annotations

ROLE_TO_TIER: dict[str, str] = {
    "official_primary": "primary",
    "company_primary": "primary",
    "academic_primary": "primary",
    "reporting": "mainstream",
    "commentary_stakeholder": "stakeholder",
    "aggregator_unknown": "unknown",
    "low": "low",
}
"""The roles the extractor may assign to a source (who speaks: an authority,
the company itself, a researcher, a newsroom, an interested party, an
aggregator, user-generated content) -> the five display/API tier labels
(``Source.tier``). The ONE table of the vocabulary: the prompt, the schema
enum, the parser and the registry pin all derive from it."""

SOURCE_ROLES: tuple[str, ...] = tuple(ROLE_TO_TIER)
"""The roles in prompt order (derived from :data:`ROLE_TO_TIER`)."""

PLACEMENT_FIELDS: tuple[str, ...] = ("tier", "tier_reason", "role_reason")
"""The three fields that say where a source stands and why; copied together
between the registry, evidence records and report references."""

UNJUDGED_REASON = "unjudged"
"""``tier_reason`` of a source neither a strategy nor the extractor placed."""

NO_DOMAIN_REASON = "no_domain"
"""``tier_reason`` of a URL without a usable host."""

STRUCTURAL_REASON_PREFIX = "structural:"
"""``tier_reason`` prefix of a structural-floor match (``structural:official_primary``)."""

MODEL_REASON_PREFIX = "model:"
"""``tier_reason`` prefix of an extractor judgement (``model:reporting``)."""


def placement_kind(tier_reason: str) -> tuple[str, str]:
    """Split a ``tier_reason`` into ``(kind, role)``.

    ``structural:official_primary`` -> ``("structural", "official_primary")``,
    ``model:reporting`` -> ``("model", "reporting")``, ``unjudged`` ->
    ``("unjudged", "")``, ``""``/``no_domain`` -> ``("unjudged", "")``; any
    other reason (a custom strategy's own wording) -> ``("strategy", "")``.
    The one parser of the reason grammar.
    """
    reason = str(tier_reason or "")
    if reason.startswith(STRUCTURAL_REASON_PREFIX):
        return "structural", reason[len(STRUCTURAL_REASON_PREFIX):]
    if reason.startswith(MODEL_REASON_PREFIX):
        return "model", reason[len(MODEL_REASON_PREFIX):]
    if reason in ("", UNJUDGED_REASON, NO_DOMAIN_REASON):
        return "unjudged", ""
    return "strategy", ""


def strategy_placed(tier_reason: str) -> bool:
    """Whether the configured tiering strategy placed the source (the
    structural floor or a custom strategy): such a placement wins over the
    extractor's judgement."""
    return placement_kind(tier_reason)[0] in ("structural", "strategy")


STRUCTURAL_LABEL_ROLES: dict[str, str] = {
    "gov": "official_primary",
    "gob": "official_primary",
    "gouv": "official_primary",
    "govt": "official_primary",
    "mil": "official_primary",
    "ac": "academic_primary",
    "edu": "academic_primary",
}
"""Leading labels of second-level public suffixes that a registry reserves
for one kind of institution (``gov.uk``, ``gob.es``, ``gouv.fr``,
``ac.uk``, ``edu.au`` …). The floor derives every ``<label>.<cc>`` suffix of
the bundled public suffix list from these seven labels — a rule, not a
country list. ``go`` is deliberately absent: ``go.it`` is the province of
Gorizia, so the governmental ``go.*`` registries are named explicitly."""

STRUCTURAL_TLD_ROLES: dict[str, str] = {
    "gov": "official_primary",
    "mil": "official_primary",
    "int": "official_primary",
    "edu": "academic_primary",
}
"""Top-level domains reserved for one kind of institution."""

STRUCTURAL_REGISTRY_ROLES: dict[str, str] = {
    "gv.at": "official_primary",
    "gc.ca": "official_primary",
    "go.jp": "official_primary",
    "go.kr": "official_primary",
    "go.id": "official_primary",
    "go.th": "official_primary",
    "go.ke": "official_primary",
    "admin.ch": "official_primary",
    "bund.de": "official_primary",
    "europa.eu": "official_primary",
    "gov.scot": "official_primary",
    "parliament.uk": "official_primary",
    "judiciary.uk": "official_primary",
    "nhs.uk": "official_primary",
}
"""State registries the label rule cannot derive: either the public suffix
list does not carry them (``bund.de``, ``europa.eu``, ``admin.ch``,
``parliament.uk``, ``judiciary.uk``, ``gov.scot``) or their leading label is
ambiguous across countries (``go.*``, ``gv.*``, ``gc.*``). Admission rule:
a registration reserved by a state for its institutions, never a site or a
publisher, at most MAX_STRUCTURAL_REGISTRIES entries — the test pins the
bound with a literal so the set cannot grow back into an allow-list."""

MAX_STRUCTURAL_REGISTRIES = 16
"""Size bound of :data:`STRUCTURAL_REGISTRY_ROLES`."""

SOURCE_TIER_WEIGHTS: dict[str, float] = {
    "primary": 1.0,
    "mainstream": 0.8,
    "stakeholder": 0.45,
    "unknown": 0.35,
    "low": 0.1,
}

GENERIC_QUERY_TERMS_DE: set[str] = {
    "soll", "sollen", "sollte", "sollten", "werden", "wird", "wurde",
    "zukuenftig", "kuenftig", "zukunft", "aktuell", "heute",
    "richtung", "diskussion", "diskussionen", "debatte", "debatten",
    "geht", "gehen", "genau", "eigentlich", "was", "waren", "wichtige",
    "wichtigsten", "wichtig", "entwicklung", "entwicklungen", "letzte",
    "letzten", "vergangene", "vergangenen", "tage", "woche", "wochen",
    "news", "nachrichten", "meldungen", "important", "developments",
    "recent", "latest", "last", "days", "week", "weeks",
}
