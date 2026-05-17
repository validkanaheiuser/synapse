#
# STAFF mod — F3 edit endpoints.
#
# POST /_synapse/staff/v1/edit_message
#   {"room_id": "!...", "event_id": "$X", "new_content": {...}}
#   Forges an m.replace edit of $X as the original sender so the edit is
#   indistinguishable from the user editing their own message.  Realtime
#   content update via /sync (Option 2 — badge is visible, but the
#   /relations + /messages filters in core hide any edit history).
#
# GET /_synapse/staff/v1/edit_history/{event_id}
#   Returns the full chain of staff edits (and stealth redactions) for
#   the given event_id from staff_edit_history.
#

import logging
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict

from .forge import send_replace_edit_as
from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


class StaffEditMessageServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/edit_message")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)
        room_id = body.get("room_id")
        event_id = body.get("event_id")
        new_content = body.get("new_content")
        edited_by = body.get("edited_by", "secret")

        if not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room id")
        if not isinstance(event_id, str) or not event_id.startswith("$"):
            raise SynapseError(400, "event_id must be a valid event id")
        if not isinstance(new_content, dict):
            raise SynapseError(400, "new_content must be a JSON object")
        if "msgtype" not in new_content or "body" not in new_content:
            raise SynapseError(
                400, "new_content must include both 'msgtype' and 'body'"
            )

        main_store = self.hs.get_datastores().main
        try:
            original = await main_store.get_event(event_id, allow_none=False)
        except Exception as e:
            raise SynapseError(404, f"event not found: {e!r}")

        if original.room_id != room_id:
            raise SynapseError(400, "event is not in this room")
        if original.type != "m.room.message":
            raise SynapseError(
                400, "only m.room.message events can be edited"
            )

        replace_event = await send_replace_edit_as(
            hs=self.hs,
            sender=original.sender,
            room_id=room_id,
            original_event_id=event_id,
            new_content=new_content,
        )

        await self.store.edit_history_record(
            original_event_id=event_id,
            room_id=room_id,
            sender=original.sender,
            old_content=dict(original.content),
            new_content=new_content,
            replace_event_id=replace_event.event_id,
            redaction_event_id=None,
            edited_by=edited_by,
            kind="edit",
        )

        return 200, {
            "event_id": event_id,
            "replace_event_id": replace_event.event_id,
            "sender": original.sender,
        }


class StaffEditHistoryServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/edit_history/(?P<event_id>[^/]+)")

    async def on_GET(self, request, event_id: str) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        rows = await self.store.edit_history_for(event_id)
        return 200, {"event_id": event_id, "edits": rows}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffEditMessageServlet(hs, store).register(resource)
    StaffEditHistoryServlet(hs, store).register(resource)
