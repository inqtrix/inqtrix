"""Skill CRUD with live owner/direct-share authorization.

Reads need view, updates need edit, and deletion stays owner-only. No grant
map crosses a request, worker, or capability boundary.
Updates require an integer revision; timestamp-based last-write-wins is not
an authorization-safe editor contract.

The skill-specific validation lives here: the ``/``-label shape, the
enum fields, the clarification-point sanitizer (deterministic ids,
author input validated HARD — unlike LLM-proposed questions there is no
soft-drop path), and the placeholder coupling rule: every ``{{name}}``
in the instructions must be declared as a point (a silent hole in the
substitution would surface mid-run, plan `3.4`); points WITHOUT a
placeholder stay allowed (context the agent needs verbatim-free).
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Mapping

from inqtrix.auth.permissions import AccessMode, ResourceAccess, SharePermission
from inqtrix.content.skills import (
    MAX_CLARIFICATION_POINTS,
    MAX_POINT_OPTIONS,
    SKILL_ALLOWED_TOOLS,
    SKILL_DELIVERABLES,
    SKILL_INVOCATIONS,
    SKILL_REQUIRES_PLAN,
    SkillConflict,
    SkillNotFound,
    SkillRecord,
    SkillRepository,
    new_skill_id,
)

__all__ = [
    "SkillService",
    "SkillValidationError",
    "SkillConflict",
    "extract_placeholders",
]

if TYPE_CHECKING:
    from inqtrix.auth.shares import ShareFacts
    from inqtrix.auth.permissions import AuthorizationService
    from inqtrix.auth.principal import UserContext
    from inqtrix.user_events import ResourceInvalidator


class SkillValidationError(ValueError):
    """Raised for client-payload problems (maps to HTTP 400)."""


_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_-]+)\s*\}\}")

_EFFORT_TOKENS = ("", "none", "minimal", "low", "medium", "high", "xhigh")
_TIER_TOKENS = ("", "high", "mid", "fast")

# Author-facing text limits. They are REFUSED, not truncated: silently
# storing a shortened field hands the author a 200 back for text the
# product then throws away, and the full version is gone -- there is no
# place left to show it. `_sanitized_points` already refuses the point
# and option COUNTS this loud way; a length is not a different kind of
# limit.
MAX_POINT_QUESTION_CHARS = 500
MAX_POINT_ASSUMPTION_CHARS = 300
MAX_OPTION_LABEL_CHARS = 60
MAX_OPTION_DESCRIPTION_CHARS = 120
MAX_ARGUMENT_HINT_CHARS = 120


def _length_checked(
    value: str, *, limit: int, field: str, where: str = ""
) -> str:
    """Return ``value`` unchanged, or refuse it -- never a shortened version."""
    if len(value) > limit:
        prefix = f"{where}: " if where else ""
        raise SkillValidationError(
            f"{prefix}'{field}' must be at most {limit} characters "
            f"(got {len(value)})"
        )
    return value


def extract_placeholders(instructions_markdown: str) -> list[str]:
    """The ordered, de-duplicated ``{{name}}`` tokens of a skill body.

    Shared by the save validation here, the library editor (point
    scaffolding), and the runtime substitution — one definition of what
    counts as a placeholder.
    """
    seen: list[str] = []
    for match in _PLACEHOLDER_PATTERN.finditer(instructions_markdown):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _sanitized_points(raw: Any) -> tuple[dict[str, Any], ...]:
    """Author-declared clarification points with deterministic ids.

    Positional ids (``p1``, ``p1_o1``) follow the clarification sanitizer
    convention so the runtime can map answers without trusting any
    client-minted id. Author input fails HARD (400) — there is no
    LLM-garbage soft path here.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SkillValidationError(
            "Field 'clarification_points' must be a list"
        )
    if len(raw) > MAX_CLARIFICATION_POINTS:
        raise SkillValidationError(
            f'At most {MAX_CLARIFICATION_POINTS} clarification points are allowed'
        )
    points: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SkillValidationError(
                f'Clarification point {index} must be an object'
            )
        question = str(item.get("question", "") or "").strip()
        if not question:
            raise SkillValidationError(
                f'Clarification point {index} requires a question'
            )
        name = str(item.get("name", "") or "").strip()
        raw_options = item.get("options") or []
        if not isinstance(raw_options, list):
            raise SkillValidationError(
                f"Clarification point {index}: 'options' must be a list"
            )
        if len(raw_options) > MAX_POINT_OPTIONS:
            raise SkillValidationError(
                f'Clarification point {index}: at most {MAX_POINT_OPTIONS} options are allowed'
            )
        options: list[dict[str, str]] = []
        for opt_index, option in enumerate(raw_options, start=1):
            if not isinstance(option, dict):
                raise SkillValidationError(
                    f'Clarification point {index}: option {opt_index} must be an object'
                )
            label = str(option.get("label", "") or "").strip()
            if not label:
                raise SkillValidationError(
                    f'Clarification point {index}: option {opt_index} requires a label'
                )
            where = f'Clarification point {index}: option {opt_index}'
            options.append(
                {
                    "id": f"p{index}_o{opt_index}",
                    "label": _length_checked(
                        label,
                        limit=MAX_OPTION_LABEL_CHARS,
                        field="label",
                        where=where,
                    ),
                    "description": _length_checked(
                        str(option.get("description", "") or "").strip(),
                        limit=MAX_OPTION_DESCRIPTION_CHARS,
                        field="description",
                        where=where,
                    ),
                }
            )
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise SkillValidationError(
                f"Clarification point {index}: 'required' must be a boolean"
            )
        point_where = f"Clarification point {index}"
        points.append(
            {
                "id": f"p{index}",
                "name": name,
                "question": _length_checked(
                    question,
                    limit=MAX_POINT_QUESTION_CHARS,
                    field="question",
                    where=point_where,
                ),
                "options": options,
                "required": required,
                "default_assumption": _length_checked(
                    str(item.get("default_assumption", "") or "").strip(),
                    limit=MAX_POINT_ASSUMPTION_CHARS,
                    field="default_assumption",
                    where=point_where,
                ),
            }
        )
    return tuple(points)


