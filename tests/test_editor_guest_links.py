from __future__ import annotations

import base64
import re
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.principal import Principal
from inqtrix.auth.permissions import SharePermission
from inqtrix.project.editor_guest_links import (
    EditorDocumentShareLink,
    EditorGuestAccess,
    EditorGuestLinkConflict,
    EditorGuestIdentity,
    EditorGuestLinkExpired,
    EditorGuestLinkNotFound,
    EditorGuestLinkRateLimited,
)
from inqtrix.services.editor_guest_link_service import EditorGuestLinkService
from inqtrix.server.routers.editor_guest_links import build_router
from inqtrix.settings import EditorGuestLinkSettings
from inqtrix.storage.editor_collaboration_postgres import (
    _active_guest_access,
)


OWNER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
DOCUMENT_ID = "ed_guest_contract"
COMMAND_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SECRET = "guest-link-contract-secret-32-bytes-minimum"


class FakeGuestStore:
    def __init__(self) -> None:
        self.link: EditorDocumentShareLink | None = None
        self.identity: EditorGuestIdentity | None = None
        self.last_created: EditorDocumentShareLink | None = None
        self.revoked_sessions = 0

    async def create_link(self, link: EditorDocumentShareLink):
        self.last_created = link
        if self.link is None:
            self.link = link
        return self.link

    async def access_summary(
        self,
        *,
        tenant_id,
        document_id,
        actor_user_id,
        since,
        now,
    ):
        del tenant_id, document_id, actor_user_id, since, now
        return {
            "guest_link_count": 2,
            "guest_open_count": 8,
            "guest_session_count": 5,
            "last_guest_accessed_at": 123.0,
        }

    async def resolve_link(self, *, tenant_id, token_digest, now):
        if (
            self.link is None
            or self.link.tenant_id != tenant_id
            or self.link.token_digest != token_digest
        ):
            raise EditorGuestLinkNotFound()
        return self.link, "Strategy.md"

    async def update_link(
        self,
        *,
        tenant_id,
        document_id,
        link_id,
        actor_user_id,
        permission,
        expires_at,
        password_hash,
        token_digest,
        expected_revision,
        command_id,
        command_payload_hash,
        command_kind,
        now,
    ):
        # Mirrors editor_guest_link_postgres.update_link where this contract
        # is observable: revision guard, per-field replacement, and the
        # session sweep that fires when EITHER half of the pair is replaced.
        del tenant_id, document_id, actor_user_id, command_payload_hash
        if self.link is None or self.link.id != link_id:
            raise EditorGuestLinkNotFound()
        if int(self.link.revision) != expected_revision:
            raise EditorGuestLinkConflict(
                "revision_conflict",
                current_revision=int(self.link.revision),
            )
        changes = {
            "revision": expected_revision + 1,
            "updated_at": now,
            "last_command_id": command_id,
            "last_command_kind": command_kind,
        }
        if permission is not None:
            changes["permission"] = permission
        if expires_at is not None:
            changes["expires_at"] = expires_at
        if password_hash is not None:
            changes["password_hash"] = password_hash
        if token_digest is not None:
            changes["token_digest"] = token_digest
        self.link = replace(self.link, **changes)
        if password_hash is not None or token_digest is not None:
            self.identity = None
            self.revoked_sessions += 1
        return self.link

    async def create_guest_identity(
        self,
        identity,
        *,
        verified_token_digest,
        verified_password_hash,
        stats_enabled,
        now,
    ):
        # Mirrors editor_guest_link_postgres.create_guest_identity: a session
        # opens only on a link that still carries the pair the unlock checked.
        assert self.link is not None
        if (
            self.link.token_digest != verified_token_digest
            or self.link.password_hash != verified_password_hash
        ):
            raise EditorGuestLinkExpired()
        self.identity = identity
        return _access(self.link, identity)

    async def resolve_guest_identity(
        self,
        *,
        tenant_id,
        session_token_digest,
        now,
        display_name=None,
        stats_enabled=True,
    ):
        if (
            self.link is None
            or self.identity is None
            or self.identity.session_token_digest != session_token_digest
        ):
            raise EditorGuestLinkNotFound()
        if display_name is not None:
            self.identity = replace(self.identity, display_name=display_name)
        return _access(self.link, self.identity)


