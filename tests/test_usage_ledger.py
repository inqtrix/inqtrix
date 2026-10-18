"""Usage-ledger recorder, wrapper feed, and cost derivation."""

from __future__ import annotations

import uuid

import pytest

from inqtrix.observability.context import (
    bind_feature,
    bind_usage_subject,
    clear_usage_subject,
    reset_feature,
)
from inqtrix.usage import recorder as recorder_module
from inqtrix.usage.memory import MemoryUsageStore
from inqtrix.usage.models import (
    UsageRow,
    summarize_usage_cost,
    usage_cost_usd,
)
from inqtrix.usage.recorder import UsageRecorder

USER = uuid.uuid4()


def _row(**overrides) -> UsageRow:
    base = dict(
        tenant_id="t1",
        user_id=USER,
        workspace_id=None,
        run_id=None,
        feature="chat",
        operation="chat",
        model="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=5,
        request_count=1,
        duration_ms=42,
        outcome="success",
        created_at=1_000.0,
    )
    base.update(overrides)
    return UsageRow(**base)


@pytest.fixture
def active_recorder(monkeypatch):
    store = MemoryUsageStore()
    rec = UsageRecorder(store, flush_interval_seconds=3600.0)
    monkeypatch.setattr(recorder_module, "_active", rec)
    yield store, rec
    rec.close()
    monkeypatch.setattr(recorder_module, "_active", None)


def test_wrapper_feeds_ledger_row(active_recorder):
    """The provider tracing wrapper books one ledger row per LLM call with the
    ambient subject, feature, and canonical model."""
    from inqtrix.observability.provider_tracing import instrument_llm
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    store, rec = active_recorder
    llm = instrument_llm(FakeLLM(), provider_name="fake", policy=SUMMARY)
    feature_token = bind_feature("knowledge")
    bind_usage_subject("tenant-a", USER, "ws-1")
    try:
        llm.complete_with_metadata("hallo")
    finally:
        reset_feature(feature_token)
        clear_usage_subject()
    rec.close()
    rows = store.rows_snapshot()
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == "tenant-a"
    assert row.user_id == USER
    assert row.workspace_id == "ws-1"
    assert row.feature == "knowledge"
    assert row.operation == "text_completion"
    assert row.model == "fake-1"
    assert row.input_tokens > 0
    assert row.request_count == 1
    assert row.outcome == "success"


def test_no_subject_books_nothing(active_recorder):
    from inqtrix.observability.provider_tracing import instrument_llm
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    store, rec = active_recorder
    clear_usage_subject()
    llm = instrument_llm(FakeLLM(), provider_name="fake", policy=SUMMARY)
    llm.complete_with_metadata("hallo")
    rec.close()
    assert store.rows_snapshot() == []


def test_error_call_books_zero_token_row(active_recorder):
    from inqtrix.observability.provider_tracing import instrument_llm
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    store, rec = active_recorder

    class _BoomLLM(FakeLLM):
        def complete_with_metadata(self, *args, **kwargs):
            raise TimeoutError("provider timed out")

    bind_usage_subject("tenant-a", USER)
    try:
        llm = instrument_llm(_BoomLLM(), provider_name="fake", policy=SUMMARY)
        with pytest.raises(TimeoutError):
            llm.complete_with_metadata("x")
    finally:
        clear_usage_subject()
    rec.close()
    rows = store.rows_snapshot()
    assert len(rows) == 1
    assert rows[0].outcome == "timeout"
    assert rows[0].input_tokens == 0


def test_flush_failure_warns_requeues_and_recovers(caplog):
    class _FlakyStore:
        def __init__(self) -> None:
            self.fail = True
            self.rows: list[UsageRow] = []

        async def insert_rows(self, rows):
            if self.fail:
                raise RuntimeError("db down")
            self.rows.extend(rows)
            return len(rows)

    store = _FlakyStore()
    rec = UsageRecorder(store, flush_interval_seconds=3600.0)
    rec.record(_row())
    with caplog.at_level("WARNING", logger="inqtrix"):
        rec._flush_once()
    assert any("Usage ledger flush failed" in r.message for r in caplog.records)
    assert store.rows == []
    store.fail = False
    rec.close()
    assert len(store.rows) == 1


