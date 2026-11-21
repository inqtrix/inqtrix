"""Helpers several agent test modules share.

The English-prompt check every prompt pin uses, and one skill created
through the skill service the way a client creates it.
"""

from __future__ import annotations

import asyncio

# Words that only occur in German instructions; an English prompt that
# carries one of them has a German sentence in it.
GERMAN_INSTRUCTION_TOKENS = (
    "Du ",
    "Werkzeug",
    "Nutzer",
    "Rueckfrage",
    "Belege",
    "Antwort",
    " nicht ",
    " und ",
    "keine ",
    "fuer ",
    "Auftrag:",
)


def _assert_english(text: str) -> None:
    hits = [token for token in GERMAN_INSTRUCTION_TOKENS if token in text]
    assert not hits, f"German instruction tokens in prompt: {hits}"


SKILL_PAYLOAD = {
    "label": "sprechzettel",
    "title": "Sprechzettel",
    "description": "Kompakter Sprechzettel.",
    "when_to_use": "Fuer Termine.",
    "instructions_markdown": (
        "Erstelle einen Sprechzettel fuer {{anlass}} mit Blick auf "
        "{{publikum}}."
    ),
    "clarification_points": [
        {
            "name": "anlass",
            "question": "Fuer welchen Anlass?",
            "options": [
                {"label": "Vorstandssitzung"},
                {"label": "Kundentermin"},
            ],
            "required": True,
            "default_assumption": "Interner Termin",
        },
        {
            "name": "publikum",
            "question": "Wer ist das Publikum?",
            "options": [],
            "required": False,
            "default_assumption": "Fachpublikum",
        },
    ],
    "deliverable": "",
    "requires_plan": "never",
    "invocation": "model_allowed",
}


def _create_skill(client, **overrides) -> str:
    payload = {**SKILL_PAYLOAD, **overrides}
    service = client.container.skill_service
    record = asyncio.run(
        service.create(payload, tenant_id="default", owner_user_id=None)
    )
    return record.id
