"""A signed-in write without a namespace lands in the writer's project.

The interface lists every project resource with the signed-in person's
canonical project namespace (``users.default_workspace_id``) in the
namespace header, and the stores compare it by strict equality. A script
that writes with a personal access token and leaves the header out must
therefore store its row in that same namespace, claiming it once through
the user directory when the person has none yet; otherwise the row exists
but never appears in the person's interface. A namespace the caller names
still wins, a principal without a user keeps writing without one, and
reads without the header stay unscoped.

One app over the memory backend with the real routers and a user
directory. The test provider resolves ``Authorization: Bearer <user id>``
like a personal access token and a session cookie like a browser session;
the browser's requests always carry the canonical namespace header.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.auth.principal import (
    ANONYMOUS_PRINCIPAL,
    STATIC_PRINCIPAL,
    Principal,
    UserContext,
)
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.project.editor_ports import DocumentNotFound, EditorDocument
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import AppContainer, build_container
from inqtrix.server.routers import (
    agent_runs,
    agent_sessions,
    asset_records,
    chat_history,
    editor_persistence,
    files,
    indexing,
    knowledge,
    knowledge_sessions,
    runs,
    vector_indexes,
)
from inqtrix.services.request_parsing import (
    WORKSPACE_ID_HEADER,
    WORKSPACE_ID_PATTERN,
    workspace_id_for_write,
)
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubLLM, StubSearch, minimal_agent_result
from tests.test_indexing_routes import _PreparedSourceParser
from tests.test_knowledge_routes import KnowledgeStubLLM, StubEmbeddings, _StubSearch
from tests.test_runs_sharing import OidcHeaderProvider

U = uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111")
U2 = uuid.UUID("bbbbbbbb-2222-4222-8222-222222222222")
N = uuid.UUID("cccccccc-3333-4333-8333-333333333333")
U_NAMESPACE = "ws_u_canonical_01"
U2_NAMESPACE = "ws_u2_canonical_02"
OTHER_NAMESPACE = "ws_other_0001"
FOREIGN_BROWSER_NAMESPACE = "ws_foreign_browser_01"

SESSION_COOKIE = "inqtrix_test_session"
STATIC_KEY_HEADER = "X-Test-Static-Key"
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


class TokenOrSessionProvider(OidcHeaderProvider):
    """Cookie-session mode with personal access tokens, minus verification.

    ``Authorization: Bearer <user id>`` resolves like a personal access token
    (kind ``pat``), the session cookie like a browser session (kind
    ``oidc_session``), :data:`STATIC_KEY_HEADER` like the static API key and
    a request without credentials to the anonymous principal. The inherited
    ``users`` property exposes the directory as the real providers do.
    """

    def resolve_principal(self, request: Request) -> Principal:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            return Principal(
                user_id=uuid.UUID(authorization.removeprefix("Bearer ")),
                kind="pat",
                tenant_id="default",
                role="member",
                pat_id="pat_namespace_test",
            )
        session_user = request.cookies.get(SESSION_COOKIE)
        if session_user:
            return Principal(
                user_id=uuid.UUID(session_user),
                kind="oidc_session",
                tenant_id="default",
                role="member",
                session_id="sess_namespace_test",
            )
        if request.headers.get(STATIC_KEY_HEADER):
            return STATIC_PRINCIPAL
        return ANONYMOUS_PRINCIPAL


@dataclass
class World:
    app: FastAPI
    client: TestClient
    container: AppContainer
    users: MemoryUserDirectory


def signed_up_users() -> MemoryUserDirectory:
    """U and U2 have a canonical namespace; N never opened the interface."""
    users = MemoryUserDirectory()

    async def sign_up() -> None:
        for user_id, subject in ((U, "user-u"), (U2, "user-u2"), (N, "user-n")):
            await users.record_login(
                tenant_id="default",
                issuer="http://idp.example",
                subject=subject,
                email=f"{subject}@example.com",
                email_verified=True,
                display_name=subject,
                canonical_user_id=user_id,
            )
        # The first session start adopts the browser's namespace.
        for user_id, namespace in ((U, U_NAMESPACE), (U2, U2_NAMESPACE)):
            adopted = await users.resolve_default_workspace(
                tenant_id="default", user_id=user_id, candidate=namespace
            )
            assert adopted == namespace

    asyncio.run(sign_up())
    return users


def build_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    users: MemoryUserDirectory | None = None,
) -> World:
    monkeypatch.setattr(
        "inqtrix.research.web_research.run_web_graph",
        lambda *args, **kwargs: minimal_agent_result(),
    )
    identity = MemoryIdentityStore()
    users = users if users is not None else signed_up_users()
    container = build_container(
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            storage=StorageSettings(
                backend="memory",
                database_url="",
                object_store_backend="local",
                object_store_path=str(tmp_path / "blobs"),
                document_parser="none",
            )
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=TokenOrSessionProvider(users),
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
    )
    app = FastAPI()
    for router_module in (
        agent_runs,
        agent_sessions,
        asset_records,
        chat_history,
        editor_persistence,
        files,
        knowledge_sessions,
        runs,
        vector_indexes,
    ):
        app.include_router(router_module.build_router(container))
    return World(app=app, client=TestClient(app), container=container, users=users)


def build_knowledge_world(tmp_path: Path) -> World:
    """The same people and credentials with a knowledge store and server-side
    file preparation, for the knowledge ask and the asset revision."""
    identity = MemoryIdentityStore()
    users = signed_up_users()
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=_StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(
                backend="memory",
                database_url="",
                object_store_backend="local",
                object_store_path=str(tmp_path / "blobs"),
            ),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=TokenOrSessionProvider(users),
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=MemoryKnowledgeStore(),
            default_top_k=4,
        ),
        document_parser=_PreparedSourceParser(),
    )
    app = FastAPI()
    for router_module in (
        asset_records,
        files,
        indexing,
        knowledge,
        knowledge_sessions,
        runs,
    ):
        app.include_router(router_module.build_router(container))
    return World(app=app, client=TestClient(app), container=container, users=users)


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    built = build_world(tmp_path, monkeypatch)
    with built.client:
        yield built


@pytest.fixture()
def knowledge_world(tmp_path: Path) -> Iterator[World]:
    built = build_knowledge_world(tmp_path)
    with built.client:
        yield built


# ------------------------------------------------------------------ #
# Requests
# ------------------------------------------------------------------ #


def token(user_id: uuid.UUID, namespace: str | None = None) -> dict[str, str]:
    """A script's request: a personal access token, a namespace only if named."""
    headers = {"Authorization": f"Bearer {user_id}"}
    if namespace is not None:
        headers[WORKSPACE_ID_HEADER] = namespace
    return headers


