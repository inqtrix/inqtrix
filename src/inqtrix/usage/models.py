"""Usage-ledger row shape and read-time cost derivation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageRow:
    """One provider call's booked consumption (immutable ledger row)."""

    tenant_id: str
    user_id: uuid.UUID
    workspace_id: str | None
    run_id: str | None
    feature: str
    operation: str
    model: str
    input_tokens: int
    output_tokens: int
    request_count: int
    duration_ms: int
    outcome: str
    created_at: float


def usage_cost_usd(
    operation: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float | None:
    """USD list cost for one aggregate, derived at READ time.

    Prices change; the ledger stores tokens, never money. The price source
    follows the operation, because the catalogues are genuinely different
    shapes rather than one shape with gaps:

    - ``chat`` / ``text_completion`` price input and output separately from
      the model cards.
    - ``embeddings`` price input only — an embedding call has no output
      tokens, which is why the embedding card carries a single price.
    - ``web_search`` is billed per call under an operator-named agent
      identifier, not per token, so no token price can be honest here.

    ``None`` means *not priceable*, never *free*. A self-hosted embedding
    model with no list price returns ``None`` for exactly that reason; so
    does an uncatalogued model and an unknown operation. Substituting a
    default price would hide the gap.
    """
    if operation in {"chat", "text_completion"}:
        from inqtrix.model_cards import resolve_model_card

        card = resolve_model_card(model)
        if card is None:
            return None
        return (
            int(input_tokens) / 1_000_000 * card.pricing.input_per_mtok
            + int(output_tokens) / 1_000_000 * card.pricing.output_per_mtok
        )
    if operation == "embeddings":
        from inqtrix.embedding_cards import resolve_embedding_card

        embedding_card = resolve_embedding_card(model)
        if embedding_card is None:
            return None
        price = embedding_card.pricing_input_per_mtok
        if price is None:
            return None
        return int(input_tokens) / 1_000_000 * price
    return None


@dataclass(frozen=True)
class UsageCostSummary:
    """A cost figure that carries what it could NOT price.

    The unpriced part is a field of the same return value, so a caller
    cannot publish the sum while dropping the caveat. Without this, a
    correct-looking total silently understates spend by whatever share of
    the consumption has no list price.
    """

    cost_usd: float
    priced_input_tokens: int
    priced_output_tokens: int
    unpriced_input_tokens: int
    unpriced_output_tokens: int
    unpriced_models: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Whether every booked token could be priced."""
        return not self.unpriced_models


def summarize_usage_cost(
    rows: "Sequence[Mapping[str, object]]",
) -> UsageCostSummary:
    """Price a set of aggregate rows and report the unpriced remainder.

    Each row needs ``operation``, ``model``, ``input_tokens`` and
    ``output_tokens`` — the shape the ledger aggregation already returns.
    This is the ONLY supported way to turn ledger rows into money: summing
    :func:`usage_cost_usd` by hand loses the unpriced remainder, which is
    the number that makes the total trustworthy.
    """
    total = 0.0
    priced_in = priced_out = 0
    unpriced_in = unpriced_out = 0
    unpriced: dict[str, None] = {}
    for row in rows:
        operation = str(row.get("operation") or "")
        model = str(row.get("model") or "")
        input_tokens = int(row.get("input_tokens") or 0)
        output_tokens = int(row.get("output_tokens") or 0)
        cost = usage_cost_usd(operation, model, input_tokens, output_tokens)
        if cost is None:
            unpriced_in += input_tokens
            unpriced_out += output_tokens
            unpriced.setdefault(model or "(unbekannt)", None)
            continue
        total += cost
        priced_in += input_tokens
        priced_out += output_tokens
    return UsageCostSummary(
        cost_usd=total,
        priced_input_tokens=priced_in,
        priced_output_tokens=priced_out,
        unpriced_input_tokens=unpriced_in,
        unpriced_output_tokens=unpriced_out,
        unpriced_models=tuple(sorted(unpriced)),
    )
