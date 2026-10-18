"""Share-service contracts over the memory identity store.

The service exposes direct user shares only.  Canonical user UUIDs are the
authority boundary; external IdP subjects never enter share records.  Active
duplicates conflict, permission changes use optimistic revisions, acceptance
is idempotent, and either the owner or the recipient may remove a share.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from typing import cast

import pytest

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.memory_authority import (
    MemoryAuthorityCoordinator,
    MemoryResourceSnapshot,
)
from inqtrix.auth.permissions import (
    AuditEntry,
    AuthorizationService,
    SharePermission,
    WorkspaceRole,
)
from inqtrix.auth.principal import Principal
from inqtrix.auth.shares import (
    OutgoingItem,
    ShareBackendUnsupported,
    ShareConflict,
    ShareFacts,
    ShareNotAllowed,
    ShareRecord,
    ShareService,
    ShareValidationError,
)

OWNER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
RECIPIENT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SECOND_RECIPIENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
STRANGER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
FOREIGN_OWNER_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
UNKNOWN_USER_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")

OWNER = Principal(user_id=OWNER_ID, kind="oidc_session")
RECIPIENT = Principal(user_id=RECIPIENT_ID, kind="oidc_session")
SECOND_RECIPIENT = Principal(
    user_id=SECOND_RECIPIENT_ID, kind="oidc_session"
)
STRANGER = Principal(user_id=STRANGER_ID, kind="oidc_session")
FOREIGN_OWNER = Principal(user_id=FOREIGN_OWNER_ID, kind="oidc_session")

RESOURCES = {
    "run_owned": OWNER_ID,
    "run_foreign": FOREIGN_OWNER_ID,
    "editor_owned": OWNER_ID,
}
TITLES = {
    "run_owned": "Meine Recherche",
    "editor_owned": "Draft report",
}
KNOWN_USERS = {
    OWNER_ID,
    RECIPIENT_ID,
    SECOND_RECIPIENT_ID,
    STRANGER_ID,
}


def make_service(
    store: MemoryIdentityStore | None = None,
    *,
    unsupported_resource_types: tuple[str, ...] = (),
) -> tuple[ShareService, AuthorizationService, MemoryIdentityStore]:
    """Build the direct-share service with deterministic UUID identities."""
    identity = store or MemoryIdentityStore()
    permissions = AuthorizationService(
        members=identity,
        shares=identity,
        audit=identity,
    )

    async def resolve_resource_owner(
        tenant_id: str, resource_id: str
    ) -> uuid.UUID | None:
        return RESOURCES.get(resource_id)

    async def resolve_resource_title(
        tenant_id: str, resource_id: str
    ) -> str | None:
        return TITLES.get(resource_id)

    async def resolve_share_facts(
        tenant_id: str, resource_ids: Sequence[str]
    ) -> dict[str, ShareFacts]:
        return {
            resource_id: ShareFacts(
                owner_user_id=RESOURCES[resource_id],
                title=TITLES.get(resource_id),
            )
            for resource_id in resource_ids
            if resource_id in RESOURCES
        }

    async def user_exists(tenant_id: str, user_id: uuid.UUID) -> bool:
        return user_id in KNOWN_USERS

    service = ShareService(
        shares=identity,
        permissions=permissions,
        owner_resolvers={
            "run": resolve_resource_owner,
            "editor_document": resolve_resource_owner,
        },
        share_facts_resolvers={
            "run": resolve_share_facts,
            "editor_document": resolve_share_facts,
        },
        title_resolvers={
            "run": resolve_resource_title,
            "editor_document": resolve_resource_title,
        },
        user_lookup=user_exists,
        audit=identity,
        unsupported_resource_types=unsupported_resource_types,
    )
    return service, permissions, identity


async def grant(
    service: ShareService,
    principal: Principal = OWNER,
    resource_type: str = "run",
    resource_id: str = "run_owned",
    invitees: tuple[tuple[uuid.UUID, SharePermission], ...] = (
        (RECIPIENT_ID, SharePermission.VIEW),
    ),
) -> tuple[ShareRecord, ...]:
    """Grant the selected resource to the default recipient."""
    return await service.grant(
        principal,
        resource_type=resource_type,
        resource_id=resource_id,
        invitees=list(invitees),
    )


class TestGrant:
    @pytest.mark.asyncio
    async def test_grant_is_pending_until_recipient_accepts(self):
        service, permissions, _identity = make_service()

        created = (await grant(service))[0]

        assert created.recipient_user_id == RECIPIENT_ID
        assert created.permission is SharePermission.VIEW
        assert created.accepted_at is None
        assert not await permissions.can(
            RECIPIENT,
            SharePermission.VIEW,
            owner_user_id=OWNER_ID,
            resource_tenant_id="default",
            resource_type="run",
            resource_id="run_owned",
        )

        accepted = await service.accept(RECIPIENT, share_id=created.id)

        assert accepted is not None
        assert accepted.accepted_at is not None
        assert await permissions.can(
            RECIPIENT,
            SharePermission.VIEW,
            owner_user_id=OWNER_ID,
            resource_tenant_id="default",
            resource_type="run",
            resource_id="run_owned",
        )

    @pytest.mark.asyncio
    async def test_editor_document_accepts_suggest_grant(self) -> None:
        service, permissions, _identity = make_service()

        created = (
            await grant(
                service,
                resource_type="editor_document",
                resource_id="editor_owned",
                invitees=((RECIPIENT_ID, SharePermission.SUGGEST),),
            )
        )[0]
        accepted = await service.accept(RECIPIENT, share_id=created.id)

        assert accepted is not None
        assert accepted.permission is SharePermission.SUGGEST
        assert await permissions.can(
            RECIPIENT,
            SharePermission.SUGGEST,
            owner_user_id=OWNER_ID,
            resource_tenant_id="default",
            resource_type="editor_document",
            resource_id="editor_owned",
        )
        assert not await permissions.can(
            RECIPIENT,
            SharePermission.EDIT,
            owner_user_id=OWNER_ID,
            resource_tenant_id="default",
            resource_type="editor_document",
            resource_id="editor_owned",
        )

    @pytest.mark.asyncio
    async def test_existing_resource_rejects_suggest_grant(self) -> None:
        service, _permissions, _identity = make_service()

        with pytest.raises(ShareValidationError, match="resource type"):
            await grant(
                service,
                invitees=((RECIPIENT_ID, SharePermission.SUGGEST),),
            )

        assert await service.list_for_resource(
            OWNER,
            resource_type="run",
            resource_id="run_owned",
        ) == ()

    @pytest.mark.asyncio
    async def test_active_duplicate_conflicts_without_mutating_share(self):
        service, _permissions, _identity = make_service()
        original = (await grant(service))[0]

        with pytest.raises(ShareConflict) as error:
            await grant(
                service,
                invitees=((RECIPIENT_ID, SharePermission.EDIT),),
            )

        assert error.value.current_revision is None
        listed = await service.list_for_resource(
            OWNER,
            resource_type="run",
            resource_id="run_owned",
        )
        assert listed == (original,)

    @pytest.mark.asyncio
    async def test_only_owner_can_grant(self):
        service, _permissions, _identity = make_service()

        with pytest.raises(ShareNotAllowed):
            await grant(service, principal=STRANGER)
        with pytest.raises(ShareNotAllowed):
            await grant(service, resource_id="run_foreign")
        with pytest.raises(ShareNotAllowed):
            await grant(service, resource_id="run_missing")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("invitee", "permission"),
        [
            (OWNER_ID, SharePermission.VIEW),
            (RECIPIENT_ID, cast(SharePermission, "manage")),
            (UNKNOWN_USER_ID, SharePermission.VIEW),
        ],
    )
    async def test_invalid_grants_fail_loudly(
        self, invitee: uuid.UUID, permission: SharePermission
    ):
        service, _permissions, _identity = make_service()

        with pytest.raises(ShareValidationError):
            await grant(service, invitees=((invitee, permission),))

    @pytest.mark.asyncio
    async def test_unknown_resource_type_is_a_validation_error(self):
        service, _permissions, _identity = make_service()

        with pytest.raises(ShareValidationError, match="resource type"):
            await service.grant(
                OWNER,
                resource_type="comet",
                resource_id="x",
                invitees=[(RECIPIENT_ID, SharePermission.VIEW)],
            )

    @pytest.mark.asyncio
    async def test_known_resource_without_transactional_backend_is_unsupported(self):
        service, _permissions, _identity = make_service(
            unsupported_resource_types=("knowledge_collection",)
        )

        with pytest.raises(ShareBackendUnsupported, match="knowledge_collection"):
            await service.grant(
                OWNER,
                resource_type="knowledge_collection",
                resource_id="kc_unsafe",
                invitees=[(RECIPIENT_ID, SharePermission.VIEW)],
            )
        assert await service.accepted_for_recipient(
            RECIPIENT, resource_type="knowledge_collection"
        ) == {}

    @pytest.mark.asyncio
    async def test_workspace_reconcile_uses_owner_not_historical_grantor(self):
        identity = MemoryIdentityStore(restrict_to_workspace_members=True)
        users = MemoryUserDirectory()
        for user_id, subject in (
            (OWNER_ID, "owner"),
            (RECIPIENT_ID, "recipient"),
            (STRANGER_ID, "stranger"),
        ):
            await users.record_login(
                tenant_id="default",
                issuer="https://issuer.example",
                subject=subject,
                email=f"{subject}@example.com",
                email_verified=True,
                display_name=subject.title(),
                canonical_user_id=user_id,
            )
        owners = {"run_owned": OWNER_ID}
        authority = MemoryAuthorityCoordinator()
        authority.bind_users(users)
        identity.bind_authority_coordinator(authority)
        authority.register_resource(
            "run",
            lambda tenant_id, resource_id: MemoryResourceSnapshot(
                exists=tenant_id == "default" and resource_id in owners,
                owner_user_id=owners.get(resource_id),
            ),
        )
        identity.add_workspace("workspace-a")
        identity.add_member("workspace-a", OWNER_ID, WorkspaceRole.OWNER)
        identity.add_member("workspace-a", RECIPIENT_ID, WorkspaceRole.VIEWER)
        identity.add_share(
            recipient_user_id=RECIPIENT_ID,
            resource_type="run",
            resource_id="run_owned",
            permission=SharePermission.VIEW,
            granted_by_user_id=STRANGER_ID,
        )

        revoked = await identity.reconcile_workspace_shares(
            tenant_id="default"
        )

        assert revoked == 0
        assert await identity.permission_for(
            tenant_id="default",
            resource_type="run",
            resource_id="run_owned",
            recipient_user_id=RECIPIENT_ID,
        ) is SharePermission.VIEW

        owners["run_owned"] = STRANGER_ID
        revoked = await identity.reconcile_workspace_shares(
            tenant_id="default"
        )

        assert revoked == 1
        assert await identity.permission_for(
            tenant_id="default",
            resource_type="run",
            resource_id="run_owned",
            recipient_user_id=RECIPIENT_ID,
        ) is None


class TestPermissionUpdate:
    @pytest.mark.asyncio
    async def test_owner_updates_permission_at_expected_revision(self):
        service, permissions, _identity = make_service()
        created = (await grant(service))[0]
        accepted = await service.accept(RECIPIENT, share_id=created.id)
        assert accepted is not None

        updated = await service.update_permission(
            OWNER,
            share_id=created.id,
            permission=SharePermission.EDIT,
            expected_revision=accepted.revision,
        )

        assert updated.id == created.id
        assert updated.permission is SharePermission.EDIT
        assert updated.revision == accepted.revision + 1
        assert updated.accepted_at == accepted.accepted_at
        assert await permissions.can(
            RECIPIENT,
            SharePermission.EDIT,
            owner_user_id=OWNER_ID,
            resource_tenant_id="default",
            resource_type="run",
            resource_id="run_owned",
        )

    @pytest.mark.asyncio
    async def test_stale_revision_conflicts_without_lost_update(self):
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]
        current = await service.update_permission(
            OWNER,
            share_id=created.id,
            permission=SharePermission.EDIT,
            expected_revision=created.revision,
        )

        with pytest.raises(ShareConflict) as error:
            await service.update_permission(
                OWNER,
                share_id=created.id,
                permission=SharePermission.VIEW,
                expected_revision=created.revision,
            )

        assert error.value.current_revision == current.revision
        listed = await service.list_for_resource(
            OWNER,
            resource_type="run",
            resource_id="run_owned",
        )
        assert listed[0].permission is SharePermission.EDIT
        assert listed[0].revision == current.revision

    @pytest.mark.asyncio
    async def test_non_owner_cannot_update_permission(self):
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]

        with pytest.raises(ShareNotAllowed):
            await service.update_permission(
                RECIPIENT,
                share_id=created.id,
                permission=SharePermission.EDIT,
                expected_revision=created.revision,
            )

    @pytest.mark.asyncio
    async def test_existing_resource_rejects_suggest_update(self) -> None:
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]

        with pytest.raises(ShareValidationError, match="resource type"):
            await service.update_permission(
                OWNER,
                share_id=created.id,
                permission=SharePermission.SUGGEST,
                expected_revision=created.revision,
            )

        (unchanged,) = await service.list_for_resource(
            OWNER,
            resource_type="run",
            resource_id="run_owned",
        )
        assert unchanged.permission is SharePermission.VIEW
        assert unchanged.revision == created.revision

    @pytest.mark.asyncio
    async def test_editor_document_updates_to_suggest(self) -> None:
        service, _permissions, _identity = make_service()
        created = (
            await grant(
                service,
                resource_type="editor_document",
                resource_id="editor_owned",
            )
        )[0]

        updated = await service.update_permission(
            OWNER,
            share_id=created.id,
            permission=SharePermission.SUGGEST,
            expected_revision=created.revision,
        )

        assert updated.permission is SharePermission.SUGGEST
        assert updated.revision == created.revision + 1


class TestAcceptance:
    @pytest.mark.asyncio
    async def test_accept_is_idempotent_for_active_recipient_share(self):
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]

        first = await service.accept(RECIPIENT, share_id=created.id)
        second = await service.accept(RECIPIENT, share_id=created.id)

        assert first is not None
        assert second == first

    @pytest.mark.asyncio
    async def test_foreign_unknown_and_removed_shares_cannot_be_accepted(self):
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]

        assert await service.accept(OWNER, share_id=created.id) is None
        assert await service.accept(STRANGER, share_id=created.id) is None
        assert await service.accept(RECIPIENT, share_id="missing") is None
        assert await service.remove(OWNER, share_id=created.id) is not None
        assert await service.accept(RECIPIENT, share_id=created.id) is None


class TestRemoval:
    @pytest.mark.asyncio
    async def test_owner_withdraws_share_and_access_disappears(self):
        service, permissions, identity = make_service()
        created = (await grant(service))[0]
        assert await service.accept(RECIPIENT, share_id=created.id) is not None

        assert await service.remove(OWNER, share_id=created.id) is not None

        assert not await permissions.can(
            RECIPIENT,
            SharePermission.VIEW,
            owner_user_id=OWNER_ID,
            resource_tenant_id="default",
            resource_type="run",
            resource_id="run_owned",
        )
        assert "share.revoked" in [
            entry.action for entry in identity.audit_entries
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("accept_first", [False, True])
    async def test_recipient_can_decline_or_leave_own_share(
        self, accept_first: bool
    ):
        service, permissions, identity = make_service()
        created = (await grant(service))[0]
        if accept_first:
            assert (
                await service.accept(RECIPIENT, share_id=created.id)
                is not None
            )

        assert await service.remove(RECIPIENT, share_id=created.id) is not None

        assert await service.inbox(RECIPIENT) == ()
        assert not await permissions.can(
            RECIPIENT,
            SharePermission.VIEW,
            owner_user_id=OWNER_ID,
            resource_tenant_id="default",
            resource_type="run",
            resource_id="run_owned",
        )
        expected_action = "share.left" if accept_first else "share.declined"
        assert expected_action in [
            entry.action for entry in identity.audit_entries
        ]

    @pytest.mark.asyncio
    async def test_stranger_and_unknown_share_removal_are_hidden(self):
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]

        assert await service.remove(STRANGER, share_id=created.id) is None
        assert await service.remove(OWNER, share_id="missing") is None
        assert len(await service.inbox(RECIPIENT)) == 1


class TestListings:
    @pytest.mark.asyncio
    async def test_only_owner_can_list_resource_shares(self):
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]

        assert await service.list_for_resource(
            OWNER,
            resource_type="run",
            resource_id="run_owned",
        ) == (created,)
        with pytest.raises(ShareNotAllowed):
            await service.list_for_resource(
                RECIPIENT,
                resource_type="run",
                resource_id="run_owned",
            )

    @pytest.mark.asyncio
    async def test_inbox_keeps_pending_and_accepted_direct_shares(self):
        service, _permissions, _identity = make_service()
        created = (await grant(service))[0]

        pending = await service.inbox(RECIPIENT)
        assert len(pending) == 1
        assert pending[0].share_id == created.id
        assert pending[0].accepted_at is None

        assert await service.accept(RECIPIENT, share_id=created.id) is not None
        accepted = await service.inbox(RECIPIENT)
        assert len(accepted) == 1
        assert accepted[0].accepted_at is not None

    @pytest.mark.asyncio
    async def test_inbox_skips_titleless_resources(self):
        service, _permissions, identity = make_service()
        identity.add_share(
            recipient_user_id=RECIPIENT_ID,
            resource_type="run",
            resource_id="run_pruned",
            permission=SharePermission.VIEW,
            granted_by_user_id=OWNER_ID,
            accepted=False,
        )

        assert await service.inbox(RECIPIENT) == ()


def owner_listing_service(
    identity: MemoryIdentityStore,
    resources: dict[str, tuple[uuid.UUID, str | None]],
    calls: dict[str, list[int]],
) -> ShareService:
    """A share service over *resources* (id -> owner, title) that counts how
    often each resolver is asked."""

    async def owner(tenant_id: str, resource_id: str) -> uuid.UUID | None:
        calls["single"].append(1)
        found = resources.get(resource_id)
        return found[0] if found else None

    async def title(tenant_id: str, resource_id: str) -> str | None:
        calls["single"].append(1)
        found = resources.get(resource_id)
        return found[1] if found else None

    async def facts(
        tenant_id: str, resource_ids: Sequence[str]
    ) -> dict[str, ShareFacts]:
        calls["batch"].append(len(resource_ids))
        return {
            resource_id: ShareFacts(*resources[resource_id])
            for resource_id in resource_ids
            if resource_id in resources
        }

    async def user_exists(tenant_id: str, user_id: uuid.UUID) -> bool:
        return True

    return ShareService(
        shares=identity,
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        owner_resolvers={"run": owner, "editor_document": owner},
        share_facts_resolvers={"run": facts, "editor_document": facts},
        title_resolvers={"run": title, "editor_document": title},
        user_lookup=user_exists,
        audit=identity,
    )


class TestOwnerListing:
    @pytest.mark.asyncio
    async def test_reads_each_resource_type_once_however_many_are_shared(self):
        identity = MemoryIdentityStore()
        resources = {
            f"run_{index}": (OWNER_ID, f"Run {index}") for index in range(100)
        } | {
            f"editor_{index}": (OWNER_ID, f"Draft {index}") for index in range(31)
        }
        calls: dict[str, list[int]] = {"batch": [], "single": []}
        service = owner_listing_service(identity, resources, calls)
        for resource_id in resources:
            identity.add_share(
                recipient_user_id=RECIPIENT_ID,
                resource_type="run" if resource_id.startswith("run_") else "editor_document",
                resource_id=resource_id,
                permission=SharePermission.VIEW,
                granted_by_user_id=OWNER_ID,
            )

        listed = await service.mine(OWNER)

        assert len(listed) == 131
        assert calls["single"] == []
        assert sorted(calls["batch"]) == [31, 100]

    @pytest.mark.asyncio
    async def test_lists_owned_titled_resources_in_order_of_first_share(self):
        identity = MemoryIdentityStore()
        resources: dict[str, tuple[uuid.UUID, str | None]] = {
            "run_a": (OWNER_ID, "A"),
            "editor_b": (OWNER_ID, "B"),
            "run_foreign": (FOREIGN_OWNER_ID, "Foreign"),
            "run_z": (OWNER_ID, "Z"),
            "run_untitled": (OWNER_ID, None),
            "editor_c": (OWNER_ID, "C"),
        }
        service = owner_listing_service(
            identity, resources, {"batch": [], "single": []}
        )
        for recipient, resource_type, resource_id, granted_by, accepted in (
            (RECIPIENT_ID, "run", "run_a", OWNER_ID, False),
            (RECIPIENT_ID, "editor_document", "editor_b", OWNER_ID, True),
            (RECIPIENT_ID, "run", "run_foreign", FOREIGN_OWNER_ID, True),
            (RECIPIENT_ID, "run", "run_z", OWNER_ID, True),
            (SECOND_RECIPIENT_ID, "run", "run_a", OWNER_ID, True),
            (RECIPIENT_ID, "run", "run_untitled", OWNER_ID, True),
            # Granted by a collaborator: the owner still comes from the resource.
            (RECIPIENT_ID, "editor_document", "editor_c", SECOND_RECIPIENT_ID, False),
            (RECIPIENT_ID, "run", "run_gone", OWNER_ID, True),
            # Granted in the owner's name, but the resource belongs to another.
            (STRANGER_ID, "run", "run_foreign", OWNER_ID, True),
        ):
            identity.add_share(
                recipient_user_id=recipient,
                resource_type=resource_type,
                resource_id=resource_id,
                permission=SharePermission.VIEW,
                granted_by_user_id=granted_by,
                accepted=accepted,
            )

        assert await service.mine(OWNER) == (
            OutgoingItem("run", "run_a", "A", share_count=2, pending_count=1),
            OutgoingItem("editor_document", "editor_b", "B", share_count=1, pending_count=0),
            OutgoingItem("run", "run_z", "Z", share_count=1, pending_count=0),
            OutgoingItem("editor_document", "editor_c", "C", share_count=1, pending_count=1),
        )
        assert await service.mine(FOREIGN_OWNER) == (
            OutgoingItem("run", "run_foreign", "Foreign", share_count=2, pending_count=0),
        )

    @pytest.mark.asyncio
    async def test_refuses_a_listing_with_a_type_this_deployment_cannot_share(self):
        service, _permissions, identity = make_service(
            unsupported_resource_types=("editor_document",)
        )
        identity.add_share(
            recipient_user_id=RECIPIENT_ID,
            resource_type="editor_document",
            resource_id="editor_owned",
            permission=SharePermission.VIEW,
            granted_by_user_id=OWNER_ID,
        )

        with pytest.raises(ShareBackendUnsupported):
            await service.mine(OWNER)

    def test_requires_a_batch_resolver_for_every_owner_resolver(self):
        identity = MemoryIdentityStore()

        async def owner(tenant_id: str, resource_id: str) -> uuid.UUID | None:
            return None

        async def facts(tenant_id: str, resource_ids: Sequence[str]) -> dict:
            return {}

        async def user_exists(tenant_id: str, user_id: uuid.UUID) -> bool:
            return True

        with pytest.raises(ValueError, match="share_facts_resolvers"):
            ShareService(
                shares=identity,
                permissions=AuthorizationService(
                    members=identity, shares=identity, audit=identity
                ),
                owner_resolvers={"run": owner, "editor_document": owner},
                share_facts_resolvers={"run": facts},
                user_lookup=user_exists,
            )

    def test_run_share_reads_leave_the_event_loop_free(self):
        """The durable run store answers synchronously; the share layer
        must not hold the API event loop while it does."""
        from inqtrix.server.container import _run_share_resolvers

        class SlowRunStore:
            def owner_user_id(self, run_id: str) -> uuid.UUID:
                time.sleep(0.2)
                return OWNER_ID

            def title(self, run_id: str) -> str:
                time.sleep(0.2)
                return "Run"

            def share_facts(self, run_ids: Sequence[str]) -> dict[str, ShareFacts]:
                time.sleep(0.2)
                return {run_id: ShareFacts(OWNER_ID, "Run") for run_id in run_ids}

        owner, title, facts = _run_share_resolvers(SlowRunStore())

        async def longest_pause() -> float:
            loop = asyncio.get_running_loop()
            pauses: list[float] = []
            running = True

            async def tick() -> None:
                last = loop.time()
                while running:
                    await asyncio.sleep(0.01)
                    now = loop.time()
                    pauses.append(now - last)
                    last = now

            ticker = asyncio.create_task(tick())
            await asyncio.sleep(0.03)
            assert await owner("default", "run_1") == OWNER_ID
            assert await title("default", "run_1") == "Run"
            assert await facts("default", ["run_1"]) == {
                "run_1": ShareFacts(OWNER_ID, "Run")
            }
            running = False
            await ticker
            return max(pauses)

        assert asyncio.run(longest_pause()) < 0.1


class TestShareFactsParity:
    """Each batch read answers exactly what the single owner and title reads
    answer, id by id, including resources the share layer must skip."""

    @staticmethod
    async def assert_parity(single, batch, resource_ids: list[str]) -> None:
        facts = await batch(resource_ids)
        for resource_id in resource_ids:
            owner, title = await single(resource_id)
            found = facts.get(resource_id)
            assert (
                (found.owner_user_id, found.title) if found else (None, None)
            ) == (owner, title), resource_id
        assert set(facts) <= set(resource_ids)

    @pytest.mark.asyncio
    async def test_runs(self):
        from inqtrix.server.runs import RunStore

        store = RunStore(
            max_concurrent=2,
            max_queue_size=8,
            completed_ttl_seconds=60,
            event_buffer_size=64,
        )
        owned = store.import_completed_run(
            source_run_id="src-owned",
            question="Owned question?",
            stack_name="stack",
            result={"answer": "x"},
            created_by_user_id=OWNER_ID,
        )["run_id"]
        legacy = store.import_completed_run(
            source_run_id="src-legacy",
            question="Legacy question?",
            stack_name="stack",
            result={"answer": "x"},
        )["run_id"]

        async def single(run_id):
            return store.owner_user_id(run_id), store.title(run_id)

        async def batch(run_ids):
            return store.share_facts(run_ids)

        await self.assert_parity(single, batch, [owned, legacy, "run_missing"])
        assert store.share_facts([owned])[owned] == ShareFacts(
            OWNER_ID, "Owned question?"
        )

    @pytest.mark.asyncio
    async def test_prompt_templates_and_skills(self):
        from inqtrix.content.prompt_templates import (
            MemoryPromptTemplateRepository,
            PromptTemplateRecord,
        )
        from inqtrix.content.skills import MemorySkillRepository, SkillRecord
        from inqtrix.services.prompt_template_service import PromptTemplateService
        from inqtrix.services.skill_service import SkillService

        identity = MemoryIdentityStore()
        authorization = AuthorizationService(
            members=identity, shares=identity, audit=identity
        )
        templates = PromptTemplateService(
            repository=MemoryPromptTemplateRepository(),
            authorization=authorization,
            durable=False,
        )
        skills = SkillService(
            repository=MemorySkillRepository(),
            authorization=authorization,
            durable=False,
        )
        now = time.time()
        for template_id, tenant_id, owner in (
            ("pt_owned", "default", OWNER_ID),
            ("pt_ownerless", "default", None),
            ("pt_foreign", "other-tenant", OWNER_ID),
        ):
            await templates._repository.create(
                PromptTemplateRecord(
                    id=template_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner,
                    title=f"Title {template_id}",
                    label=template_id.replace("_", "-"),
                    category="instruction",
                    content_markdown="Body",
                    created_at=now,
                    updated_at=now,
                )
            )
        for skill_id, tenant_id, owner in (
            ("sk_owned", "default", OWNER_ID),
            ("sk_foreign", "other-tenant", OWNER_ID),
        ):
            await skills._repository.create(
                SkillRecord(
                    id=skill_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner,
                    label=skill_id.replace("_", "-"),
                    title=f"Title {skill_id}",
                    instructions_markdown="Body",
                    created_at=now,
                    updated_at=now,
                )
            )

        async def single_template(template_id):
            return (
                await templates.owner_user_id("default", template_id),
                await templates.title("default", template_id),
            )

        async def batch_templates(template_ids):
            return await templates.share_facts("default", template_ids)

        async def single_skill(skill_id):
            return (
                await skills.owner_user_id("default", skill_id),
                await skills.title("default", skill_id),
            )

        async def batch_skills(skill_ids):
            return await skills.share_facts("default", skill_ids)

        await self.assert_parity(
            single_template,
            batch_templates,
            ["pt_owned", "pt_ownerless", "pt_foreign", "pt_missing"],
        )
        await self.assert_parity(
            single_skill, batch_skills, ["sk_owned", "sk_foreign", "sk_missing"]
        )
        assert (await batch_templates(["pt_owned"]))["pt_owned"] == ShareFacts(
            OWNER_ID, "Title pt_owned"
        )

    @pytest.mark.asyncio
    async def test_knowledge_collections(self):
        from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
        from inqtrix.knowledge.stores.ports import CollectionNotFound

        store = MemoryKnowledgeStore()
        owned = await store.create_collection(
            name="Owned collection",
            embedding_model="model",
            embedding_dim=4,
            created_by_user_id=OWNER_ID,
        )
        legacy = await store.create_collection(
            name="Legacy collection", embedding_model="model", embedding_dim=4
        )

        async def single(collection_id):
            try:
                collection = await store.get_collection(collection_id)
            except CollectionNotFound:
                return None, None
            return collection.created_by_user_id, collection.name

        await self.assert_parity(
            single,
            store.collection_share_facts,
            [owned.id, legacy.id, "kc_missing"],
        )

    @pytest.mark.asyncio
    async def test_editor_documents(self):
        from inqtrix.project.editor_memory import MemoryEditorStore
        from inqtrix.project.editor_ports import EditorDocument
        from inqtrix.services.editor_persistence_service import (
            EditorPersistenceService,
        )

        store = MemoryEditorStore()
        service = EditorPersistenceService(store=store)
        for document in (
            EditorDocument(
                id="ed_shared",
                title="Shared",
                created_by_user_id=OWNER_ID,
                content_mode="collaboration",
            ),
            EditorDocument(
                id="ed_markdown", title="Markdown", created_by_user_id=OWNER_ID
            ),
            EditorDocument(
                id="ed_deleted",
                title="Deleted",
                created_by_user_id=OWNER_ID,
                content_mode="collaboration",
                deleted_at=5.0,
            ),
            EditorDocument(
                id="ed_foreign",
                title="Foreign",
                created_by_user_id=OWNER_ID,
                content_mode="collaboration",
                tenant_id="other-tenant",
            ),
        ):
            store._documents[document.id] = document

        async def single(document_id):
            return (
                await service.share_owner_user_id("default", document_id),
                await service.share_title("default", document_id),
            )

        async def batch(document_ids):
            return await service.share_facts("default", document_ids)

        await self.assert_parity(
            single,
            batch,
            ["ed_shared", "ed_markdown", "ed_deleted", "ed_foreign", "ed_missing"],
        )
        assert await batch(["ed_shared", "ed_markdown"]) == {
            "ed_shared": ShareFacts(OWNER_ID, "Shared")
        }


class TestAuditOutcomeInvariant:
    """A denial must never be recorded as a success."""

    def test_denied_action_forces_the_denied_outcome(self):
        """Derived at construction so no writer can get it wrong.

        Both denial writers built ``AuditEntry`` directly and inherited the
        ``success`` default, so an auditor filtering the panel by outcome saw
        one denial instead of every denial.
        """
        entry = AuditEntry(
            tenant_id="default",
            actor_user_id=OWNER_ID,
            action="authz.denied",
            resource_type="run",
            resource_id="run_owned",
        )

        assert entry.outcome == "denied"

    def test_an_explicit_outcome_on_a_non_denial_is_untouched(self):
        entry = AuditEntry(
            tenant_id="default",
            actor_user_id=OWNER_ID,
            action="share.granted",
            resource_type="run",
            resource_id="run_owned",
        )

        assert entry.outcome == "success"
