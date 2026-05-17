#
# STAFF mod — F12 scheduled messages endpoints.
#

import logging
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .rest_base import StaffRestServlet, staff_pattern
from .scheduler import cancel_scheduled, parse_local_to_utc_ms, schedule_message

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


class StaffScheduleCreateServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/schedule")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)

        room_id = body.get("room_id")
        as_user = body.get("as_user")
        send_at = body.get("send_at")
        message = body.get("message")
        image_mxc = body.get("image_mxc")

        if not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room id")
        if not isinstance(as_user, str) or not UserID.is_valid(as_user):
            raise SynapseError(400, "as_user must be a valid MXID")
        if not isinstance(send_at, str):
            raise SynapseError(400, "send_at must be an ISO 8601 timestamp")
        if message is not None and not isinstance(message, str):
            raise SynapseError(400, "message must be a string")
        if image_mxc is not None and (
            not isinstance(image_mxc, str)
            or not image_mxc.startswith("mxc://")
        ):
            raise SynapseError(400, "image_mxc must be an mxc:// URI")
        if message is None and image_mxc is None:
            raise SynapseError(400,
                "at least one of message / image_mxc is required")

        timezone_name = self.hs.config.staff.staff_timezone
        try:
            send_at_ms = parse_local_to_utc_ms(send_at, timezone_name)
        except Exception as e:
            raise SynapseError(400, f"could not parse send_at: {e!r}")

        # Refuse to schedule into the past by more than a minute.  A small
        # window of latency is OK; the scheduler will run it on next tick.
        now_ms = self.clock.time_msec()
        if send_at_ms < now_ms - 60_000:
            raise SynapseError(
                400,
                f"send_at is in the past (parsed={send_at_ms}, now={now_ms})",
            )

        task_id = await schedule_message(
            self.hs, self.store,
            room_id=room_id,
            as_user=as_user,
            send_at_ms=send_at_ms,
            message=message,
            image_mxc=image_mxc,
        )
        return 200, {
            "task_id": task_id,
            "send_at_ms": send_at_ms,
            "room_id": room_id,
            "as_user": as_user,
        }


class StaffScheduleListServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/schedule")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        rows = await self.store.schedule_list_pending()
        return 200, {"tasks": rows}


class StaffScheduleDeleteServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/schedule/(?P<task_id>[^/]+)")

    async def on_DELETE(self, request, task_id: str) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        removed = await cancel_scheduled(self.hs, self.store, task_id)
        return 200, {"task_id": task_id, "removed": removed}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    # Note: /schedule has both GET and POST registered on the same pattern.
    # JsonResource will dispatch by method.
    StaffScheduleCreateServlet(hs, store).register(resource)
    StaffScheduleListServlet(hs, store).register(resource)
    StaffScheduleDeleteServlet(hs, store).register(resource)
