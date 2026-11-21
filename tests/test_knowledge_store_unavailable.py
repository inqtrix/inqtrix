"""A knowledge-store outage is a named condition, never a silent fallback.

Three layers say the same thing from the same fact: the capability manifest
publishes ``reason_code`` on the knowledge source control (registry AND
probe), the run router refuses an EXPLICIT knowledge request before any
cost (503 ``knowledge_store_unavailable``), and the kernel tool tells the
model once, in plain words, not to retry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from inqtrix.agents.kernel.deps import KernelDeps, set_kernel_deps
from inqtrix.agents.kernel.tools import build_kernel_tools
from inqtrix.auth.principal import Principal
from inqtrix.capabilities import CapabilityError
from inqtrix.capabilities.catalog.knowledge import (
    KnowledgeSearchInput,
    build_knowledge_capabilities,
)
from inqtrix.capabilities.contracts import CapabilityContext
from inqtrix.execution_authority import knowledge_store_required
from inqtrix.knowledge.stores.ports import VectorStoreUnavailable
from inqtrix.server.routers.capabilities import source_control


# ---------------------------------------------------------------- adapter --


def test_transport_failures_map_to_vector_store_unavailable() -> None:
    from inqtrix.knowledge.stores.qdrant_store import _unavailable_from

    assert isinstance(_unavailable_from(ConnectionError("refused")), VectorStoreUnavailable)
    assert isinstance(_unavailable_from(TimeoutError("slow")), VectorStoreUnavailable)
    # Not an outage: the caller keeps its own semantics.
    assert _unavailable_from(ValueError("bad input")) is None


@pytest.mark.asyncio
async def test_unreachable_qdrant_raises_vector_store_unavailable() -> None:
    """A store nobody listens on (port 1) fails as an outage, not as an
    opaque client exception the caller cannot classify."""
    from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore

    store = QdrantKnowledgeStore(url="http://127.0.0.1:1", api_key="", timeout=0.5)
    with pytest.raises(VectorStoreUnavailable):
        await store.search(
            query_embedding=[0.1, 0.2], collection_ids=["kc_1"], top_k=3,
            embedding_model="stub",
        )


class _FlakyQdrantClient:
    """Fails the next ``fail_next`` calls of ``get_collections``, then answers."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay
        self._failures_ahead = 0

    def fail_next(self, count: int) -> None:
        self._failures_ahead = count

    def get_collections(self) -> SimpleNamespace:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self._failures_ahead > 0:
            self._failures_ahead -= 1
            raise ConnectionError("connection closed by the server")
        return SimpleNamespace(collections=[])


def _qdrant_over(store_class: str, client: _FlakyQdrantClient):
    """A Qdrant index or store over *client*, built without the constructor,
    which would contact a server; the probe reads nothing but the client."""
    from inqtrix.knowledge.stores import qdrant_store

    store = object.__new__(getattr(qdrant_store, store_class))
    store._client = client
    return store


def _probe_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("Qdrant availability probe failed")
    ]


_QDRANT_LOGGER = "inqtrix.knowledge.stores.qdrant_store"
_QDRANT_STORES = ("QdrantVectorIndex", "QdrantKnowledgeStore")


@pytest.mark.parametrize("store_class", _QDRANT_STORES)
def test_one_failed_probe_call_is_repeated_and_stays_visible(
    store_class: str, caplog: pytest.LogCaptureFixture
) -> None:
    """One failed call does not report a store that answers as down; the
    failure still reaches the log with its exception type."""
    client = _FlakyQdrantClient()
    client.fail_next(1)

    with caplog.at_level(logging.WARNING, logger=_QDRANT_LOGGER):
        assert asyncio.run(_qdrant_over(store_class, client).is_available()) is True

    assert client.calls == 2
    assert _probe_warnings(caplog) == [
        "Qdrant availability probe failed (attempt 1 of 2): "
        "ConnectionError: connection closed by the server",
    ]


