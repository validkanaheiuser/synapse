#
# STAFF mod — Unified Logout and Delete Room Endpoint
#
# Client calls:
#   POST /api/logoutAndDelete
#   Body: { "room_id": "!room:domain", "current_user_id": "@staff:domain" }
#
# Forces logout on the other participant(s) in the direct chat (guest / customer),
# invalidates their access tokens and devices, cleans up custom nicknames,
# and allows clean departure from the conversation.
#

import logging
import re
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .rest_base import STAFF_API_PREFIX, StaffRestServlet

if TYPE_CHECKING:
    from synapse.http.server import JsonResource
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


def _set_cors_headers(request) -> None:
    request.setHeader(b"Access-Control-Allow-Origin", b"*")
    request.setHeader(b"Access-Control-Allow-Methods", b"POST, OPTIONS")
    request.setHeader(
        b"Access-Control-Allow-Headers",
        b"X-Requested-With, Content-Type, Authorization, Date, X-STAFF-Client",
    )
    request.setHeader(b"Access-Control-Max-Age", b"3600")


class StaffLogoutAndDeleteServlet(StaffRestServlet):
    """Unified endpoint called by element-web when deleting a DM conversation."""

    PATTERNS = (
        re.compile(r"^/api/logoutAndDelete/?$"),
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/logoutAndDelete/?$"),
        re.compile(r"^/_synapse/staff/logoutAndDelete/?$"),
    )

    def register(self, http_server: "JsonResource") -> None:
        super().register(http_server)
        http_server.register_paths(
            "OPTIONS", self.PATTERNS, self.on_OPTIONS, self.__class__.__name__
        )

    def on_OPTIONS(self, request) -> Tuple[int, JsonDict]:
        _set_cors_headers(request)
        return 204, {}

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        _set_cors_headers(request)

        body = parse_json_object_from_request(request)
        room_id = body.get("room_id")
        current_user_id = body.get("current_user_id")

        if not room_id or not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room ID", Codes.INVALID_PARAM)

        # Authenticate caller from Authorization Bearer token if present
        auth_header = request.getHeader(b"Authorization")
        if auth_header:
            try:
                requester = await self.hs.get_auth().get_user_by_req(request, allow_guest=False)
                current_user_id = requester.user.to_string()
            except Exception:
                pass

        if not current_user_id:
            raise SynapseError(400, "Missing current_user_id", Codes.MISSING_PARAM)

        if not UserID.is_valid(current_user_id):
            raise SynapseError(400, f"Invalid current_user_id: {current_user_id}", Codes.INVALID_PARAM)

        if not self.store.is_staff_user(current_user_id):
            raise SynapseError(403, "User is not authorized as staff", Codes.FORBIDDEN)

        store = self.hs.get_datastores().main
        auth_handler = self.hs.get_auth_handler()
        device_handler = self.hs.get_device_handler()

        # Find members in this room
        try:
            joined_users = await store.get_users_in_room(room_id)
        except Exception as e:
            logger.warning("STAFF: logoutAndDelete get_users_in_room failed for %s: %s", room_id, e)
            joined_users = []

        logged_out_users = []
        for user_id in joined_users:
            # Never force-logout ourselves or another staff member
            if user_id == current_user_id or self.store.is_staff_user(user_id):
                continue

            try:
                # Invalidate access tokens
                await auth_handler.delete_access_tokens_for_user(user_id)
                # Invalidate devices
                devices_map = await store.get_devices_by_user(user_id)
                if devices_map:
                    await device_handler.delete_devices(user_id, list(devices_map.keys()))
                logged_out_users.append(user_id)
                logger.info("STAFF: logoutAndDelete logged out target user %s from room %s", user_id, room_id)
            except Exception as e:
                logger.warning("STAFF: logoutAndDelete force-logout failed for %s: %s", user_id, e)

        # Clean up any custom nickname for this room
        try:
            await self.store.dm_names_delete(current_user_id, room_id)
        except Exception:
            pass

        return 200, {
            "status": "success",
            "message": "Conversation deleted and user logged out successfully",
            "room_id": room_id,
            "logged_out_users": logged_out_users,
            "logout_status": {
                "status": "logged_out",
                "message": f"Successfully logged out {len(logged_out_users)} customer user(s)",
            },
        }


def register_servlets(
    hs: "HomeServer", store: "StaffStore", resource: "JsonResource"
) -> None:
    StaffLogoutAndDeleteServlet(hs, store).register(resource)
