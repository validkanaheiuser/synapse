#
# STAFF mod — F1 enforcement: refuse every m.room.encryption state event.
#
# The homeserver config option
#   encryption_enabled_by_default_for_room_type: "off"
# only suppresses server-side AUTO-injection of m.room.encryption when a
# fresh room is created without explicit initial_state.  It does NOT stop
# a client (or another module) from sending m.room.encryption either as
# part of /createRoom's `initial_state` or as a later `/state/m.room.encryption`
# PUT.  That gap means a non-staff client (or a buggy staff client) can
# still flip encryption on for any room kind — DM, group DM, public room,
# private room — and the rest of the staff stack (forge, audit, restore,
# stealth-redact, widget inject) silently breaks once it does.
#
# This module closes the gap by registering a `check_event_allowed`
# third-party rule.  Any attempt to persist a `m.room.encryption` state
# event is rejected outright, regardless of the sender's power level or
# the room version.  The check is cheap (one `event.type` comparison) and
# fires before persistence, so encryption never even gets a chance to be
# written to the room state.
#

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from synapse.events import EventBase

logger = logging.getLogger(__name__)


ENCRYPTION_EVENT_TYPE = "m.room.encryption"


class EncryptionBlocker:
    """Stateless gate that vetoes every `m.room.encryption` event and strips it
    from `initial_state` during room creation.

    Registered via `register_third_party_rules_callbacks(check_event_allowed=..., on_create_room=...)`
    in `staff_module/__init__.py`.  The callback contract returns
    `(allowed: bool, replacement_content: dict | None)`; we always return
    `(False, None)` for encryption events and `(True, None)` for anything
    else so the rule is purely additive.

    During room creation (`on_create_room`), if a Matrix client (such as Element)
    includes `m.room.encryption` in `initial_state`, it is stripped out so the room
    is created unencrypted without failing with a 403 error.
    """

    async def on_create_room(
        self,
        requester: Any,
        config: dict,
        is_requester_admin: bool = False,
    ) -> None:
        """Strip any m.room.encryption from initial_state so rooms are created unencrypted."""
        raw_initial_state = config.get("initial_state")
        if isinstance(raw_initial_state, list):
            stripped = [
                s
                for s in raw_initial_state
                if not (isinstance(s, dict) and s.get("type") == ENCRYPTION_EVENT_TYPE)
            ]
            if len(stripped) != len(raw_initial_state):
                logger.info(
                    "STAFF: stripped %s from room creation initial_state for requester %s",
                    ENCRYPTION_EVENT_TYPE,
                    getattr(requester, "user", requester),
                )
                config["initial_state"] = stripped

    async def check_event_allowed(
        self,
        event: "EventBase",
        state: Any = None,
    ) -> tuple[bool, dict | None]:
        if event.type != ENCRYPTION_EVENT_TYPE:
            return True, None
        # Log at INFO so operators can see the rejection in their logs
        # without it being so noisy that it competes with real events.
        # The room_id + sender are sufficient to identify the culprit
        # client; we don't log content because algorithm/secret fields
        # aren't useful for debugging and could be PII-adjacent.
        logger.info(
            "STAFF: rejected %s state event in room %s from %s — "
            "encryption is force-disabled by the staff module",
            event.type, event.room_id, event.sender,
        )
        return False, None
