"""The recency window of a research run follows what the question asks for.

The classifier's window becomes a hint to the search agent ("focus on results
from the past month") on every search of the run. A question about what a
regulation requires from 2027 drew a one-month window: the searches then
missed the regulation text and its guidance, and one denied the start date
for lack of recent sources. A date in a question names the period the answer
is about, not how recent its sources must be, and a question without a window
still needs its sources: read as "no current search needed", it was answered
from the model's memory.
"""

from __future__ import annotations

from typing import Any

from inqtrix.graph import run
from inqtrix.providers.base import ProviderContext
from inqtrix.report_profiles import ReportProfile
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import AgentSettings
from inqtrix.strategies import create_default_strategies
from tests._fake_providers import FakeLLM, FakeSearch, load_search_result_fixture

_QUESTION = (
    "Welche Pflichten bringt die EU-Batterieverordnung ab 2027 für austauschbare "
    "Akkus in Smartphones, und welche Ausnahmen gibt es?"
)


class _CapturingLLM(FakeLLM):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.classify_prompts: list[str] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        if "CLASSIFICATION" in prompt:
            self.classify_prompts.append(prompt)
        return super().complete(prompt, **kwargs)


class _RecordingSearch(FakeSearch):
    def __init__(self) -> None:
        super().__init__(load_search_result_fixture("nvidia_quartalszahlen"))
        self.kwargs: list[dict[str, object]] = []

    def search(self, query: str, **kwargs: object) -> GroundedSearchResult:
        self.kwargs.append(dict(kwargs))
        return super().search(query, **kwargs)


def _run(recency: str) -> tuple[_CapturingLLM, _RecordingSearch, dict]:
    llm = _CapturingLLM(classify_recency=recency)
    search = _RecordingSearch()
    settings = AgentSettings(
        testing_mode=True,
        first_round_queries=2,
        max_rounds=1,
        min_rounds=1,
        report_profile=ReportProfile.COMPACT,
    )
    raw = run(
        _QUESTION,
        providers=ProviderContext(llm=llm, search=search),
        strategies=create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model"),
        settings=settings,
    )
    return llm, search, raw


def test_the_classifier_reads_a_date_as_the_period_not_as_a_window() -> None:
    llm, _search, _raw = _run("NONE")

    (prompt,) = llm.classify_prompts
    # No window is no reason to skip the search: a question about rules is
    # answered from sources, whatever its window.
    assert "SEARCH whenever the answer states facts that sources should back" in prompt
    assert "DIRECT only for what needs no source at all" in prompt
    assert (
        "what laws, rules or standards require (obligations, deadlines, exceptions), "
        "also from a future date"
    ) in prompt
    assert (
        "A date in the question (e.g. 'from 2027') names the period the answer is about, "
        "not how recent the sources must be."
    ) in prompt


def test_no_window_sends_no_recency_and_a_window_reaches_every_search() -> None:
    _llm, search, raw = _run("NONE")
    assert raw["result_state"]["recency"] == ""
    assert search.kwargs and all(not call.get("recency_filter") for call in search.kwargs)

    _llm, search, raw = _run("MONTH")
    assert raw["result_state"]["recency"] == "month"
    assert search.kwargs and all(call.get("recency_filter") == "month" for call in search.kwargs)
