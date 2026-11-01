"""SCIM 2.0 ``Users`` over HTTP (RFC 7644), only for the gateway with SCIM.

The identity system authenticates with the static SCIM token of the
deployment; a cookie or a personal access token never reaches these
routes. Bodies are JSON of at most 64 KiB, answers are
``application/scim+json``, and every error uses the SCIM error message.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from inqtrix.auth.lifecycle import ProvisioningOutcome, UserLifecycleStatus
from inqtrix.auth.scim import (
    DEFAULT_COUNT,
    MAX_RESULTS,
    USER_SCHEMA,
    ScimError,
    ScimUsers,
    list_response,
    patch_from_body,
    replacement_from_body,
    service_provider_config,
    user_from_body,
    user_resource,
    user_resource_type,
    user_schema,
)

if TYPE_CHECKING:
    from inqtrix.auth.directory import MirroredUser
    from inqtrix.auth.gateway import GatewayRejections

log = logging.getLogger(__name__)

SCIM_PREFIX = "/v1/scim/v2"
SCIM_MEDIA_TYPE = "application/scim+json"
#: Largest SCIM body; the web gateway otherwise admits file uploads.
MAX_BODY_BYTES = 64 * 1024
#: Largest ``startIndex`` read; beyond it a page is empty anyway, and the
#: database's offset stays in range.
MAX_START_INDEX = 2**31 - 1
_JSON_MEDIA_TYPES = frozenset({SCIM_MEDIA_TYPE, "application/json"})
_LAST_ADMIN = "The last admin of the identity source cannot be deactivated."


def build_router(container: Any) -> APIRouter | None:
    """The SCIM router of *container*, or ``None`` when SCIM is off.

    SCIM exists only in the gateway mode with ``INQTRIX_SCIM_ENABLED``: it
    manages the accounts of the gateway's identity source.
    """
    auth = container.settings.auth
    provider = container.auth_provider
    if provider.mode != "gateway" or not auth.scim_enabled:
        return None
    scim = ScimUsers(
        lifecycle=provider.lifecycle,
        users=provider.users,
        source_issuer=provider.source.issuer_key,
        deletions=container.asset_deletion_service,
        delete_action=auth.scim_delete_action,
    )
    return build_scim_router(
        scim=scim,
        # Stripped like the start check measures it: a secret file's final
        # line break must not lock the identity system out.
        token=auth.scim_token.strip(),
        rejections=provider.rejections,
        base_url=container.settings.server.public_base_url or None,
    )


def _scim_json(body: dict[str, Any], *, status_code: int = 200, headers=None) -> JSONResponse:
    return JSONResponse(
        body, status_code=status_code, media_type=SCIM_MEDIA_TYPE, headers=headers
    )


def _scim_error(method: str, error: ScimError) -> JSONResponse:
    if error.status != 401:
        # A refused token is counted and reported, throttled, by the
        # gateway's rejection accounting; a line per request would let
        # unauthenticated traffic grow the log.
        log.info(
            "SCIM %s answered with an error (status=%d scim_type=%s).",
            method,
            error.status,
            error.scim_type or "none",
        )
    headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
    return _scim_json(error.body(), status_code=error.status, headers=headers)


async def _read_body(request: Request) -> dict[str, Any]:
    """The JSON object of a SCIM request body, at most 64 KiB."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type not in _JSON_MEDIA_TYPES:
        raise ScimError(415, "The request body must be application/scim+json.")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise ScimError(413, "The request body exceeds 64 KiB.")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        # A body nested deeper than the parser recurses is no valid JSON either.
        raise ScimError(400, "The request body is not valid JSON.", "invalidSyntax") from None
    if not isinstance(data, dict):
        raise ScimError(400, "The request body must be a JSON object.", "invalidSyntax")
    return data


def _user_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ScimError(404, "The user does not exist.") from None


