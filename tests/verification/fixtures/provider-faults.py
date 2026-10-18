"""Scoped SDK faults for the canonical Agent recovery verification profile.

The orchestrator mounts this fixture into the existing API/worker images and
restores their normal commands afterwards. It neither reads secrets nor
replaces provider construction. Requests outside the exact verification marker
continue through the original SDK method.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import sys
import threading
from functools import wraps

import httpx
from openai import BadRequestError, InternalServerError, PermissionDeniedError, RateLimitError
from openai.types.chat import ChatCompletion

from inqtrix.observability.context import current_purpose

CASES = {
    "MARKE1": "context",
    "MARKE2": "output_limit",
    "MARKE3": "arguments",
    "MARKE4": "arguments_exhausted",
    "MARKE5": "transport",
    "MARKE6": "access_denied",
    "MARKE7": "unknown",
    "MARKE8": "cancel",
    "MARKE9": "context_unreducible",
    "MARKE0": "seed",
    "MARKE11": "archive_window",
    "MARKE12": "archive_catalogue",
    "MARKE13": "history_seed",
    "MARKE15": "bulk_handover",
}


class ProviderFaults:
    def __init__(self, run_id):
        if not re.fullmatch(r"inqv-[a-z0-9][a-z0-9-]{7,75}", run_id):
            raise ValueError("A canonical verification Run ID is required.")
        self.run_id = run_id
        self.lock = threading.Lock()
        self.counts = {}
        self.context_rejected = False

    @staticmethod
    def response(kwargs, *, text="", name=None, arguments=None, call_id="fixture_call"):
        calls = None if name is None else [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments},
        }]
        return ChatCompletion.model_validate({
            "id": "verification-completion", "object": "chat.completion", "created": 0,
            "model": kwargs["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": text, "tool_calls": calls}}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38},
        })

    @staticmethod
    def error(kind, status, *, code, param=None, retry_after=None):
        body = {"code": code, "message": "Controlled verification fault.", "param": param}
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        response = httpx.Response(
            status, headers=headers,
            request=httpx.Request("POST", "https://verification.invalid/chat"),
        )
        return kind("Controlled verification fault.", response=response, body=body)

    def dispatch(self, original, kwargs):
        purpose = current_purpose()
        if purpose is None or purpose.purpose not in {"agent_kernel", "agent_context_projection"}:
            return original(**kwargs)
        from inqtrix.agents.kernel.deps import kernel_deps

        try:
            question = kernel_deps().question
        except RuntimeError:
            return original(**kwargs)
        messages = kwargs.get("messages") or []
        marker = next((
            candidate for candidate in CASES
            if question.startswith(f"{self.run_id} {candidate} ") and any(
                m.get("role") == "user" and isinstance(m.get("content"), str)
                and f"{self.run_id} {candidate} " in m["content"]
                for m in messages
            )
        ), None)
        if marker is None:
            return original(**kwargs)
        case = CASES[marker]
        with self.lock:
            count = self.counts.get(case, 0) + 1
            self.counts[case] = count
        if count > 12:
            raise RuntimeError("Verification provider call ceiling exceeded.")
        fingerprint = hashlib.sha256(json.dumps(
            {key: kwargs.get(key) for key in ("messages", "tools", "model")},
            sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        # Only controlled case identifiers and hashes enter the fixture log.
        # Actual prompts and replies use the product's normal trace redaction.
        print(json.dumps({"verification_provider": {
            "run": self.run_id, "case": case, "call": count,
            "purpose": purpose.purpose, "request_sha256": fingerprint,
        }}), flush=True)
        if case == "history_seed":
            return self.response(kwargs, text="Die Notiz ist aufgenommen.")
        if case == "bulk_handover":
            if count == 1:
                return self.response(kwargs, name="web_instant", arguments=json.dumps({
                    "query": "Welcher abschließende Prüfwert steht im fiktiven Abnahmedatensatz?",
                }), call_id="fixture_bulk_result")
            return original(**kwargs)
        if case == "seed":
            if count == 1:
                return self.response(kwargs, name="write_canvas", arguments=json.dumps({
                    "title": "Verification source", "content_markdown": "Diagnostic placeholder.",
                }), call_id="fixture_seed")
            return self.response(kwargs, text="Das Quelldokument ist vorbereitet.")
        if case in {"archive_window", "archive_catalogue"}:
            deps = kernel_deps()
            if count == 1:
                if case == "archive_window":
                    # Only storage receives the tail fact. It cannot reach the
                    # model until the real read_canvas call retrieves that range.
                    source = "Neutrales Archivmaterial. " * 3_000 + "\nMARKEZIEL=83\n"
                    source_id = deps.append_context_archive("Archive source", source)
                    arguments = {"artifact_id": source_id}
                else:
                    for title, value in (("Alpha", 11), ("Beta", 29), ("Gamma", 47)):
                        deps.append_context_archive(title, f"MARKEKATALOG={value}")
                    arguments = {"artifact_id": deps.context_archive_prefix, "limit": 2}
                return self.response(kwargs, name="read_canvas", arguments=json.dumps(arguments),
                                     call_id=f"fixture_{case}_first")
            previous = next((m.get("content") for m in reversed(messages) if m.get("role") == "tool"), None)
            if not isinstance(previous, str):
                raise RuntimeError("The archive verification tool reply is absent.")
            if count == 2:
                if case == "archive_window":
                    data = json.loads(previous.split('>\n', 1)[1].split("\n</untrusted_content>", 1)[0])
                    if data["offset"] != 0 or data["is_full_source"] or "MARKEZIEL" in data["text"]:
                        raise RuntimeError("The first archive window already contains the tail fact.")
                    arguments = {
                        "artifact_id": data["source_id"], "offset": data["total_chars"] - 100,
                        "content_hash": data["content_hash"], "max_chars": 100,
                    }
                else:
                    arguments = json.loads(previous.split("these arguments: ", 1)[1])
                return self.response(kwargs, name="read_canvas", arguments=json.dumps(arguments),
                                     call_id=f"fixture_{case}_next")
            if case == "archive_catalogue" and count == 3:
                data = json.loads(previous.split('>\n', 1)[1].split("\n</untrusted_content>", 1)[0])
                if len(data["sources"]) != 1 or data["sources"][0]["title"] != "Gamma":
                    raise RuntimeError("The second archive catalogue page is inconsistent.")
                return self.response(kwargs, name="read_canvas", arguments=json.dumps({
                    "artifact_id": data["sources"][0]["source_id"],
                }), call_id="fixture_archive_catalogue_read")
            return original(**kwargs)
        if case == "context":
            if purpose.purpose == "agent_context_projection":
                return original(**kwargs)
            if count == 1:
                prompt = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "user")
                match = re.search(r"artifact_id[ =']+(art_[a-zA-Z0-9_-]+)", prompt)
                if match is None:
                    raise RuntimeError("Verification source is absent from the actual model context.")
                return self.response(kwargs, name="read_canvas", arguments=json.dumps({
                    "artifact_id": match[1],
                }), call_id="fixture_read")
            if not self.context_rejected:
                self.context_rejected = True
                raise self.error(BadRequestError, 400, code="context_length_exceeded", param="messages")
            return original(**kwargs)
        if case == "context_unreducible":
            raise self.error(BadRequestError, 400, code="context_length_exceeded", param="messages")
        if case == "output_limit":
            raise self.error(BadRequestError, 400, code="invalid_value", param="max_completion_tokens")
        if case in {"arguments", "arguments_exhausted"}:
            if case == "arguments_exhausted" or count == 1:
                return self.response(kwargs, name="write_todos", arguments='{"todos": [',
                                     call_id=f"fixture_invalid_{count}")
            return original(**kwargs)
        if case == "transport":
            if count == 1:
                raise self.error(InternalServerError, 503, code="service_unavailable")
            if count == 2:
                raise self.error(RateLimitError, 429, code="rate_limit_exceeded", retry_after="0")
            return self.response(kwargs, text="MARKE5: Die Anfrage wurde erfolgreich fortgesetzt.")
        if case == "access_denied":
            raise self.error(PermissionDeniedError, 403, code="permission_denied")
        if case == "unknown":
            raise RuntimeError("Injected internal provider failure.")
        if case == "cancel":
            if count > 1:
                raise RuntimeError("Another SDK call occurred after the cancellation fixture.")
            raise self.error(RateLimitError, 429, code="rate_limit_exceeded", retry_after="10")
        raise RuntimeError("Unknown verification case.")

    def install(self):
        import inqtrix.providers as providers

        build = providers._build_llm_provider

        @wraps(build)
        def instrument(*args, **kwargs):
            provider = build(*args, **kwargs)
            resource = provider._client.chat.completions
            if getattr(resource, "_verification_run", None) != self.run_id:
                original = resource.create

                @wraps(original)
                def controlled(**create_kwargs):
                    return self.dispatch(original, create_kwargs)

                resource.create = controlled
                resource._verification_run = self.run_id
            print(json.dumps({"verification_provider_ready": self.run_id}), flush=True)
            return provider

        providers._build_llm_provider = instrument
        build_search = providers._build_search_provider

        @wraps(build_search)
        def instrument_search(*args, **kwargs):
            provider = build_search(*args, **kwargs)
            original = provider.search

            @wraps(original)
            def controlled_search(*args, **kwargs):
                from inqtrix.agents.kernel.deps import kernel_deps
                from inqtrix.search_result import GroundedSearchResult, GroundedSource

                try:
                    question = kernel_deps().question
                except RuntimeError:
                    return original(*args, **kwargs)
                if not question.startswith(f"{self.run_id} MARKE15 "):
                    return original(*args, **kwargs)
                body = "Neutrales Quellenmaterial. " * 3_000 + "\nAbschließender Prüfwert: 93.\n"
                return GroundedSearchResult(answer=body, sources=[GroundedSource(
                    url="https://verification.invalid/bulk-source", title="Fiktiver Abnahmedatensatz",
                )])
            provider.search = controlled_search
            return provider

        providers._build_search_provider = instrument_search


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) == 2 else ""
    if role not in {"api", "worker"}:
        raise SystemExit("The orchestrator must select api or worker.")
    ProviderFaults(os.environ.get("INQTRIX_VERIFICATION_RUN_ID", "")).install()
    sys.argv = ["inqtrix" if role == "api" else "inqtrix-worker"]
    runpy.run_module("inqtrix" if role == "api" else "inqtrix.worker", run_name="__main__")
