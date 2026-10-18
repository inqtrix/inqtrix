"""Azure AI Foundry Web Search adapter for the SearchProvider interface.

Queries a pre-created Azure AI Foundry agent through the Responses API.
API-key deployments use the OpenAI data-plane client directly with the
``api-key`` header. Entra ID deployments use
``AIProjectClient.get_openai_client()`` to mint and refresh bearer tokens.
Both paths reference the configured agent per request through
``extra_body["agent_reference"]`` so the optional agent version is pinned.

The agent must be created beforehand (via Azure Portal or CLI). Pass its
``agent_name`` (and optionally ``agent_version``) and the project endpoint
to the constructor.

Four auth modes are supported (resolved in this order):

1. **Project API key** — pass ``api_key``; sent in the ``api-key`` header.
2. An explicit ``credential`` object.
3. **Service Principal** — ``tenant_id`` + ``client_id`` + ``client_secret``.
4. ``DefaultAzureCredential`` — fallback (``az login``, Managed Identity,
   VS Code sign-in, etc.).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import replace
from typing import Any

from openai import APIStatusError, OpenAI, OpenAIError, RateLimitError

from inqtrix.exceptions import (
    AgentCancelled,
    AgentRateLimited,
    AgentProviderTimeout,
    AgentTimeout,
)
from inqtrix.constants import SEARCH_TIMEOUT
from inqtrix.providers._azure_common import (
    extract_azure_api_error_details,
    resolve_azure_credential,
)
from inqtrix.providers.base import (
    SearchProvider,
    _NonFatalNoticeMixin,
    _RetryNoticeMixin,
    _apply_domain_filters,
    _bounded_timeout,
    _build_recency_language_hints,
    _call_openai_chat_completion_with_retries,
    _check_deadline,
    _check_provider_operation_deadline,
    _operation_deadline,
    responses_usage_details,
    response_raw_payload,
)
from inqtrix.search_result import CitationSpan, GroundedSearchResult, GroundedSource
from inqtrix.urls import (
    CredentialBearingUrlError,
    extract_urls,
    safe_public_url_identity,
)

log = logging.getLogger("inqtrix")

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
def _annotation_offset(value: Any) -> int | None:
    """Normalize an optional Responses API citation character offset."""

    return value if isinstance(value, int) and value >= 0 else None


class AzureFoundryWebSearch(_RetryNoticeMixin, _NonFatalNoticeMixin, SearchProvider):
    """Query the web via an Azure AI Foundry agent and the Responses API.

    Use this provider when search should run through a pre-created Foundry
    agent referenced by name. The agent's synthesized answer is one prose
    block with inline Markdown source links; there are no per-source bodies,
    so the cited URLs are surfaced as :class:`GroundedSource` anchors (with
    empty snippets) and the answer carries the content.

    Attributes:
        _agent_name (str): Foundry agent name referenced through the SDK.
        _agent_version (str): Optional agent version, shown to operators.
        _timeout (float): Per-call timeout budget before deadline clamping.
        _client: Authenticated OpenAI client from
            ``AIProjectClient.get_openai_client``.
    """

    supported_search_parameters = frozenset({
        "recency_filter",
        "language_filter",
        "domain_filter",
    })
    def __init__(
        self,
        *,
        project_endpoint: str,
        agent_name: str,
        agent_version: str = "",
        api_key: str | None = None,
        credential: Any | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = SEARCH_TIMEOUT,
        max_concurrency: int = 6,
        # Internal: accept a pre-built OpenAI client from tests.
        _client: Any | None = None,
    ) -> None:
        """Initialize the Azure AI Foundry Web Search provider.

        Args:
            project_endpoint: Azure AI Foundry project endpoint, e.g.
                ``"https://<resource>.services.ai.azure.com/api/projects/<proj>"``.
            agent_name: Name of the existing Web Search agent.
            agent_version: Optional version label, surfaced in
                ``search_model``. The SDK routes by agent name to the
                agent's deployed version.
            api_key: Optional static Foundry project key. When supplied it
                wins over all credential modes and is sent in the
                ``api-key`` header.
            credential: Optional pre-built Azure token credential. When
                supplied it wins over the Service Principal fields.
            tenant_id: Optional Entra tenant id for Service Principal auth.
            client_id: Optional Entra client id for Service Principal auth.
            client_secret: Optional Entra client secret for Service
                Principal auth.
            timeout: Default per-call timeout budget in seconds.
            max_concurrency: Maximum simultaneous calls on this provider
                instance. Research and Agent Desk callers share this gate.
            _client: Optional prebuilt OpenAI client used by tests.

        Raises:
            ValueError: If ``project_endpoint`` or ``agent_name`` is empty.

        Example:
            >>> from inqtrix import AzureFoundryWebSearch
            >>> from azure.identity import DefaultAzureCredential
            >>> search = AzureFoundryWebSearch(
            ...     project_endpoint="https://ex.services.ai.azure.com/api/projects/demo",
            ...     agent_name="web-search-agent",
            ...     agent_version="2",
            ...     credential=DefaultAzureCredential(),
            ... )
            >>> search.is_available()
            True
        """
        if not project_endpoint:
            raise ValueError("project_endpoint is required")
        if not agent_name:
            raise ValueError("agent_name is required")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")

        self._project_endpoint = project_endpoint.rstrip("/")
        self._agent_name = agent_name
        self._agent_version = agent_version
        self._timeout = timeout
        self.max_search_concurrency = int(max_concurrency)
        self._concurrency_gate = threading.BoundedSemaphore(
            self.max_search_concurrency
        )

        if _client is not None:
            self._client = _client
        elif api_key:
            # Static Foundry project key: the agent's data-plane endpoint
            # (``/openai/v1``) authenticates via the ``api-key`` header, so the
            # OpenAI client is built directly against it. AIProjectClient is the
            # control-plane SDK (Entra ID only) and is not used for key auth.
            # The agent (and its version) is selected per call via
            # ``agent_reference`` in :meth:`_call_responses`.
            self._client = OpenAI(
                base_url=f"{self._project_endpoint}/openai/v1/",
                api_key=api_key,
                default_headers={"api-key": api_key},
                timeout=timeout,
                max_retries=0,
            )
        else:
            # Entra ID token credential: AIProjectClient.get_openai_client()
            # mints and auto-refreshes the bearer token. No ``agent_name`` is
            # bound at the client; the agent is referenced per call instead.
            from azure.ai.projects import AIProjectClient

            credential = resolve_azure_credential(
                credential=credential,
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
            )
            project_client = AIProjectClient(
                endpoint=self._project_endpoint,
                credential=credential,
                allow_preview=True,
            )
            self._client = project_client.get_openai_client().with_options(
                max_retries=0,
                timeout=timeout,
            )

    _extract_api_error_details = staticmethod(extract_azure_api_error_details)

    # ------------------------------------------------------------------
    # SearchProvider interface
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        search_context_size: str = "high",
        recency_filter: str | None = None,
        language_filter: list[str] | None = None,
        domain_filter: list[str] | None = None,
        search_mode: str | None = None,
        return_related: bool = False,
        deadline: float | None = None,
    ) -> GroundedSearchResult:
        """Execute one deadline-bounded search through the shared gate."""
        operation_deadline = _operation_deadline(self._timeout, deadline)
        remaining = max(0.0, operation_deadline - time.monotonic())
        if not self._concurrency_gate.acquire(timeout=remaining):
            _check_provider_operation_deadline(
                operation_deadline,
                deadline,
                label="Azure-Foundry-WebSearch",
            )
            raise AgentProviderTimeout(
                ("Azure Foundry web search reached the operation deadline while waiting "
                "for a concurrency slot.")
            )
        try:
            return self._search_with_slot(
                query,
                search_context_size=search_context_size,
                recency_filter=recency_filter,
                language_filter=language_filter,
                domain_filter=domain_filter,
                search_mode=search_mode,
                return_related=return_related,
                deadline=operation_deadline,
                outer_deadline=deadline,
            )
        finally:
            self._concurrency_gate.release()

    def _search_with_slot(
        self,
        query: str,
        *,
        search_context_size: str = "high",
        recency_filter: str | None = None,
        language_filter: list[str] | None = None,
        domain_filter: list[str] | None = None,
        search_mode: str | None = None,
        return_related: bool = False,
        deadline: float | None = None,
        outer_deadline: float | None = None,
    ) -> GroundedSearchResult:
        """Execute a search through the Foundry agent's Responses API.

        Args:
            query: User-facing search query text.
            search_context_size: Accepted for ABC compatibility; the agent
                decides its own context depth, so this value is not used.
            recency_filter: Optional recency hint, applied as best-effort
                prompt guidance prepended to the query.
            language_filter: Optional language hint, applied as best-effort
                prompt guidance.
            domain_filter: Optional allow/deny domain list, injected as
                ``site:`` / ``-site:`` operators into the query text.
            search_mode: Accepted for ABC compatibility; not used.
            return_related: Accepted for ABC compatibility; not used.
            deadline: Optional absolute monotonic deadline for the run.

        Returns:
            GroundedSearchResult: The agent's synthesized answer plus the
            cited URLs as URL-only sources (empty snippets).

        Raises:
            AgentTimeout: If the global deadline has already elapsed.
            AgentRateLimited: If the backend surfaces a fatal rate limit.
        """
        self._clear_nonfatal_notice()
        self._clear_retry_notices()

        if deadline is not None:
            _check_deadline(deadline)

        effective_query = _apply_domain_filters(query, domain_filter)
        hint = _build_recency_language_hints(recency_filter, language_filter)
        user_content = f"{hint}\n\n{effective_query}" if hint else effective_query

        # Reference the agent (and pin its version when configured) per call,
        # the documented Foundry pattern. Without the version the backend runs
        # the agent's "latest" revision, which may differ from the deployed
        # (e.g. reasoning-effort) configuration the operator expects.
        agent_ref: dict[str, str] = {"type": "agent_reference", "name": self._agent_name}
        if self._agent_version:
            agent_ref["version"] = self._agent_version
        try:
            response = _call_openai_chat_completion_with_retries(
                provider_label=type(self).__name__,
                model=self.search_model,
                operation="web_search",
                deadline=deadline,
                outer_deadline=outer_deadline,
                timeout_label="Azure-Foundry-WebSearch",
                configured_timeout_seconds=self._timeout,
                create=lambda: self._client.responses.create(
                    input=[{"role": "user", "content": user_content}],
                    extra_body={"agent_reference": agent_ref},
                    timeout=_bounded_timeout(self._timeout, deadline),
                ),
                append_retry_notice=self._append_retry_notice,
            )
        except RateLimitError as exc:
            raise AgentRateLimited(self._agent_name, exc) from exc
        except APIStatusError as exc:
            details = self._extract_api_error_details(exc)
            if details["status_code"] == 429:
                raise AgentRateLimited(self._agent_name, exc) from exc
            status_code = int(details["status_code"] or 502)
            log.error(
                "Azure-Foundry-WebSearch failed (status=%s, type=%s)",
                status_code,
                type(exc).__name__,
            )
            self._set_nonfatal_notice(
                "Azure-Foundry-WebSearch failed; the empty result remains "
                "a visible evidence gap.",
                code=(
                    "provider_timeout"
                    if status_code == 408
                    else (
                        "upstream_5xx"
                        if status_code >= 500
                        else "provider_error"
                    )
                ),
                http_status=status_code,
            )
            return GroundedSearchResult()
        except AgentTimeout:
            raise
        except OpenAIError as exc:
            exc_text = str(exc).lower()
            if "timeout" in exc_text or "timed out" in exc_text:
                raise AgentProviderTimeout(
                    "Azure-Foundry-WebSearch provider timeout"
                ) from exc
            log.error(
                "Azure-Foundry-WebSearch failed (type=%s)",
                type(exc).__name__,
            )
            self._set_nonfatal_notice(
                "Azure-Foundry-WebSearch failed; the empty result remains "
                "a visible evidence gap.",
                code="temporary_transport",
                http_status=503,
            )
            return GroundedSearchResult()
        except AgentCancelled:
            # Run cancellation is not a provider failure: it must unwind
            # into the run's cancelled terminal state, never degrade into
            # a fake empty result with a provider_error notice.
            raise
        except Exception as exc:  # noqa: BLE001 -- non-fatal degrade (see gotcha #11)
            log.error(
                "Azure-Foundry-WebSearch failed (type=%s)",
                type(exc).__name__,
            )
            self._set_nonfatal_notice(
                "Azure-Foundry-WebSearch failed; the empty result remains "
                "a visible evidence gap.",
                code="provider_error",
                http_status=502,
            )
            return GroundedSearchResult()

        result = self._parse_response(response)
        if not result.answer:
            self._set_nonfatal_notice(
                "Azure-Foundry-WebSearch returned no answer text."
            )
        return result

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(response: Any) -> GroundedSearchResult:
        """Extract the answer and cited URLs from a Responses API reply."""
        answer = str(getattr(response, "output_text", "") or "")
        sources: list[GroundedSource] = []
        position: dict[str, int] = {}

        def add_source(
            raw_url: str,
            *,
            title: str = "",
            origin: str,
            span: CitationSpan | None = None,
        ) -> None:
            try:
                identity = safe_public_url_identity(raw_url)
            except (CredentialBearingUrlError, ValueError):
                return
            canonical = identity.canonical_url
            known = position.get(canonical)
            if known is None:
                position[canonical] = len(sources)
                sources.append(
                    GroundedSource(
                        url=identity.url,
                        title=title,
                        rank=len(sources) + 1,
                        origin=origin,
                        spans=(span,) if span else (),
                    )
                )
                return
            # The source stays one; each further citation of it is another
            # place of the answer, often the one stating a figure the review
            # has to read, so its span joins the source instead of vanishing.
            source = sources[known]
            if span and span not in source.spans:
                sources[known] = replace(source, spans=(*source.spans, span))

        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "message":
                continue
            if not answer:
                answer = "".join(
                    str(getattr(part, "text", "") or "")
                    for part in getattr(item, "content", []) or []
                    if getattr(part, "type", "output_text") == "output_text"
                ).strip()
            for content in getattr(item, "content", []) or []:
                for ann in getattr(content, "annotations", []) or []:
                    if getattr(ann, "type", None) != "url_citation":
                        continue
                    url = str(getattr(ann, "url", "") or "").strip()
                    if url:
                        start = _annotation_offset(getattr(ann, "start_index", None))
                        end = _annotation_offset(getattr(ann, "end_index", None))
                        add_source(
                            url,
                            title=str(getattr(ann, "title", "") or ""),
                            origin="url_citation",
                            span=(
                                CitationSpan(start, end)
                                if start is not None and end is not None and end > start
                                else None
                            ),
                        )

        # Provider annotations are incomplete in some Foundry responses.
        # Always merge URLs present in the answer instead of treating text
        # extraction as an all-or-nothing fallback.
        if answer:
            for match in _MARKDOWN_LINK_RE.finditer(answer):
                url = match.group(2).strip()
                if url:
                    add_source(
                        url,
                        title=match.group(1),
                        origin="markdown_link",
                    )
            for url in extract_urls(answer):
                if url:
                    add_source(
                        url,
                        origin="answer_url_fallback",
                    )

        usage = getattr(response, "usage", None)
        raw = response_raw_payload(response)
        output = getattr(response, "output", None)
        return GroundedSearchResult(
            answer=answer,
            sources=sources,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=str(getattr(response, "model", "") or ""),
            **responses_usage_details(usage),
            tool_call_count=(
                sum(getattr(item, "type", None) == "web_search_call" for item in output)
                if isinstance(output, list) else None
            ),
            raw=raw if isinstance(raw, dict) else None,
        )

    def is_available(self) -> bool:
        """Report whether the provider is configured to attempt requests.

        Returns:
            bool: ``True`` when both the agent name and project endpoint
            are present, otherwise ``False``.
        """
        return bool(self._agent_name and self._project_endpoint)

    @property
    def search_model(self) -> str:
        """Foundry web-search agent identifier shown to operators.

        Format: ``"foundry-web:<agent_name>@<version_or_latest>"``.
        """
        version = self._agent_version or "latest"
        return f"foundry-web:{self._agent_name}@{version}"
