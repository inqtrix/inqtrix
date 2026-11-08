"""One event loop per process: the rules a parse of the source can check.

A known violation stands in ``async_exceptions.py`` with its count. The list is a
ratchet in both directions, per module and rule: one more hit is red, and one fewer
is red until the entry shrinks with it. A message names the file and line the scanner
read, never a line written down by hand.
"""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.architecture.async_exceptions import ASYNC_EXCEPTIONS, AsyncException
from tests.architecture.import_graph import SOURCE_ROOT, iter_module_files

LOOP_ENTRY_MODULES = ("inqtrix.worker.__main__", "inqtrix.aio", "inqtrix.storage.migrate")
THREAD_BRIDGE_MODULE = "inqtrix.aio"
LOOP_CREATORS = ("asyncio.run", "asyncio.Runner", "new_event_loop", "run_until_complete")
THREAD_CREATORS = (
    "threading.Thread",
    "ThreadPoolExecutor",
    "asyncio.to_thread",
    "to_thread",
    "run_in_executor",
)
LEGACY_BRIDGE = ("aio.call_from_thread", "aio.submit_from_thread", "aio.run_legacy_segment")
BLOCKING_IN_ASYNC = (
    "time.sleep",
    "urllib.request.urlopen",
    "urlopen",
    "requests.",
    "httpx.Client",
    "httpx.get",
    "httpx.post",
    "httpx.put",
    "httpx.patch",
    "httpx.delete",
    "httpx.request",
    "openai.OpenAI",
    "OpenAI",
    "openai.AzureOpenAI",
    "AzureOpenAI",
    "boto3.client",
    "valkey.Valkey",
    "redis.Redis",
    "qdrant_client.QdrantClient",
    "QdrantClient",
    "psycopg.connect",
    "subprocess.run",
    "call_from_thread",
    "hash_password",
    "verify_password",
)
THREADING_PRIMITIVES = (
    "threading.Lock",
    "threading.RLock",
    "threading.Semaphore",
    "threading.BoundedSemaphore",
    "threading.Event",
    "threading.Condition",
)
HOOK_PAIRS = {
    "before_agent": "abefore_agent",
    "before_model": "abefore_model",
    "after_model": "aafter_model",
    "after_agent": "aafter_agent",
    "wrap_model_call": "awrap_model_call",
    "wrap_tool_call": "awrap_tool_call",
}
MIDDLEWARE_BASE = "AgentMiddleware"
ENGINE_ALLOWED = (
    "inqtrix.storage.db",
    "inqtrix.storage.migrate",
    "inqtrix.storage.connection_budget",
    "inqtrix.storage.runtime_contract",
)


@dataclass(frozen=True)
class Hit:
    module: str
    rule: str
    line: int


