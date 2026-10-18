"""Inqtrix — Iterative AI Research Agent.

Public API entry points are re-exported here so that ``from inqtrix
import …`` is the canonical way to access the high-level agent, the
provider adapters, the result types and the strategy ABCs. The
``__all__`` list defines the supported surface; anything below the
package's submodules (``inqtrix.providers.*``, ``inqtrix.strategies.*``,
``inqtrix.settings``) is considered internal-but-importable for
advanced use cases.

Quick start::

    from inqtrix import ResearchAgent

    agent = ResearchAgent()
    result = agent.research("Was ist der aktuelle Stand der GKV-Reform?")
    print(result.answer)

Baukasten (building-block) pattern::

    from inqtrix import ResearchAgent, AgentConfig, LiteLLM, PerplexitySearch

    llm = LiteLLM(api_key="...", default_model="gpt-4o")
    search = PerplexitySearch(api_key="...")
    agent = ResearchAgent(AgentConfig(llm=llm, search=search))
"""

__version__ = "0.2.0"

# The dev designation inside the current release line: the current dev
# milestone, as the app shows it. It is deliberately
# separate from ``__version__``, which stays on the three-segment release line
# because packaging metadata, npm, and the eval baselines are pinned to it.
# It must start with ``__version__`` — tests/test_routes.py pins that.
__display_version__ = "0.2.0.10"

# -- Public API (high-level) --
from inqtrix.agent import AgentConfig, ResearchAgent
from inqtrix.report_profiles import ReportProfile
from inqtrix.result import (
    Claim,
    ReportReference,
    ResearchMetrics,
    ResearchResult,
    ResearchResultExportOptions,
    Source,
    SourceMetrics,
    ClaimMetrics,
)

# -- Public API (providers) --
from inqtrix.providers.base import (
    LLMProvider,
    ProviderContext,
    SearchProvider,
    StructuredLLMResponse,
)
from inqtrix.providers.litellm import LiteLLM
from inqtrix.providers.perplexity import PerplexitySearch
from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.providers.azure import AzureOpenAILLM
from inqtrix.providers.azure_web_search import AzureFoundryWebSearch
from inqtrix.providers.bedrock import BedrockLLM
from inqtrix.exceptions import (
    AzureOpenAIAPIError,
    AzureFoundryWebSearchAPIError,
    BedrockAPIError,
    AgentStructuredOutputError,
)
from inqtrix.logging_config import configure_logging

# -- Public API (strategies) --
from inqtrix.strategies import (
    ClaimConsolidationStrategy,
    ClaimExtractionStrategy,
    RiskScoringStrategy,
    SourceTieringStrategy,
    StopCriteriaStrategy,
    StrategyContext,
)

__all__ = [
    # High-level
    "ResearchAgent",
    "AgentConfig",
    "ReportProfile",
    "ResearchResult",
    "ResearchResultExportOptions",
    "ResearchMetrics",
    "ReportReference",
    "Source",
    "Claim",
    "SourceMetrics",
    "ClaimMetrics",
    # Providers
    "LLMProvider",
    "SearchProvider",
    "ProviderContext",
    "StructuredLLMResponse",
    "LiteLLM",
    "PerplexitySearch",
    "AnthropicLLM",
    "AzureOpenAILLM",
    "AzureOpenAIAPIError",
    "AzureFoundryWebSearch",
    "AzureFoundryWebSearchAPIError",
    "BedrockLLM",
    "BedrockAPIError",
    "AgentStructuredOutputError",
    # Logging
    "configure_logging",
    # Strategies
    "SourceTieringStrategy",
    "ClaimExtractionStrategy",
    "ClaimConsolidationStrategy",
    "RiskScoringStrategy",
    "StopCriteriaStrategy",
    "StrategyContext",
]
