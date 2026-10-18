"""Turn the assignment and memo into anchored editor patches.

Reuses the ONE editor-instruct pipeline (``services/editor_assist_service``
— the same prompt/schema/validation the ``/v1/editor/instruct`` route
serves, one shared definition), so agent patches and user instructions can never
drift apart. The memo travels as a REFERENCE document: quoted as source
material, never as instruction. The phase only PROPOSES — applying is
an always-gated user action.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from inqtrix.agents.prompts import build_agent_patch_instruction
from inqtrix.server.editor_instructions import (
    EditorInstructError,
    EditorInstructRequestData,
    EditorInstructResult,
)
from inqtrix.server.reference_documents import ReferenceDocument

if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider

log = logging.getLogger("inqtrix")


class PatchProposalFailed(RuntimeError):
    """The instruct pipeline could not produce a usable edit set.

    Distinct from a genuine zero-edit outcome: a timeout, parse failure
    or an over-budget document mean the requested document edit never
    HAPPENED — the run must fail loudly, never report "no changes needed".
    """


def propose_patch_edits(
    llm: "LLMProvider",
    *,
    question: str,
    memo_markdown: str,
    document_markdown: str,
    language: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout: float,
) -> tuple[EditorInstructResult, int]:
    """One instruct call proposing edits; returns ``(result, tokens)``.

    Raises:
        PatchProposalFailed: Timeout, parse/validation failure, or a
            document over the instruct context budget — logged loudly,
            never swallowed, and mapped to a HARD run failure by the caller.
    """
    from inqtrix.services.editor_assist_service import (
        EditorDocumentTooLarge,
        run_editor_instruct,
    )

    request = EditorInstructRequestData(
        instruction=build_agent_patch_instruction(
            question, has_memo=bool(memo_markdown.strip())
        ),
        document_markdown=document_markdown,
        locale="en" if language.lower().startswith("en") else "de",
        reference_documents=(
            (
                ReferenceDocument(
                    label="Memo",
                    content=memo_markdown,
                    page_count=None,
                    size_bytes=None,
                ),
            )
            if memo_markdown.strip()
            else ()
        ),
        reference_warnings=(),
    )
    structured_supported = bool(
        getattr(llm, "supports_structured_output", None)
        and llm.supports_structured_output(model=model)
    )
    try:
        return run_editor_instruct(
            request,
            llm=llm,
            model=model,
            reasoning_effort=reasoning_effort,
            structured_supported=structured_supported,
            timeout_seconds=timeout,
        )
    except EditorDocumentTooLarge as exc:
        log.warning(
            "Agent patch: target document too large (error_type=%s).",
            type(exc).__name__,
        )
        raise PatchProposalFailed("patch_document_too_large") from exc
    except (EditorInstructError, TimeoutError) as exc:
        log.warning(
            "Agent patch: instruction pipeline failed (error_type=%s)",
            type(exc).__name__,
        )
        raise PatchProposalFailed("patch_proposal_failed") from exc
