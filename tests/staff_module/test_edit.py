#
# S11 — F3 staff /edit_message + /edit_history endpoints
# (`synapse.staff_module.rest_edit`).
#
#   POST /_synapse/staff/v1/edit_message
#     -> forges an m.replace edit of the original event AS the original
#        sender (federation is off, so puppeting is safe).
#   GET  /_synapse/staff/v1/edit_history/{event_id}
#     -> returns the chain of staff edits + stealth-redacts recorded in
#        `staff_edit_history`.
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


EDIT_PATH = "/_synapse/staff/v1/edit_message"
HISTORY_PREFIX = "/_synapse/staff/v1/edit_history"


def _secret_headers() -> Iterable[tuple[bytes, bytes]]:
    return [(STAFF_SECRET_HEADER, STAFF_SECRET.encode("ascii"))]


class StaffEditMessageTest(StaffHomeserverTestCase):
    """F3 — edit-on-behalf-of and the staff-only history readback."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()
        self.author_mxid = self.register_user("editauthor", "pass")
        self.author_tok = self.login("editauthor", "pass")
        self.other_mxid = self.register_user("editother", "pass")
        self.other_tok = self.login("editother", "pass")
        self.staff_mxid = self.register_user("editstaff", "pass")
        self.add_staff_user(self.staff_mxid)

        self.room_id = self.helper.create_room_as(
            self.author_mxid, tok=self.author_tok
        )
        # Send one message from `author_mxid` that we'll edit later.
        send_res = self.helper.send(
            self.room_id, body="original text", tok=self.author_tok
        )
        self.event_id = send_res["event_id"]

        # A second room (used by a "wrong room" negative test).
        self.other_room_id = self.helper.create_room_as(
            self.author_mxid, tok=self.author_tok
        )

    def _edit(self, body: dict, expect_code: int = 200) -> dict:
        channel = self.make_request(
            "POST",
            EDIT_PATH,
            content=body,
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, expect_code, channel.json_body)
        return channel.json_body

    def test_edit_with_image_new_content_succeeds(self) -> None:
        """A text message can be edited so `new_content` becomes an image
        — F4 verified that the server accepts any (msgtype, msgtype)
        combination for an m.replace edit."""
        resp = self._edit({
            "room_id": self.room_id,
            "event_id": self.event_id,
            "new_content": {
                "msgtype": "m.image",
                "body": "kitten.png",
                "url": "mxc://test/kitten",
            },
            "edited_by": self.staff_mxid,
        })
        self.assertEqual(resp["event_id"], self.event_id)
        self.assertIn("replace_event_id", resp)
        # The reply also surfaces the sender; per F3, it must be the
        # ORIGINAL sender, not the staff that triggered the edit.
        self.assertEqual(resp["sender"], self.author_mxid)

    def test_replace_event_sender_is_original_not_staff(self) -> None:
        """The persisted m.replace event must have sender == the original
        author so cached clients pick it up as a user self-edit (and the
        `(edited)` badge appears normally)."""
        resp = self._edit({
            "room_id": self.room_id,
            "event_id": self.event_id,
            "new_content": {"msgtype": "m.text", "body": "rewritten"},
            "edited_by": self.staff_mxid,
        })
        replace_event_id = resp["replace_event_id"]

        # Look up the event from the store and check its sender attribute.
        store = self.hs.get_datastores().main
        replace_event = self.get_success(
            store.get_event(replace_event_id, allow_none=False)
        )
        self.assertEqual(replace_event.sender, self.author_mxid)
        # The edit's content has the m.relates_to.m.replace pointer.
        rel = (replace_event.content or {}).get("m.relates_to") or {}
        self.assertEqual(rel.get("rel_type"), "m.replace")
        self.assertEqual(rel.get("event_id"), self.event_id)

    def test_edit_writes_staff_edit_history_row(self) -> None:
        """Each successful edit appends a row to `staff_edit_history`
        with the previous content snapshot + the staff who triggered it."""
        self._edit({
            "room_id": self.room_id,
            "event_id": self.event_id,
            "new_content": {"msgtype": "m.text", "body": "new body"},
            "edited_by": self.staff_mxid,
        })
        rows = self.get_success(
            self.hs._staff_store.edit_history_for(self.event_id)
        )
        self.assertTrue(rows)
        last = rows[-1]
        self.assertEqual(last["kind"], "edit")
        self.assertEqual(last["edited_by"], self.staff_mxid)
        self.assertEqual(last["sender"], self.author_mxid)
        # old_content snapshot includes the original body.
        self.assertEqual(last["old_content"].get("body"), "original text")
        # new_content reflects what was sent.
        self.assertEqual(last["new_content"].get("body"), "new body")

    def test_edit_history_endpoint_returns_chain(self) -> None:
        """GET /edit_history/{event_id} returns the full chain of edits
        recorded for that event."""
        # Two consecutive edits.
        self._edit({
            "room_id": self.room_id,
            "event_id": self.event_id,
            "new_content": {"msgtype": "m.text", "body": "v2"},
            "edited_by": self.staff_mxid,
        })
        self._edit({
            "room_id": self.room_id,
            "event_id": self.event_id,
            "new_content": {"msgtype": "m.text", "body": "v3"},
            "edited_by": self.staff_mxid,
        })

        channel = self.make_request(
            "GET",
            f"{HISTORY_PREFIX}/{self.event_id}",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        body = channel.json_body
        self.assertEqual(body["event_id"], self.event_id)
        self.assertEqual(len(body["edits"]), 2)
        # Chain is in ascending order — v2 was first, v3 second.
        self.assertEqual(body["edits"][0]["new_content"]["body"], "v2")
        self.assertEqual(body["edits"][1]["new_content"]["body"], "v3")

    def test_edit_nonexistent_event_returns_404(self) -> None:
        """A bogus event_id that does not exist in any room returns 404."""
        bogus_id = "$nonsense_does_not_exist:test"
        self._edit(
            {
                "room_id": self.room_id,
                "event_id": bogus_id,
                "new_content": {"msgtype": "m.text", "body": "x"},
            },
            expect_code=HTTPStatus.NOT_FOUND,
        )

    def test_edit_event_in_wrong_room_returns_400(self) -> None:
        """`event_id` exists but in a different room than `room_id` =>
        400 with the "event is not in this room" message."""
        self._edit(
            {
                "room_id": self.other_room_id,   # wrong room
                "event_id": self.event_id,       # belongs to self.room_id
                "new_content": {"msgtype": "m.text", "body": "x"},
            },
            expect_code=HTTPStatus.BAD_REQUEST,
        )
