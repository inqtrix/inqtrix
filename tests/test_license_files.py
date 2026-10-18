"""Pins the licence files against silent edits.

``LICENSE`` must stay the canonical Apache License 2.0 text as published by
the Apache Software Foundation, including the appendix with its untouched
``[yyyy] [name of copyright owner]`` placeholders: a modified licence text is
no longer "the Apache License" and downstream tooling (SPDX scanners, package
indexes) stops recognising it. ``NOTICE`` is the one file redistributors must
carry along (Apache-2.0 section 4(d)), so its wording is deliberate too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# sha256 of the canonical Apache-2.0 text (https://www.apache.org/licenses/LICENSE-2.0.txt)
# as committed in this repository. Changing LICENSE is a conscious licensing
# decision, never a side effect: update this constant only together with it.
CANONICAL_APACHE_2_0_SHA256 = (
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
)


def test_license_is_the_unmodified_apache_2_0_text() -> None:
    digest = hashlib.sha256((_ROOT / "LICENSE").read_bytes()).hexdigest()
    assert digest == CANONICAL_APACHE_2_0_SHA256, (
        "LICENSE no longer matches the canonical Apache-2.0 text "
        f"(sha256 {digest}); restore the official text or make the change "
        "a deliberate licensing decision and update the pin."
    )


def test_license_appendix_placeholders_are_untouched() -> None:
    text = (_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright [yyyy] [name of copyright owner]" in text
    assert text.lstrip().startswith("Apache License")
    assert "Version 2.0, January 2004" in text


# The NOTICE file is what redistributors must carry along (Apache-2.0
# section 4(d)). It states the attribution only: project name, copyright
# line and source URL; no licence boilerplate (that is LICENSE's job) and
# no paraphrase of the section that governs it.
EXPECTED_NOTICE = "Inqtrix\nCopyright (c) 2026 Babak Zandi\nhttps://github.com/inqtrix/inqtrix\n"


def test_notice_is_the_minimal_attribution() -> None:
    assert (_ROOT / "NOTICE").read_text(encoding="utf-8") == EXPECTED_NOTICE
