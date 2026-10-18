"""The span outcome vocabulary has one edit point: ``semconv``.

The run span, the indexing span and the failure chokepoint spelled their
outcome words inline while the tool span read the same words from
``semconv``: one vocabulary with several edit points, so a renamed outcome
would have drifted silently between span types.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "inqtrix"
_OWN_VOCABULARY = {"server/routers/session_titles.py"}
"""Modules whose span writes its own reason codes under the outcome key."""


def _outcome_values(tree: ast.AST) -> Iterator[ast.expr]:
    """Every value expression written under ``semconv.INQTRIX_OUTCOME``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Attribute) and key.attr == "INQTRIX_OUTCOME":
                    yield value
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_attribute"
            and len(node.args) > 1
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "INQTRIX_OUTCOME"
        ):
            yield node.args[1]


def test_span_outcomes_are_written_from_semconv_constants():
    inspected = 0
    offenders = []
    for path in sorted(_SOURCE.rglob("*.py")):
        relative = path.relative_to(_SOURCE).as_posix()
        if relative in _OWN_VOCABULARY:
            continue
        for value in _outcome_values(ast.parse(path.read_text(encoding="utf-8"))):
            inspected += 1
            literals = [
                node.value
                for node in ast.walk(value)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            if literals:
                offenders.append(f"{relative}:{value.lineno}: {literals}")
    assert inspected > 0, "No outcome writes inspected"
    assert not offenders, "Outcome words spelled inline:\n" + "\n".join(offenders)
