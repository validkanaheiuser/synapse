#
# STAFF mod — F9 wipe_room + F10 delete_messages + restore_message (undo).
#

import asyncio
import logging
from typing import TYPE_CHECKING, List, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict

from .forge import send_event_as
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
        await self._require_secret(request)
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
        await self._require_secret(request)
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


class StaffRestoreMessageServlet(StaffRestServlet):
    """Resurrect a previously stealth-redacted message as a NEW m.room.message.

    Body: {
        "room_id":           "!abc:server",
        "original_event_id": "$xyz",
        "marker":            "[restored] " (optional, default; pass "" to omit),
    }

    Looks up the most recent staff_edit_history row for ``original_event_id``
    (which holds a JSON snapshot of the original content as ``old_content``),
    then forges a brand-new ``m.room.message`` in the room as the original
    sender, at the CURRENT time.  This is intentionally NOT an m.replace edit:
    the original event has been redacted server-side and most clients won't
    re-render an edit of a tombstoned event.

    Returns: {"status": "ok", "event_id": "$new", "sender": "@u:s", "marker": "..."}.
    """

    PATTERNS = staff_pattern("/restore_message")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        body = parse_json_object_from_request(request)
        room_id = body.get("room_id")
        original_event_id = body.get("original_event_id")
        # Marker prefix prepended to the restored body so audit logs can
        # distinguish a real send from a restore.  Pass "" to suppress.
        marker = body.get("marker")
        if marker is None:
            marker = "[restored] "
        if not isinstance(marker, str):
            raise SynapseError(400, "marker must be a string if provided")

        if not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room id")
        if not isinstance(original_event_id, str) or not original_event_id.startswith("$"):
            raise SynapseError(400, "original_event_id must be a valid event id")

        history = await self.store.edit_history_for(original_event_id)
        if not history:
            raise SynapseError(404, "no edit_history rows for this event_id")

        # Use the LAST row written for this event_id; that's the row whose
        # `old_content` reflects what was on-screen just before the stealth
        # redact ran.  `edit_history_for` returns rows in ascending order.
        last = history[-1]
        if last.get("room_id") != room_id:
            raise SynapseError(400, "event_id is not in the given room")

        sender = last.get("sender")
        old_content = last.get("old_content") or {}
        if not isinstance(sender, str) or not sender.startswith("@"):
            raise SynapseError(500, "history row has no usable sender")

        # === AGENT I (S19): deactivated-sender fallback ===
        # If the original sender has been deactivated (F15 delete_users
        # path) the forge call below will fail at the auth-check stage —
        # deactivated users cannot send events.  When `staff.system_user`
        # is configured, transparently fall back to forging as that user
        # and prepend an attribution prefix so observers can see the
        # message was re-sent on behalf of the original poster.
        original_sender = sender
        sender_deactivated = False
        try:
            main = self.hs.get_datastores().main
            getter = getattr(main, "get_user_deactivated_status", None)
            if getter is not None:
                sender_deactivated = bool(await getter(sender))
        except Exception:
            logger.debug(
                "STAFF restore_message: cannot check deactivation for %s",
                sender, exc_info=True,
            )

        attribution_prefix = ""
        if sender_deactivated:
            system_user = getattr(
                self.hs.config.staff, "staff_system_user", None,
            )
            if isinstance(system_user, str) and system_user.startswith("@"):
                logger.info(
                    "STAFF restore_message: original sender %s is "
                    "deactivated; falling back to system_user %s",
                    sender, system_user,
                )
                sender = system_user
                attribution_prefix = (
                    f"[restored by staff on behalf of {original_sender}] "
                )
            else:
                logger.warning(
                    "STAFF restore_message: sender %s deactivated and "
                    "staff.system_user not configured; attempting forge "
                    "anyway (likely to fail)",
                    original_sender,
                )
        # === END AGENT I ===

        # Compose the restored content.  Preserve formatted_body / msgtype etc
        # from the snapshot, but optionally prepend `marker` to the body so
        # human reviewers can identify the restore at a glance.
        restored: JsonDict = dict(old_content)
        restored.setdefault("msgtype", "m.text")
        base_body = restored.get("body")
        if not isinstance(base_body, str):
            base_body = ""
        # === AGENT I (S19): when we fell back to the system user, the
        # attribution prefix runs BEFORE the operator-supplied marker so
        # the message body reads "[restored by staff on behalf of <u>] "
        # + "[restored] " + <original body>.  Both prefixes are no-ops
        # if their respective conditions don't apply.
        restored["body"] = (
            f"{attribution_prefix}{marker}{base_body}"
            if (marker or attribution_prefix)
            else base_body
        )
        if isinstance(restored.get("formatted_body"), str) and (
            marker or attribution_prefix
        ):
            # If the original was HTML-formatted, prepend the marker there too.
            restored["formatted_body"] = (
                attribution_prefix + marker + restored["formatted_body"]
            )
        # === END AGENT I ===

        try:
            new_event = await send_event_as(
                hs=self.hs,
                sender=sender,
                room_id=room_id,
                event_type="m.room.message",
                content=restored,
            )
        except Exception as e:
            logger.warning(
                "STAFF restore_message: forge failed for %s in %s: %r",
                original_event_id, room_id, e,
            )
            raise SynapseError(500, f"restore failed: {e!r}")

        # Audit: a restore is a new kind of edit_history row so future
        # readers can see the original event was undeleted.
        try:
            await self.store.edit_history_record(
                original_event_id=original_event_id,
                room_id=room_id,
                sender=sender,
                old_content={},
                new_content=restored,
                replace_event_id=new_event.event_id,
                redaction_event_id=None,
                edited_by=body.get("edited_by", "secret"),
                kind="restore",
            )
        except Exception as e:
            logger.warning("STAFF restore_message: audit log failed: %r", e)

        # === AGENT I (S19): surface the fallback in the response so the
        # STAFF UI can show a "restored on behalf of" badge when the
        # original sender is deactivated.
        response: JsonDict = {
            "status": "ok",
            "event_id": new_event.event_id,
            "sender": sender,
            "room_id": room_id,
            "original_event_id": original_event_id,
            "marker": marker,
        }
        if sender_deactivated and sender != original_sender:
            response["original_sender"] = original_sender
            response["fallback_sender"] = sender
        return 200, response
        # === END AGENT I ===


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffWipeRoomServlet(hs, store).register(resource)
    StaffDeleteMessagesServlet(hs, store).register(resource)
    StaffRestoreMessageServlet(hs, store).register(resource)
