"""Root-span helpers and the spool replay CLI."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from inqtrix.observability.otel import (
    current_trace_id_hex,
    enrich_current_span,
    operation_span,
    traced_thread_call,
)
from inqtrix.observability.replay import (
    _headers_from_env,
    _traces_url,
    replay_path,
)


@pytest.fixture()
def exporter_and_provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider
    provider.shutdown()


def test_enrich_current_span_sets_attributes(exporter_and_provider):
    exporter, provider = exporter_and_provider
    with operation_span(
        "inqtrix.run", {"inqtrix.mode": "research"}, tracer_provider=provider
    ):
        enrich_current_span(
            {
                "langfuse.user.id": "usr_0123456789abcdef",
                "langfuse.session.id": "",  # empty values are skipped
                "inqtrix.outcome": "completed",
            }
        )
        assert current_trace_id_hex() is not None
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["langfuse.user.id"] == "usr_0123456789abcdef"
    assert attrs["inqtrix.outcome"] == "completed"
    assert "langfuse.session.id" not in attrs


def test_enrich_without_active_span_is_noop():
    enrich_current_span({"inqtrix.outcome": "completed"})
    assert current_trace_id_hex() is None


def test_traced_thread_call_opens_span_inside_thread(exporter_and_provider):
    exporter, provider = exporter_and_provider

    def work() -> str:
        # The root span must be ACTIVE in the executing thread so that
        # nested provider spans parent correctly.
        assert current_trace_id_hex() is not None
        with operation_span("child", tracer_provider=provider):
            pass
        return "done"

    def enrich(span, result) -> None:
        span.set_attribute("inqtrix.result", result)

    runner = traced_thread_call(
        "inqtrix.chat",
        {"inqtrix.mode": "direct_chat"},
        work,
        enrich=enrich,
        tracer_provider=provider,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(runner).result() == "done"

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["inqtrix.chat"]
    child = spans["child"]
    assert dict(root.attributes)["inqtrix.result"] == "done"
    assert child.parent is not None
    assert child.parent.span_id == root.get_span_context().span_id
    assert (
        child.get_span_context().trace_id
        == root.get_span_context().trace_id
    )


def test_traced_thread_call_records_errors(exporter_and_provider):
    exporter, provider = exporter_and_provider

    def boom() -> None:
        raise RuntimeError("agent failed")

    runner = traced_thread_call(
        "inqtrix.chat", None, boom, tracer_provider=provider
    )
    with pytest.raises(RuntimeError, match="agent failed"):
        runner()
    (span,) = exporter.get_finished_spans()
    assert not span.status.is_ok


# An exception's text can carry a prompt, a document passage or a provider's
# answer; a span records the logs' projection of it (type, stable code,
# frames), never the text, whether content capture is on or off.
_PROSE = "Vertraulicher Absatz SENTINEL-SPAN-PROSE"


def _assert_recorded_without_prose(span) -> None:
    from opentelemetry.trace import StatusCode

    from tests._tracing_helpers import everything_a_span_exports

    assert "SENTINEL-SPAN-PROSE" not in everything_a_span_exports(span)
    assert span.status.status_code is StatusCode.ERROR
    (event,) = [event for event in span.events if event.name == "exception"]
    assert event.attributes["exception.type"] == "RuntimeError"


def test_an_exception_escaping_an_operation_span_is_recorded_without_its_text(
    exporter_and_provider,
):
    exporter, provider = exporter_and_provider
    with pytest.raises(RuntimeError, match="SENTINEL-SPAN-PROSE"):
        with operation_span("knowledge.grounding", tracer_provider=provider):
            raise RuntimeError(_PROSE)
    (span,) = exporter.get_finished_spans()
    _assert_recorded_without_prose(span)


def test_an_exception_escaping_the_chat_root_span_is_recorded_without_its_text(
    exporter_and_provider,
):
    exporter, provider = exporter_and_provider

    def boom() -> None:
        raise RuntimeError(_PROSE)

    runner = traced_thread_call("inqtrix.chat", None, boom, tracer_provider=provider)
    with pytest.raises(RuntimeError, match="SENTINEL-SPAN-PROSE"):
        runner()
    (span,) = exporter.get_finished_spans()
    _assert_recorded_without_prose(span)


def test_an_exception_escaping_the_run_span_is_recorded_without_its_text(
    exporter_and_provider, monkeypatch
):
    from inqtrix.observability import otel as otel_module
    from tests._tracing_helpers import ProxyTrace

    exporter, provider = exporter_and_provider
    monkeypatch.setattr(otel_module, "_otel_trace", ProxyTrace(provider))
    with pytest.raises(RuntimeError, match="SENTINEL-SPAN-PROSE"):
        with otel_module.run_execute_span(
            run_id="run_1", tenant_id="default", attempt=1, payload=None
        ):
            raise RuntimeError(_PROSE)
    (span,) = exporter.get_finished_spans()
    _assert_recorded_without_prose(span)


def test_a_span_that_classifies_its_own_exceptions_records_none(
    exporter_and_provider,
):
    """The kernel's tool span classifies an exception itself, and the
    capability span below it leaves the recording to the tool span: one
    exception is recorded once."""
    exporter, provider = exporter_and_provider
    with pytest.raises(RuntimeError):
        with operation_span(
            "web.search", tracer_provider=provider, record_exceptions=False
        ):
            raise RuntimeError(_PROSE)
    (span,) = exporter.get_finished_spans()
    assert [event for event in span.events if event.name == "exception"] == []
    assert span.status.is_unset


# ------------------------------------------------------------------ #
# Replay CLI
# ------------------------------------------------------------------ #


def test_traces_url_appends_v1_traces_once():
    assert (
        _traces_url("https://x/api/public/otel")
        == "https://x/api/public/otel/v1/traces"
    )
    assert (
        _traces_url("https://x/api/public/otel/v1/traces")
        == "https://x/api/public/otel/v1/traces"
    )


def test_headers_from_env(monkeypatch):
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "Authorization=Basic abc,x-langfuse-ingestion-version=4",
    )
    headers = _headers_from_env()
    assert headers["Authorization"] == "Basic abc"
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_terminate_native_run_marks_the_open_run_span(exporter_and_provider):
    """A failed segment must never render as a clean run: the terminal
    write happens while the run root span is still current, and the
    shared failure chokepoint stamps ERROR + outcome=failed onto it."""
    from opentelemetry.trace import StatusCode

    from inqtrix.execution_failures import terminate_native_run

    exporter, provider = exporter_and_provider

    class _Handle:
        def __init__(self):
            self.failed_with: tuple | None = None

        def cancel(self, reason):  # pragma: no cover — not this path
            raise AssertionError("failure must not cancel")

        def fail(self, message, *, error_type):
            self.failed_with = (message, error_type)

    handle = _Handle()
    with operation_span("inqtrix.run", None, tracer_provider=provider):
        error_type = terminate_native_run(handle, RuntimeError("boom"))
    assert handle.failed_with is not None
    assert error_type != "client_requested_cancel"
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert dict(span.attributes)["inqtrix.outcome"] == "failed"


def test_replay_path_posts_valid_lines_and_reports_failures(tmp_path):
    spool = tmp_path / "trace-spool-1-000001.otlp.jsonl"
    spool.write_text(
        json.dumps({"resourceSpans": []})
        + "\n"
        + "not-json\n"
        + json.dumps({"resourceSpans": []})
        + "\n"
    )
    calls: list[tuple[str, str]] = []

    def fake_post(url, *, content, headers):
        calls.append((url, content))
        assert headers["x-langfuse-ingestion-version"] == "4"
        return SimpleNamespace(status_code=207 if len(calls) == 2 else 200)

    sent, failed = replay_path(
        Path(tmp_path),
        endpoint="https://langfuse.local/api/public/otel",
        headers={"x-langfuse-ingestion-version": "4"},
        post=fake_post,
    )
    assert sent == 2
    assert failed == 1  # the junk line
    assert all(
        url == "https://langfuse.local/api/public/otel/v1/traces"
        for url, _ in calls
    )


class _FakeHttpxClient:
    """Captures the headers replay.main() builds; never talks HTTP."""

    last_headers: dict[str, str] | None = None

    def __init__(self, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, *, content, headers):
        _FakeHttpxClient.last_headers = dict(headers)
        return SimpleNamespace(status_code=200)


@pytest.fixture()
def replay_spool(tmp_path):
    spool = tmp_path / "trace-spool-1-000001.otlp.jsonl"
    spool.write_text(json.dumps({"resourceSpans": []}) + "\n")
    return tmp_path


def _run_replay_main(monkeypatch, replay_spool, argv_extra=()):
    import httpx

    from inqtrix.observability import replay as replay_module

    _FakeHttpxClient.last_headers = None
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)
    return replay_module.main(
        [
            str(replay_spool),
            "--endpoint",
            "https://langfuse.local/api/public/otel",
            *argv_extra,
        ]
    )


def test_replay_main_reads_auth_from_env(monkeypatch, replay_spool):
    monkeypatch.setenv("LANGFUSE_REPLAY_AUTH", "pk-lf-x:sk-lf-y")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    assert _run_replay_main(monkeypatch, replay_spool) == 0
    headers = _FakeHttpxClient.last_headers
    assert headers is not None
    import base64

    expected = base64.b64encode(b"pk-lf-x:sk-lf-y").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_replay_main_rejects_credentials_on_argv(monkeypatch, replay_spool):
    """Credentials come from the environment ONLY — argv is visible in ps
    and shell history, so the CLI must not accept an auth flag at all."""
    import pytest as _pytest

    monkeypatch.setenv("LANGFUSE_REPLAY_AUTH", "pk-env:sk-env")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    with _pytest.raises(SystemExit):
        _run_replay_main(monkeypatch, replay_spool, ("--auth", "pk:sk"))


# ------------------------------------------------------------------ #
# Run shape and totals as filterable trace metadata
# ------------------------------------------------------------------ #


def test_run_root_span_carries_filterable_metadata_not_usage(
    exporter_and_provider,
):
    """Langfuse drops model/usage/cost on invoke_agent spans, so the run
    total exported as gen_ai.usage.* never reached the trace (the trace
    sum missed 44 % of a Tief run). The total and the run shape now go
    to langfuse.trace.metadata.*, the one namespace Langfuse promotes to
    filterable trace metadata."""
    import threading

    from inqtrix.core.results import AgentResult
    from inqtrix.services.run_service import execute_run_request

    exporter, provider = exporter_and_provider

    class _Handle:
        run_id = "run-meta"
        parked = False

        def __init__(self) -> None:
            self.cancel_event = threading.Event()
            self.events: list = []

        def emit(self, event_type, payload) -> None:
            self.events.append((event_type, payload))

        def wait(self, _status) -> None:
            return

        def cancel(self, reason: str) -> None:  # pragma: no cover
            raise AssertionError(reason)

        def fail(self, message, *, error_type) -> None:  # pragma: no cover
            raise AssertionError(message)

        def total_elapsed_seconds(self) -> float:
            return 1.0

        def emit_answer(self, answer: str) -> None:
            return

        def complete(self, result, snapshot=None) -> None:
            self.result = result

    class _Algorithm:
        def run(self, _request, *, runtime, context) -> AgentResult:
            del runtime, context
            return AgentResult(
                answer="Done",
                raw={
                    "answer": "Done",
                    "usage": {"prompt_tokens": 1234, "completion_tokens": 56},
                    "result_state": {},
                },
            )

        def capabilities(self) -> dict[str, str]:
            return {"terminal_node": "answer"}

    handle = _Handle()
    with operation_span("inqtrix.run", None, tracer_provider=provider):
        execute_run_request(
            handle,
            algorithm=_Algorithm(),
            run_request=SimpleNamespace(mode="research", session_id="ses_1"),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(
                    depth="deep", agent_tier="tief", report_profile="compact"
                ),
                stack_name="azure",
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
        )
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["langfuse.trace.name"] == "run:research"
    assert attrs["langfuse.trace.metadata.mode"] == "research"
    assert attrs["langfuse.trace.metadata.depth"] == "deep"
    assert attrs["langfuse.trace.metadata.tier"] == "tief"
    assert attrs["langfuse.trace.metadata.profile"] == "compact"
    assert attrs["langfuse.trace.metadata.stack"] == "azure"
    assert attrs["langfuse.trace.metadata.usage_prompt_tokens"] == 1234
    assert attrs["langfuse.trace.metadata.usage_completion_tokens"] == 56
    # This engine keeps no cumulative count, so the number is one
    # segment's - and the metadata says so rather than calling it the run.
    assert attrs["langfuse.trace.metadata.usage_scope"] == "segment"
    assert attrs["inqtrix.outcome"] == "completed"
    # The dead usage keys are gone from the agent root (Langfuse skipped them).
    assert "gen_ai.usage.input_tokens" not in attrs
    assert "gen_ai.usage.output_tokens" not in attrs


def test_publication_and_completion_are_child_spans_of_the_run(recording):
    """The end of a run had no span of its own.

    Publishing the answer and storing the result ran under the bare run
    span (4 to 22 seconds in the acceptance traces), so nothing in the
    trace said where that time went.
    """
    import threading

    from inqtrix.core.results import AgentResult
    from inqtrix.services.run_service import execute_run_request

    class _Handle:
        run_id = "run-tail"
        parked = False

        def __init__(self) -> None:
            self.cancel_event = threading.Event()

        def emit(self, event_type, payload) -> None:
            return

        def wait(self, _status) -> None:
            return

        def total_elapsed_seconds(self) -> float:
            return 1.0

        def emit_answer(self, answer: str) -> None:
            return

        def complete(self, result, snapshot=None) -> None:
            return

    class _Algorithm:
        def run(self, _request, *, runtime, context) -> AgentResult:
            del runtime, context
            return AgentResult(
                answer="Größe",
                raw={"answer": "Größe", "usage": {}, "result_state": {}},
            )

        def capabilities(self) -> dict[str, str]:
            return {"terminal_node": "answer"}

    with operation_span("inqtrix.run", None):
        execute_run_request(
            _Handle(),
            algorithm=_Algorithm(),
            run_request=SimpleNamespace(mode="research", session_id=""),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(),
                stack_name="azure",
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
        )
    spans = {span.name: span for span in recording.get_finished_spans()}
    run = spans["inqtrix.run"]
    publish = spans["inqtrix.answer.publish"]
    complete = spans["inqtrix.run.complete"]
    assert publish.parent.span_id == run.context.span_id
    assert complete.parent.span_id == run.context.span_id
    assert publish.attributes["inqtrix.answer.bytes"] == len("Größe".encode("utf-8"))


def test_a_parked_runs_token_line_counts_what_its_name_says(
    exporter_and_provider,
):
    """A run with several segments reported its LAST one as the run total.

    ``usage`` is deliberately a segment delta: the quota books it after
    every segment, so a cumulative value there would be charged again for
    each earlier one. But a deep run parks - roles park their parent - so
    that delta is a part, and the trace field named for the run carried
    it. The quota was never wrong and Langfuse's own cost was never
    wrong; the one line a reader reads was.
    """
    import threading

    from inqtrix.core.results import AgentResult
    from inqtrix.services.run_service import execute_run_request

    exporter, provider = exporter_and_provider

    class _Handle:
        run_id = "run-parked"
        parked = False

        def __init__(self) -> None:
            self.cancel_event = threading.Event()
            self.events: list = []

        def emit(self, event_type, payload) -> None:
            self.events.append((event_type, payload))

        def wait(self, _status) -> None:
            return

        def total_elapsed_seconds(self) -> float:
            return 1.0

        def emit_answer(self, answer: str) -> None:
            return

        def complete(self, result, snapshot=None) -> None:
            self.result = result

    class _Algorithm:
        def run(self, _request, *, runtime, context) -> AgentResult:
            del runtime, context
            return AgentResult(
                answer="Done",
                raw={
                    "answer": "Done",
                    # The last segment alone ...
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                    # ... against what the run actually spent.
                    "result_state": {
                        "usage_total": {
                            "prompt_tokens": 9000,
                            "completion_tokens": 900,
                        }
                    },
                },
            )

        def capabilities(self) -> dict[str, str]:
            return {"terminal_node": "answer"}

    handle = _Handle()
    with operation_span("inqtrix.run", None, tracer_provider=provider):
        execute_run_request(
            handle,
            algorithm=_Algorithm(),
            run_request=SimpleNamespace(mode="research", session_id="ses_1"),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(
                    depth="deep", agent_tier="tief", report_profile="compact"
                ),
                stack_name="azure",
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
        )
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["langfuse.trace.metadata.usage_prompt_tokens"] == 9000
    assert attrs["langfuse.trace.metadata.usage_completion_tokens"] == 900
    assert attrs["langfuse.trace.metadata.usage_scope"] == "run"


def test_the_wire_payload_and_the_trace_report_the_same_number():
    """Two surfaces resolving the same question separately can disagree.

    The export payload and the trace metadata both answer "what did this
    run spend". Two copies of the same expression would be one copy too
    many for a rule that has an exception in it.
    """
    from inqtrix.result import (
        USAGE_SCOPE_RUN,
        USAGE_SCOPE_SEGMENT,
        reported_usage,
    )

    segment = {"prompt_tokens": 100, "completion_tokens": 10}
    total = {"prompt_tokens": 9000, "completion_tokens": 900}

    assert reported_usage({"usage": segment}, {"usage_total": total}) == (
        total,
        USAGE_SCOPE_RUN,
    )
    assert reported_usage({"usage": segment}, {}) == (
        segment,
        USAGE_SCOPE_SEGMENT,
    )


def test_a_role_child_span_names_its_profile_and_publishes_nothing(
    exporter_and_provider,
):
    """A role child shares its parent's trace: its span names the role, it
    leaves the trace shape alone, and its typed result never reaches the
    answer publisher."""
    import threading

    from inqtrix.core.results import AgentResult
    from inqtrix.services.run_service import execute_run_request

    exporter, provider = exporter_and_provider

    class _Handle:
        run_id = "run-role"
        parked = False

        def __init__(self) -> None:
            self.cancel_event = threading.Event()
            self.answers: list[str] = []

        def emit(self, event_type, payload) -> None:
            return

        def wait(self, _status) -> None:
            return

        def total_elapsed_seconds(self) -> float:
            return 1.0

        def emit_answer(self, answer: str) -> None:
            self.answers.append(answer)

        def complete(self, result, snapshot=None) -> None:
            self.result = result

    class _Algorithm:
        def run(self, _request, *, runtime, context) -> AgentResult:
            del runtime, context
            return AgentResult(
                answer="",
                raw={
                    "answer": "",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    "result_state": {
                        "work_result": {"profile": "review", "status": "completed", "value": {}},
                    },
                },
            )

        def capabilities(self) -> dict[str, str]:
            return {"terminal_node": "agent_kernel"}

    class _Publisher:
        def publish(self, *args, **kwargs):  # pragma: no cover - the failure mode
            raise AssertionError("a role run must not publish an answer")

    handle = _Handle()
    with operation_span("inqtrix.run", None, tracer_provider=provider):
        execute_run_request(
            handle,
            algorithm=_Algorithm(),
            run_request=SimpleNamespace(
                mode="agent_kernel", session_id="ses_1", work_profile="review", question="Review.",
            ),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(depth="normal", agent_tier="", report_profile=""),
                stack_name="azure",
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(quota=SimpleNamespace(max_tokens_per_run=0))
            ),
            principal=None,
            trace_root=False,
            answer_publisher=_Publisher(),
        )
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["inqtrix.work_profile"] == "review"
    assert "langfuse.trace.metadata.mode" not in attrs
    assert handle.answers == []
    assert handle.result["work_result"]["status"] == "completed"


def test_chat_root_span_exports_its_total_as_trace_metadata(
    exporter_and_provider,
):
    from inqtrix.observability.otel import chat_thread_call

    exporter, provider = exporter_and_provider
    result = SimpleNamespace(raw={"usage": {"prompt_tokens": 9, "completion_tokens": 3}})
    runner = chat_thread_call(
        lambda: result,
        mode="direct_chat",
        principal=SimpleNamespace(user_id="u1", tenant_id="t1"),
        tracer_provider=provider,
    )
    assert runner() is result
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["langfuse.trace.metadata.mode"] == "direct_chat"
    assert attrs["langfuse.trace.metadata.usage_prompt_tokens"] == 9
    assert attrs["langfuse.trace.metadata.usage_completion_tokens"] == 3
    assert "gen_ai.usage.input_tokens" not in attrs


def test_child_run_does_not_rewrite_the_trace_metadata(exporter_and_provider):
    """A child research run shares the parent's trace: it must not stamp
    its own mode/usage over the trace-level metadata of the kernel run
    (Langfuse keeps one metadata set per trace)."""
    import threading

    from inqtrix.core.results import AgentResult
    from inqtrix.services.run_service import execute_run_request

    exporter, provider = exporter_and_provider

    class _Handle:
        run_id = "run-child"
        parked = False

        def __init__(self) -> None:
            self.cancel_event = threading.Event()

        def emit(self, event_type, payload) -> None:
            return

        def wait(self, _status) -> None:
            return

        def total_elapsed_seconds(self) -> float:
            return 1.0

        def emit_answer(self, answer: str) -> None:
            return

        def complete(self, result, snapshot=None) -> None:
            return

    class _Algorithm:
        def run(self, _request, *, runtime, context) -> AgentResult:
            del runtime, context
            return AgentResult(
                answer="Done",
                raw={
                    "answer": "Done",
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                    "result_state": {},
                },
            )

        def capabilities(self) -> dict[str, str]:
            return {"terminal_node": "answer"}

    with operation_span("inqtrix.run", None, tracer_provider=provider):
        execute_run_request(
            _Handle(),
            algorithm=_Algorithm(),
            run_request=SimpleNamespace(mode="research", session_id=""),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(),
                stack_name="azure",
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
            trace_root=False,
        )
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["inqtrix.mode"] == "research"
    assert attrs["inqtrix.outcome"] == "completed"
    assert not [key for key in attrs if key.startswith("langfuse.trace.metadata.")]


@pytest.mark.parametrize(
    ("result_state", "expected_scope", "expected_prompt"),
    [
        ({}, "segment", 1234),
        (
            {"usage_total": {"prompt_tokens": 9000, "completion_tokens": 900}},
            "run",
            9000,
        ),
    ],
)
def test_the_exported_number_says_what_it_counts_like_the_trace_does(
    exporter_and_provider, result_state, expected_scope, expected_prompt
):
    """The exported number names its scope like the trace does.

    A scope on the trace but not on the wire is not enough - the wire is
    the surface a person exports into a Markdown file and reads a week
    later, where "is this the whole run or its last segment" can no
    longer be asked of anybody. Same resolution as the trace, so the two
    cannot answer differently; the scope named beside the number, so the
    number is not nameless.
    """
    import threading

    from inqtrix.core.results import AgentResult
    from inqtrix.services.run_service import execute_run_request

    exporter, provider = exporter_and_provider

    class _Handle:
        run_id = "run-export"
        parked = False
        result: dict | None = None

        def __init__(self) -> None:
            self.cancel_event = threading.Event()
            self.events: list = []

        def emit(self, event_type, payload) -> None:
            self.events.append((event_type, payload))

        def wait(self, _status) -> None:
            return

        def total_elapsed_seconds(self) -> float:
            return 1.0

        def emit_answer(self, answer: str) -> None:
            return

        def complete(self, result, snapshot=None) -> None:
            self.result = result

    class _Algorithm:
        def run(self, _request, *, runtime, context) -> AgentResult:
            del runtime, context
            return AgentResult(
                answer="Done",
                raw={
                    "answer": "Done",
                    "usage": {"prompt_tokens": 1234, "completion_tokens": 56},
                    "result_state": dict(result_state),
                },
            )

        def capabilities(self) -> dict[str, str]:
            return {"terminal_node": "answer"}

    handle = _Handle()
    with operation_span("inqtrix.run", None, tracer_provider=provider):
        execute_run_request(
            handle,
            algorithm=_Algorithm(),
            run_request=SimpleNamespace(mode="research", session_id="ses_1"),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(
                    depth="deep", agent_tier="tief", report_profile="compact"
                ),
                stack_name="azure",
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
        )

    (span,) = exporter.get_finished_spans()
    wire = (handle.result or {}).get("usage", {})
    assert wire["scope"] == expected_scope
    assert wire["prompt_tokens"] == expected_prompt
    assert dict(span.attributes)[
        "langfuse.trace.metadata.usage_scope"
    ] == wire["scope"]


def test_traced_thread_call_opens_its_span_as_a_child_of_the_request_span(
    exporter_and_provider,
):
    """Chat and session titles run in an executor thread; their span must
    join the request's trace instead of opening a trace of its own, so the
    access log's trace id leads to the model calls."""
    exporter, provider = exporter_and_provider

    with provider.get_tracer("test").start_as_current_span("POST /v1/chat/completions"):
        runner = traced_thread_call(
            "inqtrix.chat", None, lambda: "done", tracer_provider=provider
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(runner).result() == "done"

    spans = {span.name: span for span in exporter.get_finished_spans()}
    request = spans["POST /v1/chat/completions"]
    chat = spans["inqtrix.chat"]
    assert chat.parent is not None
    assert chat.parent.span_id == request.get_span_context().span_id
    assert chat.get_span_context().trace_id == request.get_span_context().trace_id


def test_a_run_span_names_the_rules_of_its_requirement_and_never_their_text(
    exporter_and_provider,
):
    """The trace shows WHICH rules at WHICH revision shaped a run and how
    large the composed requirement is — opaque ids and counts, recorded
    under any content policy. A rule's label and text are user content:
    they reach a trace only as captured prompt content, never as a span
    attribute."""
    import threading

    from inqtrix.services.run_service import execute_run_request

    exporter, provider = exporter_and_provider

    class _Stop(Exception):
        pass

    class _Handle:
        run_id = "run-rules"
        parked = False

        def __init__(self) -> None:
            self.cancel_event = threading.Event()

        def emit(self, event_type, payload) -> None:
            return

        def wait(self, _status) -> None:
            return

        def total_elapsed_seconds(self) -> float:
            return 1.0

    class _Algorithm:
        def run(self, _request, *, runtime, context):
            raise _Stop

        def capabilities(self) -> dict[str, str]:
            return {"terminal_node": "agent_kernel"}

    requirement = (
        "[Rule: Vorstand]\nGeheime Gliederung.\n[End rule: Vorstand]\n\n"
        "[Free requirement]\nNur intern."
    )

    def run(**request_fields) -> dict:
        with operation_span("inqtrix.run", None, tracer_provider=provider):
            with pytest.raises(_Stop):
                execute_run_request(
                    _Handle(),
                    algorithm=_Algorithm(),
                    run_request=SimpleNamespace(
                        mode="agent_kernel", session_id="ses_1", question="Q.",
                        **request_fields,
                    ),
                    resolved=SimpleNamespace(
                        providers=SimpleNamespace(),
                        strategies=SimpleNamespace(),
                        agent_settings=SimpleNamespace(
                            depth="normal", agent_tier="", report_profile=""
                        ),
                        stack_name="azure",
                    ),
                    runtime=SimpleNamespace(
                        settings=SimpleNamespace(
                            quota=SimpleNamespace(max_tokens_per_run=0)
                        )
                    ),
                    principal=None,
                    trace_root=False,
                    answer_publisher=None,
                )
        span = exporter.get_finished_spans()[-1]
        return dict(span.attributes)

    attrs = run(
        report_requirement=requirement,
        report_rules=(
            {"template_id": "pt_a", "label": "Vorstand", "revision": 3},
            {"template_id": "pt_b", "label": "Ton", "revision": 1},
        ),
    )
    assert attrs["inqtrix.requirement.rule_template_ids"] == ("pt_a", "pt_b")
    assert attrs["inqtrix.requirement.rule_revisions"] == (3, 1)
    assert attrs["inqtrix.requirement.composed_chars"] == len(requirement)
    recorded = json.dumps({key: list(value) if isinstance(value, tuple) else value
                           for key, value in attrs.items()})
    for user_text in ("Vorstand", "Geheime Gliederung", "Nur intern", "Ton"):
        assert user_text not in recorded, user_text
    # Typed text alone: the size, no rule attributes.
    typed_only = run(report_requirement="[Free requirement]\nKurz.", report_rules=())
    assert typed_only["inqtrix.requirement.composed_chars"] == len(
        "[Free requirement]\nKurz."
    )
    assert "inqtrix.requirement.rule_template_ids" not in typed_only
    # No requirement, no requirement attributes — also for a request
    # double that predates the fields.
    plain = run()
    assert not [key for key in plain if key.startswith("inqtrix.requirement.")]
