"""SCIM 2.0 user provisioning (the ``Users`` resource).

The identity system manages the accounts of the gateway's identity source
through SCIM. Every change runs as a user lifecycle command with the
provisioning actor; this module holds the flows a SCIM request starts, not
the HTTP surface.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, Mapping

from inqtrix.auth.gateway import (
    EMAIL_MAX_LENGTH,
    NAME_MAX_LENGTH,
    SUBJECT_MAX_LENGTH,
    has_control_characters,
)
from inqtrix.auth.lifecycle import (
    ExternalProfile,
    ProfileChange,
    ProvisioningActor,
    ProvisioningOutcome,
    UserLifecycleStatus,
)
from inqtrix.auth.log_redaction import pseudonymous_log_reference
from inqtrix.runs.deletion_operations import new_deletion_operation_id

if TYPE_CHECKING:
    from inqtrix.auth.directory import MirroredUser, UserDirectory
    from inqtrix.auth.lifecycle import UserLifecycleService
    from inqtrix.services.asset_deletion_service import AssetDeletionService

log = logging.getLogger(__name__)

#: Erasures provisioning may start per tenant and hour. A leaked SCIM token
#: thus never erases every account of the identity system within minutes;
#: beyond the limit an account stays deactivated until an administrator
#: erases it.
SCIM_ERASE_MAX_PER_HOUR = 10

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

#: Most users one list answer carries, and the default page size.
MAX_RESULTS = 200
DEFAULT_COUNT = 100

#: Longest part of the SCIM ``name`` object Inqtrix keeps.
_NAME_PART_MAX_LENGTH = NAME_MAX_LENGTH
#: Longest value Inqtrix keeps per text attribute: the bounds of the gateway
#: assertion, so an account SCIM keeps is one a sign-in can reach.
_TEXT_MAX_LENGTH = {
    "userName": SUBJECT_MAX_LENGTH,
    "externalId": SUBJECT_MAX_LENGTH,
    "displayName": NAME_MAX_LENGTH,
    "emails": EMAIL_MAX_LENGTH,
}
_NAME_PARTS = ("givenName", "familyName", "formatted")

#: ``PATCH`` operations (RFC 7644, section 3.5.2).
_PATCH_OPS = frozenset({"add", "replace", "remove"})
#: The attributes a ``PATCH`` changes, by their path in lower case.
_PATCH_PATHS = {
    path.lower(): path
    for path in (
        "active",
        "userName",
        "displayName",
        "name",
        "name.givenName",
        "name.familyName",
        "name.formatted",
        "emails",
        "externalId",
    )
}
_CORE_PREFIX = USER_SCHEMA.lower() + ":"
# Entra sends the address of a user under this filtered path.
_WORK_EMAIL = re.compile(r'^emails\[\s*type\s+eq\s+"work"\s*\]\.value$', re.IGNORECASE)

# The only filters SCIM clients need to match an account: one attribute,
# ``eq``, one quoted value. Attribute names and operators are case-insensitive
# (RFC 7644, 3.4.2.2); the value is a JSON string.
_FILTER = re.compile(
    r'^\s*(username|externalid)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE
)


class ScimError(Exception):
    """A SCIM error answer.

    Args:
        status: The HTTP status.
        detail: An English explanation that names no value of the request.
        scim_type: The SCIM ``scimType``, where RFC 7644 defines one.
    """

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def body(self) -> dict[str, Any]:
        """The SCIM error message of this answer."""
        message: dict[str, Any] = {
            "schemas": [ERROR_SCHEMA],
            "status": str(self.status),
            "detail": self.detail,
        }
        if self.scim_type is not None:
            message["scimType"] = self.scim_type
        return message


def scim_active(value: object) -> bool:
    """``active`` as SCIM clients send it: a boolean, or the text Entra sends."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ScimError(400, "The attribute active must be a boolean.", "invalidValue")