def session(user_id: uuid.UUID, namespace: str) -> dict[str, str]:
    """The browser's request: the session cookie and the canonical namespace."""
    return {
        "Cookie": f"{SESSION_COOKIE}={user_id}",
        WORKSPACE_ID_HEADER: namespace,
    }


def send(
    client: TestClient, method: str, url: str, *, expect: int, **kwargs: Any
) -> httpx.Response:
    """One request whose status must be *expect*; an escaped error fails it."""
    try:
        response = client.request(method, url, **kwargs)
    except Exception as exc:  # an unmapped error escaped the route
        pytest.fail(
            f"{method} {url} raised {type(exc).__name__}({exc}); "
            f"expected HTTP {expect}"
        )
    assert response.status_code == expect, (
        f"{method} {url}: HTTP {response.status_code} {response.text}"
    )
    return response


def listed(
    client: TestClient, url: str, headers: dict[str, str], **params: Any
) -> list[dict[str, Any]]:
    return send(
        client, "GET", url, expect=200, headers=headers, params=params or None
    ).json()["data"]


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in rows]


def stamped(created_at: float, **fields: Any) -> dict[str, Any]:
    return {"created_at": created_at, "updated_at": created_at, **fields}


def document_body(created_at: float, *, folder_id: str | None = None) -> dict[str, Any]:
    return stamped(
        created_at,
        title="Notes",
        content_markdown="# Notes\n\nWritten by a script.",
        source="blank",
        revision=1,
        folder_id=folder_id,
    )


def asset_body(section_id: str) -> dict[str, Any]:
    return stamped(
        3.0,
        section_id=section_id,
        group_id=None,
        title="Policy",
        label="policy",
        file_name="policy.txt",
        mime_type="text/plain",
        origin="library",
        page_count=None,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=6,
        server_file_id=None,
        parser_id="client",
        extracted_text="policy",
    )


