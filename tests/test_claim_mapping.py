"""Unit tests for the IdP-agnostic claim mapper (auth/mapping.py).

Covers the three axes the mapper exists to absorb — claim location
(dot-paths), value shape (array vs separator-string vs nested vs
distributed), and literal matching against allow/admin lists — without
any per-provider branch, mirroring the module's design.
"""

from __future__ import annotations

import logging

import pytest

from inqtrix.auth.mapping import (
    ClaimMappingConfig,
    DistributedClaimError,
    admission_error,
    claim_path,
    derive_is_admin,
    extract_groups,
    extract_roles,
    normalise_claim_values,
)


def test_claim_path_descends_and_misses():
    claims = {"realm_access": {"roles": ["admin"]}}
    assert claim_path(claims, "realm_access.roles") == ["admin"]
    assert claim_path(claims, "realm_access.missing") is None
    assert claim_path(claims, "resource_access.inqtrix.roles") is None


def test_normalise_array_string_and_separators():
    assert normalise_claim_values(
        ["a", "b"], separators=" ,", strip_path_prefix=False
    ) == ("a", "b")
    assert normalise_claim_values(
        "admin, staff", separators=" ,", strip_path_prefix=False
    ) == ("admin", "staff")
    assert normalise_claim_values(
        "admin staff", separators=" ,", strip_path_prefix=False
    ) == ("admin", "staff")


def test_normalise_none_and_nested_object_yield_nothing():
    assert normalise_claim_values(None, separators=" ,", strip_path_prefix=False) == ()
    assert normalise_claim_values(
        {"id": "x"}, separators=" ,", strip_path_prefix=False
    ) == ()


def test_normalise_strips_keycloak_path_prefix_only_when_enabled():
    paths = ["/Engineering/Backend", "/Ops"]
    assert normalise_claim_values(
        paths, separators=" ,", strip_path_prefix=False
    ) == ("/Engineering/Backend", "/Ops")
    assert normalise_claim_values(
        paths, separators=" ,", strip_path_prefix=True
    ) == ("Engineering/Backend", "Ops")


def test_extract_groups_required_distributed_raises():
    cfg = ClaimMappingConfig(allowed_groups=frozenset({"team-a"}))
    claims = {
        "_claim_names": {"groups": "src1"},
        "_claim_sources": {"src1": {"endpoint": "https://graph.example/x"}},
    }
    with pytest.raises(DistributedClaimError):
        extract_groups(claims, cfg)


def test_distributed_pointer_with_inline_value_is_admitted():
    # A userinfo merge can leave the _claim_names pointer in place while
    # the value is resolved inline; the present value must win (no 403).
    cfg = ClaimMappingConfig(allowed_groups=frozenset({"team-a"}))
    claims = {
        "groups": ["team-a"],
        "_claim_names": {"groups": "src1"},
        "_claim_sources": {"src1": {"endpoint": "https://graph.example/x"}},
    }
    assert extract_groups(claims, cfg) == ("team-a",)
    assert admission_error(extract_groups(claims, cfg), "a@x.io", cfg) is None


def test_wildcard_allowlist_admits_despite_distributed_groups():
    # "*" admits any authenticated user, so an unresolved distributed
    # groups claim must NOT hard-reject the login.
    cfg = ClaimMappingConfig(allowed_groups=frozenset({"*"}))
    claims = {"_claim_names": {"groups": "src1"}}
    assert extract_groups(claims, cfg) == ()
    assert admission_error((), "a@x.io", cfg) is None


def test_extract_roles_distributed_degrades_to_empty_and_warns(caplog):
    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    try:
        cfg = ClaimMappingConfig()
        claims = {"_claim_names": {"roles": "src1"}}
        assert extract_roles(claims, cfg) == ()
        assert any("distributed claim" in r.message for r in caplog.records)
    finally:
        logger.removeHandler(caplog.handler)


def test_derive_is_admin_matches_roles_or_groups():
    cfg = ClaimMappingConfig(
        admin_roles=frozenset({"inqtrix-admin"}),
        admin_groups=frozenset({"ops"}),
    )
    assert derive_is_admin((), ("inqtrix-admin",), cfg) is True
    assert derive_is_admin(("ops",), (), cfg) is True
    assert derive_is_admin(("dev",), ("viewer",), cfg) is False


def test_admission_group_allowlist_and_wildcard():
    gated = ClaimMappingConfig(allowed_groups=frozenset({"team-a"}))
    assert admission_error(("team-a",), "a@x.io", gated) is None
    assert admission_error(("team-b",), "a@x.io", gated) is not None
    wildcard = ClaimMappingConfig(allowed_groups=frozenset({"*"}))
    assert admission_error((), "a@x.io", wildcard) is None


def test_admission_domain_allowlist_rejects_mismatch_and_missing_email():
    cfg = ClaimMappingConfig(allowed_domains=frozenset({"corp.example"}))
    assert admission_error((), "bob@corp.example", cfg) is None
    assert admission_error((), "bob@other.example", cfg) is not None
    assert admission_error((), None, cfg) is not None


def test_admission_domain_match_is_case_insensitive_and_whitespace_tolerant():
    cfg = ClaimMappingConfig(allowed_domains=frozenset({"corp.example"}))
    assert admission_error((), "Alice@CORP.EXAMPLE", cfg) is None
    assert admission_error((), " bob@corp.example ", cfg) is None
