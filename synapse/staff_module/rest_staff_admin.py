#
# STAFF mod — allowlist admin endpoints (bootstrap).
#
# All three are gated by `X-Staff-Secret` so the very first staff member
# can be added without already being a logged-in admin.
#

from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore


class StaffAddServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/staff/add")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)
        user_id = body.get("user_id")
        if not isinstance(user_id, str) or not UserID.is_valid(user_id):
            raise SynapseError(400, "user_id must be a valid MXID")
        note = body.get("note")
        added_by = body.get("added_by", "secret")
        await self.store.add_staff_user(user_id, added_by=added_by, note=note)
        return 200, {"user_id": user_id}


class StaffRemoveServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/staff/remove")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)
        user_id = body.get("user_id")
        if not isinstance(user_id, str):
            raise SynapseError(400, "user_id is required")
        deleted = await self.store.remove_staff_user(user_id)
        return 200, {"user_id": user_id, "removed": bool(deleted)}


class StaffListServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/staff/list")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        rows = await self.store.list_staff_users()
        return 200, {"users": rows}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffAddServlet(hs, store).register(resource)
    StaffRemoveServlet(hs, store).register(resource)
    StaffListServlet(hs, store).register(resource)
