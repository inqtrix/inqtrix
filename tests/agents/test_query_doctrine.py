"""ONE query doctrine across every Agent-Desk site.

Everywhere the agent platform instructs query shape it asks for a
self-contained, naturally phrased evidence question, never for a keyword
chain. These pins are SITE-precise: each site must carry the doctrine
wording itself, so a later edit cannot silently revive keyword
instructions at one surface while the others moved on. The research-desk
pipeline (graph.py, src/inqtrix/prompts.py) is deliberately NOT covered
by this doctrine.
"""

from __future__ import annotations



from inqtrix.agents.kernel.tools import build_kernel_tools
from inqtrix.agents.prompts import build_agent_kernel_system_prompt
from inqtrix.prompt_language import QUESTION_LANGUAGE_DIRECTIVE, directive_for

DOCTRINE = "self-contained, naturally phrased evidence question"
ANTI_KEYWORD = "no keyword chain"


def test_kernel_answer_closure_keeps_requested_next_steps_and_relevant_limits():
    prompt = build_agent_kernel_system_prompt()
    assert "without unsolicited follow-up offers" in prompt
    assert "without reproducing internal run rules" in prompt
    assert "Explicitly requested next steps" in prompt
    assert "necessary clarifying questions" in prompt
    assert "substantively relevant limits or open points are kept" in prompt


def test_kernel_system_prompt_carries_the_doctrine() -> None:
    prompt = build_agent_kernel_system_prompt()
    assert DOCTRINE in prompt
    assert ANTI_KEYWORD in prompt
    assert "SEARCH QUERY" not in prompt
    assert "keywords" not in prompt
    # The recency rule folds the meant time range INTO the evidence
    # question instead of a provider-side recency filter alone.
    assert "put the intended period" in prompt
    assert "explicitly into the evidence question" in prompt


def test_web_instant_tool_description_carries_the_doctrine() -> None:
    tools = build_kernel_tools()
    web_instant = next(tool for tool in tools if tool.name == "web_instant")
    # The docstring wraps lines; compare the flowed text, not the wrapping.
    description = " ".join(web_instant.description.split())
    assert "self-contained, naturally phrased evidence question" in description
    assert "keyword chain" in description
    assert "SEARCH QUERY" not in description
    assert "keywords" not in description


def test_the_revision_prompt_demands_the_target_language_s_orthography():
    """A review revision writes new prose into a finished text; without the
    rule it could reintroduce ASCII umlauts the writer avoided — the German
    directive with the rule is the FIRST line of the revision prompt."""
    from inqtrix.agents.prompts import build_deep_revision_prompt

    prompt = build_deep_revision_prompt("Frage", "{}", [], language="de")
    assert prompt.split("\n", 1)[0] == directive_for("de")
    assert "Umlauten (ä, ö, ü, ß)" in prompt
    assert "nie als ae/oe/ue/ss" in prompt
    neutral = build_deep_revision_prompt("Frage", "{}", [])
    assert neutral.split("\n", 1)[0] == QUESTION_LANGUAGE_DIRECTIVE


def test_kernel_output_routing_decides_by_form_not_by_length() -> None:
    """Routing on a line count sends explanation questions to the canvas:
    under a "roughly under 15 lines" threshold any long explanation reads
    as a self-contained artifact. Length is not a measure of
    document-worthiness — the form of the result is.
    """
    prompt = build_agent_kernel_system_prompt()
    assert "no matter how long the answer turns out" in prompt
    assert "Length is not a criterion" in prompt
    assert "a long explanation stays an explanation" in prompt
    # The line-count threshold must not creep back in.
    assert "under 15 lines" not in prompt


def test_kernel_output_routing_keeps_the_document_text_out_of_the_chat() -> None:
    """A document whose text is repeated in the chat is written twice,
    paragraph for paragraph. Demanding the short note only for an UPDATE
    of an existing document leaves the first creation unruled and is
    enough to produce that duplicate, so the rule covers writing and
    updating alike, and "short" carries a countable bound instead of an
    adjective.
    """
    prompt = build_agent_kernel_system_prompt()
    assert "Whenever you write or update a canvas document" in prompt
    assert "at most three sentences" in prompt
    assert "You never repeat the document text in the chat" in prompt


def test_kernel_output_routing_keeps_its_document_addressing_rules() -> None:
    """Routing by form is only half of the output contract: an explicit
    wish of the user outranks every criterion, an existing document is
    addressed through the session context's expected_revision rather than
    by artifact_id, and an unclear target is asked about, not guessed."""
    prompt = build_agent_kernel_system_prompt()
    assert "An explicit wish of the user always overrides these criteria" in prompt
    assert "expected_revision from the session context" in prompt
    assert "never by the artifact_id" in prompt
    assert "ask with ask_user" in prompt
