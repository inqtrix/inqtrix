"""Contract tests for the public Python (library-mode) surface.

The structural rebuild must keep these constructors and signatures
byte-compatible: ``AgentConfig`` (zero-arg, ``extra="forbid"``),
``ProviderContext`` (frozen two-field dataclass), ``ResearchAgent``
(lazy, zero-arg), and the keyword-only ``create_app`` factory params.
Extensions are additive; nothing asserted here may be removed or
renamed without a deprecation path.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest
from pydantic import ValidationError

from inqtrix import ResearchAgent
from inqtrix.agent import AgentConfig
from inqtrix.providers.base import ProviderContext
from inqtrix.server.app import create_app


def test_agent_config_constructs_with_zero_args():
    config = AgentConfig()
    assert config.llm is None
    assert config.search is None


def test_agent_config_rejects_unknown_fields_loudly():
    with pytest.raises(ValidationError):
        AgentConfig(unknown_future_field=True)


def test_provider_context_is_frozen_two_field_dataclass():
    field_names = [field.name for field in dataclasses.fields(ProviderContext)]
    assert field_names == ["llm", "search"], (
        "ProviderContext must keep exactly (llm, search); new provider "
        "kinds belong in a sibling context, not here."
    )

    context = ProviderContext(llm=object(), search=object())
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.llm = object()


def test_research_agent_constructs_lazily_with_zero_args():
    """No provider construction, no env reads at __init__ time."""
    agent = ResearchAgent()
    assert isinstance(agent.config, AgentConfig)


def test_create_app_keyword_only_parameters_are_stable():
    signature = inspect.signature(create_app)
    parameters = signature.parameters

    for name in ("settings", "providers", "strategies"):
        assert name in parameters, f"create_app lost parameter {name!r}"
        parameter = parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None
