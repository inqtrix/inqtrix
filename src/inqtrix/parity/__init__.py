"""Parity tooling for auditing Inqtrix against reference research-agent runs.

This package keeps the verification workflow inside the Inqtrix repository.
It can:

- validate the canonical question contract
- execute test runs against the local HTTP test endpoint
- save run artifacts in the Litellm-compatible schema
- compare a run against baseline artifacts deterministically
- optionally generate an LLM-assisted diagnostic report over the full run traces
"""

from __future__ import annotations

# --- types & constants ---
from inqtrix.parity._types import (
    ALGORITHM_CONTEXT,
    CLAIM_REPORT_LIMIT,
    CLAIM_SNAPSHOT_LIMIT,
    DEFAULT_ANALYSIS_TIMEOUT,
    DEFAULT_BASELINES_DIR,
    DEFAULT_ENDPOINT,
    DEFAULT_QUESTIONS_FILE,
    DEFAULT_REPORTS_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_TIMEOUT,
    HEALTH_ENDPOINT,
    Issue,
    MetricCheck,
    PROJECT_ROOT,
    ParityTolerance,
    QuestionSpec,
    TEST_ENDPOINT,
)

# --- I/O ---
from inqtrix.parity._io import (
    import_baselines,
    load_baseline_dir,
    load_questions,
    load_run_file,
    normalize_test_result,
    run_questions_via_http,
    save_compare_markdown_report,
    save_csv_report,
    save_markdown_report,
    save_report,
    save_run,
)

# --- contract validation ---
from inqtrix.parity._contract import (
    build_contract_report,
    compare_question_contracts,
    format_contract_report,
    validate_question_contract,
)

# --- comparison engine ---
from inqtrix.parity._compare import compare_runs

# --- reporting ---
from inqtrix.parity._reporting import (
    build_compare_check_rows,
    build_compare_overview_rows,
    format_compare_csv,
    format_compare_report,
)

# --- LLM analysis ---
from inqtrix.parity._analysis import (
    build_llm_analysis_prompts,
    generate_llm_analysis_report,
    resolve_analysis_model_name,
)

# --- CLI ---
from inqtrix.parity._cli import build_parser, main

__all__ = [
    # types & constants
    "ALGORITHM_CONTEXT",
    "CLAIM_REPORT_LIMIT",
    "CLAIM_SNAPSHOT_LIMIT",
    "DEFAULT_ANALYSIS_TIMEOUT",
    "DEFAULT_BASELINES_DIR",
    "DEFAULT_ENDPOINT",
    "DEFAULT_QUESTIONS_FILE",
    "DEFAULT_REPORTS_DIR",
    "DEFAULT_RUNS_DIR",
    "DEFAULT_TIMEOUT",
    "HEALTH_ENDPOINT",
    "Issue",
    "MetricCheck",
    "PROJECT_ROOT",
    "ParityTolerance",
    "QuestionSpec",
    "TEST_ENDPOINT",
    # I/O
    "import_baselines",
    "load_baseline_dir",
    "load_questions",
    "load_run_file",
    "normalize_test_result",
    "run_questions_via_http",
    "save_compare_markdown_report",
    "save_csv_report",
    "save_markdown_report",
    "save_report",
    "save_run",
    # contract
    "build_contract_report",
    "compare_question_contracts",
    "format_contract_report",
    "validate_question_contract",
    # compare
    "compare_runs",
    # reporting
    "build_compare_check_rows",
    "build_compare_overview_rows",
    "format_compare_csv",
    "format_compare_report",
    # analysis
    "build_llm_analysis_prompts",
    "generate_llm_analysis_report",
    "resolve_analysis_model_name",
    # CLI
    "build_parser",
    "main",
]