def _validated_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the writable skill fields."""
    label = str(payload.get("label", "") or "").strip().lower()
    if not _LABEL_PATTERN.match(label):
        raise SkillValidationError(
            ("Field 'label' must be a / shortcut using a-z, 0-9 and '-' (1-64 "
            "characters, starting with a letter or digit)")
        )
    title = str(payload.get("title", "") or "").strip()
    if not title:
        raise SkillValidationError("Field 'title' is required")
    instructions = str(payload.get("instructions_markdown", "") or "")
    if not instructions.strip():
        raise SkillValidationError(
            "Field 'instructions_markdown' is required"
        )
    for enum_field, allowed in (
        ("deliverable", SKILL_DELIVERABLES),
        ("requires_plan", SKILL_REQUIRES_PLAN),
        ("invocation", SKILL_INVOCATIONS),
        ("model_tier", _TIER_TOKENS),
        ("effort", _EFFORT_TOKENS),
    ):
        value = payload.get(enum_field)
        if value is not None and str(value) not in allowed:
            raise SkillValidationError(
                f"Field '{enum_field}' must be one of {', '.join((repr(item) for item in allowed))}"
            )
    raw_tools = payload.get("allowed_tools") or []
    if not isinstance(raw_tools, list) or any(
        not isinstance(tool, str) or not tool.strip() for tool in raw_tools
    ):
        raise SkillValidationError(
            "Field 'allowed_tools' must be a list of non-empty strings"
        )
    unknown_tools = [
        tool for tool in raw_tools if tool not in SKILL_ALLOWED_TOOLS
    ]
    if unknown_tools:
        raise SkillValidationError(
            "Field 'allowed_tools' contains unknown tools: "
            + ", ".join(repr(tool) for tool in unknown_tools)
            + " (allowed: "
            + ", ".join(SKILL_ALLOWED_TOOLS)
            + ")"
        )
    include = payload.get("include_in_autocomplete", True)
    if not isinstance(include, bool):
        raise SkillValidationError(
            "Field 'include_in_autocomplete' must be a boolean"
        )
    points = _sanitized_points(payload.get("clarification_points"))
    # Placeholder coupling (plan `3.4`): every {{name}} must map onto a
    # declared point, or the runtime substitution would leave a visible
    # hole mid-run. Points without a placeholder stay allowed.
    placeholders = extract_placeholders(instructions)
    point_names = {point["name"] for point in points if point["name"]}
    missing = [name for name in placeholders if name not in point_names]
    if missing:
        raise SkillValidationError(
            f"""Each {{{{placeholder}}}} requires a clarification point with the same name; missing points: {', '.join(missing)}"""
        )
    return {
        "label": label,
        "title": title,
        "description": str(payload.get("description", "") or "").strip(),
        "when_to_use": str(payload.get("when_to_use", "") or "").strip(),
        "instructions_markdown": instructions,
        "clarification_points": points,
        "deliverable": str(payload.get("deliverable", "") or ""),
        "allowed_tools": tuple(
            tool.strip() for tool in raw_tools
        ),
        "requires_plan": str(payload.get("requires_plan", "auto") or "auto"),
        "invocation": str(
            payload.get("invocation", "user_only") or "user_only"
        ),
        "argument_hint": _length_checked(
            str(payload.get("argument_hint", "") or "").strip(),
            limit=MAX_ARGUMENT_HINT_CHARS,
            field="argument_hint",
        ),
        "model_tier": str(payload.get("model_tier", "") or ""),
        "effort": str(payload.get("effort", "") or ""),
        "include_in_autocomplete": include,
    }


class SkillService:
    """Application service over the skill repository.

    Args:
        repository: The persistence backend.
        durable: Whether *repository* survives server restarts; drives
            ``features.skills`` (same volatile-store rule as prompt
            templates — a browser must not sync against a store that
            reads as "everything deleted" after a bounce).
    """

    def __init__(
        self,
        *,
        repository: SkillRepository,
        authorization: "AuthorizationService",
        durable: bool = True,
        invalidator: "ResourceInvalidator | None" = None,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._durable = durable
        self._invalidator = invalidator

    @property
    def durable(self) -> bool:
        """Whether skills survive a server restart."""
        return self._durable

    async def create(
        self,
        payload: Mapping[str, Any],
        *,
        tenant_id: str,
        owner_user_id: uuid.UUID | None,
    ) -> SkillRecord:
        """Create one skill; scoped principals own what they create."""
        now = time.time()
        record = SkillRecord(
            id=new_skill_id(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_at=now,
            updated_at=now,
            **_validated_fields(payload),
        )
        stored = await self._repository.create(record)
        await self._invalidate(stored)
        return stored

    async def get_visible(
        self,
        skill_id: str,
        *,
        tenant_id: str,
        visible_to: "UserContext | None" = None,
    ) -> tuple[SkillRecord, ResourceAccess]:
        """One visible skill with its authoritative access annotation."""
        record = await self._repository.get(skill_id, tenant_id=tenant_id)
        access = await self._access(record, visible_to)
        return record, access

    async def list_visible(
        self,
        *,
        tenant_id: str,
        visible_to: "UserContext | None" = None,
    ) -> list[tuple[SkillRecord, ResourceAccess]]:
        """Visible skills with authoritative access annotations."""
        optimized = getattr(self._repository, "list_visible_for_user", None)
        if callable(optimized):
            return await optimized(
                tenant_id=tenant_id,
                actor_user_id=(
                    visible_to.principal.user_id
                    if visible_to is not None
                    else None
                ),
            )
        visible: list[tuple[SkillRecord, ResourceAccess]] = []
        for record in await self._repository.list_for_tenant(
            tenant_id=tenant_id
        ):
            try:
                access = await self._access(record, visible_to)
            except SkillNotFound:
                continue
            visible.append((record, access))
        return visible

    async def update(
        self,
        skill_id: str,
        payload: Mapping[str, Any],
        *,
        tenant_id: str,
        expected_revision: int,
        visible_to: "UserContext | None" = None,
    ) -> SkillRecord:
        """Replace the writable fields (edit grant required)."""
        current = await self._repository.get(skill_id, tenant_id=tenant_id)
        await self._access(current, visible_to, minimum=SharePermission.EDIT)
        updated = SkillRecord(
            id=current.id,
            tenant_id=current.tenant_id,
            owner_user_id=current.owner_user_id,
            revision=current.revision,
            created_at=current.created_at,
            updated_at=time.time(),
            **_validated_fields(payload),
        )
        stored = await self._repository.update(
            updated,
            expected_revision=expected_revision,
            actor_user_id=(
                visible_to.principal.user_id if visible_to is not None else None
            ),
        )
        await self._invalidate(stored)
        return stored

    async def delete(
        self,
        skill_id: str,
        *,
        tenant_id: str,
        visible_to: "UserContext | None" = None,
    ) -> None:
        """Delete one skill (owner-only; shares never delete)."""
        current = await self._repository.get(skill_id, tenant_id=tenant_id)
        access = await self._access(current, visible_to)
        if access.mode is AccessMode.SHARED:
            raise SkillNotFound(skill_id)
        await self._repository.delete(
            skill_id,
            tenant_id=tenant_id,
            actor_user_id=(
                visible_to.principal.user_id if visible_to is not None else None
            ),
        )
        if self._invalidator is not None and not getattr(
            self._repository, "atomic_resource_effects", False
        ):
            await self._invalidator.revoke_deleted(
                tenant_id=current.tenant_id,
                owner_user_id=current.owner_user_id,
                resource_type="skill_template",
                resource_id=current.id,
                scope="skills",
                actor_user_id=(
                    visible_to.principal.user_id
                    if visible_to is not None
                    else None
                ),
            )

    async def _invalidate(self, record: SkillRecord) -> None:
        """Publish fallback effects only for volatile repositories."""
        if self._invalidator is None or getattr(
            self._repository, "atomic_resource_effects", False
        ):
            return
        await self._invalidator.invalidate(
            tenant_id=record.tenant_id,
            owner_user_id=record.owner_user_id,
            resource_type="skill_template",
            resource_id=record.id,
            scope="skills",
        )

    async def _access(
        self,
        record: SkillRecord,
        visible_to: "UserContext | None",
        *,
        minimum: SharePermission = SharePermission.VIEW,
    ) -> ResourceAccess:
        """Resolve current owner/direct-share access for one skill."""
        if visible_to is None:
            if record.owner_user_id is None:
                return ResourceAccess(AccessMode.UNSCOPED)
            raise SkillNotFound(record.id)
        access = await self._authorization.resolve_resource_access(
            visible_to.principal,
            owner_user_id=record.owner_user_id,
            resource_tenant_id=record.tenant_id,
            resource_type="skill_template",
            resource_id=record.id,
            minimum=minimum,
        )
        if access is None:
            raise SkillNotFound(record.id)
        return access

    async def owner_user_id(
        self, tenant_id: str, skill_id: str
    ) -> uuid.UUID | None:
        """Owner lookup for the share layer (``None`` = unshareable)."""
        try:
            record = await self._repository.get(skill_id, tenant_id=tenant_id)
        except SkillNotFound:
            return None
        return record.owner_user_id

    async def title(self, tenant_id: str, skill_id: str) -> str | None:
        """Title lookup for the share surface (``None`` = absent)."""
        try:
            record = await self._repository.get(skill_id, tenant_id=tenant_id)
        except SkillNotFound:
            return None
        return record.title

    async def share_facts(
        self, tenant_id: str, skill_ids: Sequence[str]
    ) -> dict[str, "ShareFacts"]:
        """Owner and title of many skills in one read (share layer)."""
        return await self._repository.share_facts(skill_ids, tenant_id=tenant_id)
