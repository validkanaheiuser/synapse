#
# STAFF mod — Auto-reply message handler
#
# Automatically replies to any incoming messages sent to staff accounts
# when auto-reply is enabled in panel.
#

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

from .forge import send_event_as

if TYPE_CHECKING:
    from synapse.events import EventBase
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


class AutoReplyManager:
    """Manages the auto-reply feature for staff accounts.

    When enabled, whenever a non-staff user sends a message to a room
    containing a staff account, that staff account automatically sends
    the configured reply message back into the room.
    """

    def __init__(self, hs: "HomeServer", store: "StaffStore") -> None:
        self._hs = hs
        self._store = store
        self._enabled: bool = False
        self._message: str = ""
        self._loaded: bool = False
        # Deduplication cache for handled event IDs to ensure idempotency
        self._seen_events: Set[str] = set()

    async def ensure_loaded(self) -> None:
        """Load stored auto-reply configuration from the database on startup."""
        try:
            val = await self._store.settings_get("auto_reply")
            if val is not None:
                if isinstance(val, dict):
                    self._enabled = bool(val.get("enabled", False))
                    self._message = str(val.get("message", "") or "")
                elif isinstance(val, str):
                    try:
                        parsed = json.loads(val)
                        if isinstance(parsed, dict):
                            self._enabled = bool(parsed.get("enabled", False))
                            self._message = str(parsed.get("message", "") or "")
                    except Exception:
                        pass
            else:
                # Seed default auto_reply config into the store so it exists on startup
                await self._store.settings_upsert(
                    "auto_reply", {"enabled": False, "message": ""}
                )
            self._loaded = True
            logger.info(
                "STAFF auto_reply: loaded config (enabled=%s, msg_len=%d)",
                self._enabled,
                len(self._message),
            )
        except Exception:
            logger.exception("STAFF auto_reply: failed to load config from store")

    def is_loaded(self) -> bool:
        return self._loaded

    def get_config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "message": self._message,
        }

    def update_config(self, enabled: bool, message: str) -> None:
        self._enabled = bool(enabled)
        self._message = str(message or "")
        logger.info(
            "STAFF auto_reply: config updated (enabled=%s, msg_len=%d)",
            self._enabled,
            len(self._message),
        )

    async def on_new_event(self, event: "EventBase", state: Any = None) -> None:
        """Hook called on every newly persisted event in Synapse."""
        # Fast exit if disabled or empty message
        if not self._enabled or not self._message:
            return

        # Only process regular messages
        if event.type != "m.room.message":
            return

        # Skip state events
        if getattr(event, "state_key", None) is not None:
            return

        content = event.content or {}

        # Skip edits (m.replace)
        rel = content.get("m.relates_to") or {}
        if rel.get("rel_type") == "m.replace":
            return

        # Skip events already marked as auto_reply to prevent any recursion
        if content.get("auto_reply"):
            return

        sender = getattr(event, "sender", None)
        if not sender:
            return

        # Never auto-reply if the sender is a staff user.
        # This prevents self-reply and infinite ping-pong loops between staff members.
        if self._store.is_staff_user(sender):
            return

        # Deduplicate events
        event_id = getattr(event, "event_id", None)
        if event_id:
            if event_id in self._seen_events:
                return
            self._seen_events.add(event_id)
            if len(self._seen_events) > 3000:
                self._seen_events.clear()

        # Schedule background handler so event persistence is not blocked
        try:
            self._hs.run_as_background_process(
                "staff_auto_reply",
                self._handle_auto_reply,
                event.room_id,
                event.event_id,
                sender,
            )
        except Exception:
            logger.exception("STAFF auto_reply: failed to schedule background handler")

    async def _handle_auto_reply(
        self, room_id: str, trigger_event_id: str, sender: str
    ) -> None:
        """Background process that resolves room staff and sends the reply."""
        if not self._enabled or not self._message:
            return

        main_store = self._hs.get_datastores().main
        try:
            members = await main_store.get_users_in_room(room_id)
        except Exception:
            logger.exception("STAFF auto_reply: failed to fetch members for room %s", room_id)
            return

        # Find staff recipients joined in the room
        staff_recipients = [
            u for u in members
            if self._store.is_staff_user(u) and u != sender
        ]
        if not staff_recipients:
            return

        # In direct messages with staff, there is 1 staff user.
        # If there are multiple staff members in a room, we reply as the first staff user
        # to avoid flooding the user with duplicate identical auto-replies.
        staff_user = staff_recipients[0]

        logger.info(
            "STAFF auto_reply: replying as %s in room %s (trigger event=%s from=%s)",
            staff_user,
            room_id,
            trigger_event_id,
            sender,
        )

        try:
            await send_event_as(
                self._hs,
                sender=staff_user,
                room_id=room_id,
                event_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": self._message,
                    "auto_reply": True,
                },
            )
        except Exception:
            logger.exception(
                "STAFF auto_reply: failed to send auto-reply as %s in %s",
                staff_user,
                room_id,
            )