def test_overflow_drops_new_rows_with_warning(caplog):
    store = MemoryUsageStore()
    rec = UsageRecorder(
        store, flush_interval_seconds=3600.0, max_buffered_rows=2
    )
    with caplog.at_level("WARNING", logger="inqtrix"):
        for _ in range(4):
            rec.record(_row())
    assert any("Usage ledger buffer full" in r.message for r in caplog.records)
    rec.close()
    assert len(store.rows_snapshot()) == 2


def test_cost_derivation_follows_the_operation():
    """Each operation prices from the catalogue that actually describes it."""
    chat = usage_cost_usd("chat", "claude-haiku-4-5", 1_000_000, 1_000_000)
    assert chat is not None and chat == pytest.approx(6.0)

    # Embeddings price input only: the call has no output tokens, which is
    # why the embedding card carries a single price.
    embedding = usage_cost_usd(
        "embeddings", "text-embedding-3-small", 1_000_000, 0
    )
    assert embedding is not None and embedding == pytest.approx(0.02)

    # Self-hosted means UNPRICED, never free.
    assert usage_cost_usd("embeddings", "BAAI/bge-m3", 1_000_000, 0) is None
    # Web search is billed per call under an operator-named agent id.
    assert usage_cost_usd(
        "web_search", "foundry-web:web-search-agent@4", 100_000, 0
    ) is None
    assert usage_cost_usd("chat", "model-that-does-not-exist", 100, 100) is None
    # An unknown operation must not fall back to the chat catalogue.
    assert usage_cost_usd("invoke_agent", "claude-haiku-4-5", 100, 100) is None


