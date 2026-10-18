"""Interactive Anthropic + Perplexity research chat (terminal REPL).

Architecture
------------
Same provider stack as the sibling
[``anthropic_perplexity.py``](anthropic_perplexity.py):

1. **Anthropic Messages API** via ``AnthropicLLM`` (native ``urllib``).
2. **Perplexity Sonar API** via ``PerplexitySearch`` (OpenAI SDK pointed
   at ``https://api.perplexity.ai``).

The only difference is the run mode: instead of a single hard-coded
``QUESTION``, this script opens a chat-style loop in the terminal.
You type a question, watch the research stream, read the rendered
Markdown answer, and ask another question.

Conversation context
--------------------
Conversation history is fed into the next turn via the ``history``
argument of :meth:`inqtrix.ResearchAgent.stream`. Each turn re-runs
the full classify/plan/search/evaluate/answer graph, but the prior
Q&A is included in the classify prompt so phrases like "this", "those
states", or "the previous topic" resolve correctly.

This intentionally does not reuse hidden server-side research state.
Each turn does fresh research while carrying only visible chat history,
so the transcript fully explains what context the agent received.

History is capped at the last :data:`MAX_HISTORY_TURNS` Q&A pairs to
keep the classify prompt bounded. Use ``/clear`` to reset on demand.

Slash commands
--------------
- ``/help``, ``/?`` — show available commands
- ``/clear``        — reset conversation history (next question is fresh)
- ``/exit``, ``/bye``, ``/quit`` — leave the chat loop
- ``Ctrl+C`` during a run — cancel the current turn, keep the loop
- ``Ctrl+C`` at empty prompt — exit
- ``Ctrl+D`` — exit

Required environment variables (in ``.env`` or process env):

- ``ANTHROPIC_API_KEY``
- ``PERPLEXITY_API_KEY``

Optional (file logging, same as the sibling):

- ``INQTRIX_LOG_ENABLED``, ``INQTRIX_LOG_LEVEL``, ``INQTRIX_LOG_CONSOLE``
- ``OBSERVABILITY_PROFILE=forensic`` — structured lineage in the same log file;
  pair with ``INQTRIX_LOG_LEVEL=DEBUG`` (see ``docs/observability/logging.md`` and
  ``docs/observability/forensic-cookbook.md``).

Recommendation: leave ``INQTRIX_LOG_CONSOLE`` unset (the default).
WARNING+ records on stderr can otherwise collide with the live
progress region and visually fragment the rendered frame. File-only
logging via ``INQTRIX_LOG_ENABLED=true`` is safe.

Run with::

    uv run python examples/provider_stacks/anthropic_perplexity_chat.py
"""

from __future__ import annotations
import io
import os
import re
import sys
import time
from collections import deque

# Activate line editing for input() — arrow keys, in-session history,
# Ctrl+A/E/W/U. GNU readline ships in the stdlib on Linux/macOS; on
# Windows we fall back to pyreadline3, a pure-Python reimplementation
# that monkey-patches input() and is API-compatible with GNU readline
# (the import name stays "readline" once pyreadline3 has been imported,
# but we keep the explicit name here for clarity). The flag below is
# read by main() to choose between an ANSI prompt for input() (with
# the bash-style \001/\002 markers wrapping the colour codes so that
# readline does not miscount the prompt width) and rich's console.input
# fallback for environments where no readline is available.
_HAS_READLINE = False
try:
    import readline  # noqa: F401 — imported for its side effects only
    _HAS_READLINE = True
except ImportError:
    try:
        import pyreadline3  # noqa: F401 — Windows readline replacement
        _HAS_READLINE = True
    except ImportError:
        pass

# Windows uses Ctrl+Z + Enter as the EOF combo; Unix-likes use Ctrl+D.
# Used by the help panel so the displayed shortcut matches the host
# platform without further configuration.
EOF_HINT = "Ctrl+Z, Enter" if sys.platform == "win32" else "Ctrl+D"

from dotenv import load_dotenv