class FakeLimiter:
    def __init__(self) -> None:
        self.failures: dict[str, int] = {}
        self.locked_keys: set[str] = set()

    async def locked(self, key: str) -> bool:
        return key in self.locked_keys

    async def record_failure(self, key: str) -> None:
        self.failures[key] = self.failures.get(key, 0) + 1

    async def reset(self, key: str) -> None:
        self.failures.pop(key, None)
        self.locked_keys.discard(key)


class FakeCollaboration:
    def __init__(self) -> None:
        self.access: EditorGuestAccess | None = None

    async def create_guest_session(self, *, access, **_kwargs):
        self.access = access
        return {"access": access.link.permission}


def _principal() -> Principal:
    return Principal(
        user_id=OWNER_ID,
        kind="oidc_session",
        tenant_id="default",
        role="owner",
        display_name="Owner",
        session_id="owner-session",
    )


def _service(
    *,
    store: FakeGuestStore | None = None,
    limiter: FakeLimiter | None = None,
):
    store = store or FakeGuestStore()
    limiter = limiter or FakeLimiter()
    collaboration = FakeCollaboration()
    service = EditorGuestLinkService(
        store=store,
        collaboration=collaboration,  # type: ignore[arg-type]
        settings=EditorGuestLinkSettings(
            enabled=True,
            token_hmac_secret=SECRET,
        ),
        public_base_url="https://desk.example.test",
        rate_limiter=limiter,
    )
    return service, store, limiter, collaboration


def _access(
    link: EditorDocumentShareLink,
    identity: EditorGuestIdentity,
) -> EditorGuestAccess:
    return EditorGuestAccess(
        link=link,
        identity=identity,
        document_title="Strategy.md",
        content_markdown="# Strategy",
        persisted_sequence=3,
        projection_sequence=3,
        comment_revision=2,
    )


@pytest.mark.asyncio
async def test_created_link_uses_256_bit_token_and_never_stores_plaintext() -> None:
    service, store, _limiter, _collaboration = _service()

    result = await service.create_link(
        document_id=DOCUMENT_ID,
        permission="edit",
        ttl_seconds=3_600,
        command_id=COMMAND_ID,
        principal=_principal(),
        generation=1,
    )

    assert result["url"].startswith("https://desk.example.test/s/egl1.")
    token = result["url"].rsplit("/", 1)[1]
    material = token.split(".")[2]
    decoded = base64.urlsafe_b64decode(material + "=" * (-len(material) % 4))
    assert len(decoded) == 32
    assert re.fullmatch(r"(?:[A-Z2-9]{4}-){4}[A-Z2-9]{4}", result["password"])
    assert store.last_created is not None
    assert store.last_created.id.version == 4
    assert token not in store.last_created.token_digest
    assert result["password"] not in store.last_created.password_hash
    assert store.last_created.token_digest != token


@pytest.mark.asyncio
async def test_same_command_reconstructs_same_link_and_password() -> None:
    service, _store, _limiter, _collaboration = _service()
    kwargs = {
        "document_id": DOCUMENT_ID,
        "permission": "view",
        "ttl_seconds": 3_600,
        "command_id": COMMAND_ID,
        "principal": _principal(),
        "generation": 1,
    }

    first = await service.create_link(**kwargs)
    second = await service.create_link(**kwargs)

    assert second["id"] == first["id"]
    assert second["url"] == first["url"]
    assert second["password"] == first["password"]


@pytest.mark.asyncio
async def test_unlock_records_failure_and_resets_after_valid_password() -> None:
    service, _store, limiter, _collaboration = _service()
    created = await service.create_link(
        document_id=DOCUMENT_ID,
        permission="comment",
        ttl_seconds=3_600,
        command_id=COMMAND_ID,
        principal=_principal(),
        generation=1,
    )
    token = created["url"].rsplit("/", 1)[1]

    with pytest.raises(EditorGuestLinkNotFound):
        await service.unlock(
            token=token,
            password="wrong",
            display_name="Maria",
            throttle_key="test-key",
        )
    assert limiter.failures["test-key"] == 1

    unlocked = await service.unlock(
        token=token,
        password=created["password"],
        display_name="  Maria   Example ",
        throttle_key="test-key",
    )

    assert unlocked.access.identity.display_name == "Maria Example"
    assert "test-key" not in limiter.failures
    assert unlocked.session_token.startswith("egs1.")


