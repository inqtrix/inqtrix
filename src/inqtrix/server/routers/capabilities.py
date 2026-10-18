"""Capability manifest endpoint: clients discover features, never hardcode.

Unauthenticated by design, like ``/health``, ``/v1/models``, and
``/v1/stacks``: the UI needs the manifest before
any credential prompt, and the payload exposes only feature/algorithm
identity — no secrets, no per-deployment internals beyond what
``/health`` already reveals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from inqtrix.services.session_titler import title_capability

from inqtrix.services.attached_report_resolver import (
    MAX_ATTACHED_REPORTS,
)
from inqtrix.agents.report_requirement import (
    MAX_COMPOSED_REQUIREMENT_CHARS,
    REPORT_GUIDANCE_MAX_CHARS,
    REPORT_RULE_IDS_MAX,
)
from inqtrix.auth.principal import COOKIE_SESSION_MODES
from inqtrix.agents.kernel.algorithm import configured_kernel_limits
from inqtrix.agents.tier_policy import (
    DEFAULT_AGENT_TIER,
    tier_capabilities_payload,
)
from inqtrix.embedding_cards import build_embedding_catalog
from inqtrix.knowledge.profiles import (
    EVIDENCE_K_MAX,
    KnowledgeProfile,
    build_profile_manifest,
)
from inqtrix.providers.base import MAX_PROVIDER_ATTEMPTS
from inqtrix.services.request_parsing import (
    editor_wait_seconds,
    request_timeout_seconds,
    text_wait_seconds,
)
from inqtrix.services.system_runtime import (
    KNOWLEDGE_REASON_OPERATOR_DISABLED,
    knowledge_availability,
    runtime_feature_overrides,
    system_runtime_payload_checked,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def _permission_mode_entry(mode: str) -> dict[str, object]:
    """What one autonomy mode gates, derived from the ENFORCING code.

    Kernel tool gates come from the compiled policy config
    (``interrupt_config_for``); the web-replan rule is probed against
    the ACTUAL ``autonomy_auto_approves`` decision function with a
    synthetic web task — no literal here can drift from runtime
    behavior. ``plan_gate`` is the one graph-routing fact stated as a
    constant (autonomous skips the plan interrupt); ``patch_gate`` is
    invariant because ``propose_editor_patch`` is an always-gated tool
    and therefore interrupts in every mode.
    """
    from inqtrix.agents.control_ports import PlanTaskRecord
    from inqtrix.agents.kernel.policy import (
        ALWAYS_GATED_TOOLS,
        interrupt_config_for,
    )
    from inqtrix.agents.replan import autonomy_auto_approves

    config = interrupt_config_for(mode) or {}
    web_probe = PlanTaskRecord(
        task_id="probe",
        plan_id="probe",
        run_id="probe",
        ordinal=0,
        title="probe",
        tool_kind="web_research",
    )
    return {
        "plan_gate": mode != "autonomous",
        "web_replan_regate": not autonomy_auto_approves(
            autonomy=mode, new_tasks=[web_probe]
        ),
        "patch_gate": True,
        # Classification keys on ``user_conditional`` (set by the policy),
        # NOT the mere presence of a ``when`` predicate: a child-tool gate
        # carries an internal single-dispatch predicate but gates every
        # real delegation, so it belongs in ``kernel_gated_tools``. Only a
        # gate that leaves a user-triggerable path ungated (scoped
        # knowledge search) is ``conditional``.
        "kernel_gated_tools": sorted(
            name
            for name, entry in config.items()
            if name not in ALWAYS_GATED_TOOLS
            and not entry.get("user_conditional")
        ),
        "kernel_conditional_tools": sorted(
            name
            for name, entry in config.items()
            if entry.get("user_conditional")
        ),
        "kernel_always_gated": list(ALWAYS_GATED_TOOLS),
    }


def source_control(
    source_id: str,
    *,
    registered: bool,
    available: bool,
    reason_code: str | None,
) -> dict[str, object]:
    """One composer source control with an honest availability and reason.

    ``registered`` is the runtime registry (the capability exists);
    ``available``/``reason_code`` come from the source's own availability
    derivation (:func:`knowledge_availability` for project knowledge). A
    source is available only when both hold; otherwise ``reason_code``
    says why so the client can explain instead of hiding the control or,
    worse, advertising a dead one.
    """
    effective = registered and available
    reason = (
        None
        if effective
        else (reason_code or KNOWLEDGE_REASON_OPERATOR_DISABLED)
        if registered
        else KNOWLEDGE_REASON_OPERATOR_DISABLED
    )
    return {
        "id": source_id,
        "default": "available",
        "available": effective,
        "reason_code": reason,
    }


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the capability manifest route against the container."""
    router = APIRouter()
    registry = container.registry
    settings = container.settings
    knowledge_service = container.knowledge_service

    @router.get("/v1/capabilities")
    async def capabilities():
        runtime = await system_runtime_payload_checked(container)
        feature_overrides = runtime_feature_overrides(runtime)
        knowledge_enabled = feature_overrides.get("knowledge", False)
        knowledge_available, knowledge_reason = await knowledge_availability(
            container, vector_store_available=knowledge_enabled
        )
        files_enabled = feature_overrides.get("files", False)
        capability_ids = (
            set(container.capability_registry.ids())
            if container.capability_registry is not None
            else set()
        )
        web_source_available = "web.search.instant" in capability_ids
        knowledge_source_available = "knowledge.search" in capability_ids
        collaboration_service = container.editor_collaboration_service
        sharing_settings = getattr(settings, "sharing", None)
        guest_link_settings = getattr(settings, "editor_guest_links", None)
        sharing_available = bool(container.share_service is not None)
        sharing_configured = bool(
            getattr(sharing_settings, "enabled", sharing_available)
        )
        collaboration_configured = bool(
            getattr(
                settings.collaboration,
                "enabled",
                collaboration_service is not None,
            )
        )
        collaboration_available = bool(
            collaboration_service is not None
            and await collaboration_service.service_available()
        )
        guest_links_configured = bool(
            getattr(guest_link_settings, "enabled", False)
        )
        guest_links_available = bool(
            getattr(container, "editor_guest_link_service", None) is not None
            and sharing_available
            and collaboration_available
        )

        def feature_status(
            *,
            configured: bool,
            available: bool,
            unavailable_reason: str,
        ) -> dict[str, object]:
            if available:
                return {
                    "configured": True,
                    "available": True,
                    "state": "enabled",
                    "reason_code": None,
                }
            return {
                "configured": configured,
                "available": False,
                "state": "degraded" if configured else "disabled",
                "reason_code": (
                    unavailable_reason if configured else "operator_disabled"
                ),
            }

        sharing_reason = (
            "auth_mode_unsupported"
            if getattr(
                getattr(container, "auth_provider", None),
                "mode",
                "none",
            )
            not in COOKIE_SESSION_MODES
            else "postgres_required"
            if settings.storage.backend != "postgres"
            else "service_unreachable"
        )
        collaboration_reason = (
            "postgres_required"
            if settings.storage.backend != "postgres"
            else "auth_mode_unsupported"
            if getattr(
                getattr(container, "auth_provider", None),
                "mode",
                "none",
            )
            not in COOKIE_SESSION_MODES
            else "service_unreachable"
        )
        guest_links_reason = (
            "sharing_required"
            if not sharing_available
            else "collaboration_required"
            if not collaboration_available
            else "https_required"
            if (
                not str(
                    getattr(settings.server, "public_base_url", "")
                ).lower().startswith("https://")
                # The explicit insecure-HTTP opt-in serves guest links
                # over http — reporting https_required then would make
                # the FE hide a feature the server actually provides.
                and not settings.editor_guest_links.allow_insecure_http
            )
            else "service_unreachable"
        )
        kernel_limit_values = {
            "schnell": configured_kernel_limits(
                settings.agent_platform, depth="normal", tier="schnell"
            ),
            "normal": configured_kernel_limits(
                settings.agent_platform, depth="normal", tier="gruendlich"
            ),
            "deep": configured_kernel_limits(
                settings.agent_platform, depth="deep", tier="tief"
            ),
        }

        def kernel_limit_payload(
            values: tuple[int, int, int, int],
        ) -> dict[str, int]:
            tool_calls, tool_ceiling, steps, step_ceiling = values
            return {
                "tool_calls": tool_calls,
                "tool_calls_ceiling": tool_ceiling,
                "steps": steps,
                "steps_ceiling": step_ceiling,
            }
        payload = {
            "session_titles": {
                "enabled": settings.server.session_titles_enabled,
                **title_capability(
                    container.providers.llm, enabled=settings.server.session_titles_enabled,
                ),
            },
            "algorithms": registry.manifest(),
            "features": {
                "knowledge": knowledge_enabled,
                "embedding_provider": feature_overrides.get(
                    "embedding_provider", knowledge_enabled
                ),
                "openapi": settings.server.enable_openapi,
                # Whether GET /v1/stacks exists. Only the multi-stack factory
                # mounts it, so a browser that probes blindly guarantees one
                # 404 per page load in every single-stack deployment — and a
                # console with a guaranteed error in it stops being read.
                "multi_stack": getattr(container, "stacks", None) is not None,
                "files": files_enabled,
                # Server-side document parsing is pure MarkItDown CPU work with
                # no vector-store dependency, so it is advertised at the top
                # level (not inside the knowledge block) and stays available on
                # a transient vector-store outage. The browser gates its upload
                # parse on this; the GET /v1/files/{id}/text endpoint mirrors it.
                "document_parser": feature_overrides.get("document_parser", False),
                "sharing": sharing_available,
                # Browsers sync rules against this surface — only a
                # DURABLE store may advertise it (a volatile store
                # reads as "everything deleted" after a restart).
                "prompt_templates": (
                    container.prompt_template_service is not None
                    and container.prompt_template_service.durable
                ),
                # Skills follow the same durable-store rule.
                "skills": (
                    container.skill_service is not None
                    and container.skill_service.durable
                ),
                # Per-user quotas are enforced — the UI shows the meter
                # and (for owners) the admin panel only when this is on.
                "quota": container.quota_service is not None,
                # The project-persistence tier (chat/editor/assets/vector-index)
                # is server-durable — the frontend may move its local project
                # to the server only when this is on (a volatile store
                # would read as "everything deleted" after a restart, the
                # same rule as prompt_templates).
                "project_persistence": (
                    container.chat_history_service is not None
                    and container.chat_history_service.durable
                ),
                "collaboration": collaboration_available,
                "editor_guest_links": guest_links_available,
                "editor_guest_link_stats": bool(
                    guest_links_available
                    and bool(getattr(guest_link_settings, "stats_enabled", False))
                ),
                # Agent capability tools are registered in the same curated
                # registry consumed by deepagents and MCP adapters. This is on
                # when at least one capability is wired.
                "agent_tools": bool(
                    container.capability_registry is not None
                    and container.capability_registry.ids()
                ),
                "workspace_agent": "workspace_agent"
                in container.registry.ids(),
                "workspace_agent_durable": bool(
                    "workspace_agent" in container.registry.ids()
                    and settings.storage.backend == "postgres"
                ),
                # The cognitive kernel is true only when the registration
                # gate passed (rollout switch AND
                # checkpointer AND native tool calling) — frontends
                # feature-detect mode=agent_kernel through this flag.
                "agent_kernel": "agent_kernel" in container.registry.ids(),
            },
            "collaboration": {
                "configured": collaboration_configured,
                "service_available": collaboration_available,
                "transport_path": "/collaboration",
                "protocol_version": settings.collaboration.protocol_version,
                "schema_version": settings.collaboration.schema_version,
                "mode": "single_replica",
            },
            "feature_status": {
                # Why knowledge is unavailable: switched off by the operator
                # or its store not answering -- a client names that reason
                # instead of offering anything it cannot back.
                "knowledge": feature_status(
                    configured=knowledge_service is not None,
                    available=knowledge_available,
                    unavailable_reason=knowledge_reason or "",
                ),
                "sharing": feature_status(
                    configured=sharing_configured,
                    available=sharing_available,
                    unavailable_reason=sharing_reason,
                ),
                "collaboration": feature_status(
                    configured=collaboration_configured,
                    available=collaboration_available,
                    unavailable_reason=collaboration_reason,
                ),
                "editor_guest_links": feature_status(
                    configured=guest_links_configured,
                    available=guest_links_available,
                    unavailable_reason=guest_links_reason,
                ),
                # The operator asked for the kernel and the registration
                # gate decided; "unavailable" without its reason would be
                # exactly the silent fallback the gate exists to prevent.
                "agent_kernel": feature_status(
                    configured=bool(settings.agent_platform.kernel_enabled),
                    available="agent_kernel" in container.registry.ids(),
                    unavailable_reason=(
                        getattr(container, "kernel_gate_reason", "")
                        or "unknown"
                    ),
                ),
                "editor_guest_link_stats": feature_status(
                    configured=(
                        guest_links_configured
                        and bool(
                            getattr(guest_link_settings, "stats_enabled", False)
                        )
                    ),
                    available=bool(
                        guest_links_available
                        and bool(
                            getattr(guest_link_settings, "stats_enabled", False)
                        )
                    ),
                    unavailable_reason=(
                        "guest_links_required"
                        if not guest_links_available
                        else "operator_disabled"
                    ),
                ),
            },
            # The desk reads workspace-agent limits and vocabulary from
            # this contract instead of hardcoding them.
            "agent": {
                "autonomy_modes": ["strict", "balanced", "autonomous"],
                "default_autonomy": settings.agent_platform.default_autonomy,
                # The effective desk default publishes the configured
                # kernel default only once the
                # kernel actually registered — the frontend never
                # submits an unregistered mode.
                "default_mode": (
                    "agent_kernel"
                    if settings.agent_platform.default_agent_mode
                    == "agent_kernel"
                    and "agent_kernel" in container.registry.ids()
                    else "workspace_agent"
                ),
                # The composer renders the two-mode Standard/Auto presets
                # and maps them onto the
                # UNCHANGED wire vocabulary above. advanced_autonomy=True
                # republishes the legacy three-way control instead.
                "mode_presets": [
                    {"id": "standard", "autonomy": "balanced"},
                    {"id": "auto", "autonomy": "autonomous"},
                ],
                # Thoroughness is orthogonal to the permission mode; the
                # composer renders the toggle only when the
                # server publishes it (feature detection, no hardcoded
                # claim).
                "depth_modes": [
                    {"id": "normal"},
                    {"id": "deep"},
                ],
                # Mirrors the apply_overrides env-tier bridge: with an
                # env AGENT_TIER set, every no-override run gets the
                # bridged depth — publish THAT, not the raw env value.
                "default_depth": (
                    ("deep" if settings.agent.agent_tier == "tief" else "normal")
                    if settings.agent.agent_tier
                    else settings.agent.depth
                ),
                # Stufen (published == enforced: generated from THE
                # policy table every consumer reads). `depth_modes`
                # stays published for older clients; a tiers-aware
                # composer renders the Stufe control instead.
                "tiers": tier_capabilities_payload(
                    max_clarification_rounds=(
                        settings.agent_platform.max_clarification_rounds
                    ),
                    section_register_override=(
                        settings.agent_platform.synthesis_section_register_budget
                    ),
                ),
                # The composer's default SELECTION (env AGENT_TIER wins
                # over the vocabulary default). A request that OMITS
                # agent_tier keeps legacy depth semantics — tiers-aware
                # clients therefore always send their selection.
                "default_tier": (
                    settings.agent.agent_tier or DEFAULT_AGENT_TIER
                ),
                "advanced_autonomy": (
                    settings.agent_platform.advanced_autonomy
                ),
                "max_parallel_children": (
                    settings.agent_platform.max_parallel_children
                ),
                "discovery_max_tool_calls": (
                    settings.agent_platform.discovery_max_tool_calls
                ),
                "max_plan_tasks": settings.agent_platform.max_plan_tasks,
                # Result requirement: published == enforced. The
                # composer and the plan gate both render THESE numbers
                # instead of repeating the server's constants, so a
                # text the surface accepts is never one the server
                # refuses. ``max_rules`` counts distinct rules;
                # ``max_composed_chars`` is the ceiling of the composed
                # block, checked against each template's published
                # ``composed_chars`` before the submit.
                "report_requirement": {
                    "max_chars": REPORT_GUIDANCE_MAX_CHARS,
                    "max_rules": REPORT_RULE_IDS_MAX,
                    "max_composed_chars": MAX_COMPOSED_REQUIREMENT_CHARS,
                },
                # Attached research reports: published == enforced,
                # so the composer disables the fourth chip instead of the
                # server refusing the submission afterwards.
                "attached_reports": {"max_reports": MAX_ATTACHED_REPORTS},
                "limits": {
                    "tokens": {
                        "enabled": settings.quota.max_tokens_per_run > 0,
                        "limit": max(0, settings.quota.max_tokens_per_run),
                        "ceiling": max(0, settings.quota.max_tokens_per_run),
                        "recoverable": False,
                        "extendable": False,
                        "reason": "operator_ceiling_exactly_once_required",
                    },
                    "kernel": {
                        key: kernel_limit_payload(values)
                        for key, values in kernel_limit_values.items()
                    },
                    "mission": {
                        "discovery_tool_calls": settings.agent_platform.discovery_max_tool_calls,
                        "plan_tasks": settings.agent_platform.max_plan_tasks,
                        "replan_rounds": settings.agent_platform.max_replan_rounds,
                        "clarification_rounds": settings.agent_platform.max_clarification_rounds,
                        "parallel_children": settings.agent_platform.max_parallel_children,
                        "documents_per_analysis": settings.agent_platform.document_analysis_max_documents,
                        "document_windows_per_analysis": settings.agent_platform.document_analysis_max_windows,
                        "document_windows_per_run": settings.agent_platform.document_analysis_max_windows_per_run,
                    },
                    "research": {
                        "rounds": settings.agent.max_rounds,
                    },
                },
                "durable": settings.storage.backend == "postgres",
                "tools": (
                    container.capability_registry.manifest()
                    if container.capability_registry is not None
                    else []
                ),
                # What each permission mode actually gates — generated
                # from the enforcing sources (kernel policy config and
                # replan authorization rule), so published == enforced and
                # the composer's run overview cannot drift from the
                # runtime or promise what the runtime does not do.
                "permission_modes": {
                    mode: _permission_mode_entry(mode)
                    for mode in ("strict", "balanced", "autonomous")
                },
                # Only skill limits are exposed here. The skill list itself
                # comes from the authenticated GET /v1/skills — this
                # endpoint is unauthenticated and must never leak
                # titles or labels.
                "skills": {
                    "max_attached": (
                        settings.agent_platform.skills_max_attached
                    ),
                    "disclosure_budget_chars": (
                        settings.agent_platform.skills_disclosure_budget_chars
                    ),
                },
                # The session knowledge switch: `knowledge_filters.scope =
                # "all"` is accepted and pinned to every collection the
                # caller may see (an unscoped principal keeps its tenant-wide
                # view). Older servers omit the flag.
                "knowledge_scope_all": True,
                # Composer source controls.
                # Availability is derived from the SAME registries runtime
                # dispatch uses, so the desk never advertises a dead control.
                "source_controls": [
                    source_control(
                        "web",
                        registered=web_source_available,
                        available=True,
                        reason_code=None,
                    ),
                    # The registry says the tool exists; the shared
                    # derivation (service wired AND store answering) says
                    # whether it works — the same derivation the run
                    # admission applies, so the desk never advertises a
                    # control whose first use is refused.
                    source_control(
                        "knowledge",
                        registered=knowledge_source_available,
                        available=knowledge_available,
                        reason_code=knowledge_reason,
                    ),
                ],
            },
            # Effective HTTP wait deadlines (seconds) the browser must NOT
            # abort before. Derived from the SAME helpers the editor/text/chat
            # routes enforce, so the published value provably equals what
            # actually runs — the client derives its own AbortController
            # timeout from these instead of hardcoding one (which would
            # silently cap a raised server-side timeout). Plain integers from
            # non-secret settings, consistent with this endpoint's contract.
            "timeouts": {
                "editor_wait_seconds": editor_wait_seconds(settings.agent),
                "chat_wait_seconds": request_timeout_seconds(settings.agent),
                "text_wait_seconds": text_wait_seconds(settings.agent),
                "reasoning_operation_seconds": settings.agent.reasoning_timeout,
                "editor_operation_seconds": (
                    settings.agent.editor_assistant_timeout
                ),
                "search_operation_seconds": settings.agent.search_timeout,
                "claim_extract_operation_seconds": (
                    settings.agent.claim_extract_timeout
                ),
                "research_run_seconds": settings.agent.max_total_seconds,
                "max_attempts": MAX_PROVIDER_ATTEMPTS,
            },
        }
        if files_enabled:
            payload["files"] = {
                "max_file_bytes": settings.storage.max_file_bytes,
            }
        if knowledge_enabled:
            context = knowledge_service.knowledge
            payload["features"]["hybrid_retrieval"] = feature_overrides.get(
                "hybrid_retrieval", False
            )
            payload["features"]["reranker"] = feature_overrides.get(
                "reranker", False
            )
            payload["features"]["contextual_retrieval"] = (
                context.contextualizer is not None
            )
            embeddings = context.embeddings
            # The store's lexical-branch language is the single truth for both
            # the normalized mode (bm25/off) and the language code.
            sparse_language = getattr(context.store, "sparse_language", None)
            payload["knowledge"] = {
                "default_embedding_model": embeddings.default_model,
                "embedding_catalog": build_embedding_catalog(
                    embeddings.selectable_embedding_models
                    or [embeddings.default_model]
                ),
                "default_top_k": knowledge_service.knowledge.default_top_k,
                # Hard ceiling on the FINAL evidence count (``final_k``), so a
                # client can bound its ``final_k`` override field to the same
                # cap the algorithm clamps to.
                "evidence_k_max": EVIDENCE_K_MAX,
                # Keyword (BM25) retrieval is language-bound and never
                # cross-lingual; the cross-lingual lever is a multilingual
                # cross-encoder reranker. Static facts so the UI can show the
                # limitation honestly instead of silently expecting more.
                "sparse_mode": "bm25" if sparse_language is not None else "off",
                "sparse_language": sparse_language,
                "sparse_multilingual": False,
                "cross_lingual_recommendation": "reranker",
                # Configured vector backend NAME (e.g. "qdrant"), a
                # descriptive label for retrieval-source displays — not a
                # reachability claim (that stays with the system-runtime
                # probes).
                "vector_backend": settings.knowledge.vector_backend,
            }
            if container.knowledge_ceiling is not None:
                # The SAME ceiling instance the algorithm runs against
                # — the manifest must describe what would actually
                # execute, including operator degradation per profile.
                payload["knowledge"]["profiles"] = build_profile_manifest(
                    container.knowledge_ceiling
                )
                payload["knowledge"]["default_profile"] = (
                    KnowledgeProfile.STANDARD.value
                )
                payload["knowledge"]["reranker_provider"] = (
                    settings.knowledge.reranker_provider
                )
        return payload

    return router
