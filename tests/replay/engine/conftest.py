"""Engine replay: the request body takes part in the match.

The provider replays next door leave the body out, because they prove what the
adapter does with an answer. Engine replays prove the opposite direction: a
moved engine still sends the same model input. A changed request is a loud miss
(``CannotOverwriteExistingCassetteException``), never a live call.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from tests.replay.engine.body_matcher import same_normalized_body


def pytest_recording_configure(config: pytest.Config, vcr: Any) -> None:
    vcr.register_matcher("normalized_body", same_normalized_body)


@pytest.fixture(scope="session")
def vcr_config(vcr_config: dict) -> dict:
    return {**vcr_config, "match_on": [*vcr_config["match_on"], "normalized_body"]}


@pytest.fixture(scope="module")
def vcr_cassette_dir() -> str:
    return str(
        pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "cassettes" / "engine"
    )
