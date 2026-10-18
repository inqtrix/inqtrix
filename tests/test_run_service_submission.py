"""Native-run submission contracts that must survive worker replay."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from inqtrix.settings import AgentSettings
from inqtrix.services.run_service import RunService


class _Registry:
    def get(self, _mode: str) -> object:
        return object()


class _Store:
    def __init__(self) -> None:
        self.submission: dict[str, Any] = {}

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        self.submission = kwargs
        return {"run_id": "run-effective-settings"}


def _resolved(*, depth: str) -> SimpleNamespace:
    return SimpleNamespace(
        mode="workspace_agent",
        stack_name="",
        agent_overrides={},
        knowledge_filters={},
        agent_settings=AgentSettings(depth=depth),
    )


def test_agent_submission_persists_effective_depth_for_later_plan_edits() -> None:
    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )

    service.submit(
        question="Research",
        history="",
        messages=[],
        resolved=_resolved(depth="deep"),
        workspace_id="workspace-a",
        kind="agent",
    )

    assert store.submission["agent_overrides"]["depth"] == "deep"
    assert (
        store.submission["request_payload"]["body"]["agent_overrides"]
        == {"depth": "deep"}
    )


def test_standard_submission_keeps_explicit_override_summary_shape() -> None:
    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )

    service.submit(
        question="Research",
        history="",
        messages=[],
        resolved=_resolved(depth="deep"),
        workspace_id=None,
    )

    assert store.submission["agent_overrides"] == {}


def test_delegated_research_recency_is_persisted_for_worker_replay() -> None:
    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )

    service.submit(
        question="Research",
        history="",
        messages=[],
        resolved=_resolved(depth="deep"),
        workspace_id="workspace-a",
        kind="agent_child",
        web_recency="year",
    )

    assert store.submission["request_payload"]["body"]["web_recency"] == "year"


def test_canvas_context_is_persisted_for_worker_replay() -> None:
    """Payload proof: the attachment survives as its OWN body field.

    Never in ``question`` (clipped at persistence, leaks into share-inbox
    titles) and never in the summary override channel.
    """
    from inqtrix.core.results import CanvasContext

    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )
    context = CanvasContext.model_validate(
        {
            "artifact_id": "art_ctx1",
            "revision": 3,
            "comments": [
                {
                    "artifact_id": "art_ctx1",
                    "revision": 3,
                    "quote": "Der Umsatz stieg.",
                    "comment": "Bitte Zahl ergaenzen.",
                }
            ],
        }
    )

    service.submit(
        question="Arbeite die Kommentare ein.",
        history="",
        messages=[],
        resolved=_resolved(depth="normal"),
        workspace_id="workspace-a",
        kind="agent",
        canvas_context=context,
    )

    body = store.submission["request_payload"]["body"]
    assert body["canvas_context"] == context.model_dump(mode="json")
    assert store.submission["question"] == "Arbeite die Kommentare ein."
    assert "canvas_context" not in store.submission["agent_overrides"]


def test_without_canvas_context_the_replay_body_carries_no_key() -> None:
    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )
    service.submit(
        question="Frage",
        history="",
        messages=[],
        resolved=_resolved(depth="normal"),
        workspace_id="workspace-a",
        kind="agent",
    )
    assert "canvas_context" not in store.submission["request_payload"]["body"]


def test_the_interface_language_is_persisted_for_worker_replay() -> None:
    bodies = []
    for ui_language in ("de", ""):
        store = _Store()
        service = RunService(registry=_Registry(), runtime=SimpleNamespace(), run_store=store)
        service.submit(
            question="Frage",
            history="",
            messages=[],
            resolved=_resolved(depth="normal"),
            workspace_id="workspace-a",
            ui_language=ui_language,
        )
        bodies.append(store.submission["request_payload"]["body"])
    assert (bodies[0]["ui_language"], "ui_language" in bodies[1]) == ("de", False)


def test_rule_parts_are_persisted_for_worker_replay() -> None:
    """The resolved parts ride the replay body as their own field, next
    to the composed text the writers read: a worker segment must know
    WHICH rules (and revisions) the run is frozen under without
    re-reading the catalog."""
    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )
    parts = [{"template_id": "pt_brief", "label": "briefing", "revision": 3}]
    service.submit(
        question="Erstelle ein Briefing.",
        history="",
        messages=[],
        resolved=_resolved(depth="normal"),
        workspace_id="workspace-a",
        kind="agent",
        report_requirement="[Rule: briefing]\nKurz.\n[End rule: briefing]",
        report_rules=parts,
    )
    body = store.submission["request_payload"]["body"]
    assert body["report_rules"] == [
        {"template_id": "pt_brief", "label": "briefing", "revision": 3}
    ]
    assert "report_rules" not in store.submission["agent_overrides"]


def test_without_rules_the_replay_body_carries_no_rule_parts() -> None:
    """A run without attached rules replays byte-identically."""
    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )
    service.submit(
        question="Frage",
        history="",
        messages=[],
        resolved=_resolved(depth="normal"),
        workspace_id="workspace-a",
        kind="agent",
        report_requirement="[Free requirement]\nKurz.",
    )
    assert "report_rules" not in store.submission["request_payload"]["body"]


def test_an_incognito_ask_is_marked_in_the_payload_the_execution_reads() -> None:
    """The execution and the retention sweep read the persisted request, not
    the HTTP call: an incognito ask carries its flag there, and an ordinary
    ask carries none."""
    store = _Store()
    service = RunService(
        registry=_Registry(),
        runtime=SimpleNamespace(),
        run_store=store,
    )
    resolved = SimpleNamespace(
        mode="knowledge",
        stack_name="",
        agent_overrides={},
        knowledge_filters={},
        agent_settings=AgentSettings(),
    )

    service.submit(
        question="Frage", history="", messages=[], resolved=resolved,
        workspace_id=None, incognito=True,
    )
    assert store.submission["request_payload"]["body"]["incognito"] is True

    service.submit(
        question="Frage", history="", messages=[], resolved=resolved,
        workspace_id=None,
    )
    assert "incognito" not in store.submission["request_payload"]["body"]
