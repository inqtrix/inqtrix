"""Argparse setup and CLI command handlers for parity tooling."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib import error as urllib_error

from inqtrix.parity._analysis import generate_llm_analysis_report
from inqtrix.parity._compare import compare_runs
from inqtrix.parity._contract import build_contract_report, format_contract_report
from inqtrix.parity._io import (
    _timestamp_token,
    import_baselines,
    load_baseline_dir,
    load_questions,
    load_run_file,
    run_questions_via_http,
    save_compare_markdown_report,
    save_csv_report,
    save_markdown_report,
    save_report,
    save_run,
)
from inqtrix.parity._reporting import format_compare_csv, format_compare_report
from inqtrix.parity._types import (
    DEFAULT_BASELINES_DIR,
    DEFAULT_ENDPOINT,
    DEFAULT_QUESTIONS_FILE,
    DEFAULT_REPORTS_DIR,
    DEFAULT_RUNS_DIR,
    DEFAULT_TIMEOUT,
    QuestionSpec,
)


def _resolve_questions(selected_id: str | None, questions: list[QuestionSpec]) -> list[QuestionSpec]:
    if selected_id is None:
        return questions
    return [question for question in questions if question.id == selected_id]


def _command_contract(args: argparse.Namespace) -> int:
    questions = load_questions(args.questions_file)
    reference_questions = load_questions(
        args.reference_questions) if args.reference_questions else None
    baseline = load_baseline_dir(args.baseline_dir) if args.baseline_dir else None
    report = build_contract_report(
        questions=questions,
        reference_questions=reference_questions,
        baseline=baseline,
    )
    print(format_contract_report(report))
    return 1 if report["summary"]["errors"] else 0


def _command_run(args: argparse.Namespace) -> int:
    questions = _resolve_questions(args.question, load_questions(args.questions_file))
    if not questions:
        print(f"Unknown question id: {args.question}", file=sys.stderr)
        return 2

    try:
        run_data = run_questions_via_http(
            endpoint=args.endpoint,
            questions=questions,
            timeout=args.timeout,
        )
    except urllib_error.HTTPError as exc:
        print(f"HTTP error while running parity suite: {exc}", file=sys.stderr)
        return 2
    except urllib_error.URLError as exc:
        print(f"Endpoint unreachable: {exc}", file=sys.stderr)
        return 2

    run_path = save_run(run_data, args.output_dir)
    print(f"Saved run to {run_path}")
    return 0


def _command_compare(args: argparse.Namespace) -> int:
    questions = load_questions(args.questions_file)
    baseline = load_baseline_dir(args.baseline_dir)
    current = load_run_file(args.run_file)
    report = compare_runs(baseline, current, questions)
    rendered_report = format_compare_report(report)
    rendered_csv = format_compare_csv(report)
    print(rendered_report)
    stamp = _timestamp_token()
    report_path = save_report(report, args.report_dir, stamp=stamp)
    markdown_path = save_compare_markdown_report(
        rendered_report,
        output_dir=args.report_dir,
        stamp=stamp,
    )
    csv_path = save_csv_report(rendered_csv, output_dir=args.report_dir, stamp=stamp)
    print(f"Saved report JSON to {report_path}")
    print(f"Saved report Markdown to {markdown_path}")
    print(f"Saved report CSV to {csv_path}")
    if args.llm_analysis:
        try:
            analysis_report, used_model = generate_llm_analysis_report(
                baseline,
                current,
                questions,
                analysis_model=args.analysis_model,
            )
        except Exception as exc:
            print(f"LLM analysis failed: {exc}", file=sys.stderr)
        else:
            if analysis_report.strip():
                print("\nLLM analysis report\n")
                print(analysis_report)
                markdown_path = save_markdown_report(
                    analysis_report,
                    analysis_model=used_model,
                    output_dir=args.report_dir,
                    stamp=stamp,
                )
                print(f"Saved LLM analysis to {markdown_path}")
    return 1 if report["summary"]["failed"] else 0


def _command_import_baselines(args: argparse.Namespace) -> int:
    try:
        copied = import_baselines(args.source_dir, args.output_dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Imported {len(copied)} baseline files to {Path(args.output_dir)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the parity CLI parser."""
    parser = argparse.ArgumentParser(description="Inqtrix parity tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)

    contract = subparsers.add_parser("contract", help="Validate parity assets")
    contract.add_argument("--questions-file", default=str(DEFAULT_QUESTIONS_FILE))
    contract.add_argument("--reference-questions")
    contract.add_argument("--baseline-dir")
    contract.set_defaults(func=_command_contract)

    run = subparsers.add_parser("run", help="Run canonical questions via /v1/test/run")
    run.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    run.add_argument("--questions-file", default=str(DEFAULT_QUESTIONS_FILE))
    run.add_argument("--question")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run.add_argument("--output-dir", default=str(DEFAULT_RUNS_DIR))
    run.set_defaults(func=_command_run)

    compare = subparsers.add_parser("compare", help="Compare a run against baselines")
    compare.add_argument("run_file")
    compare.add_argument("--baseline-dir", default=str(DEFAULT_BASELINES_DIR))
    compare.add_argument("--questions-file", default=str(DEFAULT_QUESTIONS_FILE))
    compare.add_argument("--report-dir", default=str(DEFAULT_REPORTS_DIR))
    compare.add_argument("--llm-analysis", action="store_true")
    compare.add_argument("--analysis-model")
    compare.set_defaults(func=_command_compare)

    import_baseline_parser = subparsers.add_parser(
        "import-baselines",
        help="Copy Litellm baseline files into tests/integration/baselines",
    )
    import_baseline_parser.add_argument("source_dir")
    import_baseline_parser.add_argument("--output-dir", default=str(DEFAULT_BASELINES_DIR))
    import_baseline_parser.set_defaults(func=_command_import_baselines)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for parity tooling."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
