"""A failed tool call must not read as a finished step.

Tool bodies return failures as plain text (the model reads them and names the
gap). The ONE contract that says which texts are failures is
``SOURCE_TOOL_FAILURE_PREFIXES``; the transcript's ``tool.finished`` event is
one of its readers, so a desk row shows "failed" instead of a check mark.
"""

from types import SimpleNamespace

import pytest

from inqtrix.agents.context_sources import SourceCoverageLedger
from inqtrix.agents.kernel.algorithm import _observe_update
from inqtrix.agents.kernel.tools import SOURCE_TOOL_FAILURE_PREFIXES


def _deps(events: list) -> SimpleNamespace:
    return SimpleNamespace(
        emitted_tool_start_ids={},
        emitted_tool_finish_ids=set(),
        tool_call_ordinals={},
        source_coverage=SourceCoverageLedger(),
        tool_calls_used=0,
        todo_in_progress_index=None,
        work_profile=None,
        emit=lambda kind, payload: events.append((kind, payload)),
    )


def _finished(content: object, *, status: str = "success") -> dict:
    events: list = []
    deps = _deps(events)
    call = SimpleNamespace(type="ai", id="message", tool_calls=[
        {"id": "call_a", "name": "search_knowledge", "args": {"query": "Frist"}},
    ])
    reply = SimpleNamespace(
        type="tool", tool_call_id="call_a", name="search_knowledge",
        status=status, content=content,
    )
    _observe_update(deps, {"model": {"messages": [call]}}, lambda: {})
    _observe_update(deps, {"tools": {"messages": [reply]}}, lambda: {})
    finished = [data for kind, data in events if kind == "inqtrix.agent.tool.finished"]
    assert len(finished) == 1
    return finished[0]


@pytest.mark.parametrize("prefix", SOURCE_TOOL_FAILURE_PREFIXES)
def test_a_contract_failure_text_finishes_the_row_as_failed(prefix: str) -> None:
    """The regression: a visible failure text settled its row as success."""
    assert _finished(f"{prefix} the store did not respond.")["status"] == "error"


def test_a_failure_in_a_text_block_list_finishes_as_failed() -> None:
    """Content may arrive as a block list; the contract reads its text."""
    content = [{"type": "text", "text": "Tool error (knowledge.denied): no access."}]
    assert _finished(content)["status"] == "error"


def test_a_successful_result_that_mentions_a_prefix_later_stays_successful() -> None:
    """Only the START of the text is the contract; a quote inside is data."""
    content = 'Found 2 passages. One says "Tool error" in its title.'
    assert _finished(content)["status"] == "success"


def test_an_error_status_tool_message_stays_failed() -> None:
    """The existing reader rule: a rejected call is a failure as before."""
    assert _finished("Rejected by middleware", status="error")["status"] == "error"


def test_every_capability_failure_text_starts_with_a_contract_prefix() -> None:
    """Each failure the capability layer can return is a contract failure.

    The knowledge-store outage text began with "Knowledge store unavailable"
    and matched no prefix: the tool span recorded "completed" and the
    checkpoint recount counted the failed search as a successful use.
    """
    from inqtrix.agents.kernel import tools as kernel_tools
    from inqtrix.agents.kernel.deps import set_kernel_deps
    from inqtrix.capabilities import CapabilityError, UnknownCapability

    class _Registry:
        def __init__(self, error: Exception) -> None:
            self._error = error

        async def invoke(self, capability_id, payload, context):
            raise self._error

    def _texts() -> list[str]:
        texts = []
        for registry in (
            None,
            _Registry(UnknownCapability("knowledge.search")),
            _Registry(CapabilityError("knowledge.store_unavailable", "down")),
            _Registry(CapabilityError("knowledge.denied", "no access")),
        ):
            set_kernel_deps(SimpleNamespace(
                capability_registry=registry,
                capability_context=None,
            ))
            try:
                texts.append(str(kernel_tools._invoke_capability_inner(
                    "knowledge.search", {"query": "Frist"},
                )))
            finally:
                set_kernel_deps(None)
        return texts

    texts = _texts()
    assert len(texts) == 4
    for text in texts:
        assert text.startswith(SOURCE_TOOL_FAILURE_PREFIXES), text


def test_the_usage_recount_skips_a_failure_delivered_as_a_block_list() -> None:
    """The checkpoint recount reads the same contract: a failure whose content
    arrives as text blocks must not count as a successful source-tool use."""
    from langchain_core.messages import ToolMessage

    from inqtrix.agents.kernel.algorithm import _checkpointed_tool_use_counts

    messages = [
        ToolMessage(
            content=[{
                "type": "text",
                "text": "Tool unavailable: the knowledge store did not respond.",
            }],
            name="search_project_knowledge",
            tool_call_id="c1",
        ),
        ToolMessage(
            content="Treffer aus dem Bestand [K1]",
            name="search_project_knowledge",
            tool_call_id="c2",
        ),
    ]
    snapshot = SimpleNamespace(values={"messages": messages})
    assert _checkpointed_tool_use_counts(snapshot) == {"web": 0, "knowledge": 1}
