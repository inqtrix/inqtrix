""" Static default constants for agent behavior.

These are the single definition point for the settings defaults that
carry the same name in lower case. ``settings.py`` reads them rather
than spelling the literal again -- three of them had silently drifted
to a stale value while the real default lived only in the ``Field()``
call, and the documentation dutifully copied the dead constant.
``tests/test_documented_contracts.py`` pins the pairing.
"""

from __future__ import annotations

# Optional conversation labels must never occupy the answer executor.
SESSION_TITLE_CONCURRENCY = 2

MAX_ROUNDS = 2
CONFIDENCE_STOP = 7
FIRST_ROUND_QUERIES = 6
ANSWER_PROMPT_CITATIONS_MAX = 60

# Token capacity defaults
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 64_000
REQUIRED_CONTEXT_WINDOW_TOKENS = 128_000
CONTEXT_WINDOW_SAFETY_TOKENS = 2_048

# Timeouts (seconds)
REASONING_TIMEOUT = 600
EDITOR_ASSISTANT_TIMEOUT = 600
SEARCH_TIMEOUT = 600
CLAIM_EXTRACT_TIMEOUT = 600
MAX_TOTAL_SECONDS = 3_600
REQUEST_WAIT_MARGIN_SECONDS = 30
"""Grace between an inner AI deadline and its outer client wait."""

MAX_QUESTION_LENGTH = 60_000

# Search cache defaults
SEARCH_CACHE_MAXSIZE = 256
SEARCH_CACHE_TTL = 3600

# Risk scoring
HIGH_RISK_SCORE_THRESHOLD = 4

MAX_PLAN_TASKS_DEFAULT = 8
"""Task-count ceiling of one mission plan — the ONE definition behind the
validator, the ``INQTRIX_AGENT_MAX_PLAN_TASKS`` default and the control
service default (a literal 8 at any of those sites bypasses this edit
point)."""

MAX_TASK_QUERIES = 8
"""Queries one plan task may carry; the planner prompt names the same
bound ("1-8 Leitfragen") from this constant."""

CLARIFICATION_OPTION_LABEL_CHARS = 60
"""Length a clarification answer option should keep so its chip stays
short. A request, not a limit: the option schema asks for it, and a longer
option is stored and shown in full with a warning in the log, because the
stored option wording is the user's answer."""

DISCOVERY_CONTENT_QUESTIONS_MAX = 4
"""Content questions the intake is asked to write at most. A request, not a
limit: the intake prompt and the schema state it, nothing enforces it.
Every content question is searched in discovery until the discovery call
budget (``discovery_max_tool_calls``) is used up, and that budget is the
only cut, reported as a visible limit event."""

RUN_INCOGNITO_RETENTION_SECONDS = 900
"""How long an incognito knowledge ask stays in the run store after it
ended. The browser that asked has fetched its answer by then; the run, its
events and its result are deleted at the first cleanup after this window."""
