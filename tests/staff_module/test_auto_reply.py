#
# Tests for staff auto-reply functionality
# (`synapse.staff_module.auto_reply` and `synapse.staff_module.rest_autoreply`).
#

from http import HTTPStatus
from typing import Iterable

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.staff_module.conftest import (
    STAFF_SECRET,
    STAFF_SECRET_HEADER,
    StaffHomeserverTestCase,
)

AUTOREPLY_PATH = "/_synapse/staff/v1/autoreply"


def _secret_headers() -> Iterable[tuple[bytes, bytes]]:
    return [(STAFF_SECRET_HEADER, STAFF_SECRET.encode("ascii"))]


class StaffAutoReplyTest(StaffHomeserverTestCase):
    """End-to-end tests for auto-reply REST API and message event auto-response."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()
        self.staff_mxid = self.register_user("replystaff", "pass")
        self.staff_tok = self.login("replystaff", "pass")
        self.peer_mxid = self.register_user("replyuser", "pass")
        self.peer_tok = self.login("replyuser", "pass")
        self.add_staff_user(self.staff_mxid)

    def _set_autoreply(self, enabled: bool, message: str) -> None:
        channel = self.make_request(
            "POST",
            AUTOREPLY_PATH,
            content={"enabled": enabled, "message": message},
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

    def _get_autoreply(self) -> dict:
        channel = self.make_request(
            "GET",
            AUTOREPLY_PATH,
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        return channel.json_body

    def test_autoreply_get_and_post(self) -> None:
        """GET /autoreply returns default, POST updates it."""
        initial = self._get_autoreply()
        self.assertFalse(initial.get("enabled", False))

        self._set_autoreply(True, "Xin chao! Day la tin nhan tu dong.")
        updated = self._get_autoreply()
        self.assertTrue(updated.get("enabled"))
        self.assertEqual(updated.get("message"), "Xin chao! Day la tin nhan tu dong.")

    def test_settings_get_auto_reply_compatibility(self) -> None:
        """GET /settings/get/auto_reply returns 200 (not 404) and syncs with /autoreply."""
        # Check GET /settings/get/auto_reply directly
        res = self.make_request(
            "GET",
            "/_synapse/staff/v1/settings/get/auto_reply",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(res.code, HTTPStatus.OK, res.json_body)
        self.assertEqual(res.json_body.get("key"), "auto_reply")
        self.assertIn("value", res.json_body)

        # Update via /autoreply POST and verify /settings/get/auto_reply reflects it
        self._set_autoreply(True, "Sync test message")
        res2 = self.make_request(
            "GET",
            "/_synapse/staff/v1/settings/get/auto_reply",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(res2.code, HTTPStatus.OK)
        val = res2.json_body.get("value") or {}
        self.assertTrue(val.get("enabled"))
        self.assertEqual(val.get("message"), "Sync test message")

    def test_autoreply_url_variants(self) -> None:
        """Verify /autoreply/, /auto_reply, and /auto_reply/ work seamlessly."""
        self._set_autoreply(True, "Variant test")
        for path in (
            "/_synapse/staff/v1/autoreply/",
            "/_synapse/staff/v1/auto_reply",
            "/_synapse/staff/v1/auto_reply/",
        ):
            res = self.make_request("GET", path, custom_headers=_secret_headers())
            self.assertEqual(res.code, HTTPStatus.OK, f"Failed on path {path}")
            self.assertTrue(res.json_body.get("enabled"))
            self.assertEqual(res.json_body.get("message"), "Variant test")

    def test_autoreply_triggers_when_message_sent_to_staff(self) -> None:
        """When enabled, a message from peer to a DM containing staff triggers auto-reply."""
        preset_msg = "Chung toi se phan hoi som nhat!"
        self._set_autoreply(True, preset_msg)

        # Peer creates DM with staff
        room_id = self.helper.create_room_as(
            self.peer_mxid, tok=self.peer_tok
        )
        self.helper.invite(
            room_id,
            src=self.peer_mxid,
            targ=self.staff_mxid,
            tok=self.peer_tok,
        )
        self.helper.join(room_id, self.staff_mxid, tok=self.staff_tok)

        # Peer sends a message to the room
        send_res = self.helper.send(
            room_id, "Hello staff!", tok=self.peer_tok
        )
        self.assertIn("event_id", send_res)

        # Check messages in room; staff should have auto-replied
        sync_res = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=10",
            access_token=self.peer_tok,
        )
        self.assertEqual(sync_res.code, 200)
        chunk = sync_res.json_body.get("chunk", [])
        
        # Verify an event from staff_mxid with the preset body exists
        staff_replies = [
            ev for ev in chunk
            if ev.get("sender") == self.staff_mxid
            and ev.get("type") == "m.room.message"
            and (ev.get("content") or {}).get("body") == preset_msg
        ]
        self.assertTrue(len(staff_replies) >= 1, f"Expected auto-reply in {chunk}")

    def test_no_autoreply_when_disabled(self) -> None:
        """When disabled, peer message does not trigger auto-reply."""
        self._set_autoreply(False, "Tin nhan tat.")

        room_id = self.helper.create_room_as(
            self.peer_mxid, tok=self.peer_tok
        )
        self.helper.invite(
            room_id,
            src=self.peer_mxid,
            targ=self.staff_mxid,
            tok=self.peer_tok,
        )
        self.helper.join(room_id, self.staff_mxid, tok=self.staff_tok)

        self.helper.send(room_id, "Hello when disabled", tok=self.peer_tok)

        sync_res = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b&limit=10",
            access_token=self.peer_tok,
        )
        self.assertEqual(sync_res.code, 200)
        chunk = sync_res.json_body.get("chunk", [])
        staff_replies = [
            ev for ev in chunk
            if ev.get("sender") == self.staff_mxid
            and ev.get("type") == "m.room.message"
        ]
        self.assertEqual(len(staff_replies), 0)
