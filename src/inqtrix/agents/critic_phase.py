"""The basis vocabulary of the review pass: how a finding names its ground.

A finding grounds itself on a ledger reference or on a run diagnostic; both
are named by a prefix plus an id, and the prompt, the producers, the guard
and the reader projection all read the prefixes defined here.
"""

from __future__ import annotations

SOURCE_BASIS_PREFIX = "source:"
"""How a finding names a ledger reference: this prefix plus the reference id."""

DIAGNOSTIC_BASIS_PREFIX = "diagnostic:"
"""How a finding names a run diagnostic: this prefix plus the diagnostic's path.

Both prefixes are the vocabulary of ONE contract — the prompt that asks for
a basis_id, the producers that mint one, the guard that confirms it and the
reader projection that resolves it back into a name. Spelled once, so a
change cannot reach three of those four and leave the fifth reading raw ids.
"""


