#
# STAFF mod — F12 scheduled messages endpoints.
#

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .rest_base import StaffRestServlet, staff_pattern
from .scheduler import (
    cancel_scheduled,
    parse_local_to_utc_ms,
    schedule_message,
    update_scheduled,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


def _validate_payload(
    body: Dict[str, Any],
    *,
    require_room_user: bool,
    require_send_at: bool,
) -> Dict[str, Any]:
    """Shared validator for POST + PATCH bodies.

    Returns a dict of normalised values containing only the keys that
    were actually present in `body`.  `clear_message` / `clear_image`
    flags signal "caller passed an explicit JSON `null`".
    """
    out: Dict[str, Any] = {}

    if "room_id" in body or require_room_user:
        room_id = body.get("room_id")
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room id")
        out["room_id"] = room_id
    if "as_user" in body or require_room_user:
        as_user = body.get("as_user")
        if not isinstance(as_user, str) or not UserID.is_valid(as_user):
            raise SynapseError(400, "as_user must be a valid MXID")
        out["as_user"] = as_user

    if "send_at" in body:
        send_at = body.get("send_at")
        if not isinstance(send_at, str):
            raise SynapseError(400, "send_at must be an ISO 8601 timestamp")
        out["send_at"] = send_at
    elif require_send_at:
        raise SynapseError(400, "send_at is required")

    if "message" in body:
        message = body["message"]
        if message is not None and not isinstance(message, str):
            raise SynapseError(400, "message must be a string or null")
        out["message"] = message
        out["clear_message"] = message is None

    if "image_mxc" in body:
        image_mxc = body["image_mxc"]
        if image_mxc is not None and (
            not isinstance(image_mxc, str)
            or not image_mxc.startswith("mxc://")
        ):
            raise SynapseError(400, "image_mxc must be an mxc:// URI or null")
        out["image_mxc"] = image_mxc
        out["clear_image"] = image_mxc is None

    if "image_body" in body:
        image_body = body["image_body"]
        if image_body is not None and not isinstance(image_body, str):
            raise SynapseError(400, "image_body must be a string or null")
        # Trim and cap to a reasonable size — body shows in fallback
        # clients & screen readers, so silly-long values aren't useful.
        if isinstance(image_body, str):
            image_body = image_body.strip()
            if len(image_body) > 2048:
                image_body = image_body[:2048]
        out["image_body"] = image_body

    return out


class StaffScheduleCreateServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/schedule")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        body = parse_json_object_from_request(request)

        parsed = _validate_payload(
            body, require_room_user=True, require_send_at=True,
        )

        if parsed.get("message") is None and parsed.get("image_mxc") is None:
            raise SynapseError(
                400,
                "at least one of message / image_mxc is required",
            )

        timezone_name = self.hs.config.staff.staff_timezone
        try:
            send_at_ms = parse_local_to_utc_ms(parsed["send_at"], timezone_name)
        except Exception as e:
            raise SynapseError(400, f"could not parse send_at: {e!r}")

        # Refuse to schedule before "now" in Vietnam time.  We allow a 60s
        # grace window so a slow round-trip from the staff client doesn't
        # bounce a legitimate "schedule in 0–1 min" request.  Both sides of
        # the comparison are UTC epoch milliseconds; the inequality is
        # timezone-agnostic, but the error message names Vietnam time
        # because that is the canonical reference frame for staff.
        now_ms = self.clock.time_msec()
        if send_at_ms < now_ms - 60_000:
            raise SynapseError(
                400,
                "send_at is before the current Vietnam (ICT) time. "
                "Pick a moment in the future.",
            )

        task_id = await schedule_message(
            self.hs, self.store,
            room_id=parsed["room_id"],
            as_user=parsed["as_user"],
            send_at_ms=send_at_ms,
            message=parsed.get("message"),
            image_mxc=parsed.get("image_mxc"),
            image_body=parsed.get("image_body"),
        )
        return 200, {
            "task_id": task_id,
            "send_at_ms": send_at_ms,
            "room_id": parsed["room_id"],
            "as_user": parsed["as_user"],
        }


class StaffScheduleListServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/schedule")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        # Use the "_full" variant when available so callers see the
        # `image_body` column.  Falls back to the legacy list helper if
        # the schema delta has not been applied yet.
        lister = getattr(self.store, "schedule_list_pending_full", None)
        if lister is not None:
            rows = await lister()
        else:
            rows = await self.store.schedule_list_pending()
        return 200, {"tasks": rows}


class StaffScheduleDeleteServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/schedule/(?P<task_id>[^/]+)")

    async def on_DELETE(self, request, task_id: str) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        removed = await cancel_scheduled(self.hs, self.store, task_id)
        return 200, {"task_id": task_id, "removed": removed}

    async def on_PATCH(
        self, request, task_id: str,
    ) -> Tuple[int, JsonDict]:
        """Edit an existing pending scheduled message.

        Accepts the same body shape as POST (sans task_id).  Any field
        omitted from the JSON body is left unchanged on the row; passing
        `null` for `message` / `image_mxc` explicitly clears that field.
        """
        await self._require_secret(request)
        body = parse_json_object_from_request(request)

        parsed = _validate_payload(
            body, require_room_user=False, require_send_at=False,
        )

        send_at_ms: Optional[int] = None
        if "send_at" in parsed:
            timezone_name = self.hs.config.staff.staff_timezone
            try:
                send_at_ms = parse_local_to_utc_ms(
                    parsed["send_at"], timezone_name,
                )
            except Exception as e:
                raise SynapseError(400, f"could not parse send_at: {e!r}")
            now_ms = self.clock.time_msec()
            if send_at_ms < now_ms - 60_000:
                raise SynapseError(
                    400,
                    "send_at is before the current Vietnam (ICT) time. "
                    "Pick a moment in the future.",
                )

        # Disallow editing room_id / as_user — those would semantically
        # require deleting & recreating the task.  We accept them in the
        # body only because the validator is shared, but reject explicit
        # changes here.
        if "room_id" in body or "as_user" in body:
            raise SynapseError(
                400,
                "room_id and as_user cannot be changed; cancel and "
                "re-create the schedule",
            )

        updated = await update_scheduled(
            self.hs,
            self.store,
            task_id,
            send_at_ms=send_at_ms,
            message=parsed.get("message")
                if not parsed.get("clear_message") else None,
            image_mxc=parsed.get("image_mxc")
                if not parsed.get("clear_image") else None,
            image_body=parsed.get("image_body"),
            clear_message=bool(parsed.get("clear_message")),
            clear_image=bool(parsed.get("clear_image")),
        )
        if updated is None:
            raise SynapseError(404, f"unknown task_id {task_id}")
        return 200, {"task": updated}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    # Note: /schedule has both GET and POST registered on the same pattern.
    # JsonResource will dispatch by method.  /schedule/{task_id} now also
    # serves PATCH alongside DELETE.
    StaffScheduleCreateServlet(hs, store).register(resource)
    StaffScheduleListServlet(hs, store).register(resource)
    delete_servlet = StaffScheduleDeleteServlet(hs, store)
    delete_servlet.register(resource)
    # `RestServlet.register` only iterates the GET / PUT / POST / DELETE
    # method names; PATCH is not one of them, so wire it up explicitly.
    resource.register_paths(
        "PATCH",
        delete_servlet.PATTERNS,
        delete_servlet.on_PATCH,
        delete_servlet.__class__.__name__,
    )
