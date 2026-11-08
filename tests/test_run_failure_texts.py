"""Every failure code that ends a run has a text in both desk languages.

The desk names a failed run by its code and shows the provider's raw text only
on request. A code without a text falls through unchanged: after a rate limit
the research card would show the provider's English 429 body with request id,
region and links, and after a restart the store's English diagnostic. The
codes come from the server, the texts from the desk's translation table; this
test holds the two together.

A run ends with a code from four places: the classifier (its table and the
callers' fallbacks), an explicit ``RunExecutionFailure``, a literal
``error_type`` that the run service or the worker hands to ``fail``, and the
terminal errors that the run store and the research graph write themselves.
Codes computed at run time (the grounding failures of a knowledge answer)
carry their own message in the answer language and are not in scope.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = ROOT / "src" / "inqtrix" / "execution_failures.py"
RUN_FAILING_MODULES = (
    ROOT / "src" / "inqtrix" / "services" / "run_service.py",
    ROOT / "src" / "inqtrix" / "worker" / "loop.py",
)
TERMINAL_ERROR_MODULES = (
    ROOT / "src" / "inqtrix" / "runs" / "postgres_store.py",
    ROOT / "src" / "inqtrix" / "graph.py",
)
TRANSLATIONS = ROOT / "apps" / "research-desk" / "src" / "i18n" / "translations.ts"


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _classifier_codes() -> set[str]:
    tree = ast.parse(CLASSIFIER.read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "classify_execution_failure"
    )
    codes: set[str] = set()
    for default in [*function.args.defaults, *function.args.kw_defaults]:
        if (code := _string(default)) is not None:
            codes.add(code)
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and (code := _string(node.value)) is not None:
            codes.add(code)
        elif isinstance(node, ast.Dict):
            codes.update(code for value in node.values if (code := _string(value)) is not None)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and (code := _string(node.args[1])) is not None
        ):
            codes.add(code)
    return codes


def _caller_fallbacks() -> set[str]:
    codes: set[str] = set()
    for path in (ROOT / "src" / "inqtrix").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None))
                == "classify_execution_failure"
            ):
                for keyword in node.keywords:
                    if keyword.arg == "fallback" and (code := _string(keyword.value)):
                        codes.add(code)
    return codes


def _call_name(node: ast.Call) -> str | None:
    return getattr(node.func, "id", getattr(node.func, "attr", None))


def _explicit_failures() -> set[str]:
    codes: set[str] = set()
    for path in (ROOT / "src" / "inqtrix").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "RunExecutionFailure"
                and node.args
                and (code := _string(node.args[0])) is not None
            ):
                codes.add(code)
    for path in RUN_FAILING_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node) in {"fail", "afail"}:
                codes.update(
                    code for keyword in node.keywords
                    if keyword.arg == "error_type" and (code := _string(keyword.value))
                )
    for path in TERMINAL_ERROR_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                keys = [_string(key) for key in node.keys]
                if "message" in keys and "type" in keys:
                    if (code := _string(node.values[keys.index("type")])) is not None:
                        codes.add(code)
    return codes


def _run_codes() -> set[str]:
    return _classifier_codes() | _caller_fallbacks() | _explicit_failures()


def _desk_tables() -> dict[str, set[str]]:
    text = TRANSLATIONS.read_text(encoding="utf-8")
    blocks = re.findall(r"\n(\s+)failureCodes: \{\n(.*?)\n\1\},", text, re.S)
    tables = [
        set(re.findall(r"^\s+'?([a-z0-9_.]+)'?:", body, re.M)) for _indent, body in blocks
    ]
    assert len(tables) == 2, "expected one failure table per desk language (de, en)"
    return {"de": tables[0], "en": tables[1]}


def test_the_scan_finds_the_codes_it_must_find() -> None:
    codes = _run_codes()
    # Anchors from every source; a broken scan would lose them. The full set
    # is derived above.
    assert {
        "rate_limited", "provider_timeout", "server_error", "model_capacity", "task_failed",
        "knowledge_scope_missing", "answer_publication_conflict", "token_budget_exceeded",
        "max_retries_exceeded", "execution_lost", "queued_timeout", "server_restarted",
    } <= codes


def test_every_failure_code_has_a_text_in_both_desk_languages() -> None:
    codes = _run_codes()
    for language, table in _desk_tables().items():
        missing = sorted(codes - table)
        assert not missing, f"{language}: failure codes without a desk text: {missing}"
