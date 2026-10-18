"""Run a provider-backed prompt-flow debug capture.

This script mirrors ``examples/provider_stacks/bedrock_perplexity.py`` while
recording provider calls and selected state views. It intentionally does not
load ``.env`` files; credentials must already be present in the process
environment or in the provider SDK's normal credential chain.

The captured artifact directory is analyzed by ``scripts/debug_research_log.py``
after the run, so the existing debug tool remains the main reader-facing
inspection interface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, is_dataclass
from pathlib import Path
from queue import Queue
from typing import Any

from dotenv import load_dotenv

from inqtrix import AgentConfig, ReportProfile, ResearchAgent
from inqtrix.graph import run as run_graph
from inqtrix.providers.base import LLMProvider, LLMResponse, SearchProvider
from inqtrix.providers.bedrock import BedrockLLM
from inqtrix.providers.perplexity import PerplexitySearch
from inqtrix.result import ResearchResult
from inqtrix.logging_config import configure_logging
from inqtrix.urls import sanitize_error

from debug_research_log import build_artifact_flow_report

DEFAULT_QUESTION = (
    "Was waren die wichtigsten KI-Entwicklungen der letzten 7 Tage und "
    "welche Auswirkung hatte das auf die Wirtschaft?"
)
DEFAULT_BEDROCK_REGION = os.getenv("AWS_REGION", "eu-central-1")
DEFAULT_BEDROCK_MODEL = "eu.anthropic.claude-sonnet-4-6"
_MARKDOWN_URL_RE = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")


def _redact(value: Any) -> Any:
    """Return a JSON-safe copy with credential-like strings scrubbed.

    Args:
        value: Arbitrary value captured from the runtime.

    Returns:
        A JSON-serializable value with known credential patterns redacted.
    """
    if is_dataclass(value):
        return _redact(asdict(value))
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, Exception)):
        return sanitize_error(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_error(repr(value))


def _write_json(path: Path, payload: Any) -> None:
    """Write a redacted JSON artifact with stable formatting.

    Args:
        path: Artifact path to create.
        payload: JSON-compatible payload or arbitrary object accepted by
            ``_redact``.
    """
    path.write_text(
        json.dumps(_redact(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _append_jsonl(path: Path, payload: Any) -> None:
    """Append one redacted JSON line to an artifact.

    Args:
        path: JSONL artifact path.
        payload: JSON-compatible payload or arbitrary object accepted by
            ``_redact``.
    """
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_redact(payload), ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def _text_stats(text: str | None) -> dict[str, Any]:
    """Build compact text-size diagnostics.

    Args:
        text: Optional text to inspect.

    Returns:
        Character count and a short first-line preview.
    """
    raw = text or ""
    collapsed = " ".join(raw.split())
    return {
        "chars": len(raw),
        "preview": collapsed[:320],
    }


class RecordingLLM(LLMProvider):
    """Record all LLM calls while delegating behavior unchanged.

    Args:
        provider: Concrete LLM provider that should execute the real calls.
        out_dir: Directory where JSONL call artifacts are written.
    """

    def __init__(self, provider: LLMProvider, out_dir: Path) -> None:
        self._provider = provider
        self._path = out_dir / "llm_calls.jsonl"
        self._lock = threading.Lock()
        self._counter = 0

    @property
    def models(self) -> Any:
        """Return role-to-model metadata from the wrapped provider."""
        return getattr(self._provider, "models", None)

    @property
    def context_window_tokens(self) -> int | None:
        """Return the wrapped provider's context-window declaration."""
        value = getattr(self._provider, "context_window_tokens", None)
        if callable(value):
            value = value()
        return value if isinstance(value, int) else None

    def _next_index(self) -> int:
        """Return a process-local call index."""
        with self._lock:
            self._counter += 1
            return self._counter

    def _record(self, payload: dict[str, Any]) -> None:
        """Persist one LLM call artifact."""
        with self._lock:
            _append_jsonl(self._path, payload)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = 120.0,
        state: dict | None = None,
        deadline: float | None = None,
    ) -> str:
        """Delegate ``complete`` and capture prompt, response, and metadata."""
        call_index = self._next_index()
        started = time.monotonic()
        base_payload = {
            "call_index": call_index,
            "method": "complete",
            "node": (state or {}).get("_current_node"),
            "model": model,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
            "system": system,
            "prompt": prompt,
            "system_stats": _text_stats(system),
            "prompt_stats": _text_stats(prompt),
        }
        try:
            response = self._provider.complete(
                prompt,
                system=system,
                model=model,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
                state=state,
                deadline=deadline,
            )
        except Exception as exc:
            self._record({
                **base_payload,
                "elapsed_s": round(time.monotonic() - started, 3),
                "error_type": type(exc).__name__,
                "error": exc,
            })
            raise
        self._record({
            **base_payload,
            "elapsed_s": round(time.monotonic() - started, 3),
            "response": response,
            "response_stats": _text_stats(response),
        })
        return response

    def complete_with_metadata(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = 120.0,
        state: dict | None = None,
        deadline: float | None = None,
    ) -> LLMResponse:
        """Delegate ``complete_with_metadata`` and capture the full request."""
        call_index = self._next_index()
        started = time.monotonic()
        base_payload = {
            "call_index": call_index,
            "method": "complete_with_metadata",
            "node": (state or {}).get("_current_node"),
            "model": model,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
            "system": system,
            "prompt": prompt,
            "system_stats": _text_stats(system),
            "prompt_stats": _text_stats(prompt),
        }
        try:
            response = self._provider.complete_with_metadata(
                prompt,
                system=system,
                model=model,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
                state=state,
                deadline=deadline,
            )
        except Exception as exc:
            self._record({
                **base_payload,
                "elapsed_s": round(time.monotonic() - started, 3),
                "error_type": type(exc).__name__,
                "error": exc,
            })
            raise
        self._record({
            **base_payload,
            "elapsed_s": round(time.monotonic() - started, 3),
            "response": response.content,
            "response_stats": _text_stats(response.content),
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "effective_model": response.model,
            "finish_reason": response.finish_reason,
            "request_max_tokens": response.request_max_tokens,
        })
        return response

    def is_available(self) -> bool:
        """Return the wrapped provider's readiness result."""
        return self._provider.is_available()

    def consume_nonfatal_notice(self) -> str | None:
        """Return and clear a wrapped provider non-fatal notice, if present."""
        consumer = getattr(self._provider, "consume_nonfatal_notice", None)
        if callable(consumer):
            return consumer()
        return None

    def without_thinking(self) -> Any:
        """Forward the provider's thinking-suppression context manager."""
        manager = getattr(self._provider, "without_thinking", None)
        if callable(manager):
            return manager()
        return nullcontext(self)

    def __getattr__(self, name: str) -> Any:
        """Forward provider-specific attributes used by diagnostics."""
        return getattr(self._provider, name)


