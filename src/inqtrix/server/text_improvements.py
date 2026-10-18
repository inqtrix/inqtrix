"""Prompt and parsing helpers for interactive text improvement requests."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal


log = logging.getLogger("inqtrix")

TextImprovementContext = Literal["chat_input", "prompt_template"]
TextImprovementLocale = Literal["de", "en"]

MAX_TEXT_IMPROVEMENT_OUTPUT_ITEMS = 5

_CHAT_INPUT_PROMPT = """You are a precise writing assistant for an AI research chat.

Task:
Improve the following user draft so it becomes a clear, natural message that can be sent to an AI assistant.

Language rules:
- Preserve the original language of the draft.
- If the draft is German, improve it in German.
- If the draft is English, improve it in English.
- If the draft intentionally mixes languages, preserve that bilingual structure.
- Do not translate the draft unless the user explicitly asks for translation.
- Write "change_summary", "warnings", and "clarification_questions" in this interface language: <<<METADATA_LANGUAGE>>>.

Editing rules:
- Correct spelling, grammar, punctuation, and obvious typos.
- Preserve the user's original intent completely.
- Do not invent facts, requirements, sources, constraints, or decisions.
- Make the request clearer, more concrete, and more professional.
- Preserve @mentions, file names, URLs, code blocks, placeholders, IDs, and technical tokens exactly.
- Do not answer the request. Only improve the draft.
- If the draft is short, keep the improved version short.
- If the draft is complex, structure it with concise paragraphs or bullets.
- If a critical ambiguity remains, keep the best improved version and set needs_clarification=true.

Return only valid JSON:
{
  "improved_text": "...",
  "change_summary": ["..."],
  "warnings": [],
  "needs_clarification": false,
  "clarification_questions": []
}

Additional guidance:
<<<GUIDANCE>>>

Draft:
<<<TEXT>>>
<<<USER_TEXT>>>
<<<END_TEXT>>>"""

_PROMPT_TEMPLATE_PROMPT = """You are a prompt-engineering assistant for reusable prompt templates.

Task:
Optimize the following prompt template for clarity, robustness, and high-quality model behavior.

Language rules:
- Preserve the template's working language.
- If the template is German, optimize it in German.
- If the template is English, optimize it in English.
- If it is intentionally bilingual, preserve the bilingual structure.
- Do not translate unless the template explicitly requests translation behavior.
- Write "change_summary", "warnings", and "clarification_questions" in this interface language: <<<METADATA_LANGUAGE>>>.

Prompt-engineering rules:
- Preserve the original purpose and output intent.
- Improve role, task, context, constraints, input handling, and output format when helpful.
- Preserve all placeholders, variables, @mentions, Markdown headings, code blocks, XML-like tags, and technical tokens exactly.
- Do not add chain-of-thought instructions.
- Do not invent product features, hidden policies, tools, data sources, or unavailable capabilities.
- Remove ambiguity without overengineering the prompt.
- If the template contains contradictions, improve what can be improved and list the issue in warnings.

Return only valid JSON:
{
  "improved_text": "...",
  "change_summary": ["..."],
  "warnings": [],
  "needs_clarification": false,
  "clarification_questions": []
}

Additional guidance:
<<<GUIDANCE>>>

Prompt template:
<<<TEXT>>>
<<<USER_TEXT>>>
<<<END_TEXT>>>"""

_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}"
    ),
)


@dataclass(frozen=True)
class TextImprovementRequestData:
    """Validated input for one text improvement request.

    Attributes:
        context: Editing context that selects the prompt family.
        text: User-visible field contents to improve.
        locale: Interface language for metadata strings in the JSON response.
        guidance: Optional caller-provided prompt context. It guides the edit
            but is not part of the text the model should return.
    """

    context: TextImprovementContext
    text: str
    locale: TextImprovementLocale
    guidance: str | None = None


@dataclass(frozen=True)
class TextImprovementResult:
    """Normalized model output returned by the HTTP endpoint.

    Attributes:
        improved_text: Replacement candidate shown to the user for review.
        change_summary: Short, localized notes describing the edit.
        warnings: Localized warnings about ambiguity or model-output repair.
        needs_clarification: Whether the model found a blocking ambiguity.
        clarification_questions: Localized questions the UI can display.
    """

    improved_text: str
    change_summary: list[str]
    warnings: list[str]
    needs_clarification: bool
    clarification_questions: list[str]

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serializable HTTP response shape."""
        return {
            "improved_text": self.improved_text,
            "change_summary": self.change_summary,
            "warnings": self.warnings,
            "needs_clarification": self.needs_clarification,
            "clarification_questions": self.clarification_questions,
        }


class TextImprovementError(ValueError):
    """Raised when the model response cannot be safely returned."""


