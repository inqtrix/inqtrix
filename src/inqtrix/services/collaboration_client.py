"""Narrow authenticated HTTP client for the private collaboration service."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from inqtrix.observability.context import current_log_context

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
log = logging.getLogger("inqtrix")


class CollaborationServiceUnavailable(RuntimeError):
    """Raised when the optional Node service is unreachable or unready."""


class CollaborationNodeConflict(RuntimeError):
    """Raised when Node rejects a schema, sequence, or decision contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CollaborationConversion:
    """Side-effect-free conversion result used by the activation transaction."""

    schema_hash: str
    state_update: bytes
    state_vector: bytes
    state_hash: str
    projection_markdown: str
    projection_hash: str


@dataclass(frozen=True)
class CollaborationProjection:
    """Canonical Markdown projection after Node drains a document queue."""

    generation: int
    sequence: int
    markdown: str
    projection_hash: str
    schema_hash: str
    authoritative_sequence: int | None = None
    state_update: bytes | None = None
    state_vector: bytes | None = None
    state_hash: str | None = None


@dataclass(frozen=True)
class CollaborationDecisionResult:
    """Durable result of an accept/reject command executed by Node."""

    command_id: uuid.UUID
    sequence: int
    suggestion_ids: tuple[str, ...]


@dataclass(frozen=True)
class CollaborationSuggestionResult:
    """Durable result of publishing one private AI patch into Yjs."""

    command_id: uuid.UUID
    patch_id: str
    sequence: int
    suggestion_ids: tuple[str, ...]


