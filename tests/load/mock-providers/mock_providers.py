"""Provider stand-in that reproduces real call timing without provider cost.

Load measurement needs many concurrent runs. Driving them against real
providers is expensive and rate-limited, and provider latency variance would
dominate the numbers being measured. This server answers the LLM, web-search,
and embedding calls instead, so a load run exercises the application's own
threads, connections, and CPU while provider behaviour stays a controlled
input.

Fidelity rests on two artifacts produced by ``calibrate.py`` from real run
traces:

* a timing profile — per-operation p50/p95/max, sampled per request from a
  lognormal fitted to p50/p95 and clamped at the observed maximum, so the
  spread of real provider latency survives;
* a response corpus — the captured answers themselves, replayed verbatim so
  every response keeps the exact wire format and size the pipeline parses;
* recorded conversations of tool-calling operations: a request that offers
  tools gets the model turn at its position in the conversation, with the tool
  calls as they were recorded. Identifiers in the arguments are rebound to ones
  this request actually holds, so the tool work really happens.

Search results are the one synthesised payload: search spans carry no captured
result set. Extracted claims reference results by position rather than by URL,
so synthesised results still resolve.

An incoming request is mapped back to its operation by the opening of its
system instruction, which is unique per pipeline step. Matching is
longest-common-prefix rather than exact because some instructions embed the
current date. A request that matches nothing is answered from a neutral
default AND reported — a silent fallback would quietly turn a measured run
into a meaningless one.

Configuration (all optional):

``INQTRIX_MOCK_PORT``            listen port (default 9300)
``INQTRIX_MOCK_PROFILE``         timing profile path
``INQTRIX_MOCK_CORPUS``          response corpus directory
``INQTRIX_MOCK_EMBEDDING_DIM``   embedding width (default 3072)
``INQTRIX_MOCK_SEARCH_RESULTS``  synthesised results per search (default 8)
``INQTRIX_MOCK_SEED``            base seed for reproducible sampling
``INQTRIX_MOCK_LATENCY_SCALE``   multiplies every sampled latency (default 1.0)
``INQTRIX_MOCK_SELECT``          JSON: operation -> regex the reply must match

``INQTRIX_MOCK_SELECT`` exists because a captured corpus contains every shape a
step really produced, and some of those shapes change how much work the run
does at all: one classify answer decides against searching and collapses the
whole pipeline, and the evaluate answer's confidence decides whether a second
round happens. Replaying that distribution faithfully is right when measuring
capacity under representative load, and wrong when comparing two configurations,
where run depth must be held constant so the difference is attributable. The
selector makes that choice explicit and reports it, rather than leaving run
depth to chance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import threading
import time
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from prompt_routing import ROUTING_VERSION, assignment_of, request_schema_name, signature, system_prompt, tier_of

log = logging.getLogger("inqtrix.mock-providers")

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROFILE_PATH = os.environ.get(
    "INQTRIX_MOCK_PROFILE", os.path.join(_HERE, "profiles", "azure-calibrated.json")
)
_CORPUS_DIR = os.environ.get("INQTRIX_MOCK_CORPUS", os.path.join(_HERE, "corpus"))
_EMBEDDING_DIM = int(os.environ.get("INQTRIX_MOCK_EMBEDDING_DIM", "3072"))
_SEARCH_RESULTS = int(os.environ.get("INQTRIX_MOCK_SEARCH_RESULTS", "8"))
_SEED = int(os.environ.get("INQTRIX_MOCK_SEED", "1"))
_LATENCY_SCALE = float(os.environ.get("INQTRIX_MOCK_LATENCY_SCALE", "1.0"))


def _load_selectors() -> dict[str, str]:
    """Return the configured operation -> reply-pattern selection policy."""
    raw = os.environ.get("INQTRIX_MOCK_SELECT", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"INQTRIX_MOCK_SELECT is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("INQTRIX_MOCK_SELECT must be a JSON object.")
    return {str(key): str(value) for key, value in parsed.items()}


_SELECTORS = _load_selectors()

# Operation used when nothing matches. Its timing is deliberately unremarkable
# so an unnoticed fallback cannot masquerade as a fast provider.
_FALLBACK_LATENCY = {"p50": 3.0, "p95": 8.0, "max": 15.0}

_SEARCH_CHANNEL = "web_search"
_SEARCH_OPERATION = f"research.web_search.{_SEARCH_CHANNEL}"

# Run-specific identifiers in recorded tool arguments, by their product prefix.
_IDENTIFIER = re.compile(r"\b(ref|art|kd|kc)_[0-9a-zA-Z_]{6,}\b")
# Evidence labels: one per bracket as a request shows them, or grouped as a reply may.
_KNOWN_CITATION = re.compile(r"\[([KWE][0-9]+)\]")
_CITATION = re.compile(r"(?<!\\)\[[ \t]*[KWE][0-9]+(?:[ \t]*[,;][ \t]*[KWE][0-9]+)*[ \t]*\]")
_CITATION_LABEL = re.compile(r"[KWE][0-9]+")
# A list of labels in a typed reply or a tool argument, without brackets.
_LABEL_FIELD = re.compile(r'"([KWE][0-9]+)"')
_LABEL_ONLY = re.compile(r"[KWE][0-9]+")
# A knowledge prompt's excerpts, and the quote lines of an answer to it.
_EVIDENCE_BLOCK = re.compile(r'<untrusted_content source="knowledge-evidence">(.*?)</untrusted_content>', re.DOTALL)
_EVIDENCE_ENTRY = re.compile(r"^\[(K[0-9]+)\] [^\n]*\n(.*?)(?=^\[K[0-9]+\] |\Z)", re.DOTALL | re.MULTILINE)
_QUOTE_LINE = re.compile(r"^[ \t]*\[(K[0-9]+)\][ \t]*(.+?)[ \t]*$", re.MULTILINE)
_QUOTE_CHARS = "\"'„“”»«‚‘’"
_QUOTE_MIN_CHARS = 15
_ASSIGNMENT_PREFIX = 60
_EVIDENCE_ERROR = "Evidence error"


class Catalogue:
    """Timing profile, response corpus, and routing table for one profile."""

    def __init__(self, profile_path: str, corpus_dir: str) -> None:
        self.operations: dict[str, dict] = {}
        self.responses: dict[str, list[dict]] = {}
        self.conversations: dict[str, list[dict]] = {}
        self.signatures: list[tuple[str, str]] = []
        self.degraded: list[str] = []

        if os.path.exists(profile_path):
            document = json.load(open(profile_path, encoding="utf-8"))
            self.operations = document.get("operations") or {}
        else:
            self.degraded.append(
                f"timing profile {profile_path!r} missing — every call uses "
                "the neutral fallback latency"
            )

        manifest_path = os.path.join(corpus_dir, "manifest.json")
        if os.path.exists(manifest_path):
            manifest = json.load(open(manifest_path, encoding="utf-8"))
            if manifest.get("routing_version") == ROUTING_VERSION:
                for operation, values in (manifest.get("routing_signatures") or {}).items():
                    for value in values:
                        self.signatures.append((str(value), operation))
            else:
                self.degraded.append("routing format changed; regenerate the corpus with calibrate.py")
            for operation in manifest.get("responses_per_operation") or {}:
                path = os.path.join(corpus_dir, f"{operation}.json")
                if os.path.exists(path):
                    self.responses[operation] = json.load(open(path, encoding="utf-8"))
            for operation in manifest.get("conversations_per_operation") or {}:
                path = os.path.join(corpus_dir, f"{operation}.conversations.json")
                if os.path.exists(path):
                    self.conversations[operation] = json.load(open(path, encoding="utf-8"))
        else:
            self.degraded.append(
                f"response corpus {corpus_dir!r} missing — responses are "
                "synthetic and will not match real formats or sizes"
            )
        owners: dict[str, set[str]] = defaultdict(set)
        for value, operation in self.signatures:
            owners[value].add(operation)
        collisions = sum(len(operations) > 1 for operations in owners.values())
        if collisions:
            self.degraded.append(f"{collisions} ambiguous routing signatures; those requests stay unmatched")
        self.selection = self._apply_selectors()

    def _apply_selectors(self) -> dict[str, dict]:
        """Restrict each operation's replies to the configured pattern.

        A selector that matches nothing is fatal rather than ignored: serving
        the unfiltered pool instead would silently measure a different
        workload than the one that was asked for.
        """
        applied: dict[str, dict] = {}
        for operation, pattern in _SELECTORS.items():
            pool = self.responses.get(operation)
            if pool is None:
                raise SystemExit(
                    f"INQTRIX_MOCK_SELECT names operation {operation!r}, which "
                    f"has no captured responses. Known operations: "
                    f"{', '.join(sorted(self.responses)) or 'none'}"
                )
            expression = re.compile(pattern)
            kept = [entry for entry in pool if expression.search(entry.get("content") or "")]
            if not kept:
                raise SystemExit(
                    f"INQTRIX_MOCK_SELECT pattern {pattern!r} matches none of "
                    f"the {len(pool)} captured responses for {operation!r}."
                )
            self.responses[operation] = kept
            applied[operation] = {
                "pattern": pattern,
                "kept": len(kept),
                "of": len(pool),
            }
        return applied

    def resolve(self, prompt: str, *, schema_name: str = "") -> tuple[str, int]:
        """Return an operation only when one exact request identity owns it."""
        probe = signature(prompt, schema_name=schema_name)
        matches = {operation for candidate, operation in self.signatures if candidate == probe}
        if len(matches) != 1:
            return "", 0
        return matches.pop(), len(probe)

    def latency(self, operation: str) -> dict:
        """Return the latency quantiles recorded for *operation*."""
        entry = self.operations.get(operation) or {}
        return entry.get("latency_seconds") or _FALLBACK_LATENCY

    def tokens(self, operation: str) -> tuple[int, int]:
        """Return median prompt and completion token counts."""
        entry = self.operations.get(operation) or {}
        return (
            int(entry.get("prompt_tokens_median") or 0),
            int(entry.get("completion_tokens_median") or 0),
        )

    def model(self, operation: str) -> str:
        """Return a model identifier recorded for *operation*."""
        models = (self.operations.get(operation) or {}).get("models") or []
        return str(models[0]) if models else "mock-model"


class Stats:
    """Per-operation call counters and unmatched-request reporting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: dict[str, int] = defaultdict(int)
        self.slept_seconds: dict[str, float] = defaultdict(float)
        self.unmatched: dict[str, int] = defaultdict(int)
        self.synthesised: dict[str, int] = defaultdict(int)
        self.tool_turns: dict[str, int] = defaultdict(int)
        self.tool_calls_dropped: dict[str, int] = defaultdict(int)
        self.in_flight = 0
        self.in_flight_per_operation: dict[str, int] = defaultdict(int)
        self.disconnected = 0
        self.evidence_errors = 0
        self._ordinals: dict[str, int] = defaultdict(int)
        self.started = time.time()

    def next_ordinal(self, operation: str) -> int:
        """Return the 1-based position of this call within its operation."""
        with self._lock:
            self._ordinals[operation] += 1
            return self._ordinals[operation]

    def record_synthesised(self, operation: str) -> None:
        """Count a reply built on the spot instead of replayed.

        A synthesised reply has the right shape but not the size or wording
        of a real one, so a measurement that leans on it is weaker than one
        that replays. Counting them keeps that visible in the report rather
        than leaving it to be inferred.
        """
        with self._lock:
            self.synthesised[operation] += 1

    def record_tool_turn(self, operation: str, dropped: int) -> None:
        """Count a replayed model turn and the recorded calls it could not make."""
        with self._lock:
            self.tool_turns[operation] += 1
            if dropped:
                self.tool_calls_dropped[operation] += dropped

    def enter(self, operation: str) -> None:
        """Count a provider call that is holding its client's connection."""
        with self._lock:
            self.in_flight += 1
            self.in_flight_per_operation[operation] += 1

    def leave(self, operation: str, *, disconnected: bool) -> None:
        """Count the end of a held call, and whether the client hung up first."""
        with self._lock:
            self.in_flight -= 1
            self.in_flight_per_operation[operation] -= 1
            self.disconnected += int(disconnected)

    def record_evidence_error(self) -> None:
        """Count a replayed document the application refused for its citations."""
        with self._lock:
            self.evidence_errors += 1

    def record(self, operation: str, seconds: float) -> None:
        """Count one served call and the delay it introduced."""
        with self._lock:
            self.calls[operation] += 1
            self.slept_seconds[operation] += seconds

    def record_unmatched(self, probe: str, score: int) -> bool:
        """Count an unrecognised instruction; report whether it is new."""
        digest = hashlib.sha256(probe.encode("utf-8")).hexdigest()
        with self._lock:
            first = digest not in self.unmatched
            self.unmatched[digest] += 1
        if first:
            log.warning(
                "Unrecognised or ambiguous request signature %s (match %d) — "
                "served from the neutral fallback. Timing for this call is NOT calibrated.",
                digest,
                score,
            )
        return first

    def snapshot(self) -> dict:
        """Return a JSON-serialisable view of every counter."""
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self.started, 1),
                "total_calls": sum(self.calls.values()),
                "calls_per_operation": dict(sorted(self.calls.items())),
                "delay_seconds_per_operation": {
                    key: round(value, 1)
                    for key, value in sorted(self.slept_seconds.items())
                },
                "unmatched_instructions": dict(sorted(self.unmatched.items())),
                # Replies built on the spot rather than replayed: shape is
                # right, size and wording are not.
                "synthesised_replies": dict(sorted(self.synthesised.items())),
                "tool_turns": dict(sorted(self.tool_turns.items())),
                "tool_calls_dropped": dict(sorted(self.tool_calls_dropped.items())),
                # Live connections, so a cancel can be timed to the provider socket.
                "in_flight": self.in_flight,
                "in_flight_per_operation": {
                    key: value
                    for key, value in sorted(self.in_flight_per_operation.items())
                    if value
                },
                "disconnected": self.disconnected,
                # A replayed write the application refused: the run goes on without
                # the document, so its writes are missing from what this run measures.
                "evidence_errors": self.evidence_errors,
            }


