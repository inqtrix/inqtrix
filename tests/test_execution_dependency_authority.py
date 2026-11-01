"""What a safepoint re-checks for the actor who continues a run or a reindex job."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import inqtrix
from inqtrix.auth.directory import MemoryUserDirectory, MirroredUser
from inqtrix.auth.principal import Principal
from inqtrix.core.results import RunRequest, SourcePolicy
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.services.execution_dependency_authority import (
    CollectionEditAuthorizer,
    ExecutionDependencyAuthorizer,
)
from inqtrix.storage.auth_postgres import PostgresUserDirectory

ACTOR = uuid.UUID("55555555-5555-4555-8555-555555555555")


class _Authorization:
    async def resolve_user_context(self, principal):
        return SimpleNamespace(principal=principal)


class _Users:
    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled

    async def find_by_user_id(self, *, tenant_id, user_id):
        return SimpleNamespace(
            disabled_at=1.0 if self.disabled else None,
            erasure_operation_id=None,
        )


class _KnowledgeWithoutTheOwnersCollection:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def assert_collections_visible(self, collection_ids, *, visible_to):
        self.asked.append(list(collection_ids))
        raise LookupError("not visible to this actor")


def _authorizer(*, knowledge=None, users=None, skills=None):
    return ExecutionDependencyAuthorizer(
        authorization=_Authorization(),
        knowledge_service=knowledge,
        skill_service=skills,
        user_lookup=users or _Users(),
    )


def _request(knowledge: str, **fields) -> RunRequest:
    return RunRequest(
        mode="agent_kernel",
        question="q",
        knowledge_filters={"collection_ids": ["kc_of_the_owner"]},
        source_policy=SourcePolicy(knowledge=knowledge),
        **fields,
    )


def _actor() -> Principal:
    return Principal(
        user_id=ACTOR,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )


def test_a_run_without_knowledge_does_not_need_the_collections_it_cannot_reach() -> None:
    """An editor continues a shared run although the owner has a private collection."""
    knowledge = _KnowledgeWithoutTheOwnersCollection()

    _authorizer(knowledge=knowledge).check(_request("disabled"), _actor())

    assert knowledge.asked == []


def test_a_run_with_knowledge_still_ends_when_the_actor_lost_a_collection() -> None:
    """It must not go on with a silently smaller evidence set."""
    knowledge = _KnowledgeWithoutTheOwnersCollection()

    with pytest.raises(AuthorizationRevoked, match="collection was revoked"):
        _authorizer(knowledge=knowledge).check(_request("available"), _actor())

    assert knowledge.asked == [["kc_of_the_owner"]]


def test_a_run_without_knowledge_still_needs_an_active_actor() -> None:
    with pytest.raises(AuthorizationRevoked, match="missing or disabled"):
        _authorizer(users=_Users(disabled=True)).check(
            _request("disabled"), _actor()
        )


def test_a_run_without_knowledge_still_needs_its_pinned_skills() -> None:
    with pytest.raises(AuthorizationRevoked, match="skills cannot be authorized"):
        _authorizer().check(
            _request("disabled", skill_ids=("sk_pinned",)), _actor()
        )


class _KnowledgeWithEditAccess:
    """A collection the requester may still edit, recording who asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

        async def get_collection(collection_id):
            self.asked.append(collection_id)
            return SimpleNamespace(id=collection_id)

        self.knowledge = SimpleNamespace(
            store=SimpleNamespace(get_collection=get_collection)
        )

    async def collection_access(self, collection, visible_to, *, minimum):
        return None


def _directory(**state) -> MemoryUserDirectory:
    """The real directory with the actor in the given account state."""
    users = MemoryUserDirectory()
    users.insert_user(
        MirroredUser(
            user_id=ACTOR,
            issuer="http://idp.example",
            subject="actor",
            email="actor@example.com",
            email_verified=True,
            display_name="Actor",
            **state,
        )
    )
    return users


def _reindex_authorizer(knowledge, users) -> CollectionEditAuthorizer:
    return CollectionEditAuthorizer(
        authorization=_Authorization(),
        knowledge_service=knowledge,
        user_lookup=users,
    )


def test_a_run_stops_at_the_safepoint_once_its_actor_is_being_erased() -> None:
    """Like a disable, although the erasure lifts ``disabled_at`` for its own work."""
    users = _directory(erasure_operation_id="del_of_this_account")

    with pytest.raises(AuthorizationRevoked, match="actor is missing or disabled"):
        _authorizer(users=users).check(_request("disabled"), _actor())


def test_a_run_stops_on_the_erasure_mark_of_the_stored_account() -> None:
    """The mark reaches the safepoint through the row mapping of the Postgres directory."""
    row = SimpleNamespace(
        id=ACTOR,
        tenant_id="default",
        issuer="http://idp.example",
        subject="actor",
        email="actor@example.com",
        email_verified=True,
        display_name=None,
        disabled_at=None,
        instance_role="user",
        last_login_at=None,
        default_workspace_id=None,
        erasure_operation_id="del_of_this_account",
        disabled_source=None,
        scim_user_name=None,
        scim_name=None,
        scim_deleted_at=None,
        created_at=None,
    )

    class StoredAccount:
        async def find_by_user_id(self, *, tenant_id, user_id):
            return PostgresUserDirectory._to_user(row, MirroredUser)

    with pytest.raises(AuthorizationRevoked, match="actor is missing or disabled"):
        _authorizer(users=StoredAccount()).check(_request("disabled"), _actor())


def test_a_reindex_job_stops_at_the_safepoint_once_its_requester_is_being_erased() -> None:
    knowledge = _KnowledgeWithEditAccess()
    users = _directory(erasure_operation_id="del_of_this_account")

    with pytest.raises(AuthorizationRevoked, match="requester is missing or disabled"):
        _reindex_authorizer(knowledge, users).check("kc_1", _actor())

    assert knowledge.asked == []


def test_a_reindex_job_stops_at_the_safepoint_once_its_requester_is_disabled() -> None:
    knowledge = _KnowledgeWithEditAccess()

    with pytest.raises(AuthorizationRevoked, match="requester is missing or disabled"):
        _reindex_authorizer(knowledge, _directory(disabled_at=1.0)).check("kc_1", _actor())

    assert knowledge.asked == []


def test_an_active_account_passes_both_safepoints() -> None:
    knowledge = _KnowledgeWithEditAccess()

    _authorizer(users=_directory()).check(_request("disabled"), _actor())
    _reindex_authorizer(knowledge, _directory()).check("kc_1", _actor())

    assert knowledge.asked == ["kc_1"]


def test_the_deletion_machine_never_passes_these_safepoints() -> None:
    """The erasure acts as the account's owner while the account counts as gone here.

    That only holds while the machine runs no run and no reindex job: the two
    safepoints exist on those paths alone. A deletion module that starts using
    one of them has to answer how an erasure then finishes.
    """
    package = Path(inqtrix.__file__).parent
    deletion_path = (
        "services/user_erasure_executor.py",
        "services/user_erasure_aggregates.py",
        "services/asset_deletion_service.py",
        "runs/deletion_execution.py",
        "runs/deletion_postgres.py",
    )
    execution_modules = {
        "inqtrix.services.execution_dependency_authority",
        "inqtrix.services.run_service",
        "inqtrix.services.indexing_service",
        "inqtrix.worker.loop",
        "inqtrix.worker.indexing_loop",
    }

    for relative in deletion_path:
        tree = ast.parse((package / relative).read_text(encoding="utf-8"))
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not imported & execution_modules, relative
