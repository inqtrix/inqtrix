"""SCIM 2.0 Users over HTTP: token, discovery, create, read, list, update, delete."""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.gateway import GatewayRejections
from inqtrix.auth.lifecycle import (
    MemoryUserLifecycleTransaction,
    UserLifecycleService,
)
from inqtrix.auth.scim import ScimUsers
from inqtrix.auth.sessions import MemorySessionStore
from inqtrix.server.routers.scim import build_router, build_scim_router

TOKEN = "scim-token-" + "x" * 32
ISSUER = "gateway:corp-sso"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/v1/scim/v2"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_JSON = "application/scim+json"


def _world(*, base_url: str | None = None) -> SimpleNamespace:
    users = MemoryUserDirectory()
    sessions = MemorySessionStore()
    transaction = MemoryUserLifecycleTransaction(
        users=users, sessions=sessions, source_issuer=ISSUER
    )
    lifecycle = UserLifecycleService(
        users=users, sessions=sessions, transaction=transaction
    )
    scim = ScimUsers(
        lifecycle=lifecycle,
        users=users,
        source_issuer=ISSUER,
        deletions=None,
        delete_action="disable",
    )
    app = FastAPI()
    app.include_router(
        build_scim_router(
            scim=scim, token=TOKEN, rejections=GatewayRejections(), base_url=base_url
        )
    )
    return SimpleNamespace(
        users=users,
        transaction=transaction,
        client=TestClient(app, base_url="https://inqtrix.example"),
    )


def _alice(**overrides) -> dict:
    body = {
        "schemas": [USER_SCHEMA],
        "externalId": "oid-alice",
        "userName": "alice@corp.example",
        "name": {"givenName": "Alice", "familyName": "Smith"},
        "displayName": "Alice Smith",
        "emails": [{"value": "alice@corp.example", "primary": True, "type": "work"}],
        "active": True,
    }
    body.update(overrides)
    return body


def _post(world, body, **headers):
    return world.client.post(
        f"{BASE}/Users",
        json=body,
        headers={**AUTH, "Content-Type": SCIM_JSON, **headers},
    )


