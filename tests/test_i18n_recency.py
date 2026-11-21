"""The words for the time window of a research.

A window counts back from now. The progress line of a research names it,
whichever of the two set it: the classifier of the research (hour to month)
or the agent that delegated it (day to year). The approval card of the
agent names it too, and both say it in the same words.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from inqtrix.core.results import WebRecency
from inqtrix.i18n import MESSAGES, t

_CARD_TEXTS = Path(__file__).resolve().parents[1] / "apps/research-desk/src/i18n/translations.ts"


@pytest.mark.parametrize("window", ("hour", *get_args(WebRecency)))
def test_every_window_of_a_research_has_a_progress_text(window: str) -> None:
    """The progress line shows the raw value where a text is missing, so
    none may be."""
    key = f"recency_{window}"
    assert key in MESSAGES
    assert all(t({"language": language}, key) not in ("", key) for language in ("de", "en"))


@pytest.mark.parametrize("window", get_args(WebRecency))
def test_the_progress_line_and_the_approval_card_name_a_window_alike(window: str) -> None:
    """Who approved the past 7 days does not take another wording in the
    progress of the research for another period."""
    card = re.findall(rf"gateRecency{window.capitalize()}: '([^']*)'", _CARD_TEXTS.read_text(encoding="utf-8"))
    assert card == [t({"language": "de"}, f"recency_{window}"), t({"language": "en"}, f"recency_{window}")]