class _RotatedAfterResolveStore(FakeGuestStore):
    """A rotation of one credential half commits right after an unlock's read."""

    def __init__(self, replaced: str) -> None:
        super().__init__()
        self.replaced = replaced

    async def resolve_link(self, *, tenant_id, token_digest, now):
        resolved = await super().resolve_link(
            tenant_id=tenant_id, token_digest=token_digest, now=now
        )
        assert self.link is not None
        self.link = replace(self.link, **{self.replaced: "f" * 64})
        return resolved


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced", ["password_hash", "token_digest"])
async def test_unlock_refuses_a_credential_replaced_after_its_check(
    replaced: str,
) -> None:
    """The unlock hands the store the pair it checked the password against,
    not a later read, so a rotation that commits after the check opens no
    session. The attempt is not counted as a failed password: the password
    was right when it was checked."""
    store = _RotatedAfterResolveStore(replaced)
    service, _store, limiter, _collaboration = _service(store=store)
    created = await service.create_link(
        document_id=DOCUMENT_ID,
        permission="edit",
        ttl_seconds=3_600,
        command_id=COMMAND_ID,
        principal=_principal(),
        generation=1,
    )
    token = created["url"].rsplit("/", 1)[1]

    with pytest.raises(EditorGuestLinkExpired):
        await service.unlock(
            token=token,
            password=created["password"],
            display_name="Maria",
            throttle_key="test-key",
        )

    assert store.identity is None
    assert "test-key" not in limiter.failures


@pytest.mark.asyncio
async def test_locked_password_key_short_circuits_before_lookup() -> None:
    limiter = FakeLimiter()
    limiter.locked_keys.add("locked")
    service, _store, _limiter, _collaboration = _service(limiter=limiter)

    with pytest.raises(EditorGuestLinkRateLimited):
        await service.unlock(
            token="egl1.invalid.invalid.invalid",
            password="wrong",
            display_name=None,
            throttle_key="locked",
        )


@pytest.mark.asyncio
async def test_access_summary_respects_statistics_privacy_switch() -> None:
    service, _store, _limiter, _collaboration = _service()
    visible = await service.access_summary(
        document_id=DOCUMENT_ID,
        principal=_principal(),
        window_seconds=7 * 24 * 60 * 60,
    )
    assert visible == {
        "guest_link_count": 2,
        "guest_open_count": 8,
        "guest_session_count": 5,
        "last_guest_accessed_at": 123.0,
    }

    store = FakeGuestStore()
    hidden = EditorGuestLinkService(
        store=store,
        collaboration=FakeCollaboration(),  # type: ignore[arg-type]
        settings=EditorGuestLinkSettings(
            enabled=True,
            stats_enabled=False,
            token_hmac_secret=SECRET,
        ),
        public_base_url="https://desk.example.test",
        rate_limiter=FakeLimiter(),
    )
    private = await hidden.access_summary(
        document_id=DOCUMENT_ID,
        principal=_principal(),
        window_seconds=30 * 24 * 60 * 60,
    )
    assert private == {
        "guest_link_count": 2,
        "guest_open_count": 0,
        "guest_session_count": 0,
        "last_guest_accessed_at": None,
    }


