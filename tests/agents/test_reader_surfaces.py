"""Every place that hands text to a reader is classified, and derived.

The classification lives in :mod:`inqtrix.agents.reader_surfaces`; this
module is what makes it true. It reads the reader-surface calls out of
the runtime source and holds them against the map, so a surface added
without an answer to "whose words are these" fails here rather than
carrying model text unnoticed until somebody reads a transcript.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from inqtrix.agents.artifact_names import NAMED_ARTIFACT_KINDS
from inqtrix.agents.control_ports import ARTIFACT_KINDS
from inqtrix.agents.narration import NARRATION_EVENT
from inqtrix.agents.reader_surfaces import (
    MACHINERY_ARTIFACT_KINDS,
    MODEL_TEXT_GUARDED,
    NOT_READ,
    READER_TEXT_ARGUMENTS,
    READER_TEXT_AUTHOR,
    RUNTIME_TEXT,
    USER_TEXT,
    reader_surface_key,
)
from inqtrix.i18n import MESSAGES

RUNTIME = pathlib.Path("src/inqtrix")


def _callee(call: ast.Call) -> str:
    """The plain name of what is being called, however it is reached."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _artifact_kinds(call: ast.Call) -> set[str]:
    """The artifact kinds one ``upsert_artifact`` call can write.

    A conditional kind is BOTH of its branches: a call that keeps the
    kind it found writes whatever was there, and reading only one side
    would let a claim about machinery hold for a file.
    """
    for keyword in call.keywords:
        if keyword.arg != "kind":
            continue
        node = keyword.value
        branches = (
            [node.body, node.orelse] if isinstance(node, ast.IfExp) else [node]
        )
        return {
            branch.value
            for branch in branches
            if isinstance(branch, ast.Constant) and isinstance(branch.value, str)
        }
    return set()


def _surface_calls(
    node: ast.AST, inside_entry_point: bool = False
) -> list[ast.Call]:
    """Every reader-surface call under *node*, relays excluded.

    A call from one declared entry point into another forwards a text it
    was handed; the surfaces are that entry point's OWN callers, and
    counting the forwarding line too would collapse them all into it.
    """
    calls: list[ast.Call] = []
    if isinstance(node, ast.Call) and _callee(node) in READER_TEXT_ARGUMENTS:
        if not inside_entry_point:
            calls.append(node)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        inside_entry_point = (
            inside_entry_point or node.name in READER_TEXT_ARGUMENTS
        )
    for child in ast.iter_child_nodes(node):
        calls.extend(_surface_calls(child, inside_entry_point))
    return calls


