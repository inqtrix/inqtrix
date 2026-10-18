"""Private collaboration sidecar API.

The Node service has no database credentials. Every durable read and write
crosses this router, which authenticates the service secret before handing the
request to the collaboration application service.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import math
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, Request
from fastapi.responses import Response

from inqtrix.project.editor_collaboration_ports import (
    CollaborationConflict,
    CollaborationDocumentNotFound,
    CollaborationInstanceFenced,
    CollaborationLeaseInvalid,
    CollaborationPatchState,
    CollaborationSnapshot,
    CollaborationSuggestion,
    PersistCollaborationUpdate,
)
from inqtrix.services.editor_collaboration_service import (
    CollaborationAuthenticationRequired,
    CollaborationDocumentTooLarge,
    CollaborationProtocolConflict,
)
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")

_ACTOR_KINDS = frozenset({"human", "guest", "assistant", "agent", "system"})
_CHANGE_KINDS = frozenset({"direct", "suggestion", "decision", "system"})
_DECISIONS = frozenset({"accept", "reject"})
_DECISION_OUTCOMES = frozenset({"accepted", "rejected"})
_SUGGESTION_KINDS = frozenset(
    {"insertion", "deletion", "replacement", "format", "structure"}
)


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the secret-authenticated Node-to-FastAPI persistence surface."""
    service = container.editor_collaboration_service
    if service is None:
        raise RuntimeError(
            "build_router(internal_collaboration) requires a wired service"
        )
    secret = container.settings.collaboration.secret
    router = APIRouter()

    @router.post("/internal/collaboration/instances/acquire")
    async def acquire_instance(request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        try:
            lease = await service.acquire_instance(
                tenant_id=_bounded_string(body, "tenant_id", maximum=160),
                instance_id=_bounded_string(body, "instance_id", maximum=160),
                lease_seconds=_positive_number(body, "lease_seconds"),
                protocol_version=_positive_int(body, "protocol_version"),
                schema_version=_positive_int(body, "schema_version"),
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise
        return _instance_payload(lease)

    @router.post("/internal/collaboration/instances/renew")
    async def renew_instance(request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        try:
            lease = await service.renew_instance(
                tenant_id=_bounded_string(body, "tenant_id", maximum=160),
                instance_id=_bounded_string(body, "instance_id", maximum=160),
                epoch=_positive_int(body, "epoch"),
                lease_seconds=_positive_number(body, "lease_seconds"),
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise
        return _instance_payload(lease)

    @router.post("/internal/collaboration/leases/introspect")
    async def introspect_lease(request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        try:
            return await service.introspect_lease(
                token=_bounded_string(body, "lease_token", maximum=4096),
                room=_bounded_string(body, "room", maximum=256),
                instance_id=_bounded_string(body, "instance_id", maximum=160),
                epoch=_positive_int(body, "epoch"),
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise

    @router.get(
        "/internal/collaboration/documents/{document_id}/state"
    )
    async def load_document_state(document_id: str, request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        try:
            state = await service.load_state(
                tenant_id=_bounded_query_string(
                    request, "tenant_id", maximum=160
                ),
                document_id=document_id,
                generation=_positive_query_int(request, "generation"),
                instance_id=_bounded_query_string(
                    request, "instance_id", maximum=160
                ),
                epoch=_positive_query_int(request, "epoch"),
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise
        return {
            "document_id": state.document.document_id,
            "generation": state.document.generation,
            "persisted_sequence": state.document.persisted_sequence,
            "schema_version": state.document.schema_version,
            "schema_hash": state.document.schema_hash,
            "snapshot": {
                "covered_sequence": state.snapshot.covered_sequence,
                "state_update_base64": _base64(state.snapshot.state_update),
                "state_vector_base64": _base64(state.snapshot.state_vector),
                "state_hash": state.snapshot.state_hash,
            },
            "updates": [
                {
                    "sequence": update.sequence,
                    "update_hash": update.update_hash,
                    "update_base64": _base64(cast(bytes, update.update_bytes)),
                }
                for update in state.updates
            ],
            "snapshot_candidates": [
                _snapshot_candidate_payload(state.snapshot, state.updates),
                *[
                    _snapshot_candidate_payload(
                        candidate.snapshot, candidate.updates
                    )
                    for candidate in state.fallback_candidates
                ],
            ],
        }

    @router.post(
        "/internal/collaboration/documents/{document_id}/updates"
    )
    async def persist_update(document_id: str, request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        try:
            update_bytes = _canonical_base64(body, "update_base64")
            update_hash = _sha256(body, "update_hash")
            if not hmac.compare_digest(
                hashlib.sha256(update_bytes).hexdigest(), update_hash
            ):
                raise ValueError("update_hash_mismatch")
            actor_kind = _enum(body, "actor_kind", _ACTOR_KINDS)
            change_kind = _enum(body, "change_kind", _CHANGE_KINDS)
            suggestion_ids = _uuid_strings(body, "suggestion_ids", maximum=1000)
            suggestions = _suggestions(body)
            patches = _patch_states(body)
            # A sidecar that predates removal by edit never sends the field,
            # and never removes a suggestion without a decision.
            rejected_suggestion_ids = (
                _uuid_strings(body, "rejected_suggestion_ids", maximum=1000)
                if "rejected_suggestion_ids" in body
                else ()
            )
            decision = _optional_enum(body, "decision", _DECISIONS)
            decision_outcome = _optional_enum(
                body, "decision_outcome", _DECISION_OUTCOMES
            )
            if (
                (decision == "accept" and decision_outcome != "accepted")
                or (
                    decision == "reject"
                    and decision_outcome != "rejected"
                )
                or (decision is None and decision_outcome is not None)
            ):
                raise ValueError("invalid_decision_outcome")
            command_id = _optional_uuid(body, "command_id")
            command_payload_hash = _optional_sha256(
                body, "command_payload_hash"
            )
            lease_id = _optional_uuid(body, "lease_id")
            actor_id = _uuid(body, "actor_user_id")
            actor_user_id = None if actor_kind == "guest" else actor_id
            actor_guest_identity_id = (
                actor_id if actor_kind == "guest" else None
            )
            persisted = await service.persist_update(
                update=PersistCollaborationUpdate(
                    tenant_id=_bounded_string(
                        body, "tenant_id", maximum=160
                    ),
                    document_id=document_id,
                    generation=_positive_int(body, "generation"),
                    instance_id=_bounded_string(
                        body, "instance_id", maximum=160
                    ),
                    instance_epoch=_positive_int(body, "epoch"),
                    lease_id=lease_id,
                    actor_user_id=actor_user_id,
                    actor_guest_identity_id=actor_guest_identity_id,
                    update_hash=update_hash,
                    update_bytes=update_bytes,
                    actor_kind=cast(
                        Literal[
                            "human", "guest", "assistant", "agent", "system"
                        ],
                        actor_kind,
                    ),
                    change_kind=cast(
                        Literal[
                            "direct", "suggestion", "decision", "system"
                        ],
                        change_kind,
                    ),
                    suggestion_ids=suggestion_ids,
                    change_summary=_change_summary(body),
                    decision_outcome=cast(
                        Literal["accepted", "rejected"] | None,
                        decision_outcome,
                    ),
                    suggestions=suggestions,
                    patches=patches,
                    rejected_suggestion_ids=rejected_suggestion_ids,
                    decision=cast(Literal["accept", "reject"] | None, decision),
                    command_id=command_id,
                    command_payload_hash=command_payload_hash,
                    expected_sequence=_optional_non_negative_int(
                        body, "expected_sequence"
                    ),
                    now=time.time(),
                )
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise
        return {
            "sequence": persisted.sequence,
            "persisted_sequence": persisted.persisted_sequence,
            "duplicate": persisted.duplicate,
        }

    @router.post(
        "/internal/collaboration/documents/{document_id}/updates:lookup"
    )
    async def lookup_updates(document_id: str, request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        try:
            updates = await service.lookup_updates(
                tenant_id=_bounded_string(body, "tenant_id", maximum=160),
                document_id=document_id,
                generation=_positive_int(body, "generation"),
                update_hashes=_sha256_strings(
                    body, "hashes", maximum=1000
                ),
                instance_id=_bounded_string(
                    body, "instance_id", maximum=160
                ),
                epoch=_positive_int(body, "epoch"),
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise
        return {
            "updates": [
                {"hash": update.update_hash, "sequence": update.sequence}
                for update in updates
            ]
        }

    @router.post(
        "/internal/collaboration/documents/{document_id}/commands:lookup"
    )
    async def lookup_command(document_id: str, request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        try:
            persisted = await service.lookup_command(
                tenant_id=_bounded_string(body, "tenant_id", maximum=160),
                document_id=document_id,
                generation=_positive_int(body, "generation"),
                command_id=_uuid(body, "command_id"),
                command_payload_hash=_sha256(body, "command_payload_hash"),
                instance_id=_bounded_string(
                    body, "instance_id", maximum=160
                ),
                epoch=_positive_int(body, "epoch"),
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise
        if persisted is None:
            return {"found": False}
        return {
            "found": True,
            "actor_kind": persisted.actor_kind,
            "actor_user_id": str(persisted.actor_user_id),
            "change_kind": persisted.change_kind,
            "command_id": str(persisted.command_id),
            "command_payload_hash": persisted.command_payload_hash,
            "decision": persisted.decision,
            "generation": persisted.generation,
            "patch_ids": list(persisted.patch_ids),
            "sequence": persisted.sequence,
            "suggestion_ids": list(persisted.suggestion_ids),
            "update_hash": persisted.update_hash,
        }

    @router.post(
        "/internal/collaboration/documents/{document_id}/snapshots",
        status_code=204,
    )
    async def store_snapshot(document_id: str, request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        try:
            tenant_id = _bounded_string(body, "tenant_id", maximum=160)
            state_update = _canonical_base64(body, "state_update_base64")
            state_hash = _sha256(body, "state_hash")
            if not hmac.compare_digest(
                hashlib.sha256(state_update).hexdigest(), state_hash
            ):
                raise ValueError("state_hash_mismatch")
            snapshot = CollaborationSnapshot(
                document_id=document_id,
                tenant_id=tenant_id,
                generation=_positive_int(body, "generation"),
                covered_sequence=_non_negative_int(body, "covered_sequence"),
                state_update=state_update,
                state_vector=_canonical_base64(body, "state_vector_base64"),
                state_hash=state_hash,
                projection_hash=_sha256(body, "projection_hash"),
                schema_version=_positive_int(body, "schema_version"),
                schema_hash=_sha256(body, "schema_hash"),
                created_at=time.time(),
            )
            projection_markdown = _bounded_text(
                body,
                "projection_markdown",
                maximum_bytes=(
                    container.settings.collaboration.max_document_bytes
                ),
            )
            if not hmac.compare_digest(
                hashlib.sha256(projection_markdown.encode("utf-8")).hexdigest(),
                snapshot.projection_hash,
            ):
                raise ValueError("projection_hash_mismatch")
            await service.store_snapshot(
                snapshot=snapshot,
                projection_markdown=projection_markdown,
                instance_id=_bounded_string(
                    body, "instance_id", maximum=160
                ),
                epoch=_positive_int(body, "epoch"),
                tenant_id=tenant_id,
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise
        return Response(status_code=204)

    @router.get("/internal/collaboration/policy-events")
    async def policy_events(request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        try:
            raw_cursor = request.query_params.get("after_id", "0")
            raw_limit = request.query_params.get("limit", "500")
            cursor = int(raw_cursor)
            limit = int(raw_limit)
            if cursor < 0 or limit < 1 or limit > 500:
                raise ValueError("invalid_policy_event_cursor")
            return await service.policy_events(
                tenant_id=_bounded_query_string(
                    request, "tenant_id", maximum=160
                ),
                cursor=cursor,
                limit=limit,
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise


    @router.post("/internal/collaboration/maintenance:compact")
    async def compact(request: Request):
        denied = _require_internal_secret(request, secret)
        if denied is not None:
            return denied
        body = await _json_object(request)
        if body is None:
            return _invalid("invalid_json")
        document_id = body.get("document_id")
        if document_id is not None and (
            not isinstance(document_id, str)
            or not document_id
            or len(document_id) > 128
        ):
            return _invalid("invalid_document_id")
        generation = body.get("generation")
        if generation is not None:
            try:
                generation = _positive_int(body, "generation")
            except ValueError as exc:
                return _invalid(str(exc))
        try:
            return await service.run_maintenance(
                tenant_id=_bounded_string(body, "tenant_id", maximum=160),
                document_id=document_id,
                generation=generation,
                instance_id=_bounded_string(body, "instance_id", maximum=160),
                epoch=_positive_int(body, "epoch"),
            )
        except Exception as exc:
            response = _internal_error(exc)
            if response is not None:
                return response
            raise

    return router


def _require_internal_secret(request: Request, secret: str):
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {secret}"
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        log.warning("Collaboration internal API authentication was rejected.")
        return error_response(
            401,
            "Internal authentication failed.",
            "authentication_error",
            reason="internal_auth_failed",
        )
    return None


def _internal_error(exc: Exception):
    if isinstance(exc, CollaborationDocumentNotFound):
        return error_response(404, "Document not found.", "not_found", reason="not_found")
    if isinstance(exc, CollaborationLeaseInvalid):
        reason = exc.reason
        status = 403 if reason == "access_revoked" else 401
        return error_response(status, "Lease rejected.", "authentication_error", reason=reason)
    if isinstance(exc, CollaborationAuthenticationRequired):
        return error_response(
            401,
            "Lease rejected.",
            "authentication_error",
            reason="lease_invalid",
        )
    if isinstance(exc, CollaborationInstanceFenced):
        return error_response(
            409,
            "Collaboration instance is fenced.",
            "conflict",
            reason="instance_fenced",
        )
    if isinstance(exc, (CollaborationConflict, CollaborationProtocolConflict)):
        reason = getattr(exc, "reason", str(exc))
        return error_response(
            409,
            "Collaboration state conflict.",
            "conflict",
            reason=reason,
            current_sequence=getattr(exc, "current_sequence", None),
        )
    if isinstance(exc, CollaborationDocumentTooLarge):
        return error_response(
            413,
            "Collaboration payload is too large.",
            "payload_too_large",
            reason="payload_too_large",
        )
    if isinstance(exc, (ValueError, TypeError, binascii.Error)):
        reason = str(exc) if str(exc) else "invalid_request"
        return _invalid(reason)
    return None


def _invalid(reason: str):
    return error_response(
        400,
        "Invalid collaboration request.",
        "invalid_request_error",
        reason=reason,
    )


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _instance_payload(lease: Any) -> dict[str, Any]:
    return {
        "instance_id": lease.instance_id,
        "epoch": lease.epoch,
        "lease_expires_at": lease.lease_expires_at,
    }


def _snapshot_candidate_payload(snapshot: Any, updates: Any) -> dict[str, Any]:
    return {
        "covered_sequence": snapshot.covered_sequence,
        "state_update_base64": _base64(snapshot.state_update),
        "state_vector_base64": _base64(snapshot.state_vector),
        "state_hash": snapshot.state_hash,
        "updates": [
            {
                "sequence": update.sequence,
                "update_hash": update.update_hash,
                "update_base64": _base64(cast(bytes, update.update_bytes)),
            }
            for update in updates
        ],
    }


def _bounded_string(
    body: dict[str, Any], field: str, *, maximum: int
) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"invalid_{field}")
    return value


def _bounded_query_string(
    request: Request, field: str, *, maximum: int
) -> str:
    value = request.query_params.get(field)
    if value is None:
        raise ValueError(f"invalid_{field}")
    return _bounded_string({field: value}, field, maximum=maximum)


def _bounded_text(
    body: dict[str, Any], field: str, *, maximum_bytes: int
) -> str:
    value = body.get(field)
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"invalid_{field}")
    return value


def _positive_int(body: dict[str, Any], field: str) -> int:
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"invalid_{field}")
    return value


def _non_negative_int(body: dict[str, Any], field: str) -> int:
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid_{field}")
    return value


def _optional_non_negative_int(
    body: dict[str, Any], field: str
) -> int | None:
    if body.get(field) is None:
        return None
    return _non_negative_int(body, field)


def _positive_query_int(request: Request, field: str) -> int:
    value = request.query_params.get(field)
    try:
        parsed = int(value) if value is not None else 0
    except ValueError as exc:
        raise ValueError(f"invalid_{field}") from exc
    if parsed < 1:
        raise ValueError(f"invalid_{field}")
    return parsed


def _positive_number(body: dict[str, Any], field: str) -> float:
    value = body.get(field)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"invalid_{field}")
    return float(value)


def _non_negative_number(body: dict[str, Any], field: str) -> float:
    value = body.get(field)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"invalid_{field}")
    return float(value)


def _uuid(body: dict[str, Any], field: str) -> uuid.UUID:
    value = body.get(field)
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid_{field}") from exc


def _optional_uuid(body: dict[str, Any], field: str) -> uuid.UUID | None:
    if body.get(field) is None:
        return None
    return _uuid(body, field)


def _uuid_strings(
    body: dict[str, Any], field: str, *, maximum: int
) -> tuple[str, ...]:
    value = body.get(field)
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"invalid_{field}")
    parsed = tuple(str(_uuid({field: item}, field)) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"duplicate_{field}")
    return parsed


def _enum(
    body: dict[str, Any], field: str, allowed: frozenset[str]
) -> str:
    value = body.get(field)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"invalid_{field}")
    return value


def _optional_enum(
    body: dict[str, Any], field: str, allowed: frozenset[str]
) -> str | None:
    if body.get(field) is None:
        return None
    return _enum(body, field, allowed)


def _suggestions(
    body: dict[str, Any],
) -> tuple[CollaborationSuggestion, ...]:
    raw = body.get("suggestions")
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("invalid_suggestions")
    suggestions: list[CollaborationSuggestion] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid_suggestions")
        suggestions.append(
            CollaborationSuggestion(
                suggestion_id=str(_uuid(item, "suggestion_id")),
                patch_id=str(_uuid(item, "patch_id")),
                author_id=_uuid(item, "author_id"),
                created_at=_non_negative_number(item, "created_at"),
                kind=cast(
                    Literal[
                        "insertion",
                        "deletion",
                        "replacement",
                        "format",
                        "structure",
                    ],
                    _enum(item, "kind", _SUGGESTION_KINDS),
                ),
            )
        )
    ids = [item.suggestion_id for item in suggestions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_suggestions")
    return tuple(suggestions)


def _patch_states(
    body: dict[str, Any],
) -> tuple[CollaborationPatchState, ...]:
    raw = body.get("patches")
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("invalid_patches")
    patches: list[CollaborationPatchState] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid_patches")
        kinds_raw = item.get("kinds")
        if not isinstance(kinds_raw, list) or len(kinds_raw) > 5:
            raise ValueError("invalid_patch_kinds")
        kinds = tuple(
            cast(
                Literal[
                    "insertion",
                    "deletion",
                    "replacement",
                    "format",
                    "structure",
                ],
                _enum({"kind": kind}, "kind", _SUGGESTION_KINDS),
            )
            for kind in kinds_raw
        )
        if len(kinds) != len(set(kinds)):
            raise ValueError("duplicate_patch_kinds")
        patches.append(
            CollaborationPatchState(
                patch_id=str(_uuid(item, "patch_id")),
                author_id=_uuid(item, "author_id"),
                created_at=_non_negative_number(item, "created_at"),
                active_suggestion_ids=_uuid_strings(
                    item, "active_suggestion_ids", maximum=1000
                ),
                kinds=kinds,
                superseded_suggestion_ids=(
                    _uuid_strings(
                        item,
                        "superseded_suggestion_ids",
                        maximum=1,
                    )
                    if "superseded_suggestion_ids" in item
                    else ()
                ),
            )
        )
    ids = [item.patch_id for item in patches]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_patches")
    return tuple(patches)


def _change_summary(body: dict[str, Any]) -> dict[str, Any]:
    """Check the display summary for shape and size -- never for characters.

    ``before`` and ``after`` are verbatim user text: the sidecar extracts
    them with ``textBetween`` from the document's text nodes, so the string
    carries no markup at all. A character rule here would therefore reject
    keyboard input, not markup -- and because the rejection lands as HTTP
    400 on the write path, it would cost the change itself rather than the
    summary, and with it the document for every participant.

    A rule against ``<`` and ``>`` would protect nothing: the text carries no
    markup, so such a rule could only reject what a person typed, such as
    ``x < y``. The only sink is a React text child, which escapes anyway.

    What remains carries the whole safeguard: type, at most three entries,
    160 characters each, ``position`` not negative, ``kind`` from a closed
    set.

    ``changes_document`` says whether the update changed what a reader sees;
    the activity history leaves out a row that did not. A sidecar that
    predates the field never sends it, and the summary then claims nothing.
    """
    value = body.get("change_summary")
    if not isinstance(value, dict):
        raise ValueError("invalid_change_summary")
    reports_change = "changes_document" in value
    if reports_change and not isinstance(value["changes_document"], bool):
        raise ValueError("invalid_change_summary")
    raw_edits = value.get("edits")
    omitted = value.get("omitted_edit_count")
    if (
        not isinstance(raw_edits, list)
        or len(raw_edits) > 3
        or not isinstance(omitted, int)
        or isinstance(omitted, bool)
        or omitted < 0
    ):
        raise ValueError("invalid_change_summary")
    edits: list[dict[str, Any]] = []
    allowed_kinds = _SUGGESTION_KINDS | {"direct"}
    for item in raw_edits:
        if not isinstance(item, dict):
            raise ValueError("invalid_change_summary")
        before = item.get("before")
        after = item.get("after")
        position = item.get("position")
        kind = item.get("kind")
        if (
            not isinstance(before, str)
            or len(before) > 160
            or not isinstance(after, str)
            or len(after) > 160
            or not isinstance(position, int)
            or isinstance(position, bool)
            or position < 0
            or not isinstance(kind, str)
            or kind not in allowed_kinds
        ):
            raise ValueError("invalid_change_summary")
        edits.append(
            {
                "before": before,
                "after": after,
                "kind": kind,
                "position": position,
            }
        )
    summary: dict[str, Any] = {"edits": edits, "omitted_edit_count": omitted}
    if reports_change:
        summary["changes_document"] = value["changes_document"]
    return summary


def _sha256(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"invalid_{field}")
    return value


def _optional_sha256(body: dict[str, Any], field: str) -> str | None:
    if body.get(field) is None:
        return None
    return _sha256(body, field)


def _sha256_strings(
    body: dict[str, Any], field: str, *, maximum: int
) -> tuple[str, ...]:
    value = body.get(field)
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"invalid_{field}")
    parsed = tuple(_sha256({field: item}, field) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"duplicate_{field}")
    return parsed


def _canonical_base64(body: dict[str, Any], field: str) -> bytes:
    value = body.get(field)
    if not isinstance(value, str) or len(value) > 32 * 1_048_576:
        raise ValueError(f"invalid_{field}")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid_{field}") from exc
    if _base64(decoded) != value:
        raise ValueError(f"non_canonical_{field}")
    return decoded


def _base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