from inqtrix import (
    AgentConfig,
    AnthropicLLM,
    PerplexitySearch,
    ReportProfile,
    ResearchAgent,
)
from inqtrix.exceptions import AgentRateLimited, AgentTimeout
from inqtrix.logging_config import configure_logging

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text


load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────
# File-based logging with automatic secret redaction.  Disabled by
# default — enable via environment variables to keep the terminal
# clean (the chat loop already prints progress to stdout):
#
#   INQTRIX_LOG_ENABLED=true   — write logs to logs/inqtrix_*.log
#   INQTRIX_LOG_LEVEL=DEBUG    — DEBUG / INFO / WARNING (default: INFO)
#   INQTRIX_LOG_CONSOLE=true   — also print WARNING+ to stderr
#   OBSERVABILITY_PROFILE=forensic — lineage EVENT JSON in the same log file; requires
#     INQTRIX_LOG_LEVEL=DEBUG. docs/observability/logging.md,
#     docs/observability/forensic-cookbook.md
_log_path = configure_logging(
    enabled=os.getenv("INQTRIX_LOG_ENABLED", "").lower() == "true",
    level=os.getenv("INQTRIX_LOG_LEVEL", "INFO"),
    console=os.getenv("INQTRIX_LOG_CONSOLE", "").lower() == "true",
)
if _log_path:
    print(f"Logging to {_log_path}")


# Maximum number of past Q&A turns folded into the history string passed
# to the next ``stream(history=...)`` call. Older turns are dropped to
# keep the classify prompt bounded. Bump this if you want longer
# conversational memory; use /clear to reset on demand.
MAX_HISTORY_TURNS = 6

# How many recent progress lines stay visible in the live ticker viewport.
# A complete research round (analyze -> plan -> search -> evaluate) emits
# roughly 14 lines, so 14 keeps an entire round on screen without scrolling
# mid-round. Bump this if you want more retrospective context per turn.
PROGRESS_VIEWPORT_LINES = 14

# Typewriter animation for the final Markdown answer. The complete answer
# is already buffered in memory once stream() finishes — these constants
# only control the *visual* simulation of progressive rendering for an
# elegant reveal.
#
# Each "token" is one word plus its trailing whitespace, so BATCH=5 means
# the live frame redraws after every 5 words. Lower BATCH = smoother but
# more re-renders; higher = chunkier reveal. DELAY is the seconds between
# ticks; lower = faster. Defaults chosen to feel snappy: a typical 500-word
# answer reveals in about 1.2 seconds.
ANSWER_TYPEWRITER_BATCH = 5
ANSWER_TYPEWRITER_DELAY = 0.012

# Bold-cyan prompt string for input() when readline / pyreadline3 is
# active. The ``\001`` / ``\002`` markers tell readline "these bytes do
# not take screen space" so the cursor-width tracking stays correct;
# without them readline would count every byte of the escape codes as
# visible and redraw subsequent input over the prompt, making ``>>> ``
# disappear at the first keystroke. Same bash-style trick used in
# ``PS1``. Only consumed when ``_HAS_READLINE`` is ``True``; the
# no-readline path uses rich's styled ``console.input`` instead, which
# does not need the markers because no cursor-width tracking is
# happening.
PROMPT = "\n\001\033[1;36m\002>>> \001\033[0m\002"

# Slash command vocabulary. Comparisons are case-insensitive.
EXIT_COMMANDS = {"/exit", "/bye", "/quit"}
HELP_COMMANDS = {"/help", "/?"}
CLEAR_COMMANDS = {"/clear"}

