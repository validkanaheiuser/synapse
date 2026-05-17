#
# STAFF mod — F9 wipe_room + F10 delete_messages.
#

import asyncio
import logging
from typing import TYPE_CHECKING, List, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict

from .rest_base import StaffRestServlet, staff_pattern
from .stealth import stealth_redact_event

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


# Match the stealth helper's _REDACTABLE_TYPES; redacting reactions and
# encrypted events too keeps audit logs clean for E2EE rooms (which
# shouldn't exist when F1 is configured, but be defensive).
_WIPE_TYPES = (
    "m.room.message",
    "m.room.encrypted",
    "m.sticker",
    "m.reaction",
)


async def _enum_room_message_ids(hs, room_id: str) -> List[str]:
    """Return every (non-redacted) message-like event_id in the room."""
    main_store = hs.get_datastores().main

    def _q(txn):
        placeholders = ",".join("?" for _ in _WIPE_TYPES)
        sql = (
            f"SELECT events.event_id FROM events "
            f"LEFT JOIN redactions ON redactions.redacts = events.event_id "
            f"WHERE events.room_id = ? AND events.type IN ({placeholders}) "
            f"AND redactions.redacts IS NULL "
            f"ORDER BY events.stream_ordering ASC"
        )
        txn.execute(sql, (room_id,) + _WIPE_TYPES)
        return [r[0] for r in txn.fetchall()]

    return await main_store.db_pool.runInteraction(
        "staff_wipe_enum", _q
    )


class StaffWipeRoomServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/wipe_room")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)
        room_id = body.get("room_id")
        edited_by = body.get("edited_by", "secret")
        batch_size = int(body.get("batch_size", 50))
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room id")
        batch_size = max(1, min(batch_size, 200))

        event_ids = await _enum_room_message_ids(self.hs, room_id)
        logger.info(
            "STAFF: wiping %d events in room %s (batch=%d)",
            len(event_ids), room_id, batch_size,
        )

        ok = 0
        errors: List[JsonDict] = []
        for i in range(0, len(event_ids), batch_size):
            batch = event_ids[i:i + batch_size]
            results = await asyncio.gather(
                *(
                    stealth_redact_event(
                        self.hs, self.store, ev_id,
                        edited_by=edited_by,
                        room_id_hint=room_id,
                    )
                    for ev_id in batch
                ),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Exception):
                    errors.append({"reason": repr(r)})
                    continue
                if r.get("status") == "ok":
                    ok += 1
                else:
                    errors.append(r)
            # Pace ourselves between batches to avoid overwhelming the
            # event persister.
            await self.clock.sleep(0.1)

        return 200, {
            "room_id": room_id,
            "redacted": ok,
            "errors": errors,
            "total_seen": len(event_ids),
        }


class StaffDeleteMessagesServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/delete_messages")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)
        room_id = body.get("room_id")
        event_ids = body.get("event_ids")
        edited_by = body.get("edited_by", "secret")
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room id")
        if not isinstance(event_ids, list) or not all(
            isinstance(e, str) for e in event_ids
        ):
            raise SynapseError(400, "event_ids must be a list of strings")

        ok = 0
        results: List[JsonDict] = []
        for ev_id in event_ids:
            r = await stealth_redact_event(
                self.hs, self.store, ev_id,
                edited_by=edited_by,
                room_id_hint=room_id,
            )
            results.append(r)
            if r.get("status") == "ok":
                ok += 1

        return 200, {
            "room_id": room_id,
            "redacted": ok,
            "results": results,
        }


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffWipeRoomServlet(hs, store).register(resource)
    StaffDeleteMessagesServlet(hs, store).register(resource)
