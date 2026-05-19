#
# S11 — `synapse.staff_module.stealth.stealth_redact_event` tests.
#
# Stealth-redact is the F2 primitive used by /wipe_room and
# /delete_messages.  For a single `event_id` it:
#
#   1. Forges an empty-content `m.replace` edit (sent as the original
#      sender) so cached clients blank the message in realtime.
#   2. Forges a `m.room.redaction` (also as the original sender) so the
#      server-side copy is gone for fresh syncs.
#   3. Writes a `staff_edit_history` row with the original content
#      snapshot, so staff can recover the old text from
#      /staff/v1/edit_history/{event_id}.
#
# Sync delivery filters (see `synapse/rest/client/sync.py:~616-661`) then:
#   - drop `m.room.redaction` events from /sync for non-staff,
#   - drop server-side-redacted events from /sync for non-staff,
#   - deliver everything verbatim to staff.
#

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.staff_module.stealth import stealth_redact_event
from synapse.util.clock import Clock

from tests.staff_module.conftest import (
    STAFF_HEADER_NAME,
    STAFF_HEADER_VALUE,
    StaffHomeserverTestCase,
)


class StealthRedactTest(StaffHomeserverTestCase):
    """End-to-end tests for `stealth_redact_event`."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()
        self.staff_mxid = self.register_user("stealthstaff", "pass")
        self.staff_tok = self.login("stealthstaff", "pass")
        self.plain_mxid = self.register_user("stealthplain", "pass")
        self.plain_tok = self.login("stealthplain", "pass")
        self.add_staff_user(self.staff_mxid)

        self.room_id = self.helper.create_room_as(
            self.staff_mxid, tok=self.staff_tok
        )
        self.helper.invite(
            self.room_id,
            src=self.staff_mxid,
            targ=self.plain_mxid,
            tok=self.staff_tok,
        )
        self.helper.join(self.room_id, self.plain_mxid, tok=self.plain_tok)

        # Send a plain text message from `plain_mxid` that we will
        # stealth-redact.
        send_res = self.helper.send(
            self.room_id, body="hello world", tok=self.plain_tok
        )
        self.target_event_id = send_res["event_id"]

    def _run_stealth(self) -> dict:
        return self.get_success(
            stealth_redact_event(
                hs=self.hs,
                store=self.hs._staff_store,
                event_id=self.target_event_id,
                edited_by=self.staff_mxid,
            )
        )

    def _sync(self, *, tok: str, with_staff_header: bool) -> dict:
        headers = None
        if with_staff_header:
            headers = [(STAFF_HEADER_NAME, STAFF_HEADER_VALUE)]
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/sync",
            access_token=tok,
            custom_headers=headers,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def _timeline_events(self, sync_body: dict) -> list[dict]:
        return (
            sync_body.get("rooms", {})
            .get("join", {})
            .get(self.room_id, {})
            .get("timeline", {})
            .get("events", [])
        )

    def test_stealth_redact_produces_empty_edit_and_redaction(self) -> None:
        """`stealth_redact_event` returns status ok and produces both a
        replace event id and a redaction event id."""
        result = self._run_stealth()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["event_id"], self.target_event_id)
        self.assertTrue(result.get("replace_event_id"))
        self.assertTrue(result.get("redaction_event_id"))

    def test_original_event_marked_redacted_in_db(self) -> None:
        """After stealth redact, the original event's `internal_metadata`
        flag indicates redaction at the DB level."""
        self._run_stealth()
        store = self.hs.get_datastores().main
        ev = self.get_success(
            store.get_event(self.target_event_id, allow_none=False)
        )
        self.assertTrue(ev.internal_metadata.is_redacted())

    def test_edit_history_row_has_old_content_snapshot(self) -> None:
        """The forged m.replace and redaction are accompanied by a
        `staff_edit_history` row whose `old_content` is the original
        message content."""
        self._run_stealth()
        rows = self.get_success(
            self.hs._staff_store.edit_history_for(self.target_event_id)
        )
        self.assertTrue(rows, "expected an edit_history row")
        last = rows[-1]
        self.assertEqual(last["original_event_id"], self.target_event_id)
        self.assertEqual(last["sender"], self.plain_mxid)
        self.assertEqual(last["kind"], "stealth_redact")
        self.assertEqual(last["old_content"].get("body"), "hello world")
        self.assertEqual(last["old_content"].get("msgtype"), "m.text")

    def test_non_staff_sync_does_not_deliver_the_redaction(self) -> None:
        """Non-staff sync must NOT contain the `m.room.redaction` event
        nor the now-redacted original event in `timeline.events`.  The
        cached client will only see the empty m.replace edit."""
        self._run_stealth()
        body = self._sync(tok=self.plain_tok, with_staff_header=False)
        events = self._timeline_events(body)
        event_ids = [e.get("event_id") for e in events]
        types = [e.get("type") for e in events]
        # Redaction event itself must be filtered out by the sync patch.
        self.assertNotIn("m.room.redaction", types)
        # And the original (now-redacted) message must also be dropped.
        self.assertNotIn(self.target_event_id, event_ids)

    def test_non_staff_messages_does_not_return_redacted_event(self) -> None:
        """`/messages` (pagination) must also strip the redacted original
        event for non-staff readers."""
        self._run_stealth()
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{self.room_id}/messages?dir=b&limit=50",
            access_token=self.plain_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        chunk = channel.json_body.get("chunk", [])
        chunk_ids = [e.get("event_id") for e in chunk]
        # The redacted-and-stealth original must NOT appear in /messages
        # for a non-staff caller.  Note: depending on Synapse version the
        # redaction event itself may appear; we only assert on the target.
        self.assertNotIn(self.target_event_id, chunk_ids)

    def test_staff_sync_sees_redaction_normally(self) -> None:
        """A staff (header + allowlist) /sync sees the redaction event
        delivered normally — the F2 suppression only applies to
        non-staff."""
        self._run_stealth()
        body = self._sync(tok=self.staff_tok, with_staff_header=True)
        events = self._timeline_events(body)
        types = [e.get("type") for e in events]
        # The staff path bypasses the sync.py drop, so the redaction
        # event flows through to the staff client.
        self.assertIn("m.room.redaction", types)
