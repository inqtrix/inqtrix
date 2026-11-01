"""A write that names a parent the caller cannot see ends in that parent's 404.

The store checks a document's folder, an asset's section or group and a
thread's group inside the write, with one answer for an id that does not exist
and an id of another person. The route must turn that answer into the same
404, byte for byte, and nothing may be stored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_project_namespace_writes import (
    U,
    U2,
    World,
    asset_body,
    build_world,
    document_body,
    ids,
    listed,
    send,
    stamped,
    token,
)

UNKNOWN = "parent_that_does_not_exist"


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    built = build_world(tmp_path, monkeypatch)
    with built.client:
        yield built


def _create_parents(client: TestClient) -> None:
    """The same parents for the other person and for the caller."""
    for headers, suffix in ((token(U2), "other"), (token(U), "own")):
        send(client, "PUT", f"/v1/editor/folders/fold_{suffix}", expect=200,
             json=stamped(1.0, title="Folder"), headers=headers)
        send(client, "PUT", f"/v1/assets/sections/fsec_{suffix}", expect=200,
             json=stamped(1.0, kind="custom", title="Section"), headers=headers)
        send(client, "PUT", f"/v1/assets/groups/fgrp_{suffix}", expect=200,
             json=stamped(1.0, section_id=f"fsec_{suffix}", title="Group"), headers=headers)
        send(client, "PUT", f"/v1/chat/thread-groups/tg_{suffix}", expect=200,
             json=stamped(1.0, title="Group"), headers=headers)


def _thread_body(group_id: str) -> dict[str, Any]:
    return stamped(1.0, title="T", preview="", source="api", model_selection="",
                   group_id=group_id)


CASES: list[tuple[str, Callable[[str], dict[str, Any]], str, str, str]] = [
    (
        "/v1/editor/documents/ed_new",
        lambda parent: document_body(1.0, folder_id=parent),
        "fold",
        "Folder not found",
        "/v1/editor/documents?scope=all&limit=200",
    ),
    (
        "/v1/assets/groups/fgrp_new",
        lambda parent: stamped(1.0, section_id=parent, title="G"),
        "fsec",
        "Section not found",
        "/v1/assets/groups",
    ),
    (
        "/v1/assets/fa_new",
        asset_body,
        "fsec",
        "Section not found",
        "/v1/assets",
    ),
    (
        "/v1/assets/fa_new",
        lambda parent: {**asset_body("fsec_own"), "group_id": parent},
        "fgrp",
        "Group not found",
        "/v1/assets",
    ),
    (
        "/v1/chat/threads/th_new",
        _thread_body,
        "tg",
        "Group not found",
        "/v1/chat/threads",
    ),
]


@pytest.mark.parametrize(
    ("url", "body", "prefix", "message", "listing"),
    CASES,
    ids=["document_folder", "group_section", "asset_section", "asset_group", "thread_group"],
)
def test_a_write_naming_an_unknown_or_foreign_parent_answers_404(
    world: World,
    url: str,
    body: Callable[[str], dict[str, Any]],
    prefix: str,
    message: str,
    listing: str,
) -> None:
    _create_parents(world.client)
    before = ids(listed(world.client, listing, token(U)))
    # Without raised server errors, so a parent the route does not map reads
    # as the 500 a client sees instead of as an exception in this test.
    client = TestClient(world.app, raise_server_exceptions=False)

    unknown = client.put(url, json=body(UNKNOWN), headers=token(U))
    foreign = client.put(url, json=body(f"{prefix}_other"), headers=token(U))

    assert unknown.status_code == 404
    assert unknown.json() == {"error": {"message": message, "type": "not_found"}}
    assert foreign.status_code == 404
    assert foreign.content == unknown.content
    assert ids(listed(world.client, listing, token(U))) == before

    # The caller's own parent still takes the same write, and the listing shows it.
    send(world.client, "PUT", url, expect=200, json=body(f"{prefix}_own"), headers=token(U))
    assert url.rsplit("/", 1)[-1] in ids(listed(world.client, listing, token(U)))
