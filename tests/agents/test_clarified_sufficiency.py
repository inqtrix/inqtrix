"""The coverage judge must see what the user already answered.

On an underspecified task the agent asks back, the user names both
options, the search queries carry the answer — and without it the
coverage verdict still reads "da keine zwei konkreten Optionen genannt
sind". The run then keeps working against the original wording until it
hits the step limit.
"""

from __future__ import annotations

from typing import Any

from inqtrix.agents.evidence import run_sufficiency_judgement


class _RecordingLLM:
    """Captures the prompt the judge would send, then stops."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def supports_structured_output(self, *, model: Any = None) -> bool:
        return True

    def complete_structured(self, prompt: str, **kwargs: Any) -> Any:
        self.prompts.append(prompt)
        raise RuntimeError("stop after capturing the prompt")


def _capture(**kwargs: Any) -> str:
    llm = _RecordingLLM()
    try:
        run_sufficiency_judgement(
            llm,
            evidence_digest="- [K1] AI Act, Abschnitt 285",
            model=None,
            reasoning_effort=None,
            timeout=1.0,
            **kwargs,
        )
    except Exception:  # noqa: BLE001 — the stub stops at the prompt
        pass
    return llm.prompts[0] if llm.prompts else ""


def test_the_judge_is_told_what_the_user_already_settled():
    prompt = _capture(
        success_criteria=["Vergleiche die beiden Optionen."],
        clarified_context=(
            "Frage: Welche zwei Optionen?\n"
            "Antwort des Nutzers: Eigenes Rechenzentrum gegen Public Cloud."
        ),
    )
    assert "Eigenes Rechenzentrum gegen Public Cloud." in prompt
    assert "Already specified by the user" in prompt
    # And it must be told not to report a closed gap as open.
    assert "must NOT be reported as missing" in prompt


def test_without_a_clarification_the_prompt_is_unchanged():
    """The mission engine calls this too — an empty context must not add
    an empty section to its prompt."""
    prompt = _capture(success_criteria=["Kriterium A"])
    assert "Already specified by the user" not in prompt
    assert "Kriterium A" in prompt


def test_whitespace_only_clarification_adds_nothing():
    prompt = _capture(success_criteria=["Kriterium A"], clarified_context="  \n ")
    assert "Already specified by the user" not in prompt


def test_the_kernel_hands_its_recorded_answers_to_the_judge():
    """The wiring, not just the wording: what `ask_user` recorded on the
    run must actually reach the coverage prompt."""
    import pytest

    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel.cognition import judge_kernel_sufficiency
    from inqtrix.agents.kernel.deps import KernelDeps

    llm = _RecordingLLM()
    deps = KernelDeps(
        run_id="run_x",
        control=None,
        platform=None,
        llm=llm,
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )
    deps.question = "Vergleiche die beiden Optionen."
    deps.clarified_answers.append(
        "Frage: Welche zwei Optionen?\n"
        "Antwort des Nutzers: Eigenes Rechenzentrum gegen Public Cloud."
    )
    try:
        judge_kernel_sufficiency(deps)
    except Exception:  # noqa: BLE001 — the stub stops at the prompt
        pass
    assert llm.prompts, "the judge never ran"
    assert "Eigenes Rechenzentrum gegen Public Cloud." in llm.prompts[0]


def test_the_kernel_judge_reads_a_long_register_line_cut_visibly():
    """The kernel's coverage prompt carries the same register as the
    mission's: a passage longer than the line ends where a word ends,
    marked, never on a partial number the judge could take as the fact."""
    import pytest

    pytest.importorskip("deepagents")
    from inqtrix.agents.kernel.cognition import judge_kernel_sufficiency
    from inqtrix.agents.kernel.deps import KernelDeps

    llm = _RecordingLLM()
    deps = KernelDeps(
        run_id="run_x",
        control=None,
        platform=None,
        llm=llm,
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )
    deps.question = "Welche Summe bleibt offen?"
    deps.evidence_refs["ref_1"] = {
        "label": "W1",
        "reference_id": "ref_1",
        "url": "https://example.test/bericht",
        "excerpt": "Frist " * 32 + "1 500 000 EUR bleiben offen. " * 8,
    }
    try:
        judge_kernel_sufficiency(deps)
    except Exception:  # noqa: BLE001 — the stub stops at the prompt
        pass
    assert llm.prompts, "the judge never ran"
    (line,) = [
        line for line in llm.prompts[0].splitlines() if line.startswith("[W1] ")
    ]
    assert line == (
        "[W1] "
        + ("Frist " * 32).rstrip()
        + " [shortened here; the passage continues]"
    )
