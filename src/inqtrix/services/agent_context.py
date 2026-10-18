"""Per-request resolution of stack, overrides, and execution mode.

Moved from the monolithic route factory: this is where one HTTP body
turns into the provider/strategy/settings bundle an algorithm runs
against. The mode/skip_search conflict rules live here — they moved
with the code and are contract-locked (HTTP 400 with the exact German
messages) by ``tests/contract/test_chat_contract.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from inqtrix.execution_authority import requested_knowledge_scope
from inqtrix.knowledge.profiles import EVIDENCE_K_MAX, parse_knowledge_profile
from inqtrix.services.overrides import (
    AgentOverridesRequest,
    apply_overrides,
    parse_overrides_payload,
)
from inqtrix.settings import AgentSettings, Settings

log = logging.getLogger("inqtrix")

if TYPE_CHECKING:
    from inqtrix.core.algorithms import AlgorithmRegistry
    from inqtrix.providers.base import ProviderContext
    from inqtrix.strategies import StrategyContext


class StackResolutionError(Exception):
    """Raised when the multi-stack registry cannot resolve ``body['stack']``."""

    def __init__(self, message: str, available: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.available = available or []


@dataclass(frozen=True)
class ResolvedAgentContext:
    """Stack, settings, run-mode, and filters resolved from one request."""

    stack_name: str
    providers: "ProviderContext"
    strategies: "StrategyContext"
    agent_settings: AgentSettings
    agent_overrides: dict[str, Any]
    mode: str
    knowledge_filters: dict[str, Any]


def _validate_int_knowledge_filter(
    filters: dict[str, Any], name: str, lo: int, hi: int
) -> None:
    """Validate an optional integer ``knowledge_filters`` override in place.

    Rejects non-integers, ``bool`` (``isinstance(True, int)`` is True, but a
    boolean is never a valid count), and out-of-range values with HTTP 400 — so
    a bad ``top_k``/``final_k`` fails loudly on every execution path (chat,
    native runs, worker replay) instead of silently coercing to a surprising
    retrieval width. Absent keys are left untouched (server default applies).
    """
    raw = filters.get(name)
    if raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, int) or not (lo <= raw <= hi):
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": (
                        f'knowledge_filters.{name} must be an integer between {lo} and {hi}'
                    ),
                    "type": "invalid_request_error",
                }
            },
        )


class AgentContextResolver:
    """Resolve one request body into an executable agent context.

    Args:
        providers: Default provider bundle (single-stack identity).
        strategies: Default strategy bundle.
        settings: Root settings carrying the default agent settings.
        registry: Algorithm registry; its registration order drives the
            mode-validation error message, so the message stays exact
            as new algorithms register.
        stacks: Optional multi-stack registry
            (``name -> StackBundle``). ``None``/empty keeps the
            single-stack path.
        default_stack: Name of the stack used when the body names none.
    """

    def __init__(
        self,
        *,
        providers: "ProviderContext",
        strategies: "StrategyContext",
        settings: Settings,
        registry: "AlgorithmRegistry",
        stacks: dict[str, Any] | None = None,
        default_stack: str = "",
    ) -> None:
        self._providers = providers
        self._strategies = strategies
        self._settings = settings
        self._registry = registry
        self._stacks = stacks or {}
        self._default_stack = default_stack

    # -- stack resolution ------------------------------------------------ #

    def resolve_request_stack(self, body: dict[str, Any]) -> tuple[str, Any | None]:
        """Pick the stack bundle for this request.

        Returns:
            ``(stack_name, bundle)`` where ``bundle`` is the
            ``StackBundle`` to use. ``stack_name`` is empty when no
            multi-stack registry was supplied (single-stack mode).

        Raises:
            StackResolutionError: When ``body['stack']`` is not a
                string or names an unknown stack.
        """
        if not self._stacks:
            return "", None
        requested = body.get("stack")
        if requested is None:
            return self._default_stack, self._stacks[self._default_stack]
        if not isinstance(requested, str):
            raise StackResolutionError(
                f"Field 'stack' must be a string, got {type(requested).__name__}"
            )
        if requested not in self._stacks:
            raise StackResolutionError(
                f"Unknown stack {requested!r}",
                available=sorted(self._stacks.keys()),
            )
        return requested, self._stacks[requested]

    # -- mode resolution -------------------------------------------------- #

    def executes(self, mode: str) -> bool:
        """Whether this server can execute runs of *mode*.

        Server code that would start an internal child asks before it
        commits to that route; an optional engine (the kernel) may be absent.
        """
        return mode in self._registry.ids()

    def _assert_mode_registered(self, mode: str) -> None:
        """Reject a resolved mode the registry cannot execute — loudly.

        Explicit body modes are validated in :meth:`_parse_mode_payload`,
        but the INFERRED default (no ``mode`` field) falls back to the
        built-in ids. A custom registry that omits a built-in would
        otherwise surface as a bare 500 at ``registry.get(...)`` time —
        an invisible failure. Mapping it to the same 400 envelope keeps
        the error wire-visible and the registry the single execution
        truth.
        """
        if self.executes(mode):
            return
        log.warning(
            "Resolved mode %r is not registered in the registry (available: %s).",
            mode,
            ", ".join(self._registry.ids()),
        )
        listing = " or ".join(f"'{mode_id}'" for mode_id in self._registry.ids())
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": f'mode must be {listing}',
                "type": "invalid_request_error",
            }},
        )

    def _parse_mode_payload(self, body: dict[str, Any]) -> str | None:
        """Validate the optional top-level run mode against the registry."""
        raw_mode = body.get("mode")
        if raw_mode is None:
            return None
        registered = self._registry.ids()
        if raw_mode in registered:
            return raw_mode
        listing = " or ".join(f"'{mode_id}'" for mode_id in registered)
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": f'mode must be {listing}',
                "type": "invalid_request_error",
            }},
        )

    @staticmethod
    def _resolve_mode_settings(
        *,
        agent_settings: AgentSettings,
        overrides: AgentOverridesRequest | None,
        requested_mode: str | None,
    ) -> tuple[AgentSettings, str]:
        """Apply explicit mode semantics on top of resolved agent settings.

        The research/direct_llm split is carried by ``skip_search``;
        contradictions between an explicit ``mode`` and an explicit
        ``agent_overrides.skip_search`` are client errors and rejected
        with the exact historical messages.
        """
        if requested_mode is None:
            return (
                agent_settings,
                "direct_llm" if agent_settings.skip_search else "research",
            )

        has_skip_override = (
            overrides is not None
            and "skip_search" in getattr(overrides, "model_fields_set", set())
            and overrides.skip_search is not None
        )
        if has_skip_override:
            if requested_mode == "direct_llm" and overrides.skip_search is False:
                raise HTTPException(
                    status_code=400,
                    detail={"error": {
                        "message": (
                            ("mode='direct_llm' conflicts with "
                            "agent_overrides.skip_search=false")
                        ),
                        "type": "invalid_request_error",
                    }},
                )
            if requested_mode == "research" and overrides.skip_search is True:
                raise HTTPException(
                    status_code=400,
                    detail={"error": {
                        "message": (
                            ("mode='research' conflicts with "
                            "agent_overrides.skip_search=true")
                        ),
                        "type": "invalid_request_error",
                    }},
                )

        skip_search = requested_mode == "direct_llm"
        if agent_settings.skip_search is skip_search:
            return agent_settings, requested_mode
        return (
            agent_settings.model_copy(update={"skip_search": skip_search}),
            requested_mode,
        )

    # -- full resolution --------------------------------------------------- #

    def resolve(self, body: dict[str, Any]) -> ResolvedAgentContext:
        """Resolve stack, per-request overrides, and explicit run mode."""
        stack_name, stack_bundle = self.resolve_request_stack(body)
        active_providers = (
            stack_bundle.providers if stack_bundle is not None else self._providers
        )
        active_strategies = (
            stack_bundle.strategies if stack_bundle is not None else self._strategies
        )
        base_agent_settings = (
            stack_bundle.agent_settings
            if stack_bundle is not None and stack_bundle.agent_settings is not None
            else self._settings.agent
        )
        overrides = parse_overrides_payload(body.get("agent_overrides"))
        agent_overrides = (
            overrides.model_dump(mode="json", exclude_none=True)
            if overrides is not None
            else {}
        )
        requested_mode = self._parse_mode_payload(body)
        agent_settings = apply_overrides(base_agent_settings, overrides)
        agent_settings, mode = self._resolve_mode_settings(
            agent_settings=agent_settings,
            overrides=overrides,
            requested_mode=requested_mode,
        )
        self._assert_mode_registered(mode)
        raw_filters = body.get("knowledge_filters")
        if raw_filters is not None and not isinstance(raw_filters, dict):
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": "knowledge_filters must be an object",
                    "type": "invalid_request_error",
                }},
            )
        filters = raw_filters or {}
        raw_profile = filters.get("profile")
        if raw_profile is not None:
            # Validated here so a typo fails with 400 on every
            # execution path (chat, native runs, worker replay)
            # instead of silently running a different profile.
            try:
                parse_knowledge_profile(raw_profile)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                    }},
                ) from exc
        # Retrieval-width overrides validated at the same chokepoint. ``top_k``
        # mirrors the settings bound (``INQTRIX_KNOWLEDGE_TOP_K`` 1..50);
        # ``final_k`` cannot exceed the algorithm's evidence ceiling.
        _validate_int_knowledge_filter(filters, "top_k", 1, 50)
        _validate_int_knowledge_filter(filters, "final_k", 1, EVIDENCE_K_MAX)
        # The knowledge choice (chips or the session switch) is validated
        # here for every execution path, so an unknown scope or a
        # malformed chip list fails with 400 on chat completions exactly
        # as on native runs instead of being read as "no scope".
        try:
            requested_knowledge_scope(filters)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": str(exc),
                    "type": "invalid_request_error",
                }},
            ) from exc
        return ResolvedAgentContext(
            stack_name=stack_name,
            providers=active_providers,
            strategies=active_strategies,
            agent_settings=agent_settings,
            agent_overrides=agent_overrides,
            mode=mode,
            knowledge_filters=dict(raw_filters or {}),
        )

    def chat_settings_for_question(
        self,
        agent_settings: AgentSettings,
        question: str,
    ) -> AgentSettings:
        """Return request-local settings for direct chat with large attachments.

        ``max_question_length`` is a character guard for research
        questions. Direct chat already passed the route-level aggregate
        token cap, and the chat composer may inline attached documents
        into the final user message. In that mode, raise only the local
        character guard so the central graph check does not reject
        otherwise accepted payloads.

        Args:
            agent_settings: Resolved settings for this request after
                stack, override, and mode handling.
            question: The normalized current user message that will be
                passed to the agent entry point.

        Returns:
            The original settings for normal research or already-short
            direct chat requests; otherwise a request-local copy with
            ``max_question_length`` raised to the current message
            length.
        """
        if (
            not agent_settings.skip_search
            or len(question) <= agent_settings.max_question_length
        ):
            return agent_settings
        return agent_settings.model_copy(
            update={"max_question_length": len(question)}
        )
