"""Every owner-side guest-link action reaches the security log.

Granting an INTERNAL user access has always been recorded
(``share.granted`` / ``share.revoked``); granting an account-less
outsider was not. Only the guest's USE of a link
(``guest_link.accessed``) was written, so a live instance could show 47
guest accesses and no record of who issued, re-permissioned, rotated or
ended the links they came from -- the higher-risk half of sharing,
invisible.

The gate is written against the ROUTE SOURCE rather than a live call
because the omission is structural: each owner route is a separate
handler, and the next one added is the next one forgotten. It therefore
DERIVES the set of owner mutations from the router instead of listing
them: a hand-kept list cannot catch the route that does not exist yet,
which is the only case this gate is for.
"""

from __future__ import annotations

import ast
import pathlib

_ROUTER = pathlib.Path("src/inqtrix/server/routers/editor_guest_links.py")

#: Route decorators that change state. A GET is a read and is audited,
#: if at all, by the access log.
_MUTATING = {"post", "patch", "put", "delete"}


def _tree() -> ast.Module:
    return ast.parse(_ROUTER.read_text(encoding="utf-8"))


def _acts_for_the_owner(handler: ast.AsyncFunctionDef) -> bool:
    """True when the handler resolves a signed-in principal.

    A guest-side handler authenticates with the link itself and carries
    only the ambient request; it is audited separately as
    ``guest_link.accessed``.
    """
    args = handler.args
    return any(
        argument.arg == "principal"
        for argument in (*args.args, *args.kwonlyargs)
    )


def _mutates(handler: ast.AsyncFunctionDef) -> bool:
    for decorator in handler.decorator_list:
        call = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(call, ast.Attribute) and call.attr in _MUTATING:
            return True
    return False


def _owner_mutations() -> dict[str, ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(_tree())
        if isinstance(node, ast.AsyncFunctionDef)
        and _mutates(node)
        and _acts_for_the_owner(node)
    }


def test_the_probe_can_see() -> None:
    """Anchor: the derivation still reaches the routes it must cover.

    Deliberately a SUPERSET check, not an equality: a new owner mutation
    should turn the gate below red for lacking an audit call, not turn
    this anchor red for existing. An equality here would be the
    hand-kept list again, one file further along.
    """
    found = set(_owner_mutations())
    missing = sorted(
        {
            "create_link",
            "revoke_link",
            "rotate_password",
            "rotate_token",
            "update_link",
        }
        - found
    )
    assert missing == [], (
        "the derivation no longer reaches these handlers, so the gate "
        f"below would pass vacuously for them: {missing}"
    )
    # A guest-side mutation must NOT be swept in: it has no principal.
    handlers = {
        node.name
        for node in ast.walk(_tree())
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert "create_guest_comment" in handlers
    assert "create_guest_comment" not in found


def test_every_owner_mutation_records_an_audit_event() -> None:
    silent = [
        name
        for name, handler in sorted(_owner_mutations().items())
        if "_record_owner_event" not in ast.dump(handler)
    ]
    assert silent == [], (
        "these guest-link mutations change who outside the instance can "
        "reach a document and leave no trace in the security log: "
        + ", ".join(silent)
    )


def test_the_actions_are_named_like_their_internal_counterparts() -> None:
    """One vocabulary, so an auditor can filter both halves together."""
    source = _ROUTER.read_text(encoding="utf-8")
    for action in (
        "guest_link.created",
        "guest_link.permission_updated",
        "guest_link.revoked",
        "guest_link.password_rotated",
        "guest_link.token_rotated",
    ):
        assert f'action="{action}"' in source, action
    # The pre-existing guest-side event keeps its name.
    assert 'action="guest_link.accessed"' in source


def test_the_event_carries_the_actor_and_the_document() -> None:
    """An audit row without its actor reads as anonymous in the panel."""
    source = _ROUTER.read_text(encoding="utf-8")
    helper = source[source.index("async def _record_owner_event"):]
    helper = helper[: helper.index("\n    @router")]
    assert "actor_user_id=principal.user_id" in helper
    assert 'resource_type="guest_link"' in helper
    assert '"document_id": str(document_id)' in helper
