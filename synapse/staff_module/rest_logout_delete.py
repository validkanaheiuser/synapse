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
from synapse.staff_filter import has_staff_header, is_group_room, is_room_owner
from synapse.types import JsonDict, UserID

from .forge import fake_requester
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

        is_staff = self.store.is_staff_user(current_user_id)
        if not is_staff:
            if not has_staff_header(request):
                raise SynapseError(403, "Client admin required", Codes.FORBIDDEN)
            if not await is_room_owner(self.hs, room_id, current_user_id):
                raise SynapseError(
                    403,
                    "Only the room owner or staff can delete this room",
                    Codes.FORBIDDEN,
                )

        store = self.hs.get_datastores().main
        auth_handler = self.hs.get_auth_handler()
        device_handler = self.hs.get_device_handler()
        room_member_handler = self.hs.get_room_member_handler()
        account_data_handler = self.hs.get_account_data_handler()

        # 1. Collect all users associated with this room:
        # - Current joined users
        try:
            joined_users = await store.get_users_in_room(room_id)
        except Exception as e:
            logger.warning("STAFF: logoutAndDelete get_users_in_room failed for %s: %s", room_id, e)
            joined_users = []

        # - All users who have or ever had membership records in this room
        try:
            def _get_all_room_users_txn(txn):
                txn.execute("SELECT DISTINCT user_id FROM room_memberships WHERE room_id = ?", (room_id,))
                return [row[0] for row in txn]

            all_membership_users = await store.db_pool.runInteraction(
                "get_all_room_users", _get_all_room_users_txn
            )
        except Exception as e:
            logger.warning("STAFF: logoutAndDelete get_all_room_users failed for %s: %s", room_id, e)
            all_membership_users = []

        all_user_ids = set(all_membership_users) | set(joined_users) | {current_user_id}

        # DM vs normal group room. For a group room, "delete" = kick every member +
        # purge the room ONLY. Do NOT force-logout members' whole sessions and do NOT
        # touch their m.direct — those are DM-specific. (AskUser 2026-09-30)
        is_group = await is_group_room(self.hs, room_id, current_user_id)

        owner_requester = None
        if auth_header:
            try:
                owner_requester = await self.hs.get_auth().get_user_by_req(request, allow_guest=False)
            except Exception:
                pass
        if owner_requester is None:
            owner_requester = fake_requester(self.hs, current_user_id)

        # 2. Kick non-staff target users and invalidate their sessions (force logout)
        logged_out_users = []
        for user_id in all_user_ids:
            if user_id == current_user_id or self.store.is_staff_user(user_id):
                continue

            # Kick if currently joined/invited
            if user_id in joined_users:
                try:
                    target_user = UserID.from_string(user_id)
                    await room_member_handler.update_membership(
                        owner_requester,
                        target_user,
                        room_id,
                        "leave",
                        content={"reason": "Room deleted by owner"},
                    )
                except Exception as e:
                    logger.warning("STAFF: logoutAndDelete kick failed for %s in %s: %s", user_id, room_id, e)

            # DM only: invalidate the target's whole session (tokens + devices).
            # For a group room, we only remove them from the room (kick above).
            if not is_group:
                try:
                    await auth_handler.delete_access_tokens_for_user(user_id)
                    devices_map = await store.get_devices_by_user(user_id)
                    if devices_map:
                        await device_handler.delete_devices(user_id, list(devices_map.keys()))
                    logged_out_users.append(user_id)
                    logger.info("STAFF: logoutAndDelete logged out target user %s from room %s", user_id, room_id)
                except Exception as e:
                    logger.warning("STAFF: logoutAndDelete force-logout failed for %s: %s", user_id, e)

        # 3. DM only: clean up 'm.direct' so Element stops showing it as a (historical)
        # DM. Group rooms are not DMs, so skip. (AskUser 2026-09-30)
        for uid in (() if is_group else all_user_ids):
            try:
                user_account_data = await store.get_global_account_data_for_user(uid)
                direct_rooms = user_account_data.get("m.direct", {})
                if isinstance(direct_rooms, dict):
                    modified = False
                    new_direct = {}
                    for partner_id, rids in direct_rooms.items():
                        if isinstance(rids, list) and room_id in rids:
                            filtered = [r for r in rids if r != room_id]
                            if filtered:
                                new_direct[partner_id] = filtered
                            modified = True
                        else:
                            new_direct[partner_id] = rids
                    if modified:
                        await account_data_handler.add_account_data_for_user(
                            uid, "m.direct", new_direct
                        )
            except Exception as e:
                logger.warning("STAFF: logoutAndDelete clean m.direct failed for %s in %s: %s", uid, room_id, e)

        # 4. Mark room as forgotten for ALL participants in database
        # Forgotten = 1 ensures Synapse /sync will NEVER return this room to any of these users
        for uid in all_user_ids:
            try:
                await store.forget(uid, room_id)
            except Exception as e:
                logger.warning("STAFF: logoutAndDelete forget failed for %s in %s: %s", uid, room_id, e)

        # 5. Clean up custom staff tables (nicknames, scheduled messages)
        try:
            await self.store.dm_names_delete(current_user_id, room_id)
        except Exception:
            pass

        try:
            def _delete_scheduled_txn(txn):
                txn.execute("DELETE FROM staff_scheduled_messages WHERE room_id = ?", (room_id,))
            await store.db_pool.runInteraction("delete_room_scheduled_messages", _delete_scheduled_txn)
        except Exception as e:
            logger.warning("STAFF: failed to delete scheduled messages for %s: %s", room_id, e)

        # 6. Shutdown room and purge completely from database
        try:
            room_shutdown_handler = self.hs.get_room_shutdown_handler()
            await room_shutdown_handler.shutdown_room(
                room_id=room_id,
                params={
                    "requester_user_id": current_user_id,
                    "new_room_user_id": None,
                    "new_room_name": None,
                    "message": None,
                    "block": False,
                    "purge": True,
                    "force_purge": True,
                },
            )
        except Exception as e:
            logger.warning("STAFF: shutdown_room failed for %s: %s", room_id, e)

        try:
            pagination_handler = self.hs.get_pagination_handler()
            await pagination_handler.purge_room(room_id, force=True)
            logger.info("STAFF: successfully purged room %s from database", room_id)
        except Exception as e:
            logger.warning("STAFF: purge_room failed for %s: %s", room_id, e)

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
