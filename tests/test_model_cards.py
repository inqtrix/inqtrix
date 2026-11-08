"""Tests for the curated model-card catalogue and its alias resolver.

These lock in the cross-provider identifier behaviour (the same logical model
under Anthropic / Bedrock region+version identifiers resolves to one card),
the honest "unknown model -> None" contract, and basic catalogue hygiene
(self-referential aliases, sourced data, valid reasoning levels).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.constants import CONTEXT_WINDOW_SAFETY_TOKENS, DEFAULT_LLM_MAX_OUTPUT_TOKENS
from inqtrix.model_cards import (
    MODEL_CARDS,
    ModelCard,
    build_models_catalog,
    estimate_required_context_tokens,
    llm_context_window_tokens,
    llm_default_output_tokens,
    resolve_model_card,
)
from inqtrix.usage.models import usage_cost_usd

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


@pytest.mark.parametrize(
    ("bedrock_id", "card_id"),
    [
        ("global.anthropic.claude-haiku-5-5", "claude-haiku-5-5"),
        ("jp.anthropic.claude-haiku-5-5", "claude-haiku-5-5"),
        ("au.anthropic.claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("ca.anthropic.claude-opus-5-5", "claude-opus-5-5"),
    ],
)
def test_bedrock_geographic_and_global_profiles_resolve(bedrock_id: str, card_id: str) -> None:
    """A Claude id behind any Bedrock inference profile resolves to its card.

    Without the card the Bedrock adapter loses the model's request rules, the
    usage ledger cannot price the call and the provider gets no context window.
    """
    assert resolve_model_card(bedrock_id) is _card(card_id)


@pytest.mark.parametrize(
    ("model_id", "price"),
    [
        ("gpt-6.1-terra", (1.0, 5.0)),
        ("gpt-6-luna", (0.1, 0.5)),
        ("openai/gpt-6.1-terra", (1.0, 5.0)),
    ],
)
def test_gpt6_deployments_resolve_with_window_and_price(
    model_id: str, price: tuple[float, float]
) -> None:
    """An Azure deployment named after the model id, and the LiteLLM id, find the GPT-6 card.

    The card sizes the provider's context window and makes the call billable;
    without it the editor falls back to a small window and the tokens stay unpriced.
    """
    card = _card(model_id)
    assert card.context_window_tokens == 1_050_000
    assert (card.pricing.input_per_mtok, card.pricing.output_per_mtok) == price
    assert usage_cost_usd("chat", model_id, 1_000_000, 1_000_000) == pytest.approx(sum(price))


@pytest.mark.parametrize(
    ("reported", "card_id"),
    [
        ("gpt-5.4-2026-03-05", "gpt-5.4"),
        ("gpt-5.4-mini-2026-03-17", "gpt-5.4-mini"),
        ("gpt-6.1-terra-2026-10-26", "gpt-6.1-terra"),
    ],
)
def test_a_dated_snapshot_an_answer_reports_resolves(reported: str, card_id: str) -> None:
    """The ledger books the model an OpenAI or Azure answer reports, with its snapshot date.

    Without resolving that name the call stays unpriced although its card exists.
    """
    card = _card(card_id)
    assert resolve_model_card(reported) is card
    assert usage_cost_usd("chat", reported, 1_000_000, 0) == pytest.approx(
        card.pricing.input_per_mtok
    )


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
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna", "gpt-6-sol",
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


def test_the_estimate_counts_system_and_prompt_plus_output_and_the_reserve() -> None:
    estimated, required = estimate_required_context_tokens(
        system="a" * 9, prompt="b" * 8, requested_output_tokens=100
    )

    assert estimated == 5  # 17 characters at four per token, rounded up
    assert required == 5 + 100 + CONTEXT_WINDOW_SAFETY_TOKENS


def test_the_estimate_never_falls_below_one_token_or_counts_a_negative_output() -> None:
    estimated, required = estimate_required_context_tokens(
        system=None, prompt="", requested_output_tokens=-5
    )

    assert estimated == 1
    assert required == 1 + CONTEXT_WINDOW_SAFETY_TOKENS


@pytest.mark.parametrize(
    ("llm", "window"),
    [
        (SimpleNamespace(context_window_tokens=1_000), 1_000),
        (SimpleNamespace(context_window_tokens=lambda: 2_000), 2_000),
        (SimpleNamespace(_context_window_tokens=3_000), 3_000),
        (SimpleNamespace(context_window_tokens=0, _context_window_tokens=0), None),
        (SimpleNamespace(), None),
    ],
)
def test_a_provider_window_counts_only_when_positive(llm: object, window: int | None) -> None:
    assert llm_context_window_tokens(llm) == window


@pytest.mark.parametrize(
    ("llm", "output"),
    [
        (SimpleNamespace(_default_max_tokens=1_000), 1_000),
        (SimpleNamespace(_default_max_tokens=0), DEFAULT_LLM_MAX_OUTPUT_TOKENS),
        (SimpleNamespace(), DEFAULT_LLM_MAX_OUTPUT_TOKENS),
    ],
)
def test_a_provider_output_budget_falls_back_to_the_default(llm: object, output: int) -> None:
    assert llm_default_output_tokens(llm) == output


_RESPONSES_MODELS = {
    "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5-pro", "gpt-5.4-pro",
}
"""Models that refuse function tools while they reason over Chat Completions, or offer no Chat Completions."""


def test_every_openai_card_names_the_api_its_model_needs() -> None:
    """A card without the rule would send GPT-6 over Chat Completions, where every reasoning tool call fails."""
    openai_cards = [card for card in MODEL_CARDS if card.vendor == "OpenAI"]
    assert openai_cards
    for card in openai_cards:
        assert card.openai is not None, card.id
        expected = "responses" if card.id in _RESPONSES_MODELS else "chat_completions"
        assert card.openai.api == expected, card.id
    assert _RESPONSES_MODELS <= {card.id for card in openai_cards}


def test_the_input_budget_keeps_below_the_input_limit_of_a_large_window() -> None:
    """GPT-6 takes at most 922,000 input tokens of its 1,050,000-token window."""
    from inqtrix.model_cards import card_input_budget

    terra = _card("gpt-6.1-terra")
    assert card_input_budget(terra) == 1_050_000 - 128_000 - 21_000
    assert card_input_budget(terra) < 922_000


@pytest.mark.parametrize(
    ("name", "card_id"),
    [
        ("GPT-6.1-Terra", "gpt-6.1-terra"),
        ("azure/gpt-6.1-terra", "gpt-6.1-terra"),
        ("anthropic/claude-opus-4-8", "claude-opus-4-8"),
        ("arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.anthropic.claude-opus-4-8-v1:0", "claude-opus-4-8"),
        ("us-gov.anthropic.claude-opus-4-8-v1:0", "claude-opus-4-8"),
    ],
)
def test_case_gateway_prefixes_and_profile_arns_find_the_card(name: str, card_id: str) -> None:
    """Without the card a GPT-6 deployment would go over Chat Completions and fail on every reasoning tool call."""
    card = resolve_model_card(name)
    assert card is not None and card.id == card_id


def test_an_exact_alias_wins_over_a_normalized_match_of_an_earlier_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """A card for a snapshot with its own price stays reachable behind the undated card."""
    import inqtrix.model_cards as cards

    undated = _card("gpt-5.4").model_copy(update={"id": "x-model", "aliases": ["x-model"]})
    snapshot = _card("gpt-5.4").model_copy(update={"id": "x-model-2026-01-01", "aliases": ["x-model-2026-01-01"]})
    monkeypatch.setattr(cards, "MODEL_CARDS", [undated, snapshot])
    assert cards.resolve_model_card("x-model-2026-01-01").id == "x-model-2026-01-01"
    assert cards.resolve_model_card("x-model-2026-02-02").id == "x-model"


def test_the_desk_describes_every_card_in_both_languages() -> None:
    """A card without a desk translation shows the German server text to English readers."""
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parents[1] / "apps/research-desk/src/i18n/translations.ts").read_text(encoding="utf-8")
    blocks = re.findall(r"modelDescriptions: \{(.*?)\n      \},", source, re.S)
    assert len(blocks) == 2, "one table per language (de, en)"
    ids = {card.id for card in MODEL_CARDS}
    for block in blocks:
        assert set(re.findall(r"^\s+'([^']+)':", block, re.M)) == ids


def test_an_alias_written_in_capitals_still_matches_after_normalisation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case is ignored on both sides, also when the dated snapshot has to be stripped first."""
    import inqtrix.model_cards as cards

    card = _card("gpt-5.4").model_copy(update={"id": "prod-model", "aliases": ["Prod-Model"]})
    monkeypatch.setattr(cards, "MODEL_CARDS", [card])
    assert cards.resolve_model_card("prod-model-2026-01-01").id == "prod-model"


_EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def test_every_card_lists_its_levels_from_weakest_to_strongest() -> None:
    """The adapters take the first graded level as the weakest one, for "off" on a model that cannot switch off."""
    for card in MODEL_CARDS:
        positions = [_EFFORT_ORDER.index(level) for level in card.reasoning_levels]
        assert positions == sorted(positions), card.id


def test_the_input_limit_stands_where_the_provider_names_one() -> None:
    """922,000 input tokens for the 1,050,000-token windows of GPT-6, GPT-5.6 and GPT-5.5; Claude counts input plus output."""
    from inqtrix.model_cards import card_input_budget

    limited = {card.id for card in MODEL_CARDS if card.max_input_tokens}
    assert limited == {
        "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
    }
    assert all(_card(card_id).max_input_tokens == 922_000 for card_id in limited)
    assert _card("claude-opus-4-8").max_input_tokens is None
    assert card_input_budget(_card("claude-opus-4-8")) == 1_000_000 - 128_000 - 20_000
    assert card_input_budget(_card("gpt-6.1-terra").model_copy(update={"max_input_tokens": 500_000})) == 500_000
    assert "max_input_tokens" not in build_models_catalog(["gpt-6.1-terra"])[0]["card"]


def test_a_metric_label_of_a_model_without_a_card_keeps_its_path_and_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Card matching ignores case and gateway prefixes; a metric label must not merge two routes into one series."""
    from inqtrix.observability import metrics_defs

    monkeypatch.setattr(metrics_defs, "_fallback_model_labels", set())
    assert metrics_defs.metric_model_label("team-a/llama-70b") == "team-a/llama-70b"
    assert metrics_defs.metric_model_label("team-b/llama-70b") == "team-b/llama-70b"
    assert metrics_defs.metric_model_label("Team-C/Llama-70B-2026-01-01") == "Team-C/Llama-70B"
    assert metrics_defs.metric_model_label("azure/GPT-6.1-Terra") == "gpt-6.1-terra"