def _is_plain_text(value: str, limit: int) -> bool:
    """Whether *value* fits *limit* without control characters or lone surrogates.

    A lone surrogate comes only from a JSON escape; no account may hold one,
    since no answer could encode it.
    """
    return (
        len(value) <= limit
        and not has_control_characters(value)
        and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


def _bounded(value: str, attribute: str) -> str:
    """*value* if it is plain text within the bound of *attribute*."""
    limit = _TEXT_MAX_LENGTH.get(attribute, _NAME_PART_MAX_LENGTH)
    if not _is_plain_text(value, limit):
        raise ScimError(
            400,
            f"The attribute {attribute} must be text of at most {limit} characters "
            "without control characters.",
            "invalidValue",
        )
    return value


def _required_text(body: Mapping[str, Any], attribute: str) -> str:
    value = body.get(attribute)
    if not isinstance(value, str) or not value.strip():
        raise ScimError(400, f"The attribute {attribute} is required.", "invalidValue")
    return _bounded(value, attribute)


def _name_part(value: object) -> str:
    if not isinstance(value, str):
        raise ScimError(
            400,
            "Each part of name is text of at most 256 characters.",
            "invalidValue",
        )
    return _bounded(value, "name")


def _name_parts(value: object) -> dict[str, str]:
    """The parts of a SCIM ``name`` object Inqtrix keeps, empty ones included."""
    if not isinstance(value, dict):
        raise ScimError(400, "The attribute name must be an object.", "invalidValue")
    return {
        part: _name_part(value[part])
        for part in _NAME_PARTS
        if value.get(part) is not None
    }


def scim_name(value: object) -> dict[str, str] | None:
    """The parts of the SCIM ``name`` object Inqtrix keeps; empty parts are dropped."""
    if value is None:
        return None
    return {part: text for part, text in _name_parts(value).items() if text} or None


def scim_primary_email(value: object) -> str:
    """The primary address of the SCIM ``emails`` attribute, or the first."""
    if value is None:
        return ""
    if not isinstance(value, list):
        raise ScimError(400, "The attribute emails must be a list.", "invalidValue")
    emails = [
        email
        for email in value
        if isinstance(email, dict) and isinstance(email.get("value"), str)
    ]
    if not emails:
        return ""
    primary = next((email for email in emails if email.get("primary") is True), emails[0])
    return _bounded(primary["value"], "emails")


def _profile_from_body(body: Mapping[str, Any]) -> ExternalProfile:
    """The profile a SCIM ``User`` body describes, ``active`` aside."""
    user_name = _required_text(body, "userName")
    display_name = body.get("displayName")
    if display_name is not None and not isinstance(display_name, str):
        raise ScimError(400, "The attribute displayName must be text.", "invalidValue")
    if display_name:
        _bounded(display_name, "displayName")
    return ExternalProfile(
        user_name=user_name,
        display_name=display_name or None,
        name=scim_name(body.get("name")),
        email=scim_primary_email(body.get("emails")),
    )


def user_from_body(body: Mapping[str, Any]) -> tuple[str, ExternalProfile]:
    """The ``externalId`` and the profile of a SCIM ``User`` body."""
    external_id = _required_text(body, "externalId")
    profile = _profile_from_body(body)
    return external_id, replace(profile, active=scim_active(body.get("active", True)))


def replacement_from_body(
    body: Mapping[str, Any],
) -> tuple[tuple[str, ...], ProfileChange]:
    """The ``externalId`` a ``PUT`` names and its change to the profile.

    ``PUT`` replaces ``userName``, ``displayName``, ``name`` and ``emails``;
    what the body leaves out is cleared. ``active`` changes only when the
    body carries it: a missing value disables nothing and enables nothing.
    """
    external_id = body.get("externalId")
    if external_id is not None and not isinstance(external_id, str):
        raise ScimError(400, "The attribute externalId must be text.", "invalidValue")
    if external_id is not None:
        _bounded(external_id, "externalId")
    replacement = _profile_from_body(body)
    active = scim_active(body["active"]) if "active" in body else None

    def change(profile: ExternalProfile) -> ExternalProfile:
        return replace(replacement, active=profile.active if active is None else active)

    return ((external_id,) if external_id is not None else ()), change


def _text(value: object, attribute: str) -> str:
    if not isinstance(value, str):
        raise ScimError(400, f"The attribute {attribute} must be text.", "invalidValue")
    return _bounded(value, attribute)


def _set(**fields: Any) -> ProfileChange:
    return lambda profile: replace(profile, **fields)


def _merge_name(parts: Mapping[str, str]) -> ProfileChange:
    """Set the given parts of ``name``; an empty part is removed."""

    def change(profile: ExternalProfile) -> ExternalProfile:
        name = dict(profile.name or {})
        for part, text in parts.items():
            if text:
                name[part] = text
            else:
                name.pop(part, None)
        return replace(profile, name=name or None)

    return change


def _patch_target(path: str) -> str | None:
    """The attribute a ``PATCH`` path names, ``None`` for one Inqtrix does not keep."""
    normalized = path.strip()
    if normalized.lower().startswith(_CORE_PREFIX):
        normalized = normalized[len(_CORE_PREFIX) :]
    if _WORK_EMAIL.match(normalized):
        return "emails.work"
    return _PATCH_PATHS.get(normalized.lower())


def _patch_change(
    op: str, path: str, value: object, external_ids: list[str]
) -> ProfileChange | None:
    """The change one operation makes to the profile, if any.

    An ``externalId`` is collected for the caller to compare instead.
    """
    target = _patch_target(path)
    if target is None:
        # Attributes Inqtrix does not keep, roles among them, change nothing,
        # as in a POST.
        return None
    if op == "remove":
        if target in ("userName", "externalId", "active"):
            raise ScimError(400, f"The attribute {target} cannot be removed.", "mutability")
        if target == "displayName":
            return _set(display_name=None)
        if target == "name":
            return _set(name=None)
        if target.startswith("name."):
            return _merge_name({target.removeprefix("name."): ""})
        return _set(email="")
    if target == "externalId":
        external_ids.append(_text(value, "externalId"))
        return None
    if target == "active":
        return _set(active=scim_active(value))
    if target == "userName":
        if not isinstance(value, str) or not value.strip():
            raise ScimError(400, "The attribute userName is required.", "invalidValue")
        return _set(user_name=_bounded(value, "userName"))
    if target == "displayName":
        return _set(display_name=_text(value, "displayName") or None)
    if target == "name":
        return _merge_name(_name_parts(value))
    if target.startswith("name."):
        return _merge_name({target.removeprefix("name."): _name_part(value)})
    if target == "emails.work":
        return _set(email=_text(value, "emails"))
    return _set(email=scim_primary_email(value))


def patch_from_body(
    body: Mapping[str, Any],
) -> tuple[tuple[str, ...], ProfileChange]:
    """The ``externalId`` values a ``PATCH`` names and its change to the profile.

    The operations apply in order (RFC 7644, section 3.5.2): ``op`` and the
    path in any case, the path also with the core schema prefix, or no path
    with an object whose keys are paths. An object for ``name`` sets only
    the parts it names.

    Raises:
        ScimError: An operation is malformed or cannot apply.
    """
    operations = body.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise ScimError(400, "The request needs a non-empty list of Operations.", "invalidSyntax")
    external_ids: list[str] = []
    changes: list[ProfileChange] = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ScimError(400, "Each operation must be an object.", "invalidSyntax")
        op = operation.get("op")
        if not isinstance(op, str) or op.lower() not in _PATCH_OPS:
            raise ScimError(
                400, "The op of an operation must be add, replace or remove.", "invalidSyntax"
            )
        op = op.lower()
        path = operation.get("path")
        value = operation.get("value")
        if path is None:
            if op == "remove":
                raise ScimError(400, "A remove operation needs a path.", "noTarget")
            if not isinstance(value, dict):
                raise ScimError(
                    400, "An operation without a path needs an object as its value.", "invalidValue"
                )
            targets = list(value.items())
        elif isinstance(path, str):
            targets = [(path, value)]
        else:
            raise ScimError(400, "The path of an operation must be text.", "invalidPath")
        for target_path, target_value in targets:
            step = _patch_change(op, target_path, target_value, external_ids)
            if step is not None:
                changes.append(step)

    def change(profile: ExternalProfile) -> ExternalProfile:
        for step in changes:
            profile = step(profile)
        return profile

    return tuple(external_ids), change


def parse_filter(text: str) -> tuple[Literal["userName", "externalId"], str]:
    """The attribute and value of a supported SCIM filter.

    Raises:
        ScimError: ``invalidFilter`` for anything but ``userName eq`` and
            ``externalId eq`` with one quoted value.
    """
    match = _FILTER.match(text)
    if match is not None:
        try:
            value = json.loads(f'"{match.group(2)}"')
        except ValueError:
            value = None
        if isinstance(value, str):
            attribute = "userName" if match.group(1).lower() == "username" else "externalId"
            # What no account holds is no value to filter by either.
            if _is_plain_text(value, _TEXT_MAX_LENGTH[attribute]):
                return attribute, value
    raise ScimError(
        400,
        "Only the filters userName eq and externalId eq with one quoted value are supported.",
        "invalidFilter",
    )


def _timestamp(seconds: float) -> str:
    return (
        dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def user_resource(user: "MirroredUser", *, location: str) -> dict[str, Any]:
    """The SCIM ``User`` resource of *user*.

    An account of the source that provisioning never bound has no SCIM
    user name yet; it shows its subject until a change brings one.
    """
    meta: dict[str, Any] = {"resourceType": "User", "location": location}
    if user.created_at is not None:
        meta["created"] = _timestamp(user.created_at)
    resource: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(user.user_id),
        "externalId": user.subject,
        "userName": user.scim_user_name or user.subject,
        "active": user.disabled_at is None,
        "meta": meta,
    }
    if user.display_name:
        resource["displayName"] = user.display_name
    if user.scim_name:
        resource["name"] = dict(user.scim_name)
    if user.email:
        resource["emails"] = [{"value": user.email, "primary": True, "type": "work"}]
    return resource


def service_provider_config(base_url: str) -> dict[str, Any]:
    """What this SCIM service supports (RFC 7643, section 5)."""
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_RESULTS},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "The static SCIM token of the deployment.",
                "primary": True,
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{base_url}/ServiceProviderConfig",
        },
    }


