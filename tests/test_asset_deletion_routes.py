from __future__ import annotations

import time

from tests.contract._app import make_contract_client


def _asset_payload(section_id: str) -> dict:
    return {
        "section_id": section_id,
        "group_id": None,
        "title": "Policy",
        "label": "policy",
        "file_name": "policy.txt",
        "mime_type": "text/plain",
        "origin": "library",
        "page_count": None,
        "parse_status": "parsed",
        "parse_warning": None,
        "text_truncated": False,
        "size_bytes": 6,
        "server_file_id": None,
        "parser_id": "client",
        "extracted_text": "policy",
        "created_at": 1.0,
        "updated_at": 1.0,
    }


def _wait_for_operation(client, operation_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 2
    latest = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"/v1/assets/deletion-operations/{operation_id}"
        )
        assert response.status_code == 200
        latest = response.json()
        if latest["status"] == expected:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"deletion did not reach {expected}: {latest}")


def test_asset_delete_is_a_truthful_async_operation() -> None:
    with make_contract_client() as client:
        section = client.put(
            "/v1/assets/sections/sec_1",
            json={
                "kind": "custom",
                "title": "Files",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )
        assert section.status_code == 200
        asset = client.put(
            "/v1/assets/fa_1", json=_asset_payload("sec_1")
        )
        assert asset.status_code == 200

        started = client.delete("/v1/assets/fa_1")
        assert started.status_code == 202
        operation = started.json()
        assert operation["operation_id"].startswith("del_")
        assert operation["status"] in {"queued", "running", "deleted"}

        completed = _wait_for_operation(
            client, operation["operation_id"], "deleted"
        )
        assert completed["stage"] == "deleted"
        feed = client.get("/v1/assets/deletion-operations?limit=10")
        assert feed.status_code == 200
        assert any(
            item["operation_id"] == operation["operation_id"]
            for item in feed.json()["data"]
        )
        assert client.get("/v1/assets/fa_1").status_code == 404


def test_section_delete_waits_for_child_asset_cleanup() -> None:
    with make_contract_client() as client:
        client.put(
            "/v1/assets/sections/sec_1",
            json={
                "kind": "custom",
                "title": "Files",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )
        client.put("/v1/assets/fa_1", json=_asset_payload("sec_1"))
        client.put("/v1/assets/fa_2", json=_asset_payload("sec_1"))

        started = client.delete("/v1/assets/sections/sec_1")
        assert started.status_code == 202
        completed = _wait_for_operation(
            client, started.json()["operation_id"], "deleted"
        )
        assert completed["completed_items"] == 2
        assert client.get("/v1/assets/fa_1").status_code == 404
        assert client.get("/v1/assets/fa_2").status_code == 404
        sections = client.get("/v1/assets/sections").json()["data"]
        assert not any(section["id"] == "sec_1" for section in sections)


def test_group_delete_is_durable_and_keeps_child_asset() -> None:
    with make_contract_client() as client:
        client.put(
            "/v1/assets/sections/sec_1",
            json={
                "kind": "custom",
                "title": "Files",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )
        group_payload = {
            "section_id": "sec_1",
            "title": "Dossier",
            "created_at": 1.0,
            "updated_at": 1.0,
        }
        assert client.put(
            "/v1/assets/groups/fg_1",
            json=group_payload,
        ).status_code == 200
        asset_payload = _asset_payload("sec_1")
        asset_payload["group_id"] = "fg_1"
        assert client.put("/v1/assets/fa_1", json=asset_payload).status_code == 200

        started = client.delete("/v1/assets/groups/fg_1")
        assert started.status_code == 202
        operation = started.json()
        assert operation["asset_ids"] == []
        assert operation["target_kind"] == "group"
        completed = _wait_for_operation(
            client, operation["operation_id"], "deleted"
        )
        assert completed["stage"] == "deleted"

        groups = client.get("/v1/assets/groups").json()["data"]
        assert not any(group["id"] == "fg_1" for group in groups)
        child = client.get("/v1/assets/fa_1")
        assert child.status_code == 200
        assert child.json()["group_id"] is None
        assert client.put(
            "/v1/assets/groups/fg_1",
            json=group_payload,
        ).status_code == 409