# Patterns used to derive the post-run summary line from the captured
# progress log. Both are intentionally narrow: they target the numeric
# markers the agent emits in its terminal "Research finished" line —
# ``Confidence: X/10`` (identical in DE and EN) and ``Rounds: N`` /
# ``Runden: N`` (DE/EN variant). If a future inqtrix release changes
# the wording, the regex falls through and the summary silently drops
# the affected field instead of crashing.
_RE_ROUNDS = re.compile(r"(?:Rounds?|Runden):\s*(\d+)", re.IGNORECASE)
_RE_CONFIDENCE = re.compile(r"Confidence:?\s*(\d+)/10", re.IGNORECASE)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _build_agent() -> ResearchAgent:
    """Create a ResearchAgent with the same provider stack as the sibling.

    Provider construction (models, thinking budget, effort level) and
    the AgentConfig values are deliberately a 1:1 copy of
    ``anthropic_perplexity.py`` so the two examples render the same
    behaviour.  Tweak here if you want chat-specific tuning (e.g.
    lower ``max_rounds`` for snappier turns).

    See the sibling file for a full walkthrough of every option —
    only the option list is repeated here, not the prose.
    """
    anthropic_key = _require_env("ANTHROPIC_API_KEY")
    perplexity_key = _require_env("PERPLEXITY_API_KEY")

    # ── LLM Provider ────────────────────────────────────────────────
    #
    # AnthropicLLM calls the Anthropic Messages API directly
    # (https://api.anthropic.com/v1/messages) without a proxy.
    #
    # default_model: used for ALL reasoning roles — classify, plan,
    #   evaluate, and final answer synthesis.
    # claim_extract_model: cheaper/faster model used for search-result
    #   claim extraction (parallel helper calls).
    # thinking={"type": "adaptive"}: recommended for Claude 4.6 reasoning
    #   models. Forwarded on reasoning calls only.
    # effort="medium": balanced cost/speed for research workloads.
    #
    # Models: claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5
    llm = AnthropicLLM(
        api_key=anthropic_key,
        # classify_model="claude-sonnet-4-6",
        # evaluate_model="claude-sonnet-4-6",
        thinking={"type": "adaptive"},
        # Recommended default for research workloads — balanced cost/speed.
        # Drop or change to "high" for tasks needing maximum reasoning depth.
        effort="medium",
        # Three model tiers. Nodes map: answer -> high, plan/evaluate/
        # direct_chat -> mid, classify/claim_extract -> fast. Per-tier
        # effort turns reasoning on deliberately (only the high tier here);
        # tiers otherwise differ by model alone. A per-node <node>_model arg
        # overrides the tier. See docs/architecture/websearch/nodes.md
        # Fallback + the reasoning_model identity shown on /health and
        # /v1/stacks (= high tier); the tiers below are the active routing.
        default_model="claude-opus-4-7",
        tier_high_model="claude-opus-4-7",   tier_high_effort="medium",
        tier_mid_model="claude-sonnet-4-6",  tier_mid_effort="none",
        tier_fast_model="claude-haiku-4-5",  tier_fast_effort="none",
    )

    # ── Search Provider ─────────────────────────────────────────────
    #
    # PerplexitySearch pointed directly at the Perplexity Sonar API.
    # base_url has no /v1 suffix — the OpenAI SDK appends
    # /chat/completions internally. Model names are Perplexity-native
    # (sonar-pro, sonar), not LiteLLM aliases.
    search = PerplexitySearch(
        api_key=perplexity_key,
        base_url="https://api.perplexity.ai",
    )

    # ── AgentConfig ─────────────────────────────────────────────────
    #
    # Only llm + search are required for explicit setup. Every other
    # field has a sensible default. See the sibling for prose
    # explanations of each tuning knob.
    return ResearchAgent(AgentConfig(
        llm=llm,
        search=search,

        # Behaviour
        max_rounds=4,
        confidence_stop=8,
        first_round_queries=8,
        answer_prompt_citations_max=500,
        max_total_seconds=3600,
        max_question_length=60_000,

        # Per-call timeouts (seconds)
        reasoning_timeout=600,
        search_timeout=600,
        claim_extract_timeout=600,

        # Risk scoring (domain-neutral). high_risk is an observability
        # signal only; it does NOT change model selection. Use model
        # tiers for stronger models. See docs/architecture/websearch/nodes.md.
        high_risk_score_threshold=4,

        # Search cache
        search_cache_maxsize=256,
        search_cache_ttl=3600,

        report_profile=ReportProfile.DEEP,
    ))


