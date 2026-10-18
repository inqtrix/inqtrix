"""Resolution of a result requirement against the caller's own catalog.

The requirement can be set in two places — at submit time, before the run
starts, and at the plan gate — and both must resolve attached library
rules the SAME way. Two copies of this logic would drift, and the drift
would be a security one: the whole point of resolving server-side is that
a client sends ids and nothing else.

The caller supplies free text and template ids; label, revision and body
come from the caller's own visible templates. A client-supplied body
would be an unchecked text channel straight into the writing prompts, and
a client-supplied label would let one rule impersonate another in the
origin markers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from inqtrix.agents.report_requirement import (
    MAX_COMPOSED_REQUIREMENT_CHARS,
    REPORT_RULE_IDS_MAX,
    compose_requirement_blocks,
    composed_requirement_is_oversized,
    rule_part,
    rule_requirement_block,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from inqtrix.auth.context import UserContext


class ReportRequirementError(Exception):
    """A requirement that cannot be honored as asked.

    Never a silent drop: an unknown rule or an oversized composition
    fails loudly, because running under requirements the user believes
    are in force is the exact failure this feature exists to prevent.
    """

    def __init__(self, messages: "Sequence[str]") -> None:
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


class ResolvedRule(NamedTuple):
    """One attached rule as the caller's catalog holds it."""

    template_id: str
    label: str
    title: str
    """The display title on one line; ``""`` when the template has none."""
    revision: int
    body: str


def distinct_rule_ids(template_ids: "Sequence[str]") -> list[str]:
    """The attached rule ids, trimmed and deduplicated in attachment order.

    The same rule attached twice is ONE rule. Deduplicating here, in the
    resolution both entry points share, keeps the submit path and the
    plan gate in agreement: a duplicate never counts twice against the
    rule cap or the composed ceiling, and never reaches the writer twice.
    """
    return list(
        dict.fromkeys(
            str(item).strip() for item in template_ids if str(item).strip()
        )
    )


async def resolve_report_rules(
    template_ids: "Sequence[str]",
    *,
    prompt_templates: Any,
    visible_to: "UserContext | None",
) -> list[ResolvedRule]:
    """One :class:`ResolvedRule` per distinct attached rule, in attachment order.

    Raises:
        ReportRequirementError: The library is inactive, or an id is not
            visible to this caller.
    """
    wanted = distinct_rule_ids(template_ids)
    if not wanted:
        return []
    if prompt_templates is None:
        raise ReportRequirementError(
            ["The prompt library is not enabled in this instance."]
        )
    tenant_id = (
        visible_to.principal.tenant_id if visible_to is not None else "default"
    )
    visible = await prompt_templates.list_visible(
        tenant_id=tenant_id, visible_to=visible_to
    )
    by_id = {str(record.id): record for record, _access in visible}
    missing = [item for item in wanted if item not in by_id]
    if missing:
        raise ReportRequirementError(
            ["Unknown or non-visible rule: " + ", ".join(missing)]
        )
    return [
        ResolvedRule(
            template_id=str(by_id[item].id),
            label=str(by_id[item].label),
            # Display text only, so whitespace is folded to one line: a
            # line break in a library title must not break a status line.
            title=" ".join(str(by_id[item].title or "").split()),
            revision=int(by_id[item].revision),
            body=str(by_id[item].content_markdown),
        )
        for item in wanted
    ]


async def resolve_report_requirement(
    *,
    free_text: str,
    template_ids: "Sequence[str]",
    prompt_templates: Any,
    visible_to: "UserContext | None",
    frozen_rules: "Mapping[str, tuple[Mapping[str, Any], str]] | None" = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The composed requirement plus the parts it was composed from.

    Args:
        frozen_rules: Rules the run is already frozen under, by template
            id: ``(part, block)`` with the stored part and the composed
            block the run works under. A plan gate passes them so that a
            rule the user keeps stays that text at that revision — also
            after the template was edited or deleted — and only a rule the
            selection adds reads the catalog. The submit path passes none.

    Returns:
        ``(composed, parts)`` — ``composed`` is what the writing prompts
        see, ``parts`` is what the user chose, kept for the read-back so
        a surface can name the rules without re-reading the catalog.

    Raises:
        ReportRequirementError: More distinct rules than the published
            cap, an unresolvable rule, or a composition over the visible
            ceiling.
    """
    wanted = distinct_rule_ids(template_ids)
    if len(wanted) > REPORT_RULE_IDS_MAX:
        # Counted after deduplication, at the one place both entry points
        # share — a cap counted on the raw list would refuse a request
        # that attaches three rules, one of them twice.
        raise ReportRequirementError(
            [
                f'At most {REPORT_RULE_IDS_MAX} rules can be attached to one result requirement ({len(wanted)} distinct rules attached).'
            ]
        )
    frozen = dict(frozen_rules or {})
    resolved = {
        rule.template_id: (rule_part(rule._asdict()), rule_requirement_block(rule.label, rule.body))
        for rule in await resolve_report_rules(
            [item for item in wanted if item not in frozen],
            prompt_templates=prompt_templates,
            visible_to=visible_to,
        )
    }
    # A kept rule stays the part it was frozen as, its title included (or
    # its absence, for a part stored before titles were kept).
    chosen = [
        ({**rule_part(frozen[item][0]), "template_id": item}, frozen[item][1])
        if item in frozen
        else resolved[item]
        for item in wanted
    ]
    composed = compose_requirement_blocks(
        [block for _part, block in chosen], free_text
    )
    if composed_requirement_is_oversized(composed):
        raise ReportRequirementError(
            [
                f'The result requirement is too long at {len(composed)} characters (limit {MAX_COMPOSED_REQUIREMENT_CHARS}). Attach fewer or shorter rules.'
            ]
        )
    return composed, [part for part, _block in chosen]
