"""Report how source material moved through a live prompt-flow debug capture."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _load_json(path: Path, default: Any) -> Any:
    """Load one JSON artifact when present.

    Args:
        path: Artifact path to read.
        default: Value returned when the artifact is missing or invalid.

    Returns:
        Parsed JSON payload or ``default``.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL artifacts emitted by live prompt-flow capture.

    Args:
        path: JSONL artifact path.

    Returns:
        Parsed dictionaries; malformed lines are ignored so one partial
        provider call does not hide the rest of the flow.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _preview(value: Any, limit: int = 240) -> str:
    """Return a single-line preview for source and prompt material."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: max(0, limit - 3)]}..."


def _text_len(value: Any) -> int:
    """Return the character length of text-ish values."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _list_text_chars(items: Any, key: str | None = None) -> int:
    """Return total text characters in list items.

    Args:
        items: Expected list of text or dictionaries.
        key: Optional dictionary key to extract from each item.

    Returns:
        Sum of character lengths.
    """
    if not isinstance(items, list):
        return 0
    total = 0
    for item in items:
        if key and isinstance(item, dict):
            total += _text_len(item.get(key))
        else:
            total += _text_len(item)
    return total


def _first_text(items: Any, key: str | None = None) -> str:
    """Return the first non-empty text from a list-like artifact field."""
    if not isinstance(items, list):
        return ""
    for item in items:
        if key and isinstance(item, dict):
            text = str(item.get(key) or "")
        else:
            text = str(item or "")
        if text.strip():
            return text
    return ""


def _record_source_chars(record: dict[str, Any]) -> int:
    """Return source-material characters carried by one evidence record."""
    return (
        _text_len(record.get("source_snippet"))
        + _list_text_chars(record.get("source_passages"), "text")
        + _list_text_chars(record.get("data_points"))
    )


def _build_prompt_coverage_lines(
    records: list[dict[str, Any]],
    overview: str,
) -> list[str]:
    """Build source-to-prompt coverage diagnostics.

    Checks how many report-eligible EvidenceRecords actually made it into
    the single rendered evidence overview (by canonical URL), so a gap
    between "eligible" and "rendered" is visible instead of silent.
    """
    eligible = [record for record in records if record.get("report_eligible")]
    rendered = [
        record
        for record in eligible
        if str(record.get("canonical_url") or record.get("url") or "") in overview
    ]
    missing = [record for record in eligible if record not in rendered]
    claimless_eligible = [
        record for record in eligible if not (record.get("claims") or [])
    ]
    lines = [
        (
            "Prompt coverage: "
            f"eligible_records={len(eligible)} "
            f"rendered_in_overview={len(rendered)} "
            f"missing_from_overview={len(missing)} "
            f"claimless_eligible={len(claimless_eligible)}"
        )
    ]
    for record in missing[:8]:
        lines.append(
            "  missing "
            f"id={record.get('evidence_id', '')} "
            f"tier={record.get('tier', '')} "
            f"claims={len(record.get('claims') or [])} "
            f"chars={_record_source_chars(record)} "
            f"title={_preview(record.get('source_title', ''), 120)}"
        )
    return lines


def _build_search_artifact_lines(search_calls: list[dict[str, Any]]) -> list[str]:
    """Build concrete search-call diagnostics from live artifacts."""
    if not search_calls:
        return ["Search calls: none captured"]
    lines = [f"Search calls: {len(search_calls)}"]
    for call in search_calls[:20]:
        lines.append(
            "  "
            f"#{call.get('call_index')} "
            f"ctx={call.get('search_context_size')} "
            f"recency={call.get('recency_filter')} "
            f"lang={call.get('language_filter')} "
            f"domains={len(call.get('domain_filter') or [])} "
            f"answer_chars={call.get('answer_stats', {}).get('chars', 0)} "
            f"citations={call.get('citation_count', 0)} "
            f"query={_preview(call.get('query', ''), 180)}"
        )
    return lines


def _build_record_artifact_lines(records: list[dict[str, Any]]) -> list[str]:
    """Build concrete EvidenceRecord diagnostics from live artifacts."""
    if not records:
        return ["Evidence records: none captured"]
    record_types = Counter(str(record.get("record_type") or "unknown") for record in records)
    lines = [
        f"Evidence records: total={len(records)} types={dict(record_types)}",
    ]
    for index, record in enumerate(records[:30], start=1):
        passages = record.get("source_passages") or []
        lines.append(
            "  "
            f"ER{index:02d} id={record.get('evidence_id', '')} "
            f"tier={record.get('tier', '')} "
            f"eligible={record.get('report_eligible')} "
            f"claims={len(record.get('claims') or [])} "
            f"source_chars={_record_source_chars(record)} "
            f"snippet={_text_len(record.get('source_snippet'))} "
            f"passages={len(passages)}/{_list_text_chars(passages, 'text')} "
            f"citations={len(record.get('citation_set') or [])}"
        )
        lines.append(f"       title={_preview(record.get('source_title', ''), 160)}")
        lines.append(f"       query={_preview(record.get('query', ''), 160)}")
        preview = record.get("source_snippet") or _first_text(passages, "text")
        if preview:
            lines.append(f"       material={_preview(preview)}")
    return lines


