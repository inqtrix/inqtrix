"""Replay-test submodule.

Tests in this package use VCR cassettes (httpx + urllib providers) or
the botocore Stubber (Bedrock) to replay realistic backend payloads
fully offline. See ``docs/development/testing-strategy.md`` for the
full pyramid layout and recording workflow.
"""
