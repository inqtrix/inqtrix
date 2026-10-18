"""Log-context contextvars (bind/reset/current + principal stamping)."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from inqtrix.auth import log_redaction
from inqtrix.auth.log_redaction import (
    configure_stable_pseudonyms,
    stable_pseudonym,
)
from inqtrix.observability.context import (
    bind_log_context,
    bind_principal_context,
    current_log_context,
    reset_log_context,
)


@pytest.fixture(autouse=True)
def _reset_pseudonym_state():
    saved_key = log_redaction._stable_key
    saved_warned = log_redaction._fallback_warned
    yield
    log_redaction._stable_key = saved_key
    log_redaction._fallback_warned = saved_warned


def test_bind_current_reset_roundtrip():
    tokens = bind_log_context(request_id="req-1", run_id="run_abc")
    try:
        context = current_log_context()
        assert context["request_id"] == "req-1"
        assert context["run_id"] == "run_abc"
    finally:
        reset_log_context(tokens)
    assert "request_id" not in current_log_context()
    assert "run_id" not in current_log_context()


def test_unknown_field_raises_loudly():
    with pytest.raises(KeyError):
        bind_log_context(reqeust_id="typo")


def test_none_binds_empty_and_is_omitted():
    tokens = bind_log_context(run_id=None)
    try:
        assert "run_id" not in current_log_context()
    finally:
        reset_log_context(tokens)


def test_context_is_isolated_between_tasks():
    async def scenario():
        async def task_a():
            bind_log_context(run_id="run_a")
            await asyncio.sleep(0)
            return current_log_context().get("run_id")

        async def task_b():
            await asyncio.sleep(0)
            return current_log_context().get("run_id")

        return await asyncio.gather(task_a(), task_b())

    seen_a, seen_b = asyncio.run(scenario())
    assert seen_a == "run_a"
    assert seen_b is None
    assert current_log_context().get("run_id") is None


def test_principal_binding_uses_stable_pseudonym():
    configure_stable_pseudonyms("context-test-pepper")
    user_id = uuid.uuid4()
    principal = SimpleNamespace(user_id=user_id, tenant_id="default")
    tokens = bind_log_context(user="", tenant="")
    try:
        bind_principal_context(principal)
        context = current_log_context()
        assert context["user"] == stable_pseudonym("usr", user_id)
        assert str(user_id) not in context["user"]
        assert context["tenant"] == "default"
    finally:
        reset_log_context(tokens)


def test_unscoped_principal_binds_no_user():
    principal = SimpleNamespace(user_id=None, tenant_id="default")
    tokens = bind_log_context(user="", tenant="")
    try:
        bind_principal_context(principal)
        assert "user" not in current_log_context()
        assert current_log_context()["tenant"] == "default"
    finally:
        reset_log_context(tokens)


def test_bound_thread_call_carries_log_context():
    """Executor threads inherit no contextvars, so
    the wrapper must re-bind the correlation context — otherwise every
    log line of the chat/editor path loses its request_id."""
    from inqtrix.observability.context import (
        bind_log_context,
        bound_thread_call,
        current_log_context,
        reset_log_context,
    )

    seen: dict = {}

    def _inner():
        seen.update(current_log_context())

    tokens = bind_log_context(request_id="req-42", user="usr_abc")
    try:
        runner = bound_thread_call(_inner, feature="chat")
    finally:
        reset_log_context(tokens)
    # Run it where the caller's context is already gone (like a pool
    # thread would).
    assert current_log_context() == {}
    runner()
    assert seen["request_id"] == "req-42"
    assert seen["user"] == "usr_abc"
    assert current_log_context() == {}  # cleared again


def test_audit_entry_derives_actor_pseudonym():
    """The read model shows the actor ONLY as
    actor_pseudonym, so AuditEntry derives it for every writer."""
    import uuid

    from inqtrix.auth.permissions import AuditEntry

    entry = AuditEntry(
        tenant_id="default",
        actor_user_id=uuid.uuid4(),
        action="admin.thing",
        resource_type="run",
        resource_id="run_1",
    )
    assert entry.actor_pseudonym and entry.actor_pseudonym.startswith("usr_")
    anonymous = AuditEntry(
        tenant_id="default",
        actor_user_id=None,
        action="auth.login_failed",
        resource_type="user",
        resource_id="x@example.com",
    )
    assert anonymous.actor_pseudonym is None


# --------------------------------------------------------------------- #
# Trace context across the executor hop and the user on the request span
# --------------------------------------------------------------------- #


@pytest.fixture()
def _span_provider():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider
    provider.shutdown()


def test_a_span_opened_in_a_bound_executor_call_is_a_child_of_the_request_span(
    _span_provider,
):
    """``run_in_executor`` copies no contextvars. Without carrying the trace
    context across, the editor assist span opened a trace of its own, so the
    access log's trace id led to a request span without the model call."""
    from concurrent.futures import ThreadPoolExecutor

    from inqtrix.observability.context import bound_thread_call
    from inqtrix.observability.otel import operation_span

    exporter, provider = _span_provider

    def work() -> None:
        with operation_span("editor:suggest", tracer_provider=provider):
            pass

    with provider.get_tracer("test").start_as_current_span("POST /v1/editor/suggest"):
        runner = bound_thread_call(work)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(runner).result()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    request = spans["POST /v1/editor/suggest"]
    child = spans["editor:suggest"]
    assert child.parent is not None
    assert child.parent.span_id == request.get_span_context().span_id
    assert child.get_span_context().trace_id == request.get_span_context().trace_id