@pytest.mark.asyncio
async def test_mutating_guest_requires_display_name_but_view_guest_does_not() -> None:
    for permission, requires_name in (
        ("view", False),
        ("comment", True),
        ("suggest", True),
        ("edit", True),
    ):
        command = uuid.uuid4()
        service, _store, _limiter, collaboration = _service()
        created = await service.create_link(
            document_id=DOCUMENT_ID,
            permission=permission,  # type: ignore[arg-type]
            ttl_seconds=3_600,
            command_id=command,
            principal=_principal(),
            generation=1,
        )
        unlocked = await service.unlock(
            token=created["url"].rsplit("/", 1)[1],
            password=created["password"],
            display_name=None,
            throttle_key=f"key-{permission}",
        )
        if requires_name:
            with pytest.raises(ValueError, match="display_name_required"):
                await service.create_collaboration_session(
                    session_token=unlocked.session_token,
                    protocol_version=1,
                    schema_version=1,
                    current_lease_token=None,
                    rotation_command_id=None,
                    display_name=None,
                )
        else:
            result = await service.create_collaboration_session(
                session_token=unlocked.session_token,
                protocol_version=1,
                schema_version=1,
                current_lease_token=None,
                rotation_command_id=None,
                display_name=None,
            )
            assert result["access"] == "view"
            assert collaboration.access is not None


def test_guest_link_settings_reject_invalid_ttl_and_short_secret() -> None:
    with pytest.raises(ValueError, match="32 characters"):
        EditorGuestLinkSettings(enabled=True, token_hmac_secret="too-short")
    with pytest.raises(ValueError, match="less than or equal"):
        EditorGuestLinkSettings(
            default_ttl_seconds=7_200,
            max_ttl_seconds=3_600,
        )


@pytest.mark.asyncio
async def test_guest_link_kill_switch_invalidates_existing_access_before_db() -> None:
    """An issued guest lease cannot outlive the deployment module switch."""
    result = await _active_guest_access(
        None,  # type: ignore[arg-type] -- the disabled gate must not touch DB
        tenant_id="default",
        guest_identity_id=uuid.uuid4(),
        guest_link_id=uuid.uuid4(),
        document_id=DOCUMENT_ID,
        generation=1,
        minimum=SharePermission.VIEW,
        now=1.0,
        guest_links_enabled=False,
    )

    assert result is None


def test_public_unlock_uses_auth_proxy_depth_and_sets_guest_cookies() -> None:
    """The public router reads proxy policy from AuthSettings, not ServerSettings."""

    class RouterGuestService:
        source_ip: str | None = None

        def throttle_key(self, *, token: str, source_ip: str) -> str:
            assert token == "public-token"
            self.source_ip = source_ip
            return "throttle-key"

        async def unlock(self, **_kwargs):
            return SimpleNamespace(
                access=SimpleNamespace(
                    identity=SimpleNamespace(
                        created_at=1.0,
                        expires_at=3_601.0,
                    )
                ),
                session_token="guest-session-token",
            )

        @staticmethod
        def guest_payload(_access):
            return {"permission": "view"}

    service = RouterGuestService()
    container = SimpleNamespace(
        collaboration_service=object(),
        editor_guest_link_service=service,
        principal_dependency=lambda: _principal(),
        settings=SimpleNamespace(
            auth=SimpleNamespace(trusted_proxy_hops=0),
            editor_guest_links=SimpleNamespace(allow_insecure_http=False),
            server=SimpleNamespace(public_base_url="https://desk.example.test"),
        ),
        share_service=object(),
    )
    app = FastAPI()
    app.include_router(build_router(container))

    with TestClient(
        app,
        base_url="https://desk.example.test",
    ) as client:
        response = client.post(
            "/v1/editor/share-links/public-token:unlock",
            headers={"Origin": "https://desk.example.test"},
            json={"password": "valid"},
        )

    assert response.status_code == 200
    assert service.source_ip
    cookies = response.headers.get_list("set-cookie")
    assert any("inqtrix_editor_guest=" in value for value in cookies)
    assert all("Secure" in value for value in cookies)


