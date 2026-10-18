"""Tests for the standardized ``SearchProvider.search_model`` property.

Each in-tree search provider must expose a stable, operator-friendly
identifier via ``search_model``. The ABC default is intentionally loud
(``"<ClassName>(unknown)"``) so a missing override surfaces immediately
in the ``GET /health`` payload rather than silently leaking the global
``Settings.models.search_model`` default.
"""

from __future__ import annotations


from inqtrix.providers.base import SearchProvider
from inqtrix.search_result import GroundedSearchResult


# ------------------------------------------------------------------ #
# ABC default — loud "(unknown)" hint
# ------------------------------------------------------------------ #


def test_searchprovider_abc_default_is_loud():
    """A subclass that forgets to override must produce a loud identifier."""
    class _ForgotToOverride(SearchProvider):
        def search(self, query, **_kw):
            return GroundedSearchResult()

        def is_available(self) -> bool:
            return True

    assert _ForgotToOverride().search_model == "_ForgotToOverride(unknown)"


# ------------------------------------------------------------------ #
# PerplexitySearch
# ------------------------------------------------------------------ #


def test_perplexity_search_model_returns_constructor_model():
    from inqtrix.providers.perplexity import PerplexitySearch

    provider = PerplexitySearch(
        api_key="test-key",
        base_url="https://api.perplexity.ai",
        model="sonar-pro",
    )
    assert provider.search_model == "sonar-pro"


# ------------------------------------------------------------------ #
# AzureFoundryWebSearch
# ------------------------------------------------------------------ #


def test_azure_foundry_web_search_model_format():
    from inqtrix.providers.azure_web_search import AzureFoundryWebSearch

    provider = AzureFoundryWebSearch(
        project_endpoint="https://example.services.ai.azure.com/api/projects/p1",
        agent_name="web-search-agent",
        agent_version="v3",
        _client=object(),
    )
    assert provider.search_model == "foundry-web:web-search-agent@v3"


def test_azure_foundry_web_search_model_defaults_to_latest_when_no_version():
    from inqtrix.providers.azure_web_search import AzureFoundryWebSearch

    provider = AzureFoundryWebSearch(
        project_endpoint="https://example.services.ai.azure.com/api/projects/p1",
        agent_name="my-web-agent",
        _client=object(),
    )
    assert provider.search_model == "foundry-web:my-web-agent@latest"