def reservation_body(section_id: str) -> dict[str, Any]:
    return stamped(
        4.0,
        section_id=section_id,
        group_id=None,
        title="contract.pdf",
        label="contract",
        file_name="contract.pdf",
        mime_type="application/pdf",
        origin="library",
        page_count=None,
        parse_status="parsed",
        parse_warning=None,
        text_truncated=False,
        size_bytes=128,
        parser_id=None,
    )


def stored_document(world: World, document_id: str) -> EditorDocument:
    """The stored row, read past every list filter."""
    return asyncio.run(
        world.container.editor_persistence_service.store.get_document(document_id)
    )


def canonical_namespace(users: MemoryUserDirectory, user_id: uuid.UUID) -> str | None:
    mirrored = asyncio.run(users.find_by_user_id(tenant_id="default", user_id=user_id))
    assert mirrored is not None
    return mirrored.default_workspace_id


def start_run(client: TestClient, headers: dict[str, str], question: str) -> str:
    response = send(
        client, "POST", "/v1/runs", expect=202, json={"question": question},
        headers=headers,
    )
    return response.json()["run_id"]


def wait_until_finished(world: World, run_id: str, owner: uuid.UUID) -> None:
    """Wait on the store itself, so no read under test decides the wait."""
    visible_to = UserContext(
        principal=Principal(
            user_id=owner, kind="pat", tenant_id="default", role="member"
        )
    )
    deadline = time.monotonic() + 10.0
    status = None
    while time.monotonic() < deadline:
        status = world.container.run_store.get(run_id, visible_to=visible_to)[
            "status"
        ]
        if status in TERMINAL_RUN_STATUSES:
            return
        time.sleep(0.02)
    pytest.fail(f"run {run_id} did not finish: {status}")


