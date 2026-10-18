"""File I/O functions and HTTP request helpers for parity tooling."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from inqtrix.parity._types import (
    DEFAULT_BASELINES_DIR,
    DEFAULT_REPORTS_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_TIMEOUT,
    HEALTH_ENDPOINT,
    TEST_ENDPOINT,
    QuestionSpec,
)


def load_questions(path: Path | str) -> list[QuestionSpec]:
    """Load canonical question specifications from a JSON file."""
    question_path = Path(path)
    with question_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    raw_questions = payload.get("questions", [])
    questions: list[QuestionSpec] = []
    for item in raw_questions:
        questions.append(
            QuestionSpec(
                id=str(item.get("id", "")).strip(),
                question=str(item.get("question", "")).strip(),
                category=str(item.get("category", "")).strip(),
                expected_keywords=tuple(
                    str(keyword).strip()
                    for keyword in item.get("expected_keywords", [])
                    if str(keyword).strip()
                ),
                notes=str(item.get("notes", "")).strip(),
            )
        )
    return questions


def load_run_file(path: Path | str) -> dict[str, Any]:
    """Load a run artifact from disk."""
    run_path = Path(path)
    with run_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_baseline_dir(path: Path | str) -> dict[str, Any]:
    """Load per-question baseline files into the combined run schema."""
    baseline_dir = Path(path)
    files = sorted(baseline_dir.glob("q*.json"))
    if not files:
        raise FileNotFoundError(f'No baseline files found in {baseline_dir}')

    results: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for file_path in files:
        with file_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not meta:
            meta = payload.get("meta", {})
        results.append(payload.get("result", payload))

    return {"meta": meta, "results": results}


def import_baselines(
    source_dir: Path | str,
    output_dir: Path | str = DEFAULT_BASELINES_DIR,
) -> list[Path]:
    """Copy Litellm-style baseline files into the local Inqtrix repo."""
    source_path = Path(source_dir)
    files = sorted(source_path.glob("q*.json"))
    if not files:
        raise FileNotFoundError(f'No baseline files found in {source_path}')

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for file_path in files:
        target_path = target_dir / file_path.name
        shutil.copy2(file_path, target_path)
        copied.append(target_path)

    migrated_file = source_path / "baseline.json.migrated"
    if migrated_file.exists():
        target_path = target_dir / migrated_file.name
        shutil.copy2(migrated_file, target_path)
        copied.append(target_path)

    return copied


def normalize_test_result(
    question: QuestionSpec,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Normalize a single /v1/test/run result into the reference schema."""
    return {
        "question_id": question.id,
        "question": question.question,
        "category": question.category,
        "answer": result.get("answer", ""),
        "metrics": result.get("metrics", {}),
        "iteration_logs": result.get("iteration_logs", []),
        "top_sources": result.get("top_sources", []),
        "top_claims": result.get("top_claims", []),
    }


def _timestamp_token() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _build_output_path(
    output_dir: Path | str,
    *,
    filename_prefix: str,
    suffix: str,
    stamp: str | None = None,
) -> Path:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = stamp or _timestamp_token()
    return target_dir / f"{filename_prefix}_{timestamp}{suffix}"


def _write_text_artifact(
    text: str,
    *,
    output_dir: Path | str,
    filename_prefix: str,
    suffix: str,
    stamp: str | None = None,
) -> Path:
    target_path = _build_output_path(
        output_dir,
        filename_prefix=filename_prefix,
        suffix=suffix,
        stamp=stamp,
    )
    with target_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
    return target_path


def save_run(
    run_data: dict[str, Any],
    output_dir: Path | str = DEFAULT_RUNS_DIR,
    *,
    stamp: str | None = None,
) -> Path:
    """Save a run artifact with a timestamped filename."""
    run_path = _build_output_path(
        output_dir,
        filename_prefix="run",
        suffix=".json",
        stamp=stamp,
    )
    with run_path.open("w", encoding="utf-8") as handle:
        json.dump(run_data, handle, ensure_ascii=False, indent=2)
    return run_path


def save_report(
    report: dict[str, Any],
    output_dir: Path | str = DEFAULT_REPORTS_DIR,
    *,
    filename_prefix: str = "report",
    stamp: str | None = None,
) -> Path:
    """Save a parity report with a timestamped filename."""
    report_path = _build_output_path(
        output_dir,
        filename_prefix=filename_prefix,
        suffix=".json",
        stamp=stamp,
    )
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return report_path


def save_compare_markdown_report(
    report_text: str,
    *,
    output_dir: Path | str = DEFAULT_REPORTS_DIR,
    stamp: str | None = None,
) -> Path:
    """Save the deterministic compare report as Markdown."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        "# Inqtrix Parity Compare Report\n\n"
        f"- erstellt: {created_at}\n\n"
        f"{report_text}\n"
    )
    return _write_text_artifact(
        body,
        output_dir=output_dir,
        filename_prefix="report",
        suffix=".md",
        stamp=stamp,
    )


def save_csv_report(
    csv_text: str,
    *,
    output_dir: Path | str = DEFAULT_REPORTS_DIR,
    filename_prefix: str = "report",
    stamp: str | None = None,
) -> Path:
    """Save a flat CSV export for parity check rows."""
    return _write_text_artifact(
        csv_text,
        output_dir=output_dir,
        filename_prefix=filename_prefix,
        suffix=".csv",
        stamp=stamp,
    )


def save_markdown_report(
    report_text: str,
    *,
    analysis_model: str,
    output_dir: Path | str = DEFAULT_REPORTS_DIR,
    filename_prefix: str = "analysis",
    stamp: str | None = None,
) -> Path:
    """Save an LLM-assisted parity report as Markdown."""
    header = (
        "# Inqtrix Parity Analysis\n\n"
        f"- erstellt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- analyse-modell: {analysis_model}\n\n"
        "---\n\n"
    )
    return _write_text_artifact(
        header + report_text,
        output_dir=output_dir,
        filename_prefix=filename_prefix,
        suffix=".md",
        stamp=stamp,
    )


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Perform a JSON HTTP request using the standard library only.

    Raises:
        urllib.error.HTTPError: The remote endpoint returned an HTTP error.
        urllib.error.URLError: The remote endpoint could not be reached.
        json.JSONDecodeError: The response body was not valid JSON.
    """
    request_data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        request_data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib_request.Request(url, data=request_data, headers=headers, method=method)
    with urllib_request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def run_questions_via_http(
    *,
    endpoint: str,
    questions: list[QuestionSpec],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Execute canonical questions against the local HTTP test endpoint."""
    health: dict[str, Any] = {}
    health_probe_error = ""
    try:
        health = _http_json(f"{endpoint}{HEALTH_ENDPOINT}", timeout=15)
    except (urllib_error.HTTPError, urllib_error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        health = {}
        health_probe_error = str(exc)

    results: list[dict[str, Any]] = []
    for question in questions:
        raw = _http_json(
            f"{endpoint}{TEST_ENDPOINT}",
            method="POST",
            payload={"question": question.question},
            timeout=timeout,
        )
        results.append(normalize_test_result(question, raw))

    meta = {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "endpoint": endpoint,
            "agent_version": health.get("status", "unknown"),
            "reasoning_model": health.get("reasoning_model", ""),
            "search_model": health.get("search_model", ""),
            "classify_model": health.get("classify_model", ""),
            "claim_extract_model": health.get("claim_extract_model", ""),
            "evaluate_model": health.get("evaluate_model", ""),
            "testing_mode": health.get("testing_mode", False),
        },
        "results": results,
    }
    if health_probe_error:
        meta["meta"]["health_probe_error"] = health_probe_error
    return meta
