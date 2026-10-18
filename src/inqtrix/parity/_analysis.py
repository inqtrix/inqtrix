"""LLM-assisted analysis -- prompts and report generation."""

from __future__ import annotations

import json
from typing import Any

from inqtrix.parity._types import (
    ALGORITHM_CONTEXT,
    DEFAULT_ANALYSIS_TIMEOUT,
    QuestionSpec,
)


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n... (gekuerzt)"


def _extract_confidence_sequence(iteration_logs: list[dict[str, Any]]) -> list[int]:
    return [
        int(entry.get("confidence", 0) or 0)
        for entry in iteration_logs
        if entry.get("node") == "evaluate" and entry.get("confidence") is not None
    ]


def _extract_planned_queries(iteration_logs: list[dict[str, Any]]) -> list[str]:
    queries: list[str] = []
    for entry in iteration_logs:
        if entry.get("node") == "plan":
            for query in entry.get("new_queries", []) or []:
                query_text = str(query).strip()
                if query_text:
                    queries.append(query_text)
    return queries


def _truncate_logs_for_prompt(logs: list[dict[str, Any]], max_chars: int = 5000) -> str:
    """Compact iteration logs for the LLM prompt without dropping core signals."""
    trimmed: list[dict[str, Any]] = []
    for entry in logs:
        compact = dict(entry)
        if "reasoning" in compact:
            compact["reasoning"] = _truncate_text(str(compact["reasoning"]), 500)
        if "sources_summary" in compact:
            sources_summary: list[dict[str, Any]] = []
            for source in compact["sources_summary"]:
                sources_summary.append(
                    {
                        "query": _truncate_text(str(source.get("query", "")), 160),
                        "summary": _truncate_text(str(source.get("summary", "")), 320),
                        "urls": list(source.get("urls", [])[:3]),
                    }
                )
            compact["sources_summary"] = sources_summary
        trimmed.append(compact)

    serialized = json.dumps(trimmed, ensure_ascii=False, indent=1)
    return _truncate_text(serialized, max_chars)


def resolve_analysis_model_name(
    configured_model: str,
    default_model: str,
    override_model: str | None = None,
) -> str:
    """Resolve the model name for optional LLM-assisted compare analysis."""
    return override_model or configured_model or default_model


