"""UI-namespace consistency guard for destructive project-entity operations.

This is deliberately NOT an authorization check. Authorization is ownership
plus shares (``auth.permissions.require_owned_access``); the workspace is a
client-supplied UI namespace filter (see ``auth.principal.UserContext``), never
an auth input. This guard mirrors that namespace filter onto the DELETE path so
a delete issued from one project's UI namespace cannot reach a different
project's rows -- defense-in-depth against a client that, after switching to
another (same-owner) project, still references the prior project's ids.

It is intentionally permissive at the edges: a request that carries no workspace
(legacy/anonymous), or a resource with no stored workspace, is a no-op. Only an
unambiguous cross-workspace mismatch (both sides present and different) is denied.

This matches the list endpoints exactly for the both-present case (they filter
``workspace_id`` by equality only when the request supplies one). At the
resource-has-no-workspace edge it is deliberately MORE permissive than list: a
``workspace_id IS NULL`` row is dropped by a workspaced list (strict equality)
yet remains deletable from any request. That asymmetry is the safe direction for
a defense-in-depth guard -- it never blocks a legitimate delete of legacy or
anonymous data -- and such rows only arise outside the normal client flow (which
always sends a stable per-browser workspace id), so it is never reached in
practice. The guard is a super-set of "listable", never a subset.
"""

from __future__ import annotations

from typing import Callable


def deny_cross_workspace(
    *,
    resource_workspace_id: str | None,
    request_workspace_id: str | None,
    not_found: Callable[[], Exception],
) -> None:
    """Raise a not-found if the resource lives in a different workspace.

    Args:
        resource_workspace_id: The workspace the loaded resource belongs to
            (``None`` for resources stored without one).
        request_workspace_id: The UI namespace the request was issued in
            (``None`` when the client sent no workspace header).
        not_found: A factory for the per-entity not-found exception (already
            carrying the resource id), so a blocked delete is indistinguishable
            from a missing resource -- the same response the list/get filter
            would produce.

    Raises:
        Exception: The result of ``not_found()`` when both ids are present and
            differ.
    """
    if (
        request_workspace_id is not None
        and resource_workspace_id is not None
        and resource_workspace_id != request_workspace_id
    ):
        raise not_found()
