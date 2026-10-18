"""Usage-ledger read surface: the caller's own spend + the admin view.

Two audiences, one router (mounted only when a usage recorder is wired —
the ledger exists independently of quotas):

* ``GET /v1/usage`` — the authenticated caller's own consumption, always
  scoped to their canonical user id server-side.
* ``GET /v1/admin/usage`` — the tenant-wide view, gated to the instance
  admin (``instance_role == "admin"``, 404-not-403 house convention).

Both answer with the same body shape, and that shape always carries the
unpriced remainder next to the cost. A "cost" field without a "how much of
this had no price" field cannot exist here: the ledger books tokens for
model calls, embedding calls and per-call web search alike, and only some
of those have a token list price. A total that quietly dropped the rest
would be believed precisely because it looks exact.

Prices are never stored. Cost is derived at read time from the model and
embedding cards, so a price correction applies to history as well.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.server.routers._admin_guard import TENANT, require_instance_admin
from inqtrix.services.request_parsing import error_response
from inqtrix.usage.grouping import (
    USAGE_GROUP_DEFAULT,
    USAGE_GROUP_KEYS,
    costing_group_by,
    normalize_usage_group_by,
)
from inqtrix.usage.models import summarize_usage_cost, usage_cost_usd

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")

MAX_WINDOW_SECONDS = 366 * 24 * 3600
"""Widest window a single query may span.

The ledger is retained far longer than a year in some deployments; an
unbounded window would let one request aggregate the entire table.
"""


def _parse_float(value: str | None) -> float | None:
    """Parse an optional epoch-seconds query parameter."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'invalid timestamp: {value!r}') from exc


def _parse_group_by(raw: str | None) -> tuple[str, ...]:
    """Parse the comma-separated group_by parameter."""
    if not raw:
        return USAGE_GROUP_DEFAULT
    return normalize_usage_group_by(
        tuple(part.strip() for part in raw.split(",") if part.strip())
    )


def _body(rows: list[dict], group_by: tuple[str, ...]) -> dict:
    """Assemble the answer: display rows on the caller's axes, honest total.

    ``rows`` always carry the pricing axes, even when the caller grouped by
    something else — pricing folds the fine rows up into the display rows
    rather than re-deriving from a projection that may have dropped the model.
    """
    summary = summarize_usage_cost(rows)
    display: dict[tuple[str, ...], dict] = {}
    for row in rows:
        key = tuple(str(row.get(k, "")) for k in group_by)
        bucket = display.setdefault(key, {
            **{k: str(row.get(k, "")) for k in group_by},
            "input_tokens": 0,
            "output_tokens": 0,
            "request_count": 0,
            "cost_usd": 0.0,
            # False as soon as one contributing row has no list price, so a
            # partial sum can never be read as a complete one.
            "cost_complete": True,
        })
        bucket["input_tokens"] += row["input_tokens"]
        bucket["output_tokens"] += row["output_tokens"]
        bucket["request_count"] += row["request_count"]
        cost = usage_cost_usd(
            str(row.get("operation") or ""),
            str(row.get("model") or ""),
            row["input_tokens"],
            row["output_tokens"],
        )
        if cost is None:
            bucket["cost_complete"] = False
        else:
            bucket["cost_usd"] += cost
    return {
        "object": "usage_report",
        "group_by": list(group_by),
        "data": list(display.values()),
        "total": {
            "cost_usd": summary.cost_usd,
            "is_complete": summary.is_complete,
            "priced_input_tokens": summary.priced_input_tokens,
            "priced_output_tokens": summary.priced_output_tokens,
            "unpriced_input_tokens": summary.unpriced_input_tokens,
            "unpriced_output_tokens": summary.unpriced_output_tokens,
            "unpriced_models": list(summary.unpriced_models),
        },
    }


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the usage self + admin routes against the container.

    Raises:
        RuntimeError: When called without a wired usage recorder —
            registration is a composition decision, not a runtime fallback
            (mirrors the quota and knowledge routers).
    """
    from inqtrix.usage.recorder import active_usage_recorder

    recorder = active_usage_recorder()
    if recorder is None:
        raise RuntimeError("usage router requires an active usage recorder")

    router = APIRouter()
    provider = container.auth_provider
    principal_dep = container.principal_dependency
    store = recorder.store
    # Same tenant constant every admin surface uses.
    tenant_id = TENANT

    async def _window(request: Request):
        """Parse and validate the shared query parameters."""
        try:
            since = _parse_float(request.query_params.get("since"))
            until = _parse_float(request.query_params.get("until"))
            group_by = _parse_group_by(request.query_params.get("group_by"))
        except ValueError as exc:
            return None, error_response(
                400, str(exc), "invalid_request_error"
            )
        if since is not None and until is not None:
            if until < since:
                return None, error_response(
                    400,
                    "'until' is before 'since'",
                    "invalid_request_error",
                )
            if until - since > MAX_WINDOW_SECONDS:
                return None, error_response(
                    400,
                    "Time window is longer than one year",
                    "invalid_request_error",
                )
        return (since, until, group_by), None

    @router.get("/v1/usage")
    async def my_usage(request: Request):
        """The caller's own consumption and what it cost.

        The user filter is applied server-side from the verified principal;
        a caller cannot widen it to someone else's spend.
        """
        principal = await principal_dep(request)
        parsed, error = await _window(request)
        if error is not None:
            return error
        since, until, group_by = parsed
        if principal.user_id is None:
            return _body([], group_by)
        rows = await store.aggregate(
            tenant_id=tenant_id,
            group_by=costing_group_by(group_by),
            since=since,
            until=until,
            run_id=request.query_params.get("run_id") or None,
            user_id=principal.user_id,
        )
        return _body(rows, group_by)

    @router.get("/v1/admin/usage")
    async def tenant_usage(request: Request):
        """Tenant-wide consumption for the instance admin."""
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        parsed, window_error = await _window(request)
        if window_error is not None:
            return window_error
        since, until, group_by = parsed
        raw_user = request.query_params.get("user_id") or ""
        subject: uuid.UUID | None = None
        if raw_user:
            try:
                subject = uuid.UUID(raw_user)
            except ValueError:
                return error_response(
                    400,
                    "Field 'user_id' must be a UUID",
                    "invalid_request_error",
                )
        rows = await store.aggregate(
            tenant_id=tenant_id,
            group_by=costing_group_by(group_by),
            since=since,
            until=until,
            run_id=request.query_params.get("run_id") or None,
            user_id=subject,
        )
        return _body(rows, group_by)

    @router.get("/v1/usage/axes")
    async def axes():
        """The group-by vocabulary, so the UI need not hardcode it."""
        return {
            "object": "list",
            "data": list(USAGE_GROUP_KEYS),
            "default": list(USAGE_GROUP_DEFAULT),
        }

    return router
