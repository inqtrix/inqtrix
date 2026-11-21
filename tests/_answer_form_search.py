"""Search providers for the tests of the answer form of a web search.

Two providers that record what they were called with: one in the
documented signature, which knows no answer form, and one that names the
answer form among its parameters. Each call records whether an answer form
was handed to the provider at all, which a default value could not tell
from a form handed as ``None``.
"""

from __future__ import annotations

from typing import Any

from inqtrix.search_result import GroundedSearchResult, GroundedSource

NOT_HANDED = "not handed"
"""What a call records as its answer form when the keyword did not arrive."""


class PlainSearch:
    """A search provider in the documented signature, without an answer form."""

    search_model = "stub-search"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def is_available(self) -> bool:
        return True

    def search(
        self, query: str, *, search_context_size: str = "high", recency_filter: str | None = None,
        language_filter: list[str] | None = None, domain_filter: list[str] | None = None,
        search_mode: str | None = None, return_related: bool = False, deadline: float | None = None,
    ) -> GroundedSearchResult:
        self.calls.append({"query": query, "answer_form": NOT_HANDED})
        return GroundedSearchResult(
            answer=f"Antwort zu {query}",
            sources=[GroundedSource(url="https://a.example", title="A", snippet="s1", rank=1)],
            prompt_tokens=11, completion_tokens=7,
        )


class FormSearch(PlainSearch):
    """A search provider that names the answer form among its parameters."""

    supported_search_parameters = frozenset({"recency_filter", "answer_form"})

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        form = kwargs.pop("answer_form", NOT_HANDED)
        result = super().search(query, **kwargs)
        self.calls[-1]["answer_form"] = form
        return result