def test_public_unlock_drops_secure_flag_with_insecure_http_opt_in() -> None:
    """The explicit dev opt-in serves guest cookies a browser accepts
    over plain http — hardcoded Secure would let the unlock succeed and
    every following guest request fail cookie-less."""

    class RouterGuestService:
        def throttle_key(self, *, token: str, source_ip: str) -> str:
            return "throttle-key"

        async def unlock(self, **_kwargs):
            return SimpleNamespace(
                access=SimpleNamespace(
                    identity=SimpleNamespace(
                        created_at=1.0,
                        expires_at=3_601.0,
                    )
                ),
                session_token="guest-session-token",
            )

        @staticmethod
        def guest_payload(_access):
            return {"permission": "view"}

    container = SimpleNamespace(
        collaboration_service=object(),
        editor_guest_link_service=RouterGuestService(),
        principal_dependency=lambda: _principal(),
        settings=SimpleNamespace(
            auth=SimpleNamespace(trusted_proxy_hops=0),
            editor_guest_links=SimpleNamespace(allow_insecure_http=True),
            server=SimpleNamespace(public_base_url="http://127.0.0.1:8080"),
        ),
        share_service=object(),
    )
    app = FastAPI()
    app.include_router(build_router(container))

    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        response = client.post(
            "/v1/editor/share-links/public-token:unlock",
            headers={"Origin": "http://127.0.0.1:8080"},
            json={"password": "valid"},
        )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any("inqtrix_editor_guest=" in value for value in cookies)
    assert len(cookies) == 2
    assert all("Secure" not in value for value in cookies)
    # The safety attributes stay untouched by the opt-in.
    assert any("HttpOnly" in value for value in cookies)
    assert all("SameSite=lax" in value for value in cookies)


def test_guest_comment_router_uses_editor_collaboration_service() -> None:
    """Guest comment routes use the container's canonical editor service."""

    access = object()

    class RouterGuestService:
        async def session(self, token: str):
            assert token == "guest-session"
            return access

    class RouterEditorCollaboration:
        calls = 0

        async def list_guest_comments(self, **kwargs):
            self.calls += 1
            assert kwargs["access"] is access
            return {
                "threads": [],
                "revision": 0,
                "last_read_revision": 0,
                "participants": {},
            }

    editor_collaboration = RouterEditorCollaboration()
    container = SimpleNamespace(
        collaboration_service=object(),
        editor_collaboration_service=editor_collaboration,
        editor_guest_link_service=RouterGuestService(),
        principal_dependency=lambda: _principal(),
        settings=SimpleNamespace(
            auth=SimpleNamespace(trusted_proxy_hops=0),
            server=SimpleNamespace(public_base_url="https://desk.example.test"),
        ),
        share_service=object(),
    )
    app = FastAPI()
    app.include_router(build_router(container))

    with TestClient(
        app,
        base_url="https://desk.example.test",
    ) as client:
        response = client.get(
            "/v1/editor/guest/collaboration/comments",
            headers={
                "Cookie": "inqtrix_editor_guest=guest-session",
            },
        )

    assert response.status_code == 200
    assert response.json()["revision"] == 0
    assert editor_collaboration.calls == 1


async def _linked_service():
    """A created link plus an opened guest session, ready to rotate."""
    service, store, limiter, collaboration = _service()
    created = await service.create_link(
        document_id=DOCUMENT_ID,
        permission="edit",
        ttl_seconds=3_600,
        command_id=COMMAND_ID,
        principal=_principal(),
        generation=1,
    )
    token = created["url"].rsplit("/", 1)[1]
    await service.unlock(
        token=token,
        password=created["password"],
        display_name="Guest",
        throttle_key=service.throttle_key(token=token, source_ip="203.0.113.9"),
    )
    assert store.identity is not None
    return service, store, limiter, collaboration, created, token


@pytest.mark.asyncio
async def test_rotating_the_token_kills_the_old_link_and_keeps_the_password(
) -> None:
    service, store, _limiter, _collaboration, created, old_token = (
        await _linked_service()
    )
    password_hash_before = store.link.password_hash

    rotated = await service.rotate_token(
        document_id=DOCUMENT_ID,
        link_id=uuid.UUID(created["id"]),
        expected_revision=created["revision"],
        command_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        principal=_principal(),
    )

    new_token = rotated["url"].rsplit("/", 1)[1]
    assert new_token != old_token
    # The old URL is gone, not merely deprioritised.
    with pytest.raises(EditorGuestLinkNotFound):
        await service.describe_token(old_token)
    described = await service.describe_token(new_token)
    assert described["label"] == created["label"]
    # The other half of the pair is untouched: the recipient keeps it.
    assert store.link.password_hash == password_hash_before
    assert "password" not in rotated


