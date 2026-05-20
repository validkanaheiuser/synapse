#
# STAFF mod — allowlist admin endpoints (bootstrap).
#
# All three are gated by `X-Staff-Secret` so the very first staff member
# can be added without already being a logged-in admin.
#

import logging
from typing import TYPE_CHECKING, List, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


class StaffAddServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/staff/add")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        body = parse_json_object_from_request(request)
        user_id = body.get("user_id")
        if not isinstance(user_id, str) or not UserID.is_valid(user_id):
            raise SynapseError(400, "user_id must be a valid MXID")
        note = body.get("note")
        added_by = body.get("added_by", "secret")

        # === AGENT P ===
        # Optional `groups` field: a list of group_ids the new staff is
        # auto-added to.  We validate the entries are strings up-front so
        # we fail before the staff row gets inserted, but we tolerate
        # group_ids that no longer exist (silent skip with a warning) so
        # a stale frontend doesn't break the staff-add flow.
        groups_raw = body.get("groups")
        groups: List[str] = []
        if groups_raw is not None:
            if not isinstance(groups_raw, list):
                raise SynapseError(400, "groups must be a list of group_ids")
            for g in groups_raw:
                if not isinstance(g, str) or not g:
                    raise SynapseError(
                        400, f"groups entry must be a non-empty string: {g!r}",
                    )
                groups.append(g)
        # === END AGENT P ===

        await self.store.add_staff_user(user_id, added_by=added_by, note=note)

        # === AGENT P ===
        # Membership additions happen AFTER the staff row is inserted so
        # we never end up with a member row pointing at a user we failed
        # to add.  Missing groups are skipped with a warn-log; we do not
        # roll back the staff-add for a bad group_id.
        added_groups: List[str] = []
        skipped_groups: List[str] = []
        for gid in groups:
            existing = await self.store.group_get(gid)
            if existing is None:
                logger.warning(
                    "STAFF: AGENT P /staff/add skipping unknown group_id "
                    "%s for user %s",
                    gid, user_id,
                )
                skipped_groups.append(gid)
                continue
            try:
                await self.store.group_add_member(gid, user_id)
                added_groups.append(gid)
            except Exception:
                logger.exception(
                    "STAFF: AGENT P /staff/add failed to add %s to group "
                    "%s; staff row remains in place",
                    user_id, gid,
                )
                skipped_groups.append(gid)

        resp: JsonDict = {"user_id": user_id}
        if groups_raw is not None:
            resp["groups_added"] = added_groups
            resp["groups_skipped"] = skipped_groups
        # === END AGENT P ===
        return 200, resp


class StaffRemoveServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/staff/remove")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        body = parse_json_object_from_request(request)
        user_id = body.get("user_id")
        if not isinstance(user_id, str):
            raise SynapseError(400, "user_id is required")
        deleted = await self.store.remove_staff_user(user_id)
        return 200, {"user_id": user_id, "removed": bool(deleted)}


class StaffListServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/staff/list")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        rows = await self.store.list_staff_users()
        return 200, {"users": rows}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffAddServlet(hs, store).register(resource)
    StaffRemoveServlet(hs, store).register(resource)
    StaffListServlet(hs, store).register(resource)
