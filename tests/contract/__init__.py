"""Contract and snapshot tests that freeze the externally observable API.

These tests are the refactoring safety net introduced before the
structural rebuild (router split, service layer, algorithm registry).
They lock the wire shapes that HTTP clients — most importantly the
React research-desk app and OpenAI-compatible SDKs — depend on:

* ``/health`` and ``/v1/models`` payload contracts.
* ``/v1/chat/completions`` non-streaming payload and streaming SSE
  chunk sequence.
* ``/v1/runs*`` lifecycle summaries, error envelopes, and the native
  run SSE event sequence.
* Public Python surface stability (``AgentConfig``, ``ProviderContext``,
  ``create_app`` signature).

Every later refactor phase must keep this suite green without edits;
an intentional, additive contract change updates the affected test in
the same commit and says so in the commit message.
"""
