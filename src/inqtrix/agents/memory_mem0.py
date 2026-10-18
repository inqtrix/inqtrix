"""Mem0-backed implementation of the workspace-agent memory provider.

The adapter only translates between Inqtrix records and Mem0 HTTP
endpoints. Authorization, tenant isolation, and namespace derivation stay
in :mod:`inqtrix.services.agent_memory_service` so the security contract
does not depend on provider behaviour.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from inqtrix.agents.memory_ports import (
    AgentMemoryNotFound,
    AgentMemoryRecord,
    AgentMemoryUnavailable,
)

log = logging.getLogger("inqtrix")


class Mem0AgentMemoryProvider:
    """HTTP adapter for a self-hosted Mem0 API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise AgentMemoryUnavailable("Mem0 base URL is not configured")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Token {api_key.strip()}"
        self._http = httpx.AsyncClient(
            base_url=normalized,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            transport=transport,
        )

    async def list_memories(
        self, *, namespace: str, scope: str | None, limit: int
    ) -> list[AgentMemoryRecord]:
        data = await self._post(
            "/v3/memories/",
            json={
                "filters": {"user_id": namespace},
                "page": 1,
                "page_size": limit,
            },
        )
        rows = _extract_rows(data)
        records = [self._record(row) for row in rows]
        if scope is not None:
            records = [row for row in records if row.scope == scope]
        return records[:limit]

    async def recall(
        self, *, namespace: str, query: str, limit: int
    ) -> list[AgentMemoryRecord]:
        data = await self._post(
            "/v3/memories/search/",
            json={
                "query": query,
                "filters": {"user_id": namespace},
                "top_k": limit,
            },
        )
        return [self._record(row) for row in _extract_rows(data)[:limit]]

    async def retain(
        self,
        *,
        namespace: str,
        content: str,
        scope: str,
        category: str,
        confidence: float,
        source_run_id: str,
    ) -> AgentMemoryRecord:
        metadata = _metadata(
            scope=scope,
            category=category,
            confidence=confidence,
            source_run_id=source_run_id,
        )
        data = await self._post(
            "/v3/memories/add/",
            json={
                "messages": [{"role": "user", "content": content}],
                "user_id": namespace,
                "metadata": metadata,
                "infer": False,
            },
        )
        memory_id = str(
            data.get("memory_id")
            or data.get("id")
            or data.get("event_id")
            or ""
        )
        return AgentMemoryRecord(
            memory_id=memory_id,
            scope=scope,
            category=category,
            content=content,
            confidence=confidence,
            source_run_id=source_run_id,
            metadata={
                **metadata,
                **({"mem0_event_id": str(data.get("event_id"))} if data.get("event_id") else {}),
            },
        )

    async def update(
        self,
        *,
        namespace: str,
        memory_id: str,
        content: str,
        scope: str,
        category: str,
    ) -> AgentMemoryRecord:
        await self._assert_owner(namespace=namespace, memory_id=memory_id)
        data = await self._put(
            f"/v1/memories/{memory_id}/",
            json={
                "text": content,
                "metadata": _metadata(
                    scope=scope,
                    category=category,
                    confidence=0.0,
                    source_run_id="",
                ),
            },
        )
        record = self._record(data)
        return AgentMemoryRecord(
            memory_id=record.memory_id or memory_id,
            scope=scope,
            category=category,
            content=record.content or content,
            confidence=record.confidence,
            source_run_id=record.source_run_id,
            metadata=record.metadata,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    async def delete(self, *, namespace: str, memory_id: str) -> None:
        await self._assert_owner(namespace=namespace, memory_id=memory_id)
        await self._delete(f"/v1/memories/{memory_id}/")

    async def clear(self, *, namespace: str, scope: str | None) -> int:
        memories = await self.list_memories(
            namespace=namespace, scope=scope, limit=1000
        )
        for memory in memories:
            if memory.memory_id:
                await self.delete(namespace=namespace, memory_id=memory.memory_id)
        return len(memories)

    async def feedback(
        self,
        *,
        namespace: str,
        memory_id: str,
        feedback: str,
        reason: str,
    ) -> None:
        await self._assert_owner(namespace=namespace, memory_id=memory_id)
        await self._post(
            "/v1/feedback/",
            json={
                "memory_id": memory_id,
                "feedback": feedback,
                "feedback_reason": reason,
            },
        )

    async def _assert_owner(self, *, namespace: str, memory_id: str) -> None:
        data = await self._get(f"/v1/memories/{memory_id}/")
        owner = str(data.get("user_id") or data.get("userId") or "")
        if owner != namespace:
            raise AgentMemoryNotFound(memory_id)

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path)

    async def _post(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, json=json)

    async def _put(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", path, json=json)

    async def _delete(self, path: str) -> dict[str, Any]:
        return await self._request("DELETE", path)

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            log.warning(
                "Agent memory provider unavailable (error_type=%s)",
                type(exc).__name__,
            )
            raise AgentMemoryUnavailable("Memory provider unavailable") from exc
        if response.status_code == 404:
            raise AgentMemoryNotFound(path)
        if response.status_code >= 500:
            log.warning(
                "Agent memory provider returned status=%s for method=%s",
                response.status_code,
                method,
            )
            raise AgentMemoryUnavailable("Memory provider unavailable")
        if response.status_code >= 400:
            raise AgentMemoryUnavailable("Memory provider rejected request")
        if not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {"results": data}

    def _record(self, row: dict[str, Any]) -> AgentMemoryRecord:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        memory = row.get("memory")
        content = row.get("text") or row.get("content") or memory or ""
        return AgentMemoryRecord(
            memory_id=str(row.get("id") or row.get("memory_id") or ""),
            scope=str(metadata.get("inqtrix_scope") or "user"),
            category=str(metadata.get("inqtrix_category") or "project_fact"),
            content=str(content),
            confidence=float(metadata.get("inqtrix_confidence") or 0.0),
            source_run_id=str(metadata.get("inqtrix_source_run_id") or ""),
            metadata=dict(metadata),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )


def _extract_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("results", "memories", "data"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if data.get("id") or data.get("memory_id"):
        return [data]
    return []


def _metadata(
    *,
    scope: str,
    category: str,
    confidence: float,
    source_run_id: str,
) -> dict[str, Any]:
    return {
        "inqtrix_scope": scope,
        "inqtrix_category": category,
        "inqtrix_confidence": confidence,
        "inqtrix_source_run_id": source_run_id,
    }