def _build_llm_artifact_lines(llm_calls: list[dict[str, Any]]) -> list[str]:
    """Build concrete LLM prompt diagnostics from live artifacts."""
    if not llm_calls:
        return ["LLM calls: none captured"]
    methods = Counter(str(call.get("method") or "unknown") for call in llm_calls)
    nodes = Counter(str(call.get("node") or "unknown") for call in llm_calls)
    lines = [f"LLM calls: total={len(llm_calls)} methods={dict(methods)} nodes={dict(nodes)}"]
    answer_calls = [
        call for call in llm_calls
        if call.get("node") == "answer"
        or "EVIDENCE UNIT" in str(call.get("prompt") or "")
    ]
    lines.append(f"Answer/prompt-evidence calls: {len(answer_calls)}")
    for call in answer_calls[:12]:
        prompt = str(call.get("prompt") or "")
        system = str(call.get("system") or "")
        response = str(call.get("response") or "")
        lines.append(
            "  "
            f"#{call.get('call_index')} "
            f"method={call.get('method')} "
            f"node={call.get('node')} "
            f"model={call.get('effective_model') or call.get('model') or ''} "
            f"system_chars={len(system)} "
            f"prompt_chars={len(prompt)} "
            f"evidence_units={prompt.count('EVIDENCE UNIT')} "
            f"source_context_mentions={prompt.count('source_context')} "
            f"source_passage_sections={prompt.count('Source Passages:')} "
            f"data_point_sections={prompt.count('Data Points:')} "
            f"response_chars={len(response)}"
        )
        lines.append(f"       prompt_preview={_preview(prompt, 260)}")
        if response:
            lines.append(f"       response_preview={_preview(response, 220)}")
    return lines


def build_artifact_flow_report(path: Path) -> str:
    """Build a deep report from a live debug artifact directory.

    Args:
        path: Directory containing artifacts from prompt-flow capture.

    Returns:
        Human-readable report showing source material movement into the
        answer prompt.
    """
    summary = _load_json(path / "summary.json", {})
    records = _load_json(path / "evidence_ledger_snapshot.json", [])
    overview_path = path / "evidence_overview.md"
    overview = overview_path.read_text(encoding="utf-8") if overview_path.exists() else ""
    search_calls = _load_jsonl(path / "search_calls.jsonl")
    llm_calls = _load_jsonl(path / "llm_calls.jsonl")
    answer = (path / "answer.md").read_text(encoding="utf-8") if (path / "answer.md").exists() else ""

    lines = [
        f"Artifact directory: {path}",
        (
            "Run summary: "
            f"rounds={summary.get('rounds', 'n/a')} "
            f"confidence={summary.get('final_confidence', 'n/a')} "
            f"answer_chars={summary.get('answer_chars', len(answer))} "
            f"evidence_records={summary.get('evidence_record_count', len(records))} "
            f"report_eligible={summary.get('report_eligible_evidence_count', 'n/a')} "
            f"claimless={summary.get('claimless_evidence_count', 'n/a')} "
            f"claims_extracted={summary.get('claims_extracted', 'n/a')} "
            f"consolidated_claims={summary.get('consolidated_claim_count', 'n/a')} "
            f"evidence_overview_chars={summary.get('evidence_overview_chars', len(overview))} "
            f"rendered_records={summary.get('evidence_overview_rendered_records', 'n/a')} "
            f"omitted_records={summary.get('evidence_overview_omitted_records', 'n/a')} "
            f"used_citations={summary.get('used_answer_citation_count', 'n/a')}"
        ),
        "",
        *_build_search_artifact_lines(search_calls),
        "",
        *_build_record_artifact_lines(records if isinstance(records, list) else []),
        "",
        "Evidence overview (rendered single answer-prompt view):",
        f"  chars={len(overview)} record_blocks={overview.count('[E')}",
        f"  preview={_preview(overview, 320)}" if overview else "  (no evidence_overview.md captured)",
        "",
        *_build_prompt_coverage_lines(
            records if isinstance(records, list) else [],
            overview,
        ),
        "",
        *_build_llm_artifact_lines(llm_calls),
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        help="Artifact directory written by scripts/live_prompt_flow_debug.py.",
    )
    args = parser.parse_args()

    path = Path(args.target)
    if not path.is_dir():
        parser.error(f"not a directory: {path}")
    print(build_artifact_flow_report(path))


if __name__ == "__main__":
    main()