def _attribute(name: str, **overrides: Any) -> dict[str, Any]:
    attribute = {
        "name": name,
        "type": "string",
        "multiValued": False,
        "required": False,
        "caseExact": False,
        "mutability": "readWrite",
        "returned": "default",
        "uniqueness": "none",
    }
    attribute.update(overrides)
    return attribute


def user_schema(base_url: str) -> dict[str, Any]:
    """The ``User`` schema with the attributes Inqtrix keeps."""
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
        "id": USER_SCHEMA,
        "name": "User",
        "attributes": [
            _attribute("userName", required=True, uniqueness="server"),
            _attribute(
                "name",
                type="complex",
                subAttributes=[_attribute(part) for part in _NAME_PARTS],
            ),
            _attribute("displayName"),
            _attribute(
                "emails",
                type="complex",
                multiValued=True,
                subAttributes=[
                    _attribute("value"),
                    _attribute("type"),
                    _attribute("primary", type="boolean"),
                ],
            ),
            _attribute("active", type="boolean"),
            _attribute(
                "externalId", required=True, caseExact=True, mutability="immutable"
            ),
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{USER_SCHEMA}"},
    }


def user_resource_type(base_url: str) -> dict[str, Any]:
    """The only resource type: ``User``."""
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": USER_SCHEMA,
        "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/User"},
    }


