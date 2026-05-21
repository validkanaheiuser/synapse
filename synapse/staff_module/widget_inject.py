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

        room_id = event.room_id

        # Resolve which staff user (if any) we should inject widgets for.
        # Two cases:
        #
        # (A) Staff joined.  Classic F16 case — the joiner IS the staff
        #     member.  Inject the joiner's widgets.  Works for the
        #     "user-invited-staff" + auto-accept-invite flow because the
        #     auto-accept module materialises the join as a normal
        #     membership event that fans through here.
        #
        # (B) Non-staff joined a room where staff is already present.
        #     This catches the inverse "staff-invited-user" flow: staff
        #     created the DM, sent an invite, then the invitee joined.
        #     At staff's own join time (Case A's trigger) the room only
        #     had one member, so the DM check failed.  Now that the
        #     second member has arrived we can finally inject.  We
        #     locate the staff member by scanning the current member
        #     list — single short list in a DM, cheap.
        #
        # Either case is required to land on the same `_inject_widgets_
        # for_staff(room_id, staff_user)` path so the widget-instance
        # dedupe (`widget_instance_get`) prevents double-injection if
        # the trigger somehow fires twice.
        staff_user: Optional[str] = None
        if self._store.is_staff_user(target):
            if not await self._is_two_person_dm(room_id, target):
                return
            staff_user = target
        else:
            # Non-staff joiner — look for staff in the room.
            staff_in_room = await self._find_staff_in_room(room_id)
            if staff_in_room is None:
                return
            if not await self._is_two_person_dm(room_id, staff_in_room):
                return
            staff_user = staff_in_room

        logger.info(
            "STAFF: 2-person DM %s now contains staff %s "
            "(triggered by %s joining) — injecting widgets",
            room_id, staff_user, target,
        )
        # === AGENT I (S10): never block event persistence on widget injection.
        # `on_new_event` runs on the hot path that follows `EventCreationHandler
        # ._persist_events`; awaiting our forge calls here can stall every
        # subsequent event in the room.  Hand the work off to a tracked
        # background process so the event-persister returns immediately.
        # `hs.run_as_background_process` is the high-level wrapper around
        # `synapse.metrics.background_process_metrics.run_as_background_process`
        # which handles SERVER_NAME labelling for us.
        try:
            self._hs.run_as_background_process(
                "staff_widget_inject",
                self._inject_widgets_for_staff,
                room_id,
                staff_user,
            )
        except Exception:
            logger.exception(
                "STAFF: failed to schedule widget injection bg process"
            )
        # === END AGENT I ===

    async def _find_staff_in_room(self, room_id: str) -> Optional[str]:
        """Return the MXID of any staff user currently in `room_id`, or
        None.  Used by Case B (non-staff joiner) to figure out whose
        widgets to inject.  In a 2-person DM there's at most one staff
        member; we return the first one found.  Cheap: pulls the small
        member list and does a single `is_staff_user` check per member.
        """
        main = self._hs.get_datastores().main
        try:
            members: Set[str] = await main.get_users_in_room(room_id)
        except Exception:
            return None
        for mxid in members:
            try:
                if self._store.is_staff_user(mxid):
                    return mxid
            except Exception:
                continue
        return None

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
        # === AGENT P ===
        # Group-scoped: general_widgets only get injected if THIS staff
        # is in a group that contains them.  Custom widgets remain
        # per-owner.  `widget_ids_for_user_via_groups` is a single
        # round-trip SELECT joining the membership + widget join tables.
        #
        # Performance note: with at most a few dozen general widgets per
        # group and a few groups per staff, the per-widget `widget_get`
        # calls below are fine.  If first-DM injection latency becomes a
        # problem we can replace the loop with a single SELECT-IN against
        # `staff_widget_definitions` filtering by widget_type — flagged
        # here as a future optimisation, not a current bug.
        widget_ids_via_groups = (
            await self._store.widget_ids_for_user_via_groups(staff_user)
        )
        general: List[dict] = []
        if widget_ids_via_groups:
            for wid in widget_ids_via_groups:
                w = await self._store.widget_get(wid)
                if w is not None and w["widget_type"] == "general_widget":
                    general.append(w)
        else:
            # Disambiguate "in groups with no widgets" vs "in no groups
            # at all".  Only the latter triggers the fallback — if the
            # operator deliberately put the staff in an empty group we
            # respect their intent and skip general widgets.
            user_groups = await self._store.groups_for_user(staff_user)
            if not user_groups:
                # Fresh staff with no group memberships yet — inject every
                # general_widget so the operator doesn't have to set up the
                # group/membership plumbing before widgets start appearing
                # in DMs.  Small/single-staff deployments work out of the
                # box; multi-staff operators that want partitioning just
                # add the staff to a group (even an empty one) to opt out
                # of this fallback.
                general = list(
                    await self._store.widget_list(widget_type="general_widget")
                )
                logger.info(
                    "STAFF: staff %s has no group memberships — injecting "
                    "all %d general_widgets as default behavior",
                    staff_user, len(general),
                )
        custom = await self._store.widget_list(
            widget_type="staff_custom_widget", owner_user_id=staff_user,
        )
        widgets = list(general) + list(custom)
        # === END AGENT P ===

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
