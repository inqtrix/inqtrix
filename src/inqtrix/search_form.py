"""The form a caller may ask of a web search answer.

A dependency-free leaf module: the tool that offers the choice, the
capability that checks it and the search provider that sends it share the
names of the forms and the one text each of them stands for.

The form of a search answer is how one search lays out what it found. It
is not the answer contract of a research run, which decides the sections
of a report.
"""

from __future__ import annotations

from typing import Literal, get_args

AnswerForm = Literal["overview", "table", "timeline"]
"""The forms a search can be asked to answer in."""

ANSWER_FORMS: tuple[str, ...] = get_args(AnswerForm)

DEFAULT_ANSWER_FORM = "overview"
"""The form of an answer nobody asked a form of. Choosing it sends nothing:
the request is the one a search without a choice sends."""

ANSWER_FORM_PARAMETER = "answer_form"
"""The name a search provider lists among its search parameters to say that
it takes a form. A provider that does not list it is never handed one."""

_OPENING = "About the form of your answer only; search as you normally would."
_FORM_LINES = {
    "table": (
        "Form: a table with one row per value (value, unit, period, scope, "
        "source); mark values the sources do not give."
    ),
    "timeline": (
        "Form: the events of the period, newest first, each with its date and "
        "primary source."
    ),
}
# Sent to a search provider beside the bare question, so it names no
# language: the question is the only place the provider can read one from.
_LANGUAGE = "Answer in the language of the question."


def answer_form_brief(form: str | None) -> str | None:
    """The brief a search provider sends for *form*, or None where it sends none.

    None for no form, for the default form and for any name that is no
    form, so a provider never sends a text of the caller's own. The brief
    says how to lay the answer out and that it stays in the language of
    the question; it names no language and says nothing about what to
    search.
    """
    line = _FORM_LINES.get(form or "")
    return f"{_OPENING}\n{line}\n{_LANGUAGE}" if line else None
