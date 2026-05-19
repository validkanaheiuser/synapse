#
# STAFF mod -- widget-group CRUD endpoints (AGENT P).
#
#   GET    /_synapse/staff/v1/groups
#   POST   /_synapse/staff/v1/groups
#   GET    /_synapse/staff/v1/groups/{group_id}
#   PATCH  /_synapse/staff/v1/groups/{group_id}
#   DELETE /_synapse/staff/v1/groups/{group_id}
#
# A "group" is a named bag of staff users that collectively owns a set
# of general_widgets.  The widget injector (widget_inject.py) consults
# the staff member's group memberships to decide which general_widgets
# to surface in a DM.
#
# Every endpoint authenticates via `_require_staff_auth` (JWT or legacy
# X-Staff-Secret) and emits an audit row via `_audit_record` on success,
# matching the pattern in rest_admin_listing.py.
#

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.http.server import JsonResource
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


_MAX_NAME_LEN = 200


# ---------------------------------------------------------------- validators


def _validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise SynapseError(400, "name must be a string")
    stripped = name.strip()
    if not stripped:
        raise SynapseError(400, "name must be non-empty")
    if len(stripped) > _MAX_NAME_LEN:
        raise SynapseError(
            400, f"name must be at most {_MAX_NAME_LEN} characters",
        )
    return stripped


def _validate_description(description: Any) -> Optional[str]:
    if description is None:
        return None
    if not isinstance(description, str):
        raise SynapseError(400, "description must be a string or null")
    return description


# ----------------------------------------------------------------- list / create


class StaffGroupListCreateServlet(StaffRestServlet):
    """GET and POST against the /groups collection."""

    PATTERNS = staff_pattern("/groups")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        groups = await self.store.group_list_full()
        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body=None, target=None,
        )
        return 200, {"groups": groups}

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        body = parse_json_object_from_request(request)

        name = _validate_name(body.get("name"))
        description = _validate_description(body.get("description"))
        created_by = outcome.actor_user_id or "secret"

        group_id = await self.store.group_create(
            name=name, description=description, created_by=created_by,
        )
        group = await self.store.group_get(group_id)
        # group_get just ran after a successful insert in the same store;
        # treat None here as a server-side invariant violation.
        if group is None:
            raise SynapseError(500, "group disappeared after create")

        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body={"name": name}, target=group_id,
        )
        return 200, group


# -------------------------------------------------------------- get / patch / delete


class StaffGroupItemServlet(StaffRestServlet):
    """GET, PATCH, DELETE against a single /groups/{group_id} resource."""

    PATTERNS = staff_pattern("/groups/(?P<group_id>[^/]+)")

    async def on_GET(self, request, group_id: str) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        group = await self.store.group_get(group_id)
        if group is None:
            raise SynapseError(404, "group not found")
        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body=None, target=group_id,
        )
        return 200, group

    async def on_PATCH(
        self, request, group_id: str,
    ) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        body = parse_json_object_from_request(request)

        existing = await self.store.group_get(group_id)
        if existing is None:
            raise SynapseError(404, "group not found")

        fields: Dict[str, Any] = {}
        if "name" in body:
            fields["name"] = _validate_name(body["name"])
        if "description" in body:
            fields["description"] = _validate_description(body["description"])
        if "members" in body:
            fields["members"] = await self._validate_members(body["members"])
        if "widgets" in body:
            fields["widgets"] = await self._validate_widgets(body["widgets"])

        if not fields:
            raise SynapseError(400, "no updatable fields supplied")

        await self.store.group_update(group_id, fields)
        updated = await self.store.group_get(group_id)
        if updated is None:
            raise SynapseError(500, "group disappeared after update")

        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body={k: True for k in fields.keys()}, target=group_id,
        )
        return 200, updated

    async def on_DELETE(
        self, request, group_id: str,
    ) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        deleted = await self.store.group_delete(group_id)
        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body=None, target=group_id,
        )
        return 200, {"deleted": bool(deleted)}

    # ----- input validation ------------------------------------------------

    async def _validate_members(self, members: Any) -> List[str]:
        if not isinstance(members, list):
            raise SynapseError(400, "members must be a list of MXIDs")
        out: List[str] = []
        seen: set = set()
        for m in members:
            if not isinstance(m, str) or not UserID.is_valid(m):
                raise SynapseError(
                    400, f"members entry is not a valid MXID: {m!r}",
                )
            if m in seen:
                continue
            seen.add(m)
            out.append(m)
            if not self.store.is_staff_user(m):
                # Don't reject: the operator might create the group before
                # the staff row, or grant staff later.  Just log it.
                logger.warning(
                    "STAFF: AGENT P group member %s is not on the staff "
                    "allowlist; widgets will only inject once they are",
                    m,
                )
        return out

    async def _validate_widgets(self, widgets: Any) -> List[str]:
        if not isinstance(widgets, list):
            raise SynapseError(400, "widgets must be a list of widget_ids")
        out: List[str] = []
        seen: set = set()
        for w in widgets:
            if not isinstance(w, str) or not w:
                raise SynapseError(
                    400, f"widgets entry must be a non-empty string: {w!r}",
                )
            if w in seen:
                continue
            seen.add(w)
            row = await self.store.widget_get(w)
            if row is None:
                raise SynapseError(400, f"widget not found: {w}")
            if row.get("widget_type") != "general_widget":
                raise SynapseError(
                    400,
                    f"widget {w} is not a general_widget; only "
                    f"general_widgets can be group-scoped",
                )
            out.append(w)
        return out


# ----------------------------------------------------------------- registration


def register_servlets(
    hs: "HomeServer", store: "StaffStore", resource: "JsonResource",
) -> None:
    StaffGroupListCreateServlet(hs, store).register(resource)
    item_servlet = StaffGroupItemServlet(hs, store)
    item_servlet.register(resource)
    # `RestServlet.register` only auto-wires GET / PUT / POST / DELETE.
    # PATCH must be hooked up explicitly -- same pattern as rest_schedule.py.
    resource.register_paths(
        "PATCH",
        item_servlet.PATTERNS,
        item_servlet.on_PATCH,
        item_servlet.__class__.__name__,
    )
