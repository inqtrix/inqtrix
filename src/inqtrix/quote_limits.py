"""The minimum length of a verbatim quote, and the one way to count a
quote's words.

This module is a leaf: on import it loads ``re`` only, so the schema and
prompt modules that state the minimum to a model read it without pulling in
the knowledge stack. A document citation (whose minimum a document analysis
states to its model) and the re-check of a revision (a quote from a source
passage or from the finding's words) need :data:`CITE_QUOTE_MIN_WORDS` as
:func:`quote_word_count` counts them. A quote from a reading record has its
own minimum beside its check
(:data:`~inqtrix.agents.kernel.review_pass.RECORD_QUOTE_MIN_WORDS`), counted
the same way.
"""

from __future__ import annotations

import re

CITE_QUOTE_MIN_WORDS = 3
"""Words a quote needs to prove anything: a letter, a number or a lone word
is verbatim in almost every window and would verify any claim beside it."""


def quote_word_count(text: str) -> int:
    """The words of the quote *text*: ``\\w+`` over its normalized key
    (:func:`~inqtrix.knowledge.grounding.quote_key`)."""
    # Imported here, not at the top: the knowledge package loads its stack on
    # import, which would reach the schema and prompt modules that only read
    # the constant. Every caller of this function has loaded grounding already.
    from inqtrix.knowledge.grounding import quote_key

    return len(re.findall(r"\w+", quote_key(text)))