def record_directory_calls(
    users: MemoryUserDirectory, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Record every public directory method called from now on."""
    calls: list[str] = []

    def recording(name: str, bound: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(bound):
            async def recorded_async(*args: Any, **kwargs: Any) -> Any:
                calls.append(name)
                return await bound(*args, **kwargs)

            return recorded_async

        def recorded(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return bound(*args, **kwargs)

        return recorded

    for name, _function in inspect.getmembers(type(users), inspect.isfunction):
        if not name.startswith("_"):
            monkeypatch.setattr(users, name, recording(name, getattr(users, name)))
    return calls


# ------------------------------------------------------------------ #
# One contract per family the interface lists by namespace
# ------------------------------------------------------------------ #


def test_editor_document_from_a_token_without_header_is_in_the_ui_list(
    world: World,
) -> None:
    """Does a document a script writes without a namespace appear in the
    editor list the interface loads, for its owner only?"""
    client = world.client
    send(client, "PUT", "/v1/editor/documents/ed_api", expect=200,
         json=document_body(200.0), headers=token(U))
    send(client, "PUT", "/v1/editor/documents/ed_ui", expect=200,
         json=document_body(100.0), headers=session(U, U_NAMESPACE))
    send(client, "PUT", "/v1/editor/documents/ed_u2", expect=200,
         json=document_body(300.0), headers=token(U2))

    ui_rows = listed(client, "/v1/editor/documents", session(U, U_NAMESPACE),
                     scope="all", limit=200)
    assert ids(ui_rows) == ["ed_api", "ed_ui"]
    assert ui_rows[0]["content_mode"] == "markdown"
    assert ui_rows[0]["access"]["mode"] == "owner"
    assert stored_document(world, "ed_api").workspace_id == U_NAMESPACE
    assert ids(listed(client, "/v1/editor/documents", session(U2, U2_NAMESPACE),
                      scope="all", limit=200)) == ["ed_u2"]


def test_editor_folder_from_a_token_without_header_is_in_the_ui_and_ui_folders_take_script_documents(
    world: World,
) -> None:
    """Does a script's folder appear in the interface, and can a script file
    a document into a folder the interface created?"""
    client = world.client
    send(client, "PUT", "/v1/editor/folders/fold_api", expect=200,
         json=stamped(1.0, title="Script folder"), headers=token(U))
    assert ids(listed(client, "/v1/editor/folders", session(U, U_NAMESPACE))) == [
        "fold_api"
    ]
    assert ids(listed(client, "/v1/editor/folders", session(U2, U2_NAMESPACE))) == []

    send(client, "PUT", "/v1/editor/folders/fold_ui", expect=200,
         json=stamped(2.0, title="UI folder"), headers=session(U, U_NAMESPACE))
    send(client, "PUT", "/v1/editor/documents/ed_filed", expect=200,
         json=document_body(3.0, folder_id="fold_ui"), headers=token(U))
    ui_rows = listed(client, "/v1/editor/documents", session(U, U_NAMESPACE),
                     scope="all", limit=200)
    assert [(row["id"], row["folder_id"]) for row in ui_rows] == [
        ("ed_filed", "fold_ui")
    ]


def test_chat_thread_and_group_from_a_token_without_header_are_in_the_ui_lists(
    world: World,
) -> None:
    """Do a chat thread and a thread group a script writes without a
    namespace appear in the interface's chat history, for their owner only?"""
    client = world.client
    send(client, "PUT", "/v1/chat/threads/th_api", expect=200,
         json=stamped(1.0, title="Script thread", preview="", source="api",
                      model_selection="", group_id=None),
         headers=token(U))
    assert ids(listed(client, "/v1/chat/threads", session(U, U_NAMESPACE))) == [
        "th_api"
    ]
    assert ids(listed(client, "/v1/chat/threads", session(U2, U2_NAMESPACE))) == []

    send(client, "PUT", "/v1/chat/thread-groups/tg_api", expect=200,
         json=stamped(2.0, title="Script group"), headers=token(U))
    assert ids(listed(client, "/v1/chat/thread-groups", session(U, U_NAMESPACE))) == [
        "tg_api"
    ]
    assert ids(listed(client, "/v1/chat/thread-groups",
                      session(U2, U2_NAMESPACE))) == []


def test_agent_session_and_group_from_a_token_without_header_are_in_the_ui_lists(
    world: World,
) -> None:
    """Do an agent session and a session group a script writes without a
    namespace appear in the interface, for their owner only?"""
    client = world.client
    send(client, "PUT", "/v1/agent-sessions/as_api", expect=200,
         json=stamped(1.0, title="Script session", items_json="[]",
                      group_id=None),
         headers=token(U))
    assert ids(listed(client, "/v1/agent-sessions", session(U, U_NAMESPACE))) == [
        "as_api"
    ]
    assert ids(listed(client, "/v1/agent-sessions", session(U2, U2_NAMESPACE))) == []

    send(client, "PUT", "/v1/agent-session-groups/asg_api", expect=200,
         json=stamped(2.0, title="Script group"), headers=token(U))
    assert ids(listed(client, "/v1/agent-session-groups",
                      session(U, U_NAMESPACE))) == ["asg_api"]
    assert ids(listed(client, "/v1/agent-session-groups",
                      session(U2, U2_NAMESPACE))) == []


def test_knowledge_session_and_group_from_a_token_without_header_are_in_the_ui_lists(
    world: World,
) -> None:
    """Do a knowledge session and a session group a script writes without a
    namespace appear in the interface, for their owner only?"""
    client = world.client
    send(client, "PUT", "/v1/knowledge-sessions/ks_api", expect=200,
         json=stamped(1.0, title="Script session", items_json="[]",
                      group_id=None),
         headers=token(U))
    assert ids(listed(client, "/v1/knowledge-sessions",
                      session(U, U_NAMESPACE))) == ["ks_api"]
    assert ids(listed(client, "/v1/knowledge-sessions",
                      session(U2, U2_NAMESPACE))) == []

    send(client, "PUT", "/v1/knowledge-session-groups/ksg_api", expect=200,
         json=stamped(2.0, title="Script group"), headers=token(U))
    assert ids(listed(client, "/v1/knowledge-session-groups",
                      session(U, U_NAMESPACE))) == ["ksg_api"]
    assert ids(listed(client, "/v1/knowledge-session-groups",
                      session(U2, U2_NAMESPACE))) == []


def test_assets_from_a_token_without_header_share_the_ui_file_library(
    world: World,
) -> None:
    """Does a script work in the same file library as the interface: the same
    default sections, its own sections in the interface's list, and groups,
    reservations and records filed into a section the interface created?"""
    client = world.client
    # The script comes first: a library opened later must find its sections.
    script_sections = ids(send(client, "PUT", "/v1/assets/default-sections",
                               expect=200, headers=token(U)).json()["data"])
    ui_sections = ids(send(client, "PUT", "/v1/assets/default-sections",
                           expect=200, headers=session(U, U_NAMESPACE)
                           ).json()["data"])
    assert script_sections == ui_sections

    send(client, "PUT", "/v1/assets/sections/fsec_api", expect=200,
         json=stamped(1.0, kind="custom", title="Script section"),
         headers=token(U))
    assert "fsec_api" in ids(listed(client, "/v1/assets/sections",
                                    session(U, U_NAMESPACE)))
    assert ids(listed(client, "/v1/assets/sections", session(U2, U2_NAMESPACE))) == []

    send(client, "PUT", "/v1/assets/sections/fsec_ui", expect=200,
         json=stamped(2.0, kind="custom", title="UI section"),
         headers=session(U, U_NAMESPACE))
    send(client, "PUT", "/v1/assets/groups/fgrp_api", expect=200,
         json=stamped(3.0, section_id="fsec_ui", title="Script group"),
         headers=token(U))
    assert ids(listed(client, "/v1/assets/groups", session(U, U_NAMESPACE))) == [
        "fgrp_api"
    ]
    assert ids(listed(client, "/v1/assets/groups", session(U2, U2_NAMESPACE))) == []

    send(client, "POST", "/v1/assets/fa_reserved/upload-reservation",
         expect=201, json=reservation_body("fsec_ui"), headers=token(U))
    send(client, "PUT", "/v1/assets/fa_api", expect=200,
         json=asset_body("fsec_ui"), headers=token(U))
    assert set(ids(listed(client, "/v1/assets", session(U, U_NAMESPACE)))) == {
        "fa_api",
        "fa_reserved",
    }
    assert ids(listed(client, "/v1/assets", session(U2, U2_NAMESPACE))) == []


def test_vector_index_from_a_token_without_header_is_in_the_ui_list(
    world: World,
) -> None:
    """Does a vector index a script writes without a namespace appear in the
    interface, for its owner only?"""
    client = world.client
    send(client, "PUT", "/v1/vector-indexes/vix_api", expect=200,
         json=stamped(1.0, title="Script index", handle="script-index",
                      model="text-embedding-3-large", dims=3072,
                      status="ready"),
         headers=token(U))
    assert ids(listed(client, "/v1/vector-indexes", session(U, U_NAMESPACE))) == [
        "vix_api"
    ]
    assert ids(listed(client, "/v1/vector-indexes",
                      session(U2, U2_NAMESPACE))) == []


def test_file_uploaded_by_a_token_without_header_is_in_the_ui_file_list(
    world: World,
) -> None:
    """Does a file a script uploads without a namespace appear in the file
    list the interface loads, for its owner only?"""
    client = world.client
    uploaded = send(client, "POST", "/v1/files", expect=201,
                    files={"file": ("notes.txt", b"Uploaded by a script.\n",
                                    "text/plain")},
                    headers=token(U)).json()
    assert ids(listed(client, "/v1/files", session(U, U_NAMESPACE))) == [
        uploaded["id"]
    ]
    assert uploaded["workspace_id"] == U_NAMESPACE
    assert ids(listed(client, "/v1/files", session(U2, U2_NAMESPACE))) == []


def test_run_started_by_a_token_without_header_is_in_the_ui_history_and_unscoped_reads_stay_unscoped(
    world: World,
) -> None:
    """Does a run a script starts or imports without a namespace appear in
    the run history the interface loads, while a read without the header
    still lists every run of the person, whatever its namespace?"""
    client = world.client
    script_run = start_run(client, token(U), "Script run?")
    ui_run = start_run(client, session(U, U_NAMESPACE), "Interface run?")
    other_run = start_run(client, token(U, OTHER_NAMESPACE), "Named namespace run?")
    foreign_run = start_run(client, token(U2), "Another person's run?")
    for run_id, owner in (
        (script_run, U), (ui_run, U), (other_run, U), (foreign_run, U2),
    ):
        wait_until_finished(world, run_id, owner)
    imported_run = send(client, "POST", "/v1/runs/import", expect=200,
                        json={"source_run_id": "local-report-1",
                              "question": "Imported report?",
                              "status": "completed",
                              "result": {"answer": "Imported answer."}},
                        headers=token(U)).json()["run_id"]

    ui_history = {
        row["run_id"] for row in listed(client, "/v1/runs", session(U, U_NAMESPACE))
    }
    assert ui_history == {script_run, ui_run, imported_run}
    summary = send(client, "GET", f"/v1/runs/{script_run}", expect=200,
                   headers=token(U)).json()
    assert summary["workspace_id"] == U_NAMESPACE
    script_view = {row["run_id"] for row in listed(client, "/v1/runs", token(U))}
    assert script_view == {script_run, ui_run, other_run, imported_run}
    foreign_history = {
        row["run_id"]
        for row in listed(client, "/v1/runs", session(U2, U2_NAMESPACE))
    }
    assert foreign_history == {foreign_run}


# ------------------------------------------------------------------ #
# Writes that create a row elsewhere or compare the namespace exactly
# ------------------------------------------------------------------ #


def test_artifact_exported_by_a_token_without_header_is_in_the_ui_editor_list(
    world: World,
) -> None:
    """Does an editor document a script exports from an agent artifact
    appear in the editor list the interface loads?"""
    summary = world.container.run_store.submit(
        mode="research",
        workflow_contract=1,
        question="Agent task",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "done"}),
        kind="agent",
        created_by_user_id=U,
        created_by_tenant_id="default",
        workspace_id=U_NAMESPACE,
    )
    run_id = summary["run_id"]
    wait_until_finished(world, run_id, U)
    asyncio.run(world.container.agent_control_service.store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-export",
        title="Memo",
        status="ready",
        content_markdown="# Memo",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_memo",
    ))
    exported = send(world.client, "POST",
                    f"/v1/runs/{run_id}/artifacts/art_memo/export", expect=201,
                    json={"target": "editor_document"}, headers=token(U)).json()
    assert ids(listed(world.client, "/v1/editor/documents",
                      session(U, U_NAMESPACE), scope="all", limit=200)) == [
        exported["id"]
    ]


