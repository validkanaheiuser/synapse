#
# STAFF mod — stealth-redact (F2) primitive.
#
# Called by F9 (/wipe_room) and F10 (/delete_messages) for every message
# the staff wants to remove.  Two server-side actions per message:
#
#   1. Forge a `m.replace` edit of the message with empty content, sent as
#      the original sender.  Cached clients receive this via /sync and
#      blank out their local copy in realtime.
#
#   2. Forge a `m.room.redaction` of the message, sent as the original
#      sender.  Synapse marks the message as redacted server-side; the
#      sync.py patch then drops the redaction event from non-staff /sync
#      delivery so no "Message deleted" placeholder ever fires on cached
#      clients.  Fresh syncs see the message stripped from the timeline
#      by the visibility filters in /messages / /context / /event.
#
# A log row is written to staff_edit_history with the original content
# snapshot so staff can recover the original text from
# /staff/v1/edit_history/{event_id}.
#

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .forge import send_redaction_as, send_replace_edit_as

if TYPE_CHECKING:
    from synapse.events import EventBase
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


# Content of the empty-edit's m.new_content payload.  We use a single
# whitespace as `body` so element-web and element-android don't treat it
# as a parse error; the body is hidden anyway because the parent's content
# is replaced.
_EMPTY_NEW_CONTENT: Dict[str, Any] = {"msgtype": "m.text", "body": " "}


_REDACTABLE_TYPES = frozenset(
    {
        "m.room.message",
        "m.room.encrypted",
        "m.sticker",
        "m.reaction",
        "m.room.message.feedback",
    }
)


async def stealth_redact_event(
    hs: "HomeServer",
    store: "StaffStore",
    event_id: str,
    edited_by: str,
    room_id_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Empty-edit + redact a single event.  Returns a dict describing
    the outcome (useful for the response of /wipe_room and /delete_messages).
    """
    main_store = hs.get_datastores().main
    try:
        original: "EventBase" = await main_store.get_event(event_id, allow_none=False)
    except Exception as e:
        logger.warning("STAFF stealth-redact: get_event(%s) failed: %r",
                       event_id, e)
        return {"event_id": event_id, "status": "error",
                "reason": f"event not found: {e!r}"}

    if room_id_hint is not None and original.room_id != room_id_hint:
        return {"event_id": event_id, "status": "error",
                "reason": "event is not in the given room"}

    if original.type not in _REDACTABLE_TYPES:
        return {"event_id": event_id, "status": "skipped",
                "reason": f"type {original.type!r} is not redactable"}

    if original.internal_metadata.is_redacted():
        return {"event_id": event_id, "status": "skipped",
                "reason": "already redacted"}

    sender = original.sender
    room_id = original.room_id

    replace_ev_id: Optional[str] = None
    redact_ev_id: Optional[str] = None
    errors: List[str] = []

    # Step 1: empty-edit (only if it's a message; reactions etc don't edit).
    if original.type == "m.room.message":
        try:
            replace_event = await send_replace_edit_as(
                hs=hs,
                sender=sender,
                room_id=room_id,
                original_event_id=event_id,
                new_content=_EMPTY_NEW_CONTENT,
            )
            replace_ev_id = replace_event.event_id
        except Exception as e:
            errors.append(f"empty-edit failed: {e!r}")

    # Step 2: redaction.
    try:
        redact_event = await send_redaction_as(
            hs=hs,
            sender=sender,
            room_id=room_id,
            redacts_event_id=event_id,
        )
        redact_ev_id = redact_event.event_id
    except Exception as e:
        errors.append(f"redaction failed: {e!r}")

    # Step 3: audit log.
    try:
        await store.edit_history_record(
            original_event_id=event_id,
            room_id=room_id,
            sender=sender,
            old_content=dict(original.content),
            new_content=_EMPTY_NEW_CONTENT,
            replace_event_id=replace_ev_id,
            redaction_event_id=redact_ev_id,
            edited_by=edited_by,
            kind="stealth_redact",
        )
    except Exception as e:
        errors.append(f"audit failed: {e!r}")

    result = {
        "event_id": event_id,
        "status": "ok" if not errors else "partial",
        "replace_event_id": replace_ev_id,
        "redaction_event_id": redact_ev_id,
    }
    if errors:
        result["errors"] = errors
    return result
