"""Sharing/quota gate spans every multi-user mode (oidc/local/ldap).

The share-service and quota gates span every cookie-session mode, not
only oidc, so a local or ldap deployment gets the full sharing
experience. The single-operator none/apikey modes stay excluded (no
scoped identity to share with). No static-principal rescoping.
"""

from __future__ import annotations

import asyncio

import pytest

from inqtrix.auth.api_key import (
    build_auth_provider,
    build_local_provider,
)
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.settings import (
    AuthSettings,
    ServerSettings,
    Settings,
    SharingSettings,
    StorageSettings,
)

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM

def _container(auth_provider, *, sharing_enabled: bool = True):
    identity = MemoryIdentityStore()
    return build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            sharing=SharingSettings(enabled=sharing_enabled),
            storage=StorageSettings(backend="memory", database_url=""),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=auth_provider,
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
    )


def test_sharing_enabled_for_local():
    provider = build_local_provider(
        Settings(auth=AuthSettings(mode="local", session_secret="s" * 32, pat_pepper="p" * 32))
    )
    assert _container(provider).share_service is not None


def test_sharing_master_switch_hides_the_service_in_cookie_auth_mode():
    provider = build_local_provider(
        Settings(
            auth=AuthSettings(
                mode="local",
                session_secret="s" * 32,
                pat_pepper="p" * 32,
            )
        )
    )
    container = _container(provider, sharing_enabled=False)

    assert container.share_service is None


@pytest.mark.parametrize(
    "auth, server",
    [
        (AuthSettings(mode="none"), ServerSettings(public_base_url="")),
        (AuthSettings(mode="apikey"), ServerSettings(api_key="k", public_base_url="")),
    ],
)
def test_sharing_disabled_for_single_operator_modes(auth, server):
    provider = build_auth_provider(Settings(auth=auth, server=server))
    # No scoped identity -> sharing stays off (use local for single-user sharing).
    assert _container(provider).share_service is None