@pytest.mark.parametrize("store_class", _QDRANT_STORES)
def test_two_failed_probe_calls_in_a_row_report_the_store_unavailable(
    store_class: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = _FlakyQdrantClient()
    client.fail_next(2)  # a third call would answer

    with caplog.at_level(logging.WARNING, logger=_QDRANT_LOGGER):
        assert asyncio.run(_qdrant_over(store_class, client).is_available()) is False

    assert client.calls == 2
    assert _probe_warnings(caplog) == [
        "Qdrant availability probe failed (attempt 1 of 2): "
        "ConnectionError: connection closed by the server",
        "Qdrant availability probe failed (attempt 2 of 2): "
        "ConnectionError: connection closed by the server",
    ]


def test_the_repeated_probe_call_stays_inside_the_probe_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both calls share the one bound of the runtime probe, so a store that
    fails never doubles how long an admission waits: a failed and a good
    call that together take longer than the bound read as unreachable."""
    from inqtrix.services import system_runtime

    monkeypatch.setattr(system_runtime, "_RUNTIME_PROBE_TIMEOUT_SECONDS", 0.3)
    client = _FlakyQdrantClient(delay=0.2)
    client.fail_next(1)
    store = _qdrant_over("QdrantVectorIndex", client)
    container = SimpleNamespace(
        knowledge_service=SimpleNamespace(knowledge=SimpleNamespace(store=store))
    )

    assert asyncio.run(system_runtime.knowledge_availability(container)) == (
        False,
        "store_unreachable",
    )


# ------------------------------------------------------------- capability --


class _OutageService:
    async def assert_collections_visible(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def search_reported(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise VectorStoreUnavailable("vector store unreachable: refused")


def _search_definition(service: object):
    for definition in build_knowledge_capabilities(service):  # type: ignore[arg-type]
        if definition.id == "knowledge.search":
            return definition
    raise AssertionError("knowledge.search capability missing")


def test_knowledge_search_names_the_outage_as_a_503_capability_error() -> None:
    definition = _search_definition(_OutageService())
    context = CapabilityContext(
        principal=Principal(user_id=None, kind="anonymous"),
        knowledge_collection_ids=frozenset({"kc_1"}),
    )
    with pytest.raises(CapabilityError) as caught:
        asyncio.run(definition.handler(KnowledgeSearchInput(query="Frist?"), context))
    assert caught.value.code == "knowledge.store_unavailable"
    assert caught.value.http_status == 503


# ------------------------------------------------------------------ kernel --


class _OutageRegistry:
    async def invoke(self, _capability_id: str, _payload: dict, _context: object):
        raise CapabilityError(
            "knowledge.store_unavailable",
            "The knowledge store is unreachable.",
            http_status=503,
        )


def test_kernel_tool_tells_the_model_once_and_forbids_retries() -> None:
    tools = {getattr(t, "name", ""): t for t in build_kernel_tools()}
    search = tools["search_project_knowledge"]
    deps = KernelDeps(
        run_id="run_outage",
        control=None,  # type: ignore[arg-type]
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
        capability_registry=_OutageRegistry(),
    )
    try:
        set_kernel_deps(deps)
        text = search.invoke({"query": "Frist?"})
    finally:
        set_kernel_deps(None)
    # A contract failure text (SOURCE_TOOL_FAILURE_PREFIXES): the tool span,
    # the usage recount and the transcript row all read it as failed. The
    # earlier "Knowledge store unavailable" opening matched no prefix.
    assert text.startswith("Tool unavailable:")
    assert "knowledge store did not respond" in text
    assert "Do not retry" in text
    assert getattr(text, "code", "") == "knowledge.store_unavailable"


# ---------------------------------------------------------------- manifest --


def test_source_control_reports_why_knowledge_is_unavailable() -> None:
    healthy = source_control("knowledge", registered=True, available=True, reason_code=None)
    assert healthy == {"id": "knowledge", "default": "available", "available": True, "reason_code": None}
    # Not configured: the registry has no tool and the derivation says so.
    disabled = source_control(
        "knowledge", registered=False, available=False, reason_code="operator_disabled"
    )
    assert (disabled["available"], disabled["reason_code"]) == (False, "operator_disabled")
    # Configured and registered, but the store does not answer its probe.
    outage = source_control(
        "knowledge", registered=True, available=False, reason_code="store_unreachable"
    )
    assert (outage["available"], outage["reason_code"]) == (False, "store_unreachable")
    # A registry without the tool never advertises it, whatever the probe says.
    unregistered = source_control("knowledge", registered=False, available=True, reason_code=None)
    assert (unregistered["available"], unregistered["reason_code"]) == (False, "operator_disabled")


def test_knowledge_availability_is_one_derivation_for_manifest_and_admission() -> None:
    """The manifest passes its fresh probe result in; the admission runs
    only the vector-store probe. Both read the same two reasons."""
    import asyncio
    from types import SimpleNamespace

    from inqtrix.services.system_runtime import knowledge_availability

    no_service = SimpleNamespace(knowledge_service=None)
    assert asyncio.run(knowledge_availability(no_service)) == (False, "operator_disabled")

    class _Store:
        def __init__(self, ok: bool) -> None:
            self.ok = ok

        async def is_available(self) -> bool:
            return self.ok

    def _container(ok: bool):
        return SimpleNamespace(
            knowledge_service=SimpleNamespace(knowledge=SimpleNamespace(store=_Store(ok)))
        )

    assert asyncio.run(knowledge_availability(_container(True))) == (True, None)
    assert asyncio.run(knowledge_availability(_container(False))) == (False, "store_unreachable")
    # A caller holding a probe result skips the probe entirely.
    assert asyncio.run(
        knowledge_availability(_container(False), vector_store_available=True)
    ) == (True, None)


@pytest.mark.asyncio
async def test_registry_reads_map_transport_failures_too() -> None:
    """The collection registry is read BEFORE every search on the ask path;
    an outage there is the same typed failure as an outage in the search."""
    from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore

    store = QdrantKnowledgeStore(url="http://127.0.0.1:1", api_key="", timeout=0.5)
    with pytest.raises(VectorStoreUnavailable):
        await store.get_collection("kc_1")
    with pytest.raises(VectorStoreUnavailable):
        await store.list_collections()


class _VisibilityOutageService:
    async def assert_collections_visible(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise VectorStoreUnavailable("vector store unreachable: refused")

    async def search_reported(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("must not be reached")


def test_an_outage_in_the_visibility_check_is_the_same_503() -> None:
    definition = _search_definition(_VisibilityOutageService())
    context = CapabilityContext(
        principal=Principal(user_id=None, kind="anonymous"),
        knowledge_collection_ids=frozenset({"kc_1"}),
    )
    with pytest.raises(CapabilityError) as caught:
        asyncio.run(definition.handler(KnowledgeSearchInput(query="Frist?"), context))
    assert caught.value.code == "knowledge.store_unavailable"
    assert caught.value.http_status == 503


def test_admission_requires_the_store_only_for_explicit_knowledge_requests() -> None:
    assert knowledge_store_required(mode="knowledge", explicit_scope=False) is True
    assert knowledge_store_required(mode="agent_kernel", explicit_scope=True) is True
    # A run that merely COULD use knowledge is admitted; its tool fails loudly.
    assert knowledge_store_required(mode="agent_kernel", explicit_scope=False) is False
    assert knowledge_store_required(mode="research", explicit_scope=False) is False


@pytest.mark.parametrize("failures", [1, 2])
def test_a_knowledge_run_is_refused_only_after_two_failed_probe_calls(
    failures: int,
) -> None:
    """The admission of a knowledge run reads that probe: one failed call
    admits the run, two in a row refuse it with the reason before any cost."""
    from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
    from tests.contract._app import wait_for_run_status
    from tests.test_knowledge_routes import (
        _create_collection_with_document,
        make_knowledge_client,
    )

    qdrant = _FlakyQdrantClient()
    index = _qdrant_over("QdrantVectorIndex", qdrant)

    class _CanonicalStore(MemoryKnowledgeStore):
        # The canonical store hands its availability to the vector index,
        # as the Postgres store does in the full stack.
        async def is_available(self) -> bool:
            return await index.is_available()

    client, _ = make_knowledge_client(store=_CanonicalStore())
    with client:
        collection_id = _create_collection_with_document(client)
        qdrant.fail_next(failures)
        response = client.post(
            "/v1/runs",
            json={
                "question": "Wie ist die Haftung geregelt?",
                "mode": "knowledge",
                "knowledge_filters": {"collection_ids": [collection_id]},
            },
        )
        if failures == 1:
            assert response.status_code == 202
            wait_for_run_status(client, response.json()["run_id"], "completed")
        else:
            assert response.status_code == 503
            error = response.json()["error"]
            assert (error["type"], error["reason"]) == (
                "knowledge_store_unavailable",
                "store_unreachable",
            )
