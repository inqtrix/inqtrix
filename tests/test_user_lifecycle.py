"""Canonical-user lifecycle commands and their atomic memory semantics."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid

import pytest

from inqtrix.auth.credentials import LocalCredential, MemoryCredentialStore
from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.invitations import (
    Invitation,
    MemoryInvitationStore,
    RegistrationDenied,
)
from inqtrix.auth.lifecycle import (
    AdminAuthorizationError,
    LoginCommand,
    MemoryUserLifecycleTransaction,
    UserLifecycleService,
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
        actor_user_id=admin_id,
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
