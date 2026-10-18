"""Tests for the curated model-card catalogue and its alias resolver.

These lock in the cross-provider identifier behaviour (the same logical model
under Anthropic / Bedrock region+version identifiers resolves to one card),
the honest "unknown model -> None" contract, and basic catalogue hygiene
(self-referential aliases, sourced data, valid reasoning levels).
"""

from __future__ import annotations

import pytest

from inqtrix.model_cards import (
    MODEL_CARDS,
    ModelCard,
    build_models_catalog,
    resolve_model_card,
)

_ALLOWED_EFFORT = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def _card(card_id: str) -> ModelCard:
    card = resolve_model_card(card_id)
    assert card is not None, f"expected a card for {card_id!r}"
    return card


def test_anthropic_id_resolves() -> None:
    """The first-party Anthropic id resolves to its card."""
    assert _card("claude-opus-4-8").display_name == "Claude Opus 4.8"


def test_bedrock_region_and_version_resolve_to_same_card() -> None:
    """Bedrock cross-region + version variants resolve to the same logical card."""
    base = _card("claude-opus-4-8")
    for variant in (
        "anthropic.claude-opus-4-8",
        "eu.anthropic.claude-opus-4-8-v1:0",
        "us.anthropic.claude-opus-4-8",
    ):
        assert resolve_model_card(variant) is base, variant


def test_haiku_dated_and_bedrock_aliases_resolve() -> None:
    """Haiku's dated id and region-prefixed Bedrock id both resolve to one card."""
    base = _card("claude-haiku-4-5")
    assert resolve_model_card("claude-haiku-4-5-20251001") is base
    assert resolve_model_card("eu.anthropic.claude-haiku-4-5-20251001-v1:0") is base


def test_litellm_prefixed_openai_id_resolves() -> None:
    """A LiteLLM ``provider/model`` identifier resolves via the alias list."""
    assert resolve_model_card("openai/gpt-5.4").id == "gpt-5.4"


@pytest.mark.parametrize("unknown", ["", "   ", "totally-made-up-model", "gpt-9.9"])
def test_unknown_model_returns_none(unknown: str) -> None:
    """Unknown identifiers return None -- the honest 'no card' signal."""
    assert resolve_model_card(unknown) is None


@pytest.mark.parametrize("card", MODEL_CARDS, ids=lambda c: c.id)
def test_card_id_is_in_its_own_aliases(card: ModelCard) -> None:
    """Every card lists its own canonical id among its aliases (self-resolving)."""
    assert card.id in card.aliases
    assert resolve_model_card(card.id) is card


@pytest.mark.parametrize("card", MODEL_CARDS, ids=lambda c: c.id)
def test_card_data_is_sourced(card: ModelCard) -> None:
    """Each card records provenance so the curated data stays auditable."""
    assert card.source_url, card.id
    assert card.last_verified, card.id
    assert card.context_window_tokens > 0
    assert card.max_output_tokens > 0


@pytest.mark.parametrize("card", MODEL_CARDS, ids=lambda c: c.id)
def test_reasoning_levels_use_known_tokens(card: ModelCard) -> None:
    """Reasoning levels are drawn from the known effort vocabulary."""
    assert set(card.reasoning_levels) <= _ALLOWED_EFFORT, card.id


def test_catalogue_covers_the_seeded_models() -> None:
    """The seeded models the deployment ships with are all present."""
    ids = {card.id for card in MODEL_CARDS}
    expected = {
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-4-6", "claude-haiku-4-5",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5-pro", "gpt-5.5", "gpt-5.4-pro", "gpt-5.4",
        "gpt-5.4-mini", "gpt-5.4-nano",
        "gpt-5.1", "gpt-5-mini", "gpt-5-nano", "gpt-4.1",
        "gemini-3.1-pro",
    }
    assert expected <= ids


def test_haiku_has_no_reasoning_levels() -> None:
    """Haiku rejects the effort parameter, so its card exposes no levels."""
    assert _card("claude-haiku-4-5").reasoning_levels == []


def test_build_models_catalog_resolves_and_marks_unknown() -> None:
    """The catalogue builder serialises known cards and flags unknown ids."""
    catalog = build_models_catalog(["claude-opus-4-8", "totally-unknown"])
    assert catalog[0]["model_id"] == "claude-opus-4-8"
    assert catalog[0]["card"]["display_name"] == "Claude Opus 4.8"
    assert catalog[1] == {"model_id": "totally-unknown", "card": None}


def test_build_models_catalog_empty_for_no_selectable_models() -> None:
    """No selectable models -> empty catalogue (UI falls back to tier picker)."""
    assert build_models_catalog([]) == []