CATALOGUE = Catalogue(_PROFILE_PATH, _CORPUS_DIR)
STATS = Stats()
app = FastAPI(title="Inqtrix load-measurement provider stand-in")


def _sample_latency(operation: str, ordinal: int) -> float:
    """Return a per-call delay drawn from the operation's recorded spread.

    A lognormal is fitted so that its median is p50 and its 95th percentile
    is p95, then clamped at the observed maximum.

    The draw is seeded from the operation and the call's ORDINAL, not from
    the request. Seeding it from the request would give every repetition of
    the same call the same delay, so the dominant cost of a run would carry
    no spread at all and concurrent runs would advance in lockstep, both of
    which are artifacts rather than provider behaviour. An ordinal keeps a
    whole load run reproducible while letting two identical simultaneous
    calls differ the way two real provider calls do.
    """
    quantiles = CATALOGUE.latency(operation)
    p50 = max(float(quantiles.get("p50") or 0.0), 0.001)
    p95 = max(float(quantiles.get("p95") or p50), p50)
    ceiling = max(float(quantiles.get("max") or p95), p95)
    sigma = (math.log(p95) - math.log(p50)) / 1.6449 if p95 > p50 else 0.0
    seed = f"{_SEED}:{operation}:{ordinal}"
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).hexdigest())
    value = p50 * math.exp(rng.gauss(0.0, sigma)) if sigma > 0 else p50
    return max(0.0, min(value, ceiling)) * _LATENCY_SCALE


