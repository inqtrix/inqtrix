"""Bounds of the canvas attachment — visible rejection, never a cut.

Every limit REJECTS with the offending field named. A request either
arrives with its full content or the caller learns exactly why not
(no-silent-caps doctrine); there is no truncation path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from inqtrix.core.results import (
    CANVAS_COMMENT_MAX_CHARS,
    CANVAS_CONTEXT_MAX_COMMENTS,
    CANVAS_QUOTE_MAX_CHARS,
    CanvasContext,
)


def _comment(**overrides: object) -> dict[str, object]:
    return {
        "artifact_id": "art_1",
        "revision": 2,
        "quote": "Der Umsatz stieg.",
        "comment": "Bitte praezisieren.",
        **overrides,
    }


def test_valid_context_roundtrips_verbatim() -> None:
    context = CanvasContext.model_validate(
        {
            "artifact_id": "art_1",
            "revision": 2,
            "comments": [
                _comment(quote_before="Kapitel 2: ", quote_after=" Danach")
            ],
        }
    )
    dumped = context.model_dump(mode="json")
    assert dumped["comments"][0]["quote"] == "Der Umsatz stieg."
    assert dumped["comments"][0]["quote_before"] == "Kapitel 2: "
    assert CanvasContext.model_validate(dumped) == context


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CanvasContext.model_validate(
            {"artifact_id": "art_1", "revision": 2, "run_id": "r1"}
        )
    with pytest.raises(ValidationError):
        CanvasContext.model_validate(
            {
                "artifact_id": "art_1",
                "revision": 2,
                "comments": [_comment(run_id="r1")],
            }
        )


def test_bounds_reject_instead_of_truncating() -> None:
    over_long_quote = "x" * (CANVAS_QUOTE_MAX_CHARS + 1)
    with pytest.raises(ValidationError):
        CanvasContext.model_validate(
            {
                "artifact_id": "art_1",
                "revision": 2,
                "comments": [_comment(quote=over_long_quote)],
            }
        )
    # Exactly AT the bound the full text survives — proof the limit is
    # a rejection threshold, not a truncation point.
    at_bound = CanvasContext.model_validate(
        {
            "artifact_id": "art_1",
            "revision": 2,
            "comments": [_comment(comment="y" * CANVAS_COMMENT_MAX_CHARS)],
        }
    )
    assert len(at_bound.comments[0].comment) == CANVAS_COMMENT_MAX_CHARS

    too_many = [
        _comment() for _ in range(CANVAS_CONTEXT_MAX_COMMENTS + 1)
    ]
    with pytest.raises(ValidationError):
        CanvasContext.model_validate(
            {"artifact_id": "art_1", "revision": 2, "comments": too_many}
        )


def test_empty_required_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CanvasContext.model_validate({"artifact_id": "", "revision": 2})
    with pytest.raises(ValidationError):
        CanvasContext.model_validate({"artifact_id": "art_1", "revision": 0})
    with pytest.raises(ValidationError):
        CanvasContext.model_validate(
            {
                "artifact_id": "art_1",
                "revision": 2,
                "comments": [_comment(comment="")],
            }
        )