def test_upload_operations_are_listed_and_retried_by_a_token_without_header(
    world: World,
) -> None:
    """Can a script without a namespace find and retry the uploads of its
    owner's interface, which the stores compare by exact namespace?"""
    client = world.client
    ui = session(U, U_NAMESPACE)
    send(client, "PUT", "/v1/assets/sections/fsec_ui", expect=200,
         json=stamped(1.0, kind="custom", title="UI section"), headers=ui)
    operation_id = send(client, "POST", "/v1/files", expect=201,
                        files={"file": ("notes.txt", b"Uploaded in the UI.\n",
                                        "text/plain")},
                        data={"asset_id": "fa_ui", "section_id": "fsec_ui",
                              "title": "Notes", "label": "notes",
                              "origin": "library", "created_at": "1.0",
                              "updated_at": "1.0"},
                        headers=ui).json()["upload_operation"]["operation_id"]

    operations = listed(client, "/v1/uploads", token(U))
    assert [row["operation_id"] for row in operations] == [operation_id]
    assert listed(client, "/v1/uploads", session(U2, U2_NAMESPACE)) == []
    # Found, but a finished upload has nothing to retry.
    retried = send(client, "POST", f"/v1/uploads/{operation_id}/retry",
                   expect=409, headers=token(U))
    assert retried.json()["error"]["type"] == "upload_operation_conflict"


