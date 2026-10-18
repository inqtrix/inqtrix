"""Citable source view for knowledge documents (``/v1/sources/*``).

The target of HTTP knowledge citations: when
``INQTRIX_PUBLIC_BASE_URL`` is configured, ``mode=knowledge`` answers
reference ``<base>/v1/sources/<document_id>?chunk=N`` instead of the
internal ``inqtrix://`` URI scheme, so references in exported reports
are clickable. Registered only alongside the knowledge surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.knowledge.stores.ports import DocumentNotFound
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the source-view route against the container.

    Raises:
        RuntimeError: When called without a wired knowledge service —
            registration is a composition decision, not a runtime
            fallback.
    """
    service = container.knowledge_service
    if service is None:
        raise RuntimeError(
            "build_router(sources) requires a wired knowledge service; "
            "register the router only when knowledge is enabled."
        )
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency
    router = APIRouter()

    @router.get("/v1/sources/{document_id}")
    async def get_source(
        document_id: str,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Return the citable document view (full text + provenance).

        The optional ``chunk`` query parameter in citation URLs is a
        client-side anchor; the server always returns the whole
        document so the UI can highlight the cited passage.

        Visibility is the parent collection's: without ownership or an
        accepted share grant the document is a 404, indistinguishable
        from absence.
        """
        try:
            document = await service.get_document(
                document_id,
                visible_to=visible_to,
            )
        except DocumentNotFound:
            return error_response(404, "Source not found", "not_found")
        return {
            "id": document.id,
            "collection_id": document.collection_id,
            "title": document.title,
            "text": document.text,
            "metadata": dict(document.metadata),
            "chunk_count": document.chunk_count,
            "created_at": document.created_at,
        }

    return router
