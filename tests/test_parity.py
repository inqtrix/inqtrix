"""Tests for the local parity tooling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from inqtrix.constants import (
    MAX_TOTAL_SECONDS,
    REASONING_TIMEOUT,
    REQUEST_WAIT_MARGIN_SECONDS,
)
from inqtrix.parity import (
    DEFAULT_ANALYSIS_TIMEOUT,
    DEFAULT_QUESTIONS_FILE,
    DEFAULT_TIMEOUT,
    ParityTolerance,
    QuestionSpec,
    build_contract_report,
    build_compare_check_rows,
    build_llm_analysis_prompts,
    compare_runs,
    format_compare_csv,
    format_compare_report,
    generate_llm_analysis_report,
    import_baselines,
    load_questions,
    normalize_test_result,
    resolve_analysis_model_name,
)


def _result(question_id: str, *, confidence: int = 7, rounds: int = 2) -> dict:
    return {
        "question_id": question_id,
        "question": "Question",
        "category": "news",
        "answer": "DeepSeek R1 caused a stock crash in China.",
        "top_sources": [
            {"url": "https://www.bundestag.de/dokumente/x", "tier": "primary"}
        ],
        "top_claims": [
            {
                "text": "DeepSeek R1 was involved.",
                "status": "verified",
                "claim_type": "fact",
                "needs_primary": True,
                "status_reason": "mehrfach bestaetigt",
                "support_count": 2,
                "contradict_count": 0,
                "source_tier_counts": {
                    "primary": 1,
                    "mainstream": 1,
                    "stakeholder": 0,
                    "unknown": 0,
                    "low": 0,
                },
                "sources": ["https://www.bundestag.de/dokumente/x"],
            }
        ],
        "metrics": {
            "final_confidence": confidence,
            "total_rounds": rounds,
            "total_queries": 9,
            "total_citations": 60,
            "aspect_coverage_final": 0.8,
            "source_quality_score_final": 0.6,
            "claim_quality_score_final": 0.55,
            "evidence_consistency_final": 7,
            "evidence_sufficiency_final": 7,
            "competing_events_detected": False,
            "stagnation_detected": False,
            "falsification_triggered": False,
            "utility_stop_triggered": False,
            "plateau_stop_triggered": True,
            "source_tier_counts_final": {
                "primary": 2,
                "mainstream": 4,
                "stakeholder": 0,
                "unknown": 1,
                "low": 0,
            },
            "claim_status_counts_final": {
                "verified": 3,
                "contested": 1,
                "unverified": 1,
            },
        },
        "iteration_logs": [],
    }


class TestParityQuestions:

    def test_canonical_questions_file_loads(self):
        questions = load_questions(DEFAULT_QUESTIONS_FILE)
        assert [question.id for question in questions] == [
            "q001",
            "q002",
            "q003",
            "q004",
            "q005",
            "q006",
        ]

    def test_contract_report_for_canonical_questions_has_no_errors(self):
        questions = load_questions(DEFAULT_QUESTIONS_FILE)
        report = build_contract_report(questions=questions)
        assert report["summary"]["errors"] == 0


class TestImportBaselines:

    def test_import_baselines_copies_reference_files(self, tmp_path: Path):
        source_dir = tmp_path / "source"
        output_dir = tmp_path / "output"
        source_dir.mkdir()
        (source_dir / "q001.json").write_text('{"result": {"question_id": "q001"}}')
        (source_dir / "q002.json").write_text('{"result": {"question_id": "q002"}}')
        (source_dir / "baseline.json.migrated").write_text('{"results": []}')

        copied = import_baselines(source_dir, output_dir)

        assert [path.name for path in copied] == [
            "q001.json",
            "q002.json",
            "baseline.json.migrated",
        ]
        assert (output_dir / "q001.json").exists()
        assert (output_dir / "q002.json").exists()
        assert (output_dir / "baseline.json.migrated").exists()


class _StubAnalysisLLM:

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, prompt, *, system=None, model=None, timeout=0.0, **kwargs):
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "model": model,
                "timeout": timeout,
            }
        )
        return "## Analyse\nAlles nachvollziehbar."


class TestNormalizeTestResult:

    def test_normalize_test_result_keeps_reference_shape(self):
        question = QuestionSpec(
            id="q001",
            question="What happened?",
            category="news",
            expected_keywords=("DeepSeek",),
        )
        raw = {
            "answer": "DeepSeek did it.",
            "metrics": {"final_confidence": 7},
            "iteration_logs": [{"node": "classify"}],
            "top_claims": [{"text": "DeepSeek did it.", "status": "verified"}],
        }

        normalized = normalize_test_result(question, raw)
        assert normalized["question_id"] == "q001"
        assert normalized["category"] == "news"
        assert normalized["metrics"]["final_confidence"] == 7
        assert normalized["top_claims"][0]["status"] == "verified"


class TestCompareRuns:

    def test_compare_runs_passes_within_tolerance(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("DeepSeek", "Crash"),
        )
        baseline = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}
        current = {"meta": {}, "results": [_result("q001", confidence=8, rounds=2)]}

        report = compare_runs(baseline, current, [question], ParityTolerance())
        assert report["summary"]["failed"] == 0
        assert report["questions"][0]["status"] == "pass"
        assert report["summary"]["checks"]["failed"] == 0
        assert report["questions"][0]["check_summary"]["passed"] > 0

    def test_compare_runs_fails_on_major_metric_drift(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("DeepSeek",),
        )
        baseline = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}
        current = {"meta": {}, "results": [_result("q001", confidence=3, rounds=4)]}

        report = compare_runs(baseline, current, [question], ParityTolerance())
        assert report["summary"]["failed"] == 1
        assert report["questions"][0]["status"] == "fail"
        assert any(
            check["key"] == "final_confidence" and check["status"] == "fail"
            for check in report["questions"][0]["checks"]
        )

    def test_compare_runs_warns_on_missing_keywords_only(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("Anthropic",),
        )
        baseline = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}
        current = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}

        report = compare_runs(baseline, current, [question], ParityTolerance())
        assert report["summary"]["failed"] == 0
        assert report["summary"]["warnings"] == 1
        assert report["questions"][0]["status"] == "warn"

    def test_build_compare_check_rows_flattens_metric_matrix(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("Anthropic",),
        )
        baseline = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}
        current = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}

        report = compare_runs(baseline, current, [question], ParityTolerance())
        rows = build_compare_check_rows(report, only_flagged=True)

        assert any(row["metric_key"] == "expected_keywords" for row in rows)
        assert all(row["status"] != "pass" for row in rows)
        assert rows[0]["current_top_claims"]

    def test_format_compare_report_renders_markdown_tables(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("Anthropic",),
        )
        baseline = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}
        current = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}

        report = compare_runs(baseline, current, [question], ParityTolerance())
        rendered = format_compare_report(report)

        assert "## Question overview" in rendered
        assert "| Question | Status |" in rendered
        assert "## Flagged checks" in rendered
        assert "## Claim snapshots" in rendered
        assert "Current top claims:" in rendered

    def test_format_compare_csv_exports_all_metric_checks(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("DeepSeek",),
        )
        baseline = {"meta": {}, "results": [_result("q001", confidence=7, rounds=2)]}
        current = {"meta": {}, "results": [_result("q001", confidence=8, rounds=2)]}

        report = compare_runs(baseline, current, [question], ParityTolerance())
        csv_text = format_compare_csv(report)

        assert "question_id,question,category,question_status,metric_key" in csv_text
        assert "baseline_top_claims,current_top_claims" in csv_text
        assert "q001,Question,news,pass,final_confidence" in csv_text


class TestLlmCompareAnalysis:

    def test_timeout_defaults_follow_the_shared_agent_contract(self):
        assert DEFAULT_TIMEOUT == (
            MAX_TOTAL_SECONDS + REQUEST_WAIT_MARGIN_SECONDS
        )
        assert DEFAULT_ANALYSIS_TIMEOUT == REASONING_TIMEOUT

    def test_resolve_analysis_model_name_prefers_override(self):
        assert resolve_analysis_model_name(
            "configured-model", "reasoning-model", "override") == "override"
        assert resolve_analysis_model_name(
            "configured-model", "reasoning-model", None) == "configured-model"
        assert resolve_analysis_model_name("", "reasoning-model", None) == "reasoning-model"

    def test_build_llm_analysis_prompts_contains_question_and_metrics(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("DeepSeek",),
        )
        baseline = {"meta": {"timestamp": "2026-03-29T10:00:00Z"}, "results": [_result("q001")]}
        current = {"meta": {"timestamp": "2026-03-29T11:00:00Z"},
                   "results": [_result("q001", confidence=8)]}

        system_prompt, user_prompt = build_llm_analysis_prompts(baseline, current, [question])

        assert "classify" in system_prompt
        assert "Question" in user_prompt
        assert "Confidence" in user_prompt

    def test_generate_llm_analysis_report_uses_selected_model(self):
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
            expected_keywords=("DeepSeek",),
        )
        baseline = {"meta": {}, "results": [_result("q001", confidence=7)]}
        current = {"meta": {}, "results": [_result("q001", confidence=8)]}
        llm = _StubAnalysisLLM()

        report, used_model = generate_llm_analysis_report(
            baseline,
            current,
            [question],
            analysis_model="analysis-model",
            llm_provider=llm,
            default_model="reasoning-model",
            analysis_timeout=42,
        )

        assert report.startswith("## Analyse")
        assert used_model == "analysis-model"
        assert llm.calls[0]["model"] == "analysis-model"
        assert llm.calls[0]["timeout"] == 42.0
        assert "Question" in llm.calls[0]["prompt"]

    def test_auto_created_analysis_uses_effective_agent_timeout(self, monkeypatch):
        import inqtrix.providers as providers_module
        import inqtrix.settings as settings_module

        llm = _StubAnalysisLLM()
        settings = SimpleNamespace(
            agent=SimpleNamespace(reasoning_timeout=777),
            models=SimpleNamespace(reasoning_model="configured-model"),
        )
        monkeypatch.setattr(settings_module, "Settings", lambda: settings)
        monkeypatch.setattr(
            providers_module,
            "create_providers",
            lambda _settings: SimpleNamespace(llm=llm),
        )
        question = QuestionSpec(
            id="q001",
            question="Question",
            category="news",
        )

        generate_llm_analysis_report(
            {"meta": {}, "results": [_result("q001", confidence=7)]},
            {"meta": {}, "results": [_result("q001", confidence=8)]},
            [question],
        )

        assert llm.calls[0]["timeout"] == 777.0
