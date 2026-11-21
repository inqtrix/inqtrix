"""One section states how the result has to look.

The kernel appends the user's attached requirement as ONE labelled section
of its user message, closed by one precedence text: a requirement must rank
the same way in every run, or the user cannot rely on it.
"""

from __future__ import annotations

from inqtrix.agents.prompts import build_kernel_user_message

# Composed at decision time (report_requirement) — the section frames
# it, it never labels text it did not compose.
GUIDANCE = "[Free requirement]\nZielgruppe: juristische Laien."

# The precedence a requirement carries, spelled out here rather than read
# from the module: a reword of the prompt must be caught by this file.
PRECEDENCE_CLAUSES = (
    # The platform contract wins: a rule's citation style never costs a label.
    "Keep every evidence label ([K1], [W2]) even where a rule prescribes "
    "another citation style; add that style next to the label.",
    # The user's own words in this run narrow a library rule: the
    # assignment, the free requirement they typed, their clarification
    # answers.
    "Where the assignment, the user's typed free requirement or the user's "
    "answers to clarifying questions in this run explicitly narrow or "
    "change a rule, they win.",
    # Rules among themselves: attachment order decides, and the user
    # learns about the conflict instead of finding one rule silently gone.
    "Where two attached rules conflict, follow the one attached first and "
    "name the conflict in the chat answer.",
    # A skill arrives attached or later through load_skill; nearness in the
    # transcript must not let it win.
    "Where a skill, attached or loaded, conflicts with this requirement, "
    "this requirement wins.",
    # Scope: deliverable documents, and the chat answer only as deliverable.
    "Apply each rule to every deliverable document the assignment applies "
    "it to — without such scoping, to every deliverable document — and to "
    "the chat answer only when the chat answer is the deliverable.",
)

def test_the_precedence_closes_the_requirement_it_ranks():
    """Stated after the requirement, so the label clause stands between a
    rule's own citation style and the writing instructions."""
    prompt = build_kernel_user_message("Auftrag.", report_requirement=GUIDANCE)
    assert prompt.index(GUIDANCE) < prompt.index(PRECEDENCE_CLAUSES[0])


def test_the_precedence_reads_from_the_highest_rank_down():
    """Platform contract, then the user's own words in this run, then the
    rules among themselves, then skills, then the scope: a model reading
    top-down meets each clause before the ones it outranks."""
    prompt = build_kernel_user_message("Auftrag.", report_requirement=GUIDANCE)
    positions = [prompt.index(clause) for clause in PRECEDENCE_CLAUSES]
    assert positions == sorted(positions), positions


def test_the_kernel_states_the_precedence_once_with_a_requirement():
    """The requirement arrives as one section with one precedence text; a
    run without a requirement carries neither."""
    kernel = build_kernel_user_message("Auftrag.", report_requirement=GUIDANCE)
    assert kernel.count("Output requirement (user content") == 1
    for clause in PRECEDENCE_CLAUSES:
        assert kernel.count(clause) == 1, clause
    assert kernel.index(PRECEDENCE_CLAUSES[0]) < kernel.index("Assignment:\n")
    plain = build_kernel_user_message("Auftrag.")
    assert "Output requirement (user content" not in plain
    assert not any(clause in plain for clause in PRECEDENCE_CLAUSES)
