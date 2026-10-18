"""Tests for environment-backed settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from inqtrix.settings import AgentSettings, ServerSettings


def test_agent_depth_is_normalized_once_at_settings_boundary() -> None:
    """Engines receive the canonical token instead of repeating fallbacks."""
    assert AgentSettings(DEPTH=" Deep ").depth == "deep"


def test_agent_depth_rejects_unknown_values() -> None:
    """A typo must fail visibly instead of silently becoming normal depth."""
    with pytest.raises(ValidationError):
        AgentSettings(DEPTH="thorough")


class TestServerSettings:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("MAX_CONCURRENT", 0),
            ("RUN_MAX_CONCURRENT", 0),
            ("RUN_QUEUE_MAX_SIZE", -1),
            ("RUN_COMPLETED_TTL_SECONDS", -1),
            ("RUN_EVENT_BUFFER_SIZE", 0),
        ],
    )
    def test_rejects_invalid_run_limits(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            ServerSettings(**{field: value})


def test_every_constant_is_the_single_definition_point_for_its_setting() -> None:
    """A constant and its settings field must not spell the value twice.

    ``constants.py`` carried MAX_ROUNDS = 4, CONFIDENCE_STOP = 8 and
    MAX_QUESTION_LENGTH = 10_000 while the enforced defaults were 2, 7 and
    60_000. Nothing read the constants, so nothing caught the divergence --
    and the documentation copied the dead values onto several pages, where a
    ground-up review later found them as "wrong defaults". The docs were not
    drifting; they were faithfully quoting a bypassed edit point.

    This pins the pairing by name rather than by an explicit list, so a
    constant added later is covered without anyone remembering to extend it.
    """
    import inspect

    from inqtrix import constants, settings as settings_module

    numeric_constants = {
        name: value
        for name, value in vars(constants).items()
        if name.isupper() and isinstance(value, int) and not isinstance(value, bool)
    }
    assert numeric_constants, "constants.py exposes no numeric constants any more"

    # AgentConfig holds a third copy of the same values, so it is checked
    # from the same pairing rather than left to drift on its own.
    from inqtrix import agent as agent_module

    settings_classes = [
        obj
        for module in (settings_module, agent_module)
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == module.__name__ and hasattr(obj, "model_fields")
    ]

    mismatches = []
    checked = 0
    for name, value in numeric_constants.items():
        for cls in settings_classes:
            field = cls.model_fields.get(name.lower())
            if field is None:
                continue
            checked += 1
            if field.default != value:
                mismatches.append(
                    f"{cls.__name__}.{name.lower()} defaults to {field.default!r} "
                    f"but constants.{name} is {value!r}"
                )

    assert checked, "no constant is paired with a settings field any more"
    assert not mismatches, (
        "a settings default no longer matches its constant -- change the "
        "constant, not the Field() literal:\n  " + "\n  ".join(mismatches)
    )