def test_vector_index_the_ui_deleted_is_not_recreated_by_a_token_without_header(
    world: World,
) -> None:
    """Does the deletion guard of a vector index check the same namespace the
    script's write lands in, so a deleted index is not brought back?"""
    client = world.client
    ui = session(U, U_NAMESPACE)
    body = stamped(1.0, title="UI index", handle="ui-index",
                   model="text-embedding-3-large", dims=3072, status="ready")
    send(client, "PUT", "/v1/vector-indexes/vix_ui", expect=200, json=body,
         headers=ui)
    send(client, "DELETE", "/v1/vector-indexes/vix_ui", expect=202, headers=ui)
    refused = send(client, "PUT", "/v1/vector-indexes/vix_ui", expect=409,
                   json=body, headers=token(U))
    assert refused.json()["error"]["type"] == "deletion_in_progress"


def test_knowledge_ask_from_a_token_without_header_shares_its_namespace_with_its_session(
    knowledge_world: World,
) -> None:
    """Does a knowledge ask a script starts in a new session show up, with
    that session, in the interface of its owner?"""
    client = knowledge_world.client
    collection_id = send(client, "POST", "/v1/knowledge/collections",
                         expect=201, json={"name": "Contracts"},
                         headers=token(U)).json()["id"]
    send(client, "POST", f"/v1/knowledge/collections/{collection_id}/documents",
         expect=201,
         json={"title": "Framework contract",
               "text": "Die Haftung ist auf den Auftragswert begrenzt."},
         headers=token(U))
    run_id = send(client, "POST", "/v1/runs", expect=202,
                  json={"question": "Wie ist die Haftung geregelt?",
                        "mode": "knowledge",
                        "knowledge_filters": {"collection_ids": [collection_id]},
                        "session_id": "ks_script"},
                  headers=token(U)).json()["run_id"]
    wait_until_finished(knowledge_world, run_id, U)

    ui = session(U, U_NAMESPACE)
    assert ids(listed(client, "/v1/knowledge-sessions", ui)) == ["ks_script"]
    assert {row["run_id"] for row in listed(client, "/v1/runs", ui)} == {run_id}
    assert listed(client, "/v1/knowledge-sessions", session(U2, U2_NAMESPACE)) == []