def _command_summary() -> str:
    """Return the multi-line command summary used by welcome and help panels."""
    return (
        "Commands:\n"
        "  [cyan]/help[/cyan], [cyan]/?[/cyan]    Show this help\n"
        "  [cyan]/clear[/cyan]       Reset conversation history\n"
        "  [cyan]/exit[/cyan], [cyan]/bye[/cyan], [cyan]/quit[/cyan]   Leave the chat\n"
        "  [cyan]Ctrl+C[/cyan]       Cancel current turn, or exit at empty prompt\n"
        f"  [cyan]{EOF_HINT}[/cyan]       Exit"
    )


def _print_help(console: Console) -> None:
    """Print the command summary plus the current history cap."""
    body = (
        f"{_command_summary()}\n\n"
        f"History cap: last {MAX_HISTORY_TURNS} turn(s) kept "
        f"(use /clear to reset)."
    )
    console.print(Panel(body, title="Help", expand=False))


def _format_history(turns: list[tuple[str, str]]) -> str:
    """Build the history string passed to ``agent.stream(history=...)``.

    Returns an empty string when there are no prior turns. Otherwise
    formats each turn as ``Question: ...\\nAnswer: ...`` separated by
    blank lines. Only the last :data:`MAX_HISTORY_TURNS` are kept so
    the classify prompt does not grow unbounded.
    """
    if not turns:
        return ""
    recent = turns[-MAX_HISTORY_TURNS:]
    blocks = [f"Question: {q}\nAnswer: {a}" for q, a in recent]
    return "\n\n".join(blocks)


def _render_ticker(lines: deque[str], spinner: Spinner) -> Panel:
    """Build the live progress viewport renderable.

    The viewport is a single :class:`rich.panel.Panel` containing an
    animated spinner header followed by the buffered progress lines.
    Lines fade from bright (newest) through dim to ``bright_black``
    (oldest) so the eye is drawn to the most recent activity. Each
    line is single-line truncated with an ellipsis so a long inqtrix
    notice cannot overflow the viewport vertically.

    Args:
        lines: Bounded buffer of recent progress messages — the
            rightmost entry is the newest and is rendered brightest.
        spinner: A persistent :class:`rich.spinner.Spinner` instance.
            Created once per turn and reused across refreshes so the
            animation advances naturally.
    """
    rendered: list[Text] = []
    n = len(lines)
    for i, line in enumerate(lines):
        # rel == 0.0 for the newest line, rising toward 1.0 for the oldest.
        rel = (n - 1 - i) / max(n - 1, 1)
        if rel < 0.34:
            style = "white"
        elif rel < 0.67:
            style = "dim"
        else:
            style = "bright_black"
        rendered.append(
            Text(f"  {line}", style=style, no_wrap=True, overflow="ellipsis")
        )

    if not rendered:
        rendered.append(
            Text("  Initializing...", style="bright_black", no_wrap=True, overflow="ellipsis")
        )

    body = Group(spinner, *rendered)
    return Panel(
        body,
        border_style="cyan",
        expand=True,
        padding=(0, 1),
    )


def _extract_summary(progress_log: list[str], elapsed_s: float) -> str:
    """Build the one-line collapse summary printed after a turn.

    Walks the captured progress log from newest to oldest and pulls
    the most recently emitted ``Rounds: N`` and ``Confidence X/10``
    markers via :data:`_RE_ROUNDS` / :data:`_RE_CONFIDENCE`. Any
    field whose regex finds nothing is silently dropped — the caller
    always gets at least the checkmark and the duration.

    Args:
        progress_log: Every progress line emitted during the turn,
            in arrival order. Order matters because the most recent
            lines (the agent's "Research finished" terminal line)
            carry the most reliable values.
        elapsed_s: Wall-clock seconds the turn took, measured around
            the streaming loop. Rounded to whole seconds for display.

    Returns:
        A rich-markup string ready to pass to ``console.print``.
        Examples:
            ``"[green]✓[/green] Research complete · 3 rounds · Confidence 7/10 · 47s"``
            ``"[green]✓[/green] Research complete · 12s"``
    """
    rounds: str | None = None
    confidence: str | None = None
    for line in reversed(progress_log):
        if rounds is None:
            match = _RE_ROUNDS.search(line)
            if match:
                rounds = match.group(1)
        if confidence is None:
            match = _RE_CONFIDENCE.search(line)
            if match:
                confidence = match.group(1)
        if rounds and confidence:
            break

    parts = ["[green]✓[/green] Research complete"]
    if rounds:
        parts.append(f"{rounds} rounds")
    if confidence:
        parts.append(f"Confidence {confidence}/10")
    parts.append(f"{int(round(elapsed_s))}s")
    return " · ".join(parts)