def _query_int(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ScimError(400, f"The parameter {name} must be a number.", "invalidValue") from None


def _updated(outcome: ProvisioningOutcome) -> "MirroredUser":
    """The account after a ``PUT`` or ``PATCH``, or the error of its outcome."""
    if outcome.status is UserLifecycleStatus.NOT_FOUND:
        raise ScimError(404, "The user does not exist.")
    if outcome.status is UserLifecycleStatus.CONFLICT:
        raise ScimError(409, "The userName is already taken.", "uniqueness")
    if outcome.status is UserLifecycleStatus.LAST_ADMIN:
        raise ScimError(409, _LAST_ADMIN)
    if outcome.user is None:
        raise ScimError(500, "The user could not be updated.")
    return outcome.user


def build_scim_router(
    *,
    scim: ScimUsers,
    token: str,
    rejections: "GatewayRejections",
    base_url: str | None = None,
) -> APIRouter:
    """The SCIM routes under ``/v1/scim/v2`` for *scim*."""
    router = APIRouter(prefix=SCIM_PREFIX)
    expected = hashlib.sha256(token.encode("utf-8")).digest()

    def _check_token(request: Request) -> None:
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        digest = hashlib.sha256(presented.strip().encode("utf-8")).digest()
        # Digests of equal length: the comparison says nothing about the
        # token's length or content.
        if scheme.lower() != "bearer" or not hmac.compare_digest(digest, expected):
            rejections.record("scim_unauthorized")
            raise ScimError(401, "The SCIM token is missing or invalid.")

    def _base(request: Request) -> str:
        # Behind the web gateway the request names an inner address; the
        # deployment's public base URL is where clients reach SCIM.
        root = base_url or str(request.base_url)
        return f"{root.rstrip('/')}{SCIM_PREFIX}"

    def _location(request: Request, user_id: uuid.UUID) -> str:
        return f"{_base(request)}/Users/{user_id}"

    @router.get("/ServiceProviderConfig")
    async def get_service_provider_config(request: Request) -> Response:
        try:
            _check_token(request)
        except ScimError as error:
            return _scim_error("GET", error)
        return _scim_json(service_provider_config(_base(request)))

    @router.get("/ResourceTypes")
    async def get_resource_types(request: Request) -> Response:
        try:
            _check_token(request)
        except ScimError as error:
            return _scim_error("GET", error)
        resources = [user_resource_type(_base(request))]
        return _scim_json(list_response(resources, total=1, start_index=1))

    @router.get("/Schemas")
    async def get_schemas(request: Request) -> Response:
        try:
            _check_token(request)
        except ScimError as error:
            return _scim_error("GET", error)
        resources = [user_schema(_base(request))]
        return _scim_json(list_response(resources, total=1, start_index=1))

    @router.get("/Schemas/{schema_id}")
    async def get_schema(schema_id: str, request: Request) -> Response:
        try:
            _check_token(request)
            if schema_id != USER_SCHEMA:
                raise ScimError(404, "The schema does not exist.")
        except ScimError as error:
            return _scim_error("GET", error)
        return _scim_json(user_schema(_base(request)))

    @router.get("/ResourceTypes/{type_id}")
    async def get_resource_type(type_id: str, request: Request) -> Response:
        try:
            _check_token(request)
            if type_id != "User":
                raise ScimError(404, "The resource type does not exist.")
        except ScimError as error:
            return _scim_error("GET", error)
        return _scim_json(user_resource_type(_base(request)))

    @router.post("/Users")
    async def create_user(request: Request) -> Response:
        try:
            _check_token(request)
            external_id, profile = user_from_body(await _read_body(request))
            outcome = await scim.create(external_id, profile)
            if outcome.status is UserLifecycleStatus.CONFLICT:
                raise ScimError(
                    409, "The externalId or the userName is already taken.", "uniqueness"
                )
            if outcome.status is UserLifecycleStatus.LAST_ADMIN:
                raise ScimError(409, _LAST_ADMIN)
            if outcome.user is None:
                raise ScimError(500, "The user could not be provisioned.")
        except ScimError as error:
            return _scim_error("POST", error)
        location = _location(request, outcome.user.user_id)
        return _scim_json(
            user_resource(outcome.user, location=location),
            status_code=201,
            headers={"Location": location},
        )

    @router.get("/Users")
    async def list_users(request: Request) -> Response:
        try:
            _check_token(request)
            start_index = min(max(_query_int(request, "startIndex", 1), 1), MAX_START_INDEX)
            count = min(max(_query_int(request, "count", DEFAULT_COUNT), 0), MAX_RESULTS)
            total, users = await scim.list_page(
                filter_text=request.query_params.get("filter"),
                start_index=start_index,
                count=count,
            )
        except ScimError as error:
            return _scim_error("GET", error)
        resources = [
            user_resource(user, location=_location(request, user.user_id))
            for user in users
        ]
        return _scim_json(list_response(resources, total=total, start_index=start_index))

    @router.get("/Users/{user_id}")
    async def get_user(user_id: str, request: Request) -> Response:
        try:
            _check_token(request)
            user = await scim.get(_user_id(user_id))
            if user is None:
                raise ScimError(404, "The user does not exist.")
        except ScimError as error:
            return _scim_error("GET", error)
        return _scim_json(
            user_resource(user, location=_location(request, user.user_id))
        )

    @router.put("/Users/{user_id}")
    async def replace_user(user_id: str, request: Request) -> Response:
        try:
            _check_token(request)
            external_ids, change = replacement_from_body(await _read_body(request))
            user = _updated(
                await scim.update(_user_id(user_id), external_ids=external_ids, change=change)
            )
        except ScimError as error:
            return _scim_error("PUT", error)
        return _scim_json(user_resource(user, location=_location(request, user.user_id)))

    @router.patch("/Users/{user_id}")
    async def patch_user(user_id: str, request: Request) -> Response:
        try:
            _check_token(request)
            external_ids, change = patch_from_body(await _read_body(request))
            user = _updated(
                await scim.update(_user_id(user_id), external_ids=external_ids, change=change)
            )
        except ScimError as error:
            return _scim_error("PATCH", error)
        return _scim_json(user_resource(user, location=_location(request, user.user_id)))

    @router.delete("/Users/{user_id}")
    async def delete_user(user_id: str, request: Request) -> Response:
        try:
            _check_token(request)
            status = await scim.delete(_user_id(user_id))
            if status is UserLifecycleStatus.NOT_FOUND:
                raise ScimError(404, "The user does not exist.")
            if status is UserLifecycleStatus.LAST_ADMIN:
                raise ScimError(409, _LAST_ADMIN)
        except ScimError as error:
            return _scim_error("DELETE", error)
        return Response(status_code=204)

    return router