def build_llm_analysis_prompts(
    baseline: dict[str, Any],
    current: dict[str, Any],
    questions: list[QuestionSpec],
) -> tuple[str, str]:
    """Build the system/user prompts for optional LLM-assisted compare analysis."""
    baseline_results = {
        result.get("question_id"): result
        for result in baseline.get("results", [])
        if result.get("question_id")
    }
    current_results = {
        result.get("question_id"): result
        for result in current.get("results", [])
        if result.get("question_id")
    }

    sections: list[str] = []
    for question in questions:
        baseline_result = baseline_results.get(question.id)
        current_result = current_results.get(question.id)
        if baseline_result is None or current_result is None:
            continue

        baseline_metrics = baseline_result.get("metrics", {})
        current_metrics = current_result.get("metrics", {})
        baseline_logs = baseline_result.get("iteration_logs", []) or []
        current_logs = current_result.get("iteration_logs", []) or []

        section = f"""
### {question.id}: {question.question}
Kategorie: {question.category}
Expected keywords: {', '.join(question.expected_keywords) or 'keine'}

Metriken:
- Confidence: baseline={baseline_metrics.get('final_confidence', 'n/a')} aktuell={current_metrics.get('final_confidence', 'n/a')}
- Runden: baseline={baseline_metrics.get('total_rounds', 'n/a')} aktuell={current_metrics.get('total_rounds', 'n/a')}
- Queries: baseline={baseline_metrics.get('total_queries', 'n/a')} aktuell={current_metrics.get('total_queries', 'n/a')}
- Quellen: baseline={baseline_metrics.get('total_citations', 'n/a')} aktuell={current_metrics.get('total_citations', 'n/a')}
- Aspect coverage: baseline={baseline_metrics.get('aspect_coverage_final', 'n/a')} aktuell={current_metrics.get('aspect_coverage_final', 'n/a')}
- Source quality: baseline={baseline_metrics.get('source_quality_score_final', 'n/a')} aktuell={current_metrics.get('source_quality_score_final', 'n/a')}
- Claim quality: baseline={baseline_metrics.get('claim_quality_score_final', 'n/a')} aktuell={current_metrics.get('claim_quality_score_final', 'n/a')}
- Evidence consistency: baseline={baseline_metrics.get('evidence_consistency_final', 'n/a')} aktuell={current_metrics.get('evidence_consistency_final', 'n/a')}
- Evidence sufficiency: baseline={baseline_metrics.get('evidence_sufficiency_final', 'n/a')} aktuell={current_metrics.get('evidence_sufficiency_final', 'n/a')}

Confidence-Verlauf:
- Baseline: {' -> '.join(str(value) for value in _extract_confidence_sequence(baseline_logs)) or 'n/a'}
- Aktuell: {' -> '.join(str(value) for value in _extract_confidence_sequence(current_logs)) or 'n/a'}

Plan-Queries:
- Baseline: {json.dumps(_extract_planned_queries(baseline_logs), ensure_ascii=False)[:800]}
- Aktuell: {json.dumps(_extract_planned_queries(current_logs), ensure_ascii=False)[:800]}

Antwort Baseline:
{_truncate_text(str(baseline_result.get('answer', '')), 900)}

Antwort Aktuell:
{_truncate_text(str(current_result.get('answer', '')), 900)}

Iteration Logs Baseline:
{_truncate_logs_for_prompt(baseline_logs)}

Iteration Logs Aktuell:
{_truncate_logs_for_prompt(current_logs)}
""".strip()
        sections.append(section)

    system_prompt = (
        "Du bist ein erfahrener Evaluator fuer iterative Research-Agenten. "
        "Analysiere Unterschiede zwischen Baseline und aktuellem Lauf praezise, "
        "mit Fokus auf Root Cause, algorithmische Regressionen/Verbesserungen und "
        "inhaltliche Antwortqualitaet. Schreibe auf Deutsch und in Markdown.\n\n"
        + ALGORITHM_CONTEXT
    )

    user_prompt = (
        "Analysiere den folgenden Parity-Vergleich zwischen einer Baseline und einem "
        "aktuellen Inqtrix-Lauf. Verwende die deterministischen Metriken als harte "
        "Signale, aber bewerte zusaetzlich den inhaltlichen und algorithmischen Verlauf.\n\n"
        "Strukturiere den Report in diese Abschnitte:\n"
        "## 1. Executive Summary\n"
        "Kurze Gesamtbewertung mit Verbesserung/Regression und Risiko-Einschaetzung.\n\n"
        "## 2. Analyse pro Frage\n"
        "Pro Frage: Metrik-Interpretation, Antwortqualitaet, wahrscheinlich betroffener "
        "Algorithmus-Schritt (classify/plan/search/evaluate/answer), Confidence-Verlauf, "
        "und eventuelle Stop-Logik-Auffaelligkeiten.\n\n"
        "## 3. Uebergreifende Muster\n"
        "Welche systematischen Muster siehst du ueber mehrere Fragen hinweg?\n\n"
        "## 4. Empfehlungen\n"
        "Konkrete technische Empfehlungen, priorisiert nach Impact.\n\n"
        "## 5. Gesamtbewertung\n"
        "Sollte dieser Lauf als neue Baseline promoted werden? Begruende klar.\n\n"
        f"Baseline-Zeitstempel: {baseline.get('meta', {}).get('timestamp', 'unbekannt')}\n"
        f"Aktueller Lauf-Zeitstempel: {current.get('meta', {}).get('timestamp', 'unbekannt')}\n\n"
        + "\n\n".join(sections)
    )
    return system_prompt, user_prompt


def generate_llm_analysis_report(
    baseline: dict[str, Any],
    current: dict[str, Any],
    questions: list[QuestionSpec],
    *,
    analysis_model: str | None = None,
    llm_provider: Any | None = None,
    default_model: str | None = None,
    analysis_timeout: int | None = None,
) -> tuple[str, str]:
    """Generate an optional LLM-assisted diagnostic report for parity comparison."""
    if llm_provider is None:
        from inqtrix.providers import create_providers
        from inqtrix.settings import Settings

        settings = Settings()
        llm_provider = create_providers(settings).llm
        default_model = settings.models.reasoning_model
        configured_timeout = settings.agent.reasoning_timeout
    else:
        configured_timeout = DEFAULT_ANALYSIS_TIMEOUT

    configured_model = ""
    used_model = resolve_analysis_model_name(
        configured_model, default_model or "", analysis_model)
    timeout = analysis_timeout or configured_timeout
    system_prompt, user_prompt = build_llm_analysis_prompts(baseline, current, questions)
    report = llm_provider.complete(
        user_prompt,
        system=system_prompt,
        model=used_model or None,
        timeout=float(timeout),
    )
    return report, used_model or (default_model or "default")
