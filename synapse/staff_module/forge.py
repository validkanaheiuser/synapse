#
# STAFF mod — event forging
#
# Federation is disabled, so every event sender is a local user.  We forge
# a Requester with `authenticated_entity = hs.hostname` (the documented
# "server puppeting the user" pattern from synapse/types/__init__.py:240)
# and pass it to the event creation handler.  Synapse treats this as a
# server-internal action and skips access-token validation, but still
# enforces room-level auth (so e.g. redactions must satisfy the redact PL).
#

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from synapse.types import UserID, create_requester

if TYPE_CHECKING:
    from synapse.events import EventBase
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def fake_requester(hs: "HomeServer", user_id: str):
    return create_requester(
        user_id=UserID.from_string(user_id),
        authenticated_entity=hs.hostname,
    )


async def send_event_as(
    hs: "HomeServer",
    sender: str,
    room_id: str,
    event_type: str,
    content: Dict[str, Any],
    state_key: Optional[str] = None,
    ratelimit: bool = False,
) -> "EventBase":
    """Forge an event whose `sender` is the given local user."""
    requester = fake_requester(hs, sender)
    event_dict: Dict[str, Any] = {
        "type": event_type,
        "room_id": room_id,
        "sender": sender,
        "content": content,
    }
    if state_key is not None:
        event_dict["state_key"] = state_key
    handler = hs.get_event_creation_handler()
    event, _ = await handler.create_and_send_nonmember_event(
        requester,
        event_dict,
        ratelimit=ratelimit,
    )
    return event


async def send_redaction_as(
    hs: "HomeServer",
    sender: str,
    room_id: str,
    redacts_event_id: str,
    reason: Optional[str] = None,
    ratelimit: bool = False,
) -> "EventBase":
    requester = fake_requester(hs, sender)
    handler = hs.get_event_creation_handler()
    content: Dict[str, Any] = {}
    if reason is not None:
        content["reason"] = reason
    event_dict = {
        "type": "m.room.redaction",
        "room_id": room_id,
        "sender": sender,
        "redacts": redacts_event_id,
        "content": content,
    }
    event, _ = await handler.create_and_send_nonmember_event(
        requester,
        event_dict,
        ratelimit=ratelimit,
    )
    return event


async def send_replace_edit_as(
    hs: "HomeServer",
    sender: str,
    room_id: str,
    original_event_id: str,
    new_content: Dict[str, Any],
    ratelimit: bool = False,
) -> "EventBase":
    """Send a standard Matrix m.replace edit.  Used by F3 (staff edit) and
    F2 (empty-edit for stealth redact).
    """
    # The outer content carries a fallback body for clients that don't process
    # bundled aggregations.  Convention from MSC2676.
    msgtype = new_content.get("msgtype", "m.text")
    fallback_body = new_content.get("body", "")
    content: Dict[str, Any] = {
        "msgtype": msgtype,
        "body": f"* {fallback_body}" if fallback_body else "*",
        "m.new_content": dict(new_content),
        "m.relates_to": {
            "rel_type": "m.replace",
            "event_id": original_event_id,
        },
    }
    return await send_event_as(
        hs,
        sender=sender,
        room_id=room_id,
        event_type="m.room.message",
        content=content,
        ratelimit=ratelimit,
    )
