"""Owner and public HTTP routes for secure editor guest links."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from inqtrix.auth.principal import Principal
from inqtrix.auth.ratelimit import client_ip
from inqtrix.project.editor_guest_links import (
    EditorGuestLinkConflict,
    EditorGuestLinkExpired,
    EditorGuestLinkNotFound,
    EditorGuestLinkRateLimited,
)
from inqtrix.project.editor_collaboration_ports import CollaborationConflict
from inqtrix.services.editor_collaboration_service import (
    CollaborationAuthenticationRequired,
)
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

_GUEST_COOKIE = "inqtrix_editor_guest"
_GUEST_CSRF_COOKIE = "inqtrix_editor_guest_csrf"
_GUEST_CSRF_HEADER = "x-inqtrix-guest-csrf"


def build_router(container: "AppContainer") -> APIRouter:
    service = container.editor_guest_link_service
    if service is None:
        raise RuntimeError(
            "build_router(editor_guest_links) requires a wired service"
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    trusted_origin = _origin(container.settings.server.public_base_url)

    async def _record_owner_event(
        *,
        action: str,
        link: object,
        document_id: str,
        principal: Principal,
        detail: dict[str, str] | None = None,
    ) -> None:
        """Write one owner-side guest-link fact to the security log.

        Issuing, re-permissioning, rotating and ending a guest link all
        change who outside this instance can reach a document, which is
        the higher-risk half of sharing: the counterpart for an internal
        recipient (``share.granted`` / ``share.revoked``) has always been
        recorded, while the account-less one was not. Only the guest's
        USE of a link (``guest_link.accessed``) was.

        Defensive resolution mirrors that existing call: embedded and test
        compositions run this router without a permission service, and
        telemetry never imposes one.

        ``link`` is whatever the service returned for the route: the
        payload shape is the service's business, so the identifier is
        resolved here once instead of at every call site.
        """
        audit_sink = getattr(
            getattr(container, "permission_service", None),
            "audit_sink",
            None,
        )
        if audit_sink is None:
            return
        from inqtrix.services.audit_service import AuditService

        link_id = link.get("id") if isinstance(link, dict) else link

        await AuditService(audit_sink).record_event(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            action=action,
            resource_type="guest_link",
            resource_id=str(link_id),
            detail={"document_id": str(document_id), **(detail or {})},
        )

    @router.get("/v1/editor/documents/{document_id}/share-links")
    async def list_links(
        document_id: str,
        principal: Principal = Depends(principal_dep),
    ):
        try:
            links = await service.list_links(
                document_id=document_id,
                principal=principal,
            )
        except Exception as exc:
            response = _owner_error(exc)
            if response is not None:
                return response
            raise
        return {"object": "list", "data": list(links)}

    @router.post(
        "/v1/editor/documents/{document_id}/share-links",
        status_code=201,
    )
    async def create_link(
        document_id: str,
        request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(request)
        if body is None:
            return _invalid("Invalid JSON body")
        try:
            command_id = _uuid(body, "command_id")
            generation = _positive_int(body, "generation")
            permission = _permission(body.get("permission"))
            ttl_seconds = _optional_positive_int(body, "ttl_seconds")
            result = await service.create_link(
                document_id=document_id,
                permission=permission,
                ttl_seconds=ttl_seconds,
                command_id=command_id,
                principal=principal,
                generation=generation,
            )
        except Exception as exc:
            response = _owner_error(exc)
            if response is not None:
                return response
            raise
        await _record_owner_event(
            action="guest_link.created",
            link=result,
            document_id=document_id,
            principal=principal,
            detail={"permission": permission},
        )
        return {"object": "editor_share_link", "data": result}

    @router.patch(
        "/v1/editor/documents/{document_id}/share-links/{link_id}"
    )
    async def update_link(
        document_id: str,
        link_id: str,
        request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(request)
        if body is None:
            return _invalid("Invalid JSON body")
        try:
            parsed_link_id = uuid.UUID(link_id)
            permission = (
                _permission(body.get("permission"))
                if "permission" in body
                else None
            )
            ttl_seconds = _optional_positive_int(body, "ttl_seconds")
            result = await service.update_link(
                document_id=document_id,
                link_id=parsed_link_id,
                permission=permission,
                ttl_seconds=ttl_seconds,
                expected_revision=_non_negative_int(body, "expected_revision"),
                command_id=_uuid(body, "command_id"),
                principal=principal,
            )
        except Exception as exc:
            response = _owner_error(exc)
            if response is not None:
                return response
            raise
        await _record_owner_event(
            action="guest_link.permission_updated",
            link=result,
            document_id=document_id,
            principal=principal,
            detail={"permission": permission},
        )
        return {"object": "editor_share_link", "data": result}

    @router.delete(
        "/v1/editor/documents/{document_id}/share-links/{link_id}"
    )
    async def revoke_link(
        document_id: str,
        link_id: str,
        request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(request)
        if body is None:
            return _invalid("Invalid JSON body")
        try:
            result = await service.revoke_link(
                document_id=document_id,
                link_id=uuid.UUID(link_id),
                expected_revision=_non_negative_int(body, "expected_revision"),
                command_id=_uuid(body, "command_id"),
                principal=principal,
            )
        except Exception as exc:
            response = _owner_error(exc)
            if response is not None:
                return response
            raise
        await _record_owner_event(
            action="guest_link.revoked",
            link=result,
            document_id=document_id,
            principal=principal,
        )
        return {"object": "editor_share_link", "data": result}

    @router.post(
        "/v1/editor/documents/{document_id}/share-links/"
        "{link_id}:rotate-password"
    )
    async def rotate_password(
        document_id: str,
        link_id: str,
        request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(request)
        if body is None:
            return _invalid("Invalid JSON body")
        try:
            result = await service.rotate_password(
                document_id=document_id,
                link_id=uuid.UUID(link_id),
                expected_revision=_non_negative_int(body, "expected_revision"),
                command_id=_uuid(body, "command_id"),
                principal=principal,
            )
        except Exception as exc:
            response = _owner_error(exc)
            if response is not None:
                return response
            raise
        await _record_owner_event(
            action="guest_link.password_rotated",
            link=result,
            document_id=document_id,
            principal=principal,
        )
        return {"object": "editor_share_link", "data": result}

    @router.post(
        "/v1/editor/documents/{document_id}/share-links/"
        "{link_id}:rotate-token"
    )
    async def rotate_token(
        document_id: str,
        link_id: str,
        request: Request,
        principal: Principal = Depends(principal_dep),
    ):
        body = await _json_object(request)
        if body is None:
            return _invalid("Invalid JSON body")
        try:
            result = await service.rotate_token(
                document_id=document_id,
                link_id=uuid.UUID(link_id),
                expected_revision=_non_negative_int(body, "expected_revision"),
                command_id=_uuid(body, "command_id"),
                principal=principal,
            )
        except Exception as exc:
            response = _owner_error(exc)
            if response is not None:
                return response
            raise
        await _record_owner_event(
            action="guest_link.token_rotated",
            link=result,
            document_id=document_id,
            principal=principal,
        )
        return {"object": "editor_share_link", "data": result}

    @router.get(
        "/v1/editor/documents/{document_id}/access-summary"
    )
    async def access_summary(
        document_id: str,
        window: str = "7d",
        principal: Principal = Depends(principal_dep),
    ):
        if window not in {"7d", "30d"}:
            return _invalid("window must be 7d or 30d")
        try:
            guest_activity = await service.access_summary(
                document_id=document_id,
                principal=principal,
                window_seconds=(
                    7 * 24 * 60 * 60
                    if window == "7d"
                    else 30 * 24 * 60 * 60
                ),
            )
            links = await service.list_links(
                document_id=document_id,
                principal=principal,
            )
            direct_shares = await container.share_service.list_for_resource(
                principal,
                resource_type="editor_document",
                resource_id=document_id,
            )
        except Exception as exc:
            response = _owner_error(exc)
            if response is not None:
                return response
            raise
        return {
            "object": "editor_access_summary",
            "window": window,
            "direct_share_count": len(direct_shares),
            **guest_activity,
            "share_links": list(links),
        }

    @router.get("/v1/editor/share-links/{token}")
    async def describe_link(token: str):
        try:
            payload = await service.describe_token(token)
        except (EditorGuestLinkNotFound, EditorGuestLinkExpired):
            return _guest_error(404, "This link is invalid or has expired.")
        return _guest_json(payload)

    @router.post("/v1/editor/share-links/{token}:unlock")
    async def unlock_link(token: str, request: Request):
        if not _origin_allowed(request, trusted_origin):
            return _guest_error(403, "Disallowed origin.")
        body = await _json_object(request)
        if body is None:
            return _guest_error(400, "Invalid request.")
        password = body.get("password")
        display_name = body.get("display_name")
        if not isinstance(password, str) or len(password) > 256:
            return _guest_error(400, "Invalid request.")
        if display_name is not None and not isinstance(display_name, str):
            return _guest_error(400, "Invalid request.")
        throttle_key = service.throttle_key(
            token=token,
            source_ip=client_ip(
                request,
                trusted_proxy_hops=(
                    container.settings.auth.trusted_proxy_hops
                ),
            ),
        )
        try:
            unlocked = await service.unlock(
                token=token,
                password=password,
                display_name=display_name,
                throttle_key=throttle_key,
            )
        except EditorGuestLinkRateLimited:
            return _guest_error(
                429,
                "Too many attempts. Please try again later.",
            )
        except (EditorGuestLinkNotFound, EditorGuestLinkExpired):
            return _guest_error(401, "Invalid link or password.")
        except ValueError:
            return _guest_error(400, "The display name is invalid.")
        # guest_link.accessed: account-less access to a shared
        # document is exactly what the trail must show. Anonymous actor;
        # ip/user_agent come from the ambient request origin, the auth
        # method is stamped explicitly (no principal resolution ran).
        # getattr-defensive: embedded/test compositions run this router
        # without a permission service — telemetry never imposes one.
        audit_sink = getattr(
            getattr(container, "permission_service", None),
            "audit_sink",
            None,
        )
        if audit_sink is not None:
            from inqtrix.services.audit_service import AuditService

            await AuditService(audit_sink).record_event(
                tenant_id="default",
                action="guest_link.accessed",
                resource_type="guest_link",
                resource_id=str(unlocked.access.link.id),
                detail={
                    "document_id": str(unlocked.access.link.document_id)
                },
                origin={"auth_method": "guest_link"},
            )
        csrf = secrets.token_urlsafe(32)
        response = _guest_json(service.guest_payload(unlocked.access))
        max_age = max(
            1,
            int(unlocked.access.identity.expires_at - unlocked.access.identity.created_at),
        )
        # Secure unless the operator explicitly opted into HTTP guest
        # links (dev escape hatch; the boot guard already logged the
        # loud warning). Without this, browsers would drop the cookies
        # over plain http and every guest login would silently fail.
        secure_cookies = (
            not container.settings.editor_guest_links.allow_insecure_http
        )
        response.set_cookie(
            _GUEST_COOKIE,
            unlocked.session_token,
            max_age=max_age,
            secure=secure_cookies,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            _GUEST_CSRF_COOKIE,
            csrf,
            max_age=max_age,
            secure=secure_cookies,
            httponly=False,
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/v1/editor/guest/session")
    async def guest_session(request: Request):
        token = request.cookies.get(_GUEST_COOKIE, "")
        if not token:
            return _guest_error(401, "Guest session required.")
        try:
            access = await service.session(token)
        except (EditorGuestLinkNotFound, EditorGuestLinkExpired):
            return _guest_error(401, "Guest session has expired.")
        return _guest_json(service.guest_payload(access))

    @router.post("/v1/editor/guest/collaboration/session")
    async def guest_collaboration_session(request: Request):
        denied = _guest_mutation_denied(request, trusted_origin)
        if denied is not None:
            return denied
        token = request.cookies.get(_GUEST_COOKIE, "")
        body = await _json_object(request)
        if not token or body is None:
            return _guest_error(401, "Guest session required.")
        try:
            rotation_command = body.get("rotation_command_id")
            result = await service.create_collaboration_session(
                session_token=token,
                protocol_version=_positive_int(body, "protocol_version"),
                schema_version=_positive_int(body, "schema_version"),
                current_lease_token=(
                    str(body["lease_token"])
                    if body.get("lease_token") is not None
                    else None
                ),
                rotation_command_id=(
                    uuid.UUID(str(rotation_command))
                    if rotation_command is not None
                    else None
                ),
                display_name=(
                    str(body["display_name"])
                    if body.get("display_name") is not None
                    else None
                ),
            )
        except ValueError as exc:
            return _guest_error(400, str(exc))
        except CollaborationAuthenticationRequired:
            return _guest_error(401, "Guest session is no longer valid.")
        except (EditorGuestLinkNotFound, EditorGuestLinkExpired):
            return _guest_error(401, "Guest session has expired.")
        return _guest_json(result)

    @router.get("/v1/editor/guest/collaboration/comments")
    async def list_guest_comments(request: Request):
        token = request.cookies.get(_GUEST_COOKIE, "")
        try:
            access = await service.session(token)
            status = request.query_params.get("status", "all")
            if status not in {"all", "open", "resolved"}:
                raise ValueError("Invalid comment filter.")
            result = await container.editor_collaboration_service.list_guest_comments(
                access=access,
                since_revision=_query_int(
                    request, "since_revision", default=0, minimum=0, maximum=None
                ),
                status=status,
                limit=_query_int(
                    request, "limit", default=50, minimum=1, maximum=200
                ),
            )
        except Exception as exc:
            denied = _guest_collaboration_error(exc)
            if denied is not None:
                return denied
            raise
        return _guest_json(result)

    @router.post("/v1/editor/guest/collaboration/comments")
    async def create_guest_comment(request: Request):
        denied = _guest_mutation_denied(request, trusted_origin)
        if denied is not None:
            return denied
        body = await _json_object(request)
        try:
            access = await service.session(
                request.cookies.get(_GUEST_COOKIE, "")
            )
            result = await container.editor_collaboration_service.create_guest_comment(
                access=access,
                thread_id=_uuid(body or {}, "thread_id"),
                message_id=_uuid(body or {}, "message_id"),
                anchor=_comment_anchor((body or {}).get("anchor")),
                quote_text=_comment_quote((body or {}).get("quote")),
                body_markdown=_comment_body((body or {}).get("body_markdown")),
                mention_user_ids=_comment_mentions(
                    (body or {}).get("mention_user_ids")
                ),
                expected_revision=_non_negative_int(
                    body or {}, "expected_revision"
                ),
                command_id=_uuid(body or {}, "command_id"),
            )
        except Exception as exc:
            denied = _guest_collaboration_error(exc)
            if denied is not None:
                return denied
            raise
        return _guest_json(result)

    @router.post(
        "/v1/editor/guest/collaboration/comments/{thread_id}/replies"
    )
    async def reply_to_guest_comment(thread_id: str, request: Request):
        denied = _guest_mutation_denied(request, trusted_origin)
        if denied is not None:
            return denied
        body = await _json_object(request)
        try:
            access = await service.session(
                request.cookies.get(_GUEST_COOKIE, "")
            )
            result = (
                await container.editor_collaboration_service.reply_to_guest_comment(
                    access=access,
                    thread_id=uuid.UUID(thread_id),
                    message_id=_uuid(body or {}, "message_id"),
                    body_markdown=_comment_body(
                        (body or {}).get("body_markdown")
                    ),
                    mention_user_ids=_comment_mentions(
                        (body or {}).get("mention_user_ids")
                    ),
                    expected_revision=_non_negative_int(
                        body or {}, "expected_revision"
                    ),
                    command_id=_uuid(body or {}, "command_id"),
                )
            )
        except Exception as exc:
            denied = _guest_collaboration_error(exc)
            if denied is not None:
                return denied
            raise
        return _guest_json(result)

    @router.patch(
        "/v1/editor/guest/collaboration/comments/{thread_id}"
    )
    async def update_guest_comment_thread(thread_id: str, request: Request):
        denied = _guest_mutation_denied(request, trusted_origin)
        if denied is not None:
            return denied
        body = await _json_object(request)
        try:
            status = (body or {}).get("status")
            if status not in {"open", "resolved"}:
                raise ValueError("Invalid thread status.")
            access = await service.session(
                request.cookies.get(_GUEST_COOKIE, "")
            )
            result = (
                await container.editor_collaboration_service.set_guest_comment_status(
                    access=access,
                    thread_id=uuid.UUID(thread_id),
                    status=status,
                    expected_revision=_non_negative_int(
                        body or {}, "expected_revision"
                    ),
                    command_id=_uuid(body or {}, "command_id"),
                )
            )
        except Exception as exc:
            denied = _guest_collaboration_error(exc)
            if denied is not None:
                return denied
            raise
        return _guest_json(result)

    @router.patch(
        "/v1/editor/guest/collaboration/comments/{thread_id}/"
        "messages/{message_id}"
    )
    async def update_guest_comment_message(
        thread_id: str,
        message_id: str,
        request: Request,
    ):
        return await _mutate_guest_comment_message(
            request=request,
            thread_id=thread_id,
            message_id=message_id,
            delete_message=False,
            trusted_origin=trusted_origin,
            service=service,
            collaboration_service=container.editor_collaboration_service,
        )

    @router.delete(
        "/v1/editor/guest/collaboration/comments/{thread_id}/"
        "messages/{message_id}"
    )
    async def delete_guest_comment_message(
        thread_id: str,
        message_id: str,
        request: Request,
    ):
        return await _mutate_guest_comment_message(
            request=request,
            thread_id=thread_id,
            message_id=message_id,
            delete_message=True,
            trusted_origin=trusted_origin,
            service=service,
            collaboration_service=container.editor_collaboration_service,
        )

    @router.post("/v1/editor/guest/collaboration/comments/read")
    async def mark_guest_comments_read(request: Request):
        denied = _guest_mutation_denied(request, trusted_origin)
        if denied is not None:
            return denied
        body = await _json_object(request)
        try:
            access = await service.session(
                request.cookies.get(_GUEST_COOKIE, "")
            )
            revision = (
                await container.editor_collaboration_service.mark_guest_comments_read(
                    access=access,
                    revision=_non_negative_int(body or {}, "revision"),
                )
            )
        except Exception as exc:
            denied = _guest_collaboration_error(exc)
            if denied is not None:
                return denied
            raise
        return _guest_json({"last_read_revision": revision})

    return router


def _owner_error(exc: Exception):
    if isinstance(exc, (EditorGuestLinkNotFound, EditorGuestLinkExpired)):
        return error_response(404, "Not found", "not_found")
    if isinstance(exc, EditorGuestLinkConflict):
        extra = (
            {"current_revision": exc.current_revision}
            if exc.current_revision is not None
            else {}
        )
        return error_response(409, exc.reason, "conflict", **extra)
    if isinstance(exc, (ValueError, TypeError)):
        return _invalid(str(exc))
    return None


def _guest_json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _guest_error(status: int, message: str) -> JSONResponse:
    return _guest_json(
        {"error": {"message": message, "type": "guest_access_error"}},
        status_code=status,
    )


def _guest_collaboration_error(exc: Exception) -> JSONResponse | None:
    if isinstance(exc, (EditorGuestLinkNotFound, EditorGuestLinkExpired)):
        return _guest_error(401, "Guest session has expired.")
    if isinstance(exc, CollaborationAuthenticationRequired):
        return _guest_error(403, str(exc))
    if isinstance(exc, CollaborationConflict):
        return _guest_error(
            409,
            str(exc) or "The comment state has changed.",
        )
    if isinstance(exc, (ValueError, TypeError)):
        return _guest_error(400, str(exc))
    return None


async def _mutate_guest_comment_message(
    *,
    request: Request,
    thread_id: str,
    message_id: str,
    delete_message: bool,
    trusted_origin: str,
    service: Any,
    collaboration_service: Any,
) -> JSONResponse:
    denied = _guest_mutation_denied(request, trusted_origin)
    if denied is not None:
        return denied
    body = await _json_object(request)
    try:
        access = await service.session(
            request.cookies.get(_GUEST_COOKIE, "")
        )
        result = await collaboration_service.update_guest_comment_message(
            access=access,
            thread_id=uuid.UUID(thread_id),
            message_id=uuid.UUID(message_id),
            body_markdown=(
                None
                if delete_message
                else _comment_body((body or {}).get("body_markdown"))
            ),
            mention_user_ids=(
                ()
                if delete_message
                else _comment_mentions((body or {}).get("mention_user_ids"))
            ),
            delete_message=delete_message,
            expected_revision=_non_negative_int(
                body or {}, "expected_revision"
            ),
            command_id=_uuid(body or {}, "command_id"),
        )
    except Exception as exc:
        denied = _guest_collaboration_error(exc)
        if denied is not None:
            return denied
        raise
    return _guest_json(result)


def _guest_mutation_denied(
    request: Request,
    trusted_origin: str,
) -> JSONResponse | None:
    if not _origin_allowed(request, trusted_origin):
        return _guest_error(403, "Disallowed origin.")
    header = request.headers.get(_GUEST_CSRF_HEADER, "")
    cookie = request.cookies.get(_GUEST_CSRF_COOKIE, "")
    if (
        not header
        or not cookie
        or not hmac.compare_digest(
            hashlib.sha256(header.encode("utf-8")).digest(),
            hashlib.sha256(cookie.encode("utf-8")).digest(),
        )
    ):
        return _guest_error(403, "CSRF validation failed.")
    return None


def _origin_allowed(request: Request, trusted_origin: str) -> bool:
    origin = request.headers.get("origin")
    return origin == trusted_origin


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _uuid(body: dict[str, Any], key: str) -> uuid.UUID:
    value = body.get(key)
    if not isinstance(value, str):
        raise ValueError(f'{key} must be a UUID')
    return uuid.UUID(value)


def _positive_int(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if type(value) is not int or value < 1:
        raise ValueError(f'{key} must be positive')
    return value


def _non_negative_int(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f'{key} must be non-negative')
    return value


def _optional_positive_int(
    body: dict[str, Any],
    key: str,
) -> int | None:
    if body.get(key) is None:
        return None
    return _positive_int(body, key)


def _permission(value: Any):
    if value not in {"view", "comment", "suggest", "edit"}:
        raise ValueError("permission is invalid")
    return value


def _query_int(
    request: Request,
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None,
) -> int:
    raw = request.query_params.get(key)
    value = default if raw is None else int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{key} is outside the valid range')
    return value


def _comment_body(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("body_markdown must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 20_000:
        raise ValueError("body_markdown is invalid")
    return normalized


def _comment_quote(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 1_000:
        raise ValueError("quote is invalid")
    return value


def _comment_mentions(value: Any) -> tuple[uuid.UUID, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("mention_user_ids is invalid")
    return tuple(dict.fromkeys(uuid.UUID(str(item)) for item in value))


def _comment_anchor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("anchor is invalid")
    encoded = str(value)
    if len(encoded) > 20_000:
        raise ValueError("anchor is too large")
    return value


def _invalid(message: str):
    return error_response(400, message, "invalid_request_error")
