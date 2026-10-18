"""Tests for shared helpers in `inqtrix.providers._azure_common`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from inqtrix.providers._azure_common import (
    AZURE_OPENAI_DEFAULT_SCOPE,
    build_azure_openai_token_provider,
)


def test_build_token_provider_returns_none_without_inputs():
    """No credential and no SP fields -> None (caller decides)."""
    assert build_azure_openai_token_provider() is None


def test_build_token_provider_uses_client_secret_credential():
    """Service Principal fields build a `ClientSecretCredential`."""
    fake_credential = MagicMock(name="ClientSecretCredential-instance")
    fake_token_provider = MagicMock(name="bearer-provider")

    with (
        patch("azure.identity.ClientSecretCredential") as mock_csc,
        patch("azure.identity.get_bearer_token_provider") as mock_get_provider,
    ):
        mock_csc.return_value = fake_credential
        mock_get_provider.return_value = fake_token_provider

        provider = build_azure_openai_token_provider(
            tenant_id="t-1",
            client_id="c-1",
            client_secret="s-1",
        )

    mock_csc.assert_called_once_with(
        tenant_id="t-1",
        client_id="c-1",
        client_secret="s-1",
    )
    mock_get_provider.assert_called_once_with(
        fake_credential, AZURE_OPENAI_DEFAULT_SCOPE
    )
    assert provider is fake_token_provider


def test_build_token_provider_passes_explicit_credential_through():
    """A pre-built credential is reused without re-instantiation."""
    pre_built = MagicMock(name="pre-built-credential")
    fake_token_provider = MagicMock(name="bearer-provider")

    with (
        patch("azure.identity.ClientSecretCredential") as mock_csc,
        patch("azure.identity.get_bearer_token_provider") as mock_get_provider,
    ):
        mock_get_provider.return_value = fake_token_provider

        provider = build_azure_openai_token_provider(credential=pre_built)

    mock_csc.assert_not_called()
    mock_get_provider.assert_called_once_with(pre_built, AZURE_OPENAI_DEFAULT_SCOPE)
    assert provider is fake_token_provider


def test_build_token_provider_respects_custom_scope():
    pre_built = MagicMock()
    fake_token_provider = MagicMock()

    with patch("azure.identity.get_bearer_token_provider") as mock_get_provider:
        mock_get_provider.return_value = fake_token_provider

        build_azure_openai_token_provider(
            credential=pre_built,
            scope="https://ai.azure.com/.default",
        )

    mock_get_provider.assert_called_once_with(
        pre_built, "https://ai.azure.com/.default"
    )
