"""Guard owned log templates; interpolated user/provider content is untouched.

The reviewed vocabulary catches known German prose, not every language mistake.
New diagnostics still require editorial review. Dynamic templates are admitted
only at named producers whose emitted wording is checked by functional tests.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re


_SOURCE = Path(__file__).resolve().parents[1] / "src" / "inqtrix"
_LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
_GERMAN_WORDS = frozenset(
    """
    abbruch abgebrochen abgeschlossen abschliessen abschlussphase aktiviert
    aktiv aktivitaetspruefung angefordert angehaengt angenommen angemeldete
    antwort antworten anfrage anzahl aufgeloester aufloesbaren aufraeumen
    aufruf ausfuehrung ausgeloest ausgewertet beurteilt bestaetigt bestehende
    beendet benoetigt bereits bereinigung beschraenkt bleibt bleiben
    durchlauf eingereicht eintraege eintrag englisch enthaelt ergebnis
    erfolgreich erlaubten erneut ersetzen fehlgeschlagen fehlgeschlagene
    fehlt fehlend fehler fliessband fluechtigem folgt fortsetzung frage
    freigaben freigegeben fuer für geaendert gekappt geklemmt gekuerzt
    geladen geladener geloescht gespeichert gespeichertem gespeicherte
    gesendet gesetzt gestoppt gestaged gueltigen gueltiges haengt hinweis
    hoechstens ignoriert inkompatibel kandidat kandidaten keine kein keiner
    kindes kindern laenger laeuft lauf laufende laufzeit lesbar
    loeschoperation loeschoperationen mehr meldet meldung meldungen mittlere
    nachgeholt naechste naechsten naechster nachrecherche negativ nicht
    noetig ohne pruefbar pruefung quell quellen quellenrolle quellenrollen
    rechenzeit restliche restkriterium rueckfragen sachluecke sammlungen
    schemafehler schluessel sektion sektionen serverseitig serverseitigem
    sichtbar sonde spielraum sprache standardstufe startet stillschweigend
    stufe teilaufgabe teilfragen telemetrie uebernehmen uebernommen
    ueberschreiten ueberschritten uebersprungen uebertragen uebrig
    ungueltig ungueltiger ungueltige unbekannte unbekannten unbekanntes
    unerwartetem unerwarteten unvollstaendig unveraendert unveraenderten
    veraltet veraltete veraltetes verfuegbar verfuegbare vergleich verloren
    verletzt veroeffentlicht verworfen versuch versuchen vollstaendig
    vollstaendige vollstaendigkeit vorab vorhandene vorgemerkt vorherige
    vorgang wartet wartete warteten wechselt wechselte weitergeschrieben
    werte widerspricht wieder wiederholt wird wurden wurde zeichen zeile
    zeilen zeitbudget zusaetzlicher zurueckgesetzt zustaendig zustellversuchen
    designprinzip
    """.split()
)
_DYNAMIC_PRODUCERS = Counter({
    ("nodes.py", "_emit_node_model_resolution_warning", "message"): 1,
    ("providers/anthropic.py", "AnthropicLLM.__init__", "warning"): 1,
    ("providers/azure.py", "AzureOpenAILLM.__init__", "warning"): 1,
    ("providers/bedrock.py", "BedrockLLM.__init__", "warning"): 1,
})


def _log_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LEVELS
        ):
            continue
        receiver = node.func.value
        named_logger = isinstance(receiver, ast.Name) and receiver.id in {
            "log", "_log", "logger"
        }
        inline_logger = (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Attribute)
            and isinstance(receiver.func.value, ast.Name)
            and receiver.func.value.id == "logging"
            and receiver.func.attr == "getLogger"
        )
        if named_logger or inline_logger:
            calls.append(node)
    return calls


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ".".join(reversed(names))


def test_owned_log_templates_use_english():
    failures = []
    dynamic = Counter()
    static_count = 0
    for path in sorted(_SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        relative = path.relative_to(_SOURCE).as_posix()
        for call in _log_calls(tree):
            template = call.args[0] if call.args else next(
                (keyword.value for keyword in call.keywords if keyword.arg == "msg"),
                None,
            )
            if isinstance(template, ast.Constant) and isinstance(template.value, str):
                static_count += 1
                words = set(re.findall(r"[^\W\d_]+", template.value.casefold()))
                found = words & _GERMAN_WORDS
                if found:
                    failures.append(f"{relative}:{call.lineno}: {sorted(found)}")
            else:
                expression = ast.unparse(template) if template is not None else "<missing>"
                dynamic[(relative, _scope(call, parents), expression)] += 1
    assert static_count > 0, "No log templates inspected"
    assert not failures, "German prose in owned log templates:\n" + "\n".join(failures)
    assert dynamic == _DYNAMIC_PRODUCERS, (
        "Review dynamic log producers and pin their emitted messages: "
        f"added={dynamic - _DYNAMIC_PRODUCERS}, removed={_DYNAMIC_PRODUCERS - dynamic}"
    )


def test_log_discovery_includes_inline_loggers_and_ignores_parser_errors():
    tree = ast.parse(
        'log.warning("first")\n'
        '_log.error("second")\n'
        'logger.info(msg="third")\n'
        'logging.getLogger("inqtrix").warning("fourth")\n'
        'parser.error("command-line validation")\n'
    )
    assert [call.lineno for call in _log_calls(tree)] == [1, 2, 3, 4]
