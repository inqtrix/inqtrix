"""Effective-actor revocation checks at external-call boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from inqtrix.providers.base import (
    ChatTurn,
    LLMProvider,
    LLMResponse,
    ProviderContext,
    SearchProvider,
    StructuredLLMResponse,
    reported_capability,
)
from inqtrix.search_result import GroundedSearchResult

_T = TypeVar("_T")


class AuthorizationRevoked(RuntimeError):
    """Raised when the current effective actor may no longer continue.

    The stable ``code`` lets native runs, indexing jobs, and capability
    surfaces converge on one terminal reason without matching exception text.
    """

    code = "authorization_revoked"


def pinned_knowledge_collection_ids(
    knowledge_filters: Mapping[str, Any],
    *,
    scoped_principal: bool,
) -> frozenset[str] | None:
    """Restore the immutable knowledge boundary from a persisted request.

    A concrete list, including an empty list, is authoritative. Scoped HTTP
    admission always persists such a list; missing or null state for a scoped
    principal is therefore unsafe legacy input and fails closed to an empty
    boundary. Anonymous/static library execution keeps ``None`` because those
    modes have no per-user sharing boundary.

    Args:
        knowledge_filters: Persisted run request filters.
        scoped_principal: Whether the effective actor has a canonical user ID.

    Returns:
        The immutable collection IDs, an empty boundary for incomplete scoped
        state, or ``None`` for deliberately unscoped execution.

    Raises:
        RuntimeError: If persisted collection IDs are not a list of non-empty
            strings.
    """
    raw_collection_ids = knowledge_filters.get("collection_ids")
    if raw_collection_ids is None:
        return frozenset() if scoped_principal else None
    if not isinstance(raw_collection_ids, list) or any(
        not isinstance(item, str) or not item for item in raw_collection_ids
    ):
        raise RuntimeError(
            "Persisted knowledge collection scope must be a list of IDs."
        )
    return frozenset(raw_collection_ids)


def inherited_knowledge_filters(
    boundary: "frozenset[str] | None",
    *,
    explicit: bool,
    scope: str | None = None,
) -> dict[str, Any]:
    """The knowledge scope a DELEGATED child must be submitted with.

    A child is not admitted through the HTTP router, so the
    submission-time collection pin never runs for it — and
    :func:`pinned_knowledge_collection_ids` fails closed on a missing
    pin. Every delegated run therefore executed with an EMPTY knowledge
    boundary while its surface reported knowledge as available: it saw
    zero collections, asked the user for access it already had, or
    answered from nothing.

    Inheriting is not widening. The child receives EXACTLY the parent's
    admitted set, and ``None`` — deliberately unscoped execution with no
    per-user sharing boundary — is passed on as "no pin", never as an
    empty one.

    Args:
        boundary: The parent's admitted collection ids, or ``None`` for
            unscoped execution.
        explicit: Whether that scope is the user's own pick, carried so
            the child's gate predicate reads it the same way the
            parent's did.
        scope: The parent's persisted scope label (``"all"`` for the
            session switch), carried so the child's execution snapshot
            names the same provenance.

    Returns:
        Filters to merge into the child's resolved request; empty when
        there is no boundary to inherit.
    """
    if boundary is None:
        return {}
    filters: dict[str, Any] = {
        "collection_ids": sorted(boundary),
        "explicit": explicit,
    }
    # The provenance label rides along so a child reports the same
    # boundary the parent does ("all" = the session switch, otherwise
    # pinned chips), never a different word for the same collection set.
    if scope is not None:
        filters["scope"] = scope
    return filters


class _GuardedProvider:
    """Shared before/after check for one synchronous provider adapter."""

    def __init__(self, provider: object, check: Callable[[], None]) -> None:
        self._provider = provider
        self._check = check

    def _call(
        self,
        operation: Callable[..., _T],
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        """Check immediately around an external operation.

        The post-call check is the important half: if access is revoked while
        the remote system is working, its return value never reaches storage,
        events, or the next agent step.
        """
        self._check()
        result = operation(*args, **kwargs)
        self._check()
        return result

    def __getattr__(self, name: str) -> Any:
        """Expose provider metadata/retry observers without duplicating it."""
        return getattr(self._provider, name)


class AuthorizationGuardedLLM(_GuardedProvider, LLMProvider):
    """LLM provider decorator enforcing the effective actor at each call."""

    @property
    def selectable_models(self) -> list[str]:
        """Preserve the wrapped provider's selectable model catalogue."""
        return list(self._provider.selectable_models)

    @property
    def context_window_tokens(self) -> int | None:
        """Preserve the wrapped provider's configured context window."""
        return self._provider.context_window_tokens

    def complete(self, *args: Any, **kwargs: Any) -> str:
        """Run a text completion between live authorization checks."""
        return self._call(self._provider.complete, *args, **kwargs)

    def complete_with_metadata(
        self, *args: Any, **kwargs: Any
    ) -> LLMResponse:
        """Run a metered completion between live authorization checks."""
        return self._call(
            self._provider.complete_with_metadata, *args, **kwargs
        )

    def complete_structured(
        self, *args: Any, **kwargs: Any
    ) -> StructuredLLMResponse:
        """Run a structured completion between live checks."""
        return self._call(
            self._provider.complete_structured, *args, **kwargs
        )

    def chat(self, *args: Any, **kwargs: Any) -> ChatTurn:
        """Run a tool-capable chat turn between live checks."""
        return self._call(self._provider.chat, *args, **kwargs)

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        """Forward non-I/O structured-output capability discovery."""
        return reported_capability(
            self._provider, "supports_structured_output", model=model
        )

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        """Forward non-I/O tool-call capability discovery."""
        return reported_capability(
            self._provider, "supports_tool_calls", model=model
        )

    def is_available(self) -> bool:
        """Forward local provider readiness without consuming authority."""
        return self._provider.is_available()