class CollaborationNodeClient:
    """Constructor-configured client; secrets never enter URLs or logs.

    The pooled ``httpx.AsyncClient`` keeps its keep-alive connections bound
    to the event loop that first used it. Synchronous callers such as kernel
    tool threads run every request on a private, short-lived loop
    (``sync_bridge.run_coro_sync``), and several tool calls of one model turn
    execute concurrently. A pooled connection created on one such loop fails
    with ``RuntimeError: Event loop is closed`` when a later loop reuses it.
    The pool is therefore owned by the loop that first used it; a request
    from any other loop uses a one-shot client that closes with the request.
    """

    def __init__(
        self,
        *,
        base_url: str,
        secret: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._client = self._new_client()
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=dict(self._headers),
            timeout=self._timeout_seconds,
            transport=self._transport,
        )

    @contextlib.asynccontextmanager
    async def _client_for_current_loop(self) -> AsyncIterator[httpx.AsyncClient]:
        """The pooled client on its owning loop, a one-shot client elsewhere."""
        loop = asyncio.get_running_loop()
        if self._owner_loop is None and not self._client.is_closed:
            self._owner_loop = loop
        if loop is self._owner_loop and not self._client.is_closed:
            yield self._client
            return
        async with self._new_client() as client:
            yield client

    async def available(self) -> bool:
        """Whether Node currently reports its fenced instance as ready."""
        try:
            async with self._client_for_current_loop() as client:
                response = await client.get("/health/ready")
        except httpx.HTTPError:
            log.warning("Collaboration service readiness request failed.")
            return False
        if response.status_code != 200:
            log.warning(
                "Collaboration service is not ready (status=%d).",
                response.status_code,
            )
        return response.status_code == 200

    async def convert(
        self,
        *,
        document_id: str,
        markdown: str,
        schema_version: int,
        max_document_bytes: int,
    ) -> CollaborationConversion:
        payload = await self._post(
            "/internal/convert",
            {
                "document_id": document_id,
                "markdown": markdown,
                "schema_version": schema_version,
                "max_document_bytes": max_document_bytes,
            },
        )
        snapshot = _object(payload.get("snapshot"), "snapshot")
        return CollaborationConversion(
            schema_hash=_hash(payload.get("schema_hash"), "schema_hash"),
            state_update=_bytes(
                snapshot.get("state_update_base64"), "state_update_base64"
            ),
            state_vector=_bytes(
                snapshot.get("state_vector_base64"), "state_vector_base64"
            ),
            state_hash=_hash(snapshot.get("state_hash"), "state_hash"),
            projection_markdown=_string(
                payload.get("projection_markdown"), "projection_markdown"
            ),
            projection_hash=_hash(
                payload.get("projection_hash"), "projection_hash"
            ),
        )

    async def project(
        self,
        *,
        document_id: str,
        generation: int,
        minimum_sequence: int,
    ) -> CollaborationProjection:
        payload = await self._post(
            f"/internal/documents/{document_id}/project",
            {
                "generation": generation,
                "minimum_sequence": minimum_sequence,
            },
        )
        snapshot_value = payload.get("snapshot")
        snapshot = (
            _object(snapshot_value, "snapshot")
            if snapshot_value is not None
            else None
        )
        return CollaborationProjection(
            generation=_positive_int(payload.get("generation"), "generation"),
            sequence=_non_negative_int(payload.get("sequence"), "sequence"),
            markdown=_string(
                payload.get("projection_markdown"), "projection_markdown"
            ),
            projection_hash=_hash(
                payload.get("projection_hash"), "projection_hash"
            ),
            schema_hash=_hash(payload.get("schema_hash"), "schema_hash"),
            state_update=(
                _bytes(snapshot.get("state_update_base64"), "state_update_base64")
                if snapshot is not None
                else None
            ),
            state_vector=(
                _bytes(snapshot.get("state_vector_base64"), "state_vector_base64")
                if snapshot is not None
                else None
            ),
            state_hash=(
                _hash(snapshot.get("state_hash"), "state_hash")
                if snapshot is not None
                else None
            ),
        )

    async def decide(
        self,
        *,
        document_id: str,
        generation: int,
        expected_sequence: int,
        command_id: uuid.UUID,
        patch_ids: tuple[str, ...],
        decision: Literal["accept", "reject"],
        actor_user_id: uuid.UUID,
    ) -> CollaborationDecisionResult:
        payload = await self._post(
            f"/internal/documents/{document_id}/decisions",
            {
                "generation": generation,
                "expected_sequence": expected_sequence,
                "command_id": str(command_id),
                "patch_ids": list(patch_ids),
                "decision": decision,
                "actor_user_id": str(actor_user_id),
            },
        )
        returned_command = uuid.UUID(
            _string(payload.get("command_id"), "command_id")
        )
        if returned_command != command_id:
            raise CollaborationServiceUnavailable(
                "collaboration service returned the wrong command id"
            )
        return CollaborationDecisionResult(
            command_id=returned_command,
            sequence=_positive_int(payload.get("sequence"), "sequence"),
            suggestion_ids=tuple(
                _string(value, "suggestion_id")
                for value in _array(payload.get("suggestion_ids"), "suggestion_ids")
            ),
        )

    async def publish_suggestion(
        self,
        *,
        document_id: str,
        generation: int,
        expected_sequence: int,
        command_id: uuid.UUID,
        patch_id: str,
        actor_kind: Literal["assistant", "agent"],
        actor_user_id: uuid.UUID,
        target_markdown: str,
        base_markdown: str | None = None,
    ) -> CollaborationSuggestionResult:
        """Publish a canonical target as a tracked shared suggestion.

        ``base_markdown`` travels only when given: without it the service
        keeps the exact sequence rule for the publication.
        """
        body: dict[str, Any] = {
            "generation": generation,
            "expected_sequence": expected_sequence,
            "command_id": str(command_id),
            "patch_id": patch_id,
            "actor_kind": actor_kind,
            "actor_user_id": str(actor_user_id),
            "target_markdown": target_markdown,
        }
        if base_markdown is not None:
            body["base_markdown"] = base_markdown
        payload = await self._post(
            f"/internal/documents/{document_id}/suggestions",
            body,
        )
        returned_command = uuid.UUID(
            _string(payload.get("command_id"), "command_id")
        )
        returned_patch = _string(payload.get("patch_id"), "patch_id")
        if returned_command != command_id or returned_patch != patch_id:
            raise CollaborationServiceUnavailable(
                "collaboration service returned the wrong suggestion identity"
            )
        return CollaborationSuggestionResult(
            command_id=returned_command,
            patch_id=returned_patch,
            sequence=_positive_int(payload.get("sequence"), "sequence"),
            suggestion_ids=tuple(
                _string(value, "suggestion_id")
                for value in _array(payload.get("suggestion_ids"), "suggestion_ids")
            ),
        )

    async def aclose(self) -> None:
        """Close the pooled internal HTTP client."""
        if self._owner_loop is not None and self._owner_loop.is_closed():
            # The owning loop is gone, and its keep-alive sockets went with
            # it; closing the pool would need that loop.
            log.debug("Collaboration client pool belonged to a closed event loop.")
            return
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._client_for_current_loop() as client:
                response = await client.post(
                    path, json=body, headers=_correlation_headers()
                )
        except httpx.HTTPError as exc:
            raise CollaborationServiceUnavailable(
                "collaboration service is unreachable"
            ) from exc
        if response.status_code == 409:
            raise CollaborationNodeConflict(_response_reason(response))
        if response.status_code >= 500:
            raise CollaborationServiceUnavailable(
                "collaboration service is not ready"
            )
        if response.status_code >= 400:
            raise CollaborationNodeConflict(_response_reason(response))
        try:
            return _object(response.json(), "response")
        except ValueError as exc:
            raise CollaborationServiceUnavailable(
                "collaboration service returned invalid JSON"
            ) from exc