async def _serve(operation: str, request: Request | None = None) -> float:
    """Delay for the operation's sampled latency and count the call.

    The delay watches the connection: a client that hangs up ends it, and the
    in-flight count shows how long a cancelled run really held its provider call.
    """
    ordinal = STATS.next_ordinal(operation)
    seconds = _sample_latency(operation, ordinal)
    STATS.record(operation, seconds)
    STATS.enter(operation)
    disconnected = False
    try:
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(remaining, 0.25))
            if request is not None and await request.is_disconnected():
                disconnected = True
                break
    finally:
        STATS.leave(operation, disconnected=disconnected)
    return seconds


def _schema_instance(schema: Any, salt: str, depth: int = 0) -> Any:
    """Build a minimal instance satisfying *schema*.

    Some operations cannot be replayed from a capture because their reply
    shape depends on the request: chunk contextualisation must return
    exactly as many entries as the batch carried, and the caller rejects
    any other count. Generating from the request's own schema is the only
    way those calls can complete at all.
    """
    if not isinstance(schema, dict) or depth > 8:
        return f"mock-{salt[:8]}"
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties") or {}
        return {
            name: _schema_instance(sub, salt, depth + 1)
            for name, sub in properties.items()
        }
    if kind == "array":
        count = int(schema.get("minItems") or 1) or 1
        item = schema.get("items") or {}
        built = []
        for index in range(count):
            entry = _schema_instance(item, f"{salt}{index}", depth + 1)
            # Positional identifiers are validated against the request, so a
            # constant would fail the caller's ordering check.
            if isinstance(entry, dict):
                for key in ("chunk_number", "index", "id", "number"):
                    if key in entry and isinstance(entry[key], int):
                        entry[key] = index + 1
            built.append(entry)
        return built
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    # Long enough to satisfy a minLength guard and to carry realistic bulk.
    return f"Synthetischer Kontext {salt[:8]} zur Einordnung des Abschnitts."


