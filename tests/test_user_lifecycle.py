"""Canonical-user lifecycle commands and their atomic memory semantics."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid
from dataclasses import replace

import pytest

from inqtrix.auth.credentials import LocalCredential, MemoryCredentialStore
from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.invitations import (
    CONFIRMED_EMAIL_REQUIRED,
    Invitation,
    MemoryInvitationStore,
    RegistrationDenied,
)
from inqtrix.auth.lifecycle import (
    AdminActor,
    AdminAuthorizationError,
    ExternalProfile,
    LifecycleActor,
    LoginCommand,
    MemoryUserLifecycleTransaction,
    ProvisioningActor,
    UserDisabledError,
    UserLifecycleService,
    UserLifecycleStatus,
    UserNotProvisionedError,
)
from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
from inqtrix.auth.pat import MemoryPatStore, PatService
from inqtrix.auth.permissions import WorkspaceRole
from inqtrix.auth.sessions import AuthSession, MemorySessionStore
from inqtrix.storage.invitations_postgres import accept_open_invitations


def _session(
    user_id: uuid.UUID,
    *,
    subject: str,
    issuer: str = "https://idp.example",
) -> AuthSession:
    now = time.time()
    return AuthSession(
        id=f"session-{uuid.uuid4()}",
        user_id=user_id,
        issuer=issuer,
        subject=subject,
        email=f"{subject}@example.com",
        display_name=subject,
        groups=(),
        csrf_random=uuid.uuid4().hex,
        created_at=now,
        expires_at=now + 3600,
    )


def _memory_lifecycle(
    *,
    users: MemoryUserDirectory,
    sessions: MemorySessionStore,
    invitations: MemoryInvitationStore | None = None,
    credentials: MemoryCredentialStore | None = None,
    pat_store: MemoryPatStore | None = None,
) -> UserLifecycleService:
    transaction = MemoryUserLifecycleTransaction(
        users=users,
        sessions=sessions,
        invitations=invitations,
        credentials=credentials,
        pat_store=pat_store,
    )
    return UserLifecycleService(
        users=users,
        sessions=sessions,
        invitations=invitations,
        credentials=credentials,
        transaction=transaction,
    )


def test_split_store_lifecycle_is_rejected_at_construction():
    """Scoped lifecycle composition cannot silently fall back to split writes."""
    with pytest.raises(RuntimeError, match="atomic transaction"):
        UserLifecycleService(
            users=MemoryUserDirectory(), sessions=MemorySessionStore()
        )


def test_memory_lifecycle_reports_atomic_effects_only_after_sink_binding():
    """Memory composition must fail closed until invalidations are deliverable."""
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction = MemoryUserLifecycleTransaction(users=users, sessions=sessions)
    lifecycle = UserLifecycleService(
        users=users,
        sessions=sessions,
        transaction=transaction,
    )

    assert not lifecycle.atomic_effects

    transaction.bind_user_event_sink(lambda **_kwargs: None)

    assert lifecycle.atomic_effects


@pytest.mark.asyncio
async def test_memory_lifecycle_rolls_back_all_stores_when_effect_delivery_fails():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction = MemoryUserLifecycleTransaction(users=users, sessions=sessions)

    def fail_delivery(**_kwargs):
        raise RuntimeError("event sink unavailable")

    transaction.bind_user_event_sink(fail_delivery)
    lifecycle = UserLifecycleService(
        users=users,
        sessions=sessions,
        transaction=transaction,
    )
    proposed = _session(uuid.uuid4(), subject="rollback")

    with pytest.raises(RuntimeError, match="event sink unavailable"):
        await lifecycle.provision_login(
            LoginCommand(
                tenant_id="default",
                issuer=proposed.issuer,
                subject=proposed.subject,
                email=proposed.email or "",
                email_verified=True,
                display_name=proposed.display_name,
                session=proposed,
            )
        )

    assert await users.find_user(
        tenant_id="default", issuer=proposed.issuer, subject=proposed.subject
    ) is None
    assert await sessions.get(proposed.id) is None
    assert transaction.audit_entries == []
    assert transaction.invalidations == []


@pytest.mark.asyncio
async def test_login_reuses_existing_canonical_id_for_the_session():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(users=users, sessions=sessions)
    existing = await users.record_login(
        tenant_id="default",
        issuer="https://idp.example",
        subject="alice",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice",
    )
    proposed = _session(uuid.uuid4(), subject="alice")

    resolved = await lifecycle.provision_login(
        LoginCommand(
            tenant_id="default",
            issuer=proposed.issuer,
            subject=proposed.subject,
            email=proposed.email or "",
            email_verified=True,
            display_name=proposed.display_name,
            session=proposed,
        )
    )

    assert resolved.user_id == existing.user_id
    stored_session = await sessions.get(proposed.id)
    assert stored_session is not None
    assert stored_session.user_id == existing.user_id


@pytest.mark.asyncio
async def test_invite_required_login_consumes_membership_with_user_and_session():
    identity = MemoryIdentityStore()
    identity.add_workspace("ws-team")
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    invitations = MemoryInvitationStore(identity)
    lifecycle = _memory_lifecycle(
        users=users, sessions=sessions, invitations=invitations
    )
    owner_id = uuid.uuid4()
    await invitations.create(
        tenant_id="default",
        workspace_id="ws-team",
        email="alice@example.com",
        role=WorkspaceRole.EDITOR,
        invited_by_user_id=owner_id,
        expires_at=time.time() + 3600,
    )
    proposed = _session(uuid.uuid4(), subject="alice")

    user = await lifecycle.provision_login(
        LoginCommand(
            tenant_id="default",
            issuer=proposed.issuer,
            subject=proposed.subject,
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
            session=proposed,
            invitation_required=True,
        )
    )

    assert await identity.role_in_workspace(
        tenant_id="default", user_id=user.user_id, workspace_id="ws-team"
    ) is WorkspaceRole.EDITOR
    assert await sessions.get(proposed.id) is not None


@pytest.mark.asyncio
async def test_invite_required_denial_leaves_no_user_or_session():
    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(
        users=users,
        sessions=sessions,
        invitations=MemoryInvitationStore(identity),
    )
    proposed = _session(uuid.uuid4(), subject="stranger")

    with pytest.raises(RegistrationDenied):
        await lifecycle.provision_login(
            LoginCommand(
                tenant_id="default",
                issuer=proposed.issuer,
                subject=proposed.subject,
                email="stranger@example.com",
                email_verified=True,
                display_name="Stranger",
                session=proposed,
                invitation_required=True,
            )
        )

    assert await users.find_user(
        tenant_id="default", issuer=proposed.issuer, subject=proposed.subject
    ) is None
    assert await sessions.get(proposed.id) is None


@pytest.mark.asyncio
async def test_invite_required_rechecks_status_before_committing_login():
    class RevokedBetweenChecks(MemoryInvitationStore):
        async def has_open_for_email(
            self, *, tenant_id: str, email: str, now: float
        ) -> bool:
            del tenant_id, email, now
            return True

        async def accept_open_for_email(
            self,
            *,
            tenant_id: str,
            email: str,
            user_id: uuid.UUID,
            now: float,
        ) -> tuple[Invitation, ...]:
            del tenant_id, email, user_id, now
            return ()

    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    invitations = RevokedBetweenChecks(identity)
    lifecycle = _memory_lifecycle(
        users=users, sessions=sessions, invitations=invitations
    )
    proposed = _session(uuid.uuid4(), subject="revoked")

    with pytest.raises(RegistrationDenied):
        await lifecycle.provision_login(
            LoginCommand(
                tenant_id="default",
                issuer=proposed.issuer,
                subject=proposed.subject,
                email=proposed.email or "",
                email_verified=True,
                display_name=proposed.display_name,
                session=proposed,
                invitation_required=True,
            )
        )

    assert await users.find_user(
        tenant_id="default", issuer=proposed.issuer, subject=proposed.subject
    ) is None
    assert await sessions.get(proposed.id) is None


@pytest.mark.asyncio
async def test_memory_invitation_acceptance_rolls_back_when_membership_fails():
    identity = MemoryIdentityStore()
    invitations = MemoryInvitationStore(identity)
    invitation = await invitations.create(
        tenant_id="default",
        workspace_id="missing-workspace",
        email="alice@example.com",
        role=WorkspaceRole.EDITOR,
        invited_by_user_id=uuid.uuid4(),
        expires_at=time.time() + 3600,
    )

    with pytest.raises(KeyError, match="unknown workspace"):
        await invitations.accept_open_for_email(
            tenant_id="default",
            email="alice@example.com",
            user_id=uuid.uuid4(),
            now=time.time(),
        )

    (stored,) = await invitations.list_for_workspace(
        tenant_id="default", workspace_id="missing-workspace"
    )
    assert stored.id == invitation.id
    assert stored.accepted_at is None


@pytest.mark.asyncio
async def test_disable_command_revokes_every_live_credential_surface():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    credentials = MemoryCredentialStore()
    pat_store = MemoryPatStore()
    pat_service = PatService(
        store=pat_store,
        pepper="p" * 32,
        max_per_user=10,
        default_ttl_days=0,
    )
    lifecycle = _memory_lifecycle(
        users=users,
        sessions=sessions,
        credentials=credentials,
        pat_store=pat_store,
    )
    admin_id = uuid.uuid4()
    admin_credential = LocalCredential(
        user_id=admin_id,
        subject="local-admin",
        email="admin@example.com",
        password_hash="hash",
        display_name="Admin",
        created_at=time.time(),
    )
    assert await lifecycle.create_local_account(
        tenant_id="default",
        credential=admin_credential,
        role="admin",
        first_only=True,
    ) is not None
    user_id = uuid.uuid4()
    credential = LocalCredential(
        user_id=user_id,
        subject="local-alice",
        email="alice@example.com",
        password_hash="hash",
        display_name="Alice",
        created_at=time.time(),
    )
    browser_session = _session(user_id, subject=credential.subject, issuer="local")
    user = await lifecycle.create_local_account(
        tenant_id="default",
        credential=credential,
        role="user",
        session=browser_session,
        actor_user_id=admin_id,
    )
    assert user is not None
    await pat_service.create_token(
        tenant_id="default", owner_user_id=user_id, name="automation"
    )

    outcome = await lifecycle.set_disabled(
        tenant_id="default",
        user_id=user_id,
        disabled_at=time.time(),
        actor=AdminActor(admin_id),
    )

    assert outcome.value == "updated"
    mirror = await users.find_by_user_id(tenant_id="default", user_id=user_id)
    stored_credential = await credentials.get_by_user_id(
        tenant_id="default", user_id=user_id
    )
    assert mirror is not None and mirror.disabled_at is not None
    assert stored_credential is not None and stored_credential.disabled_at is not None
    assert await sessions.get(browser_session.id) is None
    assert await pat_store.list_for_owner(
        tenant_id="default", owner_user_id=user_id
    ) == ()


@pytest.mark.asyncio
async def test_concurrent_first_logins_create_exactly_one_active_admin():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(users=users, sessions=sessions)

    async def login(subject: str):
        proposed = _session(uuid.uuid4(), subject=subject)
        return await lifecycle.provision_login(
            LoginCommand(
                tenant_id="default",
                issuer=proposed.issuer,
                subject=subject,
                email=proposed.email or "",
                email_verified=True,
                display_name=subject,
                session=proposed,
                first_login_owner=True,
            )
        )

    await asyncio.gather(login("alice"), login("bob"))

    rows = await users.list_users(tenant_id="default")
    assert sum(row.instance_role == "admin" for row in rows) == 1


@pytest.mark.asyncio
async def test_memory_lifecycle_invalidates_peer_instance_admins():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction = MemoryUserLifecycleTransaction(users=users, sessions=sessions)
    lifecycle = UserLifecycleService(
        users=users, sessions=sessions, transaction=transaction
    )
    admin_a = await users.record_login(
        tenant_id="default",
        issuer="test",
        subject="admin-a",
        email="a@example.com",
        email_verified=True,
        display_name="A",
    )
    admin_b = await users.record_login(
        tenant_id="default",
        issuer="test",
        subject="admin-b",
        email="b@example.com",
        email_verified=True,
        display_name="B",
    )
    target = await users.record_login(
        tenant_id="default",
        issuer="test",
        subject="target",
        email="target@example.com",
        email_verified=True,
        display_name="Target",
    )
    await users.set_instance_role(
        tenant_id="default", user_id=admin_a.user_id, role="admin"
    )
    await users.set_instance_role(
        tenant_id="default", user_id=admin_b.user_id, role="admin"
    )

    assert (
        await lifecycle.set_role(
            tenant_id="default",
            user_id=target.user_id,
            role="admin",
            actor_user_id=admin_a.user_id,
        )
    ).value == "updated"

    targets = {item.target_user_id for item in transaction.invalidations}
    assert {admin_a.user_id, admin_b.user_id, target.user_id} <= targets


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["demote", "disable"])
async def test_admin_command_revalidates_actor_inside_memory_authority_boundary(
    revocation: str,
) -> None:
    """A command admitted before actor revocation cannot mutate afterwards."""
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction = MemoryUserLifecycleTransaction(users=users, sessions=sessions)
    lifecycle = UserLifecycleService(
        users=users, sessions=sessions, transaction=transaction
    )
    coordinator = MemoryAuthorityCoordinator()
    coordinator.bind_users(users)
    coordinator.bind_identity(MemoryIdentityStore())
    lifecycle.bind_authority_coordinator(coordinator)

    admin = await users.record_login(
        tenant_id="default",
        issuer="test",
        subject="admin",
        email="admin@example.com",
        email_verified=True,
        display_name="Admin",
    )
    peer_admin = await users.record_login(
        tenant_id="default",
        issuer="test",
        subject="peer-admin",
        email="peer@example.com",
        email_verified=True,
        display_name="Peer",
    )
    target = await users.record_login(
        tenant_id="default",
        issuer="test",
        subject="target",
        email="target@example.com",
        email_verified=True,
        display_name="Target",
    )
    for user_id in (admin.user_id, peer_admin.user_id):
        await users.set_instance_role(
            tenant_id="default", user_id=user_id, role="admin"
        )

    started = threading.Event()
    errors: queue.Queue[BaseException] = queue.Queue()

    def run_admitted_command() -> None:
        started.set()
        try:
            asyncio.run(
                lifecycle.set_role(
                    tenant_id="default",
                    user_id=target.user_id,
                    role="admin",
                    actor_user_id=admin.user_id,
                )
            )
        except BaseException as exc:
            errors.put(exc)

    with coordinator.lock:
        worker = threading.Thread(target=run_admitted_command)
        worker.start()
        assert started.wait(timeout=5)
        if revocation == "demote":
            await users.set_instance_role(
                tenant_id="default", user_id=admin.user_id, role="user"
            )
        else:
            await users.set_disabled(
                tenant_id="default",
                user_id=admin.user_id,
                disabled_at=time.time(),
            )

    await asyncio.to_thread(worker.join, 5)

    assert not worker.is_alive()
    assert isinstance(errors.get_nowait(), AdminAuthorizationError)
    unchanged = await users.find_by_user_id(
        tenant_id="default", user_id=target.user_id
    )
    assert unchanged is not None and unchanged.instance_role == "user"


@pytest.mark.asyncio
async def test_invitation_acceptance_locks_workspace_before_invitation_update():
    workspace_id = uuid.uuid4()

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return iter(self._rows)

        def all(self):
            return list(self._rows)

    class RecordingSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            if len(self.statements) <= 2:
                return Result((workspace_id,))
            return Result(())

    session = RecordingSession()

    assert await accept_open_invitations(
        session,
        tenant_id="default",
        email="invitee@example.com",
        user_id=uuid.uuid4(),
        now=time.time(),
    ) == ()

    candidate_read, workspace_lock, invitation_update = session.statements
    assert candidate_read.get_final_froms()[0].name == "invitations"
    assert workspace_lock.get_final_froms()[0].name == "workspaces"
    assert workspace_lock._for_update_arg is not None
    assert invitation_update.table.name == "invitations"


# A login carries profile values only when its assertion does. The rules
# below hold for every cookie mode and in both backends; the Postgres twins
# live in tests/storage/test_user_lifecycle_postgres.py.


def _login(
    session: AuthSession,
    *,
    email: str = "",
    email_verified: bool = False,
    display_name: str | None = None,
    **flags: bool,
) -> LoginCommand:
    return LoginCommand(
        tenant_id="default",
        issuer=session.issuer,
        subject=session.subject,
        email=email,
        email_verified=email_verified,
        display_name=display_name,
        session=replace(session, email=email or None, display_name=display_name),
        **flags,
    )


def _audited_lifecycle(
    *,
    users: MemoryUserDirectory,
    sessions: MemorySessionStore,
    invitations: MemoryInvitationStore | None = None,
) -> tuple[MemoryUserLifecycleTransaction, UserLifecycleService]:
    transaction = MemoryUserLifecycleTransaction(
        users=users, sessions=sessions, invitations=invitations
    )
    return transaction, UserLifecycleService(
        users=users,
        sessions=sessions,
        invitations=invitations,
        transaction=transaction,
    )


def _role_updates(
    transaction: MemoryUserLifecycleTransaction, user_id: uuid.UUID
) -> list[dict[str, str]]:
    return [
        entry.detail
        for entry in transaction.audit_entries
        if entry.action == "user.role_updated"
        and entry.resource_id == str(user_id)
    ]


@pytest.mark.asyncio
async def test_login_without_email_or_name_keeps_the_stored_profile():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(users=users, sessions=sessions)
    await users.record_login(
        tenant_id="default",
        issuer="https://idp.example",
        subject="alice",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice Example",
    )
    proposed = _session(uuid.uuid4(), subject="alice")

    user = await lifecycle.provision_login(_login(proposed))

    assert (user.email, user.email_verified, user.display_name) == (
        "alice@example.com",
        True,
        "Alice Example",
    )
    stored = await sessions.get(proposed.id)
    assert stored is not None
    assert (stored.email, stored.display_name) == (
        "alice@example.com",
        "Alice Example",
    )


@pytest.mark.asyncio
async def test_login_with_an_email_but_no_name_keeps_the_stored_name():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(users=users, sessions=sessions)
    await users.record_login(
        tenant_id="default",
        issuer="https://idp.example",
        subject="alice",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice Example",
    )
    proposed = _session(uuid.uuid4(), subject="alice")

    user = await lifecycle.provision_login(
        _login(proposed, email="alice@new.example", email_verified=False)
    )

    assert (user.email, user.email_verified, user.display_name) == (
        "alice@new.example",
        False,
        "Alice Example",
    )


@pytest.mark.asyncio
async def test_login_values_overwrite_the_stored_profile():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(users=users, sessions=sessions)
    await users.record_login(
        tenant_id="default",
        issuer="https://idp.example",
        subject="alice",
        email="alice@example.com",
        email_verified=False,
        display_name="Alice Example",
    )
    proposed = _session(uuid.uuid4(), subject="alice")

    user = await lifecycle.provision_login(
        _login(
            proposed,
            email="alice@new.example",
            email_verified=True,
            display_name="Alice New",
        )
    )

    assert (user.email, user.email_verified, user.display_name) == (
        "alice@new.example",
        True,
        "Alice New",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "expected"),
    [("bob@example.com", "bob@example.com"), ("", "bob")],
    ids=["email", "subject"],
)
async def test_a_missing_name_falls_back_to_the_email_then_the_subject(
    email: str, expected: str
):
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(users=users, sessions=sessions)
    proposed = _session(uuid.uuid4(), subject="bob")

    user = await lifecycle.provision_login(
        _login(proposed, email=email, email_verified=bool(email))
    )

    assert user.display_name == expected
    stored = await sessions.get(proposed.id)
    assert stored is not None and stored.display_name == expected


@pytest.mark.asyncio
async def test_the_name_fallback_fills_only_an_empty_stored_name():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(users=users, sessions=sessions)
    await users.record_login(
        tenant_id="default",
        issuer="https://idp.example",
        subject="carol",
        email="carol@example.com",
        email_verified=True,
        display_name=None,
    )
    proposed = _session(uuid.uuid4(), subject="carol")

    user = await lifecycle.provision_login(_login(proposed))

    assert user.display_name == "carol@example.com"


@pytest.mark.asyncio
async def test_a_role_granted_by_a_login_claim_is_audited_once():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction, lifecycle = _audited_lifecycle(users=users, sessions=sessions)

    first = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="dana"), is_admin=True)
    )
    await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="dana"), is_admin=True)
    )

    assert first.instance_role == "admin"
    assert _role_updates(transaction, first.user_id) == [
        {"instance_role": "admin", "source": "login_claim"}
    ]


@pytest.mark.asyncio
async def test_a_first_owner_grant_is_audited_with_its_source():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction, lifecycle = _audited_lifecycle(users=users, sessions=sessions)

    owner = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="erin"), first_login_owner=True)
    )
    second = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="finn"), first_login_owner=True)
    )

    assert owner.instance_role == "admin"
    assert _role_updates(transaction, owner.user_id) == [
        {"instance_role": "admin", "source": "first_owner"}
    ]
    assert second.instance_role == "user"
    assert _role_updates(transaction, second.user_id) == []


@pytest.mark.asyncio
async def test_a_login_regrants_admin_after_a_demotion_and_audits_it():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction, lifecycle = _audited_lifecycle(users=users, sessions=sessions)
    dana = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="dana"), is_admin=True)
    )
    erin = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="erin"), is_admin=True)
    )
    await lifecycle.set_role(
        tenant_id="default",
        user_id=dana.user_id,
        role="user",
        actor_user_id=erin.user_id,
    )

    again = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="dana"), is_admin=True)
    )

    assert again.instance_role == "admin"
    assert [detail.get("source") for detail in _role_updates(
        transaction, dana.user_id
    )] == ["login_claim", None, "login_claim"]


@pytest.mark.asyncio
async def test_a_login_without_a_role_change_writes_no_role_audit():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction, lifecycle = _audited_lifecycle(users=users, sessions=sessions)

    user = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="gus"))
    )

    assert user.instance_role == "user"
    assert _role_updates(transaction, user.user_id) == []


async def _invited(identity: MemoryIdentityStore) -> MemoryInvitationStore:
    identity.add_workspace("ws-team")
    invitations = MemoryInvitationStore(identity)
    await invitations.create(
        tenant_id="default",
        workspace_id="ws-team",
        email="alice@example.com",
        role=WorkspaceRole.EDITOR,
        invited_by_user_id=uuid.uuid4(),
        expires_at=time.time() + 3600,
    )
    return invitations


@pytest.mark.asyncio
async def test_an_unverified_email_never_accepts_an_invitation():
    identity = MemoryIdentityStore()
    invitations = await _invited(identity)
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(
        users=users, sessions=sessions, invitations=invitations
    )

    user = await lifecycle.provision_login(
        _login(
            _session(uuid.uuid4(), subject="alice"),
            email="alice@example.com",
            email_verified=False,
        )
    )

    assert await identity.role_in_workspace(
        tenant_id="default", user_id=user.user_id, workspace_id="ws-team"
    ) is None
    assert await invitations.has_open_for_email(
        tenant_id="default", email="alice@example.com", now=time.time()
    )


@pytest.mark.asyncio
async def test_invite_only_registration_needs_a_verified_email():
    identity = MemoryIdentityStore()
    invitations = await _invited(identity)
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    lifecycle = _memory_lifecycle(
        users=users, sessions=sessions, invitations=invitations
    )
    proposed = _session(uuid.uuid4(), subject="alice")

    with pytest.raises(RegistrationDenied) as excinfo:
        await lifecycle.provision_login(
            _login(
                proposed,
                email="alice@example.com",
                email_verified=False,
                invitation_required=True,
            )
        )

    assert (excinfo.value.reason, str(excinfo.value)) == (
        "email_unverified",
        CONFIRMED_EMAIL_REQUIRED,
    )

    assert await users.find_user(
        tenant_id="default", issuer=proposed.issuer, subject="alice"
    ) is None
    assert await sessions.get(proposed.id) is None


# The admins of one identity source (memory twins of the Postgres cases).

GATEWAY_ISSUER = "gateway:corp-sso"


async def _admin(
    users: MemoryUserDirectory, *, issuer: str, subject: str
) -> uuid.UUID:
    user = await users.record_login(
        tenant_id="default",
        issuer=issuer,
        subject=subject,
        email=f"{subject}@example.com",
        email_verified=True,
        display_name=subject,
    )
    await users.set_instance_role(
        tenant_id="default", user_id=user.user_id, role="admin"
    )
    return user.user_id


def _source_lifecycle(
    users: MemoryUserDirectory,
    sessions: MemorySessionStore,
    source_issuer: str | None,
) -> tuple[MemoryUserLifecycleTransaction, UserLifecycleService]:
    transaction = MemoryUserLifecycleTransaction(
        users=users, sessions=sessions, source_issuer=source_issuer
    )
    return transaction, UserLifecycleService(
        users=users, sessions=sessions, transaction=transaction
    )


@pytest.mark.asyncio
async def test_first_owner_counts_only_admins_of_the_identity_source():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    await _admin(users, issuer="local", subject="legacy-admin")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    first = await lifecycle.provision_login(
        _login(
            _session(uuid.uuid4(), subject="gw-1", issuer=GATEWAY_ISSUER),
            first_login_owner=True,
        )
    )
    second = await lifecycle.provision_login(
        _login(
            _session(uuid.uuid4(), subject="gw-2", issuer=GATEWAY_ISSUER),
            first_login_owner=True,
        )
    )

    assert first.instance_role == "admin"
    assert second.instance_role == "user"


@pytest.mark.asyncio
async def test_without_a_source_every_admin_counts_for_the_first_owner():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    await _admin(users, issuer="local", subject="legacy-admin")
    _transaction, lifecycle = _source_lifecycle(users, sessions, None)

    first = await lifecycle.provision_login(
        _login(_session(uuid.uuid4(), subject="oidc-1"), first_login_owner=True)
    )

    assert first.instance_role == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["demote", "disable"])
async def test_the_last_admin_of_the_source_is_kept_beside_another_issuer(
    command: str,
):
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    await _admin(users, issuer="local", subject="legacy-admin")
    gateway = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    if command == "demote":
        status = await lifecycle.set_role(
            tenant_id="default",
            user_id=gateway,
            role="user",
            actor_user_id=gateway,
        )
    else:
        status = await lifecycle.set_disabled(
            tenant_id="default",
            user_id=gateway,
            disabled_at=time.time(),
            actor=AdminActor(gateway),
        )

    assert status is UserLifecycleStatus.LAST_ADMIN
    kept = await users.find_by_user_id(tenant_id="default", user_id=gateway)
    assert kept is not None
    assert (kept.instance_role, kept.disabled_at) == ("admin", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["demote", "disable"])
async def test_an_admin_of_another_issuer_is_not_kept_as_the_last_admin(
    command: str,
):
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    legacy = await _admin(users, issuer="local", subject="legacy-admin")
    gateway = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    if command == "demote":
        status = await lifecycle.set_role(
            tenant_id="default",
            user_id=legacy,
            role="user",
            actor_user_id=gateway,
        )
    else:
        status = await lifecycle.set_disabled(
            tenant_id="default",
            user_id=legacy,
            disabled_at=time.time(),
            actor=AdminActor(gateway),
        )

    assert status is UserLifecycleStatus.UPDATED


@pytest.mark.asyncio
async def test_the_last_active_admin_can_be_neither_demoted_nor_disabled():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, None)

    async def demote(user_id: uuid.UUID, actor: uuid.UUID) -> UserLifecycleStatus:
        return await lifecycle.set_role(
            tenant_id="default", user_id=user_id, role="user", actor_user_id=actor
        )

    async def disable(user_id: uuid.UUID, actor: uuid.UUID) -> UserLifecycleStatus:
        return await lifecycle.set_disabled(
            tenant_id="default",
            user_id=user_id,
            disabled_at=time.time(),
            actor=AdminActor(actor),
        )

    first = await _admin(users, issuer="local", subject="a")
    assert await demote(first, first) is UserLifecycleStatus.LAST_ADMIN
    assert await disable(first, first) is UserLifecycleStatus.LAST_ADMIN

    # With a second admin the first may step down; the second is then the
    # last active admin and keeps both the role and the access.
    second = await _admin(users, issuer="local", subject="b")
    assert await demote(first, second) is UserLifecycleStatus.UPDATED
    assert await demote(second, second) is UserLifecycleStatus.LAST_ADMIN
    assert await disable(second, second) is UserLifecycleStatus.LAST_ADMIN

    plain = await users.record_login(
        tenant_id="default",
        issuer="local",
        subject="c",
        email="c@example.com",
        email_verified=True,
        display_name="c",
    )
    assert await disable(plain.user_id, second) is UserLifecycleStatus.UPDATED


@pytest.mark.asyncio
async def test_the_first_owner_is_counted_per_tenant():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, None)

    owners = [
        await lifecycle.provision_login(
            replace(
                _login(
                    _session(uuid.uuid4(), subject="same-subject"),
                    first_login_owner=True,
                ),
                tenant_id=tenant_id,
            )
        )
        for tenant_id in ("tenant-a", "tenant-b")
    ]

    assert [owner.instance_role for owner in owners] == ["admin", "admin"]


@pytest.mark.asyncio
async def test_admin_events_wake_admins_of_every_issuer():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    legacy = await _admin(users, issuer="local", subject="legacy-admin")
    gateway = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    member = await users.record_login(
        tenant_id="default",
        issuer=GATEWAY_ISSUER,
        subject="gw-member",
        email="gw-member@example.com",
        email_verified=True,
        display_name="Member",
    )
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    await lifecycle.set_role(
        tenant_id="default",
        user_id=member.user_id,
        role="admin",
        actor_user_id=gateway,
    )

    woken = {
        invalidation.target_user_id
        for invalidation in transaction.invalidations
        if invalidation.resource_id == str(member.user_id)
    }
    assert {legacy, gateway} <= woken


def test_an_empty_source_issuer_is_a_wiring_error():
    from inqtrix.storage.user_lifecycle_postgres import (
        PostgresUserLifecycleTransaction,
    )

    with pytest.raises(ValueError):
        MemoryUserLifecycleTransaction(
            users=MemoryUserDirectory(),
            sessions=MemorySessionStore(),
            source_issuer="",
        )
    with pytest.raises(ValueError):
        PostgresUserLifecycleTransaction(
            session_factory=object(), app_role="inqtrix_app", source_issuer=""
        )


async def _member(
    users: MemoryUserDirectory, *, issuer: str, subject: str
) -> uuid.UUID:
    user = await users.record_login(
        tenant_id="default",
        issuer=issuer,
        subject=subject,
        email=f"{subject}@example.com",
        email_verified=True,
        display_name=subject,
    )
    return user.user_id


async def _set_disabled(
    lifecycle: UserLifecycleService,
    user_id: uuid.UUID,
    actor: LifecycleActor,
    *,
    disabled: bool = True,
) -> UserLifecycleStatus:
    return await lifecycle.set_disabled(
        tenant_id="default",
        user_id=user_id,
        disabled_at=time.time() if disabled else None,
        actor=actor,
    )


async def _disable_state(
    users: MemoryUserDirectory, user_id: uuid.UUID
) -> tuple[bool, str | None]:
    user = await users.find_by_user_id(tenant_id="default", user_id=user_id)
    assert user is not None
    return user.disabled_at is not None, user.disabled_source


@pytest.mark.asyncio
async def test_the_actor_decides_the_source_of_a_disable_and_its_audit():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    by_admin = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    by_provider = await _member(users, issuer=GATEWAY_ISSUER, subject="bob")
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    assert (
        await _set_disabled(lifecycle, by_admin, AdminActor(admin))
        is UserLifecycleStatus.UPDATED
    )
    assert (
        await _set_disabled(lifecycle, by_provider, ProvisioningActor())
        is UserLifecycleStatus.UPDATED
    )

    assert await _disable_state(users, by_admin) == (True, "admin")
    assert await _disable_state(users, by_provider) == (True, "provisioning")
    assert {
        (entry.resource_id, entry.actor_user_id, entry.actor_type)
        for entry in transaction.audit_entries
        if entry.action == "user.disabled"
    } == {
        (str(by_admin), admin, "user"),
        (str(by_provider), None, "provisioning"),
    }


@pytest.mark.asyncio
async def test_a_disable_from_provisioning_cuts_the_account_off():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    browser_session = _session(alice, subject="alice", issuer=GATEWAY_ISSUER)
    await sessions.create(browser_session)
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    await _set_disabled(lifecycle, alice, ProvisioningActor())

    assert await sessions.get(browser_session.id) is None


@pytest.mark.asyncio
async def test_provisioning_reaches_only_accounts_of_the_identity_source():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    local = await _member(users, issuer="local", subject="carol")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    assert (
        await _set_disabled(lifecycle, local, ProvisioningActor())
        is UserLifecycleStatus.NOT_FOUND
    )
    assert await _disable_state(users, local) == (False, None)


@pytest.mark.asyncio
async def test_provisioning_neither_takes_over_nor_lifts_an_administrators_disable():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    await _set_disabled(lifecycle, alice, AdminActor(admin))

    # The provider reports the account inactive as well: the disable stays
    # the administrator's, so the provider's later "active" cannot lift it.
    assert (
        await _set_disabled(lifecycle, alice, ProvisioningActor())
        is UserLifecycleStatus.UPDATED
    )
    assert (
        await _set_disabled(lifecycle, alice, ProvisioningActor(), disabled=False)
        is UserLifecycleStatus.KEPT_DISABLED
    )

    assert await _disable_state(users, alice) == (True, "admin")
    assert [
        entry.action
        for entry in transaction.audit_entries
        if entry.resource_id == str(alice)
    ] == ["user.disabled", "user.scim_enable_ignored"]


@pytest.mark.asyncio
async def test_a_disable_without_a_source_counts_as_an_administrators():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    # A disable written before the source existed.
    await users.set_disabled(
        tenant_id="default", user_id=alice, disabled_at=time.time()
    )
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    assert (
        await _set_disabled(lifecycle, alice, ProvisioningActor(), disabled=False)
        is UserLifecycleStatus.KEPT_DISABLED
    )
    assert await _disable_state(users, alice) == (True, None)


@pytest.mark.asyncio
async def test_provisioning_lifts_its_own_disable_and_an_administrator_lifts_any():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    await _set_disabled(lifecycle, alice, ProvisioningActor())
    assert (
        await _set_disabled(lifecycle, alice, ProvisioningActor(), disabled=False)
        is UserLifecycleStatus.UPDATED
    )
    assert await _disable_state(users, alice) == (False, None)

    await _set_disabled(lifecycle, alice, ProvisioningActor())
    await _set_disabled(lifecycle, alice, AdminActor(admin), disabled=False)
    assert await _disable_state(users, alice) == (False, None)

    # An administrator who disables an account the provider disabled
    # takes the disable over.
    await _set_disabled(lifecycle, alice, ProvisioningActor())
    await _set_disabled(lifecycle, alice, AdminActor(admin))
    assert await _disable_state(users, alice) == (True, "admin")


@pytest.mark.asyncio
async def test_provisioning_never_disables_the_last_admin_of_the_source():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    await _admin(users, issuer="local", subject="legacy-admin")
    gateway = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    assert (
        await _set_disabled(lifecycle, gateway, ProvisioningActor())
        is UserLifecycleStatus.LAST_ADMIN
    )
    assert await _disable_state(users, gateway) == (False, None)


@pytest.mark.asyncio
async def test_provisioning_without_an_identity_source_is_a_wiring_error():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    _transaction, lifecycle = _source_lifecycle(users, sessions, None)

    with pytest.raises(RuntimeError, match="identity source"):
        await _set_disabled(lifecycle, alice, ProvisioningActor())
    assert await _disable_state(users, alice) == (False, None)


@pytest.mark.asyncio
async def test_an_administrator_without_the_role_cannot_disable():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    former = await _member(users, issuer=GATEWAY_ISSUER, subject="former-admin")
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    with pytest.raises(AdminAuthorizationError):
        await _set_disabled(lifecycle, alice, AdminActor(former))
    assert await _disable_state(users, alice) == (False, None)


def _profile(user_name: str = "alice.smith", **values) -> ExternalProfile:
    return ExternalProfile(user_name=user_name, **values)


async def _provisioned(
    lifecycle: UserLifecycleService, subject: str, **values
) -> uuid.UUID:
    outcome = await lifecycle.provision_external_user(
        tenant_id="default", subject=subject, profile=_profile(**values)
    )
    assert outcome.status is UserLifecycleStatus.UPDATED and outcome.user is not None
    return outcome.user.user_id


def _actions(transaction: MemoryUserLifecycleTransaction, user_id: uuid.UUID):
    return [
        entry.action
        for entry in transaction.audit_entries
        if entry.resource_id == str(user_id)
    ]


@pytest.mark.asyncio
async def test_provisioning_creates_an_account_without_a_sign_in():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id="default",
        subject="oid-alice",
        profile=_profile(
            display_name="Alice Smith",
            name={"givenName": "Alice"},
            email="alice@example.com",
        ),
    )

    assert outcome.status is UserLifecycleStatus.UPDATED and not outcome.adopted
    user = outcome.user
    assert user is not None
    assert (user.issuer, user.subject, user.scim_user_name) == (
        GATEWAY_ISSUER,
        "oid-alice",
        "alice.smith",
    )
    assert (user.email, user.email_verified, user.display_name) == (
        "alice@example.com",
        False,
        "Alice Smith",
    )
    assert user.scim_name == {"givenName": "Alice"}
    assert (user.last_login_at, user.disabled_at, user.instance_role) == (
        None,
        None,
        "user",
    )
    assert [
        (entry.action, entry.actor_user_id, entry.actor_type, entry.detail)
        for entry in transaction.audit_entries
    ] == [
        (
            "user.provisioned",
            None,
            "provisioning",
            {"fields": "userName,displayName,name,emails,active"},
        )
    ]


@pytest.mark.asyncio
async def test_an_inactive_account_from_provisioning_starts_disabled_by_it():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    alice = await _provisioned(lifecycle, "oid-alice", active=False)

    assert await _disable_state(users, alice) == (True, "provisioning")


@pytest.mark.asyncio
async def test_provisioning_adopts_an_account_it_never_bound():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    signed_in = await _member(users, issuer=GATEWAY_ISSUER, subject="oid-alice")
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id="default",
        subject="oid-alice",
        profile=_profile(display_name="Other Name", email="other@example.com"),
    )

    assert outcome.status is UserLifecycleStatus.UPDATED and outcome.adopted
    user = outcome.user
    assert user is not None and user.user_id == signed_in
    assert user.scim_user_name == "alice.smith"
    # Adoption binds the account; the profile a sign-in brought stays.
    assert (user.email, user.display_name) == ("oid-alice@example.com", "oid-alice")
    assert [
        (entry.action, entry.detail) for entry in transaction.audit_entries
    ] == [("user.scim_adopted", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["bound", "tombstone", "erasing", "name_taken", "name_in_another_case"]
)
async def test_provisioning_refuses_what_is_already_taken(state: str):
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="oid-alice")
    bob = await _member(users, issuer=GATEWAY_ISSUER, subject="oid-bob")
    changes = {
        "bound": (alice, {"scim_user_name": "alice.old"}),
        # A tombstone need not be bound: provisioning can delete an account
        # of the source that it never bound.
        "tombstone": (
            alice,
            {"scim_deleted_at": time.time(), "disabled_at": time.time()},
        ),
        "erasing": (alice, {"erasure_operation_id": "op-erase"}),
        "name_taken": (bob, {"scim_user_name": "alice.smith"}),
        "name_in_another_case": (bob, {"scim_user_name": "ALICE.Smith"}),
    }
    target, values = changes[state]
    users.update_user(tenant_id="default", user_id=target, **values)
    before = await users.find_by_user_id(tenant_id="default", user_id=alice)
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id="default", subject="oid-alice", profile=_profile()
    )

    assert outcome.status is UserLifecycleStatus.CONFLICT and outcome.user is None
    assert await users.find_by_user_id(tenant_id="default", user_id=alice) == before


@pytest.mark.asyncio
async def test_provisioning_binds_only_under_the_identity_source():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    local = await _member(users, issuer="local", subject="oid-alice")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id="default", subject="oid-alice", profile=_profile()
    )

    assert outcome.status is UserLifecycleStatus.UPDATED and not outcome.adopted
    assert outcome.user is not None and outcome.user.user_id != local
    assert outcome.user.issuer == GATEWAY_ISSUER
    kept = await users.find_by_user_id(tenant_id="default", user_id=local)
    assert kept is not None and kept.scim_user_name is None


@pytest.mark.asyncio
async def test_a_change_from_provisioning_unconfirms_only_a_new_email():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice", email="alice@example.com")
    # A sign-in confirmed the address.
    users.update_user(tenant_id="default", user_id=alice, email_verified=True)

    renamed = await lifecycle.update_external_profile(
        tenant_id="default",
        user_id=alice,
        change=lambda profile: replace(profile, display_name="Alice S."),
    )
    moved = await lifecycle.update_external_profile(
        tenant_id="default",
        user_id=alice,
        change=lambda profile: replace(profile, email="new@example.com"),
    )

    assert renamed.status is moved.status is UserLifecycleStatus.UPDATED
    assert renamed.user is not None and renamed.user.email_verified is True
    assert moved.user is not None
    assert (moved.user.email, moved.user.email_verified) == ("new@example.com", False)
    assert [
        (entry.action, entry.detail)
        for entry in transaction.audit_entries
        if entry.action == "user.scim_updated"
    ] == [
        ("user.scim_updated", {"fields": "displayName"}),
        ("user.scim_updated", {"fields": "emails"}),
    ]


@pytest.mark.asyncio
async def test_active_from_a_change_follows_the_disable_rules():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    bob = await _provisioned(lifecycle, "oid-bob", user_name="bob")

    inactive = await lifecycle.update_external_profile(
        tenant_id="default",
        user_id=alice,
        change=lambda profile: replace(profile, active=False),
    )
    await _set_disabled(lifecycle, bob, AdminActor(admin))
    kept = await lifecycle.update_external_profile(
        tenant_id="default",
        user_id=bob,
        change=lambda profile: replace(profile, active=True),
    )

    assert inactive.status is kept.status is UserLifecycleStatus.UPDATED
    assert await _disable_state(users, alice) == (True, "provisioning")
    assert await _disable_state(users, bob) == (True, "admin")
    assert _actions(transaction, bob)[-1] == "user.scim_enable_ignored"


@pytest.mark.asyncio
async def test_a_change_to_a_taken_user_name_is_a_conflict():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    await _provisioned(lifecycle, "oid-bob", user_name="bob")

    outcome = await lifecycle.update_external_profile(
        tenant_id="default",
        user_id=alice,
        change=lambda profile: replace(profile, user_name="BOB", display_name="x"),
    )

    assert outcome.status is UserLifecycleStatus.CONFLICT
    kept = await users.find_by_user_id(tenant_id="default", user_id=alice)
    assert kept is not None
    assert (kept.scim_user_name, kept.display_name) == ("alice.smith", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["other_issuer", "tombstone", "erasing"])
async def test_a_change_reaches_no_foreign_tombstoned_or_erased_account(state: str):
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    if state == "other_issuer":
        target = await _member(users, issuer="local", subject="carol")
    else:
        target = await _provisioned(lifecycle, "oid-carol", user_name="carol")
        values = (
            {"scim_deleted_at": time.time(), "disabled_at": time.time()}
            if state == "tombstone"
            else {"erasure_operation_id": "op-erase"}
        )
        users.update_user(tenant_id="default", user_id=target, **values)
    before = await users.find_by_user_id(tenant_id="default", user_id=target)

    outcome = await lifecycle.update_external_profile(
        tenant_id="default",
        user_id=target,
        change=lambda profile: replace(profile, display_name="Changed"),
    )

    assert outcome.status is UserLifecycleStatus.NOT_FOUND
    assert await users.find_by_user_id(tenant_id="default", user_id=target) == before


@pytest.mark.asyncio
async def test_deprovisioning_cuts_off_and_leaves_a_tombstone_once():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    browser_session = _session(alice, subject="oid-alice", issuer=GATEWAY_ISSUER)
    await sessions.create(browser_session)

    first = await lifecycle.deprovision(tenant_id="default", user_id=alice)
    second = await lifecycle.deprovision(tenant_id="default", user_id=alice)
    again = await lifecycle.provision_external_user(
        tenant_id="default", subject="oid-alice", profile=_profile()
    )

    assert first is UserLifecycleStatus.UPDATED
    assert second is UserLifecycleStatus.NOT_FOUND
    assert again.status is UserLifecycleStatus.CONFLICT
    user = await users.find_by_user_id(tenant_id="default", user_id=alice)
    assert user is not None and user.scim_deleted_at is not None
    assert await _disable_state(users, alice) == (True, "provisioning")
    assert await sessions.get(browser_session.id) is None
    assert _actions(transaction, alice) == [
        "user.provisioned",
        "user.disabled",
        "user.deprovisioned",
    ]


@pytest.mark.asyncio
async def test_deprovisioning_frees_the_user_name_but_never_the_identity():
    """A new person of the source can take the user name of a deleted
    account; the deleted identity itself does not come back."""
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    await lifecycle.deprovision(tenant_id="default", user_id=alice)

    successor = await lifecycle.provision_external_user(
        tenant_id="default", subject="oid-successor", profile=_profile()
    )
    # A free user name, so that only the identity can refuse it.
    returning = await lifecycle.provision_external_user(
        tenant_id="default", subject="oid-alice", profile=_profile("alice.returns")
    )

    tombstone = await users.find_by_user_id(tenant_id="default", user_id=alice)
    assert tombstone is not None and tombstone.scim_deleted_at is not None
    assert tombstone.scim_user_name is None
    assert successor.status is UserLifecycleStatus.UPDATED
    assert successor.user is not None and successor.user.scim_user_name == "alice.smith"
    assert returning.status is UserLifecycleStatus.CONFLICT


@pytest.mark.asyncio
async def test_deprovisioning_keeps_an_administrators_disable_and_the_last_admin():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    await _set_disabled(lifecycle, alice, AdminActor(admin))

    kept_source = await lifecycle.deprovision(tenant_id="default", user_id=alice)
    last_admin = await lifecycle.deprovision(tenant_id="default", user_id=admin)

    assert kept_source is UserLifecycleStatus.UPDATED
    assert await _disable_state(users, alice) == (True, "admin")
    assert last_admin is UserLifecycleStatus.LAST_ADMIN
    spared = await users.find_by_user_id(tenant_id="default", user_id=admin)
    assert spared is not None
    assert (spared.disabled_at, spared.scim_deleted_at) == (None, None)


@pytest.mark.asyncio
async def test_the_provisioning_page_shows_the_source_without_tombstones_or_erasures():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    first = await _provisioned(lifecycle, "oid-a", user_name="a.user")
    disabled = await _provisioned(lifecycle, "oid-b", user_name="B.User", active=False)
    tombstone = await _provisioned(lifecycle, "oid-c", user_name="c.user")
    await lifecycle.deprovision(tenant_id="default", user_id=tombstone)
    erasing = await _provisioned(lifecycle, "oid-d", user_name="d.user")
    users.update_user(
        tenant_id="default", user_id=erasing, erasure_operation_id="op-erase"
    )
    await _member(users, issuer="local", subject="oid-e")
    unbound = await _member(users, issuer=GATEWAY_ISSUER, subject="oid-f")

    def ids(page):
        return [user.user_id for user in page[1]]

    everything = await users.list_users_page(tenant_id="default", issuer=GATEWAY_ISSUER)
    by_name = await users.list_users_page(
        tenant_id="default", issuer=GATEWAY_ISSUER, user_name="b.user"
    )
    by_subject = await users.list_users_page(
        tenant_id="default", issuer=GATEWAY_ISSUER, subject="oid-f"
    )
    second = await users.list_users_page(
        tenant_id="default", issuer=GATEWAY_ISSUER, start_index=2, count=1
    )

    assert (everything[0], ids(everything)) == (3, [first, disabled, unbound])
    assert (by_name[0], ids(by_name)) == (1, [disabled])
    assert (by_subject[0], ids(by_subject)) == (1, [unbound])
    assert (second[0], ids(second)) == (3, [disabled])


@pytest.mark.asyncio
async def test_a_sign_in_keeps_the_provisioning_binding():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice", name={"givenName": "Alice"})
    proposed = _session(uuid.uuid4(), subject="oid-alice", issuer=GATEWAY_ISSUER)

    resolved = await lifecycle.provision_login(
        LoginCommand(
            tenant_id="default",
            issuer=GATEWAY_ISSUER,
            subject="oid-alice",
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
            session=proposed,
        )
    )

    assert resolved.user_id == alice
    assert (resolved.scim_user_name, resolved.scim_name) == (
        "alice.smith",
        {"givenName": "Alice"},
    )
    assert (resolved.email, resolved.email_verified) == ("alice@example.com", True)
    assert resolved.last_login_at is not None


@pytest.mark.asyncio
async def test_a_change_without_a_user_name_leaves_an_account_unbound():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    first = await _member(users, issuer=GATEWAY_ISSUER, subject="oid-f")
    second = await _member(users, issuer=GATEWAY_ISSUER, subject="oid-g")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)

    outcomes = [
        await lifecycle.update_external_profile(
            tenant_id="default",
            user_id=target,
            change=lambda profile: replace(profile, display_name="Renamed"),
        )
        for target in (first, second)
    ]

    assert [outcome.status for outcome in outcomes] == [
        UserLifecycleStatus.UPDATED,
        UserLifecycleStatus.UPDATED,
    ]
    for target in (first, second):
        user = await users.find_by_user_id(tenant_id="default", user_id=target)
        assert user is not None
        assert (user.scim_user_name, user.display_name) == (None, "Renamed")


def _source_login(subject: str) -> LoginCommand:
    session = _session(uuid.uuid4(), subject=subject, issuer=GATEWAY_ISSUER)
    return LoginCommand(
        tenant_id="default",
        issuer=GATEWAY_ISSUER,
        subject=subject,
        email=session.email or "",
        email_verified=True,
        display_name=session.display_name,
        session=session,
        provisioned_only=True,
    )


@pytest.mark.asyncio
async def test_when_provisioning_owns_creation_an_unknown_identity_gets_no_account():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    login = _source_login("oid-new")

    with pytest.raises(UserNotProvisionedError):
        await lifecycle.provision_login(login)

    assert list(await users.list_users(tenant_id="default")) == []
    assert await sessions.get(login.session.id) is None


@pytest.mark.asyncio
async def test_when_provisioning_owns_creation_a_provisioned_account_signs_in():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")

    user = await lifecycle.provision_login(_source_login("oid-alice"))

    assert user.user_id == alice


@pytest.mark.asyncio
async def test_when_provisioning_owns_creation_a_tombstone_stays_disabled():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    assert (
        await lifecycle.deprovision(tenant_id="default", user_id=alice)
        is UserLifecycleStatus.UPDATED
    )

    with pytest.raises(UserDisabledError):
        await lifecycle.provision_login(_source_login("oid-alice"))


def _enable_details(transaction: MemoryUserLifecycleTransaction, user_id: uuid.UUID):
    return [
        entry.detail
        for entry in transaction.audit_entries
        if entry.resource_id == str(user_id) and entry.action == "user.enabled"
    ]


@pytest.mark.asyncio
async def test_an_administrator_lifts_a_tombstone_with_the_disable():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    assert (
        await lifecycle.deprovision(tenant_id="default", user_id=alice)
        is UserLifecycleStatus.UPDATED
    )

    status = await _set_disabled(lifecycle, alice, AdminActor(admin), disabled=False)

    restored = await users.find_by_user_id(tenant_id="default", user_id=alice)
    assert status is UserLifecycleStatus.UPDATED
    assert (restored.disabled_at, restored.scim_deleted_at, restored.disabled_source) == (
        None,
        None,
        None,
    )
    assert _enable_details(transaction, alice) == [{"restored_from_deprovisioning": "true"}]


@pytest.mark.asyncio
async def test_a_restored_account_stays_unbound_until_provisioning_binds_it():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    await lifecycle.deprovision(tenant_id="default", user_id=alice)
    await _set_disabled(lifecycle, alice, AdminActor(admin), disabled=False)
    restored = await users.find_by_user_id(tenant_id="default", user_id=alice)

    bound = await lifecycle.provision_external_user(
        tenant_id="default", subject="oid-alice", profile=_profile()
    )

    assert restored is not None and restored.scim_user_name is None
    assert bound.status is UserLifecycleStatus.UPDATED and bound.adopted
    assert bound.user is not None and bound.user.user_id == alice
    assert bound.user.scim_user_name == "alice.smith"


@pytest.mark.asyncio
async def test_an_enable_without_a_tombstone_names_no_restore():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    admin = await _admin(users, issuer=GATEWAY_ISSUER, subject="gw-admin")
    alice = await _member(users, issuer=GATEWAY_ISSUER, subject="alice")
    transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    await _set_disabled(lifecycle, alice, AdminActor(admin))

    await _set_disabled(lifecycle, alice, AdminActor(admin), disabled=False)

    assert _enable_details(transaction, alice) == [{}]


@pytest.mark.asyncio
async def test_provisioning_never_lifts_a_tombstone():
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    _transaction, lifecycle = _source_lifecycle(users, sessions, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, "oid-alice")
    await lifecycle.deprovision(tenant_id="default", user_id=alice)

    status = await _set_disabled(lifecycle, alice, ProvisioningActor(), disabled=False)

    buried = await users.find_by_user_id(tenant_id="default", user_id=alice)
    assert status is UserLifecycleStatus.NOT_FOUND
    assert buried.disabled_at is not None and buried.scim_deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["deprovision", "active-false"])
async def test_provisioning_cuts_off_and_wakes_the_account(command):
    """A deactivation through provisioning ends the account's tokens and wakes
    its open streams in the same step."""
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    pat_store = MemoryPatStore()
    transaction = MemoryUserLifecycleTransaction(
        users=users, sessions=sessions, pat_store=pat_store, source_issuer=GATEWAY_ISSUER
    )
    lifecycle = UserLifecycleService(users=users, sessions=sessions, transaction=transaction)
    alice = await _provisioned(lifecycle, "oid-alice")
    await PatService(store=pat_store, pepper="p" * 32, default_ttl_days=0).create_token(
        tenant_id="default", owner_user_id=alice, name="automation"
    )
    woken_before = [item for item in transaction.invalidations if item.target_user_id == alice]

    if command == "deprovision":
        await lifecycle.deprovision(tenant_id="default", user_id=alice)
    else:
        await lifecycle.update_external_profile(
            tenant_id="default", user_id=alice, change=lambda profile: replace(profile, active=False)
        )

    woken = [item for item in transaction.invalidations if item.target_user_id == alice]
    assert len(woken) > len(woken_before)
    assert await pat_store.list_for_owner(tenant_id="default", owner_user_id=alice) == ()