@pytest.mark.asyncio
async def test_rotating_the_token_never_returns_or_stores_the_plaintext(
) -> None:
    service, store, _limiter, _collaboration, created, _old = (
        await _linked_service()
    )

    rotated = await service.rotate_token(
        document_id=DOCUMENT_ID,
        link_id=uuid.UUID(created["id"]),
        expected_revision=created["revision"],
        command_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        principal=_principal(),
    )

    token = rotated["url"].rsplit("/", 1)[1]
    material = token.split(".")[2]
    decoded = base64.urlsafe_b64decode(material + "=" * (-len(material) % 4))
    assert len(decoded) == 32
    assert token not in store.link.token_digest
    assert store.link.token_digest != token


@pytest.mark.asyncio
async def test_rotating_the_token_ends_the_session_opened_with_the_old_one(
) -> None:
    service, store, _limiter, _collaboration, created, _old = (
        await _linked_service()
    )
    assert store.revoked_sessions == 0

    await service.rotate_token(
        document_id=DOCUMENT_ID,
        link_id=uuid.UUID(created["id"]),
        expected_revision=created["revision"],
        command_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        principal=_principal(),
    )

    assert store.revoked_sessions == 1
    assert store.identity is None


@pytest.mark.asyncio
async def test_rotating_the_password_leaves_the_link_reachable() -> None:
    service, store, _limiter, _collaboration, created, token = (
        await _linked_service()
    )
    digest_before = store.link.token_digest

    rotated = await service.rotate_password(
        document_id=DOCUMENT_ID,
        link_id=uuid.UUID(created["id"]),
        expected_revision=created["revision"],
        command_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
        principal=_principal(),
    )

    assert rotated["password"] != created["password"]
    assert store.link.token_digest == digest_before
    # Same URL, new password: describing the token still works.
    assert (await service.describe_token(token))["label"] == created["label"]
    # And the session opened with the old password is gone.
    assert store.revoked_sessions == 1


@pytest.mark.asyncio
async def test_token_rotation_refuses_a_stale_revision() -> None:
    service, _store, _limiter, _collaboration, created, _old = (
        await _linked_service()
    )

    with pytest.raises(EditorGuestLinkConflict):
        await service.rotate_token(
            document_id=DOCUMENT_ID,
            link_id=uuid.UUID(created["id"]),
            expected_revision=created["revision"] + 7,
            command_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
            principal=_principal(),
        )


def test_production_store_sweeps_sessions_for_either_replaced_secret() -> None:
    """The fake above mirrors a rule that lives in the Postgres store.

    Bound stated: this reads the production SOURCE, because the store has no
    test seam and the only real exercise of it is the live two-user run. It
    catches the mirror drifting apart from the original — for example a
    later edit that narrows the sweep back to the password — not every way
    the sweep could break.
    """
    source = (
        Path("src/inqtrix/storage/editor_guest_link_postgres.py")
        .read_text(encoding="utf-8")
    )
    assert 'values["token_digest"] = token_digest' in source
    assert (
        "if password_hash is not None or token_digest is not None:" in source
    )


def test_every_command_kind_the_service_writes_is_admitted_by_the_table() -> None:
    """The database, not the fake store, is what refuses an unknown kind.

    Found the hard way: `rotate_token` passed every unit test and then hit
    a CHECK violation on the first real call, because the fake store has no
    constraints. This closes the gap for the whole family instead of
    the one case — a future command kind fails here, not in production.
    """
    service_source = (
        Path("src/inqtrix/services/editor_guest_link_service.py")
        .read_text(encoding="utf-8")
    )
    written = set(re.findall(r'command_kind="([a-z_]+)"', service_source))
    written |= set(
        re.findall(r'last_command_kind="([a-z_]+)"', service_source)
    )
    assert written, "probe read nothing — the literal shape changed"

    orm_source = (
        Path("src/inqtrix/storage/editor_guest_link_orm.py")
        .read_text(encoding="utf-8")
    )
    match = re.search(
        r"last_command_kind IN \"\s*\"\(([^)]*)\)", orm_source
    )
    assert match is not None, "probe did not find the check constraint"
    admitted = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert written <= admitted, f"not admitted by the table: {written - admitted}"
