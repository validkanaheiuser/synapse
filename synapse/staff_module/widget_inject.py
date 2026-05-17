#
# STAFF mod — F16 widget auto-injection on staff DMs.
#
# Hooked via register_third_party_rules_callbacks(on_new_event=...).
# Fires after every persisted event.  We watch for m.room.member joins
# where the joiner is a staff user and the room is a 2-person DM, then
# inject the staff's general_widget + staff_custom_widget definitions as
# state events forged from the staff user.
#
# Widget state events are auto-hidden by F8 (HIDDEN_STATE_TYPES includes
# `im.vector.modular.widgets` and `m.widget`) so "X added a widget"
# system messages never appear.
#

import logging
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Set

from .forge import send_event_as
from .widget_payload import build_widget_state_content

if TYPE_CHECKING:
    from synapse.events import EventBase
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


# We use the type element-web supports (`im.vector.modular.widgets`).
# element-android and element-ios both accept this AND the newer m.widget.
WIDGET_EVENT_TYPE = "im.vector.modular.widgets"


class WidgetInjector:
    def __init__(self, hs: "HomeServer", store: "StaffStore"):
        self._hs = hs
        self._store = store

    async def on_new_event(self, event: "EventBase", state: Any = None) -> None:
        # Cheap exits first.
        if event.type != "m.room.member":
            return
        if event.content.get("membership") != "join":
            return
        target = event.state_key
        if not isinstance(target, str):
            return
        if not self._store.is_staff_user(target):
            return

        room_id = event.room_id
        if not await self._is_two_person_dm(room_id, target):
            return

        logger.info(
            "STAFF: detected staff %s joining DM %s — injecting widgets",
            target, room_id,
        )
        await self._inject_widgets_for_staff(room_id, target)

    async def _is_two_person_dm(self, room_id: str, staff_user: str) -> bool:
        main = self._hs.get_datastores().main
        try:
            members: Set[str] = await main.get_users_in_room(room_id)
        except Exception:
            return False
        # Strictly two members; one is the staff.  In a closed (federation
        # off) deployment, that's the canonical "DM with staff" signal.
        if len(members) != 2 or staff_user not in members:
            return False
        # Belt-and-braces: also accept the room if the m.direct account
        # data of either party lists the room id (Element-set flag).
        return True

    async def _inject_widgets_for_staff(
        self, room_id: str, staff_user: str
    ) -> None:
        # Combine general_widgets (all staff share) + staff_custom_widgets
        # owned by THIS staff.
        general = await self._store.widget_list(widget_type="general_widget")
        custom = await self._store.widget_list(
            widget_type="staff_custom_widget", owner_user_id=staff_user,
        )
        widgets = list(general) + list(custom)

        for w in widgets:
            existing = await self._store.widget_instance_get(
                w["widget_id"], room_id
            )
            if existing is not None:
                continue  # already injected into this room
            await self._inject_one(room_id, staff_user, w)

    async def _inject_one(
        self, room_id: str, sender: str, widget: dict
    ) -> None:
        widget_id = widget["widget_id"]
        # The widget's state_key in the room is a per-room instance id.
        # We use the widget_id itself as the state_key for stability
        # across rooms — the same widget gets the same key everywhere.
        state_key = widget_id
        content = build_widget_state_content(widget, sender)
        try:
            ev = await send_event_as(
                self._hs,
                sender=sender,
                room_id=room_id,
                event_type=WIDGET_EVENT_TYPE,
                content=content,
                state_key=state_key,
            )
            await self._store.widget_instance_record(
                widget_id=widget_id,
                room_id=room_id,
                injected_by=sender,
                last_state_event_id=ev.event_id,
            )
        except Exception:
            logger.exception(
                "STAFF: failed to inject widget %s into %s",
                widget_id, room_id,
            )