def _pick_response(operation: str, salt: str, schema: Any = None) -> dict:
    """Return a reply for *operation*, replayed when captured, else built."""
    pool = CATALOGUE.responses.get(operation) or []
    if pool:
        digest = hashlib.sha256(f"{_SEED}:{operation}:{salt}".encode("utf-8")).digest()
        return pool[int.from_bytes(digest[:4], "big") % len(pool)]
    prompt_tokens, completion_tokens = CATALOGUE.tokens(operation)
    if schema is not None:
        content = json.dumps(_schema_instance(schema, salt), ensure_ascii=False)
        STATS.record_synthesised(operation)
        return {
            "content": content,
            "prompt_tokens": prompt_tokens or len(content) // 4,
            "completion_tokens": completion_tokens or len(content) // 4,
        }
    STATS.record_synthesised(operation)
    return {
        "content": "",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def _offered_tools(body: dict) -> set[str]:
    """Return the tool names a chat request offers the model."""
    names = set()
    for tool in body.get("tools") or []:
        if isinstance(tool, dict):
            names.add(str((tool.get("function") or {}).get("name") or tool.get("name") or ""))
    return names - {""}


def _first_user_text(messages: list) -> str:
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _choose_conversation(pool: list[dict], offered: set[str], user_text: str) -> dict:
    """Pick the recorded conversation for a request, stable across its turns.

    Only a recording whose tools this request offers can be replayed. Among those
    the same assignment in the same tier comes first, then the same tier, and the
    choice among equals follows the unchanging first user message.
    """
    fitting = [
        entry for entry in pool
        if all(call["name"] in offered for turn in entry["turns"] for call in turn.get("tool_calls") or [])
    ] or pool
    tier = tier_of(user_text)
    assignment = assignment_of(user_text)
    same_tier = [entry for entry in fitting if entry.get("tier") == tier] or fitting
    same_task = [
        entry for entry in same_tier
        if assignment and entry.get("assignment") and assignment.startswith(str(entry["assignment"])[:_ASSIGNMENT_PREFIX])
    ] or same_tier
    digest = hashlib.sha256(f"{_SEED}:{user_text}".encode("utf-8")).digest()
    return same_task[int.from_bytes(digest[:4], "big") % len(same_task)]


def _rebind(value: Any, held: dict[str, list[str]], ordinal: int) -> tuple[Any, bool]:
    """Point recorded identifiers at ones this request holds; report any it lacks."""
    if isinstance(value, dict):
        rebound = {key: _rebind(item, held, ordinal) for key, item in value.items()}
        return {key: item[0] for key, item in rebound.items()}, all(item[1] for item in rebound.values())
    if isinstance(value, list):
        rebound_items = [_rebind(item, held, ordinal + index) for index, item in enumerate(value)]
        return [item[0] for item in rebound_items], all(item[1] for item in rebound_items)
    if isinstance(value, str):
        match = _IDENTIFIER.fullmatch(value)
        if match:
            candidates = held.get(match.group(1)) or []
            if not candidates:
                return value, False
            return candidates[ordinal % len(candidates)], True
    return value, True


def _conversation_turn(operation: str, body: dict, salt: str) -> dict | None:
    """Return the recorded model turn for a tool-offering request, as wire fields."""
    offered = _offered_tools(body)
    pool = CATALOGUE.conversations.get(operation)
    if not offered or not pool:
        return None
    messages = body.get("messages") or []
    position = sum(1 for message in messages if isinstance(message, dict) and message.get("role") == "assistant")
    conversation = _choose_conversation(pool, offered, _first_user_text(messages))
    turns = conversation["turns"]
    held: dict[str, list[str]] = defaultdict(list)
    for match in _IDENTIFIER.finditer(json.dumps(messages, ensure_ascii=False)):
        if match.group(0) not in held[match.group(1)]:
            held[match.group(1)].append(match.group(0))
    calls: list[dict] = []
    dropped = 0
    content = ""
    turn = turns[-1]
    # A turn whose calls this request cannot make is skipped, not turned into an
    # answer: a role run must still reach the typed result its recording ended with.
    for ahead, turn in enumerate(turns[position:]):
        for index, call in enumerate(turn.get("tool_calls") or []):
            arguments, resolved = _rebind(call.get("arguments") or {}, held, index)
            if call["name"] not in offered or not resolved:
                dropped += 1
                continue
            arguments = _fit_arguments(arguments, _known_labels(messages))
            calls.append({
                "id": f"call_{salt[:12]}_{position + ahead}_{index}",
                "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(arguments, ensure_ascii=False)},
            })
        content = turn.get("content") or ""
        if calls or content:
            break
    STATS.record_tool_turn(operation, dropped)
    if not calls and not content:
        # Nothing callable is left: end with the recorded close rather than an
        # empty reply the caller would retry.
        content = next((t.get("content") for t in reversed(turns) if t.get("content")), "") or "Erledigt."
    return {
        "message": {"role": "assistant", "content": content or None, **({"tool_calls": calls} if calls else {})},
        "finish_reason": "tool_calls" if calls else "stop",
        "prompt_tokens": int(turn.get("prompt_tokens") or 0),
        "completion_tokens": int(turn.get("completion_tokens") or 0),
    }


def _known_labels(messages: Any) -> dict[str, list[str]]:
    """Return the evidence labels a request shows, by family; instructions show none."""
    known: dict[str, list[str]] = defaultdict(list)
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        for label in _KNOWN_CITATION.findall(str(message.get("content") or "")):
            if label not in known[label[0]]:
                known[label[0]].append(label)
    return known


def _fit_citations(content: str, known: dict[str, list[str]]) -> str:
    """Point the citations of a recorded reply at evidence this request knows.

    A recording cites the evidence of its own run. Replayed as it is, those labels
    are unknown here and send the caller down a citation repair no recorded run
    took, which adds a provider call and tells nothing about the real system. The
    choice among known labels is stable, and with none left the citation goes.
    """

    def fit(label: str) -> str | None:
        family = known.get(label[0])
        if not family or label in family:
            return label if family else None
        return family[int(label[1:]) % len(family)]

    def fitted(match: re.Match[str]) -> str:
        labels = [fit(label) for label in _CITATION_LABEL.findall(match.group(0))]
        return " ".join(f"[{label}]" for label in labels if label)

    def fitted_field(match: re.Match[str]) -> str:
        label = fit(match.group(1))
        return f'"{label}"' if label else match.group(0)

    return _LABEL_FIELD.sub(fitted_field, _CITATION.sub(fitted, content))


def _fit_arguments(value: Any, known: dict[str, list[str]]) -> Any:
    """Fit the citations in recorded tool arguments, such as a document's text."""
    if isinstance(value, dict):
        return {key: _fit_arguments(item, known) for key, item in value.items()}
    if isinstance(value, list):
        return [_fit_arguments(item, known) for item in value]
    if isinstance(value, str):
        return _fit_citations(f'"{value}"', known)[1:-1] if _LABEL_ONLY.fullmatch(value) else _fit_citations(value, known)
    return value


def _fit_quotes(content: str, messages: Any) -> str:
    """Make the quotes of a recorded knowledge answer verbatim in this request's excerpts.

    The caller publishes an answer only if every quote stands in the excerpt its
    label names, and the synthetic embeddings retrieve other excerpts than the
    recorded run saw. Replayed as it is, every such answer would fail as ungrounded
    and the run would measure a failure no real run had. The answer text stays
    recorded; only what it quotes comes from here.
    """
    head, marker, tail = content.partition("ANSWER:")
    if not marker or "QUOTES:" not in head:
        return content
    excerpts: dict[str, str] = {}
    for message in messages or []:
        text = str(message.get("content") or "") if isinstance(message, dict) else ""
        for block in _EVIDENCE_BLOCK.findall(text):
            for entry in _EVIDENCE_ENTRY.finditer(block):
                excerpts[entry.group(1)] = entry.group(2)
    if not excerpts:
        return content

    def fitted(match: re.Match[str]) -> str:
        label = match.group(1)
        quote = match.group(2).strip(_QUOTE_CHARS).strip()
        excerpt = excerpts.get(label)
        if excerpt is None:
            label, excerpt = next(iter(excerpts.items()))
        if quote and quote in excerpt:
            return match.group(0)
        lines = [line.strip() for line in excerpt.splitlines() if line.strip()]
        sentences = [line for line in lines if len(line) >= _QUOTE_MIN_CHARS and not line.startswith("#")] or lines
        return f'[{label}] "{sentences[len(quote) % len(sentences)]}"' if sentences else ""

    return _QUOTE_LINE.sub(fitted, head) + marker + tail


def _note_refused_write(messages: Any) -> None:
    """Count when the application has just refused a replayed document."""
    last = messages[-1] if isinstance(messages, list) and messages else None
    if isinstance(last, dict) and last.get("role") == "tool" and _EVIDENCE_ERROR in str(last.get("content") or ""):
        STATS.record_evidence_error()


def _request_salt(body: dict) -> str:
    """Return a stable per-request salt derived from the whole request.

    The entire body is hashed. Truncating it would collapse distinct
    requests onto one salt: the pipeline's largest prompts share a long
    constant preamble and differ only in the evidence appended far beyond
    any short prefix, so a prefix hash would hand every one of them the
    same reply and the same delay.
    """
    digest = hashlib.sha256()
    digest.update(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()[:16]


async def _read_json(request: Request) -> dict:
    """Return the request body as a dict, tolerating an empty body."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a client problem
        return {}
    return body if isinstance(body, dict) else {}


def _resolve(body: dict) -> tuple[str, str]:
    """Return the operation for *body* and the salt identifying the request."""
    salt = _request_salt(body)
    schema_name = request_schema_name(body)
    prompt = system_prompt(body.get("messages") or body.get("input"), schema_name=schema_name)
    operation, score = CATALOGUE.resolve(prompt, schema_name=schema_name)
    if not operation:
        STATS.record_unmatched(signature(prompt, schema_name=schema_name), score)
        operation = "unmatched"
    return operation, salt


async def _chat_answer(body: dict, request: Request) -> dict:
    """The Chat Completions answer to *body*, from the catalogue."""
    operation, salt = _resolve(body)
    _note_refused_write(body.get("messages"))
    await _serve(operation, request)
    turn = _conversation_turn(operation, body, salt)
    # A structured request carries the shape its caller will validate. Pass it
    # through: some operations cannot be replayed at all because the reply
    # length depends on the request, and the caller rejects any other count.
    schema = (
        ((body.get("response_format") or {}).get("json_schema") or {}).get("schema")
    )
    captured = turn or _pick_response(operation, salt, schema)
    message = (turn or {}).get("message") or {"role": "assistant", "content": captured.get("content") or ""}
    if message.get("content"):
        fitted = _fit_citations(message["content"], _known_labels(body.get("messages")))
        message = {**message, "content": _fit_quotes(fitted, body.get("messages"))}
    return {
        "id": f"chatcmpl-{salt}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model") or CATALOGUE.model(operation),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": (turn or {}).get("finish_reason") or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": int(captured.get("prompt_tokens") or 0),
            "completion_tokens": int(captured.get("completion_tokens") or 0),
            "total_tokens": int(captured.get("prompt_tokens") or 0)
            + int(captured.get("completion_tokens") or 0),
        },
    }


@app.post("/azure/openai/v1/chat/completions")
@app.post("/openai/v1/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """Answer an OpenAI-compatible chat completion."""
    return JSONResponse(await _chat_answer(await _read_json(request), request))


def _input_text(content: Any) -> str:
    """The text of a Responses input content: a string or its text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
        )
    return ""


def _chat_from_responses(body: dict) -> dict:
    """The Chat Completions request that asks what a Responses request asks.

    The models whose cards name the Responses API (GPT-6, GPT-5.6) send their
    turns as input items. The stand-in answers both forms from one catalogue,
    so the operation, the recorded tool turns and the structured shapes are
    the same as on the Chat Completions path: a message item becomes a
    message, the calls of one answer join one assistant message, a call
    output becomes a tool message, and reasoning items carry nothing to match.
    """
    messages: list[dict] = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": str(body["instructions"])})
    for item in body.get("input") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or ("message" if item.get("role") else "")
        if kind == "message":
            role = str(item.get("role") or "user")
            messages.append({
                "role": "system" if role == "developer" else role,
                "content": _input_text(item.get("content")),
            })
        elif kind == "function_call":
            call = {
                "id": str(item.get("call_id") or ""),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or ""),
                },
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": _input_text(item.get("output")),
            })
    chat: dict = {"model": body.get("model"), "messages": messages}
    tools = [
        {"type": "function", "function": {key: value for key, value in tool.items() if key != "type"}}
        for tool in body.get("tools") or []
        if isinstance(tool, dict) and tool.get("type") == "function"
    ]
    if tools:
        chat["tools"] = tools
    text_format = (body.get("text") or {}).get("format") or {}
    if text_format.get("type") == "json_schema":
        chat["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": text_format.get("name"),
                "schema": text_format.get("schema"),
                "strict": text_format.get("strict"),
            },
        }
    return chat