def _dotted(node: ast.AST) -> str:
    """Return ``a.b.c`` for a name or an attribute chain, else an empty string.

    A call inside the chain is read through: ``asyncio.get_running_loop().x`` is
    ``asyncio.get_running_loop.x``.
    """
    parts: list[str] = []
    while isinstance(node, (ast.Attribute, ast.Call)):
        if isinstance(node, ast.Call):
            node = node.func
            continue
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _matches(called: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if pattern.endswith("."):
            if called.startswith(pattern):
                return True
        elif called == pattern or called.startswith(f"{pattern}."):
            return True
        elif "." not in pattern and called.endswith(f".{pattern}"):
            return True
    return False


def _calls(node: ast.AST) -> Iterator[ast.Call]:
    return (child for child in ast.walk(node) if isinstance(child, ast.Call))


def _own_body(function: ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """Walk an async function without entering a function nested inside it."""
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _primitive_attributes(tree: ast.Module) -> set[str]:
    """Attributes this module builds from a blocking ``threading`` primitive."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _matches(_dotted(node.value.func), THREADING_PRIMITIVES):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        names.add(target.attr)
                    elif isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _swallows(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    kinds = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    broad = caught is None or any(
        _dotted(kind).split(".")[-1] in {"CancelledError", "BaseException"}
        for kind in kinds
        if kind is not None
    )
    return broad and not any(isinstance(node, ast.Raise) for node in ast.walk(handler))


def _middleware_classes(trees: dict[str, ast.Module]) -> set[str]:
    """The names of all classes that derive from the framework's middleware base."""
    bases: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names = {_dotted(base).split(".")[-1] for base in node.bases}
                bases.setdefault(node.name, set()).update(names)
    found = {MIDDLEWARE_BASE}
    while True:
        more = {name for name, parents in bases.items() if parents & found} - found
        if not more:
            return found - {MIDDLEWARE_BASE}
        found |= more


def module_hits(module: str, tree: ast.Module, middleware: set[str]) -> Iterator[Hit]:
    """Yield every violation of the parsed rules in one module."""
    if module not in LOOP_ENTRY_MODULES:
        for call in _calls(tree):
            if _matches(_dotted(call.func), LOOP_CREATORS):
                yield Hit(module, "loop", call.lineno)
    if module != THREAD_BRIDGE_MODULE:
        for call in _calls(tree):
            if _matches(_dotted(call.func), THREAD_CREATORS):
                yield Hit(module, "thread", call.lineno)
            elif _matches(_dotted(call.func), LEGACY_BRIDGE):
                yield Hit(module, "legacy_bridge", call.lineno)
    primitives = _primitive_attributes(tree)
    for function in ast.walk(tree):
        if isinstance(function, ast.AsyncFunctionDef):
            for node in _own_body(function):
                if not isinstance(node, ast.Call):
                    continue
                called = _dotted(node.func)
                if _matches(called, BLOCKING_IN_ASYNC):
                    yield Hit(module, "blocking", node.lineno)
                elif isinstance(node.func, ast.Attribute) and node.func.attr in {"acquire", "wait"}:
                    if _dotted(node.func.value).split(".")[-1] in primitives:
                        yield Hit(module, "blocking", node.lineno)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in middleware:
            defined = {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for hook, async_hook in HOOK_PAIRS.items():
                if hook in defined and async_hook not in defined:
                    yield Hit(module, "hook_pair", node.lineno)
        if isinstance(node, ast.ExceptHandler) and _swallows(node):
            yield Hit(module, "cancel", node.lineno)
    if module not in ENGINE_ALLOWED:
        for node in ast.walk(tree):
            if isinstance(node, ast.alias) and node.name == "NullPool":
                yield Hit(module, "null_pool", node.lineno)
            elif isinstance(node, ast.Call):
                called = _dotted(node.func).rsplit(".", 1)[-1]
                if called in {"build_engine", "create_async_engine"}:
                    yield Hit(module, "engine", node.lineno)


def _parsed() -> dict[str, ast.Module]:
    return {
        module: ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
        for module, path in iter_module_files()
    }


def live_hits() -> list[Hit]:
    trees = _parsed()
    middleware = _middleware_classes(trees)
    return [hit for module, tree in trees.items() for hit in module_hits(module, tree, middleware)]


def locations(hits: Iterable[Hit]) -> dict[tuple[str, str], list[int]]:
    """The lines behind each module and rule, as the scanner read them."""
    found: dict[tuple[str, str], list[int]] = {}
    for hit in hits:
        found.setdefault((hit.module, hit.rule), []).append(hit.line)
    return found


def describe(
    violations: Iterable[tuple[str, str]],
    where: dict[tuple[str, str], list[int]],
    paths: dict[str, str],
    known: dict[tuple[str, str], int] | None = None,
) -> str:
    """One line per violation: its hits, what its entry allows, and its places in line order.

    The file follows from the module; a line with several hits says how many.
    """
    lines = []
    for module, rule in violations:
        found = Counter(where.get((module, rule), []))
        places = ", ".join(
            f"{paths.get(module, module)}:{line}" + (f" ({count}x)" if count > 1 else "")
            for line, count in sorted(found.items())
        ) or "no scanned line"
        allowed = f", the list allows {known.get((module, rule), 0)}" if known is not None else ""
        lines.append(f"{module} {rule}: {sum(found.values())} hits{allowed} at {places}")
    return "\n".join(lines)


def ratchet_report(
    hits: list[Hit], known: dict[tuple[str, str], int], paths: dict[str, str]
) -> tuple[str, str]:
    """The ratchet's two messages: violations above their entry, and entries above what is left."""
    new, stale = count_ratchet(Counter((hit.module, hit.rule) for hit in hits), known)
    where = locations(hits)
    return describe(sorted(new), where, paths, known), describe(sorted(stale), where, paths, known)


def _shown_paths() -> dict[str, str]:
    return {module: path.relative_to(SOURCE_ROOT.parent).as_posix() for module, path in iter_module_files()}


def count_ratchet(
    live: Counter[tuple[str, str]], known: dict[tuple[str, str], int]
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    """Return what exceeds its entry and the entries above what is left."""
    new = {key: count for key, count in live.items() if count > known.get(key, 0)}
    stale = {key: count for key, count in known.items() if live.get(key, 0) < count}
    return new, stale


def test_the_counts_match_the_list_exactly() -> None:
    hits = live_hits()
    live = Counter((hit.module, hit.rule) for hit in hits)
    known = {(entry.module, entry.rule): entry.count for entry in ASYNC_EXCEPTIONS}
    new, stale = count_ratchet(live, known)
    print(
        f"edges checked {sum(live.values())}, violations {sum(new.values())}, "
        f"exceptions live {len(known) - len(stale)}"
    )
    new_message, stale_message = ratchet_report(hits, known, _shown_paths())
    assert not new_message, "new async violations:\n" + new_message
    assert not stale_message, "entries above their live count, shrink them:\n" + stale_message


def test_an_entry_stands_once_with_a_reason() -> None:
    keys = [(entry.module, entry.rule) for entry in ASYNC_EXCEPTIONS]
    assert len(keys) == len(set(keys))
    assert all(entry.reason.strip() and entry.count > 0 for entry in ASYNC_EXCEPTIONS)


def kernel_entries(entries: Iterable[AsyncException]) -> list[str]:
    return [
        entry.module
        for entry in entries
        if entry.module == "inqtrix.harness" or entry.module.startswith("inqtrix.harness.")
    ]


def test_the_kernel_never_gets_an_entry() -> None:
    assert not kernel_entries(ASYNC_EXCEPTIONS)


def test_an_entry_for_the_kernel_is_seen() -> None:
    entries = [
        AsyncException("inqtrix.harness.dispatch", "thread", 1, "why"),
        AsyncException("inqtrix.harnessing", "thread", 1, "why"),
    ]
    assert kernel_entries(entries) == ["inqtrix.harness.dispatch"]


def test_a_new_violation_names_the_lines_the_scanner_read() -> None:
    hits = [
        Hit("inqtrix.runs.probe", "thread", 40),
        Hit("inqtrix.runs.probe", "thread", 7),
        Hit("inqtrix.runs.other", "loop", 3),
    ]
    paths = {"inqtrix.runs.probe": "src/inqtrix/runs/probe.py"}
    assert describe([("inqtrix.runs.probe", "thread")], locations(hits), paths) == (
        "inqtrix.runs.probe thread: 2 hits at src/inqtrix/runs/probe.py:7, src/inqtrix/runs/probe.py:40"
    )


def test_the_report_counts_hits_and_marks_a_line_with_several() -> None:
    """Two hits on one line count twice, as the ratchet counts them; an entry above its hits names them too."""
    hits = [Hit("m", "hook_pair", 196), Hit("m", "hook_pair", 196), Hit("m", "hook_pair", 275)]
    paths = {"m": "src/m.py"}
    assert ratchet_report(hits, {("m", "hook_pair"): 2}, paths) == (
        "m hook_pair: 3 hits, the list allows 2 at src/m.py:196 (2x), src/m.py:275", "",
    )
    assert ratchet_report(hits[:1], {("m", "hook_pair"): 2}, paths) == (
        "", "m hook_pair: 1 hits, the list allows 2 at src/m.py:196",
    )


def test_the_ratchet_counts_both_ways() -> None:
    key, other, gone = ("a", "thread"), ("b", "blocking"), ("c", "loop")
    new, stale = count_ratchet(Counter({key: 3, other: 1}), {key: 2, other: 2, gone: 1})
    assert new == {key: 3}
    assert stale == {other: 2, gone: 1}
    assert count_ratchet(Counter({key: 2}), {key: 2}) == ({}, {})


def _hits(source: str, module: str = "inqtrix.runs.probe") -> list[str]:
    tree = ast.parse(textwrap.dedent(source))
    return [hit.rule for hit in module_hits(module, tree, _middleware_classes({module: tree}))]


@pytest.mark.parametrize(
    ("rule", "source"),
    [
        ("loop", "import asyncio\nasyncio.run(work())"),
        ("loop", "import asyncio\nloop = asyncio.new_event_loop()"),
        ("loop", "loop.run_until_complete(work())"),
        ("thread", "import asyncio\nasync def f():\n    await asyncio.to_thread(work)"),
        ("thread", "loop.run_in_executor(None, work)"),
        ("thread", "import asyncio\nasyncio.get_running_loop().run_in_executor(None, work)"),
        ("thread", "import threading\nthreading.Thread(target=work)"),
        ("legacy_bridge", "from inqtrix import aio\naio.call_from_thread(work())"),
        ("legacy_bridge", "from inqtrix import aio\nasync def f():\n    await aio.run_legacy_segment(work)"),
        ("thread", "from concurrent.futures import ThreadPoolExecutor\nThreadPoolExecutor()"),
        ("blocking", "import time\nasync def f():\n    time.sleep(1)"),
        ("blocking", "import valkey\nasync def f():\n    valkey.Valkey.from_url('x')"),
        ("blocking", "import requests\nasync def f():\n    requests.get('x')"),
        (
            "blocking",
            """
            import threading
            class Holder:
                def __init__(self):
                    self._gate = threading.BoundedSemaphore(2)
                async def use(self):
                    self._gate.acquire()
            """,
        ),
        (
            "hook_pair",
            """
            class Hook(AgentMiddleware):
                def before_model(self, state):
                    return state
            """,
        ),
        (
            "hook_pair",
            """
            class Base(AgentMiddleware):
                pass
            class Hook(Base):
                def before_agent(self, state):
                    return state
            """,
        ),
        ("cancel", "import asyncio\ntry:\n    work()\nexcept asyncio.CancelledError:\n    pass"),
        ("cancel", "try:\n    work()\nexcept BaseException:\n    pass"),
        ("cancel", "try:\n    work()\nexcept:\n    pass"),
        ("null_pool", "from sqlalchemy.pool import NullPool\nbuild(poolclass=NullPool)"),
        ("engine", "build_engine(url, null_pool=True)"),
        ("engine", "build_engine(url, null_pool=flag)"),
        ("engine", "build_engine(url)"),
        ("engine", "from sqlalchemy.ext.asyncio import create_async_engine\ncreate_async_engine(url)"),
        ("engine", "from inqtrix.storage import db\ndb.build_engine(url)"),
    ],
)
def test_a_violation_of_each_rule_is_seen(rule: str, source: str) -> None:
    assert rule in _hits(source)


@pytest.mark.parametrize(
    "source",
    [
        "import time\ndef f():\n    time.sleep(1)",
        "import time\nasync def f():\n    def later():\n        time.sleep(1)\n    return later",
        "import asyncio\ntry:\n    work()\nexcept asyncio.CancelledError:\n    raise",
        "try:\n    work()\nexcept Exception:\n    pass",
        "build_process_engine(storage)",
        "import threading\nclass H:\n    def __init__(self):\n        self._gate = other()\n    async def f(self):\n        await self._gate.acquire()",
        """
        class Hook(AgentMiddleware):
            def before_model(self, state):
                return state
            async def abefore_model(self, state):
                return state
        """,
    ],
)
def test_what_the_rules_allow_stays_allowed(source: str) -> None:
    assert _hits(source) == []


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("inqtrix.worker.__main__", "import asyncio\nasyncio.run(work())"),
        ("inqtrix.aio", "import asyncio\nloop = asyncio.new_event_loop()"),
        ("inqtrix.storage.migrate", "import asyncio\nasyncio.run(work())"),
        ("inqtrix.aio", "import asyncio\nasync def f():\n    await asyncio.to_thread(work)"),
        ("inqtrix.storage.db", "build_engine(url, null_pool=True)"),
    ],
)
def test_the_named_modules_may(module: str, source: str) -> None:
    assert _hits(source, module) == []