def parse_text_improvement_payload(
    body: dict[str, Any],
    *,
    max_text_chars: int,
) -> TextImprovementRequestData:
    """Validate and normalize the raw HTTP payload.

    Args:
        body: JSON object parsed from the request body.
        max_text_chars: Maximum accepted input length in characters.

    Returns:
        TextImprovementRequestData: Normalized request fields.

    Raises:
        ValueError: If the payload is malformed, unsupported, empty,
            oversized, or likely to contain secret material.
    """
    context = body.get("context")
    if context not in ("chat_input", "prompt_template"):
        raise ValueError("context must be 'chat_input' or 'prompt_template'.")

    raw_text = body.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("text must be a non-empty string.")

    text = raw_text.strip()
    if len(text) > max_text_chars:
        raise ValueError(
            f"text exceeds the maximum length ({len(text)} > {max_text_chars})."
        )
    if text_looks_sensitive(text):
        raise ValueError(
            "text appears to contain secret material and was not sent to the model."
        )

    raw_locale = body.get("locale", "en")
    locale: TextImprovementLocale = "de" if raw_locale == "de" else "en"

    raw_guidance = body.get("guidance")
    guidance = raw_guidance.strip() if isinstance(raw_guidance, str) else None
    if guidance and text_looks_sensitive(guidance):
        raise ValueError(
            "guidance appears to contain secret material and was not sent to the model."
        )

    return TextImprovementRequestData(
        context=context,
        text=text,
        locale=locale,
        guidance=guidance or None,
    )


def build_text_improvement_prompt(request: TextImprovementRequestData) -> str:
    """Build the context-specific model prompt for one request.

    Args:
        request: Validated request data.

    Returns:
        str: Prompt ready for ``LLMProvider.complete``.
    """
    template = (
        _PROMPT_TEMPLATE_PROMPT
        if request.context == "prompt_template"
        else _CHAT_INPUT_PROMPT
    )
    metadata_language = "German" if request.locale == "de" else "English"
    guidance = request.guidance or "No additional guidance."
    return (
        template
        .replace("<<<METADATA_LANGUAGE>>>", metadata_language)
        .replace("<<<GUIDANCE>>>", guidance)
        .replace("<<<USER_TEXT>>>", request.text)
    )


def parse_text_improvement_response(raw_text: str) -> TextImprovementResult:
    """Parse and normalize the model's JSON response.

    Args:
        raw_text: Raw visible text returned by the LLM provider.

    Returns:
        TextImprovementResult: Validated response fields.

    Raises:
        TextImprovementError: If no valid JSON object with a non-empty
            ``improved_text`` can be parsed.
    """
    warnings: list[str] = []
    payload_text = raw_text.strip()
    if not payload_text:
        raise TextImprovementError("Text improvement model returned an empty response.")

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as initial_error:
        extracted = _extract_json_object(payload_text)
        if extracted is None:
            raise TextImprovementError(
                "Text improvement model did not return valid JSON."
            ) from initial_error
        log.warning(
            "Text improvement response required JSON extraction fallback "
            "(error_type=%s)",
            type(initial_error).__name__,
        )
        warnings.append("The model returned extra text around the JSON response.")
        try:
            payload = json.loads(extracted)
        except json.JSONDecodeError as repair_error:
            raise TextImprovementError(
                "Text improvement model returned malformed JSON."
            ) from repair_error

    if not isinstance(payload, dict):
        raise TextImprovementError("Text improvement JSON must be an object.")

    improved_text = payload.get("improved_text")
    if not isinstance(improved_text, str) or not improved_text.strip():
        raise TextImprovementError(
            "Text improvement JSON must contain a non-empty improved_text."
        )

    return TextImprovementResult(
        improved_text=improved_text.strip(),
        change_summary=_string_list(payload.get("change_summary")),
        warnings=[*_string_list(payload.get("warnings")), *warnings][
            :MAX_TEXT_IMPROVEMENT_OUTPUT_ITEMS
        ],
        needs_clarification=bool(payload.get("needs_clarification", False)),
        clarification_questions=_string_list(payload.get("clarification_questions")),
    )


def text_looks_sensitive(text: str) -> bool:
    """Return whether ``text`` contains obvious secret-looking material."""
    return any(pattern.search(text) for pattern in _SENSITIVE_TEXT_PATTERNS)


def _string_list(value: Any) -> list[str]:
    """Normalize a model-provided list of strings to a short clean list."""
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        clean = item.strip()
        if clean:
            items.append(clean)
        if len(items) >= MAX_TEXT_IMPROVEMENT_OUTPUT_ITEMS:
            break
    return items


def _extract_json_object(raw_text: str) -> str | None:
    """Return the first top-level JSON object embedded in ``raw_text``."""
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return raw_text[start : end + 1]
