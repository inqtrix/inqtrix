"""Embedding-model catalog: the knowledge-side sibling of model_cards.

Mirrors :mod:`inqtrix.model_cards` for embedding models: a hand-
maintained catalog of cards plus ``build_embedding_catalog`` that
resolves a provider's ``selectable_embedding_models`` list into
card-annotated entries for the React picker.

The decisive difference from chat/editor model selection: an embedding
model is chosen ONCE per knowledge collection and is immutable after
creation — it fixes the vector dimension of every chunk. The UI shows
this catalog at collection creation; switching later means re-embedding
the whole collection, never silently mixing dimensions.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingCard(BaseModel):
    """Operator/UI-facing fact sheet for one embedding model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(
        ...,
        description=(
            "Canonical model identifier as sent to the embeddings "
            "endpoint and stored immutably on a collection."
        ),
    )
    """Canonical model identifier as sent to the embeddings endpoint and stored immutably on a collection."""
    display_name: str = Field(
        ...,
        description="Human-readable name shown in the picker.",
    )
    """Human-readable name shown in the picker."""
    vendor: str = Field(
        ...,
        description="Provider label (OpenAI, Voyage, BAAI, ...).",
    )
    """Provider label (OpenAI, Voyage, BAAI, ...)."""
    dims: int = Field(
        ...,
        gt=0,
        description=(
            "Vector dimension the model produces. Becomes the "
            "collection's immutable ``embedding_dim``; every chunk "
            "upsert is validated against it."
        ),
    )
    """Vector dimension the model produces. Becomes the collection's immutable ``embedding_dim``; every chunk upsert is validated against it."""
    max_input_tokens: int = Field(
        ...,
        gt=0,
        description=(
            "Maximum input length per embedding call. The chunker must "
            "stay below this — overlong chunks are split, never "
            "silently truncated by the provider."
        ),
    )
    """Maximum input length per embedding call. The chunker must stay below this — overlong chunks are split, never silently truncated by the provider."""
    multilingual: bool = Field(
        ...,
        description=(
            "Whether the model is built for multilingual retrieval. "
            "Load-bearing for Inqtrix's German-first corpus."
        ),
    )
    """Whether the model is built for multilingual retrieval. Load-bearing for Inqtrix's German-first corpus."""
    pricing_input_per_mtok: float | None = Field(
        None,
        description=(
            "USD per million input tokens, ``None`` for self-hosted/"
            "open-weight models where cost is hardware-bound."
        ),
    )
    """USD per million input tokens, ``None`` for self-hosted/open-weight models where cost is hardware-bound."""
    source_url: str = Field(
        ...,
        description="Documentation URL the card facts were taken from.",
    )
    """Documentation URL the card facts were taken from."""
    last_verified: str = Field(
        ...,
        description="``YYYY-MM`` the card facts were last checked.",
    )
    """``YYYY-MM`` the card facts were last checked."""


EMBEDDING_CARDS: tuple[EmbeddingCard, ...] = (
    EmbeddingCard(
        id="text-embedding-3-small",
        display_name="OpenAI Text Embedding 3 Small",
        vendor="OpenAI",
        dims=1536,
        max_input_tokens=8191,
        multilingual=True,
        pricing_input_per_mtok=0.02,
        source_url="https://platform.openai.com/docs/guides/embeddings",
        last_verified="2026-06",
    ),
    EmbeddingCard(
        id="text-embedding-3-large",
        display_name="OpenAI Text Embedding 3 Large",
        vendor="OpenAI",
        dims=3072,
        max_input_tokens=8191,
        multilingual=True,
        pricing_input_per_mtok=0.13,
        source_url="https://platform.openai.com/docs/guides/embeddings",
        last_verified="2026-06",
    ),
    EmbeddingCard(
        id="BAAI/bge-m3",
        display_name="BGE-M3",
        vendor="BAAI",
        dims=1024,
        max_input_tokens=8192,
        multilingual=True,
        pricing_input_per_mtok=None,
        source_url="https://huggingface.co/BAAI/bge-m3",
        last_verified="2026-06",
    ),
    EmbeddingCard(
        id="voyage-3-large",
        display_name="Voyage 3 Large",
        vendor="Voyage AI",
        dims=1024,
        max_input_tokens=32000,
        multilingual=True,
        pricing_input_per_mtok=0.18,
        source_url="https://docs.voyageai.com/docs/embeddings",
        last_verified="2026-06",
    ),
)
"""Hand-maintained embedding catalog (facts re-verified per release)."""

_CARDS_BY_ID: dict[str, EmbeddingCard] = {card.id: card for card in EMBEDDING_CARDS}


def resolve_embedding_card(model_id: str) -> EmbeddingCard | None:
    """Return the card for *model_id*, or ``None`` when uncatalogued."""
    return _CARDS_BY_ID.get(model_id)


def build_embedding_catalog(selectable_models: list[str]) -> list[dict]:
    """Annotate a provider's selectable embedding models with cards.

    Mirrors ``model_cards.build_models_catalog``: unknown ids degrade
    gracefully to ``card: None`` (the UI shows the bare id) instead of
    being dropped — an operator-configured model must never vanish
    silently from the picker.
    """
    catalog: list[dict] = []
    for model_id in selectable_models:
        card = resolve_embedding_card(model_id)
        catalog.append(
            {
                "model_id": model_id,
                "card": card.model_dump(mode="json") if card is not None else None,
            }
        )
    return catalog
