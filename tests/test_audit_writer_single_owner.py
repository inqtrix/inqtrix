"""Every audit row is written through ONE envelope-deriving entry point.

An audit row carries more than its action: the actor pseudonym the admin
panel filters and displays by, the request origin, the correlation keys
that join it to logs and traces, and the outcome.  A hand-rolled
``insert(audit_log)`` compiles and stores a row that looks complete in
the database and reads as *anonymous* in the panel, because the read
model shows the actor ONLY as ``actor_pseudonym``.

A storage module that writes its rows directly turns a whole family of
events -- the user lifecycle (login admitted, account created, account
disabled and enabled, password reset) or the workspace family -- into
rows attributed to "anonym" in the security log while the row itself
carries the actor id.

``append_audit_row`` (in-transaction) and the sink's ``record`` (which
takes an already-built ``AuditEntry``) are the two legitimate writers;
both derive the envelope.  This gate keeps it that way.
"""

from __future__ import annotations

import ast
import pathlib

# The two modules that ARE the writers: one owns the in-transaction
# insert, the other owns the sink insert that takes a complete entry.
_WRITERS = {
    "src/inqtrix/storage/resource_access.py",
    "src/inqtrix/storage/identity_postgres.py",
}
_SINK_FUNCTION = "record"


def _audit_inserts(path: pathlib.Path) -> list[tuple[int, str]]:
    """Lines in *path* that build an INSERT against ``audit_log``.

    Matches both spellings the codebase uses: ``insert(audit_log)`` and
    ``audit_log.insert()``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    enclosing: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(getattr(child, "lineno", 0), node.name)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = (
            isinstance(func, ast.Name)
            and func.id == "insert"
            and any(
                isinstance(a, ast.Name) and a.id == "audit_log"
                for a in node.args
            )
        ) or (
            isinstance(func, ast.Attribute)
            and func.attr == "insert"
            and isinstance(func.value, ast.Name)
            and func.value.id == "audit_log"
        )
        if hit:
            found.append((node.lineno, enclosing.get(node.lineno, "<module>")))
    return found


def test_only_the_two_owners_insert_audit_rows() -> None:
    root = pathlib.Path("src/inqtrix")
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.as_posix()
        inserts = _audit_inserts(path)
        if not inserts:
            continue
        if rel not in _WRITERS:
            offenders.extend(f"{rel}:{line} in {fn}()" for line, fn in inserts)
            continue
        if rel.endswith("identity_postgres.py"):
            # Only the sink may insert here; anything else is a bypass.
            offenders.extend(
                f"{rel}:{line} in {fn}()"
                for line, fn in inserts
                if fn != _SINK_FUNCTION
            )
    assert offenders == [], (
        "audit rows must be written through append_audit_row() or the sink's "
        "record(AuditEntry); these inserts bypass the envelope and land in "
        "the admin panel without an actor: " + ", ".join(offenders)
    )


def test_the_user_lifecycle_store_routes_through_the_owner() -> None:
    """The family that actually broke keeps its regression pinned by name."""
    source = pathlib.Path(
        "src/inqtrix/storage/user_lifecycle_postgres.py"
    ).read_text(encoding="utf-8")
    for action in (
        "user.login_admitted",
        "user.created",
        "user.role_updated",
        "user.disabled",
        "user.password_reset",
        "invitation.accepted",
    ):
        assert action in source, f"{action} is no longer written here"
    assert "append_audit_row" in source
    assert "audit_log.insert()" not in source
