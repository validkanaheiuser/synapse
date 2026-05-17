#
# STAFF mod — F14 create_user + F15 delete_users.
#

import logging
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .forge import fake_requester
from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


class StaffCreateUserServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/create_user")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)
        username = body.get("username")
        password = body.get("password")
        display_name = body.get("display_name")
        admin = bool(body.get("admin", False))

        if not isinstance(username, str) or not username:
            raise SynapseError(400, "username is required")
        if not isinstance(password, str) or not password:
            raise SynapseError(400, "password is required")
        if "@" in username or ":" in username:
            raise SynapseError(
                400,
                "username must be a localpart only (no '@' / ':' / domain)",
            )

        auth_handler = self.hs.get_auth_handler()
        registration_handler = self.hs.get_registration_handler()

        password_hash = await auth_handler.hash(password)
        try:
            user_id = await registration_handler.register_user(
                localpart=username,
                password_hash=password_hash,
                admin=admin,
                by_admin=True,
                default_display_name=display_name,
            )
        except SynapseError:
            raise
        except Exception as e:
            raise SynapseError(500, f"register_user failed: {e!r}")

        # Mint a device + access token so the caller can hand it back to
        # the new user.
        try:
            device_id, access_token, _, _ = (
                await registration_handler.register_device(
                    user_id=user_id,
                    device_id=None,
                    initial_display_name="staff-provisioned",
                )
            )
        except Exception as e:
            logger.warning(
                "STAFF: created %s but failed to mint device/token: %r",
                user_id, e,
            )
            device_id = None
            access_token = None

        return 200, {
            "user_id": user_id,
            "device_id": device_id,
            "access_token": access_token,
        }


class StaffDeleteUsersServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/delete_users")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)
        usernames = body.get("usernames")
        if not isinstance(usernames, list) or not all(
            isinstance(u, str) for u in usernames
        ):
            raise SynapseError(400, "usernames must be a list of MXIDs")

        deactivate_handler = self.hs.get_deactivate_account_handler()
        results = []
        for mxid in usernames:
            if not UserID.is_valid(mxid):
                results.append(
                    {"user_id": mxid, "status": "error",
                     "reason": "invalid MXID"}
                )
                continue
            try:
                requester = fake_requester(self.hs, mxid)
                await deactivate_handler.deactivate_account(
                    mxid, erase_data=True, requester=requester,
                    by_admin=True,
                )
                results.append({"user_id": mxid, "status": "deleted"})
            except Exception as e:
                logger.warning(
                    "STAFF: deactivate %s failed: %r", mxid, e
                )
                results.append(
                    {"user_id": mxid, "status": "error", "reason": str(e)}
                )

        return 200, {"results": results}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffCreateUserServlet(hs, store).register(resource)
    StaffDeleteUsersServlet(hs, store).register(resource)
