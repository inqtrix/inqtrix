"""The parts of a result requirement name each rule by the title it had.

A part is what the user chose — read back by surfaces that name the rule
without re-reading the catalog. Its label is the composer handle; its
title is what the pill showed when the rule was attached. A rule kept at
a plan gate stays what it was frozen as, title included, and a part
stored before titles were kept stays without one: readers fall back to
the label, producers never invent a title.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from inqtrix.agents.report_requirement import (
    requirement_attached_payload,
    rule_requirement_block,
)
from inqtrix.content.prompt_templates import PromptTemplateRecord
from inqtrix.services.report_requirement_resolver import (
    resolve_report_requirement,
    resolve_report_rules,
)


def _catalog(*records: PromptTemplateRecord) -> SimpleNamespace:
    async def list_visible(*, tenant_id, visible_to):
        return [(record, "owner") for record in records]

    return SimpleNamespace(list_visible=list_visible)


def _rule(template_id: str, *, title: str, label: str, revision: int = 1) -> PromptTemplateRecord:
    return PromptTemplateRecord(
        id=template_id,
        tenant_id="default",
        owner_user_id=None,
        title=title,
        label=label,
        category="instruction",
        content_markdown="Gliedere klar.",
        revision=revision,
    )


def _resolve(catalog, ids, *, frozen=None):
    return asyncio.run(
        resolve_report_requirement(
            free_text="",
            template_ids=ids,
            prompt_templates=catalog,
            visible_to=None,
            frozen_rules=frozen,
        )
    )


def test_a_resolved_rule_names_itself_by_its_title_on_one_line():
    catalog = _catalog(_rule("pt_a", title="Executive\n  Briefing ", label="briefing"))
    _composed, parts = _resolve(catalog, ["pt_a"])
    assert parts == [
        {"template_id": "pt_a", "label": "briefing", "title": "Executive Briefing", "revision": 1}
    ]
    [resolved] = asyncio.run(
        resolve_report_rules(["pt_a"], prompt_templates=catalog, visible_to=None)
    )
    assert (resolved.template_id, resolved.label, resolved.title, resolved.revision) == (
        "pt_a", "briefing", "Executive Briefing", 1,
    )


def test_a_rule_without_a_title_carries_none():
    _composed, parts = _resolve(_catalog(_rule("pt_a", title="  ", label="briefing")), ["pt_a"])
    assert parts == [{"template_id": "pt_a", "label": "briefing", "revision": 1}]


def test_a_kept_rule_keeps_the_title_it_was_frozen_with():
    # The catalog renamed the rule since it was attached: a kept rule stays
    # what the run works under, and a part stored before titles were kept
    # gains none it never had.
    catalog = _catalog(
        _rule("pt_a", title="Neuer Name", label="briefing", revision=2),
        _rule("pt_b", title="Ton", label="ton", revision=4),
    )
    frozen = {
        "pt_a": (
            {"template_id": "pt_a", "label": "briefing", "title": "Executive Briefing", "revision": 1},
            rule_requirement_block("briefing", "Gliedere klar."),
        ),
        "pt_b": (
            {"template_id": "pt_b", "label": "ton", "revision": 3},
            rule_requirement_block("ton", "Sachlich."),
        ),
    }
    _composed, parts = _resolve(catalog, ["pt_a", "pt_b"], frozen=frozen)
    assert parts == [
        {"template_id": "pt_a", "label": "briefing", "title": "Executive Briefing", "revision": 1},
        {"template_id": "pt_b", "label": "ton", "revision": 3},
    ]


def test_the_attached_event_keeps_a_stored_title_and_invents_none():
    parts = [
        {"template_id": "pt_a", "label": "briefing", "title": "Executive Briefing", "revision": 1},
        {"template_id": "pt_b", "label": "ton", "revision": 3},
    ]
    composed = "\n\n".join(
        [
            rule_requirement_block("briefing", "Gliedere klar."),
            rule_requirement_block("ton", "Sachlich."),
        ]
    )
    payload = requirement_attached_payload(composed, parts, review_owed=True, run_id="run_1")
    assert payload["rules"] == parts
