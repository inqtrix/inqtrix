"""Export existing algorithm judgements through the tracing batch lifecycle.

Scores are telemetry, not an additional evaluator or a durable billing record.
Provider content never enters a score. Missing measurements stay absent.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from inqtrix.observability import semconv

log = logging.getLogger("inqtrix")

RESEARCH_SCORE_FIELDS = {
    "final_confidence": ("final_confidence", "NUMERIC"),
    "claim_quality_score": ("claim_quality_score", "NUMERIC"),
    "source_quality_score": ("source_quality_score", "NUMERIC"),
    "aspect_coverage": ("aspect_coverage", "NUMERIC"),
    "evidence_contract_status": ("evidence_contract_status", "CATEGORICAL"),
    "stop_reason": ("_stop_reason", "CATEGORICAL"),
    "omitted_evidence_records": ("omitted_evidence_record_count", "NUMERIC"),
    "answer_contract": ("answer_contract", "CATEGORICAL"),
}
SCORE_TYPES = {
    **{name: kind for name, (_, kind) in RESEARCH_SCORE_FIELDS.items()},
    "critic_verdict": "CATEGORICAL",
    "deep_review_findings": "NUMERIC",
    "deep_review_complete": "BOOLEAN",
    "deep_review_grounded": "BOOLEAN",
    "deep_review_contradictions_named": "BOOLEAN",
}
_COMMENT = "Algorithm-produced signal; not independent semantic verification."


def _valid_score(value: Any, kind: str) -> bool:
    if kind == "BOOLEAN":
        return isinstance(value, bool)
    if kind == "CATEGORICAL":
        return isinstance(value, str) and bool(value.strip())
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def run_score_attributes(mode: str, state: Mapping[str, Any]) -> dict[str, Any]:
    """Project only metrics that the executing algorithm actually supplies."""
    values: dict[str, Any] = {}
    if mode == "research":
        values = {
            name: state[field]
            for name, (field, _) in RESEARCH_SCORE_FIELDS.items()
            if field in state
        }
    elif mode == "workspace_agent":
        critic = state.get("critic_report")
        if isinstance(critic, dict) and "verdict" in critic:
            values["critic_verdict"] = critic["verdict"]
    return {
        semconv.score_key(name): value
        for name, value in values.items()
        if _valid_score(value, SCORE_TYPES[name])
    }


_record_warning = False


def record_run_scores(mode: str, state: Mapping[str, Any], *, trace_root: bool) -> None:
    """Mark the completed execution span; no network on the run thread."""
    global _record_warning
    try:
        from inqtrix.observability.otel import enrich_current_span

        enrich_current_span(
            {
                **run_score_attributes(mode, state),
                semconv.INQTRIX_SCORES_READY: True,
                semconv.INQTRIX_SCORES_TRACE_ROOT: trace_root,
            }
        )
    except Exception:  # noqa: BLE001 — recording cannot affect publication
        if not _record_warning:
            _record_warning = True
            log.warning(
                "Run scores could not be recorded; score telemetry is incomplete."
            )


def record_deep_review_scores(verdict: Any) -> None:
    """Keep the model's judgement distinct from the success of its edits.

    ``deep_review_grounded`` is the verdict's ``grounded``: the outputs'
    statements are supported by the supplied material, or the review
    reported each gap - a finding on its passage or a not_checked point -;
    an output never has to say a gap itself.
    """
    global _record_warning
    try:
        from inqtrix.observability.otel import enrich_current_span

        enrich_current_span(
            {
                semconv.score_key("deep_review_findings"): len(verdict.findings),
                semconv.score_key("deep_review_complete"): verdict.complete,
                semconv.score_key("deep_review_grounded"): verdict.grounded,
                semconv.score_key(
                    "deep_review_contradictions_named"
                ): verdict.contradictions_named,
            }
        )
    except Exception:  # noqa: BLE001 — preserve the reviewed answer
        if not _record_warning:
            _record_warning = True
            log.warning(
                "Deep-review scores could not be recorded; score telemetry is incomplete."
            )


def score_bodies(spans: Sequence[Any]) -> list[dict[str, Any]]:
    """Build idempotent, content-free Langfuse score requests."""
    bodies = []
    for span in spans:
        attributes = span.attributes or {}
        if attributes.get(semconv.INQTRIX_SCORES_READY) is not True:
            continue
        context = span.context
        if context is None or not context.is_valid:
            continue
        trace_id = format(context.trace_id, "032x")
        observation_id = format(context.span_id, "016x")
        for name, kind in SCORE_TYPES.items():
            value = attributes.get(semconv.score_key(name))
            if not _valid_score(value, kind):
                continue
            body = {
                "id": str(
                    uuid5(NAMESPACE_URL, f"inqtrix:{trace_id}:{observation_id}:{name}")
                ),
                "traceId": trace_id,
                "name": name,
                "dataType": kind,
                "value": value if kind == "CATEGORICAL" else float(value),
                "comment": _COMMENT,
            }
            if attributes.get(semconv.INQTRIX_SCORES_TRACE_ROOT) is not True:
                body["observationId"] = observation_id
            bodies.append(body)
    return bodies


def build_score_processor(settings: Any) -> Any | None:
    """Reuse the SDK queue and shutdown contract, only for a Langfuse sink."""
    observability = settings.observability
    if not observability.trace_scores or observability.tracing != "otlp":
        return None
    from inqtrix.observability.trace_readers import (
        langfuse_auth_header,
        langfuse_base_url,
    )

    base = langfuse_base_url(
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    )
    authorization = langfuse_auth_header()
    if not base or not authorization:
        log.warning(
            "Run scores disabled: OTLP target or credentials do not identify Langfuse."
        )
        return None

    import httpx
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )

    class ScoreExporter(SpanExporter):
        def __init__(self) -> None:
            self.client = httpx.Client(timeout=5.0)
            self.warned = False

        def export(self, spans: Sequence[Any]) -> Any:
            try:
                for body in score_bodies(spans):
                    response = self.client.post(
                        f"{base}/api/public/scores",
                        json=body,
                        headers={"Authorization": authorization},
                    )
                    response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 — never touch the observed run
                if not self.warned:
                    self.warned = True
                    log.warning(
                        "Run-score export failed (error_type=%s); this batch is incomplete. "
                        "No durable retry is available; run results are unaffected.",
                        type(exc).__name__,
                    )
                return SpanExportResult.FAILURE
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            self.client.close()

    class CompletedRunProcessor(SpanProcessor):
        def __init__(self) -> None:
            self.batch = BatchSpanProcessor(ScoreExporter())

        def on_end(self, span: Any) -> None:
            if (span.attributes or {}).get(semconv.INQTRIX_SCORES_READY) is True:
                self.batch.on_end(span)

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return self.batch.force_flush(timeout_millis)

        def shutdown(self) -> None:
            self.batch.shutdown()

    return CompletedRunProcessor()