def _correlation_headers() -> dict[str, str]:
    """Die Korrelations-Id dieser Anfrage an den Knoten weiterreichen.

    Ein Klick erzeugt Logzeilen an mehreren Stellen. Ohne mitgereichte Id
    lassen sie sich nur ueber geratene Zeitstempel verbinden -- bei zwei
    gleichzeitigen Nutzern also gar nicht.

    Dieser Kopf verbindet ZWEI Stationen: die Zeile des Gateways und die
    Ablehnungszeile des Sidecars (``correlationField`` in dessen
    ``httpRouter``). Die dritte -- der Rueckweg Sidecar -> interne API, auf
    dem der Ablehnungsgrund ueberhaupt entsteht -- bleibt ausdruecklich
    UNVERBUNDEN: der Kopfsatz jenes Aufrufs ist im Sidecar fest verdrahtet,
    und ihn durchzureichen hiesse, die Id durch drei Schichten zu faedeln
    oder einen Kontextspeicher einzufuehren. Der Nutzen deckt das heute
    nicht; die Luecke ist benannt, nicht uebersehen.

    Leer, wenn kein Kontext gebunden ist (Hintergrundaufgaben, Tests): dann
    darf der Knoten seine eigene Id vergeben, statt eine leere zu erben.
    """
    request_id = current_log_context().get("request_id", "")
    return {"X-Request-ID": request_id} if request_id else {}


#: Der Platzhalter, mit dem der Sidecar sagt: "abgelehnt, aber ich habe
#: dafuer kein eigenes Wort". Er ist kein Grund, sondern das Eingestaendnis,
#: keinen zu kennen -- der echte reist in ``upstream_reason`` daneben mit.
_UNNAMED_NODE_CONFLICT = "upstream_conflict"


def _response_reason(response: httpx.Response) -> str:
    """Den Grund lesen, den der Knoten genannt hat -- den genauesten.

    Der Sidecar bildet die rund vierzig Konfliktgruende der internen API auf
    seine kleine Vokabelliste ab, weil die den WebSocket-Schliesscode
    steuert. Was er nicht abbilden kann, heisst bei ihm
    ``upstream_conflict``, und der urspruengliche Name faehrt in
    ``upstream_reason`` mit. Wer nur ``reason`` liest, wirft ihn hier
    endgueltig weg: der Nutzer liest dann "upstream_conflict", waehrend im
    Sidecar-Log "patch_not_found" steht, und niemand kann die beiden
    verbinden.

    Der Ersatzname gewinnt bewusst NUR gegen den Platzhalter. Bildet der
    Sidecar auf einen eigenen Grund ab (etwa ``sequence_conflict``), traegt
    dieser eine eigene Behandlung und bleibt massgeblich -- auch wenn
    daneben ein feinerer Name steht.
    """
    try:
        payload = _object(response.json(), "response")
    except ValueError:
        return "node_rejected"
    error = payload.get("error")
    if isinstance(error, dict):
        reason = error.get("reason")
        upstream = error.get("upstream_reason")
        if (
            reason == _UNNAMED_NODE_CONFLICT
            and isinstance(upstream, str)
            and upstream
        ):
            return upstream
        if isinstance(reason, str):
            return reason
    if isinstance(payload.get("reason"), str):
        return payload["reason"]
    return "node_rejected"


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _hash(value: Any, field: str) -> str:
    text = _string(value, field)
    if not _SHA256_PATTERN.fullmatch(text):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text


def _bytes(value: Any, field: str) -> bytes:
    text = _string(value, field)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field} must be canonical base64") from exc


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value
