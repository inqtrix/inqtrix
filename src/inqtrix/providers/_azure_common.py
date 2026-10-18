"""Shared helpers for the Azure-flavoured providers.

The Azure variants (``AzureOpenAILLM``, ``AzureFoundryWebSearch``)
live in separate modules because their request shapes and response
schemas diverge. What they *do* share is
boilerplate around credential handling, base URL normalisation and API
error diagnostics. This module centralises those helpers so a fix in
one place propagates to every Azure provider.

Inheritance would couple the providers too tightly; free functions
keep the call-sites explicit while preventing copy-paste drift between
the providers.
"""

from __future__ import annotations

from typing import Any, Callable

from openai import APIStatusError


AZURE_OPENAI_DEFAULT_SCOPE = "https://cognitiveservices.azure.com/.default"
"""Default OAuth scope for Azure OpenAI (Cognitive Services) Entra ID auth.

Used by :func:`build_azure_openai_token_provider` when no explicit
``scope`` is supplied. This is the scope documented by Microsoft for
Azure OpenAI resources accessed via Service Principal, Managed
Identity, or other Entra ID flows.
"""


def extract_azure_api_error_details(exc: APIStatusError) -> dict[str, Any]:
    """Normalise an ``APIStatusError`` from an Azure endpoint.

    Azure Cognitive, Azure OpenAI and Azure AI Foundry all surface
    failures through ``openai.APIStatusError`` with slightly different
    body shapes. This helper returns a shallow dict with the fields
    the providers want to log or attach to an ``AzureOpenAIAPIError``
    so diagnostics stay consistent.

    Args:
        exc: The SDK exception raised by a chat-completion or
            responses request.

    Returns:
        dict[str, Any]: ``status_code``, ``request_id``,
        ``error_code`` and ``message`` — any of which may be empty
        when the backend did not supply the value.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    request_id: str | None = None
    for header_name in ("apim-request-id", "x-request-id", "request-id"):
        header_value = headers.get(header_name)
        if isinstance(header_value, str) and header_value.strip():
            request_id = header_value.strip()
            break

    error_code = ""
    message = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            raw_code = error.get("code")
            raw_message = error.get("message")
            if isinstance(raw_code, str):
                error_code = raw_code.strip()
            if isinstance(raw_message, str):
                message = raw_message.strip()
        if not error_code:
            top_code = body.get("code")
            if isinstance(top_code, str):
                error_code = top_code.strip()
        if not message:
            top_message = body.get("message")
            if isinstance(top_message, str):
                message = top_message.strip()

    return {
        "status_code": getattr(exc, "status_code", None),
        "request_id": request_id,
        "error_code": error_code,
        "message": message,
    }


def resolve_azure_credential(
    *,
    credential: Any | None,
    tenant_id: str | None,
    client_id: str | None,
    client_secret: str | None,
) -> Any:
    """Build or return an Azure credential object.

    Shared between Foundry-backed providers that authenticate via
    ``azure-identity`` rather than a raw API key. The ``azure-identity``
    package is imported lazily so users who only rely on API keys do
    not need it installed.

    Args:
        credential: A pre-built credential object. When supplied it
            wins over tenant/client credentials.
        tenant_id: Service principal tenant id.
        client_id: Service principal client id.
        client_secret: Service principal secret.

    Returns:
        Any: A credential object (``ClientSecretCredential`` if all
        three service-principal fields are supplied, otherwise
        ``DefaultAzureCredential``).
    """
    if credential is not None:
        return credential

    from azure.identity import ClientSecretCredential, DefaultAzureCredential

    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )
    return DefaultAzureCredential()


def build_azure_openai_token_provider(
    *,
    credential: Any | None = None,
    tenant_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    scope: str = AZURE_OPENAI_DEFAULT_SCOPE,
) -> Callable[[], str] | None:
    """Build an Azure OpenAI bearer-token provider from explicit inputs.

    The OpenAI SDK accepts a callable for ``api_key`` so it can refresh
    the bearer token before each request. This helper produces such a
    callable from the same Service Principal / explicit credential
    inputs that the Foundry providers already accept directly. The
    ``azure-identity`` package is imported lazily so callers that only
    use static API keys do not pay the import cost.

    Resolution order matches :func:`resolve_azure_credential`:

    1. ``credential`` (a pre-built ``TokenCredential``-like object)
    2. ``tenant_id`` + ``client_id`` + ``client_secret``
       → ``ClientSecretCredential``
    3. Returns ``None`` if none of the inputs are present, leaving the
       caller to decide whether that is an error.

    Args:
        credential: Optional pre-built Azure credential. When supplied it
            wins over the Service Principal fields.
        tenant_id: Service principal tenant id.
        client_id: Service principal client id.
        client_secret: Service principal secret.
        scope: OAuth scope for the bearer token. Defaults to the Azure
            OpenAI / Cognitive Services scope. Override this for
            non-OpenAI Azure surfaces (for example
            ``"https://ai.azure.com/.default"`` for Foundry).

    Returns:
        Callable[[], str] | None: Bearer-token provider compatible with
        the OpenAI SDK's ``azure_ad_token_provider`` or ``api_key``
        callable input, or ``None`` when no credential inputs were
        provided.
    """
    has_sp = bool(tenant_id and client_id and client_secret)
    if credential is None and not has_sp:
        return None

    from azure.identity import (
        ClientSecretCredential,
        get_bearer_token_provider,
    )

    if credential is None:
        credential = ClientSecretCredential(
            tenant_id=tenant_id or "",
            client_id=client_id or "",
            client_secret=client_secret or "",
        )
    return get_bearer_token_provider(credential, scope)


def normalize_openai_v1_base_url(endpoint_or_base_url: str) -> str:
    """Normalize an Azure OpenAI endpoint to the ``/openai/v1/`` form.

    Microsoft's current Azure OpenAI guidance recommends hitting the
    ``https://<resource>.openai.azure.com/openai/v1/`` endpoint
    directly rather than the deprecated date-based ``api_version``
    flow. Users often paste either the bare resource URL or the full
    ``/openai/v1`` form, so both are accepted. An Azure AI Project
    endpoint (``.../api``) is rejected because that path serves a
    different API surface.

    Args:
        endpoint_or_base_url: Raw endpoint string as supplied by the
            caller.

    Returns:
        str: Trailing-slash-terminated base URL suitable for the
        OpenAI SDK ``base_url`` argument.

    Raises:
        ValueError: If the input endpoint points at an Azure AI
            Project (``.../api``) URL that cannot be reused.
    """
    stripped = endpoint_or_base_url.strip().rstrip("/")
    if stripped.endswith("/api"):
        raise ValueError(
            "The provided endpoint looks like an Azure AI Project endpoint (.../api). "
            "For Azure OpenAI use the resource endpoint or the full /openai/v1/ base URL."
        )
    if stripped.endswith("/openai/v1"):
        return f"{stripped}/"
    return f"{stripped}/openai/v1/"
