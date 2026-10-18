"""Contracts for the paired stack configuration and credential templates."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_EXAMPLE = _ROOT / "deploy" / ".env.stack.example"
_SECRETS_EXAMPLE = _ROOT / "deploy" / ".env.stack.secrets.example"
_MIGRATION_SECRETS_EXAMPLE = (
    _ROOT / "deploy" / ".env.migrate.secrets.example"
)
_SENSITIVE_NAME = re.compile(
    r"(?:PASSWORD|SECRET|TOKEN|PEPPER|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
    r"KEY_?SEED)$"
)
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
