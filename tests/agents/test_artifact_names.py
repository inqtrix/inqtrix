"""Cross-language parity + semantics of derived artifact file names.

The fixture ``tests/fixtures/artifact_name_parity.json`` holds
hand-maintained expected values of this Python reference, consumed
byte-identically by the TypeScript side (``artifactNames.parity.test.ts``):
one checked-in anchor fixture both languages assert against, so neither
side can change without the fixture changing with it.
"""

from __future__ import annotations

import json
from pathlib import Path

from inqtrix.agents.artifact_names import (
    ARTIFACT_NAME_FALLBACK,
    NAMED_ARTIFACT_KINDS,
    artifact_slug,
    assign_artifact_file_names,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "artifact_name_parity.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_slug_cases_match_the_fixture():
    cases = _fixture()["slug_cases"]
    assert len(cases) >= 15
    for case in cases:
        assert artifact_slug(case["title"]) == case["slug"], case["title"]


def test_assign_cases_match_the_fixture():
    cases = _fixture()["assign_cases"]
    assert len(cases) >= 5
    for case in cases:
        got = assign_artifact_file_names(
            [(item[0], item[1]) for item in case["items"]]
        )
        assert got == case["expected"], case["items"]


def test_suffix_sits_before_the_extension():
    names = assign_artifact_file_names([("a", "Memo"), ("b", "Memo")])
    assert names == {"a": "memo.md", "b": "memo-2.md"}


def test_empty_titles_fall_back_loudly_named():
    assert artifact_slug("") == ARTIFACT_NAME_FALLBACK
    assert artifact_slug("###") == ARTIFACT_NAME_FALLBACK


def test_named_kinds_are_exactly_the_canvas_documents():
    assert NAMED_ARTIFACT_KINDS == ("memo", "deliverable")