class AuthorizationGuardedSearch(_GuardedProvider, SearchProvider):
    """Search provider decorator enforcing the effective actor per request."""

    def search(
        self, *args: Any, **kwargs: Any
    ) -> GroundedSearchResult:
        """Run one search between live authorization checks."""
        return self._call(self._provider.search, *args, **kwargs)

    def is_available(self) -> bool:
        """Forward local search readiness without consuming authority."""
        return self._provider.is_available()

    @property
    def search_model(self) -> str:
        """Expose the wrapped provider's operator-facing model label."""
        return self._provider.search_model


def guard_provider_context(
    providers: ProviderContext,
    check: Callable[[], None],
) -> ProviderContext:
    """Decorate the request-resolved LLM and search providers once.

    Algorithms continue to consume the established ``ProviderContext``. This
    avoids provider-specific branches and ensures every LLM/search operation,
    including calls inside helper strategies, shares the same safepoint rule.
    """
    return ProviderContext(
        llm=AuthorizationGuardedLLM(providers.llm, check),
        search=AuthorizationGuardedSearch(providers.search, check),
    )


def knowledge_store_required(*, mode: str, explicit_scope: bool) -> bool:
    """Whether a submission depends on a reachable knowledge store.

    A knowledge-desk ask and any run that pins collections explicitly (a
    chip, ``/wissen``) cannot do what the user asked without the store;
    admitting them during an outage would burn the run's cost before the
    first tool call fails. Runs that merely could use knowledge are
    admitted and their tool fails loudly instead.
    """
    return mode == "knowledge" or explicit_scope


KNOWLEDGE_SCOPE_ALL = "all"
KNOWLEDGE_SCOPE_PINNED = "pinned"
KNOWLEDGE_SCOPE_INHERITED = "inherited"
"""The only accepted value of ``knowledge_filters.scope``: every collection
the caller may see, resolved at submission and pinned like explicit ids."""


def requested_knowledge_scope(
    knowledge_filters: Mapping[str, Any],
) -> tuple[str | None, list[str]]:
    """Read the user's knowledge choice from a submitted request.

    Returns ``("all", [])`` for ``scope: "all"`` (the session switch),
    ``("pinned", ids)`` for a non-empty ``collection_ids`` list (chips) and
    ``(None, [])`` when the request names no knowledge at all. A pinned
    list wins over a scope flag so an ``@``-chip narrows the switch.

    Raises:
        ValueError: If ``scope`` carries anything but ``"all"`` — an
            unknown scope must never be read as "no scope".
    """
    raw_ids = knowledge_filters.get("collection_ids")
    if raw_ids is not None and not isinstance(raw_ids, list):
        raise ValueError("collection_ids must be a list of collection ids")
    ids = list(raw_ids or [])
    if any(not isinstance(item, str) or not item for item in ids):
        # A malformed chip list is a bad request, never "no chips": reading
        # it as unscoped would widen the run to every visible collection.
        raise ValueError("collection_ids must contain only non-empty collection ids")
    if ids:
        return KNOWLEDGE_SCOPE_PINNED, ids
    scope = knowledge_filters.get("scope")
    if scope is None or scope == "":
        return None, []
    if scope != KNOWLEDGE_SCOPE_ALL:
        raise ValueError(f"unknown knowledge scope {scope!r}; expected {KNOWLEDGE_SCOPE_ALL!r}")
    return KNOWLEDGE_SCOPE_ALL, []
