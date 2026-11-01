"""Shared-in skills stay user-activated for the recipient.

A skill another user shared with the caller carries that user's
instructions. The recipient may attach it explicitly, but the
recipient's MODEL never activates it on its own: it is absent from the
kernel's skill catalog and spends none of its budget, and ``load_skill``
refuses it before any of its clarification points reach a model or the
user. Resuming is not activating: an editor who continues a shared run
keeps the skills the run already loaded. The world is the production
composition minus token verification: the oidc test provider over
canonical user headers, the real share and authorization services,
memory backends and the kernel platform.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("deepagents")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.auth.principal import Principal
from inqtrix.server.container import build_container
from inqtrix.server.routers import runs as runs_router
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.server.routers.skills import build_router as build_skills_router
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.agents.test_kernel_skills import (
    SKILL_PAYLOAD,
    ScriptedToolLLM,
    SkillCheckingLLM,
    _text_turn,
    _tool_turn,
)
from tests.test_runs_sharing import (
    OWNER,
    RECIPIENT,
    STRANGER,
    OidcHeaderProvider,
    accept_via_http,
    user_headers,
)

# Inert markers: the shared skill's catalog text and instructions must
# never reach the recipient's model unless the recipient attached it.
SHARED_DESCRIPTION = "Fremde Beschreibung MARKE1"
SHARED_INSTRUCTIONS = "Fremde Anweisung MARKE2"
OWN_DESCRIPTION = "Eigene Beschreibung MARKE3"

# The refusal ``load_skill`` itself writes; a reword must be caught here.
USER_ONLY_REFUSAL = "may only be activated by the user"
# The visible overflow line of the catalog (``_skills_context``).
OVERFLOW_LINE = "- ... and "


def make_world(
    llm: ScriptedToolLLM, *, disclosure_budget: int | None = None
) -> TestClient:
    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()

    async def mirror() -> None:
        for user_id, subject, name in (
            (OWNER, "user-owner", "Olga Owner"),
            (RECIPIENT, "user-recipient", "Rita Recipient"),
            (STRANGER, "user-stranger", "Stefan Stranger"),
        ):
            await users.record_login(
                tenant_id="default",
                issuer="http://idp.example",
                subject=subject,
                email=f"{subject}@example.com",
                email_verified=True,
                display_name=name,
                canonical_user_id=user_id,
            )

    asyncio.run(mirror())
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    platform: dict[str, Any] = {
        "INQTRIX_AGENT_ALLOW_VOLATILE": True,
        "INQTRIX_AGENT_KERNEL_ENABLED": True,
    }
    if disclosure_budget is not None:
        platform["INQTRIX_AGENT_SKILLS_DISCLOSURE_BUDGET_CHARS"] = disclosure_budget
    settings.agent_platform = AgentPlatformSettings(**platform)
    container = build_container(
        providers=SimpleNamespace(llm=llm, search=None),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(2),
        auth_provider=OidcHeaderProvider(users),
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
    )
    app = FastAPI()
    app.include_router(runs_router.build_router(container))
    app.include_router(build_skills_router(container))
    app.include_router(build_shares_router(container))
    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def create_skill(client: TestClient, *, user_id, **overrides) -> dict[str, Any]:
    response = client.post(
        "/v1/skills",
        json={**SKILL_PAYLOAD, **overrides},
        headers=user_headers(user_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def share_with_recipient(
    client: TestClient, skill_id: str, *, permission: str = "view"
) -> None:
    granted = client.post(
        "/v1/shares",
        json={
            "resource_type": "skill_template",
            "resource_id": skill_id,
            "invitees": [
                {"user_id": str(RECIPIENT), "permission": permission}
            ],
        },
        headers=user_headers(OWNER),
    )
    assert granted.status_code == 201, granted.text
    accept_via_http(client, granted.json()["data"][0]["id"])
    # Anchor: the recipient really sees the skill as shared-in, so the
    # assertions below measure the shared case and not an invisible one.
    listed = client.get("/v1/skills", headers=user_headers(RECIPIENT)).json()
    access = {item["id"]: item["access"] for item in listed["data"]}
    assert access[skill_id] == {"mode": "shared", "permission": permission}


def shared_skill(client: TestClient, *, permission: str = "view", **overrides):
    record = create_skill(
        client,
        user_id=OWNER,
        **{
            "label": "fremd-stil",
            "description": SHARED_DESCRIPTION,
            "instructions_markdown": SHARED_INSTRUCTIONS,
            **overrides,
        },
    )
    share_with_recipient(client, record["id"], permission=permission)
    return record


def own_skill(client: TestClient) -> dict[str, Any]:
    return create_skill(
        client,
        user_id=RECIPIENT,
        label="eigen-stil",
        description=OWN_DESCRIPTION,
    )


def submit_as_recipient(client: TestClient, *, skill_ids=None) -> str:
    body: dict[str, Any] = {
        "question": "Entwirf eine E-Mail an das Team.",
        "mode": "agent_kernel",
        "autonomy": "autonomous",
    }
    if skill_ids:
        body["skill_ids"] = skill_ids
    response = client.post(
        "/v1/runs", json=body, headers=user_headers(RECIPIENT)
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def wait_settled(client: TestClient, run_id: str) -> str:
    deadline = time.time() + 10.0
    status = ""
    while time.time() < deadline:
        status = client.get(
            f"/v1/runs/{run_id}", headers=user_headers(RECIPIENT)
        ).json().get("status", "")
        if status in {"completed", "failed", "waiting_for_input"}:
            return status
        time.sleep(0.02)
    pytest.fail(f"run {run_id} never settled (last status {status!r})")


def tool_reply(llm: ScriptedToolLLM, call_id: str) -> str:
    for message in llm.chat_calls[-1]["messages"]:
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            return str(message.get("content") or "")
    raise AssertionError(f"no tool reply for {call_id}")


def model_saw(llm: ScriptedToolLLM, text: str) -> bool:
    return any(
        text in str(message.get("content") or "")
        for call in llm.chat_calls
        for message in call["messages"]
    )


class CountingSkillCheckLLM(SkillCheckingLLM):
    """Skill point check that counts how often a model was asked."""

    def __init__(self, turns: list[Any]) -> None:
        super().__init__(turns)
        self.structured_calls = 0

    def complete_structured(self, prompt: str, **kwargs: Any) -> Any:
        self.structured_calls += 1
        return super().complete_structured(prompt, **kwargs)


@pytest.mark.parametrize("permission", ["view", "edit"])
def test_shared_skill_is_absent_from_the_recipients_catalog(permission):
    llm = ScriptedToolLLM([_text_turn("Fertig.")])
    client = make_world(llm)
    with client:
        shared = shared_skill(client, permission=permission)
        own = own_skill(client)
        assert wait_settled(client, submit_as_recipient(client)) == "completed"

    first_user = llm.chat_calls[0]["messages"][-1]["content"]
    # Positive control: the recipient's own model_allowed skill is listed,
    # marker included, so the marker probes below can see a catalog line.
    assert "/eigen-stil" in first_user
    assert own["id"] in first_user
    assert "MARKE3" in first_user
    # The shared skill never reaches the recipient's catalog, whatever
    # the recipient's grant on it.
    assert "/fremd-stil" not in first_user
    assert shared["id"] not in first_user
    assert not model_saw(llm, "MARKE1")
    assert not model_saw(llm, "MARKE2")


@pytest.mark.parametrize("long_skill", ["shared", "own"])
def test_only_activatable_skills_spend_catalog_budget(long_skill):
    llm = ScriptedToolLLM([_text_turn("Fertig.")])
    client = make_world(llm, disclosure_budget=500)
    with client:
        # A skill whose catalog line alone overflows the budget.
        if long_skill == "shared":
            shared_skill(
                client, description=SHARED_DESCRIPTION + " lang" * 150
            )
        else:
            create_skill(
                client,
                user_id=RECIPIENT,
                label="eigen-lang",
                description="Eigene lange Beschreibung" + " lang" * 150,
            )
        own = own_skill(client)
        assert wait_settled(client, submit_as_recipient(client)) == "completed"

    first_user = llm.chat_calls[0]["messages"][-1]["content"]
    assert own["id"] in first_user
    if long_skill == "shared":
        # Nothing was cut: the shared skill is no candidate at all.
        assert OVERFLOW_LINE not in first_user
    else:
        # Positive control: an own skill too long for the budget is cut
        # visibly, so the probe above sees the overflow line when it exists.
        assert OVERFLOW_LINE + "1 more" in first_user


def test_load_skill_refuses_a_shared_skill_for_the_recipient():
    llm = ScriptedToolLLM(
        [
            _tool_turn("call_shared", "load_skill", {"skill_id": "PENDING"}),
            _tool_turn("call_own", "load_skill", {"skill_id": "PENDING"}),
            _text_turn("Fertig."),
        ]
    )
    client = make_world(llm)
    with client:
        shared = shared_skill(client)
        own = own_skill(client)
        # The model learned the id elsewhere (an earlier turn, a pasted
        # link): the tool itself must refuse it.
        llm._turns[0] = _tool_turn(
            "call_shared", "load_skill", {"skill_id": shared["id"]}
        )
        llm._turns[1] = _tool_turn(
            "call_own", "load_skill", {"skill_id": own["id"]}
        )
        assert wait_settled(client, submit_as_recipient(client)) == "completed"

    shared_reply = tool_reply(llm, "call_shared")
    assert USER_ONLY_REFUSAL in shared_reply
    assert not shared_reply.startswith("[skill_geladen:")
    assert not model_saw(llm, "MARKE2")
    # Positive control: the recipient's own skill still loads.
    assert tool_reply(llm, "call_own").startswith(
        f"[skill_geladen:{own['id']}@"
    )


def test_refused_shared_skill_never_asks_its_clarification_points():
    llm = CountingSkillCheckLLM(
        [
            _tool_turn("call_shared", "load_skill", {"skill_id": "PENDING"}),
            _text_turn("Fertig."),
        ]
    )
    client = make_world(llm)
    with client:
        shared = shared_skill(
            client,
            instructions_markdown=SHARED_INSTRUCTIONS + " fuer {{zielgruppe}}.",
            clarification_points=[
                {
                    "name": "zielgruppe",
                    "question": "Welche Zielgruppe?",
                    "options": [],
                    "required": True,
                    "default_assumption": "",
                }
            ],
        )
        llm._turns[0] = _tool_turn(
            "call_shared", "load_skill", {"skill_id": shared["id"]}
        )
        # Refused before its inputs resolve: no park for the user ...
        assert wait_settled(client, submit_as_recipient(client)) == "completed"

    # ... and no point check reached a model.
    assert llm.structured_calls == 0
    assert USER_ONLY_REFUSAL in tool_reply(llm, "call_shared")


def test_editor_resuming_a_shared_run_rearms_the_skill_it_loaded():
    """An editor who resumes a shared run is the segment's actor.

    The model loaded the owner's own skill in the owner's segment, and the
    owner shared that skill with the editor too. Re-arming restores the
    run's activation for every actor who may still see the skill, even as
    shared-in; the model-activation rule applies where a skill is first
    loaded, not when a run goes on.
    """
    from langchain_core.messages import ToolMessage

    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.kernel.tools import SKILL_LOADED_MARKER

    client = make_world(ScriptedToolLLM([]))
    with client:
        loaded = shared_skill(client)
    container = client.container  # type: ignore[attr-defined]
    algorithm = KernelAgentAlgorithm(
        control_store=MemoryAgentControlStore(),
        checkpointer=None,
        platform=AgentPlatformSettings(),
        skill_service=container.skill_service,
    )
    marker = SKILL_LOADED_MARKER.format(
        skill_id=loaded["id"], revision=loaded["revision"]
    )
    # The checkpointed result of the owner's load: producing tool and
    # marker are genuine.
    snapshot = SimpleNamespace(
        values={
            "messages": [
                ToolMessage(
                    content=f"{marker}\nAnweisungen",
                    name="load_skill",
                    tool_call_id="call_load",
                )
            ]
        }
    )

    def resume_as(user_id) -> KernelDeps:
        principal = Principal(
            user_id=user_id, kind="oidc_session", tenant_id="default", role="member"
        )
        deps = KernelDeps(
            run_id="run_shared",
            control=MemoryAgentControlStore(),
            platform=AgentPlatformSettings(),
            llm=None,
            model=None,
            reasoning_effort=None,
            timeout=1,
        )
        deps.principal = principal
        deps.capability_context = SimpleNamespace(
            visible_to=asyncio.run(
                container.permission_service.resolve_user_context(principal)
            )
        )
        algorithm._reactivate_loaded_skills(deps, snapshot)
        return deps

    # The editor sees the skill as shared-in; the run goes on with it.
    assert [record.id for record in resume_as(RECIPIENT).skills] == [
        loaded["id"]
    ]
    # An actor who may not see the skill cannot resume with it.
    with pytest.raises(
        RuntimeError, match="cannot be safely reactivated on resume"
    ):
        resume_as(STRANGER)


def test_recipient_may_still_attach_a_shared_skill():
    llm = ScriptedToolLLM([_text_turn("Fertig.")])
    client = make_world(llm)
    with client:
        shared = shared_skill(client)
        assert (
            wait_settled(client, submit_as_recipient(client, skill_ids=[shared["id"]]))
            == "completed"
        )

    first_user = llm.chat_calls[0]["messages"][-1]["content"]
    # Attaching is the user's own decision: the shared skill's
    # instructions ride the run.
    assert "[Skill 'fremd-stil'" in first_user
    assert "MARKE2" in first_user
