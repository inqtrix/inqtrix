"""Guard owned log templates, the shape of their arguments and the logger names.

The reviewed vocabulary catches known German prose, not every language mistake.
New diagnostics still require editorial review. Dynamic templates are admitted
only at named producers whose emitted wording is checked by functional tests.

A log argument never turns an object into its raw text: ``str()``, ``repr()``,
``.args``, the sanitizers and ``_clip()`` pass on whatever the object carries,
including prompts, model output or document text. The check reads top-level
arguments only, so ``len(str(x))`` passes; text that reaches a log line through
a plain variable is covered by the content tests.

Every module of ``inqtrix`` logs under its own import path, so a level can be
set per area and the ``logger`` field names the module; the web gateway logs
under ``inqtrix.web_gateway``. Outside the ``inqtrix.`` tree the log filter
would treat a record as foreign.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re


_SOURCE = Path(__file__).resolve().parents[1] / "src"
_PACKAGES = ("inqtrix", "inqtrix_web_gateway")
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
    ("inqtrix/nodes.py", "_emit_node_model_resolution_warning", "message"): 1,
    ("inqtrix/providers/anthropic.py", "AnthropicLLM.__init__", "warning"): 1,
    ("inqtrix/providers/azure.py", "AzureOpenAILLM.__init__", "warning"): 1,
    ("inqtrix/providers/bedrock.py", "BedrockLLM.__init__", "warning"): 1,
})
_RAW_TEXT_CALLS = frozenset({"str", "repr", "sanitize_error", "sanitize_log_message", "_clip"})
# The logging configuration speaks for the process: its status line, crash
# hooks and notices go to the family root, and it reads the web level.
_CONFIGURATION_MODULE = "inqtrix.logging_config"
_CONFIGURATION_LOGGERS = Counter({"inqtrix": 4, "uvicorn": 1})
_GATEWAY_FAMILY = "inqtrix.web_gateway"
# Alembic loads the migration environment and its revisions from their file
# paths, under names such as ``env_py``.
_LOADED_BY_PATH = Path("inqtrix", "storage", "migrations")


def _source_files() -> list[Path]:
    return [path for package in _PACKAGES for path in sorted((_SOURCE / package).rglob("*.py"))]


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
            and receiver.func.value.id in {"logging", "stdlib_logging"}
            and receiver.func.attr == "getLogger"
        )
        if named_logger or inline_logger:
            calls.append(node)
    return calls


def _passed_through(expression: ast.expr) -> list[ast.expr]:
    """The expression and every part that a conditional or an index hands on."""

    if isinstance(expression, ast.IfExp):
        return _passed_through(expression.body) + _passed_through(expression.orelse)
    if isinstance(expression, ast.BoolOp):
        return [part for value in expression.values for part in _passed_through(value)]
    if isinstance(expression, ast.Subscript):
        return [expression, *_passed_through(expression.value)]
    return [expression]


def _is_raw_text(expression: ast.expr) -> bool:
    if isinstance(expression, ast.Attribute):
        return expression.attr == "args"
    if not isinstance(expression, ast.Call):
        return False
    function = expression.func
    if isinstance(function, ast.Name):
        return function.id in _RAW_TEXT_CALLS
    return isinstance(function, ast.Attribute) and function.attr in _RAW_TEXT_CALLS


def _raw_text_arguments(call: ast.Call) -> list[str]:
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    return [
        ast.unparse(argument)
        for argument in arguments
        if any(_is_raw_text(part) for part in _passed_through(argument))
    ]


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ".".join(reversed(names))


def _module_name(path: Path) -> str:
    parts = path.relative_to(_SOURCE).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _other_runtime_name(path: Path, tree: ast.Module) -> str | None:
    """The ``__name__`` a module gets when it does not run under its import path."""

    if path.relative_to(_SOURCE).is_relative_to(_LOADED_BY_PATH):
        return re.sub(r"\W", "_", path.name)
    runs_as_main = path.name == "__main__.py" or any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(
            isinstance(comparator, ast.Constant) and comparator.value == "__main__"
            for comparator in node.test.comparators
        )
        for node in tree.body
    )
    return "__main__" if runs_as_main else None


def _logger_names(tree: ast.AST, module: str, *, other_name: str | None) -> list[tuple[int, set[str]]]:
    """Every ``getLogger`` call with the names it can produce while the module runs."""

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute) and function.attr == "getLogger"
            or isinstance(function, ast.Name) and function.id == "getLogger"
        ):
            continue
        argument = node.args[0] if node.args else next(
            (keyword.value for keyword in node.keywords if keyword.arg == "name"), None
        )
        if isinstance(argument, ast.Name) and argument.id == "__name__":
            names = {module} if other_name is None else {module, other_name}
        elif isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            names = {argument.value}
        else:
            names = {f"<{ast.unparse(argument) if argument is not None else 'root'}>"}
        found.append((node.lineno, names))
    return sorted(found, key=lambda item: item[0])


def test_owned_log_templates_use_english():
    failures = []
    dynamic = Counter()
    static_count = 0
    for path in _source_files():
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
        'stdlib_logging.getLogger("inqtrix.web_gateway").info("fifth")\n'
        'parser.error("command-line validation")\n'
    )
    assert [call.lineno for call in _log_calls(tree)] == [1, 2, 3, 4, 5]


def test_log_arguments_never_carry_raw_text():
    failures = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(_SOURCE).as_posix()
        for call in _log_calls(tree):
            failures.extend(f"{relative}:{call.lineno}: {text}" for text in _raw_text_arguments(call))
    assert not failures, (
        "Raw text as a log argument; log a type name, a code, a count or an id instead:\n"
        + "\n".join(failures)
    )


def test_raw_text_detection_reads_top_level_arguments_and_their_branches():
    tree = ast.parse(
        'log.warning("a %s", str(exc))\n'
        'log.warning("a %s", exc.args[0] if exc.args else type(exc).__name__)\n'
        'log.warning("a %s", str(exc) or "?")\n'
        'log.warning("a %s", str(exc)[:200])\n'
        'log.warning("a %s", _clip(review.detail, 2000))\n'
        'stdlib_logging.getLogger("inqtrix.web_gateway").info("a %s", repr(value))\n'
        'log.error("a %s", urls.sanitize_error(exc))\n'
        'log.warning("a %d", len(str(exc)))\n'
        'log.warning("a %s", type(exc).__name__)\n'
        'log.warning("a %s", task_id if known else "<task>")\n'
    )
    flagged = [call.lineno for call in _log_calls(tree) if _raw_text_arguments(call)]
    assert flagged == [1, 2, 3, 4, 5, 6, 7]


def test_every_module_logs_under_its_own_name():
    failures = []
    packages = set()
    configuration = Counter()
    for path in _source_files():
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package = module.split(".", 1)[0]
        relative = path.relative_to(_SOURCE).as_posix()
        for line, names in _logger_names(tree, module, other_name=_other_runtime_name(path, tree)):
            packages.add(package)
            if module == _CONFIGURATION_MODULE:
                configuration.update(names)
                continue
            if package == "inqtrix_web_gateway":
                wrong = {
                    name
                    for name in names
                    if name != _GATEWAY_FAMILY and not name.startswith(f"{_GATEWAY_FAMILY}.")
                }
            else:
                wrong = names - {module}
            if wrong:
                failures.append(f"{relative}:{line}: {sorted(wrong)}")
    assert packages == set(_PACKAGES), "No logger lookups inspected"
    assert not failures, (
        "Name the logger after the module with getLogger(__name__), and write the import path out "
        "where the module runs under another name (as the main program, or loaded by Alembic "
        f"from its file); the web gateway logs under {_GATEWAY_FAMILY}:\n" + "\n".join(failures)
    )
    assert configuration == _CONFIGURATION_LOGGERS, (
        "Review the loggers the logging configuration addresses: "
        f"added={configuration - _CONFIGURATION_LOGGERS}, "
        f"removed={_CONFIGURATION_LOGGERS - configuration}"
    )


def test_logger_name_discovery_resolves_what_a_module_produces():
    tree = ast.parse(
        'log = logging.getLogger(__name__)\n'
        'logging.getLogger("inqtrix").warning("inline")\n'
        'gateway = stdlib_logging.getLogger("inqtrix.web_gateway")\n'
        'root = getLogger()\n'
        'named = logging.getLogger(name)\n'
        'keyword = logging.getLogger(name=__name__)\n'
    )
    assert _logger_names(tree, "inqtrix.example", other_name=None) == [
        (1, {"inqtrix.example"}),
        (2, {"inqtrix"}),
        (3, {"inqtrix.web_gateway"}),
        (4, {"<root>"}),
        (5, {"<name>"}),
        (6, {"inqtrix.example"}),
    ]
    assert _logger_names(tree, "inqtrix.example", other_name="__main__")[0] == (
        1,
        {"inqtrix.example", "__main__"},
    )
    package = _SOURCE / "inqtrix"
    guarded = ast.parse('if __name__ == "__main__":\n    main()\n')
    plain = ast.parse("main()\n")
    assert _other_runtime_name(package / "tool.py", guarded) == "__main__"
    assert _other_runtime_name(package / "worker" / "__main__.py", plain) == "__main__"
    assert _other_runtime_name(package / "storage" / "migrations" / "env.py", plain) == "env_py"
    assert (
        _other_runtime_name(package / "storage" / "migrations" / "versions" / "0001_a.py", plain)
        == "0001_a_py"
    )
    assert _other_runtime_name(package / "tool.py", plain) is None
    assert _module_name(_SOURCE / "inqtrix" / "worker" / "__main__.py") == "inqtrix.worker.__main__"
    assert _module_name(_SOURCE / "inqtrix" / "providers" / "__init__.py") == "inqtrix.providers"
