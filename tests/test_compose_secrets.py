"""Contracts for the paired stack configuration and credential templates."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_EXAMPLE = _ROOT / "deploy" / ".env.stack.example"
_SECRETS_EXAMPLE = _ROOT / "deploy" / ".env.stack.secrets.example"
_MIGRATION_SECRETS_EXAMPLE = (
    _ROOT / "deploy" / ".env.migrate.secrets.example"
)
_STARTER_EXAMPLE = _ROOT / ".env.example"
_SENSITIVE_NAME = re.compile(
    r"(?:PASSWORD|SECRET|TOKEN|PEPPER|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
    r"KEY_?SEED|HEADERS)$|BEARER_TOKEN"
)
# Read by a library itself, not by an Inqtrix setting or a Compose file.
_LIBRARY_VARIABLES = frozenset({
    "OTEL_EXPORTER_OTLP_ENDPOINT",
})
_ASSIGNMENT = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$",
    re.MULTILINE,
)
_DOCUMENTED_ASSIGNMENT = re.compile(
    r"^\s*#?\s*(?P<name>[A-Z][A-Z0-9_]*)\s*=",
    re.MULTILINE,
)


def _assignments(path: Path) -> dict[str, str]:
    return {
        match.group("name"): match.group("value")
        for match in _ASSIGNMENT.finditer(path.read_text(encoding="utf-8"))
    }


def test_stack_template_separates_configuration_from_credentials() -> None:
    config = _assignments(_CONFIG_EXAMPLE)
    secrets = _assignments(_SECRETS_EXAMPLE)
    migration_secrets = _assignments(_MIGRATION_SECRETS_EXAMPLE)

    assert not set(config).intersection(secrets | migration_secrets)
    assert not set(secrets).intersection(migration_secrets)
    assert not any(_SENSITIVE_NAME.search(name) for name in config)
    assert secrets["INQTRIX_PG_PASSWORD"] == "CHANGE_ME_PG_PASSWORD"
    assert config["INQTRIX_DATABASE_URL"] == (
        "postgresql+asyncpg://${INQTRIX_PG_USER:-inqtrix}:"
        "${INQTRIX_PG_PASSWORD}@postgres:5432/"
        "${INQTRIX_PG_DB:-inqtrix}"
    )
    assert config["INQTRIX_ENV_FILE"] == "deploy/.env.stack"
    assert config["INQTRIX_SECRETS_FILE"] == (
        "deploy/.env.stack.secrets"
    )
    assert "INQTRIX_VALKEY_URL" not in secrets
    assert (
        "INQTRIX_VALKEY_URL=redis://:${INQTRIX_VALKEY_PASSWORD}"
        "@valkey:6379/0"
    ) in _CONFIG_EXAMPLE.read_text(encoding="utf-8")
    assert {
        "INQTRIX_SESSION_SECRET",
        "INQTRIX_PAT_PEPPER",
    }.issubset(secrets)
    assert "LITELLM_API_KEY" not in secrets
    assert "PERPLEXITY_API_KEY" not in secrets
    secrets_text = _SECRETS_EXAMPLE.read_text(encoding="utf-8")
    assert "# LITELLM_API_KEY=CHANGE_ME_LITELLM_KEY" in secrets_text
    assert "# PERPLEXITY_API_KEY=CHANGE_ME_PERPLEXITY_KEY" in secrets_text


def test_stack_templates_document_each_key_in_exactly_one_place() -> None:
    """Copy/edit instructions must never create parallel assignment recipes."""
    documented: dict[str, list[str]] = {}
    for path in (
        _CONFIG_EXAMPLE,
        _SECRETS_EXAMPLE,
        _MIGRATION_SECRETS_EXAMPLE,
    ):
        for match in _DOCUMENTED_ASSIGNMENT.finditer(
            path.read_text(encoding="utf-8")
        ):
            documented.setdefault(match.group("name"), []).append(path.name)

    duplicates = {
        name: locations
        for name, locations in documented.items()
        if len(locations) > 1
    }
    assert not duplicates, (
        "stack templates document parallel assignment recipes: "
        f"{duplicates}"
    )

    misplaced_secrets = sorted(
        name
        for name, locations in documented.items()
        if _SENSITIVE_NAME.search(name)
        and (
            len(locations) != 1
            or locations[0] not in {
                _SECRETS_EXAMPLE.name,
                _MIGRATION_SECRETS_EXAMPLE.name,
            }
        )
    )
    assert not misplaced_secrets, (
        "secret-shaped assignments must live only in the secrets template: "
        f"{misplaced_secrets}"
    )


def _read_variable_names() -> set[str]:
    """Every variable an Inqtrix setting, the logging setup or Compose reads."""
    from pydantic import AliasChoices
    from pydantic_settings import BaseSettings

    import inqtrix.settings as settings_module
    from inqtrix.logging_config import LoggingSettings

    names = set(_LIBRARY_VARIABLES)
    for value in vars(settings_module).values():
        if not (isinstance(value, type) and issubclass(value, BaseSettings)):
            continue
        for field_name, field in value.model_fields.items():
            names.add((field.alias or field_name).upper())
            if isinstance(field.validation_alias, AliasChoices):
                names.update(
                    str(choice).upper()
                    for choice in field.validation_alias.choices
                )
    prefix = LoggingSettings.model_config["env_prefix"]
    names.update(
        f"{prefix}{name}".upper() for name in LoggingSettings.model_fields
    )
    for path in (_ROOT / "deploy" / "compose").glob("*.yaml"):
        names.update(
            re.findall(
                r"\$\{([A-Z][A-Z0-9_]*)", path.read_text(encoding="utf-8")
            )
        )
    return names


def test_every_variable_the_stack_template_documents_is_read() -> None:
    """A documented variable that nothing reads misleads whoever sets it."""
    documented = set(
        _DOCUMENTED_ASSIGNMENT.findall(
            _CONFIG_EXAMPLE.read_text(encoding="utf-8")
        )
    )

    unread = sorted(documented - _read_variable_names())

    assert not unread, (
        f"the stack template documents variables nothing reads: {unread}"
    )


def test_every_credential_the_stack_template_names_has_a_secrets_line() -> None:
    """A credential the config template sends to the secrets file is offered there.

    The config template tells the operator, for example, to set
    AWS_BEARER_TOKEN_BEDROCK in deploy/.env.stack.secrets; without a line to
    uncomment in the credential template that instruction points at nothing.
    """
    named = {
        name
        for name in re.findall(
            r"\b[A-Z][A-Z0-9_]{2,}\b",
            _CONFIG_EXAMPLE.read_text(encoding="utf-8"),
        )
        if _SENSITIVE_NAME.search(name)
    }
    offered = set(
        _DOCUMENTED_ASSIGNMENT.findall(
            _SECRETS_EXAMPLE.read_text(encoding="utf-8")
        )
    )

    missing = sorted(named - offered)

    assert not missing, (
        "the stack template names credentials the secrets template never "
        f"offers: {missing}"
    )


@pytest.mark.parametrize(
    "template", [_CONFIG_EXAMPLE, _SECRETS_EXAMPLE, _STARTER_EXAMPLE], ids=lambda path: path.name
)
def test_the_env_templates_name_no_retired_setting(template: Path) -> None:
    """A retired name only confuses: it does nothing or stops the start."""
    from inqtrix.settings import RETIRED_SETTINGS

    text = template.read_text(encoding="utf-8")

    named = [entry.name for entry in RETIRED_SETTINGS if entry.name in text]

    assert not named, (
        f"{template.name} still names retired settings: {named}"
    )


def test_the_stack_templates_show_the_defaults_of_the_database_pool() -> None:
    """Uncommenting a documented pool value changes nothing."""
    from inqtrix.settings import StorageSettings

    compose = _CONFIG_EXAMPLE.read_text(encoding="utf-8")
    helm = (_ROOT / "deploy" / "helm" / "inqtrix" / "values.yaml").read_text(
        encoding="utf-8"
    )
    for name in ("pool_size", "pool_max_overflow", "pool_timeout_seconds"):
        field = StorageSettings.model_fields[name]
        shown = {
            "compose": re.search(
                rf"^# {field.alias}=(\S+)$", compose, re.MULTILINE
            ),
            "helm": re.search(
                rf'^\s*# {field.alias}: "([^"]+)"$', helm, re.MULTILINE
            ),
        }
        for template, match in shown.items():
            assert match is not None, f"{template} does not show {field.alias}"
            assert float(match.group(1)) == float(field.default), (
                f"{template} shows {field.alias}={match.group(1)}, "
                f"the default is {field.default}"
            )


def test_bundled_s3_example_uses_a_valid_non_empty_region() -> None:
    """Uncommenting the adjacent SeaweedFS block must remain startable."""
    config_text = _CONFIG_EXAMPLE.read_text(encoding="utf-8")

    assert "# INQTRIX_S3_REGION=us-east-1" in config_text
    assert "# INQTRIX_S3_REGION=" not in config_text.replace(
        "# INQTRIX_S3_REGION=us-east-1",
        "",
    )


def test_parallel_secret_materialization_files_are_retired() -> None:
    retired = (
        "/".join(("compose", "compose.dev" + ".yaml")),
        "/".join(("compose", "compose.e2e-local" + ".yaml")),
        "/".join(("compose", "prepare-stack-" + "secrets.sh")),
        "/".join(("compose", "seaweedfs-" + "s3.json")),
        "/".join(("scripts", "compose-owner-" + "upgrade.sh")),
    )

    assert all(not (_ROOT / "deploy" / path).exists() for path in retired)


def test_every_profile_secret_the_preflight_demands_is_offered_in_the_template()\
        -> None:
    """A profile the preflight gates on must have a line to uncomment.

    `required_profile_secrets` refuses to start a profile whose secret is
    unset, so an operator who copies the two templates and follows the
    profile's instructions must find the name in the credential template.
    Without this contract a neighbouring edit can carry a still-required
    line away silently -- removing the ldap lines around
    INQTRIX_OIDC_CLIENT_SECRET, for example, can take it along.
    """
    from inqtrix.deploy.preflight import REQUIRED_PROFILE_SECRETS

    offered = set(
        _DOCUMENTED_ASSIGNMENT.findall(
            _SECRETS_EXAMPLE.read_text(encoding="utf-8")
        )
    )
    missing = {
        name: profile
        for profile, names in REQUIRED_PROFILE_SECRETS.items()
        for name in names
        if name not in offered
    }
    assert not missing, (
        "the preflight refuses these profiles without a secret the "
        f"credential template never mentions: {missing}"
    )
