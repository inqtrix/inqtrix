"""Trace search queries through the REAL evidence pipeline.

This is a debugging aid, not part of the package. By default it runs two
(configurable) search queries through the exact functions the search node uses --
``provider.search`` -> ``normalize_source_provenance`` -> ``query_synthesis``
-> ``assemble_evidence_records`` -> ``merge_evidence_records`` ->
``render_evidence_ledger_overview`` -- and writes the full, untruncated data
of every stage to ``scripts/debug_out/<provider>_<ts>.json`` plus a readable
summary on stdout. The goal is to follow a raw provider answer along the
dataflow and confirm nothing is silently capped or mis-stored.

With ``--full`` it runs the actual LangGraph research loop with the selected
search provider and dumps the state produced by classify -> plan -> search ->
evaluate -> answer. This mode is the verification path for query synthesis,
claim extraction, consolidation, evidence projection, and final answer context.

Credentials are read from the environment (.env). Examples:

    uv run python scripts/debug_search_dataflow.py --provider perplexity
    uv run python scripts/debug_search_dataflow.py --provider azure \\
        --query "Welche Quartalszahlen hat NVIDIA erreicht?"
    uv run python scripts/debug_search_dataflow.py --provider perplexity --full
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

from inqtrix.evidence import (
    assemble_evidence_records,
    merge_evidence_records,
    render_evidence_ledger_overview,
)
from inqtrix.agent import AgentConfig, ResearchAgent
from inqtrix.graph import run as run_graph
from inqtrix.nodes import _tier_explanations_for_urls
from inqtrix.report_profiles import tuning_for_report_profile
from inqtrix.runtime_logging import make_record_id, normalize_source_provenance
from inqtrix.strategies._source_tiering import DefaultSourceTiering

_UNCAPPED = 10**9  # render budget large enough that nothing is truncated

DEFAULT_QUERIES = [
    "Welche Quartalszahlen hat NVIDIA erreicht?",
    "Was gibt es Neues zu KI in dieser Woche?",
]


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses/objects to JSON-serializable values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _build_provider(name: str) -> tuple[Any, str]:
    """Construct the requested search provider from environment variables."""
    if name == "perplexity":
        from inqtrix.providers.perplexity import PerplexitySearch

        api_key = os.getenv("PERPLEXITY_API_KEY")
        if not api_key:
            sys.exit("Set PERPLEXITY_API_KEY in the environment.")
        instructions = os.getenv("INQTRIX_PERPLEXITY_INSTRUCTIONS")
        # A preset bundles the citation-instruction system prompt, so the answer
        # carries inline [n] markers; an explicit model (raw, no preset) does not.
        model = os.getenv("INQTRIX_PERPLEXITY_MODEL")
        if model:
            return (
                PerplexitySearch(api_key=api_key, model=model, instructions=instructions),
                "PerplexitySearch",
            )
        preset = os.getenv("INQTRIX_PERPLEXITY_PRESET", "fast-search")
        return (
            PerplexitySearch(api_key=api_key, preset=preset, instructions=instructions),
            "PerplexitySearch",
        )

    if name == "azure":
        from inqtrix.providers.azure_web_search import AzureFoundryWebSearch

        endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT")
        agent = os.getenv("WEB_SEARCH_AGENT_NAME")
        if not (endpoint and agent):
            sys.exit("Set AZURE_AI_PROJECT_ENDPOINT and WEB_SEARCH_AGENT_NAME.")
        kwargs: dict[str, Any] = {"project_endpoint": endpoint, "agent_name": agent}
        version = os.getenv("WEB_SEARCH_AGENT_VERSION")
        if version:
            kwargs["agent_version"] = version
        project_key = os.getenv("AZURE_AI_PROJECT_API_KEY")
        tid, cid, sec = (
            os.getenv("AZURE_TENANT_ID"),
            os.getenv("AZURE_CLIENT_ID"),
            os.getenv("AZURE_CLIENT_SECRET"),
        )
        if project_key:
            kwargs["api_key"] = project_key
        elif tid and cid and sec:
            kwargs.update(tenant_id=tid, client_id=cid, client_secret=sec)
        return AzureFoundryWebSearch(**kwargs), "AzureFoundryWebSearch"

    sys.exit(f"Unknown provider: {name}")


def run(provider_name: str, queries: list[str], out_dir: Path) -> Path:
    """Run the queries through the real pipeline and write the dump file."""
    provider, label = _build_provider(provider_name)
    strategies = SimpleNamespace(source_tiering=DefaultSourceTiering())

    ledger: list[dict[str, Any]] = []
    query_synthesis: dict[str, dict[str, Any]] = {}
    per_query: list[dict[str, Any]] = []

    for index, query in enumerate(queries):
        result = provider.search(query)
        notice = provider.consume_nonfatal_notice()
        query_id = make_record_id("qry", str(index), query)
        tier_explanations = _tier_explanations_for_urls(result.citation_urls, strategies)
        source_records, citation_records = normalize_source_provenance(
            result,
            query_id=query_id,
            provider=label,
            tier_explanations=tier_explanations,
        )
        # No LLM summary in this lightweight trace: surface the provider's own
        # (citation-neutralized) synthesis so it still appears in the context.
        query_synthesis[query_id] = {
            "query": query,
            "round": 0,
            "provider_answer": result.answer,
            "related_questions": list(result.related_questions),
            "citation_urls_by_rank": {
                str(src.rank): src.url for src in result.sources if src.rank and src.url
            },
        }
        records = assemble_evidence_records(
            query_id=query_id,
            query=query,
            provider=label,
            source_records=source_records,
            citation_records=citation_records,
            claim_entries=[],
        )
        ledger = merge_evidence_records(ledger, records)
        per_query.append(
            {
                "query": query,
                "query_id": query_id,
                "nonfatal_notice": notice,
                "stage_1_grounded_search_result": _to_jsonable(result),
                "stage_2_source_records": source_records,
                "stage_2_citation_records": citation_records,
                "stage_3_query_synthesis": query_synthesis[query_id],
                "stage_4_evidence_records": records,
            }
        )

    overview = render_evidence_ledger_overview(
        ledger,
        max_total_chars=_UNCAPPED,
        max_record_chars=_UNCAPPED,
        query_synthesis=query_synthesis,
    )

    dump = {
        "provider": label,
        "generated_at": datetime.now().astimezone().isoformat(),
        "note": "LLM claim-extraction and verification pipelines not run (search-side trace).",
        "per_query": per_query,
        "stage_5_evidence_ledger": ledger,
        "stage_6_final_llm_context": overview.markdown,
        "stage_6_label_urls": overview.label_urls,
        "stage_6_allowed_urls": overview.allowed_urls,
        "stage_6_rendered_record_count": overview.rendered_record_count,
        "stage_6_omitted_record_count": overview.omitted_record_count,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{provider_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(label, per_query, overview, path)
    return path


def run_full(provider_name: str, queries: list[str], out_dir: Path) -> Path:
    """Run the real LangGraph pipeline and write a full-flow dump file."""
    provider, label = _build_provider(provider_name)
    agent = ResearchAgent(
        AgentConfig(
            search=provider,
            testing_mode=True,
            observability_profile="forensic",
        )
    )
    providers, strategies, settings = agent._ensure_initialised()  # noqa: SLF001 - debug script
    tuning = tuning_for_report_profile(settings.report_profile)

    runs: list[dict[str, Any]] = []
    for query in queries:
        raw = run_graph(
            query,
            providers=providers,
            strategies=strategies,
            settings=settings,
        )
        state = raw.get("result_state", {}) or {}
        overview = render_evidence_ledger_overview(
            state.get("evidence_ledger", []) or [],
            max_total_chars=tuning.prompt_evidence_total_char_budget,
            max_record_chars=tuning.prompt_evidence_record_char_limit,
            query_synthesis=state.get("query_synthesis", {}) or {},
            label_by_evidence_id=state.get("evidence_label_by_id", {}) or {},
        )
        runs.append(
            {
                "query": query,
                "answer": raw.get("answer", ""),
                "usage": raw.get("usage", {}),
                "queries": state.get("queries", []),
                "query_synthesis": state.get("query_synthesis", {}),
                "source_records": state.get("source_records", {}),
                "provider_citation_records": state.get("provider_citation_records", []),
                "evidence_ledger": state.get("evidence_ledger", []),
                "raw_claims": state.get("raw_claims", []),
                "consolidated_claims": state.get("consolidated_claims", []),
                "claim_status_counts": state.get("claim_status_counts", {}),
                "source_tier_counts": state.get("source_tier_counts", {}),
                "final_evidence_context": overview.markdown,
                "label_urls": overview.label_urls,
                "allowed_urls": overview.allowed_urls,
                "rendered_evidence_record_count": overview.rendered_record_count,
                "omitted_evidence_record_count": overview.omitted_record_count,
                "iteration_logs": state.get("iteration_logs", []),
            }
        )

    dump = {
        "provider": label,
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "full",
        "runs": _to_jsonable(runs),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{provider_name}_full_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_full_summary(label, runs, path)
    return path


def _print_summary(label: str, per_query: list, overview: Any, path: Path) -> None:
    """Print a readable summary (lengths prove nothing was capped)."""
    print(f"\n=== {label} -> {path} ===")
    for entry in per_query:
        gsr = entry["stage_1_grounded_search_result"]
        print(f"\nQuery: {entry['query']}")
        if entry["nonfatal_notice"]:
            print(f"  NOTICE: {entry['nonfatal_notice']}")
        print(f"  raw answer length: {len(gsr['answer'])} chars")
        print(f"  sources returned : {len(gsr['sources'])}")
        for src in gsr["sources"]:
            print(
                f"    rank={src['rank']:>2} snippet_len={len(src['snippet']):>6} "
                f"url={src['url']}"
            )
        print(f"  evidence records : {len(entry['stage_4_evidence_records'])}")
        for rec in entry["stage_4_evidence_records"]:
            print(
                f"    tier={rec['tier']:<11} snippet_len={len(rec['source_snippet']):>6} "
                f"passages={len(rec['source_passages'])} url={rec['canonical_url']}"
            )
    print(
        f"\nFinal LLM context: {len(overview.markdown)} chars | "
        f"rendered={overview.rendered_record_count} omitted={overview.omitted_record_count}"
    )


def _print_full_summary(label: str, runs: list[dict[str, Any]], path: Path) -> None:
    """Print the high-signal summary for a full graph trace."""
    print(f"\n=== {label} FULL -> {path} ===")
    for entry in runs:
        print(f"\nQuery: {entry['query']}")
        print(f"  answer chars      : {len(entry['answer'])}")
        print(f"  search queries    : {len(entry['queries'])}")
        print(f"  evidence records  : {len(entry['evidence_ledger'])}")
        print(f"  consolidated claims: {len(entry['consolidated_claims'])}")
        print(f"  evidence context  : {len(entry['final_evidence_context'])} chars")
        print(f"  labels            : {len(entry['label_urls'])}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["perplexity", "azure"], default="perplexity")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the complete LangGraph pipeline instead of the search-side trace.",
    )
    parser.add_argument(
        "--query", action="append", help="Override the query (repeatable, max 2 used)."
    )
    parser.add_argument("--out", default="scripts/debug_out")
    args = parser.parse_args()
    queries = (args.query or DEFAULT_QUERIES)[:2]
    if args.full:
        run_full(args.provider, queries, Path(args.out))
    else:
        run(args.provider, queries, Path(args.out))


if __name__ == "__main__":
    main()
