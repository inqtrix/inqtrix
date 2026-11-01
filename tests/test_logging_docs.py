"""The examples the logging documentation gives for an application's own logger.

``docs/observability/logging.md`` points a reader who keeps their own logging
at the scripts under ``examples/logging/``. The standard-library and the
OpenTelemetry script run here as separate processes, the way a reader starts
them, so the logging they set up never reaches this test process. Their
output must show the correlation fields on Inqtrix records, an exception of
an Inqtrix record only as type and frames (its message quotes the user's
question) and no known secret shape. structlog and loguru are no dependencies
of Inqtrix, so those two scripts are only compiled and read: each must attach
``InqtrixLogFilter`` to the handler that renders or forwards the records of
the standard library, and to nothing else.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples" / "logging"
LOGGING_PAGE = REPO_ROOT / "docs" / "observability" / "logging.md"

# The values the runnable examples bind and log. They are spelled out here
# instead of being read from the scripts, so a changed script cannot move the
# expectation with it.
REQUEST_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
RUN_ID = "3f6b2c1e-9a4d-4e8b-b1c2-5d7e9f0a1b2c"
QUESTION_WORD = "ACME-4711"
DB_PASSWORD = "s3cret-Passw0rd"
# The descriptor InqtrixLogFilter writes for the example's provider error:
# type and stable code, never the message.
DESCRIPTOR = "ProviderError[status_code=429]"
SCRUBBED_DB_LINE = "Database connection to postgresql://[REDACTED]@db.internal:5432/acme failed; retrying."
# The examples bind a raw tenant id; a record carries only its pseudonym.
TENANT_PSEUDONYM = re.compile(r"ten_[0-9a-f]{16}")


@dataclass(frozen=True)
class ExampleRun:
    returncode: int
    stdout: str
    stderr: str

    def records(self) -> list[dict[str, Any]]:
        assert self.returncode == 0, self.stderr
        assert self.stderr == ""
        return [json.loads(line) for line in self.stdout.splitlines() if line.strip()]


def _run_example(name: str, folder: Path) -> ExampleRun:
    """Start the script as a reader does, without this machine's ``.env`` or settings."""

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(folder),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }
    completed = subprocess.run(
        [sys.executable, str(EXAMPLES / name)],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return ExampleRun(completed.returncode, completed.stdout, completed.stderr)


@pytest.fixture(scope="module")
def stdlib_run(tmp_path_factory: pytest.TempPathFactory) -> ExampleRun:
    return _run_example("stdlib_json_lines.py", tmp_path_factory.mktemp("stdlib"))


@pytest.fixture(scope="module")
def otel_run(tmp_path_factory: pytest.TempPathFactory) -> ExampleRun:
    return _run_example("opentelemetry_logs.py", tmp_path_factory.mktemp("otel"))


def _only(records: list[dict[str, Any]], **expected: Any) -> dict[str, Any]:
    matches = [
        record
        for record in records
        if all(record.get(key) == value for key, value in expected.items())
    ]
    assert len(matches) == 1, f"expected one record with {expected}, got {len(matches)}: {records}"
    return matches[0]


def _projected_traceback(text: str) -> None:
    lines = text.splitlines()
    assert lines[0] == "Traceback (most recent call last):"
    assert lines[-1] == DESCRIPTOR
    assert lines[1:-1] and all(line.startswith('  File "') for line in lines[1:-1])


# --------------------------------------------------------------------- #
# Standard library: own handler, own JSON formatter
# --------------------------------------------------------------------- #


def test_stdlib_example_stamps_the_correlation_fields_on_inqtrix_records(
    stdlib_run: ExampleRun,
) -> None:
    records = [r for r in stdlib_run.records() if r["logger"].startswith("inqtrix.")]

    assert len(records) == 2
    for record in records:
        assert record.get("request_id") == REQUEST_ID
        assert record.get("run_id") == RUN_ID
        assert TENANT_PSEUDONYM.fullmatch(str(record.get("tenant")))


def test_stdlib_example_shows_an_inqtrix_exception_as_type_and_frames_only(
    stdlib_run: ExampleRun,
) -> None:
    records = stdlib_run.records()
    failure = _only(records, logger="inqtrix.example", severity="WARNING")

    assert failure["message"] == f"Provider call failed: {DESCRIPTOR}"
    _projected_traceback(failure["exception"])
    assert QUESTION_WORD not in stdlib_run.stdout + stdlib_run.stderr


def test_stdlib_example_scrubs_secret_shapes_from_application_records(
    stdlib_run: ExampleRun,
) -> None:
    records = stdlib_run.records()

    assert _only(records, logger="acme.storage")["message"] == SCRUBBED_DB_LINE
    assert DB_PASSWORD not in stdlib_run.stdout + stdlib_run.stderr


# --------------------------------------------------------------------- #
# OpenTelemetry logs: the SDK's LoggingHandler, console exporter
# --------------------------------------------------------------------- #


def test_opentelemetry_example_exports_the_correlation_fields_as_attributes(
    otel_run: ExampleRun,
) -> None:
    started = _only(otel_run.records(), body=f"Run {RUN_ID} segment started (mode=research).")
    attributes = started["attributes"]

    assert attributes.get("request_id") == REQUEST_ID
    assert attributes.get("run_id") == RUN_ID
    assert TENANT_PSEUDONYM.fullmatch(str(attributes.get("tenant")))
    # The ids the filter stamps are those of the span the record belongs to.
    assert started["trace_id"] == f"0x{attributes.get('trace_id')}"
    assert started["span_id"] == f"0x{attributes.get('span_id')}"
    assert started["trace_id"] != "0x" + "0" * 32


def test_opentelemetry_example_exports_no_attribute_beyond_the_documented_ones(
    otel_run: ExampleRun,
) -> None:
    # The handler exports every attribute a record carries, so whatever the
    # filter leaves on a record arrives at the collector.
    documented = {
        "service",
        "request_id",
        "run_id",
        "user",
        "tenant",
        "trace_id",
        "span_id",
        "code.file.path",
        "code.function.name",
        "code.line.number",
        "exception.stacktrace",
    }

    records = otel_run.records()

    assert records
    for record in records:
        assert set(record["attributes"]) <= documented


def test_opentelemetry_example_exports_an_inqtrix_exception_as_type_and_frames_only(
    otel_run: ExampleRun,
) -> None:
    failure = _only(otel_run.records(), body=f"Provider call failed: {DESCRIPTOR}")
    attributes = failure["attributes"]

    _projected_traceback(attributes["exception.stacktrace"])
    assert "exception.message" not in attributes
    assert QUESTION_WORD not in otel_run.stdout + otel_run.stderr


def test_opentelemetry_example_scrubs_secret_shapes_from_application_records(
    otel_run: ExampleRun,
) -> None:
    _only(otel_run.records(), body=SCRUBBED_DB_LINE)

    assert DB_PASSWORD not in otel_run.stdout + otel_run.stderr


# --------------------------------------------------------------------- #
# structlog and loguru: compiled and read, not run
# --------------------------------------------------------------------- #


def _module(name: str) -> ast.Module:
    path = EXAMPLES / name
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec", dont_inherit=True)
    return ast.parse(source, filename=str(path))


def _calls(tree: ast.AST, method: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    ]


def _filter_receivers(tree: ast.AST) -> list[str]:
    """Everything ``InqtrixLogFilter()`` is added to, as source text."""

    return [
        ast.unparse(call.func.value)
        for call in _calls(tree, "addFilter")
        if len(call.args) == 1
        and isinstance(call.args[0], ast.Call)
        and ast.unparse(call.args[0].func) == "InqtrixLogFilter"
    ]


def _imports_the_filter(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "inqtrix"
        and any(alias.name == "InqtrixLogFilter" and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )


def _is_root_logger(node: ast.expr, root_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in root_names
    return ast.unparse(node) == "logging.getLogger()"


def _installed_on_root(tree: ast.AST, handler: str) -> bool:
    """Whether *handler* becomes a handler of the root logger."""

    root_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and ast.unparse(node.value) == "logging.getLogger()"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    added = any(
        _is_root_logger(call.func.value, root_names)
        and [ast.unparse(arg) for arg in call.args] == [handler]
        for call in _calls(tree, "addHandler")
    )
    configured = any(
        keyword.arg == "handlers"
        and isinstance(keyword.value, (ast.List, ast.Tuple))
        and handler in [ast.unparse(item) for item in keyword.value.elts]
        for call in _calls(tree, "basicConfig")
        if ast.unparse(call.func.value) == "logging"
        for keyword in call.keywords
    )
    return added or configured


def _base_name(node: ast.expr) -> str | None:
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Name):
            return node.id
        else:
            return None


def test_structlog_example_filters_the_handler_that_renders_standard_records() -> None:
    tree = _module("structlog_processor_formatter.py")

    assert _imports_the_filter(tree)
    assert _filter_receivers(tree) == ["handler"]
    renders = [
        call
        for call in _calls(tree, "setFormatter")
        if ast.unparse(call.func.value) == "handler"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Call)
        and ast.unparse(call.args[0].func) == "structlog.stdlib.ProcessorFormatter"
    ]
    assert len(renders) == 1
    assert _installed_on_root(tree, "handler")
    # structlog writes its own events itself: handed to the filtered handler,
    # their event dictionary would arrive as rendered text.
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "wrap_for_formatter" not in attributes
    assert "LoggerFactory" not in attributes


def test_loguru_example_filters_the_intercept_handler() -> None:
    tree = _module("loguru_intercept.py")

    assert _imports_the_filter(tree)
    assert _filter_receivers(tree) == ["handler"]
    (created,) = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and [ast.unparse(target) for target in node.targets] == ["handler"]
    ]
    assert isinstance(created, ast.Call) and isinstance(created.func, ast.Name)
    (intercept,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == created.func.id
    ]
    assert [ast.unparse(base) for base in intercept.bases] == ["logging.Handler"]
    (emit,) = [
        node
        for node in intercept.body
        if isinstance(node, ast.FunctionDef) and node.name == "emit"
    ]
    assert any(_base_name(call.func) == "logger" for call in _calls(emit, "log"))
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "loguru"
        and [alias.name for alias in node.names] == ["logger"]
        for node in ast.walk(tree)
    )
    assert _installed_on_root(tree, "handler")


# --------------------------------------------------------------------- #
# The page names every example
# --------------------------------------------------------------------- #


def test_the_logging_page_names_every_logging_example() -> None:
    page = LOGGING_PAGE.read_text(encoding="utf-8")
    examples = sorted(path.name for path in EXAMPLES.iterdir() if path.suffix in {".py", ".yaml"})

    assert examples, "examples/logging/ holds no example"
    missing = [name for name in examples if f"examples/logging/{name}" not in page]
    assert not missing, f"docs/observability/logging.md never names: {missing}"
