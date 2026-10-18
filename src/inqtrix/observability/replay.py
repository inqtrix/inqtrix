"""Replay spooled traces into an OTLP/HTTP-JSON endpoint (Langfuse).

The Baukasten counterpart to ``INQTRIX_TRACING=file``: a deployment
records full traces WITHOUT a backend, and this CLI imports them later —
into a permanent Langfuse, or one started temporarily just for an
investigation. Each spool line is one ``ExportTraceServiceRequest`` in
the official protobuf JSON mapping, POSTed verbatim.

Usage::

    # keys sourced from the secrets file, not typed on the command line
    set -a; . deploy/.secrets/…/.env.stack.secrets; set +a
    python -m inqtrix.observability.replay logs/traces \
        --endpoint https://langfuse.example/api/public/otel

``--endpoint`` falls back to ``OTEL_EXPORTER_OTLP_ENDPOINT``. Project
keys come from ``LANGFUSE_REPLAY_AUTH`` (``pk:sk``, becomes Basic auth)
or from ``OTEL_EXPORTER_OTLP_HEADERS`` (comma-separated ``Name=Value``
pairs, the standard OTel format). ``--auth pk:sk`` also works but is
discouraged: argv is visible in ``ps`` output and shell history — and a
literal ``VAR=… python …`` prefix would land in shell history as well,
hence the source-from-file example above. The Langfuse real-time header
``x-langfuse-ingestion-version: 4`` is always added unless the caller
supplied one.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable


def _iter_spool_files(target: Path) -> Iterable[Path]:
    if target.is_dir():
        yield from sorted(target.glob("trace-spool-*.otlp.jsonl"))
        return
    yield target


def _headers_from_env() -> dict[str, str]:
    """The ONE OTLP header parser (signal-specific precedence + decode).

    Shared with the admin export, so the very same OTEL_EXPORTER_OTLP_*
    configuration authenticates in both the admin export and the replay
    CLI; a private copy here would let the two drift apart.
    """
    from inqtrix.observability.trace_readers import _headers_from_otlp_env

    return _headers_from_otlp_env()


def _traces_url(endpoint: str) -> str:
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith("/v1/traces"):
        return trimmed
    return f"{trimmed}/v1/traces"


def replay_path(
    target: Path,
    *,
    endpoint: str,
    headers: dict[str, str],
    post: Callable[..., Any],
) -> tuple[int, int]:
    """POST every spool line to *endpoint*; returns (sent, failed).

    ``post`` is ``httpx.Client.post``-shaped and injectable for tests.
    Invalid lines and rejected batches are reported but never abort the
    replay — a partially imported spool is more useful than none.
    """
    url = _traces_url(endpoint)
    sent = 0
    failed = 0
    for spool_file in _iter_spool_files(target):
        for line_number, line in enumerate(
            spool_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError:
                failed += 1
                print(
                    f"UEBERSPRUNGEN {spool_file.name}:{line_number} "
                    "(keine gueltige JSON-Zeile)",
                    file=sys.stderr,
                )
                continue
            response = post(url, content=line, headers=headers)
            status = getattr(response, "status_code", 0)
            if 200 <= status < 300:
                sent += 1
            else:
                failed += 1
                print(
                    f"FEHLER {spool_file.name}:{line_number} -> "
                    f"HTTP {status}",
                    file=sys.stderr,
                )
    return sent, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m inqtrix.observability.replay",
        description=(
            "Spool-Traces (OTLP-JSON-Zeilen) in einen OTLP/HTTP-JSON-"
            "Endpoint importieren, z.B. Langfuse /api/public/otel."
        ),
    )
    parser.add_argument(
        "path",
        help="Spool-Datei oder -Verzeichnis (Default-Verzeichnis: logs/traces)",
    )
    parser.add_argument(
        "--endpoint",
        default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        help=(
            "OTLP-Basis-Endpoint (Default: OTEL_EXPORTER_OTLP_ENDPOINT); "
            "/v1/traces wird ergaenzt, falls es fehlt."
        ),
    )
    arguments = parser.parse_args(argv)
    if not arguments.endpoint:
        parser.error(
            "No endpoint: set --endpoint or export OTEL_EXPORTER_OTLP_ENDPOINT."
        )

    headers = _headers_from_env()
    # Credentials come from the environment only: argv is visible in ps
    # and shell history, so there is deliberately no --auth flag.
    auth = os.getenv("LANGFUSE_REPLAY_AUTH", "")
    if auth:
        token = base64.b64encode(auth.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    headers.setdefault("x-langfuse-ingestion-version", "4")
    headers.setdefault("Content-Type", "application/json")

    import httpx

    with httpx.Client(timeout=30.0) as client:
        sent, failed = replay_path(
            Path(arguments.path),
            endpoint=arguments.endpoint,
            headers=headers,
            post=client.post,
        )
    print(f"Replay fertig: {sent} Batches importiert, {failed} Fehler.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