def _response_from_chat(answer: dict, request: dict) -> dict:
    """The Responses answer that says what the Chat Completions *answer* says.

    It carries every field the API always sends (the SDK's model requires
    them), echoing the request's tools and tool choice as the API does.
    """
    choice = answer["choices"][0]
    message = choice.get("message") or {}
    salt = str(answer["id"]).removeprefix("chatcmpl-")
    output: list[dict] = []
    if message.get("content"):
        output.append({
            "type": "message",
            "id": f"msg_{salt}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": message["content"], "annotations": []}],
        })
    for call in message.get("tool_calls") or []:
        output.append({
            "type": "function_call",
            "id": f"fc_{call['id']}",
            "call_id": call["id"],
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"],
            "status": "completed",
        })
    usage = answer["usage"]
    response = {
        "id": f"resp_{salt}",
        "object": "response",
        "created_at": answer["created"],
        "model": answer["model"],
        "status": "completed",
        "output": output,
        "parallel_tool_calls": bool(request.get("parallel_tool_calls", True)),
        "tool_choice": request.get("tool_choice") or "auto",
        "tools": list(request.get("tools") or []),
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    if choice.get("finish_reason") == "length":
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


@app.post("/azure/openai/v1/responses")
@app.post("/openai/v1/responses")
async def responses(request: Request) -> JSONResponse:
    """Answer an OpenAI Responses request (the GPT-6 and GPT-5.6 cards)."""
    body = await _read_json(request)
    chat = _chat_from_responses(body)
    return JSONResponse(_response_from_chat(await _chat_answer(chat, request), body))


@app.post("/azure-embeddings/openai/deployments/{deployment}/embeddings")
@app.post("/v1/embeddings")
async def embeddings(request: Request, deployment: str = "mock") -> JSONResponse:
    """Answer an embedding request with one deterministic vector per input.

    The caller requires exactly one vector per input, in order, at the
    collection's fixed width; anything else fails before the load run
    produces a number.
    """
    body = await _read_json(request)
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        inputs = [raw_input]
    elif isinstance(raw_input, list):
        inputs = raw_input
    else:
        inputs = []

    data = []
    for index, item in enumerate(inputs):
        digest = hashlib.sha256(str(item).encode("utf-8")).digest()
        rng = random.Random(digest)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(_EMBEDDING_DIM)]
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        data.append(
            {
                "object": "embedding",
                "index": index,
                "embedding": [value / norm for value in vector],
            }
        )
    STATS.record("embedding", 0.0)
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": body.get("model") or deployment,
            "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
        }
    )