def _measure_markdown_height(console: Console, markdown_text: str) -> int:
    """Return how many terminal rows :func:`Markdown` will occupy.

    Renders the Markdown once into a discard buffer at the user's
    current terminal width and counts the resulting newlines. Source-
    text newline counting would underestimate by a wide margin —
    header underlines, code-block padding, table borders, and line
    wraps each add rows that are only visible after rendering.
    """
    measure_buf = io.StringIO()
    Console(
        file=measure_buf,
        force_terminal=True,
        color_system=None,
        width=console.size.width,
    ).print(Markdown(markdown_text))
    return measure_buf.getvalue().count("\n")


def _typewriter_render(console: Console, markdown_text: str) -> None:
    """Render Markdown progressively for a typewriter feel.

    The complete answer text is already in memory by the time this is
    called — the animation is purely cosmetic. Splits the text into
    word-plus-trailing-whitespace tokens and pushes them into a
    transient :class:`rich.live.Live` region whose renderable is the
    :class:`rich.markdown.Markdown` of the growing buffer. Once the
    animation finishes (or is interrupted with Ctrl+C), the live
    region is cleared and the complete Markdown is rendered as a
    static block.

    Skip path for tall answers: when the rendered Markdown would be
    taller than the terminal viewport, the typewriter is bypassed and
    the answer is printed statically right away. Reason: rich's
    transient :class:`Live` cleanup only clears the *visible* portion
    of its rendered region. As soon as content has scrolled past the
    top of the terminal during the animation, those scrolled-off
    rows stay in the terminal scrollback. The post-animation static
    print then duplicates them — the user would see the early
    sections of the answer twice. The cosmetic animation is not
    worth the duplication risk for long answers.

    Pressing Ctrl+C during the animation skips the rest of it; the
    full answer is still rendered as a static block afterwards.

    Tuning: see :data:`ANSWER_TYPEWRITER_BATCH` and
    :data:`ANSWER_TYPEWRITER_DELAY` for speed knobs.
    """
    tokens = re.findall(r"\S+\s*", markdown_text)
    if not tokens:
        console.print(Markdown(markdown_text))
        return

    # Leave a couple of rows for the next prompt and visual breathing
    # space. If the answer wouldn't fit, render statically only.
    rendered_lines = _measure_markdown_height(console, markdown_text)
    if rendered_lines >= console.size.height - 2:
        console.print(Markdown(markdown_text))
        return

    accumulated = ""
    try:
        with Live(
            Markdown(""),
            console=console,
            refresh_per_second=60,
            transient=True,
        ) as live:
            total = len(tokens)
            for i, token in enumerate(tokens, start=1):
                accumulated += token
                if i % ANSWER_TYPEWRITER_BATCH == 0 or i == total:
                    live.update(Markdown(accumulated))
                    if i < total:
                        time.sleep(ANSWER_TYPEWRITER_DELAY)
    except KeyboardInterrupt:
        # Skip-to-end: the animation is purely cosmetic, so a Ctrl+C
        # during it should not cancel the turn. Fall through to the
        # static render so the user keeps the complete answer on screen.
        pass

    console.print(Markdown(markdown_text))