class RecordingSearch(SearchProvider):
    """Record all search calls while delegating behavior unchanged.

    Args:
        provider: Concrete search provider that executes the real query.
        out_dir: Directory where JSONL call artifacts are written.
    """

    def __init__(self, provider: SearchProvider, out_dir: Path) -> None:
        self._provider = provider
        self._path = out_dir / "search_calls.jsonl"
        self._lock = threading.Lock()
        self._counter = 0

    def _next_index(self) -> int:
        """Return a process-local search-call index."""
        with self._lock:
            self._counter += 1
            return self._counter

    def search(
        self,
        query: str,
        *,
        search_context_size: str = "high",
        recency_filter: str | None = None,
        language_filter: list[str] | None = None,
        domain_filter: list[str] | None = None,
        search_mode: str | None = None,
        return_related: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Delegate ``search`` and capture normalized request/response data."""
        call_index = self._next_index()
        started = time.monotonic()
        base_payload = {
            "call_index": call_index,
            "query": query,
            "search_context_size": search_context_size,
            "recency_filter": recency_filter,
            "language_filter": language_filter,
            "domain_filter": domain_filter,
            "search_mode": search_mode,
            "return_related": return_related,
            "search_model": self.search_model,
        }
        try:
            result = self._provider.search(
                query,
                search_context_size=search_context_size,
                recency_filter=recency_filter,
                language_filter=language_filter,
                domain_filter=domain_filter,
                search_mode=search_mode,
                return_related=return_related,
                deadline=deadline,
            )
        except Exception as exc:
            with self._lock:
                _append_jsonl(
                    self._path,
                    {
                        **base_payload,
                        "elapsed_s": round(time.monotonic() - started, 3),
                        "error_type": type(exc).__name__,
                        "error": exc,
                    },
                )
            raise
        citations = result.get("citations") or []
        with self._lock:
            _append_jsonl(
                self._path,
                {
                    **base_payload,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "answer": result.get("answer", ""),
                    "answer_stats": _text_stats(result.get("answer", "")),
                    "citation_count": len(citations),
                    "citations": citations,
                    "related_questions": result.get("related_questions") or [],
                    "prompt_tokens": result.get("_prompt_tokens", 0),
                    "completion_tokens": result.get("_completion_tokens", 0),
                    "raw_keys": sorted(str(k) for k in result.keys()),
                },
            )
        return result

    def is_available(self) -> bool:
        """Return the wrapped provider's readiness result."""
        return self._provider.is_available()

    @property
    def search_model(self) -> str:
        """Return the wrapped provider's search-model label."""
        return getattr(self._provider, "search_model", type(self._provider).__name__)

    def __getattr__(self, name: str) -> Any:
        """Forward provider-specific attributes such as capabilities."""
        return getattr(self._provider, name)


def _build_result_summary(state: dict[str, Any], result: ResearchResult) -> dict[str, Any]:
    """Build a compact, provider-neutral evidence-flow summary.

    Args:
        state: Final graph state.
        result: Public result object built from ``state``.

    Returns:
        Summary dictionary focused on evidence movement into the answer.
    """
    from inqtrix.evidence import render_evidence_ledger_overview

    evidence_ledger = state.get("evidence_ledger") or []
    consolidated_claims = state.get("consolidated_claims") or []
    overview = render_evidence_ledger_overview(
        evidence_ledger, max_total_chars=200000, max_record_chars=4000
    )
    used_answer_urls = []
    for match in _MARKDOWN_URL_RE.finditer(state.get("answer") or ""):
        url = match.group(1)
        if url not in used_answer_urls:
            used_answer_urls.append(url)
    evidence_samples = []
    for record in evidence_ledger[:40]:
        citations = record.get("citation_set") or []
        claims = record.get("claims") or []
        passages = record.get("source_passages") or []
        evidence_samples.append({
            "evidence_id": record.get("evidence_id"),
            "source_title": record.get("source_title"),
            "canonical_url": record.get("canonical_url") or record.get("url"),
            "tier": record.get("tier"),
            "query": record.get("query"),
            "source_date": record.get("source_date"),
            "report_eligible": record.get("report_eligible"),
            "claim_count": len(claims),
            "source_passage_count": len(passages),
            "source_snippet_chars": len(str(record.get("source_snippet") or "")),
            "citation_count": len(citations),
        })
    return {
        "answer_chars": len(state.get("answer") or ""),
        "rounds": state.get("round"),
        "final_confidence": state.get("final_confidence"),
        "done_reason": state.get("done_reason"),
        "total_prompt_tokens": state.get("total_prompt_tokens", 0),
        "total_completion_tokens": state.get("total_completion_tokens", 0),
        "all_citation_count": len(state.get("all_citations") or []),
        "used_answer_citation_count": len(used_answer_urls),
        "evidence_record_count": len(evidence_ledger),
        "report_eligible_evidence_count": sum(
            1 for record in evidence_ledger if record.get("report_eligible")
        ),
        "claimless_evidence_count": sum(
            1 for record in evidence_ledger if not record.get("claims")
        ),
        "claims_extracted": sum(len(record.get("claims") or []) for record in evidence_ledger),
        "verified_claims": sum(
            1 for claim in consolidated_claims if claim.get("status") == "verified"
        ),
        "contested_claims": sum(
            1 for claim in consolidated_claims if claim.get("status") == "contested"
        ),
        "unverified_claims": sum(
            1 for claim in consolidated_claims if claim.get("status") == "unverified"
        ),
        "consolidated_claim_count": len(consolidated_claims),
        "evidence_overview_chars": len(overview.markdown),
        "evidence_overview_rendered_records": overview.rendered_record_count,
        "evidence_overview_omitted_records": overview.omitted_record_count,
        "evidence_overview_allowed_citations": len(overview.allowed_urls),
        "answer_prompt_diagnostics": state.get("answer_prompt_diagnostics") or {},
        "algorithm_report_blocked": state.get("algorithm_report_blocked"),
        "evidence_contract": state.get("evidence_contract"),
        "top_sources": [source.model_dump(mode="json") for source in result.top_sources[:20]],
        "used_answer_urls": used_answer_urls[:80],
        "evidence_samples": evidence_samples,
    }


def _parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--max-total-seconds", type=int, default=1000)
    parser.add_argument("--confidence-stop", type=int, default=8)
    parser.add_argument("--bedrock-region", default=DEFAULT_BEDROCK_REGION)
    parser.add_argument("--bedrock-model", default=DEFAULT_BEDROCK_MODEL)
    parser.add_argument("--bedrock-claim-extract-model", default=DEFAULT_BEDROCK_MODEL)
    parser.add_argument(
        "--bedrock-thinking",
        choices=("adaptive", "none"),
        default="adaptive",
        help="Bedrock extended-thinking mode for reasoning calls.",
    )
    parser.add_argument(
        "--bedrock-effort",
        choices=("low", "medium", "high", "xhigh", "max", "none"),
        default="medium",
        help="Bedrock output_config.effort for reasoning calls.",
    )
    parser.add_argument(
        "--load-dotenv",
        action="store_true",
        help=(
            "Load .env before constructing the provider stack. Use only when "
            "the operator explicitly wants the example-script credential path."
        ),
    )
    parser.add_argument(
        "--report-profile",
        choices=("compact", "deep"),
        default="deep",
    )
    return parser.parse_args()


def main() -> None:
    """Execute the live debug run and write artifacts."""
    args = _parse_args()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = (
        Path(args.out_dir).expanduser()
        if args.out_dir
        else Path("/private/tmp") / f"inqtrix-live-debug-{timestamp}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("INQTRIX_LOG_ENABLED", "true")
    os.environ.setdefault("INQTRIX_LOG_LEVEL", "DEBUG")
    os.environ.setdefault("INQTRIX_LOG_CONSOLE", "false")
    os.environ.setdefault("INQTRIX_LOG_DIR", str(out_dir / "logs"))
    os.environ.setdefault("INQTRIX_OBSERVABILITY_PROFILE", "forensic")
    if args.load_dotenv:
        load_dotenv()
    configure_logging(
        enabled=True,
        level="DEBUG",
        console=False,
        log_dir=str(Path(os.environ["INQTRIX_LOG_DIR"])),
    )

    perplexity_key = os.getenv("PERPLEXITY_API_KEY")
    if not perplexity_key:
        raise RuntimeError(
            "PERPLEXITY_API_KEY is not present in the process environment. "
            "This debug harness does not read .env files."
        )

    aws_profile = os.getenv("AWS_PROFILE", "papillon-bedrock")
    thinking = (
        {"type": "adaptive"}
        if args.bedrock_thinking == "adaptive"
        else None
    )
    effort = None if args.bedrock_effort == "none" else args.bedrock_effort
    base_llm = BedrockLLM(
        profile_name=aws_profile,
        region_name=args.bedrock_region,
        default_model=args.bedrock_model,
        claim_extract_model=args.bedrock_claim_extract_model,
        thinking=thinking,
        effort=effort,
    )
    base_search = PerplexitySearch(
        api_key=perplexity_key,
        base_url="https://api.perplexity.ai",
        model="sonar-pro",
    )
    llm = RecordingLLM(base_llm, out_dir)
    search = RecordingSearch(base_search, out_dir)
    profile = (
        ReportProfile.DEEP
        if args.report_profile.lower() == "deep"
        else ReportProfile.COMPACT
    )
    config = AgentConfig(
        llm=llm,
        search=search,
        report_profile=profile,
        max_rounds=args.max_rounds,
        confidence_stop=args.confidence_stop,
        max_total_seconds=args.max_total_seconds,
        reasoning_timeout=900,
        search_timeout=900,
        claim_extract_timeout=900,
        testing_mode=True,
        observability_profile="forensic",
    )
    agent = ResearchAgent(config)
    providers, strategies, settings = agent._ensure_initialised()
    event_path = out_dir / "run_events.jsonl"

    def record_event(event: str, payload: dict[str, Any]) -> None:
        _append_jsonl(event_path, {"event": event, "payload": payload})

    run_id = f"live-debug-{uuid.uuid4().hex[:12]}"
    raw = run_graph(
        args.question,
        progress_queue=Queue(),
        providers=providers,
        strategies=strategies,
        settings=settings,
        run_id=run_id,
        run_event_sink=record_event,
    )
    state = raw["result_state"]
    (out_dir / "answer.md").write_text(state.get("answer") or "", encoding="utf-8")
    _write_json(out_dir / "result_state_raw.json", state)
    result = ResearchResult.from_raw(raw)
    _write_json(out_dir / "run_config.json", {
        "run_id": run_id,
        "question": args.question,
        "provider_stack": "bedrock_perplexity",
        "llm_model": base_llm.models.reasoning_model,
        "claim_extract_model": base_llm.models.effective_claim_extract_model,
        "bedrock_region": args.bedrock_region,
        "bedrock_thinking": args.bedrock_thinking,
        "bedrock_effort": args.bedrock_effort,
        "search_model": base_search.search_model,
        "aws_profile_present": bool(aws_profile),
        "report_profile": str(profile.value),
        "settings": settings.model_dump(mode="json"),
    })
    from inqtrix.evidence import render_evidence_ledger_overview

    _write_json(out_dir / "summary.json", _build_result_summary(state, result))
    _write_json(out_dir / "iteration_logs.json", state.get("iteration_logs") or [])
    _write_json(out_dir / "evidence_ledger_snapshot.json", state.get("evidence_ledger") or [])
    _write_json(out_dir / "consolidated_claims.json", state.get("consolidated_claims") or [])
    _write_json(out_dir / "score_ledger.json", state.get("score_ledger") or [])
    _write_json(out_dir / "public_result.json", result.model_dump(mode="json"))
    _overview = render_evidence_ledger_overview(
        state.get("evidence_ledger") or [],
        max_total_chars=200000,
        max_record_chars=4000,
    )
    (out_dir / "evidence_overview.md").write_text(_overview.markdown, encoding="utf-8")
    flow_report = build_artifact_flow_report(out_dir)
    (out_dir / "flow_report.txt").write_text(flow_report, encoding="utf-8")

    print(json.dumps({
        "out_dir": str(out_dir),
        "run_id": run_id,
        "answer_chars": len(state.get("answer") or ""),
        "evidence_records": len(state.get("evidence_ledger") or []),
        "evidence_overview_chars": len(_overview.markdown),
        "llm_calls_path": str(out_dir / "llm_calls.jsonl"),
        "search_calls_path": str(out_dir / "search_calls.jsonl"),
        "summary_path": str(out_dir / "summary.json"),
        "answer_path": str(out_dir / "answer.md"),
        "flow_report_path": str(out_dir / "flow_report.txt"),
    }, ensure_ascii=False, indent=2))
    print("\n--- FLOW REPORT ---")
    print(flow_report)


if __name__ == "__main__":
    main()
