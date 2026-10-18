"""Final kernel-answer citation repair and reference projection."""

from __future__ import annotations

from typing import Any

from inqtrix.agents.kernel.algorithm import (
    _result_references,
    _validate_kernel_answer_citations,
)
from inqtrix.agents.report_quality import CitationValidationFailed


LEDGER = [
    {"label": "W1", "url": "https://example.test/one"},
    {"label": "K2", "url": "inqtrix://documents/two#chunk-0"},
]


def test_unknown_only_citation_never_falls_back_to_the_whole_ledger() -> None:
    assert _result_references("Erfundene Zuordnung [W9].", LEDGER) == []


def test_mixed_valid_and_unknown_citations_keep_only_the_valid_subset() -> None:
    projected = _result_references(
        "Belegt [K2], aber diese Zuordnung ist unbekannt [W9].",
        LEDGER,
    )

    assert [reference["label"] for reference in projected] == ["K2"]


class _Deps:
    def __init__(self, user_text: str = "") -> None:
        self.llm = object()
        self.model = "model"
        self.reasoning_effort = None
        self.timeout = 30.0
        self.question = "What does GPT-5.6 Sol cost?"
        self._user_text = user_text
        self.usage: list[tuple[int, int]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    def user_text(self) -> str:
        """The stand-in for ``KernelDeps.user_text``: this run's user text."""
        return "\n".join(text for text in (self.question, self._user_text) if text)

    def check_abort(self) -> None:
        return

    def book_usage(self, prompt: int, completion: int) -> None:
        self.usage.append((prompt, completion))

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


def test_bounded_repair_is_used_before_reference_projection(monkeypatch) -> None:
    deps = _Deps()

    def repair(*_args, **_kwargs):
        return "Korrigierte Aussage [W1].", {
            "prompt_tokens": 11,
            "completion_tokens": 3,
        }

    monkeypatch.setattr(
        "inqtrix.agents.report_quality.validate_and_repair_citations",
        repair,
    )

    answer = _validate_kernel_answer_citations(  # type: ignore[arg-type]
        deps,
        "Aussage [W9].",
        LEDGER,
    )

    assert answer == "Korrigierte Aussage [W1]."
    assert deps.usage == [(11, 3)]
    assert [reference["label"] for reference in _result_references(answer, LEDGER)] == [
        "W1"
    ]
    assert deps.events[-1][1]["status"] == "repaired"


def test_failed_repair_marks_unknown_label_and_preserves_valid_citation(
    monkeypatch,
) -> None:
    deps = _Deps()

    def fail(*_args, **_kwargs):
        raise CitationValidationFailed(
            "still invalid",
            usage={"prompt_tokens": 7, "completion_tokens": 2},
        )

    monkeypatch.setattr(
        "inqtrix.agents.report_quality.validate_and_repair_citations",
        fail,
    )

    answer = _validate_kernel_answer_citations(  # type: ignore[arg-type]
        deps,
        "Belastbar [W1], erfunden [W9].",
        LEDGER,
    )

    assert "[W1]" in answer
    assert "[W9]" not in answer
    assert "[unsupported: W9]" in answer
    assert deps.usage == [(7, 2)]
    assert [reference["label"] for reference in _result_references(answer, LEDGER)] == [
        "W1"
    ]
    assert deps.events[-1][1]["status"] == "degraded"


def test_uncited_answer_keeps_the_basis_only_when_the_run_gathered_sources() -> None:
    """The basis fallback is for answers built on sources THIS run
    searched or read. A run that only wrote a file (its ledger filled by
    reading a document) must not surface "N Quellen" on a status answer."""
    answer = "Die Antwort steht jetzt als Canvas-Datei vor."
    assert _result_references(answer, LEDGER, basis_fallback=True) == LEDGER
    assert _result_references(answer, LEDGER, basis_fallback=False) == []
    # A citing answer is unaffected by the flag.
    cited = _result_references("Belegt [W1].", LEDGER, basis_fallback=False)
    assert [reference["label"] for reference in cited] == ["W1"]


def test_final_answer_repairs_registered_internal_ids_without_unknown_labels(monkeypatch):
    deps = _Deps()
    ledger = [{"label": "W1", "reference_id": "opaque-evidence-key"}]
    calls = []
    def repair(*_args, **kwargs):
        calls.append(kwargs)
        return "Supported [W1].", {"prompt_tokens": 7, "completion_tokens": 2}
    monkeypatch.setattr("inqtrix.agents.report_quality.validate_and_repair_citations", repair)
    answer = _validate_kernel_answer_citations(deps, "Supported [W1]. opaque-evidence-key", ledger)
    assert answer == "Supported [W1]."
    assert len(calls) == 1
    assert calls[0]["internal_reference_ids"] == ["opaque-evidence-key"]
    assert deps.usage == [(7, 2)]


def test_failed_internal_id_repair_does_not_publish_the_unclean_final_answer(monkeypatch):
    import pytest
    deps = _Deps()
    ledger = [{"label": "W1", "reference_id": "opaque-evidence-key"}]
    def fail(*_args, **_kwargs):
        raise CitationValidationFailed("internal identifiers remain", usage={"prompt_tokens": 7, "completion_tokens": 2})
    monkeypatch.setattr("inqtrix.agents.report_quality.validate_and_repair_citations", fail)
    with pytest.raises(CitationValidationFailed):
        _validate_kernel_answer_citations(deps, "Supported [W1]. opaque-evidence-key", ledger)
    assert deps.usage == [(7, 2)]


def test_internal_id_repair_provider_failure_cannot_publish_the_original(monkeypatch):
    import pytest
    deps = _Deps()
    ledger = [{"label": "W1", "reference_id": "opaque-evidence-key"}]
    def fail(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr("inqtrix.agents.report_quality.validate_and_repair_citations", fail)
    with pytest.raises(CitationValidationFailed, match="Internal reference metadata"):
        _validate_kernel_answer_citations(deps, "Supported [W1]. opaque-evidence-key", ledger)
    assert deps.usage == []


def test_an_id_from_an_earlier_user_turn_is_not_a_leak(monkeypatch) -> None:
    """An id the user named one turn ago still belongs to the user.

    ``session_history`` is filled only where a segment composes a user
    message, so on a resumed segment that waives the review the kernel no
    longer holds the earlier turns at all. The durable request does - it is
    rebuilt from the persisted payload on every attempt - so the guard
    reads the user's words from there and does not send a run into the
    repair round for answering the question it was asked.
    """
    from inqtrix.agents.kernel.algorithm import _request_user_text
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.core.results import CanvasComment, CanvasContext, RunRequest

    request = RunRequest(
        mode="agent_kernel",
        question="Und jetzt?",
        history="User: Was steht in doc-42?",
        report_requirement="Nimm rev-7 als Fassung.",
        canvas_context=CanvasContext(
            artifact_id="art_1", revision=1,
            comments=(CanvasComment(
                artifact_id="art_1", revision=1,
                quote="Der Absatz", comment="Bezieht sich auf gen-2.",
            ),),
        ),
    )
    deps = KernelDeps(
        run_id="run_resumed", control=None, platform=None, llm=None,
        model=None, reasoning_effort=None, timeout=1,
        question=request.question,
        request_user_text=_request_user_text(request),
    )
    # Hydration never ran on this segment; the request still carries it.
    assert deps.session_history == ""
    for value in ("doc-42", "rev-7", "gen-2"):
        assert value in deps.user_text(), value

    ledger = [{
        "label": "K1", "document_id": "doc-42", "chunk_index": 0,
        "revision_id": "rev-7", "generation_id": "gen-2",
    }]
    answer = "In doc-42 (rev-7, gen-2) steht die Antwort [K1]."

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the user's own identifiers went into a repair round")

    monkeypatch.setattr(
        "inqtrix.agents.report_quality.validate_and_repair_citations", never
    )
    assert _validate_kernel_answer_citations(deps, answer, ledger) == answer

    # An id of the same ledger the user never named is still a leak.
    deps.request_user_text = ""
    deps.question = "Und jetzt?"
    monkeypatch.setattr(
        "inqtrix.agents.report_quality.validate_and_repair_citations",
        lambda *_a, **kwargs: ("Bereinigt [K1].", {"prompt_tokens": 1, "completion_tokens": 1}),
    )
    assert _validate_kernel_answer_citations(deps, answer, ledger) == "Bereinigt [K1]."