def _search_results(salt: str, count: int) -> list[dict]:
    """Return synthesised search results of realistic size and shape."""
    rng = random.Random(f"{_SEED}:search:{salt}")
    results = []
    for index in range(1, count + 1):
        token = rng.randrange(16**8)
        results.append(
            {
                "id": index,
                "url": f"https://example.test/{salt[:8]}/{token:08x}",
                "title": f"Quelle {index} zu {salt[:6]}",
                # Snippet length drives downstream claim-extraction prompt
                # size, so it is generated at a realistic scale.
                "snippet": " ".join(f"beleg{token:x}{n}" for n in range(120)),
                "date": "2026-01-01",
                "last_updated": "2026-01-01",
            }
        )
    return results


def _search_usage() -> dict[str, int]:
    """Use captured inclusive token counts; defaults only serve an absent profile."""
    prompt_tokens, completion_tokens = (
        CATALOGUE.tokens(_SEARCH_OPERATION)
        if _SEARCH_OPERATION in CATALOGUE.operations
        else (32, 128)
    )
    return {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}


@app.post("/perplexity/v1/responses")
@app.post("/v1/responses")
async def perplexity_responses(request: Request) -> JSONResponse:
    """Answer a Perplexity-compatible search request."""
    body = await _read_json(request)
    salt = _request_salt(body)
    operation = _SEARCH_OPERATION
    await _serve(operation, request)
    results = _search_results(salt, _SEARCH_RESULTS)
    citations = " ".join(f"[{entry['id']}]" for entry in results)
    return JSONResponse(
        {
            "id": f"resp-{salt}",
            "model": body.get("model") or "mock-search",
            "output": [
                {"type": "search_results", "results": results},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"Zusammenfassung der Treffer. {citations}",
                        }
                    ],
                },
            ],
            "usage": _search_usage(),
        }
    )


