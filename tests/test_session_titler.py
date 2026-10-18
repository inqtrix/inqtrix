"""The optional title call uses the Fast tier and accounts for real usage."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from inqtrix.observability.context import current_purpose
from inqtrix.providers.base import LLMResponse, StructuredLLMResponse
from inqtrix.services import session_titler as module
from inqtrix.services.session_titler import (
    SessionTitle,
    TitleAdmission,
    generate_title,
    title_capability,
)


class TitleLLM:
    def __init__(
        self,
        title="Digitaler Zwilling für Batteriespeicher",
        *,
        native=True,
        fail=False,
    ):
        self.models = SimpleNamespace(
            tier_fast_model="fast-model",
            tier_fast_effort="none",
            reasoning_model="main-model",
        )
        self.title, self.native, self.fail = title, native, fail
        self.calls = []
        self.after_call = lambda: None

    def is_available(self):
        return True

    def supports_structured_output(self, **kwargs):
        return self.native

    def _call(self, prompt, kwargs):
        self.calls.append((prompt, kwargs, current_purpose()))
        if kwargs.get("state") is not None:
            kwargs["state"]["total_prompt_tokens"] += 13
            kwargs["state"]["total_completion_tokens"] += 7
        self.after_call()
        if self.fail:
            raise ValueError("Native parsing failed after provider usage was recorded")

    def complete_structured(self, prompt, **kwargs):
        self._call(prompt, kwargs)
        return StructuredLLMResponse(
            parsed={"title": self.title}, prompt_tokens=13, completion_tokens=7
        )

    def complete_with_metadata(self, prompt, **kwargs):
        self._call(prompt, kwargs)
        return LLMResponse(
            content=json.dumps({"title": self.title}),
            prompt_tokens=13,
            completion_tokens=7,
        )


class QuotaRecorder:
    def __init__(self):
        self.bookings = []

    def record_blocking(self, subject, dimension, amount):
        self.bookings.append((subject, dimension.value, amount))


@pytest.mark.parametrize("native", [True, False])
def test_title_call_uses_dedicated_fast_model_language_directive_and_one_usage_receipt(
    monkeypatch, native
):
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100))
    llm, quota = TitleLLM(native=native), QuotaRecorder()
    result = generate_title(
        llm,
        question="Wie funktioniert ein digitaler Zwilling?",
        language="de",
        deadline=108,
        quota_service=quota,
        quota_subject="account",
    )
    assert result == {
        "title": "Digitaler Zwilling für Batteriespeicher",
        "reason": None,
        "model": "fast-model",
        "usage": {"prompt_tokens": 13, "completion_tokens": 7},
    }
    assert len(llm.calls) == 1
    prompt, kwargs, purpose = llm.calls[0]
    assert prompt.startswith("Antworte IMMER auf Deutsch")
    assert "Do not answer the question" in prompt
    assert "at most six words and 60 characters" in prompt
    assert (
        kwargs["model"],
        kwargs["reasoning_effort"],
        kwargs["max_output_tokens"],
        kwargs["timeout"],
        kwargs["deadline"],
    ) == ("fast-model", "none", 512, 8, 108)
    assert purpose is not None and purpose.purpose == "session_title"
    assert quota.bookings == [("account", "llm_tokens", 20)]


def test_question_language_wins_over_interface_language(monkeypatch):
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100))
    llm = TitleLLM(title="Battery storage digital twin")
    generate_title(
        llm,
        question="How would you build a digital twin for a battery?",
        language="de",
        deadline=108,
    )
    assert llm.calls[0][0].startswith("ALWAYS respond in English")


def test_no_fast_model_never_falls_back_to_main_model(monkeypatch):
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100))
    llm = TitleLLM()
    llm.models.tier_fast_model = ""
    assert title_capability(llm, enabled=True)["reason"] == "no_fast_model"
    assert (
        generate_title(llm, question="A question", language="en", deadline=108)[
            "reason"
        ]
        == "no_fast_model"
    )
    assert llm.calls == []


def test_invalid_title_is_discarded_but_usage_is_booked(monkeypatch):
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100))
    llm, quota = TitleLLM(title="Title: a response"), QuotaRecorder()
    result = generate_title(
        llm,
        question="A question",
        language="en",
        deadline=108,
        quota_service=quota,
        quota_subject="account",
    )
    assert (result["title"], result["reason"]) == (None, "invalid_title")
    assert quota.bookings == [("account", "llm_tokens", 20)]
    assert len(llm.calls) == 1


def test_native_parsing_failure_still_books_provider_accounting_state(monkeypatch):
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100))
    llm, quota = TitleLLM(fail=True), QuotaRecorder()
    with pytest.raises(ValueError, match="Native parsing"):
        generate_title(
            llm,
            question="A question",
            language="en",
            deadline=108,
            quota_service=quota,
            quota_subject="account",
        )
    assert quota.bookings == [("account", "llm_tokens", 20)]


def test_late_completion_is_charged_without_publishing_title(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    llm, quota = TitleLLM(), QuotaRecorder()
    llm.after_call = lambda: clock.__setitem__(0, 109)
    result = generate_title(
        llm,
        question="A question",
        language="en",
        deadline=108,
        quota_service=quota,
        quota_subject="account",
    )
    assert (result["title"], result["reason"]) == (None, "timeout")
    assert quota.bookings == [("account", "llm_tokens", 20)]


@pytest.mark.parametrize(
    "title",
    [
        "",
        " a",
        "a ",
        "a\nb",
        "a\x00b",
        '"quoted"',
        "A label.",
        "Title: label",
        "one two three four five six seven",
        "a" * 61,
    ],
)
def test_title_schema_rejects_unusable_labels(title):
    with pytest.raises(ValidationError):
        SessionTitle(title=title)


def test_title_schema_accepts_natural_unicode_and_word_internal_apostrophes():
    assert (
        SessionTitle(title="Newton's Überlegungen zur Gravitation").title
        == "Newton's Überlegungen zur Gravitation"
    )


def test_optional_admission_is_bounded_and_rate_windows_expire():
    clock = [100.0]
    gate = TitleAdmission(clock=lambda: clock[0])
    assert gate.acquire(("a",))
    assert gate.acquire(("b",))
    assert not gate.acquire(("c",))
    gate.release()
    gate.release()
    for _ in range(5):
        assert gate.acquire(("a",))
        gate.release()
    assert not gate.acquire(("a",))
    clock[0] = 161
    assert gate.acquire(("a",))
    gate.release()
