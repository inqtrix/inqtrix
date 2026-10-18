"""Web-research algorithms wrapping the existing LangGraph engine.

The graph (``inqtrix.graph``), its nodes, and its state machine are
unchanged by the platform rebuild; this package adapts them to the
:class:`~inqtrix.core.algorithms.AgentAlgorithm` contract so they
dispatch through the registry like every future mode.
"""

from inqtrix.research.web_research import (
    DirectLlmAlgorithm,
    WebResearchAlgorithm,
)

__all__ = ["DirectLlmAlgorithm", "WebResearchAlgorithm"]
