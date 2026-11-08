"""Known layer violations: a ratchet that only shrinks, down to empty."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoundaryException:
    """One edge the layer table forbids, and why it still does.

    Where the import stands is the scanner's to say: the check names the file and
    line it read, so the list holds nothing that a moved import could make stale.
    """

    importer: str
    imported: str
    reason: str


BOUNDARY_EXCEPTIONS: tuple[BoundaryException, ...] = (
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.capabilities",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.capabilities.contracts",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.capabilities.registry",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.core.context",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.execution_authority",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.project.editor_patch_ports",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.project.editor_ports",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.services.agent_context",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.services.editor_persistence_service",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "inqtrix.services.run_service",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.checkpoint_guard",
        "inqtrix.runs.ports",
        "product code still reaches infrastructure directly",
    ),
    BoundaryException(
        "inqtrix.agents.checkpointing",
        "inqtrix.core.constants",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.clarification",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.discovery",
        "inqtrix.capabilities",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.document_analysis",
        "inqtrix.capabilities.catalog.knowledge",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.document_analysis",
        "inqtrix.capabilities.contracts",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "inqtrix.capabilities",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "inqtrix.core.context",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "inqtrix.execution_authority",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "inqtrix.runs.shared",
        "product code still reaches infrastructure directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.deps",
        "inqtrix.capabilities",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.deps",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.deps",
        "inqtrix.execution_authority",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.review_pass",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.state",
        "langchain.agents.middleware",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.state",
        "langchain_core.messages",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.state",
        "langgraph.channels.delta",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.state",
        "langgraph.graph.message",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "inqtrix.capabilities",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "inqtrix.capabilities.catalog.knowledge",
        "product code still reaches the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "inqtrix.execution_authority",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "inqtrix.project.editor_ports",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "inqtrix.project.editor_search",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "inqtrix.runs.records",
        "product code still reaches infrastructure directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.work_profiles",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.work_profiles",
        "inqtrix.runs.shared",
        "product code still reaches infrastructure directly",
    ),
    BoundaryException(
        "inqtrix.agents.patch_phase",
        "inqtrix.server.editor_instructions",
        "product code still reaches the composition layer",
    ),
    BoundaryException(
        "inqtrix.agents.patch_phase",
        "inqtrix.server.reference_documents",
        "product code still reaches the composition layer",
    ),
    BoundaryException(
        "inqtrix.agents.patch_phase",
        "inqtrix.services.editor_assist_service",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.plan_models",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.prompts",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.skills_runtime",
        "inqtrix.core.constants",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.skills_runtime",
        "inqtrix.services.skill_service",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.source_policy",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.source_policy",
        "inqtrix.execution_authority",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.agents.synthesis",
        "inqtrix.core.results",
        "product code still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities",
        "inqtrix.services.editor_patch_service",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities",
        "inqtrix.services.editor_persistence_service",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities",
        "inqtrix.services.knowledge_service",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.context",
        "inqtrix.agents.context_archive",
        "a capability still reaches product code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.context",
        "inqtrix.agents.context_sources",
        "a capability still reaches product code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.editor",
        "inqtrix.agents.context_sources",
        "a capability still reaches product code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.editor",
        "inqtrix.project.editor_patch_ports",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.editor",
        "inqtrix.project.editor_ports",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.editor",
        "inqtrix.project.editor_search",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.editor",
        "inqtrix.services.editor_patch_service",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.editor",
        "inqtrix.services.editor_persistence_service",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.knowledge",
        "inqtrix.agents.context_sources",
        "a capability still reaches product code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.catalog.knowledge",
        "inqtrix.services.knowledge_service",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.capabilities.contracts",
        "inqtrix.auth.principal",
        "a capability still reaches application code directly",
    ),
    BoundaryException(
        "inqtrix.citation_labels",
        "inqtrix.agents.markdown",
        "a base module still reaches product code",
    ),
    BoundaryException(
        "inqtrix.evidence",
        "inqtrix.agents.evidence",
        "an engine still uses a helper that lives in product code",
    ),
    BoundaryException(
        "inqtrix.exceptions",
        "inqtrix.providers.base",
        "a base module still reaches an engine",
    ),
    BoundaryException(
        "inqtrix.execution_failures",
        "inqtrix.execution_authority",
        "observability still reaches application code",
    ),
    BoundaryException(
        "inqtrix.graph",
        "inqtrix.agents.markdown",
        "an engine still uses a helper that lives in product code",
    ),
    BoundaryException(
        "inqtrix.graph",
        "inqtrix.core.results",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.knowledge.algorithm",
        "inqtrix.agents.markdown",
        "an engine still uses a helper that lives in product code",
    ),
    BoundaryException(
        "inqtrix.knowledge.algorithm",
        "inqtrix.core.context",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.knowledge.algorithm",
        "inqtrix.core.results",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.knowledge.algorithm",
        "inqtrix.prompts",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.knowledge.contextualize",
        "inqtrix.prompts",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.knowledge.decompose",
        "inqtrix.prompts",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.knowledge.gate",
        "inqtrix.prompts",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.knowledge.source_cleanup",
        "inqtrix.storage.source_authority",
        "an engine still reaches infrastructure",
    ),
    BoundaryException(
        "inqtrix.knowledge.stores.memory",
        "inqtrix.execution_authority",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.nodes",
        "inqtrix.agents.markdown",
        "an engine still uses a helper that lives in product code",
    ),
    BoundaryException(
        "inqtrix.providers.anthropic",
        "inqtrix.state",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.providers.azure",
        "inqtrix.state",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.providers.bedrock",
        "inqtrix.state",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.providers.litellm",
        "inqtrix.state",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.providers.rerankers",
        "inqtrix.prompts",
        "an engine still reaches into another engine",
    ),
    BoundaryException(
        "inqtrix.quote_limits",
        "inqtrix.knowledge.grounding",
        "a base module still reaches an engine",
    ),
    BoundaryException(
        "inqtrix.research.web_research",
        "inqtrix.agents.source_policy",
        "an engine still uses a helper that lives in product code",
    ),
    BoundaryException(
        "inqtrix.research.web_research",
        "inqtrix.core.algorithms",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.research.web_research",
        "inqtrix.core.context",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.research.web_research",
        "inqtrix.core.results",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.result",
        "inqtrix.core.results",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.runs.indexing_memory",
        "inqtrix.execution_authority",
        "infrastructure still reaches application code",
    ),
    BoundaryException(
        "inqtrix.runs.indexing_memory",
        "inqtrix.indexing_failures",
        "infrastructure still reaches an engine",
    ),
    BoundaryException(
        "inqtrix.runs.indexing_memory",
        "inqtrix.knowledge.contextualize",
        "infrastructure still reaches an engine",
    ),
    BoundaryException(
        "inqtrix.runs.indexing_postgres",
        "inqtrix.execution_authority",
        "infrastructure still reaches application code",
    ),
    BoundaryException(
        "inqtrix.runs.memory_store",
        "inqtrix.execution_authority",
        "infrastructure still reaches application code",
    ),
    BoundaryException(
        "inqtrix.runs.postgres_store",
        "inqtrix.execution_authority",
        "infrastructure still reaches application code",
    ),
    BoundaryException(
        "inqtrix.runtime_logging",
        "inqtrix.scoring",
        "observability still reaches an engine",
    ),
    BoundaryException(
        "inqtrix.services.agent_source_service",
        "inqtrix.capabilities",
        "a service still calls the capability catalog directly",
    ),
    BoundaryException(
        "inqtrix.services.editor_assist_service",
        "inqtrix.server.editor_instructions",
        "run and editor types still live in the composition layer",
    ),
    BoundaryException(
        "inqtrix.services.editor_assist_service",
        "inqtrix.server.editor_suggestions",
        "run and editor types still live in the composition layer",
    ),
    BoundaryException(
        "inqtrix.services.editor_assist_service",
        "inqtrix.server.reference_documents",
        "run and editor types still live in the composition layer",
    ),
    BoundaryException(
        "inqtrix.settings",
        "inqtrix.core.constants",
        "a base module still reaches application code",
    ),
    BoundaryException(
        "inqtrix.settings",
        "inqtrix.report_profiles",
        "a base module still reaches an engine",
    ),
    BoundaryException(
        "inqtrix.state",
        "inqtrix.core.results",
        "an engine still reaches application code",
    ),
    BoundaryException(
        "inqtrix.storage.agent_control_postgres",
        "inqtrix.agents.artifact_lines",
        "infrastructure still uses a helper that lives in product code",
    ),
    BoundaryException(
        "inqtrix.storage.agent_control_postgres",
        "inqtrix.agents.context_archive",
        "infrastructure still uses a helper that lives in product code",
    ),
    BoundaryException(
        "inqtrix.storage.agent_control_postgres",
        "inqtrix.execution_authority",
        "infrastructure still reaches application code",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "langgraph.graph",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.algorithm",
        "langgraph.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.checkpointing",
        "langgraph.checkpoint.memory",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.checkpointing",
        "langgraph.checkpoint.postgres",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.checkpointing",
        "langgraph.checkpoint.serde.event_hooks",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.checkpointing",
        "langgraph.checkpoint.serde.jsonplus",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.harness",
        "langchain.agents",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.harness",
        "langchain.agents.middleware",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "langchain.agents.structured_output",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "langgraph.errors",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.algorithm",
        "langgraph.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.chat_bridge",
        "langchain_core.exceptions",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.chat_bridge",
        "langchain_core.language_models.chat_models",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.chat_bridge",
        "langchain_core.messages",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.chat_bridge",
        "langchain_core.outputs",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.chat_bridge",
        "langchain_core.utils.function_calling",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.middleware",
        "langchain.agents.middleware",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.middleware",
        "langchain.agents.middleware.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.middleware",
        "langchain_core.messages",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.middleware",
        "langgraph.errors",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.middleware",
        "langgraph.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.compaction",
        "langchain.agents.middleware",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.compaction",
        "langchain.agents.middleware.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.compaction",
        "langchain_core.messages",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.compaction",
        "langchain_core.messages.utils",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.compaction",
        "langchain_core.utils.function_calling",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.compaction",
        "langgraph.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.model_recovery",
        "langchain.agents.middleware.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.model_recovery",
        "langchain.agents.structured_output",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.model_recovery",
        "langchain_core.messages",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.model_recovery",
        "langchain_core.messages.utils",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.model_recovery",
        "langgraph.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "langchain_core.tools",
        "the framework stays behind the runtime adapter once it exists",
    ),
    BoundaryException(
        "inqtrix.agents.kernel.tools",
        "langgraph.types",
        "the framework stays behind the runtime adapter once it exists",
    ),
)