def _run_turn(
    agent: ResearchAgent,
    question: str,
    history: str,
    console: Console,
) -> str:
    """Stream one research turn and render the result.

    Progress lines are shown in a bounded, in-place "ticker" viewport
    while the agent runs: the most recent
    :data:`PROGRESS_VIEWPORT_LINES` messages stay visible, older ones
    scroll out the top with a colour fade, and a spinner header
    confirms the agent is still active. When the run completes the
    viewport disappears (``transient=True``) and is replaced by a
    one-line summary plus the rendered Markdown answer.

    Returns the final answer text so the caller can append it to the
    running history. Raises whatever ``agent.stream`` raises so the
    loop can decide how to react (cancel, timeout, rate limit). On
    exceptions the live region is cleaned up by ``Live.__exit__``;
    no summary line is printed because the turn never completed.
    """
    answer_buf: list[str] = []
    progress_lines: deque[str] = deque(maxlen=PROGRESS_VIEWPORT_LINES)
    progress_log: list[str] = []
    in_answer = False

    spinner = Spinner(
        "dots",
        text=Text("Researching", style="bold cyan"),
        style="cyan",
    )

    t0 = time.monotonic()
    with Live(
        _render_ticker(progress_lines, spinner),
        console=console,
        refresh_per_second=12,
        transient=True,
    ) as live:
        for chunk in agent.stream(question, history=history, include_progress=True):
            if not in_answer and chunk == "---\n":
                in_answer = True
                continue
            if in_answer:
                answer_buf.append(chunk)
                continue

            # Progress chunks have the shape "> <message>\n" — strip the
            # prefix so the ticker shows the message body only. Older
            # entries fall out of the deque automatically when the
            # newest one comes in beyond MAX_HISTORY_TURNS.
            text = chunk[2:].rstrip("\n") if chunk.startswith("> ") else chunk.rstrip("\n")
            if text:
                progress_lines.append(text)
                progress_log.append(text)
                live.update(_render_ticker(progress_lines, spinner))

    elapsed = time.monotonic() - t0

    full_answer = "".join(answer_buf).strip()
    if full_answer:
        console.print(_extract_summary(progress_log, elapsed))
        console.print()
        _typewriter_render(console, full_answer)
    return full_answer


def main() -> None:
    """Run the interactive chat loop until the user exits."""
    console = Console()
    agent = _build_agent()

    # Each entry is a (question, answer) pair from a completed turn.
    turns: list[tuple[str, str]] = []

    while True:
        try:
            if _HAS_READLINE:
                raw = input(PROMPT).strip()
            else:
                # No readline / pyreadline3 → no cursor-width tracking,
                # so rich's styled console.input works correctly here
                # and the \001/\002-wrapped PROMPT would print its
                # markers as literal control bytes.
                raw = console.input("\n[bold cyan]>>> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            console.print("Bye.")
            return

        if not raw:
            continue

        lowered = raw.lower()
        if lowered in EXIT_COMMANDS:
            console.print("Bye.")
            return
        if lowered in HELP_COMMANDS:
            _print_help(console)
            continue
        if lowered in CLEAR_COMMANDS:
            turns.clear()
            console.print("[green]History cleared.[/green]")
            continue

        history = _format_history(turns)
        try:
            answer = _run_turn(agent, raw, history, console)
        except KeyboardInterrupt:
            console.print()
            console.print("[yellow]Turn cancelled.[/yellow]")
            continue
        except AgentTimeout as exc:
            console.print(Panel(
                f"The research run hit the wall-clock deadline: {exc}",
                title="Timeout",
                border_style="yellow",
                expand=False,
            ))
            continue
        except AgentRateLimited as exc:
            console.print(Panel(
                f"The provider returned a rate-limit error: {exc}\n"
                "Try again later or lower request volume.",
                title="Rate limit",
                border_style="yellow",
                expand=False,
            ))
            continue
        except Exception as exc:
            console.print(Panel(
                f"{type(exc).__name__}: {exc}",
                title="Error",
                border_style="red",
                expand=False,
            ))
            continue

        if answer:
            turns.append((raw, answer))


if __name__ == "__main__":
    main()
