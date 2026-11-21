"""One policy derives web-research permission and profile in every agent."""

import pytest

from inqtrix.agents.web_execution_policy import derive_web_research_policy


def test_normal_adaptive_research_always_uses_compact() -> None:
    automatic = derive_web_research_policy(depth="normal")
    assert (automatic.allowed, automatic.profile) == (True, "compact")


def test_deep_always_uses_deep_and_invalid_depth_fails_loudly() -> None:
    deep = derive_web_research_policy(depth="deep")
    assert (deep.allowed, deep.profile) == (True, "deep")
    with pytest.raises(ValueError):
        derive_web_research_policy(depth="invalid")  # type: ignore[arg-type]
