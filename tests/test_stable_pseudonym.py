"""Instance-stable pseudonyms (INQTRIX_PSEUDONYM_PEPPER).

The pepper turns the per-process log references into instance-stable
ones: the same person carries the SAME ``usr_<hex16>`` reference in the
API server, in every worker, and across restarts. Without a pepper the
historical per-process behaviour (and its one-time WARNING) applies.
"""

from __future__ import annotations

import logging
import re
import uuid

import pytest

from inqtrix.auth import log_redaction
from inqtrix.auth.log_redaction import (
    configure_stable_pseudonyms,
    pseudonymous_log_reference,
    stable_pseudonym,
    stable_pseudonyms_active,
)

_REFERENCE_FORMAT = re.compile(r"^usr_[0-9a-f]{16}$")


@pytest.fixture(autouse=True)
def _reset_pseudonym_state():
    """Restore the module state so tests cannot leak a configured pepper."""
    saved_key = log_redaction._stable_key
    saved_warned = log_redaction._fallback_warned
    log_redaction._stable_key = None
    log_redaction._fallback_warned = False
    yield
    log_redaction._stable_key = saved_key
    log_redaction._fallback_warned = saved_warned


def test_same_pepper_yields_identical_references_across_processes():
    """Two processes sharing the pepper agree on every reference —
    reconfiguring simulates the second process (fresh module state)."""
    user_id = uuid.uuid4()
    assert configure_stable_pseudonyms("shared-instance-pepper") is True
    first = pseudonymous_log_reference("usr", user_id)

    # Simulate the worker process: fresh module state, same pepper.
    log_redaction._stable_key = None
    assert configure_stable_pseudonyms("shared-instance-pepper") is True
    second = pseudonymous_log_reference("usr", user_id)

    assert first == second
    assert _REFERENCE_FORMAT.fullmatch(first)
    assert stable_pseudonyms_active() is True


def test_different_pepper_breaks_linkability():
    user_id = uuid.uuid4()
    configure_stable_pseudonyms("pepper-one")
    first = pseudonymous_log_reference("usr", user_id)
    configure_stable_pseudonyms("pepper-two")
    assert pseudonymous_log_reference("usr", user_id) != first


def test_stable_pseudonym_matches_log_reference():
    """One derivation: durable surfaces and log lines must agree."""
    user_id = uuid.uuid4()
    configure_stable_pseudonyms("shared-instance-pepper")
    assert stable_pseudonym("usr", user_id) == pseudonymous_log_reference(
        "usr", user_id
    )


def test_namespaces_stay_domain_separated():
    subject = uuid.uuid4()
    configure_stable_pseudonyms("shared-instance-pepper")
    usr = stable_pseudonym("usr", subject)
    res = stable_pseudonym("res", subject)
    assert usr.split("_", 1)[1] != res.split("_", 1)[1]


def test_missing_identifier_is_explicit_none():
    configure_stable_pseudonyms("shared-instance-pepper")
    assert stable_pseudonym("usr", None) == "none"
    assert stable_pseudonym("usr", "") == "none"


def test_empty_pepper_falls_back_with_one_warning(caplog):
    inqtrix_logger = logging.getLogger("inqtrix")
    inqtrix_logger.addHandler(caplog.handler)
    previous_level = inqtrix_logger.level
    previous_propagate = inqtrix_logger.propagate
    inqtrix_logger.setLevel(logging.WARNING)
    # Only the explicitly attached handler may capture: with propagation
    # a second (root) capture would double-count the single emission.
    inqtrix_logger.propagate = False
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            assert configure_stable_pseudonyms("") is False
            assert configure_stable_pseudonyms(None) is False
    finally:
        inqtrix_logger.removeHandler(caplog.handler)
        inqtrix_logger.setLevel(previous_level)
        inqtrix_logger.propagate = previous_propagate

    warnings = [
        record
        for record in caplog.records
        if "INQTRIX_PSEUDONYM_PEPPER" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert stable_pseudonyms_active() is False


def test_fallback_references_change_when_pepper_arrives():
    """The process-local tier and the instance tier are distinct keys."""
    user_id = uuid.uuid4()
    fallback = pseudonymous_log_reference("usr", user_id)
    configure_stable_pseudonyms("shared-instance-pepper")
    assert pseudonymous_log_reference("usr", user_id) != fallback
