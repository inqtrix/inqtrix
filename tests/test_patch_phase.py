"""Tests for the agent patch phase on top of the editor instruction pipeline."""

from __future__ import annotations

import json

import pytest

from inqtrix.agents.patch_phase import PatchProposalFailed, propose_patch_edits
from inqtrix.server.editor_instructions import (
    EditorInstructError,
    EditorInstructResult,
)


class _StubLLM:
    """Answers every instruct call with one fixed model response."""

    context_window_tokens = 16000

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompt: str | None = None

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return False

    def complete(self, prompt: str, **kwargs: object) -> str:
        self.prompt = prompt
        return self.response


def _response(text: str) -> str:
    return json.dumps({
        "assistant_message": "Der Memo-Befund ist eingearbeitet.",
        "edits": [{
            "find": "Der Zeitplan steht.",
            "quote_before": "",
            "quote_after": "",
            "position": "replace",
            "text": text,
            "note": "Zeitplan ergänzt.",
        }],
        "warnings": [],
    })


def _propose(llm: _StubLLM) -> tuple[EditorInstructResult, int]:
    return propose_patch_edits(
        llm,  # type: ignore[arg-type]
        question="Ergänze den Zeitplan aus dem Memo.",
        memo_markdown="Meilenstein 1 im Mai [K1].",
        document_markdown="# Plan\n\nDer Zeitplan steht.",
        language="de",
        model=None,
        reasoning_effort=None,
        timeout=30,
    )


def test_patch_fails_loudly_when_its_only_edit_writes_the_memo_marker() -> None:
    # The memo is reference document [1] of the request. An edit that writes
    # "[1]" into the document is discarded; with nothing left the patch never
    # happened, which must fail the run instead of reading as "no changes".
    llm = _StubLLM(_response("Der Zeitplan steht: Meilenstein 1 im Mai, siehe [1]."))

    with pytest.raises(PatchProposalFailed, match="^patch_proposal_failed$") as raised:
        _propose(llm)

    assert llm.prompt is not None and "[1] Memo" in llm.prompt
    assert isinstance(raised.value.__cause__, EditorInstructError)
    assert "wrote a marker or file token of this request" in str(raised.value.__cause__)


def test_patch_keeps_the_citation_labels_of_its_memo() -> None:
    llm = _StubLLM(_response("Der Zeitplan steht: Meilenstein 1 im Mai [K1]."))

    result, _tokens = _propose(llm)

    assert [edit.text for edit in result.edits] == [
        "Der Zeitplan steht: Meilenstein 1 im Mai [K1]."
    ]
