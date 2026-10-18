"""Trace content policy: its fallbacks are visible, never silent."""

from __future__ import annotations

import json
import logging

from inqtrix.observability import content


def _warnings(caplog, text: str) -> list[logging.LogRecord]:
    # One record per emitted WARNING: the handler sits on the inqtrix logger
    # and may also see the record again through propagation.
    unique = {
        id(record): record
        for record in caplog.records
        if record.levelno == logging.WARNING and text in record.getMessage()
    }
    return list(unique.values())


def _capture_inqtrix(caplog) -> logging.Logger:
    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    return logger


def test_an_unpublished_policy_is_built_from_settings_and_says_so(monkeypatch, caplog):
    # Building the policy from the environment was silent: a process that
    # traced without publishing its composed policy captured content under
    # settings nobody chose for it, and nothing said so.
    built = content.ContentCapturePolicy(configured_capture=False, max_attr_bytes=1000)
    monkeypatch.setattr(content, "_active_policy", None)
    monkeypatch.setattr(content, "build_content_policy", lambda settings: built)
    monkeypatch.setattr("inqtrix.settings.Settings", lambda: object())
    logger = _capture_inqtrix(caplog)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            assert content.active_content_policy() is built
            assert content.active_content_policy() is built
    finally:
        logger.removeHandler(caplog.handler)
    assert len(_warnings(caplog, "No trace content policy was published")) == 1


def test_an_unserializable_payload_stays_valid_json_and_says_so(monkeypatch, caplog):
    # A payload JSON could not render fell back to its Python text without a
    # word: the span attribute was no JSON, and nothing said why.
    class Opaque:
        def __str__(self) -> str:
            raise ValueError("no text")

    monkeypatch.setattr(content, "_warned", set())
    policy = content.ContentCapturePolicy(configured_capture=True, max_attr_bytes=1000)
    logger = _capture_inqtrix(caplog)
    try:
        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            for _ in range(2):
                clipped = policy.clip_payload({"value": Opaque()})
                assert isinstance(json.loads(clipped.text), str)
    finally:
        logger.removeHandler(caplog.handler)
    assert len(_warnings(caplog, "could not be serialized as JSON")) == 1


def test_withheld_content_turns_capture_off_for_its_scope_only():
    # An incognito request asks that nothing it says reaches a trace: inside
    # its scope the policy captures nothing, whatever it was configured to.
    policy = content.ContentCapturePolicy(configured_capture=True, max_attr_bytes=1000)
    assert policy.capture_content
    with content.withheld_content():
        assert not policy.capture_content
        assert policy.configured_capture
    assert policy.capture_content


def test_withheld_content_reaches_every_thread_the_trace_context_reaches():
    # Chat turns and runs hand their work to pool threads. Each hop carries
    # the trace context, captured or as a copied context, and the withheld
    # scope must cross every hop with it.
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from inqtrix.observability.otel import attached_trace_context, capture_trace_context

    policy = content.ContentCapturePolicy(configured_capture=True, max_attr_bytes=1000)

    def captures() -> bool:
        return policy.capture_content

    def captures_within(captured) -> bool:
        with attached_trace_context(captured):
            return policy.capture_content

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(captures).result()
        with content.withheld_content():
            captured = capture_trace_context()
            copied = contextvars.copy_context()
        assert not pool.submit(captures_within, captured).result()
        assert not pool.submit(copied.run, captures).result()
        assert pool.submit(captures).result()