def _created(world, **overrides) -> str:
    response = _post(world, _alice(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _error(response) -> tuple[str, str | None]:
    body = response.json()
    assert body["schemas"] == [ERROR_SCHEMA]
    assert response.headers["content-type"].startswith(SCIM_JSON)
    return body["status"], body.get("scimType")


ROUTES = [
    ("GET", "/ServiceProviderConfig"),
    ("GET", "/ResourceTypes"),
    ("GET", "/Schemas"),
    ("GET", f"/Schemas/{USER_SCHEMA}"),
    ("GET", "/ResourceTypes/User"),
    ("GET", "/Users"),
    ("POST", "/Users"),
    ("GET", f"/Users/{uuid.uuid4()}"),
    ("PUT", f"/Users/{uuid.uuid4()}"),
    ("PATCH", f"/Users/{uuid.uuid4()}"),
    ("DELETE", f"/Users/{uuid.uuid4()}"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer ipat_x_y"},
    ],
    ids=["missing", "wrong", "not-bearer", "pat"],
)
def test_every_scim_route_needs_the_scim_token(method, path, headers):
    world = _world()

    response = world.client.request(method, f"{BASE}{path}", headers=headers)

    assert response.status_code == 401
    assert _error(response) == ("401", None)
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_refused_token_is_counted_with_its_outcome(caplog):
    world = _world()

    with caplog.at_level(logging.WARNING):
        world.client.get(f"{BASE}/Users", headers={"Authorization": "Bearer wrong"})

    assert "outcome=scim_unauthorized" in caplog.text
    assert "wrong" not in caplog.text


def test_the_service_provider_config_names_what_is_supported():
    world = _world()

    response = world.client.get(f"{BASE}/ServiceProviderConfig", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(SCIM_JSON)
    config = response.json()
    assert config["patch"] == {"supported": True}
    assert config["filter"] == {"supported": True, "maxResults": 200}
    for unsupported in ("bulk", "sort", "etag", "changePassword"):
        assert config[unsupported]["supported"] is False
    assert [scheme["type"] for scheme in config["authenticationSchemes"]] == [
        "oauthbearertoken"
    ]


def test_resource_types_and_schemas_offer_users_only():
    world = _world()

    types = world.client.get(f"{BASE}/ResourceTypes", headers=AUTH).json()
    schemas = world.client.get(f"{BASE}/Schemas", headers=AUTH).json()

    assert [resource["id"] for resource in types["Resources"]] == ["User"]
    assert types["Resources"][0]["schema"] == USER_SCHEMA
    assert [schema["id"] for schema in schemas["Resources"]] == [USER_SCHEMA]
    names = {attribute["name"] for attribute in schemas["Resources"][0]["attributes"]}
    assert names == {"userName", "name", "displayName", "emails", "active", "externalId"}


def test_post_creates_an_account_with_its_location():
    world = _world()

    response = _post(world, _alice())

    assert response.status_code == 201
    user = response.json()
    assert response.headers["location"] == (
        f"https://inqtrix.example{BASE}/Users/{user['id']}"
    )
    assert (user["externalId"], user["userName"], user["active"]) == (
        "oid-alice",
        "alice@corp.example",
        True,
    )
    assert user["name"] == {"givenName": "Alice", "familyName": "Smith"}
    assert user["emails"] == [
        {"value": "alice@corp.example", "primary": True, "type": "work"}
    ]
    assert user["meta"]["resourceType"] == "User"
    assert user["meta"]["location"] == response.headers["location"]
    assert user["meta"]["created"].endswith("Z")
    stored = asyncio.run(
        world.users.find_by_user_id(tenant_id="default", user_id=uuid.UUID(user["id"]))
    )
    assert stored is not None
    assert (stored.issuer, stored.subject, stored.email_verified) == (
        ISSUER,
        "oid-alice",
        False,
    )


@pytest.mark.parametrize(("active", "expected"), [("False", False), ("true", True)])
def test_post_reads_active_also_as_a_string(active, expected):
    world = _world()

    response = _post(world, _alice(active=active))

    assert response.status_code == 201
    assert response.json()["active"] is expected


@pytest.mark.parametrize("missing", ["externalId", "userName"])
def test_post_without_its_keys_is_invalid(missing):
    world = _world()
    body = _alice()
    del body[missing]

    response = _post(world, body)

    assert response.status_code == 400
    assert _error(response) == ("400", "invalidValue")


def test_post_of_a_taken_user_name_is_a_conflict():
    world = _world()
    _created(world)

    response = _post(world, _alice(externalId="oid-other", userName="ALICE@corp.example"))

    assert response.status_code == 409
    assert _error(response) == ("409", "uniqueness")


def test_post_adopts_an_account_of_the_source_it_never_bound():
    world = _world()
    signed_in = asyncio.run(
        world.users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject="oid-alice",
            email="alice@corp.example",
            email_verified=True,
            display_name="Alice",
        )
    )

    response = _post(world, _alice())

    assert response.status_code == 201
    assert response.json()["id"] == str(signed_in.user_id)


def test_get_answers_an_account_of_the_source_only():
    world = _world()
    alice = _created(world)
    local = asyncio.run(
        world.users.record_login(
            tenant_id="default",
            issuer="local",
            subject="carol",
            email="carol@corp.example",
            email_verified=True,
            display_name="Carol",
        )
    )
    system = asyncio.run(
        world.users.record_login(
            tenant_id="default",
            issuer="inqtrix:system",
            subject="system",
            email="",
            email_verified=False,
            display_name=None,
        )
    )
    tombstone = _created(world, externalId="oid-tom", userName="tom")
    world.users.update_user(
        tenant_id="default",
        user_id=uuid.UUID(tombstone),
        scim_deleted_at=time.time(),
        disabled_at=time.time(),
    )
    erasing = _created(world, externalId="oid-eve", userName="eve")
    world.users.update_user(
        tenant_id="default", user_id=uuid.UUID(erasing), erasure_operation_id="op-erase"
    )

    def status(user_id) -> int:
        return world.client.get(f"{BASE}/Users/{user_id}", headers=AUTH).status_code

    assert status(alice) == 200
    assert [
        status(user_id)
        for user_id in (
            local.user_id,
            system.user_id,
            tombstone,
            erasing,
            uuid.uuid4(),
            "not-a-uuid",
        )
    ] == [404] * 6


def test_an_unbound_account_of_the_source_shows_its_subject_as_user_name():
    world = _world()
    signed_in = asyncio.run(
        world.users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject="oid-bob",
            email="bob@corp.example",
            email_verified=True,
            display_name="Bob",
        )
    )

    user = world.client.get(f"{BASE}/Users/{signed_in.user_id}", headers=AUTH).json()

    assert (user["userName"], user["externalId"]) == ("oid-bob", "oid-bob")


def test_the_list_filters_strictly_and_pages():
    world = _world()
    alice = _created(world)
    bob = _created(world, externalId="oid-bob", userName="bob@corp.example")

    def listing(**params):
        return world.client.get(f"{BASE}/Users", params=params, headers=AUTH)

    by_name = listing(filter='userName eq "ALICE@corp.example"').json()
    by_subject = listing(filter='externalId eq "oid-bob"').json()
    unknown_case = listing(filter='externalId eq "OID-BOB"').json()
    any_case_operator = listing(filter='USERNAME EQ "bob@corp.example"').json()
    second = listing(startIndex=2, count=1).json()
    from_zero = listing(startIndex=0).json()

    assert by_name["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert (by_name["totalResults"], [r["id"] for r in by_name["Resources"]]) == (1, [alice])
    assert [r["id"] for r in by_subject["Resources"]] == [bob]
    assert unknown_case["totalResults"] == 0
    assert [r["id"] for r in any_case_operator["Resources"]] == [bob]
    assert from_zero["startIndex"] == 1
    assert (second["totalResults"], second["startIndex"], second["itemsPerPage"]) == (2, 2, 1)
    assert [r["id"] for r in second["Resources"]] == [bob]
    for unsupported in (
        'userName sw "a"',
        'displayName eq "Alice"',
        "userName eq alice",
        'userName eq "a" and externalId eq "b"',
    ):
        response = listing(filter=unsupported)
        assert response.status_code == 400, unsupported
        assert _error(response) == ("400", "invalidFilter")


@pytest.mark.parametrize(
    "escaped",
    ["a\\u0000b", "a\\ud800b"],
    ids=["control-character", "lone-surrogate"],
)
def test_a_filter_value_holds_to_the_bounds_of_the_attribute(escaped):
    # The value is decoded from JSON escapes; what no attribute may hold is
    # no valid filter either.
    world = _world()

    response = world.client.get(
        f"{BASE}/Users", params={"filter": f'userName eq "{escaped}"'}, headers=AUTH
    )

    assert response.status_code == 400
    assert _error(response) == ("400", "invalidFilter")


def test_a_lone_surrogate_in_a_body_is_an_invalid_value():
    import json

    world = _world()
    body = json.dumps(_alice(displayName="Alice \ud800")).encode("ascii")

    response = world.client.post(
        f"{BASE}/Users", content=body, headers={**AUTH, "Content-Type": SCIM_JSON}
    )

    assert response.status_code == 400
    assert _error(response) == ("400", "invalidValue")


def test_the_external_id_takes_the_bound_of_the_gateway_subject():
    # SCIM binds the subject the API gateway signs in with: one bound for both.
    from inqtrix.auth.gateway import SUBJECT_MAX_LENGTH

    world = _world()

    fits = _post(world, _alice(externalId="o" * SUBJECT_MAX_LENGTH))
    beyond = _post(world, _alice(externalId="p" * (SUBJECT_MAX_LENGTH + 1), userName="bob"))

    assert fits.status_code == 201, fits.text
    assert beyond.status_code == 400
    assert _error(beyond) == ("400", "invalidValue")


def test_delete_leaves_a_tombstone_that_scim_no_longer_finds():
    world = _world()
    alice = _created(world)

    first = world.client.delete(f"{BASE}/Users/{alice}", headers=AUTH)
    again = world.client.delete(f"{BASE}/Users/{alice}", headers=AUTH)
    read = world.client.get(f"{BASE}/Users/{alice}", headers=AUTH)

    assert first.status_code == 204
    assert (again.status_code, read.status_code) == (404, 404)
    stored = asyncio.run(
        world.users.find_by_user_id(tenant_id="default", user_id=uuid.UUID(alice))
    )
    assert stored is not None and stored.scim_deleted_at is not None


def test_a_new_person_can_take_the_user_name_of_a_deleted_one():
    world = _world()
    alice = _created(world)
    assert world.client.delete(f"{BASE}/Users/{alice}", headers=AUTH).status_code == 204

    successor = _post(world, _alice(externalId="oid-successor"))
    # A free user name, so that only the deleted identity can refuse it.
    returning = _post(world, _alice(userName="alice.returns@corp.example"))

    assert successor.status_code == 201
    assert successor.json()["userName"] == "alice@corp.example"
    assert returning.status_code == 409
    assert _error(returning) == ("409", "uniqueness")


def test_a_body_beyond_64_kib_is_refused():
    world = _world()

    response = _post(world, _alice(displayName="x" * (64 * 1024)))

    assert response.status_code == 413
    assert _error(response) == ("413", None)


def test_a_body_nested_beyond_the_parser_is_a_syntax_error():
    # Deeper than the JSON parser recurses, yet within 64 KiB.
    world = _world()

    response = world.client.post(
        f"{BASE}/Users",
        content=b"[" * 20_000,
        headers={**AUTH, "Content-Type": SCIM_JSON},
    )

    assert response.status_code == 400
    assert _error(response) == ("400", "invalidSyntax")


def test_a_body_without_a_declared_length_is_bounded_too():
    world = _world()

    def chunks():
        yield b'{"externalId": "oid-alice", "userName": "alice", "displayName": "'
        for _ in range(70):
            yield b"x" * 1024
        yield b'"}'

    response = world.client.post(
        f"{BASE}/Users",
        content=chunks(),
        headers={**AUTH, "Content-Type": SCIM_JSON},
    )

    assert response.status_code == 413


def test_a_page_holds_at_most_200_users():
    world = _world()
    for number in range(201):
        asyncio.run(
            world.users.record_login(
                tenant_id="default",
                issuer=ISSUER,
                subject=f"oid-{number}",
                email=f"user-{number}@corp.example",
                email_verified=True,
                display_name=None,
            )
        )

    page = world.client.get(f"{BASE}/Users", params={"count": 500}, headers=AUTH).json()

    assert (page["totalResults"], page["itemsPerPage"]) == (201, 200)


def test_post_keeps_the_primary_address_and_bounded_name_parts():
    world = _world()
    emails = [
        {"value": "home@private.example", "type": "home"},
        {"value": "alice@corp.example", "type": "work", "primary": True},
    ]

    created = _post(world, _alice(emails=emails))
    too_long = _post(
        world,
        _alice(externalId="oid-long", userName="long", name={"givenName": "x" * 257}),
    )

    assert created.status_code == 201
    assert created.json()["emails"][0]["value"] == "alice@corp.example"
    assert too_long.status_code == 400
    assert _error(too_long) == ("400", "invalidValue")


def test_a_body_of_another_media_type_is_refused():
    world = _world()

    response = world.client.post(
        f"{BASE}/Users",
        content=b"externalId=oid-alice",
        headers={**AUTH, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415
    assert _error(response) == ("415", None)


def test_no_scim_value_reaches_the_log(caplog):
    world = _world()
    marker = "zz-scim-7f3a"

    with caplog.at_level(logging.DEBUG):
        _post(
            world,
            _alice(
                externalId=f"{marker}-sub",
                userName=f"{marker}@corp.example",
                displayName=f"{marker} Name",
                name={"givenName": f"{marker}-given", "familyName": f"{marker}-family"},
                emails=[{"value": f"{marker}@mail.example", "primary": True}],
            ),
        )
        _post(world, _alice(externalId=f"{marker}-other", userName=f"{marker}@corp.example"))
        world.client.get(
            f"{BASE}/Users", params={"filter": f'userName eq "{marker}"'}, headers=AUTH
        )
        world.client.get(
            f"{BASE}/Users", params={"filter": f'nickName eq "{marker}"'}, headers=AUTH
        )

    # The test client logs its request URLs; Inqtrix's own lines must stay
    # free of SCIM values (its access lines drop the query anyway).
    own = [record.getMessage() for record in caplog.records if record.name.startswith("inqtrix")]
    assert own, "the SCIM requests wrote no line of their own"
    assert not [line for line in own if marker in line]


def test_the_scim_router_exists_only_for_the_gateway_with_scim():
    def container(mode, enabled):
        auth = SimpleNamespace(
            scim_enabled=enabled, scim_token=TOKEN, scim_delete_action="disable"
        )
        server = SimpleNamespace(public_base_url="")
        provider = SimpleNamespace(
            mode=mode,
            lifecycle=None,
            users=MemoryUserDirectory(),
            source=SimpleNamespace(issuer_key=ISSUER),
            rejections=GatewayRejections(),
        )
        return SimpleNamespace(
            settings=SimpleNamespace(auth=auth, server=server),
            auth_provider=provider,
            asset_deletion_service=None,
        )

    mounted = build_router(container("gateway", True))

    assert mounted is not None
    assert {route.path for route in mounted.routes} >= {
        f"{BASE}/Users",
        f"{BASE}/Users/{{user_id}}",
        f"{BASE}/ServiceProviderConfig",
    }
    assert build_router(container("gateway", False)) is None
    assert build_router(container("oidc", True)) is None


def _put(world, user_id, body):
    return world.client.put(
        f"{BASE}/Users/{user_id}",
        json=body,
        headers={**AUTH, "Content-Type": SCIM_JSON},
    )


def _patch_body(world, user_id, body):
    return world.client.patch(
        f"{BASE}/Users/{user_id}",
        json=body,
        headers={**AUTH, "Content-Type": SCIM_JSON},
    )


def _patch(world, user_id, *operations):
    return _patch_body(
        world, user_id, {"schemas": [PATCH_SCHEMA], "Operations": list(operations)}
    )


def _stored(world, user_id):
    return asyncio.run(
        world.users.find_by_user_id(tenant_id="default", user_id=uuid.UUID(str(user_id)))
    )


def test_put_replaces_the_profile():
    world = _world()
    alice = _created(world)

    response = _put(
        world,
        alice,
        {
            "schemas": [USER_SCHEMA],
            "externalId": "oid-alice",
            "userName": "alice.smith@corp.example",
            "displayName": "Alice S.",
            "name": {"givenName": "Alicia"},
            "emails": [{"value": "alicia@corp.example", "primary": True}],
        },
    )

    assert response.status_code == 200, response.text
    user = response.json()
    assert (user["userName"], user["displayName"], user["name"]) == (
        "alice.smith@corp.example",
        "Alice S.",
        {"givenName": "Alicia"},
    )
    assert user["emails"][0]["value"] == "alicia@corp.example"
    assert user["active"] is True


def test_put_clears_what_it_leaves_out_but_keeps_the_state():
    world = _world()
    alice = _created(world, active=False)

    response = _put(world, alice, {"externalId": "oid-alice", "userName": "alice@corp.example"})

    assert response.status_code == 200, response.text
    user = response.json()
    assert not {"displayName", "name", "emails"} & set(user)
    # Without active the request asserts nothing about the state.
    assert user["active"] is False


@pytest.mark.parametrize(
    ("external_id", "expected"),
    [("oid-other", ("400", "mutability")), ("oid-alice", None), (None, None)],
    ids=["other", "same", "absent"],
)
def test_put_keeps_the_external_id(external_id, expected):
    world = _world()
    alice = _created(world)
    body = _alice(externalId=external_id)
    if external_id is None:
        del body["externalId"]

    response = _put(world, alice, body)

    if expected is None:
        assert response.status_code == 200, response.text
    else:
        assert _error(response) == expected
    assert _stored(world, alice).subject == "oid-alice"


def test_put_without_user_name_is_invalid():
    world = _world()
    alice = _created(world)
    body = _alice()
    del body["userName"]

    response = _put(world, alice, body)

    assert _error(response) == ("400", "invalidValue")


def test_patch_reads_operations_as_entra_sends_them():
    world = _world()
    alice = _created(world)

    deactivated = _patch(world, alice, {"op": "Replace", "path": "active", "value": "False"})
    reactivated = _patch(world, alice, {"op": "replace", "path": "active", "value": True})

    assert (deactivated.status_code, deactivated.json()["active"]) == (200, False)
    assert (reactivated.status_code, reactivated.json()["active"]) == (200, True)


def test_patch_changes_parts_and_keeps_the_rest():
    world = _world()
    alice = _created(world)

    response = _patch(
        world,
        alice,
        {"op": "replace", "path": "name.givenName", "value": "Alicia"},
        {"op": "add", "path": "name", "value": {"formatted": "Alicia Smith"}},
        {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "alicia@corp.example"},
        {
            "op": "replace",
            "path": "urn:ietf:params:scim:schemas:core:2.0:User:displayName",
            "value": "Alicia Smith",
        },
    )

    assert response.status_code == 200, response.text
    user = response.json()
    assert user["name"] == {"givenName": "Alicia", "familyName": "Smith", "formatted": "Alicia Smith"}
    assert user["emails"][0]["value"] == "alicia@corp.example"
    assert (user["displayName"], user["userName"]) == ("Alicia Smith", "alice@corp.example")
    assert _stored(world, alice).email_verified is False


def test_patch_without_a_path_takes_each_key_as_a_path():
    world = _world()
    alice = _created(world)

    response = _patch(
        world,
        alice,
        {
            "op": "Replace",
            "value": {"displayName": "A. Smith", "name.familyName": "Smyth", "active": "False"},
        },
    )

    assert response.status_code == 200, response.text
    user = response.json()
    assert (user["displayName"], user["active"]) == ("A. Smith", False)
    assert user["name"] == {"givenName": "Alice", "familyName": "Smyth"}


def test_patch_removes_optional_attributes():
    world = _world()
    alice = _created(world)

    response = _patch(
        world,
        alice,
        {"op": "remove", "path": "displayName"},
        {"op": "remove", "path": "name.givenName"},
        {"op": "remove", "path": "emails"},
    )

    assert response.status_code == 200, response.text
    user = response.json()
    assert not {"displayName", "emails"} & set(user)
    assert user["name"] == {"familyName": "Smith"}


@pytest.mark.parametrize(
    ("operation", "scim_type"),
    [
        ({"op": "remove", "path": "userName"}, "mutability"),
        ({"op": "remove", "path": "externalId"}, "mutability"),
        ({"op": "remove", "path": "active"}, "mutability"),
        ({"op": "remove"}, "noTarget"),
        ({"op": "replace", "path": "externalId", "value": "oid-other"}, "mutability"),
        ({"op": "move", "path": "displayName", "value": "x"}, "invalidSyntax"),
        ({"op": "replace", "path": "active", "value": "yes"}, "invalidValue"),
        ({"op": "replace", "path": "userName", "value": ""}, "invalidValue"),
        ({"op": "replace", "path": "name.givenName", "value": "x" * 257}, "invalidValue"),
        ({"op": "replace", "value": "not-an-object"}, "invalidValue"),
    ],
    ids=[
        "remove-user-name",
        "remove-external-id",
        "remove-active",
        "remove-without-path",
        "other-external-id",
        "unknown-op",
        "active-not-boolean",
        "empty-user-name",
        "long-name-part",
        "pathless-not-object",
    ],
)
def test_patch_refuses_what_it_cannot_apply(operation, scim_type):
    world = _world()
    alice = _created(world)

    response = _patch(world, alice, operation)

    assert _error(response) == ("400", scim_type)
    stored = _stored(world, alice)
    assert (stored.scim_user_name, stored.display_name, stored.disabled_at) == (
        "alice@corp.example",
        "Alice Smith",
        None,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"schemas": [PATCH_SCHEMA]},
        {"schemas": [PATCH_SCHEMA], "Operations": []},
        {"schemas": [PATCH_SCHEMA], "Operations": {"op": "replace"}},
        {"schemas": [PATCH_SCHEMA], "Operations": ["replace"]},
    ],
    ids=["missing", "empty", "object", "not-objects"],
)
def test_patch_needs_a_list_of_operations(body):
    world = _world()
    alice = _created(world)

    response = _patch_body(world, alice, body)

    assert _error(response) == ("400", "invalidSyntax")


def test_patch_accepts_the_same_external_id():
    world = _world()
    alice = _created(world)

    response = _patch(world, alice, {"op": "replace", "path": "externalId", "value": "oid-alice"})

    assert response.status_code == 200, response.text


def test_patch_never_changes_roles_or_attributes_inqtrix_does_not_keep():
    world = _world()
    alice = _created(world)
    before = world.client.get(f"{BASE}/Users/{alice}", headers=AUTH).json()

    response = _patch(
        world,
        alice,
        {"op": "add", "path": "roles", "value": [{"value": "admin"}]},
        {"op": "replace", "path": "title", "value": "Lead"},
        {
            "op": "replace",
            "value": {"urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {"department": "X"}},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == before
    assert _stored(world, alice).instance_role == "user"
    assert "user.scim_updated" not in [entry.action for entry in world.transaction.audit_entries]


def test_active_true_keeps_an_admin_disable():
    world = _world()
    alice = _created(world)
    world.users.update_user(
        tenant_id="default",
        user_id=uuid.UUID(alice),
        disabled_at=time.time(),
        disabled_source="admin",
    )

    response = _patch(world, alice, {"op": "replace", "path": "active", "value": True})

    assert (response.status_code, response.json()["active"]) == (200, False)
    assert "user.scim_enable_ignored" in [entry.action for entry in world.transaction.audit_entries]


def test_updates_of_a_tombstone_or_an_unknown_id_answer_404():
    world = _world()
    alice = _created(world)
    assert world.client.delete(f"{BASE}/Users/{alice}", headers=AUTH).status_code == 204

    statuses = [
        _patch(world, user_id, {"op": "replace", "path": "active", "value": True}).status_code
        for user_id in (alice, uuid.uuid4(), "not-a-uuid")
    ] + [_put(world, user_id, _alice()).status_code for user_id in (alice, uuid.uuid4())]

    assert statuses == [404] * 5
    assert _stored(world, alice).disabled_at is not None


def test_an_update_to_a_taken_user_name_is_a_conflict():
    world = _world()
    _created(world)
    bob = _created(world, externalId="oid-bob", userName="bob@corp.example")

    response = _patch(world, bob, {"op": "replace", "path": "userName", "value": "ALICE@corp.example"})

    assert _error(response) == ("409", "uniqueness")


def test_an_unbound_account_stays_unbound_until_a_user_name_arrives():
    world = _world()
    signed_in = asyncio.run(
        world.users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject="oid-bob",
            email="bob@corp.example",
            email_verified=True,
            display_name="Bob",
        )
    )

    renamed = _patch(world, signed_in.user_id, {"op": "replace", "path": "displayName", "value": "Bob B."})
    unbound = _stored(world, signed_in.user_id).scim_user_name
    bound = _patch(world, signed_in.user_id, {"op": "replace", "path": "userName", "value": "bob@corp.example"})

    assert (renamed.status_code, renamed.json()["userName"]) == (200, "oid-bob")
    assert unbound is None
    assert bound.json()["userName"] == "bob@corp.example"
    assert _stored(world, signed_in.user_id).scim_user_name == "bob@corp.example"


def test_post_drops_empty_name_parts():
    world = _world()

    response = _post(world, _alice(name={"givenName": "", "familyName": "Smith"}))

    assert response.json()["name"] == {"familyName": "Smith"}


def test_no_value_of_an_update_reaches_the_log(caplog):
    world = _world()
    alice = _created(world)
    marker = "zz-scim-9c1d"

    with caplog.at_level(logging.DEBUG):
        _patch(world, alice, {"op": "replace", "path": "displayName", "value": f"{marker} Name"})
        _patch(
            world,
            alice,
            {"op": "replace", "path": 'emails[type eq "work"].value', "value": f"{marker}@mail.example"},
            {"op": "replace", "path": "name.givenName", "value": f"{marker}-given"},
        )
        _patch(world, alice, {"op": "replace", "path": f"{marker}.x", "value": marker})
        _patch(world, alice, {"op": "replace", "path": "externalId", "value": marker})
        _put(world, alice, _alice(externalId=marker, displayName=marker))

    own = [record.getMessage() for record in caplog.records if record.name.startswith("inqtrix")]
    assert own, "the SCIM requests wrote no line of their own"
    assert not [line for line in own if marker in line]


def _mounted(token: str, *, public_base_url: str = "") -> TestClient:
    provider = SimpleNamespace(
        mode="gateway",
        lifecycle=None,
        users=MemoryUserDirectory(),
        source=SimpleNamespace(issuer_key=ISSUER),
        rejections=GatewayRejections(),
    )
    container = SimpleNamespace(
        settings=SimpleNamespace(
            auth=SimpleNamespace(
                scim_enabled=True, scim_token=token, scim_delete_action="disable"
            ),
            server=SimpleNamespace(public_base_url=public_base_url),
        ),
        auth_provider=provider,
        asset_deletion_service=None,
    )
    app = FastAPI()
    app.include_router(build_router(container))
    return TestClient(app, base_url="http://10.0.0.7:5100")


def test_a_token_written_with_surrounding_space_still_opens():
    client = _mounted(f"  {TOKEN}\n")

    response = client.get(f"{BASE}/ServiceProviderConfig", headers=AUTH)

    assert response.status_code == 200


def test_locations_name_the_public_base_url():
    client = _mounted(TOKEN, public_base_url="https://desk.example.org/")

    config = client.get(f"{BASE}/ServiceProviderConfig", headers=AUTH).json()

    assert config["meta"]["location"] == (
        "https://desk.example.org/v1/scim/v2/ServiceProviderConfig"
    )


def test_the_scim_token_is_compared_in_constant_time():
    tree = ast.parse(inspect.getsource(build_scim_router))
    compared = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(name in ast.unparse(node) for name in ("digest", "expected", "token"))
    ]
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "hmac.compare_digest"
    ]

    assert compared == []
    assert calls


def test_a_refused_token_writes_only_the_throttled_warning(caplog):
    world = _world()

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            world.client.get(f"{BASE}/Users", headers={"Authorization": "Bearer wrong"})

    router_lines = [r for r in caplog.records if r.name == "inqtrix.server.routers.scim"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert router_lines == []
    assert len(warnings) == 1


def test_the_filter_reads_escaped_quotes_and_backslashes():
    world = _world()
    tricky = _created(world, externalId="oid-tricky", userName='a"b\\c')

    found = world.client.get(
        f"{BASE}/Users",
        params={"filter": 'userName eq "a\\"b\\\\c"'},
        headers=AUTH,
    ).json()

    assert [user["id"] for user in found["Resources"]] == [tricky]


def test_single_schema_and_resource_type_are_routable():
    world = _world()

    schema = world.client.get(f"{BASE}/Schemas/{USER_SCHEMA}", headers=AUTH)
    resource_type = world.client.get(f"{BASE}/ResourceTypes/User", headers=AUTH)
    unknown = [
        world.client.get(f"{BASE}/Schemas/urn:example:unknown", headers=AUTH),
        world.client.get(f"{BASE}/ResourceTypes/Group", headers=AUTH),
    ]

    assert (schema.status_code, schema.json()["id"]) == (200, USER_SCHEMA)
    assert (resource_type.status_code, resource_type.json()["id"]) == (200, "User")
    assert [_error(response) for response in unknown] == [("404", None)] * 2


@pytest.mark.parametrize(
    "body",
    [
        _alice(userName="alice\u0000@corp.example"),
        _alice(userName="a" * 300),
        _alice(externalId="oid\u0007"),
        _alice(displayName="Alice\u001b[31m"),
        _alice(emails=[{"value": "a" * 330 + "@corp.example", "primary": True}]),
    ],
    ids=["nul-user-name", "long-user-name", "bell-external-id", "escape-display-name", "long-email"],
)
def test_text_attributes_without_bounds_are_invalid(body):
    world = _world()

    response = _post(world, body)

    assert _error(response) == ("400", "invalidValue")


def test_a_patch_cannot_smuggle_control_characters():
    world = _world()
    alice = _created(world)

    response = _patch(world, alice, {"op": "replace", "path": "userName", "value": "al\u0000ice"})

    assert _error(response) == ("400", "invalidValue")


def test_a_huge_start_index_answers_an_empty_page():
    world = _world()
    _created(world)

    response = world.client.get(
        f"{BASE}/Users", params={"startIndex": "99999999999999999999999"}, headers=AUTH
    )

    assert response.status_code == 200
    assert (response.json()["startIndex"], response.json()["Resources"]) == (2**31 - 1, [])