def test_document_revision_from_a_token_without_header_reads_an_asset_of_the_ui(
    knowledge_world: World,
) -> None:
    """Can a script without a namespace index a file its owner uploaded in
    the interface, whose namespace the route compares exactly?"""
    client = knowledge_world.client
    ui = session(U, U_NAMESPACE)
    send(client, "PUT", "/v1/assets/sections/fsec_ui", expect=200,
         json=stamped(1.0, kind="custom", title="Sources"), headers=ui)
    uploaded = send(client, "POST", "/v1/files", expect=202,
                    files={"file": ("source.txt", b"Prepared on the server.",
                                    "text/plain")},
                    data={"asset_id": "fa_ui", "section_id": "fsec_ui",
                          "title": "Source", "label": "source",
                          "origin": "library", "created_at": "1.0",
                          "updated_at": "1.0"},
                    headers=ui).json()
    uploads = knowledge_world.container.upload_operation_service
    claimed = uploads.operations.claim_for_execution(
        uploaded["upload_operation"]["operation_id"], "default",
        allow_takeover=False,
    )
    assert claimed is not None
    uploads.execute_claimed(claimed)

    collection_id = send(client, "POST", "/v1/knowledge/collections",
                         expect=201, json={"name": "Sources"},
                         headers=token(U)).json()["id"]
    started = send(client, "POST",
                   f"/v1/knowledge/collections/{collection_id}/document-revisions",
                   expect=202, json={"asset_id": "fa_ui", "title": "Source"},
                   headers=token(U)).json()
    assert started["operation_kind"] == "document_revision"


# ------------------------------------------------------------------ #
# Claim, explicit namespaces and principals without a user
# ------------------------------------------------------------------ #