@app.post("/foundry/openai/v1/responses")
@app.post("/api/projects/{project}/openai/v1/responses")
async def foundry_responses(request: Request, project: str = "mock") -> JSONResponse:
    """Answer an Azure AI Foundry web-search request."""
    body = await _read_json(request)
    salt = _request_salt(body)
    operation = _SEARCH_OPERATION
    await _serve(operation, request)
    results = _search_results(salt, _SEARCH_RESULTS)
    # Markdown links are the citation fallback the caller harvests when no
    # structured annotation is present, so links carry the sources here.
    prose = " ".join(f"[{entry['title']}]({entry['url']})" for entry in results)
    return JSONResponse(
        {
            "id": f"resp-{salt}",
            "object": "response",
            "created_at": int(time.time()),
            "model": body.get("model") or "mock-search",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"Rechercheergebnis. {prose}",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": entry["url"],
                                    "title": entry["title"],
                                    "start_index": 0,
                                    "end_index": 1,
                                }
                                for entry in results
                            ],
                        }
                    ],
                }
            ],
            "usage": _search_usage(),
        }
    )


@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request) -> JSONResponse:
    """Answer an Anthropic Messages request."""
    body = await _read_json(request)
    schema_name = request_schema_name(body)
    system = [{"role": "system", "content": str(body["system"])}] if body.get("system") else []
    prompt = system_prompt([*system, *(body.get("messages") or [])], schema_name=schema_name)
    operation, score = CATALOGUE.resolve(prompt, schema_name=schema_name)
    salt = _request_salt(body)
    if not operation:
        STATS.record_unmatched(signature(prompt, schema_name=schema_name), score)
        operation = "unmatched"
    await _serve(operation, request)
    captured = _pick_response(operation, salt)
    return JSONResponse(
        {
            "id": f"msg-{salt}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model") or CATALOGUE.model(operation),
            "content": [{"type": "text", "text": captured.get("content") or ""}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": int(captured.get("prompt_tokens") or 0),
                "output_tokens": int(captured.get("completion_tokens") or 0),
            },
        }
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Report readiness and any degraded artifact, never silently."""
    return JSONResponse(
        {
            "status": "degraded" if CATALOGUE.degraded else "ok",
            "operations": len(CATALOGUE.operations),
            "signatures": len(CATALOGUE.signatures),
            "responses": sum(len(v) for v in CATALOGUE.responses.values()),
            "conversations": sum(len(v) for v in CATALOGUE.conversations.values()),
            "embedding_dim": _EMBEDDING_DIM,
            "latency_scale": _LATENCY_SCALE,
            # Run depth is a measurement input, so the active selection policy
            # is reported: a pinned run must never be mistaken for a
            # representative one.
            "selection": CATALOGUE.selection or "captured distribution",
            "degraded": CATALOGUE.degraded,
        }
    )


@app.get("/admin/stats")
async def admin_stats() -> JSONResponse:
    """Return call counters — the proof that no real provider was reached."""
    return JSONResponse(STATS.snapshot())


def main() -> None:
    """Run the stand-in server."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    for message in CATALOGUE.degraded:
        log.warning("Degraded: %s", message)
    log.info(
        "Provider stand-in ready: %d operations, %d signatures, %d responses, "
        "embedding_dim=%d, latency_scale=%.2f",
        len(CATALOGUE.operations),
        len(CATALOGUE.signatures),
        sum(len(v) for v in CATALOGUE.responses.values()),
        _EMBEDDING_DIM,
        _LATENCY_SCALE,
    )
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 — container-internal test service
        port=int(os.environ.get("INQTRIX_MOCK_PORT", "9300")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
