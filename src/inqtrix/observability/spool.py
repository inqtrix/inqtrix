"""File spool for spans (``INQTRIX_TRACING=file``).

The Baukasten mode between "nothing" and "live backend": spans are
written as **standard OTLP-JSON** lines into rotating files, so a
deployment records full debug depth WITHOUT running Langfuse — and can
import the files later (each line is one ``ExportTraceServiceRequest``
rendered with the official protobuf JSON mapping, i.e. directly
POST-able to any OTLP/HTTP-JSON endpoint such as Langfuse's
``/api/public/otel/v1/traces``).

Retention here is SIZE-based (the plan's 2-GiB backstop): one file per
writer, rotated at a fixed slice size, oldest files deleted once the
directory exceeds the configured total. Time-based retention is the
backend's job in ``otlp`` mode, not the spool's.

Shared-directory safety: several processes (host-mode API + worker) may
spool into the same directory. Every writer owns files it created in
this incarnation (pid + random token in the name, created O_EXCL) and
never appends to anyone else's. The total cap counts the WHOLE
directory; foreign files are protected only while fresh — once nothing
has written to them for :data:`_FOREIGN_STALE_SECONDS` their writer is
gone or idle and they become reclaimable, so dead processes can never
grow the directory beyond the cap forever.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover — typing only
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExportResult

log = logging.getLogger("inqtrix")

# One spool file grows to this size before rotating to a fresh one;
# small enough that the total-cap cleanup has usable granularity.
_SLICE_MAX_BYTES = 64 * 1024 * 1024
_SPOOL_PREFIX = "trace-spool-"
_SPOOL_SUFFIX = ".otlp.jsonl"
# A foreign spool file untouched for this long is considered abandoned
# (its writer exited or idles); only then may the cap delete it. An
# actively written file keeps a fresh mtime and stays protected.
_FOREIGN_STALE_SECONDS = 600.0


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _spool_files(directory: Path) -> list[Path]:
    """All spool files, oldest-first BY MTIME.

    Mtime order is robust where lexicographic name order is not (PIDs of
    unequal digit width, random writer tokens, wrapped sequences).
    """
    files = list(directory.glob(f"{_SPOOL_PREFIX}*{_SPOOL_SUFFIX}"))
    return sorted(files, key=_safe_mtime)


def build_spool_exporter(directory: str, max_total_mb: int):
    """Build the spool ``SpanExporter`` (import-guarded factory).

    Kept behind a factory so importing this module never requires the
    ``observability`` extra; only actually building the exporter does.
    """
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import (
        encode_spans,
    )
    from opentelemetry.sdk.trace.export import (
        SpanExporter,
        SpanExportResult,
    )

    try:
        from google.protobuf.json_format import MessageToDict
    except Exception as exc:  # pragma: no cover — proto ships with the extra
        raise RuntimeError(
            "OTLP-JSON spool requires protobuf (part of the "
            "observability extra)"
        ) from exc

    class SpoolSpanExporter(SpanExporter):
        """Append OTLP-JSON lines to rotating, size-capped spool files."""

        def __init__(self) -> None:
            self._dir = Path(directory)
            self._dir.mkdir(parents=True, exist_ok=True)
            self._max_total_bytes = max_total_mb * 1024 * 1024
            self._lock = threading.Lock()
            self._sequence = 0
            self._current: Path | None = None
            # pid alone is NOT a stable writer identity (containers are
            # always PID 1; hosts recycle pids) — the random token makes
            # this incarnation's files unambiguous.
            self._pid = os.getpid()
            self._token = secrets.token_hex(4)
            # Streak flag: one WARNING per failure streak, not per batch.
            self._export_failing = False
            # Spans can carry forensic prompts/responses (content
            # capture), so take the directory away from OTHER — but keep
            # the group bits. Containers run as an arbitrary non-root UID
            # in group 0 and write through the GROUP permission; forcing
            # 0700 there would lock the process out of its own spool
            # (the image pre-creates the mount point group-writable for
            # exactly this reason).
            try:
                current = stat.S_IMODE(self._dir.stat().st_mode)
                os.chmod(self._dir, current & ~stat.S_IRWXO)
            except OSError:  # noqa: BLE001 — best effort on odd filesystems
                # Fail-safe, never fail-SILENT: forensic content in a
                # directory whose permissions could not be tightened is
                # something the operator must know about.
                log.warning(
                    "Could not set trace spool directory %s to mode 0700.",
                    self._dir,
                    exc_info=True,
                )

        def _own_prefix(self) -> str:
            return f"{_SPOOL_PREFIX}{self._pid}-{self._token}-"

        def _next_file(self) -> Path:
            # O_EXCL: this writer only ever appends to files it CREATED,
            # with 0600 from the first byte. A name collision (pid reuse,
            # two PID-1 containers on one volume) bumps the sequence
            # instead of appending into a foreign or legacy-mode file.
            while True:
                self._sequence += 1
                path = self._dir / (
                    f"{self._own_prefix()}{self._sequence:06d}"
                    f"{_SPOOL_SUFFIX}"
                )
                try:
                    os.close(
                        os.open(
                            path,
                            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                            # Group-readable on purpose: the API process
                            # reads the WORKER's slices through the
                            # shared spool volume for the trace export,
                            # and hardened runtimes give the two pods
                            # different UIDs in the same group 0. Owner
                            # + group only — never other.
                            0o640,
                        )
                    )
                    return path
                except FileExistsError:  # noqa: PERF203 — rare collision
                    continue
                except OSError:
                    # Odd filesystem: fall back to plain append-create —
                    # loudly, because the 0600-from-first-byte guarantee
                    # is gone for this slice.
                    log.warning(
                        "Trace spool slice %s created without O_EXCL/0600 due to "
                        "filesystem limitations.",
                        path.name,
                        exc_info=True,
                    )
                    return path

        def _enforce_total_cap(self) -> None:
            # The cap is the documented DIRECTORY total. Own files are
            # always reclaimable (oldest first, never the live slice);
            # foreign files only once stale — a live writer keeps its
            # slice fresh, a dead one must not leak disk forever.
            files = _spool_files(self._dir)
            total = sum(_safe_size(f) for f in files)
            if total <= self._max_total_bytes:
                return
            now = time.time()
            own_prefix = self._own_prefix()
            for candidate in files:
                if total <= self._max_total_bytes:
                    break
                if candidate == self._current:
                    # Never delete the file we are writing; the cap is a
                    # backstop, not a hard guarantee within one slice.
                    continue
                if (
                    not candidate.name.startswith(own_prefix)
                    and now - _safe_mtime(candidate)
                    < _FOREIGN_STALE_SECONDS
                ):
                    # Another live process may be appending — protected
                    # while fresh.
                    continue
                try:
                    size = candidate.stat().st_size
                    candidate.unlink()
                    total -= size
                    log.warning(
                        "Trace spool capacity reached; deleted oldest file %s.",
                        candidate.name,
                    )
                except OSError:  # noqa: PERF203 — best-effort cleanup
                    continue

        def export(
            self, spans: Sequence["ReadableSpan"]
        ) -> "SpanExportResult":
            if not spans:
                return SpanExportResult.SUCCESS
            try:
                request = encode_spans(spans)
                line = json.dumps(
                    MessageToDict(request), ensure_ascii=False
                )
                with self._lock:
                    # A fork inherits self._current; children must write
                    # their own files (no concurrent append) under a
                    # fresh identity.
                    if os.getpid() != self._pid:
                        self._pid = os.getpid()
                        self._token = secrets.token_hex(4)
                        self._current = None
                    if (
                        self._current is None
                        or not self._current.exists()
                        or self._current.stat().st_size >= _SLICE_MAX_BYTES
                    ):
                        self._current = self._next_file()
                    with self._current.open("a", encoding="utf-8") as sink:
                        sink.write(line + "\n")
                    self._enforce_total_cap()
            except Exception as exc:  # noqa: BLE001 — never crash the app
                # The batch processor retries every few seconds, so a
                # full or read-only volume would otherwise write one
                # traceback per interval for the rest of the process
                # life — onto the disk that is already full. Report the
                # START of a failure streak with the traceback, then
                # stay quiet until it recovers (which is reported too).
                if not self._export_failing:
                    self._export_failing = True
                    log.warning(
                        "Trace spool export failed (%s); further identical failures are"
                        " suppressed until recovery.",
                        type(exc).__name__,
                        exc_info=True,
                    )
                return SpanExportResult.FAILURE
            if self._export_failing:
                self._export_failing = False
                log.info("Trace spool export recovered.")
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

    return SpoolSpanExporter()
