"""What an operator sees when a service process ends at an unhandled exception.

The process hook keeps the exception projected (type, code, frames) in ``exc``
and adds a summary of the chain to the message: the message of an exception
that Inqtrix code raised itself, scrubbed; a foreign exception only as type and
code; a pydantic validation error by type, location and the schema message,
never the input.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from pydantic import BaseModel, ValidationError, field_validator

import inqtrix
from inqtrix import logging_config

MARKER = "MARKER-5e1b"
_PACKAGE_PROBE_FILE = str(Path(inqtrix.__file__).resolve().parent / "_exit_probe.py")


def _configure(**kwargs: Any) -> Any:
    kwargs.setdefault("service", "api")
    return logging_config.configure_logging(**kwargs)


def _json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def _inqtrix_code(source: str) -> dict[str, Any]:
    """Functions whose frames report a file inside the installed package."""

    namespace: dict[str, Any] = {}
    exec(compile(source, _PACKAGE_PROBE_FILE, "exec"), namespace)  # noqa: S102 - test probe
    return namespace


def _raised(call: Callable[[], Any]) -> BaseException:
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the probe returns what it caught
        return exc
    raise AssertionError("the call did not raise")


def _process_exit(exc: BaseException) -> None:
    sys.excepthook(type(exc), exc, exc.__traceback__)


def _critical(out: str) -> dict[str, Any]:
    (line,) = [line for line in _json_lines(out) if line["level"] == "CRITICAL"]
    return line


def _tls_error() -> BaseException:
    from inqtrix.server.security import resolve_tls_paths

    return _raised(
        lambda: resolve_tls_paths(SimpleNamespace(tls_keyfile="key.pem", tls_certfile=""))
    )


def test_an_inqtrix_raise_shows_its_message_at_process_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    _process_exit(_tls_error())

    line = _critical(capsys.readouterr().out)
    assert "must be set together" in line["message"]
    assert "RuntimeError" in line["exc"]
    assert "must be set together" not in line["exc"]


def test_a_foreign_message_never_shows(capsys: pytest.CaptureFixture[str]) -> None:
    # ``ipaddress`` raises with its own ``raise`` and quotes the value.
    _configure()
    _process_exit(_raised(lambda: ipaddress.ip_address(MARKER)))

    out = capsys.readouterr().out
    assert MARKER not in out
    assert _critical(out)["message"] == "Unhandled exception; the process stops: ValueError"


def test_a_builtin_called_from_inqtrix_code_keeps_its_message_hidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _inqtrix_code("def parse(value):\n    return int(value)\n")
    _configure()
    _process_exit(_raised(lambda: probe["parse"](MARKER)))

    out = capsys.readouterr().out
    assert MARKER not in out
    assert "ValueError" in _critical(out)["message"]


@pytest.mark.parametrize("target", ["console", "none"])
def test_a_shown_message_is_scrubbed(capsys: pytest.CaptureFixture[str], target: str) -> None:
    # With ``none`` the summary goes straight to stderr, past the log filter.
    probe = _inqtrix_code(
        "def connect():\n"
        "    raise RuntimeError('cannot reach postgresql://app:Se/cr3tPw@db/inqtrix')\n"
    )
    _configure(target=target)
    _process_exit(_raised(probe["connect"]))

    captured = capsys.readouterr()
    assert "cr3tPw" not in captured.out + captured.err
    assert "cannot reach postgresql://[REDACTED]@db/inqtrix" in captured.out + captured.err


def test_a_foreign_cause_shows_only_its_type(capsys: pytest.CaptureFixture[str]) -> None:
    probe = _inqtrix_code(
        "import ipaddress\n"
        "def bind(address):\n"
        "    try:\n"
        "        ipaddress.ip_address(address)\n"
        "    except ValueError as exc:\n"
        "        raise RuntimeError('the bind address is not an IP address') from exc\n"
    )
    _configure()
    _process_exit(_raised(lambda: probe["bind"](MARKER)))

    out = capsys.readouterr().out
    assert _critical(out)["message"] == (
        "Unhandled exception; the process stops: RuntimeError: the bind address is not an "
        "IP address; caused by ValueError"
    )
    assert MARKER not in out


def test_validation_errors_name_type_and_field_without_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from inqtrix.settings import AgentSettings

    _configure()
    _process_exit(_raised(lambda: AgentSettings(max_rounds=MARKER)))

    out = capsys.readouterr().out
    message = _critical(out)["message"]
    assert "int_parsing" in message
    assert "max_rounds" in message
    assert MARKER not in out


def test_an_inqtrix_model_validator_message_shows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from inqtrix.settings import StorageSettings

    _configure()
    _process_exit(_raised(lambda: StorageSettings(object_store_backend="s3", s3_bucket="")))

    assert "INQTRIX_S3_BUCKET must not be blank" in _critical(capsys.readouterr().out)["message"]


def test_a_foreign_validator_message_stays_hidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Probe(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _check(cls, value: str) -> str:
            raise ValueError(f"rejected {value}")

    _configure()
    _process_exit(_raised(lambda: _Probe(name=MARKER)))

    out = capsys.readouterr().out
    message = _critical(out)["message"]
    assert "value_error" in message
    assert "name" in message
    assert MARKER not in out
    assert "rejected" not in out


def test_the_model_tier_message_names_no_value() -> None:
    from inqtrix.settings import AgentSettings

    with pytest.raises(ValidationError) as error:
        AgentSettings(model_tier=MARKER)

    (detail,) = error.value.errors(include_input=False)
    assert "model_tier" in detail["msg"]
    assert MARKER not in detail["msg"]


def test_the_none_target_writes_the_summary_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(target="none")
    _process_exit(_tls_error())

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be set together" in captured.err


def test_a_retired_setting_is_visible_with_the_none_target(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from inqtrix.settings import check_retired_settings

    monkeypatch.setenv("INQTRIX_LOG_ENABLED", MARKER)
    _configure(target="none")
    _process_exit(_raised(check_retired_settings))

    captured = capsys.readouterr()
    assert "Retired setting INQTRIX_LOG_ENABLED is set; use INQTRIX_LOG_TARGET" in captured.err
    assert MARKER not in captured.err + captured.out


def test_a_thread_crash_stays_projected(capsys: pytest.CaptureFixture[str]) -> None:
    probe = _inqtrix_code(
        "def work(marker):\n    raise RuntimeError('job detail ' + marker)\n"
    )
    _configure()
    worker = threading.Thread(target=probe["work"], args=(MARKER,), name="probe-thread")
    worker.start()
    worker.join()

    out = capsys.readouterr().out
    (line,) = [line for line in _json_lines(out) if line["level"] == "ERROR"]
    assert line["message"] == "Unhandled exception in thread probe-thread."
    assert MARKER not in out


def test_errno_is_a_stable_code(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    logging.getLogger("uvicorn.error").error(OSError(98, f"Address already in use {MARKER}"))

    out = capsys.readouterr().out
    (line,) = _json_lines(out)
    assert line["message"] == "OSError[errno=98]"
    assert MARKER not in out


@pytest.mark.parametrize("attribute", ["code", "error_code", "status_code", "sqlstate", "errno"])
def test_projecting_a_projected_traceback_keeps_its_code(attribute: str) -> None:
    def _raise() -> None:
        exc = OSError(98, "in use") if attribute == "errno" else RuntimeError("detail")
        if attribute != "errno":
            setattr(exc, attribute, "E42")
        raise exc

    projected = logging_config.project_exception(_raised(_raise))[1]

    assert logging_config._project_preformatted_traceback(projected) == projected


@pytest.mark.parametrize("field", ["target", "max_bytes"])
def test_invalid_logging_settings_name_no_input(field: str) -> None:
    with pytest.raises(ValueError) as error:
        logging_config.configure_logging(service="api", **{field: MARKER})

    # The target validator lowercases its input before it rejects it.
    assert MARKER.lower() not in str(error.value).lower()
    assert f"@{field}" in str(error.value)
    assert error.value.__suppress_context__ is True


def test_an_invalid_api_port_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    import inqtrix.__main__ as server_main

    monkeypatch.setattr(server_main, "configure_logging", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(server_main, "log_status_line", lambda *args, **kwargs: None)
    monkeypatch.setattr(server_main, "check_retired_settings", lambda **kwargs: [])
    monkeypatch.setattr(
        server_main,
        "Settings",
        lambda: SimpleNamespace(collaboration=SimpleNamespace(max_frame_bytes=1, max_queued_frames=2)),
    )
    monkeypatch.setattr(server_main, "create_app", lambda settings: "app")
    monkeypatch.setattr(server_main.uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setenv("INQTRIX_SERVER_PORT", MARKER)

    with pytest.raises(RuntimeError, match="INQTRIX_SERVER_PORT must be an integer") as error:
        server_main.main()

    assert MARKER not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_the_worker_contract_error_line_carries_no_cause_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from inqtrix.worker import __main__ as worker_main

    def _guard() -> None:
        raise RuntimeError(f"contract violated {MARKER}")

    with caplog.at_level(logging.ERROR, logger="inqtrix"):
        with pytest.raises(RuntimeError):
            worker_main._verified_database_claim_contract(_guard, sleep=lambda seconds: None)

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert errors
    assert all(MARKER not in record.getMessage() for record in errors)
