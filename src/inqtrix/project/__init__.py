"""Project-persistence tier: server-persistent chat/editor/assets.

When the platform runs with a Postgres backend, the entities that
otherwise live only in the local markdown project become server-
persistent, scoped per ``(tenant_id, created_by_user_id, workspace_id)`` like
the other owned resources. Without Postgres the app stays local-first —
the tier is capability-gated, never forced.

This package holds the store ports and their memory/Postgres
implementations for chat history, editor documents and patches,
file-asset records, vector-index records, agent and knowledge sessions,
and account preferences. The relational schemas live in
``inqtrix.storage.*_orm``; the HTTP surface in
``inqtrix.server.routers``; orchestration in ``inqtrix.services``.
"""