def list_response(resources: list[dict[str, Any]], *, total: int, start_index: int) -> dict[str, Any]:
    """A SCIM ``ListResponse``."""
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


class ScimUsers:
    """The SCIM ``Users`` flows on top of the user lifecycle.

    Args:
        lifecycle: The user lifecycle of the gateway deployment.
        users: The user directory SCIM reads from.
        source_issuer: The issuer key of the identity source; SCIM sees
            only accounts bound under it.
        deletions: The deletion service that runs an account erasure;
            required for ``erase``.
        delete_action: ``disable`` deactivates an account and leaves a
            tombstone; ``erase`` then also starts the account erasure, at
            most :data:`SCIM_ERASE_MAX_PER_HOUR` times an hour.
        tenant_id: The tenant SCIM provisions.
    """

    def __init__(
        self,
        *,
        lifecycle: "UserLifecycleService",
        users: "UserDirectory",
        source_issuer: str,
        deletions: "AssetDeletionService | None",
        delete_action: Literal["disable", "erase"],
        tenant_id: str = "default",
    ) -> None:
        if delete_action == "erase" and deletions is None:
            raise ValueError(
                "SCIM delete action 'erase' needs the deletion service that "
                "runs an account erasure"
            )
        self._lifecycle = lifecycle
        self._users = users
        self._source_issuer = source_issuer
        self._deletions = deletions
        self._delete_action = delete_action
        self._tenant_id = tenant_id

    async def create(
        self, external_id: str, profile: ExternalProfile
    ) -> ProvisioningOutcome:
        """``POST /Users``: a new account, or the adoption of an unbound one."""
        outcome = await self._lifecycle.provision_external_user(
            tenant_id=self._tenant_id, subject=external_id, profile=profile
        )
        if outcome.status is UserLifecycleStatus.UPDATED and outcome.user is not None:
            log.info(
                "SCIM POST %s an account (outcome=provisioned user_ref=%s).",
                "adopted" if outcome.adopted else "created",
                pseudonymous_log_reference("usr", outcome.user.user_id),
            )
        return outcome

    async def get(self, user_id: uuid.UUID) -> "MirroredUser | None":
        """``GET /Users/{id}``: the account, if SCIM may see it.

        SCIM sees accounts of the identity source; a tombstone and an
        account being erased are gone for it.
        """
        user = await self._users.find_by_user_id(
            tenant_id=self._tenant_id, user_id=user_id
        )
        if (
            user is None
            or user.issuer != self._source_issuer
            or user.scim_deleted_at is not None
            or user.erasure_operation_id is not None
        ):
            return None
        return user

    async def update(
        self,
        user_id: uuid.UUID,
        *,
        external_ids: tuple[str, ...],
        change: ProfileChange,
    ) -> ProvisioningOutcome:
        """``PUT`` and ``PATCH /Users/{id}``: one change to an account SCIM sees.

        ``externalId`` is immutable: a request may repeat the account's
        subject, never name another one.

        Raises:
            ScimError: ``mutability`` for another ``externalId``.
        """
        user = await self.get(user_id)
        if user is None:
            return ProvisioningOutcome(UserLifecycleStatus.NOT_FOUND)
        if any(external_id != user.subject for external_id in external_ids):
            raise ScimError(400, "The attribute externalId cannot change.", "mutability")
        return await self._lifecycle.update_external_profile(
            tenant_id=self._tenant_id, user_id=user_id, change=change
        )

    async def list_page(
        self, *, filter_text: str | None, start_index: int, count: int
    ) -> tuple[int, tuple["MirroredUser", ...]]:
        """``GET /Users``: one page of the accounts SCIM sees, and their total."""
        user_name = subject = None
        if filter_text is not None:
            attribute, value = parse_filter(filter_text)
            if attribute == "userName":
                user_name = value
            else:
                subject = value
        return await self._users.list_users_page(
            tenant_id=self._tenant_id,
            issuer=self._source_issuer,
            user_name=user_name,
            subject=subject,
            start_index=start_index,
            count=count,
        )

    async def delete(self, user_id: uuid.UUID) -> UserLifecycleStatus:
        """``DELETE /Users/{id}``: deactivate with a tombstone, maybe erase.

        The answer is the one of the deprovisioning: once it ran, the
        resource is gone for SCIM, whatever becomes of the erasure.
        """
        status = await self._lifecycle.deprovision(
            tenant_id=self._tenant_id, user_id=user_id
        )
        if status is not UserLifecycleStatus.UPDATED:
            return status
        user_ref = pseudonymous_log_reference("usr", user_id)
        log.info("SCIM DELETE deactivated an account (outcome=deprovisioned user_ref=%s).", user_ref)
        if self._delete_action == "erase":
            await self._start_erasure(user_id, user_ref)
        return status

    async def _start_erasure(self, user_id: uuid.UUID, user_ref: str) -> None:
        outcome, report = await self._lifecycle.begin_erasure(
            tenant_id=self._tenant_id,
            user_id=user_id,
            actor=ProvisioningActor(),
            operation_id=new_deletion_operation_id(),
            hourly_limit=SCIM_ERASE_MAX_PER_HOUR,
        )
        if outcome is UserLifecycleStatus.LIMITED:
            log.warning(
                "SCIM DELETE started no erasure: the hourly limit is reached "
                "(outcome=scim_erase_limited user_ref=%s); the account stays "
                "deactivated until an administrator erases it.",
                user_ref,
            )
            return
        if outcome is UserLifecycleStatus.REFUSED:
            log.warning(
                "SCIM DELETE started no erasure: the account still holds what "
                "blocks it (outcome=erasure_refused user_ref=%s reasons=%d); it "
                "stays deactivated.",
                user_ref,
                len(report.get("refused") or ()),
            )
            return
        if outcome is not UserLifecycleStatus.UPDATED:
            log.warning(
                "SCIM DELETE started no erasure "
                "(outcome=scim_erase_not_started status=%s user_ref=%s).",
                outcome.value,
                user_ref,
            )
            return
        assert self._deletions is not None
        try:
            await self._deletions.start_user_erasure(
                user_id,
                actor=ProvisioningActor(),
                tenant_id=self._tenant_id,
                operation_id=str(report["operation_id"]),
            )
        except Exception as error:  # noqa: BLE001 - the DELETE itself succeeded
            # The account is deprovisioned and marked for erasure; an
            # administrator resumes the erasure from the admin surface.
            log.warning(
                "SCIM DELETE could not start the erasure "
                "(outcome=scim_erase_start_failed user_ref=%s error=%s); the "
                "account is marked for erasure until an administrator resumes "
                "it with Delete account.",
                user_ref,
                type(error).__name__,
            )
