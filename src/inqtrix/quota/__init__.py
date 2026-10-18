"""Per-user usage quotas for oidc multi-user deployments.

The quota layer caps how much a single authenticated user may consume
per calendar month (runs, LLM tokens, embedding tokens) plus a stock
cap on stored bytes. It is the enforcement half of the two-level rule:
:class:`~inqtrix.settings.QuotaSettings` is the operator ceiling, the
admin UI sets a tenant default and per-user overrides within it, and
:func:`effective_limit` resolves the three layers into one number.

The subject is the canonical ``(tenant_id, user_id)`` pair (request
workspace ids are the non-authoritative ui_namespace).
Quotas bind only in oidc mode; the anonymous/static principals bypass
entirely, so existing deployments stay byte-identical.
"""

from inqtrix.quota.models import (
    DEFAULT_USER_ID,
    STOCK_PERIOD,
    DimensionUsage,
    QuotaDimension,
    QuotaExceeded,
    QuotaSubject,
    consumed_tokens,
    current_period_start,
    effective_limit,
    estimate_tokens,
    period_end,
)
from inqtrix.quota.ports import QuotaStore

__all__ = [
    "DEFAULT_USER_ID",
    "STOCK_PERIOD",
    "DimensionUsage",
    "QuotaDimension",
    "QuotaExceeded",
    "QuotaStore",
    "QuotaSubject",
    "consumed_tokens",
    "current_period_start",
    "effective_limit",
    "estimate_tokens",
    "period_end",
]
