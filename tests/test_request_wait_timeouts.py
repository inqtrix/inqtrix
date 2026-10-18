"""Contract for the HTTP wait-timeout derivations in ``request_parsing``.

These pin the relationship the timeout dependency chain documents:

* every derived wait exceeds its inner per-call budget by exactly
  ``REQUEST_WAIT_MARGIN_SECONDS`` so the inner call's specific error wins
  before the outer ``asyncio.wait_for`` fires a generic 504;
* the editor and ``/v1/text`` waits hang off *different* base timeouts
  (``editor_assistant_timeout`` vs ``claim_extract_timeout``) and bound
  different work, even though they share the same ``min(...)`` shape;
* a raised inner budget is always capped by the whole-run HTTP deadline.

They go red if someone re-inlines a divergent ``+ 30`` literal, swaps which
base a wait derives from, or drops the run-deadline cap.
"""

from __future__ import annotations

from inqtrix.services.request_parsing import (
    REQUEST_WAIT_MARGIN_SECONDS,
    editor_wait_seconds,
    request_timeout_seconds,
    text_wait_seconds,
)
from inqtrix.settings import AgentSettings


def _settings(**overrides: int) -> AgentSettings:
    # editor_assistant_timeout is set DISTINCT from reasoning_timeout so the
    # editor-wait assertions actually pin the derivation to the editor budget
    # and cannot pass by coincidental default equality (editor_assistant_timeout
    # defaults to the same 600-second value as reasoning_timeout).
    base = {
        "max_total_seconds": 300,
        "reasoning_timeout": 120,
        "editor_assistant_timeout": 200,
        "claim_extract_timeout": 60,
    }
    base.update(overrides)
    return AgentSettings(**base)


def test_request_timeout_exceeds_run_budget_by_the_margin() -> None:
    settings = _settings(max_total_seconds=300)
    assert (
        request_timeout_seconds(settings)
        == 300 + REQUEST_WAIT_MARGIN_SECONDS
    )


def test_editor_and_text_waits_hang_off_different_base_timeouts() -> None:
    # Same min() shape, different base: editor -> editor_assistant_timeout,
    # text -> claim_extract_timeout. With a generous run budget neither hits the
    # cap, so each tracks its own base + margin. editor_assistant_timeout (200)
    # is deliberately != reasoning_timeout (120): the editor assertion goes red
    # if editor_wait_seconds were reverted to derive from reasoning_timeout.
    settings = _settings(
        max_total_seconds=3600,
        reasoning_timeout=120,
        editor_assistant_timeout=200,
        claim_extract_timeout=60,
    )
    assert editor_wait_seconds(settings) == 200 + REQUEST_WAIT_MARGIN_SECONDS
    assert text_wait_seconds(settings) == 60 + REQUEST_WAIT_MARGIN_SECONDS
    assert editor_wait_seconds(settings) != text_wait_seconds(settings)


def test_inner_budget_is_capped_by_the_run_deadline() -> None:
    # An inner per-call budget raised above the run deadline cannot let the wait
    # outlive the whole run: both waits cap at request_timeout_seconds. The
    # editor budget is raised via editor_assistant_timeout (its real base).
    settings = _settings(
        max_total_seconds=100,
        editor_assistant_timeout=900,
        claim_extract_timeout=900,
    )
    cap = request_timeout_seconds(settings)
    assert editor_wait_seconds(settings) == cap
    assert text_wait_seconds(settings) == cap