def test_cost_summary_carries_what_it_could_not_price():
    """The unpriced remainder is a field of the same return value.

    A total that silently drops unpriced consumption understates spend and
    is believed precisely because it looks precise.
    """
    summary = summarize_usage_cost(
        [
            {
                "operation": "chat",
                "model": "claude-haiku-4-5",
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
            {
                "operation": "web_search",
                "model": "foundry-web:web-search-agent@4",
                "input_tokens": 169_609,
                "output_tokens": 0,
            },
            {
                "operation": "embeddings",
                "model": "BAAI/bge-m3",
                "input_tokens": 5_000,
                "output_tokens": 0,
            },
        ]
    )

    assert summary.cost_usd == pytest.approx(6.0)
    assert summary.priced_input_tokens == 1_000_000
    assert summary.unpriced_input_tokens == 174_609
    assert summary.unpriced_models == (
        "BAAI/bge-m3",
        "foundry-web:web-search-agent@4",
    )
    assert summary.is_complete is False


def test_cost_summary_reports_completeness_when_everything_is_priced():
    summary = summarize_usage_cost(
        [
            {
                "operation": "chat",
                "model": "claude-haiku-4-5",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
            }
        ]
    )

    assert summary.is_complete is True
    assert summary.unpriced_models == ()


@pytest.mark.asyncio
async def test_memory_store_aggregation_and_prune():
    store = MemoryUsageStore()
    other_user = uuid.uuid4()
    await store.insert_rows(
        [
            _row(created_at=100.0),
            _row(created_at=200.0, model="gpt-5.4", input_tokens=7),
            _row(created_at=300.0, user_id=other_user, output_tokens=9),
            _row(created_at=400.0, tenant_id="t2"),
        ]
    )
    by_model = await store.aggregate(tenant_id="t1", group_by="model")
    assert {r["model"] for r in by_model} == {"claude-haiku-4-5", "gpt-5.4"}
    by_user = await store.aggregate(tenant_id="t1", group_by="user_id")
    assert len(by_user) == 2
    windowed = await store.aggregate(
        tenant_id="t1", group_by="feature", since=150.0, until=350.0
    )
    assert windowed[0]["request_count"] == 2
    pruned = await store.prune(days=1)
    assert pruned == 4  # all epoch-1970 rows fall before the cutoff
    assert store.rows_snapshot() == []


def test_ledger_matches_quota_booking_per_run(monkeypatch):
    """The ledger's per-run token sum equals
    the quota booking — both read the SAME provider-reported numbers,
    one per call (ledger) and one aggregated at run end (quota)."""
    import asyncio
    import threading
    from types import SimpleNamespace

    from inqtrix.observability.context import clear_usage_subject
    from inqtrix.observability.provider_tracing import instrument_llm
    from inqtrix.quota.memory import MemoryQuotaStore
    from inqtrix.quota.models import QuotaDimension, QuotaSubject
    from inqtrix.services.quota_service import QuotaService
    from inqtrix.services.run_service import execute_run_request
    from inqtrix.settings import QuotaSettings
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    store = MemoryUsageStore()
    rec = UsageRecorder(store, flush_interval_seconds=3600.0)
    monkeypatch.setattr(recorder_module, "_active", rec)

    llm = instrument_llm(FakeLLM(), provider_name="fake", policy=SUMMARY)

    class _LedgerRunAlgorithm:
        def run(self, _request, *, runtime, context):
            from inqtrix.core.results import AgentResult

            del runtime
            prompt = completion = 0
            for _ in range(2):
                response = context.providers.llm.complete_with_metadata("q")
                prompt += response.prompt_tokens
                completion += response.completion_tokens
            return AgentResult(
                answer="Done",
                raw={
                    "answer": "Done",
                    "usage": {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                    },
                    "result_state": {},
                },
            )

        def capabilities(self):
            return {"terminal_node": "answer"}

    class _DoneHandle:
        run_id = "run-ledger-consistency"
        parked = False

        def __init__(self) -> None:
            self.cancel_event = threading.Event()

        def emit(self, _event_type, _payload) -> None:
            return

        def wait(self, _status) -> None:
            return

        def emit_answer(self, _answer) -> None:
            return

        def total_elapsed_seconds(self) -> float:
            return 1.0

        def complete(self, result, snapshot=None) -> None:
            self.result = result

    quota_service = QuotaService(
        store=MemoryQuotaStore(), settings=QuotaSettings()
    )
    subject = QuotaSubject(tenant_id="tenant-a", user_id=USER)
    # The executing loop binds run_id into the log context before the
    # segment; the ledger reads it from there.
    from inqtrix.observability.context import (
        bind_log_context,
        reset_log_context,
    )

    log_tokens = bind_log_context(run_id="run-ledger-consistency")
    try:
        execute_run_request(
            _DoneHandle(),
            algorithm=_LedgerRunAlgorithm(),
            run_request=SimpleNamespace(mode="research"),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(llm=llm),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(),
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
            quota_service=quota_service,
            quota_subject=subject,
            workspace_id="ws-9",
        )
    finally:
        clear_usage_subject()
        reset_log_context(log_tokens)
    rec.close()
    monkeypatch.setattr(recorder_module, "_active", None)

    rows = [
        r
        for r in store.rows_snapshot()
        if r.run_id == "run-ledger-consistency"
    ]
    assert len(rows) == 2
    ledger_total = sum(r.input_tokens + r.output_tokens for r in rows)
    quota_rows = asyncio.run(quota_service.usage_for(subject))
    quota_used = next(
        r.used
        for r in quota_rows
        if r.dimension == QuotaDimension.LLM_TOKENS
    )
    assert ledger_total == quota_used == 36
    assert all(r.feature == "research" for r in rows)
    assert all(r.workspace_id == "ws-9" for r in rows)


def test_partial_multi_tenant_flush_does_not_double_book():
    """One transaction per tenant — a failing tenant must
    not cause the already-committed tenant's rows to be inserted twice."""
    other_user = uuid.uuid4()

    class _PickyStore:
        def __init__(self) -> None:
            self.rows: list[UsageRow] = []
            self.reject_tenant = "t2"

        async def insert_rows(self, rows):
            if rows and rows[0].tenant_id == self.reject_tenant:
                raise RuntimeError("tenant t2 write failed")
            self.rows.extend(rows)
            return len(rows)

    store = _PickyStore()
    rec = UsageRecorder(store, flush_interval_seconds=3600.0)
    rec.record(_row(tenant_id="t1"))
    rec.record(_row(tenant_id="t2", user_id=other_user))
    rec._flush_once()
    assert [r.tenant_id for r in store.rows] == ["t1"]
    store.reject_tenant = "none"
    rec.close()
    # t1 exactly once, t2 recovered on the retry.
    assert sorted(r.tenant_id for r in store.rows) == ["t1", "t2"]


def test_poison_batch_is_dropped_loudly_after_retry_budget(caplog):
    """A permanently rejected batch must not wedge the
    buffer forever — it is dropped with an ERROR, not silently."""
    from inqtrix.usage import recorder as rec_module

    class _AlwaysRejects:
        async def insert_rows(self, rows):
            raise RuntimeError("check constraint violated")

    rec = UsageRecorder(_AlwaysRejects(), flush_interval_seconds=3600.0)
    rec.record(_row())
    with caplog.at_level("ERROR", logger="inqtrix"):
        for _ in range(rec_module._MAX_FLUSH_RETRIES + 1):
            rec._flush_once()
    assert any("discarding them so subsequent usage recording can continue" in r.message for r in caplog.records)
    rec.record(_row())  # buffer is usable again
    rec.close()


def test_flush_warning_never_carries_raw_identifiers(caplog):
    """The failure WARNING must not embed bound
    parameters (raw user ids / tenant ids) via exc_info."""

    class _LeakyStore:
        async def insert_rows(self, rows):
            raise RuntimeError(
                f"boom with parameters tenant_id={rows[0].tenant_id!r} "
                f"user_id={rows[0].user_id!r}"
            )

    rec = UsageRecorder(_LeakyStore(), flush_interval_seconds=3600.0)
    rec.record(_row(tenant_id="tenant-secret"))
    with caplog.at_level("WARNING", logger="inqtrix"):
        rec._flush_once()
    rendered = "\n".join(
        r.getMessage() + (r.exc_text or "") for r in caplog.records
    )
    assert "tenant-secret" not in rendered
    assert str(USER) not in rendered
    assert "RuntimeError" in rendered  # the error class stays visible
    rec.close()


def test_complete_path_books_state_accumulator_tokens(active_recorder):
    """``complete()`` returns bare text; its tokens only reach
    the caller through state= — the ledger must book the same delta quota
    books, not a zero-token row."""
    from inqtrix.observability.provider_tracing import instrument_llm
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    store, rec = active_recorder

    class _StatefulLLM(FakeLLM):
        def complete(self, prompt, **kwargs):
            state = kwargs.get("state")
            if state is not None:
                state["total_prompt_tokens"] += 13
                state["total_completion_tokens"] += 4
            return "text-answer"

    llm = instrument_llm(_StatefulLLM(), provider_name="fake", policy=SUMMARY)
    usage_state = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
    bind_usage_subject("tenant-a", USER)
    try:
        llm.complete("hi", state=usage_state)
    finally:
        clear_usage_subject()
    rec.close()
    rows = store.rows_snapshot()
    assert len(rows) == 1
    assert (rows[0].input_tokens, rows[0].output_tokens) == (13, 4)


def test_indexing_ledger_bind_is_independent_of_quota(monkeypatch):
    """The indexing ledger subject comes from the
    ACTOR, not from the quota service — quotas are default-OFF and must
    not decide whether consumption history exists."""
    import uuid as _uuid

    from inqtrix.observability.context import current_usage_subject
    from inqtrix.services import indexing_service

    seen = {}

    def _capture(handle, **kwargs):
        subject = current_usage_subject()
        seen["tenant"] = subject.tenant_id if subject else None
        seen["user"] = subject.user_id if subject else None
        seen["workspace"] = subject.workspace_id if subject else None

    monkeypatch.setattr(
        indexing_service, "_dispatch_indexing_operation", _capture
    )
    actor = _uuid.uuid4()
    indexing_service.execute_indexing_operation(
        object(),
        knowledge_service=object(),
        operation_kind="document_revision",
        collection_id="kc_1",
        embedding_model="m",
        generation_id=None,
        document_id="kd_1",
        revision_id="rev_1",
        quota_service=None,      # quotas OFF
        quota_subject=None,      # -> no quota subject at all
        actor_user_id=actor,
        tenant_id="tenant-a",
        workspace_id="ws-7",
    )
    assert seen == {
        "tenant": "tenant-a",
        "user": actor,
        "workspace": "ws-7",
    }
    assert current_usage_subject() is None  # cleared in finally


def test_embedding_call_books_the_estimated_input_tokens(active_recorder):
    """Embedding wrappers book the same estimate the quota path uses over the
    same texts, so ledger and quota cannot drift apart."""
    from inqtrix.observability.provider_tracing import instrument_embeddings
    from inqtrix.quota.models import estimate_tokens
    from tests.test_provider_tracing import SUMMARY, FakeEmbeddings

    store, rec = active_recorder
    embeddings = instrument_embeddings(FakeEmbeddings(), policy=SUMMARY)
    texts = ["Meldewege und Fristen", "Wiederanlaufziele je Dienst"]
    bind_usage_subject("tenant-a", USER, "ws-1")
    try:
        embeddings.embed_documents(texts)
        embeddings.embed_query(texts[0])
    finally:
        clear_usage_subject()
    rec.close()

    rows = store.rows_snapshot()
    assert [row.operation for row in rows] == ["embeddings", "embeddings"]
    assert rows[0].input_tokens == sum(estimate_tokens(text) for text in texts)
    assert rows[1].input_tokens == estimate_tokens(texts[0])
    assert all(row.output_tokens == 0 for row in rows)


def test_ledger_keeps_the_full_model_id_when_the_metric_label_is_capped(
    active_recorder, monkeypatch
):
    """The metric cardinality guard protects a time series, not an immutable
    billing record: the ledger keeps the identifier a price can resolve."""
    from inqtrix.observability import metrics_defs
    from inqtrix.observability.provider_tracing import instrument_llm
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    monkeypatch.setattr(metrics_defs, "_fallback_model_labels", set())
    monkeypatch.setattr(metrics_defs, "_fallback_cap_warned", False)
    for index in range(metrics_defs._FALLBACK_MODEL_LABEL_LIMIT + 1):
        metrics_defs.metric_model_label(f"filler-model-{index}")
    assert metrics_defs.metric_model_label("fake-1") == "other"

    store, rec = active_recorder
    llm = instrument_llm(FakeLLM(), provider_name="fake", policy=SUMMARY)
    bind_usage_subject("tenant-a", USER, "ws-1")
    try:
        llm.complete_with_metadata("hallo")
    finally:
        clear_usage_subject()
    rec.close()

    assert store.rows_snapshot()[0].model == "fake-1"


@pytest.mark.asyncio
async def test_aggregation_groups_by_the_pair_pricing_needs():
    """Model alone cannot be priced; the operation picks the catalogue."""
    store = MemoryUsageStore()
    await store.insert_rows(
        [
            _row(model="gpt-5.4", operation="chat", input_tokens=10),
            _row(
                model="text-embedding-3-small",
                operation="embeddings",
                input_tokens=7,
            ),
        ]
    )

    rows = await store.aggregate(tenant_id="t1")

    assert {(r["model"], r["operation"]) for r in rows} == {
        ("gpt-5.4", "chat"),
        ("text-embedding-3-small", "embeddings"),
    }


@pytest.mark.asyncio
async def test_aggregation_narrows_to_one_run_and_rejects_unknown_axes():
    store = MemoryUsageStore()
    await store.insert_rows(
        [
            _row(run_id="run_a", input_tokens=3),
            _row(run_id="run_b", input_tokens=5),
        ]
    )

    only_a = await store.aggregate(tenant_id="t1", run_id="run_a")
    assert sum(r["input_tokens"] for r in only_a) == 3

    # Rejected, not silently dropped: grouping by less than asked answers a
    # different question than the caller posed.
    with pytest.raises(ValueError):
        await store.aggregate(tenant_id="t1", group_by=("nonsense",))


def test_usage_read_surface_is_mounted_by_create_app():
    """The router must exist in a built app, not only in principle.

    It resolves the process recorder while the routes are being built, so
    installing the recorder after registration left every usage path a 404
    while every unit test still passed. Session auth is the gate — without a
    verified principal there is no per-user consumption to report.
    """
    from inqtrix.server.app import create_app
    from inqtrix.settings import (
        AuthSettings,
        ModelSettings,
        ServerSettings,
        Settings,
    )

    app = create_app(
        settings=Settings(
            auth=AuthSettings(
                mode="local",
                session_secret="s" * 32,
                pat_pepper="p" * 32,
                pseudonym_pepper="pepper" * 6,
                oidc_insecure_dev_cookies=True,
            ),
            models=ModelSettings(),
            server=ServerSettings(),
        )
    )
    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/v1/usage" in paths
    assert "/v1/admin/usage" in paths
    assert "/v1/usage/axes" in paths


def test_cost_is_independent_of_the_display_grouping():
    """Two groupings of the same window must report the same money.

    The price catalogue is chosen by operation and the rate by model, so a
    grouping that omits the model made every row unpriceable and the total
    zero — the same data answered 1.30 USD or 0.00 USD depending on how the
    caller asked to see it.
    """
    from inqtrix.server.routers.usage import _body
    from inqtrix.usage.grouping import costing_group_by, normalize_usage_group_by

    ledger = [
        {
            "feature": "research",
            "model": "claude-haiku-4-5",
            "operation": "chat",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "request_count": 3,
        },
        {
            "feature": "knowledge",
            "model": "text-embedding-3-small",
            "operation": "embeddings",
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "request_count": 5,
        },
    ]

    nach_modell = _body(ledger, normalize_usage_group_by(("model", "operation")))
    nach_feature = _body(ledger, normalize_usage_group_by(("feature",)))

    assert nach_modell["total"]["cost_usd"] == pytest.approx(6.02)
    assert nach_feature["total"]["cost_usd"] == pytest.approx(6.02)
    assert sum(r["cost_usd"] for r in nach_feature["data"]) == pytest.approx(6.02)
    assert all(r["cost_complete"] for r in nach_feature["data"])
    # The store must be asked for the pricing axes even when they are not shown.
    assert costing_group_by(("feature",)) == ("feature", "model", "operation")


def test_a_row_with_an_unpriced_share_is_marked_incomplete():
    """A partial sum must never read as a complete one."""
    from inqtrix.server.routers.usage import _body
    from inqtrix.usage.grouping import normalize_usage_group_by

    body = _body(
        [
            {
                "feature": "research",
                "model": "claude-haiku-4-5",
                "operation": "chat",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "request_count": 1,
            },
            {
                "feature": "research",
                "model": "foundry-web:web-search-agent@4",
                "operation": "web_search",
                "input_tokens": 50_000,
                "output_tokens": 0,
                "request_count": 2,
            },
        ],
        normalize_usage_group_by(("feature",)),
    )

    assert len(body["data"]) == 1
    zeile = body["data"][0]
    assert zeile["cost_usd"] == pytest.approx(1.0)
    assert zeile["cost_complete"] is False
    assert zeile["input_tokens"] == 1_050_000
