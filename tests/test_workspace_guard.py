"""Edge-case matrix for the UI-namespace delete guard (deny_cross_workspace).

The guard backs the cross-project data-loss defense-in-depth: it denies a
delete whose request workspace differs from the resource's, but stays permissive
at the None edges so legacy/anonymous flows and workspace-less resources are
never blocked. These are the exact branch decisions; each would flip if someone
loosened or over-tightened the condition.
"""

import pytest

from inqtrix.services.workspace_guard import deny_cross_workspace


class _NotFound(Exception):
    pass


def _guard(resource: str | None, req_ws: str | None) -> None:
    deny_cross_workspace(
        resource_workspace_id=resource,
        request_workspace_id=req_ws,
        not_found=_NotFound,
    )


def test_denies_only_an_unambiguous_mismatch() -> None:
    with pytest.raises(_NotFound):
        _guard(resource="ws_a", req_ws="ws_b")


def test_allows_a_matching_workspace() -> None:
    _guard(resource="ws_a", req_ws="ws_a")  # no raise


@pytest.mark.parametrize(
    ("resource", "req_ws"),
    [
        (None, "ws_b"),   # workspace-less resource: not blocked by a namespaced request
        ("ws_a", None),   # request carried no workspace (legacy/anonymous client)
        (None, None),     # neither present
    ],
)
def test_is_permissive_at_the_none_edges(resource, req_ws) -> None:
    _guard(resource=resource, req_ws=req_ws)  # no raise
