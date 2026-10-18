"""Storage configuration contracts for managed PostgreSQL and S3."""

from __future__ import annotations

import pytest

from inqtrix.settings import StorageSettings


def test_migration_settings_keep_backwards_compatible_defaults() -> None:
    settings = StorageSettings()

    assert settings.migration_database_url == ""
    assert settings.migration_rls_mode == "auto"
    assert settings.migration_services_quiesced is False
    assert settings.runtime_login_policy == "restricted"


def test_managed_s3_default_chain_needs_no_explicit_endpoint_or_keys() -> None:
    settings = StorageSettings(
        object_store_backend="s3",
        s3_auth_mode="default",
        s3_bucket="managed-bucket",
        s3_addressing_style="auto",
        s3_bucket_provisioning="existing",
    )

    assert settings.s3_endpoint_url == ""
    assert settings.s3_access_key == ""
    assert settings.s3_secret_key == ""


def test_managed_s3_default_chain_rejects_shadowing_static_credentials() -> None:
    with pytest.raises(ValueError, match="must not set Inqtrix static"):
        StorageSettings(
            object_store_backend="s3",
            s3_auth_mode="default",
            s3_bucket="managed-bucket",
            s3_access_key="stale-access",
            s3_secret_key="stale-secret",
        )


def test_static_s3_accepts_temporary_session_credentials() -> None:
    settings = StorageSettings(
        object_store_backend="s3",
        s3_auth_mode="static",
        s3_endpoint_url="https://objects.example.test",
        s3_bucket="files",
        s3_access_key="access",
        s3_secret_key="secret",
        s3_session_token="session",
    )

    assert settings.s3_session_token == "session"


def test_s3_credentials_are_trimmed_before_contract_validation() -> None:
    settings = StorageSettings(
        object_store_backend="s3",
        s3_auth_mode="static",
        s3_bucket="files",
        s3_access_key="  access  ",
        s3_secret_key="  secret  ",
        s3_session_token="   ",
    )

    assert settings.s3_access_key == "access"
    assert settings.s3_secret_key == "secret"
    assert settings.s3_session_token == ""

    with pytest.raises(ValueError, match="requires"):
        StorageSettings(
            object_store_backend="s3",
            s3_auth_mode="static",
            s3_bucket="files",
            s3_access_key="   ",
            s3_secret_key="   ",
        )


def test_s3_kms_key_is_rejected_without_kms_encryption() -> None:
    with pytest.raises(ValueError, match="INQTRIX_S3_KMS_KEY_ID"):
        StorageSettings(
            object_store_backend="s3",
            s3_bucket="files",
            s3_access_key="access",
            s3_secret_key="secret",
            s3_kms_key_id="arn:aws:kms:eu-central-1:123:key/example",
        )
