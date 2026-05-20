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
        await self._require_secret(request)
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
        await self._require_secret(request)
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


class StaffForceLogoutServlet(StaffRestServlet):
    """Invalidate access tokens + devices for the given user(s).

    Body accepts:
        * ``usernames`` (required): list of MXIDs.
        * ``device_ids`` (optional): per-MXID map ``{mxid: [device_id, ...]}``
          OR a single flat list applied to every MXID.

    Per-device targeting:
        ``device_handler.delete_devices`` already invokes
        ``auth_handler.delete_access_tokens_for_devices`` internally
        (see ``synapse/handlers/device.py``), so deleting a device fully
        invalidates the tokens bound to it.  We therefore prefer the
        per-device path and only fall back to the user-wide token wipe
        when no device list is supplied.

    Used by the room-delete cascade on the staff client: after kicking
    & leaving, the staff forces a logout so the user's local cache of
    the room is wiped on next start (Element clears IndexedDB on
    missing-session response).
    """

    PATTERNS = staff_pattern("/force_logout")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        body = parse_json_object_from_request(request)
        usernames = body.get("usernames")
        if not isinstance(usernames, list) or not all(
            isinstance(u, str) for u in usernames
        ):
            raise SynapseError(400, "usernames must be a list of MXIDs")

        # Normalise the optional device_ids parameter into a
        # {mxid: list[str] | None} mapping.  ``None`` means "all devices
        # for this user" (legacy behaviour); a list (possibly empty)
        # means "only these device ids".
        raw_device_ids = body.get("device_ids")
        per_user_devices: dict[str, list[str] | None] = {}
        if raw_device_ids is None:
            per_user_devices = {mxid: None for mxid in usernames}
        elif isinstance(raw_device_ids, list):
            if not all(isinstance(d, str) for d in raw_device_ids):
                raise SynapseError(
                    400, "device_ids list must contain strings"
                )
            for mxid in usernames:
                per_user_devices[mxid] = list(raw_device_ids)
        elif isinstance(raw_device_ids, dict):
            for mxid in usernames:
                ids = raw_device_ids.get(mxid)
                if ids is None:
                    per_user_devices[mxid] = None
                    continue
                if not isinstance(ids, list) or not all(
                    isinstance(d, str) for d in ids
                ):
                    raise SynapseError(
                        400,
                        f"device_ids[{mxid!r}] must be a list of strings",
                    )
                per_user_devices[mxid] = list(ids)
        else:
            raise SynapseError(
                400,
                "device_ids must be a list, an object keyed by MXID, or absent",
            )

        auth_handler = self.hs.get_auth_handler()
        device_handler = self.hs.get_device_handler()
        store = self.hs.get_datastores().main

        results = []
        for mxid in usernames:
            if not UserID.is_valid(mxid):
                results.append({"user_id": mxid, "status": "error",
                                "reason": "invalid MXID"})
                continue

            # === AGENT H === S8 self-guard.
            # Refuse to forcibly log out anyone who is on the staff
            # allowlist.  Without this guard a staff member with the
            # admin secret could accidentally — or maliciously — lock
            # every other staff member out of the box.  We still record
            # them in the response so callers know the request was
            # intentionally skipped (not silently dropped).
            if self.store.is_staff_user(mxid):
                results.append({
                    "user_id": mxid,
                    "status": "skipped_staff",
                    "reason": "user is in staff allowlist",
                })
                continue
            # === END AGENT H ===

            requested_ids = per_user_devices.get(mxid)
            try:
                if requested_ids is None:
                    # Full logout — preserve legacy behaviour.
                    await auth_handler.delete_access_tokens_for_user(mxid)
                    devices_map = await store.get_devices_by_user(mxid)
                    all_ids = list(devices_map.keys())
                    if all_ids:
                        await device_handler.delete_devices(mxid, all_ids)
                    results.append({
                        "user_id": mxid,
                        "status": "logged_out",
                        "devices_removed": len(all_ids),
                        "scope": "all",
                    })
                else:
                    # Per-device logout.  delete_devices internally
                    # delegates to delete_access_tokens_for_devices,
                    # so the device's tokens go with it.
                    if not requested_ids:
                        # Caller explicitly passed an empty list — nothing
                        # to do, but report it cleanly.
                        results.append({
                            "user_id": mxid,
                            "status": "logged_out",
                            "devices_removed": 0,
                            "scope": "devices",
                        })
                        continue

                    # Validate that the devices belong to this user;
                    # silently skip unknown IDs to be idempotent.
                    devices_map = await store.get_devices_by_user(mxid)
                    valid_ids = [
                        d for d in requested_ids if d in devices_map
                    ]
                    if valid_ids:
                        await device_handler.delete_devices(mxid, valid_ids)
                    results.append({
                        "user_id": mxid,
                        "status": "logged_out",
                        "devices_removed": len(valid_ids),
                        "devices_unknown": len(requested_ids) - len(valid_ids),
                        "scope": "devices",
                    })
            except Exception as e:
                logger.warning(
                    "STAFF: force_logout %s failed: %r", mxid, e
                )
                results.append(
                    {"user_id": mxid, "status": "error", "reason": str(e)}
                )

        # === AGENT H === S8: surface the skipped-staff MXIDs at the
        # top level too, so callers that don't iterate `results` (e.g.
        # the room-delete cascade UI that only cares about totals) can
        # still tell whether anyone was spared.
        skipped_staff = [
            r["user_id"] for r in results
            if r.get("status") == "skipped_staff"
        ]
        return 200, {"results": results, "skipped_staff": skipped_staff}
        # === END AGENT H ===


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffCreateUserServlet(hs, store).register(resource)
    StaffDeleteUsersServlet(hs, store).register(resource)
    StaffForceLogoutServlet(hs, store).register(resource)
