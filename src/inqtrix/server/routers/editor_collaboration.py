"""Public editor live-collaboration API."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.pagination import InvalidCursor, decode_cursor
from inqtrix.project.editor_collaboration_ports import (
    CollaborationConflict,
    CollaborationDocumentNotFound,
    CollaborationInstanceFenced,
    CollaborationLeaseInvalid,
    CollaborationRateLimited,
)
from inqtrix.project.editor_ports import DocumentNotFound
from inqtrix.services.collaboration_client import (
    CollaborationNodeConflict,
    CollaborationServiceUnavailable,
)
from inqtrix.services.editor_collaboration_service import (
    CollaborationAuthenticationRequired,
    CollaborationDocumentTooLarge,
    CollaborationProtocolConflict,
)
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

_HISTORY_ACTIVITY_TYPES = frozenset(
    {"direct", "suggestion", "decision", "system", "comment"}
)
_OPEN_ACTIVITY_TYPES = frozenset(
    {"insertion", "deletion", "replacement", "format", "structure"}
)
_COMMENT_STATUSES = frozenset({"all", "open", "resolved"})
_MAX_COMMENT_BODY = 8_192
_MAX_COMMENT_QUOTE = 1_600
_MAX_COMMENT_ANCHOR = 8_192
_MAX_COMMENT_MENTIONS = 50


def build_router(container: "AppContainer") -> APIRouter:
    """Bind public collaboration routes when the optional module is enabled."""
    service = container.editor_collaboration_service
    if service is None:
        raise RuntimeError(
            "build_router(editor_collaboration) requires a wired service"
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency

    @router.post("/v1/editor/documents/{document_id}/collaboration:enable")
    async def enable_collaboration(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        expected_revision = body.get("expected_revision")
        expected_metadata_revision = body.get("expected_metadata_revision")
        schema_version = body.get("schema_version")
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (
                expected_revision,
                expected_metadata_revision,
                schema_version,
            )
        ):
            return error_response(
                400,
                "Revisions and schema_version must be integers",
                "invalid_request_error",
            )
        try:
            state = await service.enable_document(
                document_id=document_id,
                expected_revision=expected_revision,
                expected_metadata_revision=expected_metadata_revision,
                schema_version=schema_version,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return {
            "content_mode": "collaboration",
            "generation": state.generation,
            "schema_version": state.schema_version,
            "schema_hash": state.schema_hash,
            "persisted_sequence": state.persisted_sequence,
            "projection_sequence": state.projection_sequence,
        }

    @router.post("/v1/editor/documents/{document_id}/collaboration/session")
    async def create_session(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        protocol_version = body.get("protocol_version")
        schema_version = body.get("schema_version")
        current_lease_token = body.get("lease_token")
        rotation_command = body.get("rotation_command_id")
        if not isinstance(protocol_version, int) or not isinstance(
            schema_version, int
        ):
            return error_response(
                400,
                "protocol_version and schema_version must be integers",
                "invalid_request_error",
            )
        if current_lease_token is not None and (
            not isinstance(current_lease_token, str)
            or not current_lease_token
            or len(current_lease_token) > 4096
        ):
            return error_response(
                400,
                "lease_token must be a valid string",
                "invalid_request_error",
            )
        if rotation_command is not None and (
            current_lease_token is None or not isinstance(rotation_command, str)
        ):
            return error_response(
                400,
                "rotation_command_id requires a lease_token",
                "invalid_request_error",
            )
        try:
            rotation_command_id = (
                uuid.UUID(rotation_command)
                if rotation_command is not None
                else None
            )
        except ValueError:
            return error_response(
                400,
                "rotation_command_id must be a UUID",
                "invalid_request_error",
            )
        try:
            return await service.create_session(
                document_id=document_id,
                protocol_version=protocol_version,
                schema_version=schema_version,
                current_lease_token=current_lease_token,
                rotation_command_id=rotation_command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise

    @router.get("/v1/editor/documents/{document_id}/activity")
    async def list_activity(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """List history or open patches; unavailable exact previews are null."""
        view = req.query_params.get("view", "history")
        if view not in {"open", "history"}:
            return error_response(
                400, "Invalid activity view", "invalid_request_error"
            )
        cursor = req.query_params.get("cursor")
        author = req.query_params.get("author_id")
        type_filter = req.query_params.get("type")
        try:
            limit = max(1, min(int(req.query_params.get("limit", "50")), 200))
        except ValueError:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        before_sequence: int | None = None
        open_before: tuple[float, str] | None = None
        try:
            if view == "open":
                open_before = decode_cursor(cursor)
            else:
                before_sequence = int(cursor) if cursor is not None else None
        except (InvalidCursor, ValueError):
            return error_response(400, "Invalid cursor", "invalid_cursor")
        try:
            author_user_id = uuid.UUID(author) if author is not None else None
        except ValueError:
            return error_response(
                400, "Invalid author_id", "invalid_request_error"
            )
        if before_sequence is not None and before_sequence < 1:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        allowed_types = (
            _OPEN_ACTIVITY_TYPES if view == "open" else _HISTORY_ACTIVITY_TYPES
        )
        if type_filter is not None and type_filter not in allowed_types:
            return error_response(
                400, "Invalid activity type", "invalid_request_error"
            )
        try:
            page = await service.list_activity(
                document_id=document_id,
                view=cast(Literal["open", "history"], view),
                before_sequence=before_sequence,
                open_before=open_before,
                author_user_id=author_user_id,
                type_filter=type_filter,
                limit=limit,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        # The page holds only JSON-native values, and the inspector polls it
        # while open: answered directly, it skips FastAPI's generic encoder.
        return JSONResponse(
            {
                "object": "list",
                "data": list(page.items),
                "next_cursor": (
                    str(page.next_cursor) if page.next_cursor is not None else None
                ),
            }
        )

    @router.get(
        "/v1/editor/documents/{document_id}/collaboration/comments"
    )
    async def list_comments(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            since_revision = _bounded_integer(
                req.query_params.get("since_revision", "0"),
                field="since_revision",
                minimum=0,
            )
            limit = _bounded_integer(
                req.query_params.get("limit", "100"),
                field="limit",
                minimum=1,
                maximum=200,
            )
            status = req.query_params.get("status", "all")
            if status not in _COMMENT_STATUSES:
                raise ValueError("status must be all, open or resolved")
            return await service.list_comments(
                document_id=document_id,
                since_revision=since_revision,
                status=cast(Literal["all", "open", "resolved"], status),
                limit=limit,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise

    @router.post(
        "/v1/editor/documents/{document_id}/collaboration/comments"
    )
    async def create_comment(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            generation, expected_revision, command_id = _comment_command(body)
            result = await service.create_comment(
                document_id=document_id,
                generation=generation,
                thread_id=_uuid_field(body, "thread_id"),
                message_id=_uuid_field(body, "message_id"),
                anchor=_comment_anchor(body.get("anchor")),
                quote_text=_comment_quote(body.get("quote")),
                body_markdown=_comment_body(body.get("body_markdown")),
                mention_user_ids=_comment_mentions(body.get("mention_user_ids")),
                expected_revision=expected_revision,
                command_id=command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return result

    @router.post(
        "/v1/editor/documents/{document_id}/collaboration/comments/"
        "{thread_id}/replies"
    )
    async def reply_to_comment(
        document_id: str,
        thread_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            generation, expected_revision, command_id = _comment_command(body)
            result = await service.reply_to_comment(
                document_id=document_id,
                generation=generation,
                thread_id=_uuid_value(thread_id, field="thread_id"),
                message_id=_uuid_field(body, "message_id"),
                body_markdown=_comment_body(body.get("body_markdown")),
                mention_user_ids=_comment_mentions(body.get("mention_user_ids")),
                expected_revision=expected_revision,
                command_id=command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return result

    @router.patch(
        "/v1/editor/documents/{document_id}/collaboration/comments/{thread_id}"
    )
    async def update_comment_thread(
        document_id: str,
        thread_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            generation, expected_revision, command_id = _comment_command(body)
            status = body.get("status")
            if status not in {"open", "resolved"}:
                raise ValueError("status must be open or resolved")
            result = await service.set_comment_status(
                document_id=document_id,
                generation=generation,
                thread_id=_uuid_value(thread_id, field="thread_id"),
                status=cast(Literal["open", "resolved"], status),
                expected_revision=expected_revision,
                command_id=command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return result

    @router.delete(
        "/v1/editor/documents/{document_id}/collaboration/comments/{thread_id}"
    )
    async def delete_comment_thread(
        document_id: str,
        thread_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            generation, expected_revision, command_id = _comment_command(body)
            result = await service.delete_comment_thread(
                document_id=document_id,
                generation=generation,
                thread_id=_uuid_value(thread_id, field="thread_id"),
                expected_revision=expected_revision,
                command_id=command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return result

    @router.patch(
        "/v1/editor/documents/{document_id}/collaboration/comments/"
        "{thread_id}/messages/{message_id}"
    )
    async def update_comment_message(
        document_id: str,
        thread_id: str,
        message_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            generation, expected_revision, command_id = _comment_command(body)
            result = await service.update_comment_message(
                document_id=document_id,
                generation=generation,
                thread_id=_uuid_value(thread_id, field="thread_id"),
                message_id=_uuid_value(message_id, field="message_id"),
                body_markdown=_comment_body(body.get("body_markdown")),
                mention_user_ids=_comment_mentions(body.get("mention_user_ids")),
                delete_message=False,
                expected_revision=expected_revision,
                command_id=command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return result

    @router.delete(
        "/v1/editor/documents/{document_id}/collaboration/comments/"
        "{thread_id}/messages/{message_id}"
    )
    async def delete_comment_message(
        document_id: str,
        thread_id: str,
        message_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            generation, expected_revision, command_id = _comment_command(body)
            result = await service.update_comment_message(
                document_id=document_id,
                generation=generation,
                thread_id=_uuid_value(thread_id, field="thread_id"),
                message_id=_uuid_value(message_id, field="message_id"),
                body_markdown=None,
                mention_user_ids=(),
                delete_message=True,
                expected_revision=expected_revision,
                command_id=command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return result

    @router.post(
        "/v1/editor/documents/{document_id}/collaboration/comments/read"
    )
    async def mark_comments_read(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            generation = _integer_field(body, "generation", minimum=1)
            revision = _integer_field(body, "revision", minimum=0)
            last_read_revision = await service.mark_comments_read(
                document_id=document_id,
                generation=generation,
                revision=revision,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return {"last_read_revision": last_read_revision}

    @router.post(
        "/v1/editor/documents/{document_id}/collaboration/projection:flush"
    )
    async def flush_projection(
        document_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            projection = await service.flush_projection(
                document_id=document_id,
                principal=principal,
                visible_to=visible_to,
            )
            if projection.authoritative_sequence != projection.sequence:
                raise CollaborationConflict(
                    "projection_not_current",
                    current_sequence=projection.authoritative_sequence,
                )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return {
            "generation": projection.generation,
            "sequence": projection.sequence,
            "authoritative_sequence": projection.authoritative_sequence,
            "content_markdown": projection.markdown,
            "projection_hash": projection.projection_hash,
        }

    @router.post("/v1/editor/documents/{document_id}/patches:decide")
    async def decide_patches(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        patch_ids = body.get("patch_ids")
        decision = body.get("decision")
        expected_sequence = body.get("expected_sequence")
        decision_id = body.get("decision_id")
        if (
            not isinstance(patch_ids, list)
            or not patch_ids
            or not all(isinstance(value, str) and value for value in patch_ids)
            or decision not in {"accept", "reject"}
            or not isinstance(expected_sequence, int)
            or isinstance(expected_sequence, bool)
            or expected_sequence < 0
            or not isinstance(decision_id, str)
        ):
            return error_response(
                400, "Invalid decision command", "invalid_request_error"
            )
        try:
            command_id = uuid.UUID(decision_id)
            parsed_patch_ids = tuple(str(uuid.UUID(value)) for value in patch_ids)
        except ValueError:
            return error_response(
                400,
                "decision_id and patch_ids must be UUIDs",
                "invalid_request_error",
            )
        try:
            result = await service.decide(
                document_id=document_id,
                patch_ids=parsed_patch_ids,
                decision=decision,
                expected_sequence=expected_sequence,
                command_id=command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return {
            "decision_id": str(result.command_id),
            "sequence": result.sequence,
            "suggestion_ids": list(result.suggestion_ids),
        }

    @router.post(
        "/v1/editor/documents/{document_id}/suggestions:publish"
    )
    async def publish_suggestion(
        document_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        body = await _json_object(req)
        if body is None:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        patch_id = body.get("patch_id")
        command_id = body.get("command_id")
        actor_kind = body.get("actor_kind")
        expected_sequence = body.get("expected_sequence")
        target_markdown = body.get("target_markdown")
        # The projection the target was built on. With it, the
        # collaboration service places the AI change onto the document as it
        # is now; without it, the room must still be at expected_sequence.
        base_markdown = body.get("base_markdown")
        if (
            not isinstance(patch_id, str)
            or not isinstance(command_id, str)
            or actor_kind != "assistant"
            or not isinstance(expected_sequence, int)
            or isinstance(expected_sequence, bool)
            or expected_sequence < 0
            or not isinstance(target_markdown, str)
            or (base_markdown is not None and not isinstance(base_markdown, str))
        ):
            return error_response(
                400,
                "Invalid suggestion command",
                "invalid_request_error",
            )
        try:
            parsed_patch_id = uuid.UUID(patch_id)
            parsed_command_id = uuid.UUID(command_id)
        except ValueError:
            return error_response(
                400,
                "patch_id and command_id must be UUIDs",
                "invalid_request_error",
            )
        try:
            result = await service.publish_suggestion(
                document_id=document_id,
                patch_id=str(parsed_patch_id),
                target_markdown=target_markdown,
                base_markdown=base_markdown,
                actor_kind="assistant",
                expected_sequence=expected_sequence,
                command_id=parsed_command_id,
                principal=principal,
                visible_to=visible_to,
            )
        except Exception as exc:
            response = _public_error(exc)
            if response is not None:
                return response
            raise
        return {
            "command_id": str(result.command_id),
            "patch_id": result.patch_id,
            "sequence": result.sequence,
            "suggestion_ids": list(result.suggestion_ids),
        }

    return router


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _bounded_integer(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} must be an integer') from exc
    if isinstance(value, bool) or parsed < minimum:
        raise ValueError(f'{field} is outside the allowed range')
    if maximum is not None and parsed > maximum:
        raise ValueError(f'{field} is outside the allowed range')
    return parsed


def _integer_field(
    body: dict[str, Any],
    field: str,
    *,
    minimum: int,
) -> int:
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f'{field} must be a valid integer')
    return value


def _uuid_value(value: Any, *, field: str) -> uuid.UUID:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f'{field} must be a UUID')
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f'{field} must be a UUID') from exc


def _uuid_field(body: dict[str, Any], field: str) -> uuid.UUID:
    return _uuid_value(body.get(field), field=field)


def _comment_command(
    body: dict[str, Any],
) -> tuple[int, int, uuid.UUID]:
    return (
        _integer_field(body, "generation", minimum=1),
        _integer_field(body, "expected_revision", minimum=0),
        _uuid_field(body, "command_id"),
    )


def _comment_body(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("body_markdown must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_COMMENT_BODY
        or "\x00" in normalized
    ):
        raise ValueError("body_markdown is empty or too long")
    return normalized


def _comment_quote(value: Any) -> str:
    if not isinstance(value, str) or len(value) > _MAX_COMMENT_QUOTE:
        raise ValueError("quote must be a short string")
    if "\x00" in value:
        raise ValueError("quote contains invalid characters")
    return value


def _comment_mentions(value: Any) -> tuple[uuid.UUID, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or len(value) > _MAX_COMMENT_MENTIONS
    ):
        raise ValueError("mention_user_ids must be a short list")
    parsed = tuple(
        _uuid_value(item, field="mention_user_ids") for item in value
    )
    if len(set(parsed)) != len(parsed):
        raise ValueError("mention_user_ids must not contain duplicates")
    return parsed


def _comment_anchor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("anchor must be an object")
    if (
        len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        > _MAX_COMMENT_ANCHOR
    ):
        raise ValueError("anchor is too large")
    required_text = ("quoteBefore", "selectedText", "quoteAfter")
    if any(
        not isinstance(value.get(field), str)
        for field in required_text
    ):
        raise ValueError("anchor contains invalid text fields")
    if any(
        len(cast(str, value[field])) > _MAX_COMMENT_QUOTE
        for field in required_text
    ):
        raise ValueError("anchor contains text fields that are too long")
    start = value.get("from")
    end = value.get("to")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or start < 0
        or not isinstance(end, int)
        or isinstance(end, bool)
        or end < start
    ):
        raise ValueError("anchor contains an invalid range")
    for field in ("relativeFrom", "relativeTo"):
        relative = value.get(field)
        if relative is not None and (
            not isinstance(relative, str) or len(relative) > 4096
        ):
            raise ValueError("anchor contains an invalid relative position")
    return dict(value)


def _public_error(exc: Exception):
    if isinstance(exc, (DocumentNotFound, CollaborationDocumentNotFound)):
        return error_response(404, "Document not found", "not_found")
    if isinstance(exc, CollaborationAuthenticationRequired):
        return error_response(
            403,
            "Live collaboration requires an active browser session.",
            "forbidden",
            reason="cookie_session_required",
        )
    if isinstance(exc, CollaborationDocumentTooLarge):
        return error_response(
            413,
            "The document is too large for live collaboration.",
            "invalid_request_error",
            reason="document_too_large",
        )
    if isinstance(exc, CollaborationRateLimited):
        return error_response(
            429,
            "Too many collaboration sessions or session requests.",
            "rate_limit_error",
            reason=exc.reason,
        )
    if isinstance(exc, CollaborationServiceUnavailable):
        return error_response(
            503,
            "Live collaboration is currently unavailable.",
            "service_unavailable",
            reason="collaboration_unavailable",
        )
    if isinstance(
        exc,
        (
            CollaborationConflict,
            CollaborationProtocolConflict,
            CollaborationNodeConflict,
        ),
    ):
        reason = getattr(exc, "reason", str(exc))
        current_sequence = getattr(exc, "current_sequence", None)
        # Nur wo der Server einen konkreten Stand nennen kann, ist die
        # Aussage "nicht mehr aktuell" wahr. Fuer jeden anderen Konflikt
        # benennt die Meldung den Vorgang, ohne eine Ursache zu erfinden:
        # eine falsche Begruendung schickt den Nutzer in eine Wiederholung,
        # die nie gelingen kann, und verdeckt den echten Grund.
        message = (
            "The collaboration state is no longer current."
            if current_sequence is not None
            else f'The change was rejected (reason: {reason}).'
        )
        return error_response(
            409,
            message,
            "conflict",
            reason=reason,
            current_sequence=current_sequence,
        )
    if isinstance(exc, CollaborationLeaseInvalid):
        if exc.reason in {"access_revoked", "permission_denied"}:
            return error_response(
                403,
                "Collaboration access was revoked.",
                "forbidden",
                reason=exc.reason,
            )
        return error_response(
            401,
            "The collaboration session is no longer valid.",
            "authentication_error",
            reason=exc.reason,
        )
    if isinstance(exc, CollaborationInstanceFenced):
        return error_response(
            503,
            "Live collaboration is not ready.",
            "service_unavailable",
            reason=str(exc),
        )
    if isinstance(exc, ValueError):
        return error_response(400, str(exc), "invalid_request_error")
    return None