def test_two_concurrent_first_writes_claim_one_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a person without a namespace writes twice at once, do both rows
    land in the one namespace the directory then holds, and does a later
    first session start in a browser adopt it instead of its own id?"""
    users = signed_up_users()
    claim = users.resolve_default_workspace
    candidates: list[str] = []
    both_arrived = asyncio.Event()

    async def claim_when_both_arrived(**kwargs: Any) -> str | None:
        # Hold each claim for N until the other write has reached the
        # directory too, so neither request can see the other's finished claim.
        if kwargs.get("user_id") != N:
            return await claim(**kwargs)
        candidates.append(kwargs["candidate"])
        if len(candidates) >= 2:
            both_arrived.set()
        try:
            await asyncio.wait_for(both_arrived.wait(), timeout=2.0)
        except TimeoutError:
            pass
        return await claim(**kwargs)

    # Installed before the routers exist, whenever they look the directory up.
    monkeypatch.setattr(users, "resolve_default_workspace", claim_when_both_arrived)
    world = build_world(tmp_path, monkeypatch, users=users)

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=world.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first, second = await asyncio.gather(
                client.put("/v1/editor/documents/ed_race_a",
                           json=document_body(1.0), headers=token(N)),
                client.put("/v1/editor/documents/ed_race_b",
                           json=document_body(2.0), headers=token(N)),
            )
            assert (first.status_code, second.status_code) == (200, 200), (
                first.text, second.text,
            )
            mirrored = await users.find_by_user_id(tenant_id="default", user_id=N)
            assert mirrored is not None
            claimed = mirrored.default_workspace_id
            assert claimed is not None, (
                "two first writes without a namespace claimed none for the writer"
            )
            store = world.container.editor_persistence_service.store
            assert (await store.get_document("ed_race_a")).workspace_id == claimed
            assert (await store.get_document("ed_race_b")).workspace_id == claimed
            assert WORKSPACE_ID_PATTERN.fullmatch(claimed)
            listing = await client.get(
                "/v1/editor/documents",
                params={"scope": "all", "limit": 200},
                headers=session(N, claimed),
            )
            assert listing.status_code == 200, listing.text
            assert {row["id"] for row in listing.json()["data"]} == {
                "ed_race_a",
                "ed_race_b",
            }
        later_session_start = await claim(
            tenant_id="default", user_id=N, candidate=FOREIGN_BROWSER_NAMESPACE
        )
        assert later_session_start == claimed
        assert len(candidates) == 2, (
            "both first writes must claim through the directory at once; "
            f"claims seen: {candidates}"
        )

    asyncio.run(scenario())


def test_explicit_header_wins_and_claims_nothing_while_an_empty_one_counts_as_missing(
    world: World,
) -> None:
    """Is a namespace the caller names stored as given without becoming the
    caller's canonical one, while an empty header counts as no namespace?"""
    client = world.client
    send(client, "PUT", "/v1/editor/documents/ed_explicit", expect=200,
         json=document_body(1.0), headers=token(N, OTHER_NAMESPACE))
    assert stored_document(world, "ed_explicit").workspace_id == OTHER_NAMESPACE
    assert canonical_namespace(world.users, N) is None

    send(client, "PUT", "/v1/editor/documents/ed_empty", expect=200,
         json=document_body(2.0), headers=token(N, ""))
    claimed = canonical_namespace(world.users, N)
    assert claimed is not None, "a write with an empty header claimed no namespace"
    assert claimed != OTHER_NAMESPACE
    assert stored_document(world, "ed_empty").workspace_id == claimed
    assert stored_document(world, "ed_explicit").workspace_id == OTHER_NAMESPACE


def test_invalid_header_is_refused_before_anything_is_claimed(world: World) -> None:
    """Is a malformed namespace still refused with 400, leaving neither a
    claim nor a row behind?"""
    response = send(world.client, "PUT", "/v1/editor/documents/ed_invalid",
                    expect=400, json=document_body(1.0),
                    headers=token(N, "not a namespace!"))
    assert "Invalid workspace_id." in response.text
    assert canonical_namespace(world.users, N) is None
    with pytest.raises(DocumentNotFound):
        stored_document(world, "ed_invalid")


@pytest.mark.parametrize(
    "credentials",
    [{}, {STATIC_KEY_HEADER: "static-key"}],
    ids=["anonymous", "static-key"],
)
def test_principal_without_user_writes_without_namespace_and_never_asks_the_directory(
    world: World, monkeypatch: pytest.MonkeyPatch, credentials: dict[str, str]
) -> None:
    """Does a principal without a user (open server, static key) keep
    storing rows without a namespace, without consulting the directory?"""
    calls = record_directory_calls(world.users, monkeypatch)
    send(world.client, "PUT", "/v1/editor/documents/ed_unscoped", expect=200,
         json=document_body(1.0), headers=credentials)
    assert calls == []
    stored = stored_document(world, "ed_unscoped")
    assert (stored.created_by_user_id, stored.workspace_id) == (None, None)


def test_a_user_without_a_directory_writes_without_namespace() -> None:
    """Does a provider without a user directory keep its signed-in writes
    without a namespace instead of failing?"""
    request = Request({
        "type": "http", "method": "PUT", "path": "/", "headers": [],
        "query_string": b"",
    })
    principal = Principal(
        user_id=U, kind="pat", tenant_id="default", role="member"
    )
    assert asyncio.run(workspace_id_for_write(request, principal, None)) is None
