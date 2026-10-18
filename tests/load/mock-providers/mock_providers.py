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
  every response keeps the exact wire format and size the pipeline parses.

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

from prompt_routing import ROUTING_VERSION, request_schema_name, signature, system_prompt

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


class Catalogue:
    """Timing profile, response corpus, and routing table for one profile."""

    def __init__(self, profile_path: str, corpus_dir: str) -> None:
        self.operations: dict[str, dict] = {}
        self.responses: dict[str, list[dict]] = {}
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
            for operation in (manifest.get("provenance") or {}).get("non_replayable_operations", []):
                self.degraded.append(f"tool-call replay unavailable for {operation}")
            for operation in manifest.get("responses_per_operation") or {}:
                path = os.path.join(corpus_dir, f"{operation}.json")
                if os.path.exists(path):
                    self.responses[operation] = json.load(open(path, encoding="utf-8"))
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


async def _serve(operation: str) -> float:
    """Delay for the operation's sampled latency and count the call."""
    ordinal = STATS.next_ordinal(operation)
    seconds = _sample_latency(operation, ordinal)
    STATS.record(operation, seconds)
    await asyncio.sleep(seconds)
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
    prompt = system_prompt(body.get("messages") or body.get("input"))
    schema_name = request_schema_name(body)
    operation, score = CATALOGUE.resolve(prompt, schema_name=schema_name)
    if not operation:
        STATS.record_unmatched(signature(prompt, schema_name=schema_name), score)
        operation = "unmatched"
    return operation, salt


@app.post("/azure/openai/v1/chat/completions")
@app.post("/openai/v1/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """Answer an OpenAI-compatible chat completion."""
    body = await _read_json(request)
    operation, salt = _resolve(body)
    await _serve(operation)
    # A structured request carries the shape its caller will validate. Pass it
    # through: some operations cannot be replayed at all because the reply
    # length depends on the request, and the caller rejects any other count.
    schema = (
        ((body.get("response_format") or {}).get("json_schema") or {}).get("schema")
    )
    captured = _pick_response(operation, salt, schema)
    content = captured.get("content") or ""
    return JSONResponse(
        {
            "id": f"chatcmpl-{salt}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or CATALOGUE.model(operation),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": int(captured.get("prompt_tokens") or 0),
                "completion_tokens": int(captured.get("completion_tokens") or 0),
                "total_tokens": int(captured.get("prompt_tokens") or 0)
                + int(captured.get("completion_tokens") or 0),
            },
        }
    )


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
    await _serve(operation)
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
    await _serve(operation)
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
    prompt = str(body.get("system") or "") or system_prompt(body.get("messages"))
    schema_name = request_schema_name(body)
    operation, score = CATALOGUE.resolve(prompt, schema_name=schema_name)
    salt = _request_salt(body)
    if not operation:
        STATS.record_unmatched(signature(prompt, schema_name=schema_name), score)
        operation = "unmatched"
    await _serve(operation)
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