def test_the_request_trace_context_does_not_leak_into_the_next_task_on_the_same_pooled_thread(
    _span_provider,
):
    """The pool reuses its threads: the attached request context must be
    detached when the call ends, or the next unrelated task on that thread
    would join a finished request's trace."""
    from concurrent.futures import ThreadPoolExecutor

    from inqtrix.observability.context import bound_thread_call
    from inqtrix.observability.otel import current_trace_id_hex

    _exporter, provider = _span_provider
    seen: dict[str, object] = {}

    def work() -> None:
        seen["inside"] = current_trace_id_hex()

    with provider.get_tracer("test").start_as_current_span("request") as span:
        request_trace = format(span.get_span_context().trace_id, "032x")
        runner = bound_thread_call(work)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(runner).result()
        seen["next_task"] = pool.submit(current_trace_id_hex).result()

    assert seen["inside"] == request_trace
    assert seen["next_task"] is None


def test_resolving_the_principal_stamps_the_pseudonymous_user_on_the_request_span(
    _span_provider,
):
    """The request span is the trace root; without the user there, every
    request trace in the tracing backend was anonymous although the log line
    of the same request named the user's pseudonym."""
    from inqtrix.observability import semconv

    exporter, provider = _span_provider
    user_id = uuid.UUID("12345678-1234-4234-8234-123456789abc")
    principal = SimpleNamespace(user_id=user_id, tenant_id="default")
    tokens = bind_log_context(user="", tenant="")
    try:
        with provider.get_tracer("test").start_as_current_span("POST /v1/editor/suggest"):
            bind_principal_context(principal)
        logged_user = current_log_context()["user"]
    finally:
        reset_log_context(tokens)

    (span,) = exporter.get_finished_spans()
    attributes = dict(span.attributes)
    assert attributes[semconv.LANGFUSE_USER_ID] == stable_pseudonym("usr", user_id)
    # The trace user is the log line's user, so both join on one reference.
    assert attributes[semconv.LANGFUSE_USER_ID] == logged_user
    assert str(user_id) not in json.dumps(attributes, default=str)


def test_an_unscoped_principal_stamps_no_user_on_the_request_span(_span_provider):
    from inqtrix.observability import semconv

    exporter, provider = _span_provider
    tokens = bind_log_context(user="", tenant="")
    try:
        with provider.get_tracer("test").start_as_current_span("GET /v1/runs"):
            bind_principal_context(SimpleNamespace(user_id=None, tenant_id="default"))
    finally:
        reset_log_context(tokens)

    (span,) = exporter.get_finished_spans()
    assert semconv.LANGFUSE_USER_ID not in dict(span.attributes)
