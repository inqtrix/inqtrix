"""Pins where a documentation page must agree with the code it describes.

The repository has no link check and no docs gate, so a page can contradict
the code indefinitely. These tests cover the cases where that contradiction
is not merely stale but actively misleading: the page told a reader that a
working feature does not exist, or gave a value range the implementation
cannot produce.
"""

from __future__ import annotations

import re
from pathlib import Path

from inqtrix.report_profiles import ReportProfile
from inqtrix.strategies._risk_scoring import KeywordRiskScorer

_DOCS = Path(__file__).resolve().parent.parent / "docs"


def test_every_report_profile_is_named_in_its_documentation() -> None:
    """A profile the API accepts must appear on the page that lists them.

    report-profiles.md said `"compact"` and `"deep"` were "the two accepted
    values" and that anything else returns HTTP 400, while the enum had
    accepted `schnell` all along. A reader following the page would never
    reach for a feature that works.
    """
    page = (_DOCS / "configuration" / "report-profiles.md").read_text(
        encoding="utf-8"
    )
    missing = [p.value for p in ReportProfile if f"`{p.value}`" not in page]
    assert not missing, (
        f"report-profiles.md never names these accepted profiles: {missing}"
    )


def test_the_documented_risk_score_range_is_reachable() -> None:
    """The documented ceiling must be a score the scorer can actually return.

    Three pages and the code's own docstring claimed 0-10 and a "+2 policy"
    family. The policy keywords live in `derive_required_aspects`, a
    different function; `score` has four independent +1 branches, so 4 is
    the maximum. A range nobody can reach makes the high-risk threshold
    unreadable.
    """
    scorer = KeywordRiskScorer()
    every_family = "aktuell prozent sollen " + "x" * 230
    ceiling = scorer.score(every_family)

    assert ceiling == 4, (
        "score() no longer maxes at 4; update the documented range in "
        "docs/architecture/websearch/nodes.md and the docstrings in "
        "strategies/_risk_scoring.py together with it"
    )
    text = (_DOCS / "architecture" / "websearch" / "nodes.md").read_text(
        encoding="utf-8"
    )
    assert "0-10" not in text and "0–10" not in text, (
        "websearch/nodes.md still advertises a risk-score range of 0-10"
    )


def test_the_research_loop_page_states_the_real_profile_bounds() -> None:
    """The narrative page repeats the profile bounds, so it can drift from them.

    Such numbers drift silently: a later-round width or a profile count the
    code does not have goes unnoticed as long as no two copies are read
    together. The page earns its table only if the table is checked.
    """
    import inqtrix.report_profiles as rp

    page = (_DOCS / "architecture" / "websearch" / "loop.md").read_text(
        encoding="utf-8"
    )
    tunings = {
        "schnell": rp._SCHNELL_TUNING,
        "compact": rp._COMPACT_TUNING,
        "deep": rp._DEEP_TUNING,
    }
    rows = {
        "Maximum rounds": "max_rounds",
        "Minimum rounds": "min_rounds",
        "Confidence stop": "confidence_stop",
        "First-round queries": "first_round_queries",
    }
    for label, key in rows.items():
        line = next(
            (l for l in page.splitlines() if l.startswith(f"| {label} ")),
            None,
        )
        assert line is not None, f"websearch/loop.md lost the '{label}' row"
        cells = [c.strip() for c in line.strip("|").split("|")][1:]
        expected = [
            str(t.settings_overrides[key]) for t in tunings.values()
        ]
        assert cells == expected, (
            f"websearch/loop.md says {label} = {cells}, the profiles say "
            f"{expected} (order: {list(tunings)})"
        )

    floors = [str(t.min_report_eligible_evidence) for t in tunings.values()]
    floor_line = next(
        (
            l
            for l in page.splitlines()
            if l.startswith("| Report-eligible evidence floor ")
        ),
        None,
    )
    assert floor_line is not None, "websearch/loop.md lost the evidence floor row"
    assert [c.strip() for c in floor_line.strip("|").split("|")][1:] == floors


def test_every_docs_page_is_reachable_from_the_index() -> None:
    """A page nobody links is a page nobody finds.

    Folder names carry no titles, so an unlinked page is reachable only
    by already knowing it
    exists. There is no link check in this repository, so this is the
    gate.
    """
    index = (_DOCS / "README.md").read_text(encoding="utf-8")
    linked = {
        target.split("/")[-1]
        for target in re.findall(r"\]\(([^)#]*\.md)", index)
    }
    pages = {
        p.name
        for p in _DOCS.rglob("*.md")
        if p.name != "README.md"
    }
    missing = sorted(pages - linked)
    assert not missing, (
        "these pages are in docs/ but nothing in docs/README.md links "
        f"them: {missing}"
    )


_APP = Path(__file__).resolve().parent.parent / "apps" / "research-desk" / "src"


def test_the_composer_presets_state_the_real_profile_bounds() -> None:
    """The Research Desk composer repeats the profile presets in TypeScript.

    The composer sends only the fields a person moved away from its preset,
    so a preset that disagrees with the backend profile silently changes
    every untouched run: the schnell preset once said 7 while the profile
    said 6, and every UI run stopped later than the profile advertises.
    No manifest publishes these numbers, so the copy is checked here,
    exactly like the loop.md table above.
    """
    import inqtrix.report_profiles as rp

    composer = (
        _APP / "features" / "researchDesk" / "components" / "Composer.tsx"
    ).read_text(encoding="utf-8")
    constants = (_APP / "features" / "researchRuns" / "types.ts").read_text(
        encoding="utf-8"
    )
    named = {
        match.group(1): int(match.group(2))
        for match in re.finditer(
            r"export const (DEEP_RESEARCH_[A-Z_]+) = (\d+)", constants
        )
    }
    fields = {
        "confidenceStop": "confidence_stop",
        "firstRoundQueries": "first_round_queries",
        "maxRounds": "max_rounds",
        "minRounds": "min_rounds",
    }
    tunings = {
        "schnell": rp._SCHNELL_TUNING,
        "compact": rp._COMPACT_TUNING,
        "deep": rp._DEEP_TUNING,
    }
    for profile, tuning in tunings.items():
        block = re.search(rf"\n  {profile}: \{{(.*?)\n  \}}", composer, re.S)
        assert block is not None, f"Composer.tsx lost the {profile} preset block"
        for ts_key, py_key in fields.items():
            match = re.search(rf"{ts_key}: ([A-Z_]+|\d+),", block.group(1))
            assert match is not None, (
                f"Composer.tsx {profile} preset lacks {ts_key}"
            )
            raw = match.group(1)
            value = named[raw] if raw in named else int(raw)
            assert value == tuning.settings_overrides[py_key], (
                f"Composer.tsx {profile}.{ts_key} = {value}, but "
                f"report_profiles.py says {tuning.settings_overrides[py_key]}"
            )
