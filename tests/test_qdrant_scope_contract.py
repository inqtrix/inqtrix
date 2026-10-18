"""Offline unit tests for ``QdrantKnowledgeStore._resolve_target``.

The port-parity tests in ``test_qdrant_store.py`` are gated on a live Qdrant
server (``INQTRIX_TEST_QDRANT_URL``). ``_resolve_target`` is a PURE method,
though — it reads each collection's ``embedding_model`` and builds a payload
filter via the import-only ``_require_qdrant``/``_scope_filter`` helpers — so its
canonical mixed-embedding contract is verifiable here WITHOUT a server:

* explicit multi-model selection -> hard ``KnowledgeError`` (parity with the
  Postgres and memory stores), NEVER a silently narrowed result set
  (no silent fallbacks);
* explicit single-model selection -> exactly the chosen collections;
* implicit search-all -> narrow to the query model's collections.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# _resolve_target calls _require_qdrant(), which imports qdrant_client — an
# optional dependency (the `knowledge-qdrant` extra), absent from a default
# `uv sync`. Skip cleanly when it is not installed rather than erroring.
pytest.importorskip("qdrant_client")

from inqtrix.knowledge.stores.ports import KnowledgeError
from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore


def _collection(cid: str, model: str) -> SimpleNamespace:
    """A minimal stand-in — ``_resolve_target`` reads only id + embedding_model."""
    return SimpleNamespace(id=cid, embedding_model=model)


def _store(collections: list[SimpleNamespace]) -> QdrantKnowledgeStore:
    """A store shell whose collection accessors return the given fakes.

    Built via ``__new__`` so no live Qdrant connection is opened;
    ``_resolve_target`` reaches only ``_sync_get_collection`` /
    ``_sync_list_collections`` plus the pure ``_require_qdrant`` /
    ``_scope_filter`` helpers.
    """
    by_id = {c.id: c for c in collections}
    store = object.__new__(QdrantKnowledgeStore)
    store._sync_get_collection = lambda cid: by_id[cid]
    store._sync_list_collections = lambda: list(collections)
    return store


def _scoped_ids(scope_filter) -> list[str]:
    """The collection ids the resolved payload filter restricts the search to."""
    return list(scope_filter.must[0].match.any)


def test_explicit_mixed_model_selection_is_a_hard_error():
    """An explicit selection spanning two embedding models must raise, matching
    the Postgres/memory contract — never silently drop the second model."""
    store = _store([_collection("a", "model-x"), _collection("b", "model-y")])
    with pytest.raises(KnowledgeError, match="query one model scope at a time"):
        # retrieval.py passes collection_ids[0]'s model as embedding_model; the
        # old code silently narrowed to {"a"} here instead of failing.
        store._resolve_target(["a", "b"], "model-x")


def test_explicit_single_model_selection_keeps_every_chosen_collection():
    store = _store([_collection("a", "model-x"), _collection("b", "model-x")])
    _name, scope_filter = store._resolve_target(["a", "b"], "model-x")
    assert _scoped_ids(scope_filter) == ["a", "b"]


def test_implicit_search_all_narrows_to_the_query_model():
    """No explicit collection_ids: the vector index is per-model, so narrowing to
    the query model's collections is correct (parity with Postgres _resolve_scope)."""
    store = _store([_collection("a", "model-x"), _collection("b", "model-y")])
    _name, scope_filter = store._resolve_target(None, "model-x")
    assert _scoped_ids(scope_filter) == ["a"]


def test_explicit_empty_scope_is_not_reinterpreted_as_search_all():
    store = _store([_collection("a", "model-x")])

    _name, empty_filter = store._resolve_target([], "model-x")
    _name, implicit_filter = store._resolve_target(None, "model-x")

    assert empty_filter is None
    assert _scoped_ids(implicit_filter) == ["a"]
