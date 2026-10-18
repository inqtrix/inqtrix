"""Read existing originals under the current saved-session authority."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, replace

from inqtrix.agents.context_archive import (
    ARCHIVE_READ_WINDOW_CHARS, TRANSCRIPT_PAYLOAD_KEY, ArchiveVersionConflict,
    ArchiveWindow, is_private_archive, part_info, text_hash, window_bounds,
)
from inqtrix.agents.context_sources import (
    SOURCE_MAX_MATCHES, SOURCE_PAGE_SIZE, SOURCE_SCAN_CHARS, SOURCE_UNSCANNED_LISTED,
    ContextReadInput, ContextSearchInput, ContextSearchOutput, ContextSource,
    ContextWindow, decode_source, encode_source, has_external_input, merge_materials, merge_source_receipts,
)
from inqtrix.agents.control_ports import (
    ArtifactNotFound, ApprovalNotFound, ClarificationNotFound, public_decision_payload,
)
from inqtrix.capabilities import CapabilityError
from inqtrix.project.agent_sessions_ports import AgentSessionNotFound
from inqtrix.exceptions import RunNotFound
from inqtrix.services.report_requirement_resolver import ReportRequirementError, resolve_report_rules
from inqtrix.services.workspace_guard import deny_cross_workspace
from inqtrix.urls import scrub_credential_urls


def _unavailable() -> CapabilityError:
    return CapabilityError("context.not_found", "Source is unavailable in this conversation.", http_status=404)


SOURCE_TURN_LABEL_CHARS = 80


def _turn_label(run) -> str:
    """The assignment a run-bound source belongs to, as a short excerpt.

    Seven sources titled "Kernel transcript" or "Antwort" give a reader no
    way to pick the turn it means; the run's own question does. Existing
    metadata only, shown under the same credential scrub as every title.
    """
    question = " ".join(str(run.get("question") or "").split())
    if len(question) > SOURCE_TURN_LABEL_CHARS:
        return question[:SOURCE_TURN_LABEL_CHARS] + "…"
    return question


def _with_turn(title: str, label: str) -> str:
    return f"{title} · {label}" if label else title


def _material_unavailable() -> CapabilityError:
    return CapabilityError(
        "context.material_unavailable",
        "A document this source depends on is no longer available to you.",
        http_status=404,
    )


class AgentSourceService:
    """One read-only facade; identity and conversation never come from model input."""

    def __init__(self, *, sessions, run_store, control, knowledge=None, skills=None, editor=None,
                 prompt_templates=None):
        self.sessions, self.runs, self.control = sessions, run_store, control
        self.knowledge, self.skills, self.editor = knowledge, skills, editor
        self.prompt_templates = prompt_templates

    async def archive_catalogue(self, context):
        """The existing run-archive catalogue with the same source authorization."""
        await self._scope(context)
        rows = await self.control.list_source_artifacts([context.run_id])
        for row in rows:
            if row.kind == "context_archive":
                await self._archive_scope(row, rows, context)
        entries = await self.control.list_context_archive_sources(context.run_id, include_private=True)
        await self._scope(context)
        return [replace(entry, transcript=None) for entry in entries]

    async def read_archive(self, artifact_id, context, *, offset=None, content_hash="", max_chars=ARCHIVE_READ_WINDOW_CHARS):
        """Run-local read_canvas compatibility over the one authorized reader."""
        await self._scope(context)
        rows = await self.control.list_source_artifacts([context.run_id])
        row = next((item for item in rows if item.artifact_id == artifact_id and item.kind == "context_archive"), None)
        if row is None:
            raise _unavailable()
        info = part_info(row.artifact_id, row.payload)
        source_id = info.source_id if info else row.artifact_id
        version = info.content_hash if info else "legacy:" + str(row.revision)
        position = (info.start if info else 0) if offset is None else offset
        fingerprint = (info.content_hash if info else "") if offset is None else content_hash
        window = await self.read(ContextReadInput(
            source_id=encode_source("transcript" if is_private_archive(row.payload) else "archive",
                                    context.run_id, source_id, version),
            offset=position, content_hash=fingerprint, max_chars=max_chars,
        ), context)
        return (ArchiveWindow(source_id, window.source.title, window.content_hash, window.text,
                              window.offset, window.end, window.total_chars,
                              window.next_read["offset"] if window.next_read else None, window.representation),
                window._authorization_receipt)

    async def assert_scope(self, context):
        """Recheck the executing owner's private conversation boundary."""
        await self._scope(context)

    async def validate_receipt(self, context, receipt):
        """Recheck prior transfers before a provider call or checkpoint resume.

        Restricted inputs were admitted in this run only. Their marker remains
        on archived transcripts and prohibits retrieval from a different run.
        """
        await self._scope(context)
        await self._authorize_dependencies({"schema": 1, **receipt}, context, current_origin=True)
        await self._scope(context)

    async def _scope(self, context):
        if context.authority_check is not None:
            context.authority_check()
        if not context.run_id or (context.principal is not None and context.principal.user_id is not None and context.visible_to is None):
            raise _unavailable()
        try:
            current = self.runs.get(context.run_id, visible_to=context.visible_to, workspace_id=context.workspace_id)
            if (current.get("access") or {}).get("mode") not in {"owner", "unscoped"}:
                raise _unavailable()
            session_id = current.get("session_id") or ""
            if session_id:
                session = await self.sessions.get_session(session_id, visible_to=context.visible_to)
                deny_cross_workspace(resource_workspace_id=session.workspace_id,
                                     request_workspace_id=context.workspace_id,
                                     not_found=lambda: AgentSessionNotFound(session_id))
                if session.lifecycle_status != "active":
                    raise _unavailable()
            return current, session_id
        except (RunNotFound, AgentSessionNotFound) as exc:
            raise _unavailable() from exc

    async def _origin(self, context, origin_id):
        """The run a source id names, under the reader's current authority.

        An internal role run (a review, a section writer) is no source of
        the conversation, by id exactly as in the catalogue: it holds only
        material another run already read, and its handover carries that
        run's requirement with every rule body while it binds no rules of
        its own. Only the role run itself reads its own archives, which lie
        inside its executing boundary.
        """
        current, session_id = await self._scope(context)
        try:
            origin = self.runs.get(origin_id, visible_to=context.visible_to, workspace_id=context.workspace_id)
        except RunNotFound as exc:
            raise _unavailable() from exc
        if (origin.get("access") or {}).get("mode") not in {"owner", "unscoped"}:
            raise _unavailable()
        if origin_id != current["run_id"] and (
            not session_id or origin.get("session_id") != session_id or origin.get("work_profile")
        ):
            raise _unavailable()
        return origin

    async def _authorize_dependencies(self, descriptor, context, *, current_origin=False):
        if not isinstance(descriptor, dict) or descriptor.get("schema") != 1:
            raise _unavailable()
        if descriptor.get("restricted_inputs") and not current_origin:
            # File/editor originals require their own current release/deletion
            # adapters. Missing adapters never turn into an implicit grant.
            raise _unavailable()
        ids = set(descriptor.get("collection_ids") or ())
        boundary = context.knowledge_collection_ids
        if ids and (boundary is None or not ids.issubset(boundary) or self.knowledge is None):
            raise _unavailable()
        if ids:
            try:
                await self.knowledge.assert_collections_visible(sorted(ids), visible_to=context.visible_to)
            except Exception as exc:
                raise _unavailable() from exc
        for skill_id, revision in (descriptor.get("skill_revisions") or {}).items():
            if self.skills is None:
                raise _unavailable()
            try:
                skill, _ = await self.skills.get_visible(
                    skill_id, tenant_id=context.principal.tenant_id if context.principal else "default",
                    visible_to=context.visible_to,
                )
            except Exception as exc:
                raise _unavailable() from exc
            if skill.revision != revision:
                raise _unavailable()
        await self._authorize_rules(descriptor.get("rule_revisions") or {}, context, current_origin=current_origin)
        for field, owner_only in (("origin_run_ids", False), ("owner_origin_run_ids", True)):
            if not self.runs.source_origins_visible(
                list(descriptor.get(field) or ()), visible_to=context.visible_to, owner_only=owner_only,
            ):
                raise _unavailable()
        await self._authorize_materials(merge_materials([descriptor.get("materials")]), context)
        return merge_source_receipts([descriptor])

    async def _authorize_rules(self, rule_revisions, context, *, current_origin):
        """Re-check every bound rule against the reader's CURRENT catalog.

        A transcript holds the full text of the rules its run worked under.
        A deleted, edited or no longer shared rule therefore closes it —
        through the same catalog resolution the submit and the plan gate
        use, and a missing catalog is never an implicit grant. Rules are
        frozen per run, so a run's own parts are exempt when it reads its
        own archives: editing a template mid-run changes nothing the run
        works under. Every rule taken over from another run stays bound.
        """
        bound = {str(template_id): int(revision) for template_id, revision in rule_revisions.items()}
        if bound and current_origin:
            body = self.runs.source_request_body(context.run_id, visible_to=context.visible_to,
                                                 workspace_id=context.workspace_id)
            own = {(str(part.get("template_id") or ""), int(part.get("revision") or 0))
                   for part in body.get("report_rules") or () if isinstance(part, dict)}
            bound = {template_id: revision for template_id, revision in bound.items()
                     if (template_id, revision) not in own}
        if not bound:
            return
        try:
            resolved = await resolve_report_rules(sorted(bound), prompt_templates=self.prompt_templates,
                                                  visible_to=context.visible_to)
        except ReportRequirementError as exc:
            raise _unavailable() from exc
        current = {rule.template_id: rule.revision for rule in resolved}
        if any(current.get(template_id) != revision for template_id, revision in bound.items()):
            raise _unavailable()

    async def _authorize_materials(self, materials, context):
        """Re-check every document a transcript transferred against the reader's CURRENT rights.

        A deleted or hidden document, a revoked editor share or a deleted
        transferred comment closes the transcript (`material_unavailable`);
        a changed document version does not — the archived text stays a
        historical snapshot while its material exists. A missing service is
        never an implicit grant.
        """
        if materials["knowledge_documents"]:
            if self.knowledge is None:
                raise _material_unavailable()
            for row in materials["knowledge_documents"]:
                try:
                    await self.knowledge.get_document(row["document_id"], visible_to=context.visible_to)
                except Exception as exc:
                    raise _material_unavailable() from exc
        if materials["editor_documents"]:
            if self.editor is None:
                raise _material_unavailable()
            for row in materials["editor_documents"]:
                try:
                    await self.editor.assert_comments_visible(
                        row["document_id"], list(row["comment_ids"]), visible_to=context.visible_to,
                    )
                except Exception as exc:
                    raise _material_unavailable() from exc

    async def _archive_scope(self, row, all_rows, context):
        descriptor = row.payload.get(TRANSCRIPT_PAYLOAD_KEY)
        if descriptor is None and row.run_id == context.run_id:
            # Current-run text already lies inside the executing run boundary.
            body = self.runs.source_request_body(row.run_id, visible_to=context.visible_to,
                                                 workspace_id=context.workspace_id)
            descriptor = {
                "schema": 1,
                "collection_ids": list((body.get("knowledge_filters") or {}).get("collection_ids") or ()),
                "skill_revisions": body.get("skill_revisions") or {},
                "origin_run_ids": [],
                "restricted_inputs": bool(has_external_input(body.get("messages"))),
            }
        if descriptor is None:
            candidates = [item.payload[TRANSCRIPT_PAYLOAD_KEY] for item in all_rows
                          if item.run_id == row.run_id and is_private_archive(item.payload)]
            if not candidates:
                raise _unavailable()
            descriptor = {"schema": 1, **merge_source_receipts(candidates)}
        return await self._authorize_dependencies(descriptor, context, current_origin=row.run_id == context.run_id)

    async def _catalogue(self, context):
        current, session_id = await self._scope(context)
        runs = self.runs.source_run_catalogue(
            session_id, visible_to=context.visible_to, workspace_id=context.workspace_id,
        ) if session_id else [current]
        by_id = {row["run_id"]: row for row in runs}
        rows = await self.control.list_source_artifacts(list(by_id))
        decisions = await self.control.list_source_inputs(list(by_id))
        entries = []
        def add(kind, run_id, local_id, version, title, *, available=True, reason=""):
            entries.append(ContextSource(
                source_id=encode_source(kind, run_id, local_id, version), kind=kind,
                origin_run_id=run_id, version=version, title=title,
                available=available, unavailable_reason=reason,
            ))
        answer_runs = {row.run_id for row in rows if row.kind == "answer" and row.status == "ready"}
        transcript_runs = {row.run_id for row in rows if is_private_archive(row.payload)
                           and row.payload[TRANSCRIPT_PAYLOAD_KEY].get("format") == "checkpoint_json"}
        for run_id, run in by_id.items():
            if run_id != context.run_id and run.get("kind") != "agent_child":
                question = str(run.get("question") or "")
                title = question[:100] + ("…" if len(question) > 100 else "")
                add("question", run_id, "", "request", title or "User question")
            if run.get("status") == "completed" and run_id not in answer_runs:
                add("result", run_id, "", "result", _with_turn("Published answer", _turn_label(run)))
            if (run_id != context.run_id and run.get("mode") == "agent_kernel"
                    and run.get("status") in {"completed", "failed", "cancelled"} and run_id not in transcript_runs):
                add("transcript", run_id, "", "", _with_turn("Kernel transcript", _turn_label(run)),
                    available=False, reason="not_recorded")
        for row in sorted(rows, key=lambda x: (x.created_at, x.artifact_id)):
            label = _turn_label(by_id.get(row.run_id) or {})
            if row.kind in {"answer", "memo", "deliverable"} and row.status == "ready":
                add("artifact", row.run_id, row.artifact_id, str(row.revision), _with_turn(row.title, label))
            elif row.kind == "context_archive" and row.status == "ready":
                info = part_info(row.artifact_id, row.payload)
                if info is not None and info.index:
                    continue
                kind = "transcript" if is_private_archive(row.payload) else "archive"
                version = info.content_hash if info else "legacy:" + str(row.revision)
                try:
                    await self._archive_scope(row, rows, context)
                    available, reason = True, ""
                except CapabilityError as exc:
                    available = False
                    reason = "material_unavailable" if exc.code == "context.material_unavailable" else "origin_scope_unavailable"
                add(kind, row.run_id, row.artifact_id, version,
                    _with_turn("Kernel transcript" if kind == "transcript" else (info.title if info else row.title), label),
                    available=available, reason=reason)
        for record in sorted(decisions, key=lambda item: (item["created_at"], item["kind"], item["id"])):
            add(record["kind"], record["run_id"], record["id"], "decision",
                _with_turn("User " + record["kind"], _turn_label(by_id.get(record["run_id"]) or {})))
        await self._scope(context)
        return entries

    async def read(self, payload: ContextReadInput, context) -> ContextWindow:
        try:
            kind, origin_id, local_id, version = decode_source(payload.source_id)
            origin = await self._origin(context, origin_id)
            receipt = {}
            representation = "text"
            if kind == "question":
                if local_id or version != "request" or origin.get("kind") == "agent_child":
                    raise _unavailable()
                text = origin.get("question_full")
                if text is None and not origin.get("question_clipped"):
                    text = origin.get("question")
                if not isinstance(text, str):
                    raise _unavailable()
                title = "User question"
            elif kind == "result":
                if local_id or version != "result":
                    raise _unavailable()
                result = self.runs.result(origin_id, visible_to=context.visible_to, workspace_id=context.workspace_id)
                text, title = result.get("answer"), "Published answer"
                if not isinstance(text, str):
                    raise _unavailable()
            elif kind in {"archive", "transcript", "artifact"}:
                rows = await self.control.list_source_artifacts([origin_id])
                row = next((row for row in rows if row.artifact_id == local_id), None)
                if row is None:
                    raise _unavailable()
                title = row.title
                if kind == "artifact":
                    if row.kind not in {"answer", "memo", "deliverable"} or is_private_archive(row.payload):
                        raise _unavailable()
                    row, _ = await self.control.get_artifact(origin_id, local_id)
                    if row.status != "ready" or str(row.revision) != version:
                        raise ArchiveVersionConflict("Artifact changed; discover the source again.")
                    text = row.content_markdown
                else:
                    if row.kind != "context_archive" or (kind == "transcript") != is_private_archive(row.payload):
                        raise _unavailable()
                    info = part_info(row.artifact_id, row.payload)
                    if info is not None and info.index:
                        raise _unavailable()
                    actual_version = info.content_hash if info else "legacy:" + str(row.revision)
                    if actual_version != version:
                        raise ArchiveVersionConflict("Archive changed; discover the source again.")
                    receipt = await self._archive_scope(row, rows, context)
                    window = await self.control.read_context_archive(
                        origin_id, local_id, offset=payload.offset, content_hash=payload.content_hash,
                        max_chars=payload.max_chars, include_private=True,
                    )
                    await self._archive_scope(row, rows, context)
                    await self._origin(context, origin_id)
                    return self._window(payload, kind, origin_id, version, window.title, window.text,
                                        window.offset, window.end, window.total_chars, window.content_hash,
                                        row.payload[TRANSCRIPT_PAYLOAD_KEY].get("format", "checkpoint_json")
                                        if kind == "transcript" else window.fidelity, receipt)
            elif kind in {"clarification", "approval"}:
                if version != "decision":
                    raise _unavailable()
                if kind == "clarification":
                    record = await self.control.get_clarification(origin_id, local_id)
                    if record.status != "answered":
                        raise _unavailable()
                    data = {key: value for key, value in asdict(record).items()
                            if key in {"question", "questions", "options", "answer", "answers", "option_id"}}
                else:
                    record = await self.control.get_approval(origin_id, local_id)
                    if record.status == "pending":
                        raise _unavailable()
                    data = {"decision": record.decision, "note": record.note,
                            "decision_payload": public_decision_payload(record)}
                text, title, representation = json.dumps(data, ensure_ascii=False, sort_keys=True), "User " + kind, "decision_json"
            else:
                raise _unavailable()
            # Preserve the original; do not silently change its coordinates or
            # bypass the intake's credential-URL protection through retrieval.
            if scrub_credential_urls(text) != text:
                raise CapabilityError("context.protected_content", "This source needs a credential-safe projection.")
            digest = text_hash(text)
            if payload.offset and not payload.content_hash:
                raise ValueError("A continuation requires the source fingerprint.")
            start, end = window_bounds(len(text), payload.offset, payload.max_chars, payload.content_hash, digest)
            await self._origin(context, origin_id)
            return self._window(payload, kind, origin_id, version, title, text[start:end], start, end,
                                len(text), digest, representation, receipt)
        except (ArtifactNotFound, ApprovalNotFound, ClarificationNotFound, RunNotFound, AgentSessionNotFound) as exc:
            raise _unavailable() from exc
        except ArchiveVersionConflict as exc:
            raise CapabilityError("context.version_changed", str(exc), http_status=409) from exc
        except ValueError as exc:
            raise CapabilityError("context.invalid_read", str(exc)) from exc

    @staticmethod
    def _window(payload, kind, origin_id, version, title, text, start, end, total, digest, representation, receipt):
        result = ContextWindow(
            source=ContextSource(source_id=payload.source_id, kind=kind, origin_run_id=origin_id, version=version, title=title),
            text=text, offset=start, end=end, total_chars=total, content_hash=digest,
            representation=representation, is_full_source=start == 0 and end == total,
            next_read={"source_id": payload.source_id, "offset": end, "content_hash": digest,
                       "max_chars": payload.max_chars} if end < total else None,
        )
        result._authorization_receipt = merge_source_receipts([receipt, {"owner_origin_run_ids": [origin_id]}])
        return result

    async def search(self, payload: ContextSearchInput, context) -> ContextSearchOutput:
        entries = await self._catalogue(context)
        if payload.source_id:
            entries = [entry for entry in entries if entry.source_id == payload.source_id]
            if not entries:
                raise _unavailable()
        if payload.query:
            # Conversation originals precede copies inside execution history.
            # A large early transcript must not consume every first page before
            # later published answers can be reached. Stable sorting preserves
            # catalogue order within each kind and retains every source.
            priority = {"question": 0, "clarification": 0, "approval": 0,
                        "artifact": 1, "result": 2, "archive": 3, "transcript": 4}
            entries.sort(key=lambda entry: priority[entry.kind])
        digest = text_hash(json.dumps([
            context.run_id, payload.query, payload.source_id, payload.case_sensitive,
            [entry.model_dump() for entry in entries],
        ], sort_keys=True, ensure_ascii=False))
        index, offset, fingerprint = 0, 0, ""
        if payload.cursor:
            try:
                if len(payload.cursor) > 4096:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(payload.cursor.encode("ascii")))
                if (not isinstance(value, list) or len(value) != 4 or value[0] != digest
                        or type(value[1]) is not int or not 0 <= value[1] <= len(entries)
                        or type(value[2]) is not int or value[2] < 0 or not isinstance(value[3], str)):
                    raise ValueError()
                _, index, offset, fingerprint = value
            except (ValueError, UnicodeError) as exc:
                raise CapabilityError("context.cursor_changed", "Source set changed or cursor is invalid; restart the search.") from exc
        if not payload.query:
            selected = entries[index:index + SOURCE_PAGE_SIZE]
            end = index + len(selected)
            cursor = self._cursor(digest, end, 0, "") if end < len(entries) else None
            unavailable = [entry for entry in selected if not entry.available]
            result = ContextSearchOutput(sources=selected, unavailable_sources=unavailable, query="",
                                         scan_complete=cursor is None, corpus_complete=False,
                                         next_cursor=cursor,
                                         next_search=payload.model_copy(update={"cursor": cursor}) if cursor else None)
            result._authorization_receipt = {"owner_origin_run_ids": sorted({entry.origin_run_id for entry in selected})}
            return result
        pattern = re.compile(re.escape(payload.query), 0 if payload.case_sensitive else re.IGNORECASE)
        matches, scanned, unavailable, receipts = [], [], [], []
        remaining = SOURCE_SCAN_CHARS
        while index < len(entries) and remaining > 0 and len(matches) < SOURCE_MAX_MATCHES:
            entry = entries[index]
            if not entry.available:
                unavailable.append(entry)
                index, offset, fingerprint = index + 1, 0, ""
                continue
            lookback = min(offset, len(payload.query) - 1)
            start = offset - lookback
            try:
                window = await self.read(ContextReadInput(
                    source_id=entry.source_id, offset=start, content_hash=fingerprint,
                    max_chars=min(ARCHIVE_READ_WINDOW_CHARS, remaining + lookback),
                ), context)
            except CapabilityError as exc:
                if exc.code == "context.version_changed":
                    raise
                if exc.code not in {"context.not_found", "context.protected_content", "context.material_unavailable"}:
                    raise
                unavailable.append(entry.model_copy(update={"available": False, "unavailable_reason": exc.code}))
                index, offset, fingerprint = index + 1, 0, ""
                continue
            receipts.append(window._authorization_receipt)
            before = offset
            search_at = 0
            stop = window.end
            while found := pattern.search(window.text, search_at):
                search_at = found.start() + 1
                hit_start, hit_end = start + found.start(), start + found.end()
                if hit_end <= before:
                    continue
                left, right = max(0, found.start() - 120), min(len(window.text), found.end() + 120)
                matches.append({
                    "source": entry.model_dump(), "start": hit_start, "end": hit_end,
                    "excerpt": window.text[left:right], "excerpt_start": start + left,
                    "content_hash": window.content_hash,
                    "read": {"source_id": entry.source_id, "offset": start + left,
                             "content_hash": window.content_hash, "max_chars": max(1, right - left)},
                })
                if len(matches) >= SOURCE_MAX_MATCHES:
                    stop = hit_end
                    break
            scanned.append({"source_id": entry.source_id, "version": entry.version, "start": before,
                            "end": stop, "total_chars": window.total_chars, "content_hash": window.content_hash})
            remaining -= max(0, stop - before)
            offset, fingerprint = stop, window.content_hash
            if stop >= window.total_chars:
                index, offset, fingerprint = index + 1, 0, ""
            elif stop <= before:
                raise CapabilityError("context.scan_stalled", "The source scan made no progress.")
        complete = index >= len(entries)
        cursor = None if complete else self._cursor(digest, index, offset, fingerprint)
        # A limited result names where the remainder lives: the sources this
        # scan has not reached, in scan order, so the next call can address one
        # by source_id instead of paging through everything before it.
        unscanned = [entry for entry in entries[index + (1 if offset else 0):] if entry.available]
        result = ContextSearchOutput(
            sources=[], matches=matches, scanned_ranges=scanned, unavailable_sources=unavailable,
            unscanned_sources=unscanned[:SOURCE_UNSCANNED_LISTED], unscanned_total=len(unscanned),
            query=payload.query, scan_complete=complete,
            corpus_complete=complete and not payload.cursor and not unavailable,
            next_cursor=cursor,
            next_search=payload.model_copy(update={"cursor": cursor}) if cursor else None,
        )
        result._authorization_receipt = merge_source_receipts(receipts)
        await self.validate_receipt(context, result._authorization_receipt)
        await self._scope(context)
        return result

    @staticmethod
    def _cursor(digest, index, offset, fingerprint):
        return base64.urlsafe_b64encode(json.dumps([digest, index, offset, fingerprint]).encode()).decode()
