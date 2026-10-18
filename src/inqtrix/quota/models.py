"""Quota domain model: dimensions, period math, limit resolution.

Pure and dependency-free so the rules are unit-tested directly. The two
load-bearing pieces are :func:`current_period_start` (the calendar-month
window, lazily rolled — no cron) and :func:`effective_limit` (the
three-layer resolution under the operator ceiling).
"""

from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass
from enum import StrEnum

#: Sentinel ``subject_user_id`` for the tenant-wide default limit row (the
#: admin's "Standard für alle"). ``None`` can never collide with a
#: canonical user UUID.
DEFAULT_USER_ID: None = None

#: Sentinel ``period_start`` for stock dimensions (no monthly window —
#: the level rises on use and falls on release, never rolls over).
STOCK_PERIOD = 0.0


class QuotaDimension(StrEnum):
    """The metered cost vectors.

    Flow dimensions reset each calendar month; the stock dimension
    (``stored_bytes``) is a level freed by deletion, never reset.
    """

    RUNS = "runs"
    LLM_TOKENS = "llm_tokens"
    EMBEDDING_TOKENS = "embedding_tokens"
    STORED_BYTES = "stored_bytes"

    @property
    def is_stock(self) -> bool:
        """Whether this is a stock level (vs. a per-period flow)."""
        return self is QuotaDimension.STORED_BYTES


FLOW_DIMENSIONS: tuple[QuotaDimension, ...] = tuple(
    d for d in QuotaDimension if not d.is_stock
)


@dataclass(frozen=True)
class QuotaSubject:
    """Canonical quota account for one user within one tenant."""

    tenant_id: str
    user_id: uuid.UUID


@dataclass(frozen=True)
class StockLifecycleState:
    """Canonical contribution of one resource to a stock counter.

    ``amount`` is the resource's current contribution, while ``tombstoned``
    permanently fences the lifecycle key from being charged again.  A new
    physical resource must therefore receive a new lifecycle key.
    """

    stock_key: str
    subject: QuotaSubject
    dimension: QuotaDimension
    amount: int
    tombstoned: bool


def file_stock_key(file_id: str) -> str:
    """Return the stable stock identity for one physical file lifecycle."""

    value = str(file_id).strip()
    if not value:
        raise ValueError("file stock identity requires a non-empty file id")
    return f"file:{value}"


@dataclass(frozen=True)
class DimensionUsage:
    """Resolved usage for one dimension (the UI/enforcement view).

    Attributes:
        dimension: Which cost vector.
        used: Consumed amount in the active window (current month for
            flow dimensions, the running total for stock).
        limit: Effective limit, or ``None`` for unlimited.
        period_start: Unix timestamp of the window start (``STOCK_PERIOD``
            for stock dimensions).
    """

    dimension: QuotaDimension
    used: int
    limit: int | None
    period_start: float

    @property
    def remaining(self) -> int | None:
        """Amount left before the limit, or ``None`` when unlimited."""
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)

    def allows(self, amount: int) -> bool:
        """Whether *amount* more fits under the effective limit."""
        return self.limit is None or self.used + amount <= self.limit


class QuotaExceeded(RuntimeError):
    """Raised when an action would cross a user's effective limit.

    Carries the facts the 429 envelope and the UI need: which dimension,
    the limit, the current usage, and when the window resets.
    """

    def __init__(
        self,
        *,
        dimension: QuotaDimension,
        limit: int,
        used: int,
        reset_at: float,
    ) -> None:
        super().__init__(f"quota exceeded for {dimension.value}: {used}/{limit}")
        self.dimension = dimension
        self.limit = limit
        self.used = used
        self.reset_at = reset_at


class QuotaAdjustmentConflict(RuntimeError):
    """An idempotency key was replayed with contradictory accounting facts."""

    def __init__(self, adjustment_id: str) -> None:
        self.adjustment_id = adjustment_id
        super().__init__(
            "quota adjustment id contradicts its original subject, dimension, "
            "or amount"
        )


def consumed_tokens(usage: dict | None) -> int:
    """Total LLM tokens from a provider ``usage`` dict (prompt + completion).

    The single reader of the ``{"prompt_tokens", "completion_tokens"}``
    shape the graph and chat paths emit, so the runs, non-streaming
    chat, and streaming chat recording sites cannot drift. Missing or
    ``None`` fields count as 0; a missing dict counts as 0.
    """
    if not usage:
        return 0
    return int(usage.get("prompt_tokens", 0) or 0) + int(
        usage.get("completion_tokens", 0) or 0
    )


def estimate_tokens(text: str) -> int:
    """Rough token count for embedding ingestion (no provider usage).

    Document ingestion embeds chunks without a per-call usage object, so
    its embedding-token spend is approximated with the standard
    ~4-chars-per-token heuristic. Every LLM surface (chat, runs, editor,
    text) records the provider's REAL usage instead. Embeddings estimate
    because the ``EmbeddingProvider`` contract returns vectors only and
    discards any provider-side usage object, not because the numbers are
    unobtainable in principle. The usage ledger books the same estimate
    over the same texts, so both accountings agree by construction.
    Rounds up so non-empty text always costs at least one token.
    """
    return -(-len(text) // 4) if text else 0


def current_period_start(now: float) -> float:
    """Unix timestamp of the start of *now*'s calendar month (UTC).

    The window key for flow dimensions. Lazily evaluated everywhere —
    a counter whose stored period differs from this value reads as 0
    and is overwritten on the next write, so no scheduled job is needed.
    """
    moment = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def period_end(period_start: float) -> float:
    """Unix timestamp where the month starting at *period_start* ends.

    I.e. the start of the following month — the reset moment shown to
    the user. ``STOCK_PERIOD`` has no end and returns ``0.0``.
    """
    if period_start == STOCK_PERIOD:
        return 0.0
    start = _dt.datetime.fromtimestamp(period_start, tz=_dt.timezone.utc)
    month = start.month + 1
    year = start.year + (1 if month > 12 else 0)
    month = 1 if month > 12 else month
    return start.replace(year=year, month=month).timestamp()


def effective_limit(
    *,
    override: int | None,
    tenant_default: int | None,
    env_default: int,
    env_ceiling: int,
) -> int | None:
    """Resolve the three configurable layers under the operator ceiling.

    Precedence: a per-user *override* wins over the admin-set
    *tenant_default*, which wins over the *env_default*. ``0`` is an
    explicit "unlimited" at any layer; absence (``None``) of an
    override/default falls through to the next. The result is then
    clamped to *env_ceiling* — a non-zero ceiling caps even an
    "unlimited" choice, which is what keeps an admin from setting
    themselves above the operator's hard bound.

    Returns:
        The effective limit, or ``None`` for unlimited.
    """
    if override is not None:
        chosen = override
    elif tenant_default is not None:
        chosen = tenant_default
    else:
        chosen = env_default
    base = None if chosen == 0 else chosen
    ceiling = None if env_ceiling == 0 else env_ceiling
    if base is None:
        return ceiling
    if ceiling is None:
        return base
    return min(base, ceiling)
