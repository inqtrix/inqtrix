"""No NEW German prose in the agent runtime's own strings.

The run-visible texts live in :data:`inqtrix.i18n.MESSAGES` so they
follow the answer language. The table has its own gates; this one guards
the other half -- the string literals in the code, where German prose
drifts back silently. Such a residue is invisible to the table gates,
because those read the table and nothing else.

**How it decides, and what it cannot do.** The question "is this fragment
German?" has no reliable answer: ``detect_ui_language`` reports ENGLISH for
"Synthese fehlgeschlagen." -- two words, no umlaut, no stopword -- and so
would any other detector. So this gate does not ask it. It runs a
one-sided test instead: a literal is flagged when it reads as prose (a
space, at least twelve characters) and carries a German MARK, meaning a
real umlaut, an eszett, or a German function word. English text has none
of those, so a hit is evidence; a German fragment without a mark slips
through, and that bound is deliberate and stated rather than hidden.

Two calibrations, each against a false positive:

* Quoted spans are stripped first. English documentation that cites a
  German example (``"... a VISIBLE '... und N weitere' line"``) is correct
  English, not a finding.
* ``die`` and ``war`` are NOT marks although they are common German words:
  they collide with ordinary English ("runs then die with the process",
  "the war in ..."). A gate that cries wolf on English gets switched off.

**Not every baseline entry is a defect.** Nine of them sit in code that is
ALREADY bilingual through its own small mechanism -- ``editor_instructions``
picks per locale via ``_localized_warning``, ``evidence`` and the knowledge
paths branch on ``language == "de"``. Those read correctly to a user;
moving them into the table is consolidation, not repair, and it is worth
doing only when that code is touched anyway. The rest are German-only and
are real. Keeping both kinds in one list is deliberate -- the gate asks
"does this come from the table?", and both answers are "no" -- but the
distinction matters when an entry is worked off.

**The baseline may only shrink.** Listing the existing literals keeps
the gate hard for new code while the debt stays visible and
countable -- an always-red gate would be a mask, and deleting the finding
would be worse. Fix one, remove its line; the test fails if an entry
disappears from the code but stays in this list, so the list cannot rot.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

# A German mark: umlaut, eszett, or a function word with no English twin.
GERMAN_MARK = re.compile(
    r"[äöüßÄÖÜ]"
    r"|\b(der|das|dem|den|des|und|oder|nicht|kein|keine|keinen|wurde|wurden"
    r"|wird|werden|ist|sind|waren|eine|einer|einem|einen|vor|nach|beim|im"
    r"|mit|ohne|weil|dass|wegen|noch|schon|mehr|nur|auch|aber|sich|sein|seine"
    r"|diese|dieser|dieses|jede|jeder|alle|allen)\b"
)
QUOTED = re.compile(r"``[^`]*``|`[^`]*`|'[^']*'|\"[^\"]*\"")
MIN_PROSE_CHARS = 12

RUNTIME = pathlib.Path("src/inqtrix")

# Exempt by FILE, each for a stated reason -- never "it is noisy".
EXEMPT = {
    # German is the point of these modules, not a defect.
    "i18n.py": "the message table itself",
    "prompt_language.py": "the per-language answer directives",
    "model_cards.py": (
        "server-side model descriptions; /health is unauthenticated and "
        "knows no locale, so the frontend translates and falls back to "
        "these (the 7f decision)"
    ),
    "default_prompt_seed.py": "seed prompt templates the operator edits",
    # Developer tooling, never shown to a product user.
    "replay.py": "trace-replay CLI help",
    "_analysis.py": "parity evaluation tool",
    "_compare.py": "parity evaluation tool",
    "_types.py": "parity evaluation tool",
    "chat_bridge.py": "ImportError for a missing extra, read by developers",
}

# Known residue, predating the gate. MAY ONLY SHRINK.
BASELINE: set[tuple[str, str]] = {
    # agents/algorithm.py
    ('agents/algorithm.py', '---\n\n> Hinweis: Das Memo wurde zwischenzeitlich manuell bearbeitet. Di'),
    # agents/control_ports.py
    ('agents/control_ports.py', 'Aufgabe vor der Ausfuehrung abgebrochen.'),
    ('agents/control_ports.py', 'Der Lauf endete, bevor die Freigabe entschieden wurde.'),
    # agents/kernel/algorithm.py
    # agents/kernel/tools.py
    ('agents/kernel/tools.py', "Der Agent-Kernel braucht das 'agent'-Extra (uv sync --extra agent)."),
    # agents/limit_contract.py
    ('agents/limit_contract.py', 'teilstand übernehmen'),
    # evidence.py
    ('evidence.py', 'aus einer Suche'),
    ('evidence.py', 'belegfähige Quelle'),
    ('evidence.py', 'belegfähige Quellen'),
    # knowledge/algorithm.py
    ('knowledge/algorithm.py', 'Die Antwort wurde nicht veröffentlicht, weil das Modell keine vollstän'),
    # knowledge/grounding.py
    ('knowledge/grounding.py', 'Die Antwort wurde nicht veröffentlicht, weil der erforderliche Belegbl'),
    ('knowledge/grounding.py', 'Die Antwort wurde nicht veröffentlicht, weil mindestens ein als wörtli'),
    # nodes.py
    ('nodes.py', ': nicht zugeordnet]'),
    ('nodes.py', 'Belege ausserhalb des Evidenz-Budgets ('),
    ('nodes.py', 'Evidenzlage noch zu duenn ('),
    ('nodes.py', 'Mehrere zentrale Aussagen sind zwischen Quellen umstritten.'),
    ('nodes.py', 'Was ist die Primärquelle für'),
    ('nodes.py', 'Welche Primärdaten verifizieren die zentralen Zahlen zu'),
    ('nodes.py', 'Welche Quellen beantworten die offene Evidenzlücke'),
    ('nodes.py', 'Welche glaubwürdige Evidenz widerspricht den wichtigsten Aussagen zu'),
    ('nodes.py', 'Welche unabhängigen Quellen bestätigen oder widerlegen die Aussage, da'),
    # project/asset_records_ports.py
    ('project/asset_records_ports.py', 'Temporäre Dateien'),
    # server/editor_instructions.py
    ('server/editor_instructions.py', 'Append-Änderung mit Anker wurde auf Einfügen nach Anker normalisiert.'),
    ('server/editor_instructions.py', 'Eine vorgeschlagene Textstelle konnte nicht eindeutig im Dokument vali'),
    ('server/editor_instructions.py', 'Leere Änderung verworfen.'),
    ('server/editor_instructions.py', 'Änderung ohne Anker verworfen.'),
}


def _german_prose_literals(source: str) -> list[tuple[int, str]]:
    """Prose literals carrying a German mark, docstrings excluded.

    A pattern handed to ``re.compile`` is skipped: German words inside one
    are language PROCESSING, not text a reader sees, and translating them
    would break the parser. Structural, so it needs no per-file exemption
    -- the earlier file-level exemption in the summary gate was too coarse
    and hid four real findings.
    """
    tree = ast.parse(source)
    docs = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        )
    }
    patterns: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
        ):
            patterns.update(
                child.lineno
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
            )
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.lineno in patterns:
            continue
        value = node.value
        stripped = value.strip()
        if (
            value in docs
            or " " not in stripped
            or len(stripped) < MIN_PROSE_CHARS
            or not GERMAN_MARK.search(QUOTED.sub(" ", value))
        ):
            continue
        found.append((node.lineno, stripped[:70]))
    return found


def _runtime_files() -> list[pathlib.Path]:
    return sorted(p for p in RUNTIME.rglob("*.py") if p.name not in EXEMPT)


@pytest.mark.parametrize(
    "path", _runtime_files(), ids=lambda p: str(p.relative_to(RUNTIME))
)
def test_no_new_german_prose_in_runtime_strings(path: pathlib.Path):
    rel = str(path.relative_to(RUNTIME))
    fresh = [
        (line, text)
        for line, text in _german_prose_literals(path.read_text(encoding="utf-8"))
        if (rel, text) not in BASELINE
    ]
    assert not fresh, (
        f"{rel}: German prose in a runtime string. Text the run produces "
        f"belongs in inqtrix.i18n.MESSAGES and reaches the call site through "
        f"t(...), so it follows the answer language. New at "
        + ", ".join(f"line {line} ({text[:45]!r})" for line, text in fresh)
    )


def test_the_baseline_cannot_rot():
    """An entry that no longer exists in the code must leave this list.

    Without this the list would quietly keep granting exemptions for text
    that was fixed long ago -- and the next real finding could hide behind
    a stale line.
    """
    present = set()
    for path in _runtime_files():
        rel = str(path.relative_to(RUNTIME))
        for _line, text in _german_prose_literals(
            path.read_text(encoding="utf-8")
        ):
            present.add((rel, text))
    stale = sorted(BASELINE - present)
    assert not stale, (
        "BASELINE lists text that is no longer in the code -- delete these "
        f"lines: {stale}"
    )


def test_the_gate_reads_what_it_claims_to_read():
    """Prove the instrument, including both calibrations."""
    # German prose is found.
    assert _german_prose_literals('x = "Die Aufgabe wurde abgebrochen."')
    # ASCII substitutes still carry function words, so they are found too.
    assert _german_prose_literals('x = "Aufgabe vor der Ausfuehrung beendet."')
    # English prose is not.
    assert not _german_prose_literals('x = "The task was cancelled early."')
    # English that CITES German is not.
    assert not _german_prose_literals(
        'x = "cut with a VISIBLE \'... und N weitere\' line"'
    )
    # The two collision words are not marks.
    assert not _german_prose_literals(
        'x = "Interrupted runs then die with the process, logged loudly."'
    )
    assert not _german_prose_literals('x = "The war in numbers, by source."')
    # Docstrings are prose about the code, not text the run emits.
    assert not _german_prose_literals('def f():\n    """Die Aufgabe hier."""\n')
    # A key is not prose.
    assert not _german_prose_literals('x = t(s, "agent_task_summary_failed")')
    # A regex is language processing, not text.
    assert not _german_prose_literals(
        'P = re.compile(r"nicht mehr als (\\d+) s[aä]tze")'
    )
