"""Preserve actual checkpoint messages before execution-cache cleanup."""

from __future__ import annotations

import json
import logging

from inqtrix.agents.context_archive import text_hash
from inqtrix.agents.kernel.deps import run_coro
from inqtrix.exceptions import AgentContextArchiveError

from inqtrix.agents.context_sources import (
    MATERIAL_DEPENDENCIES, SOURCE_RECEIPT, merge_materials, merge_source_receipts,
)

log = logging.getLogger("inqtrix")

MATERIAL_TOOLS = frozenset({
    "read_project_document", "search_project_knowledge", "cite_document_passages",
    "read_editor_document", "search_editor_document", "propose_editor_patch",
})
"""The only tools whose checkpointed artifacts may enter material
dependencies into a transcript descriptor (the receipt template: a relayed
text, e.g. a web answer quoting an id, never becomes a dependency)."""


def transcript_descriptor(deps, encoded, body, representation, checkpoint_id=""):
    """The dependencies a later run must still hold to read this transcript.

    Rules bind like skills: the transcript holds the full text of every
    rule the run worked under (its own frozen parts) and of every rule a
    transcript it read was bound to, so each is named by revision.
    ``rule_revisions`` is written only when a rule is named — a run
    without rules keeps the descriptor it always had.
    """
    collections = set(getattr(deps.capability_context, "knowledge_collection_ids", None) or ())
    skills = {record.id: record.revision for record in deps.skills}
    rules = {
        str(part["template_id"]): int(part["revision"]) for part in deps.report_rules
    }
    origins = set()
    owner_origins = set()
    receipts = [deps.source_receipt()]
    # Only raw file/image inputs stay behind the conservative lock until
    # their intake path is bound. Documents a run actually read are
    # named below as materials and re-authorized on every later access.
    restricted = deps.context_has_external_inputs
    materials = []
    for message in encoded:
        artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
        receipt = artifact.get(SOURCE_RECEIPT)
        if message.get("type") == "tool" and message.get("name") in {"read_context", "search_context", "read_canvas"} and isinstance(receipt, dict):
            receipts.append(receipt)
        if message.get("type") == "tool" and message.get("name") in MATERIAL_TOOLS:
            materials.append(artifact.get(MATERIAL_DEPENDENCIES))
    combined = merge_source_receipts(receipts)
    collections.update(combined["collection_ids"])
    origins.update(combined["origin_run_ids"])
    owner_origins.update(combined["owner_origin_run_ids"])
    skills.update(combined["skill_revisions"])
    rules.update(combined.get("rule_revisions") or {})
    restricted |= combined["restricted_inputs"]
    merged_materials = merge_materials([*materials, combined["materials"]])
    # A knowledge window or hit binds its collection like an explicit release.
    collections.update(row["collection_id"] for row in merged_materials["knowledge_documents"] if row["collection_id"])
    for reference in deps.evidence_refs.values():
        origins.update(reference.get("source_run_ids") or ())
        if reference.get("source_run_id"):
            origins.add(reference["source_run_id"])
    origins.discard(deps.run_id)
    owner_origins.discard(deps.run_id)
    return {
        "schema": 1, "checkpoint_id": checkpoint_id or text_hash(body),
        "format": representation, "collection_ids": sorted(collections),
        "skill_revisions": skills, **({"rule_revisions": rules} if rules else {}),
        "origin_run_ids": sorted(origins),
        "restricted_inputs": restricted, "owner_origin_run_ids": sorted(owner_origins),
        "materials": merged_materials,
    }


def archive_checkpoint(deps, state, request, *, required: bool = True) -> str:
    """Store a private, immutable snapshot; failed publication never permits deletion."""
    try:
        state = state() if callable(state) else state
        values = dict(getattr(state, "values", {}) or {})
        messages = values.get("messages") or []
        if not messages:
            return ""
        encoded = [
            message.model_dump(mode="json") if hasattr(message, "model_dump")
            else dict(message) if isinstance(message, dict)
            else (_ for _ in ()).throw(TypeError("Unsupported checkpoint message."))
            for message in messages
        ]
        checkpoint_id = str(((getattr(state, "config", None) or {}).get("configurable") or {}).get("checkpoint_id") or "")
        body = json.dumps({
            "schema": 1,
            "coverage": "checkpoint_messages",
            "messages": encoded,
            "todos": values.get("todos") or [],
            "context_projections": values.get("kernel_context_projections") or {},
            "usage": values.get("kernel_usage") or {},
        }, ensure_ascii=False, sort_keys=True)
        descriptor = transcript_descriptor(deps, encoded, body, "checkpoint_json", checkpoint_id)
        records = run_coro(deps.control.put_context_archive(
            run_id=deps.run_id, title="Kernel execution transcript",
            text=body, transcript=descriptor,
        ))
        if not records:
            raise AgentContextArchiveError("Transcript archive returned no readable source.")
        return records[0].artifact_id
    except Exception as exc:
        if required:
            raise AgentContextArchiveError(
                "The execution transcript could not be preserved; its checkpoint was retained."
            ) from exc
        log.warning(
            "The execution transcript could not be archived; its checkpoint was retained (error_type=%s).",
            type(exc).__name__,
        )
        return ""