def _port_parameters() -> dict[str, tuple[str, ...]]:
    """Each port's POSITIONAL parameter names, read from its definition.

    Without this the enumeration reads only keyword arguments, and an
    argument passed by position is a reader surface the list cannot see:
    ``upsert_artifact(run_id, kind, title)`` would silently drop a title
    from the map, and the test that exists to find unclassified surfaces
    would pass. ``emit_answer(answer)`` is that case today.

    Derived, like everything else here: the names come from the ``def``
    in the tree, never from a copy of the signature. Several definitions
    of one port (a Protocol and its implementations) must agree on the
    positional prefix, or the mapping would depend on which file was read
    last - so disagreement fails loudly instead.
    """
    seen: dict[str, set[tuple[str, ...]]] = {}
    for path in sorted(RUNTIME.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in READER_TEXT_ARGUMENTS:
                continue
            names = tuple(
                argument.arg
                for argument in [*node.args.posonlyargs, *node.args.args]
            )
            seen.setdefault(node.name, set()).add(names)
    disagreeing = {
        name: sorted(orders) for name, orders in seen.items() if len(orders) > 1
    }
    assert not disagreeing, (
        "a reader-surface port is defined with two different positional "
        f"orders, so a positional argument has no one name: {disagreeing}"
    )
    return {name: next(iter(orders)) for name, orders in seen.items()}


def _named_arguments(call: ast.Call, positional: tuple[str, ...]):
    """Every argument of *call* as (name, value), positional ones included.

    A method call spends the first parameter on the receiver, so it is
    dropped when the callee is reached through an attribute and the
    definition starts with ``self``.
    """
    names = list(positional)
    if isinstance(call.func, ast.Attribute) and names[:1] in (["self"], ["cls"]):
        names = names[1:]
    for index, value in enumerate(call.args):
        if isinstance(value, ast.Starred) or index >= len(names):
            break
        yield names[index], value
    for keyword in call.keywords:
        if keyword.arg:
            yield keyword.arg, keyword.value


def _reader_surfaces() -> dict[str, dict[str, object]]:
    """Every reader surface in the runtime, read out of the source.

    Derived from the choke points named in
    :data:`READER_TEXT_ARGUMENTS`, never from a list of places: a surface
    can only exist by calling one of them, so walking their call sites is
    the one reading that cannot be out of date. Each surface keeps what
    the later assertions need — the literal it writes, if it writes one,
    and the artifact kinds it writes them under.
    """
    positional = _port_parameters()
    found: dict[str, dict[str, object]] = {}
    for path in sorted(RUNTIME.rglob("*.py")):
        module = (
            str(path.relative_to(RUNTIME).with_suffix("")).replace("/", ".")
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _surface_calls(tree):
            callee = _callee(node)
            arguments = READER_TEXT_ARGUMENTS[callee]
            kinds = _artifact_kinds(node)
            for name, value in _named_arguments(
                node, positional.get(callee, ())
            ):
                if name not in arguments:
                    continue
                key = reader_surface_key(
                    module, callee, name, ast.unparse(value)
                )
                entry = found.setdefault(
                    key, {"expression": value, "kinds": set()}
                )
                entry["kinds"] |= kinds  # type: ignore[operator]
    return found


REASON_CODES = frozenset({"overflow"})
"""Strings in a reader-text expression that are codes, not prose.

A reason a function maps to a message of its own
(``not_asked_narration(entries, 'overflow', state)``) is spelled at the
call site and translated inside. It is neither reader text nor an i18n
key, so it needs an answer of its own; there is one of them, and a
second is a good moment to look at whether that function should take the
message key instead.
"""


def _spelled_text(expression: ast.AST) -> set[str]:
    """Every string in *expression* that is neither a key nor a code.

    Reading only a top-level constant misses the shape that actually
    occurs: the text spelled INSIDE an expression, as the fallback of a
    lookup or a branch of a conditional. The memo's canvas name carried
    an English ``'Memo'`` that way, past a test written to catch exactly
    that, because the argument as a whole was a call and not a constant.

    What is skipped is skipped structurally, never by how a string
    looks: a ``.get`` key, a subscript, a dict key. What survives has to
    be an :mod:`inqtrix.i18n` key — singular, or the ``_one``/``_other``
    pair ``t_count`` splits into — or one of the declared reason codes.
    """
    spelled: set[str] = set()

    class _Reader(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            skip = set()
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                skip.add(id(node.args[0]))
            for child in ast.iter_child_nodes(node):
                if id(child) not in skip:
                    self.visit(child)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            self.visit(node.value)

        def visit_Dict(self, node: ast.Dict) -> None:
            for value in node.values:
                self.visit(value)

        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            # A key BUILT from a variable part (``f"kernel_recovery_
            # {reason}"``) is still a key. Accepted only when the fixed
            # part actually names a family in the message table, so a
            # sentence with a placeholder in it is not let through.
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(
                    part.value, str
                ):
                    if not any(
                        part.value and part.value in key for key in MESSAGES
                    ):
                        self.visit(part)
                else:
                    self.visit(part)

        def visit_Constant(self, node: ast.Constant) -> None:
            if not isinstance(node.value, str) or not node.value:
                return
            if node.value in MESSAGES or f"{node.value}_one" in MESSAGES:
                return
            if node.value in REASON_CODES:
                return
            spelled.add(node.value)

    _Reader().visit(expression)
    return spelled


def test_every_reader_surface_says_whose_words_it_carries():
    """The classification is derived from the code, never restated.

    A surface nobody classified is a surface nobody asked the question
    about: did a model write this text, and if so what keeps an internal
    identifier out of it. Both halves of the answer matter — the guard
    without the list leaves surfaces nobody looked at, and the list
    without the guard is a label.

    The key carries the EXPRESSION, so re-writing what a surface emits
    re-asks the question rather than inheriting the old answer. That
    costs a line of churn when an expression is edited, and it is the
    reason the list cannot go stale the way a list of file names does.
    """
    derived = set(_reader_surfaces())
    classified = set(READER_TEXT_AUTHOR)
    assert derived == classified, (
        "a reader surface has no author; unclassified: "
        f"{sorted(derived - classified)}, classified but no longer a "
        f"surface: {sorted(classified - derived)}"
    )
    assert set(READER_TEXT_AUTHOR.values()) <= {
        MODEL_TEXT_GUARDED,
        RUNTIME_TEXT,
        USER_TEXT,
        NOT_READ,
    }


def test_the_machinery_kinds_are_kinds_this_server_still_writes():
    """The exemption is held against the registry, not against itself."""
    assert set(MACHINERY_ARTIFACT_KINDS) <= set(ARTIFACT_KINDS)
    assert not set(MACHINERY_ARTIFACT_KINDS) & set(NAMED_ARTIFACT_KINDS)


def test_a_surface_nobody_reads_writes_nothing_a_reader_can_open():
    """``NOT_READ`` is checked, not taken on the comment's word.

    It is the weakest claim in the map — "this is stored but nobody sees
    it" — and the one that rots silently, because making an artifact kind
    openable happens somewhere else entirely. It is also the blanket
    exemption from the fixed-language rule below, which is what makes a
    wrong one expensive: a German literal on an English reader's
    transcript, legitimized by one word in a dictionary.

    Two things are checked, because the claim failed both ways. A
    narration line carries no artifact kind at all, so the old check
    (``kinds`` outside the named tuple) was vacuously true for it and a
    literal transcript line could claim this and pass. And the tuple it
    was held against was the wrong one: ``NAMED_ARTIFACT_KINDS`` is about
    canvas documents with derived file names, while ``answer`` and
    ``critic_report`` are read without being in it.
    """
    surfaces = _reader_surfaces()
    wrong = {
        key: sorted(surfaces[key]["kinds"]) or "writes no artifact kind"
        for key, author in READER_TEXT_AUTHOR.items()
        if author == NOT_READ
        and not (
            surfaces[key]["kinds"]
            and surfaces[key]["kinds"] <= set(MACHINERY_ARTIFACT_KINDS)
        )
    }
    assert not wrong, (
        "a surface claims nobody reads it, but it is not a write of a "
        f"machinery artifact kind: {wrong}"
    )


def test_no_reader_surface_writes_text_in_one_fixed_language():
    """A literal cannot follow the answer language, so it may not be read.

    The language policy has one source of truth for user-visible text
    (:mod:`inqtrix.i18n`), and a string spelled into a call site cannot
    obey it: whoever wrote it picked a language for every reader of every
    run. A fixed German answer title reached exactly this way, past the
    prose gate, which measures twelve characters and a space.

    Empty markdown is exempt and is the only exemption: it carries no
    language, and it is how a row is staged before its text exists.
    """
    offenders = {
        key: sorted(spelled)
        for key, entry in _reader_surfaces().items()
        if READER_TEXT_AUTHOR.get(key) != NOT_READ
        and (spelled := _spelled_text(entry["expression"]))
    }
    assert not offenders, (
        "a reader surface writes a fixed-language literal; it belongs in "
        f"inqtrix.i18n: {offenders}"
    )


def test_the_narration_choke_point_is_the_only_narration_emitter():
    """Deriving from ``_emit_narration`` is only worth something if it is one.

    The enumeration reads narration surfaces off the calls to that helper.
    A second place emitting ``NARRATION_EVENT`` would be a reader surface
    the list cannot see, and the guard inside the helper would not run for
    it either — one hole, counted twice.

    The event is looked for in all three spellings it can have, because
    this is the one assertion in this file that pattern-matches instead
    of deriving, and matching one spelling is matching none: a second
    emitter written ``narration.NARRATION_EVENT`` or with the event's
    string spelled out — the idiom this codebase uses elsewhere — passed
    the earlier form of this test untouched.
    """
    emitters: list[str] = []
    for path in sorted(RUNTIME.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            names = (
                isinstance(first, ast.Name) and first.id == "NARRATION_EVENT"
            ) or (
                isinstance(first, ast.Attribute)
                and first.attr == "NARRATION_EVENT"
            ) or (
                isinstance(first, ast.Constant)
                and first.value == NARRATION_EVENT
            )
            if names:
                emitters.append(f"{path}:{node.lineno}")
    assert len(emitters) == 1, (
        f"NARRATION_EVENT is emitted from more than one place: {emitters}"
    )


def test_a_port_is_called_by_its_name_and_with_named_arguments():
    """The derivation reads calls; two shapes would hide one from it.

    A spread (``upsert_artifact(**row)``) has no argument names to read,
    so the call produces no surface at all — a new canvas document,
    invisible to the list that exists to find it. An alias
    (``write = deps.control.upsert_artifact``) moves the port's name out
    of the call, and the walk looks for names.

    Both were probed against the earlier derivation and both were green,
    which is why they are refused here rather than described as limits.
    Refusing costs a caller nothing: these ports take named arguments,
    and none of them is called in a loop over rows.
    """
    spreads: list[str] = []
    aliases: list[str] = []
    for path in sorted(RUNTIME.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) in (
                READER_TEXT_ARGUMENTS
            ):
                if any(keyword.arg is None for keyword in node.keywords):
                    spreads.append(f"{path.name}:{node.lineno}")
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                bound = (
                    value.attr
                    if isinstance(value, ast.Attribute)
                    else value.id
                    if isinstance(value, ast.Name)
                    else ""
                )
                if bound in READER_TEXT_ARGUMENTS:
                    aliases.append(f"{path.name}:{node.lineno}")
    assert not spreads, (
        "a reader-surface port is called with a mapping spread, so its "
        f"arguments have no names the enumeration can read: {spreads}"
    )
    assert not aliases, (
        "a reader-surface port is bound to another name, so its call "
        f"sites no longer carry it: {aliases}"
    )


@pytest.mark.parametrize("argument", ["title", "content_markdown"])
def test_the_artifact_port_declares_the_arguments_the_list_reads(argument):
    """The reader-text arguments are the port's arguments, not a guess."""
    import inspect

    from inqtrix.agents.control_ports import AgentControlStore

    signature = inspect.signature(AgentControlStore.upsert_artifact)
    assert argument in signature.parameters
    assert argument in READER_TEXT_ARGUMENTS["upsert_artifact"]


NARRATION_GUARDS = frozenset({
    # The kernel's own projection, built from the same inputs its canvas
    # write uses.
    "deps.guard_reader_text",
    # The kernel's again, from a middleware that holds its deps on self.
    "self.deps.guard_reader_text",
    # The mission's, which needs the phase state the line is emitted from.
    "lambda line: _guard_reader_text(state, line, evidence=evidence)",
})
"""The guards a narration line may be emitted with.

The emitter requires a ``guard`` argument, which stops a caller from
forgetting one - but not from passing something that guards nothing. An
identity function satisfies the signature and the type, so a check of the
signature alone passes with every kernel guard replaced by
``lambda t: t``. A required argument is a shape; this is the content.
"""


def test_every_narration_line_is_emitted_with_a_real_guard():
    """A guard argument nobody checks is a shape, not a guarantee.

    Naming the permitted expressions here rather than deriving them is
    deliberate: what makes an expression a guard cannot be read off its
    syntax, only off what it does. The list is short, it changes when an
    engine gains a transcript, and a new engine that brings its own guard
    is exactly the moment to look at it once.
    """
    import ast

    passed: dict[str, list[str]] = {}
    for path in sorted(RUNTIME.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            # THE shared emitter only. A wrapper (the mission has one)
            # reaches the reader through this call like everyone else, so
            # checking here covers every path without knowing the wrappers.
            if name != "emit_narration":
                continue
            guards = [kw for kw in node.keywords if kw.arg == "guard"]
            assert guards, f"{path.name}:{node.lineno} emits a narration line without a guard"
            passed.setdefault(ast.unparse(guards[0].value), []).append(
                f"{path.name}:{node.lineno}"
            )
    assert passed, "no narration emission found at all"
    unknown = set(passed) - NARRATION_GUARDS
    assert not unknown, (
        "a narration line is emitted with something that is not a known guard: "
        + "; ".join(f"{expr} at {', '.join(passed[expr])}" for expr in sorted(unknown))
    )


RECORD_PORTS = ("save_plan", "_transition_task", "transition_plan_task",
                "build_clarification")
"""The ports that write a DURABLE record a person is later shown.

Separated from the other ports because the guarantee has a different
shape. A narration line carries its guard in the emitter, an answer
carries it in the algorithm that produced it — both have their own test.
A record is written by ordinary code in an ordinary function, and there
the only thing that makes ``MODEL_TEXT_GUARDED`` more than a word in a
dictionary is that the function doing the writing also does the
checking.
"""

READER_TEXT_GUARDS = frozenset({
    "_guarded_plan",
    "_guard_reader_text",
    "guard_reader_text",
    "guarded_reader_text",
    "guarded_questions",
})
"""What counts as checking, by name.

Named rather than derived, for the same reason ``NARRATION_GUARDS`` is:
what makes a call a guard cannot be read off its syntax. The list is
short and every entry is a binding of the one mechanism in
``report_quality`` — the mission's, the kernel's, the reconciling
service's, and the two shape-aware projections over a plan and a
clarification round.
"""


def test_every_record_port_is_guarded_by_the_function_that_writes_it():
    """A classification nobody checks is a label; this checks it.

    ``MODEL_TEXT_GUARDED`` on a record surface claims that something took
    the identifiers out before the row was written. Here the claim is read back
    out of the function that writes the row — the guard has to be in the
    same place as the write, which is also the only place that still has
    the run's state to bind it to.
    """
    positional = _port_parameters()
    unguarded: dict[str, str] = {}
    for path in sorted(RUNTIME.rglob("*.py")):
        module = (
            str(path.relative_to(RUNTIME).with_suffix("")).replace("/", ".")
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            guards = {
                _callee(call)
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            } & READER_TEXT_GUARDS
            for call in _surface_calls(node, node.name in READER_TEXT_ARGUMENTS):
                if _callee(call) not in RECORD_PORTS:
                    continue
                for name, value in _named_arguments(
                    call, positional.get(_callee(call), ())
                ):
                    if name not in READER_TEXT_ARGUMENTS[_callee(call)]:
                        continue
                    key = reader_surface_key(
                        module, _callee(call), name, ast.unparse(value)
                    )
                    if (
                        READER_TEXT_AUTHOR.get(key) == MODEL_TEXT_GUARDED
                        and not guards
                    ):
                        unguarded[key] = f"{path.name}:{node.name}"
    assert not unguarded, (
        "a record surface is classified as guarded, but the function that "
        f"writes it calls no guard: {unguarded}"
    )
