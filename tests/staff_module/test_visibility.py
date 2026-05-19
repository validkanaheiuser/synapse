#
# S11 — visibility / sync timeline-vs-state relocation tests.
#
# Covers the F8 patch in `synapse/rest/client/sync.py` (lines ~616-661),
# which moves state events whose type is in
# `synapse.staff_filter.HIDDEN_STATE_TYPES` from `timeline.events` into
# `state.events` for non-staff requesters, and keeps them in the timeline
# verbatim for staff.
#
# A "staff" requester is defined as: header `X-STAFF-Client: 1` present
# AND requester user_id in the `staff_users` allowlist (see
# `synapse.staff_filter.is_staff_request`).  Either half failing causes
# the relocation to apply.
#

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.staff_module.conftest import (
    STAFF_HEADER_NAME,
    STAFF_HEADER_VALUE,
    StaffHomeserverTestCase,
)


# State event types that the sync patch relocates from timeline -> state.
# We test with `m.room.member` because that event reliably appears in
# both timeline and state in tests (it's emitted on every join).
HIDDEN_TYPE = "m.room.member"


def _types_in(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


class StaffSyncVisibilityTest(StaffHomeserverTestCase):
    """The F8 timeline-vs-state relocation only applies when a request is
    BOTH header-tagged AND allowlisted."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()

        # One user we mark as staff, one we leave plain.
        self.staff_mxid = self.register_user("staffuser", "pass")
        self.staff_tok = self.login("staffuser", "pass")
        self.plain_mxid = self.register_user("plainuser", "pass")
        self.plain_tok = self.login("plainuser", "pass")

        # Promote staffuser into the allowlist and refresh the cache so
        # `is_staff_user` returns True for them.
        self.add_staff_user(self.staff_mxid)

        # Both users in one room.  We use the staff user as the creator
        # to ensure they see their own m.room.member join in the timeline.
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

    def _do_sync(
        self,
        *,
        access_token: str,
        send_staff_header: bool,
    ) -> dict:
        headers = None
        if send_staff_header:
            headers = [(STAFF_HEADER_NAME, STAFF_HEADER_VALUE)]
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/sync",
            access_token=access_token,
            custom_headers=headers,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def _room_block(self, sync_body: dict, room_id: str) -> dict:
        return (
            sync_body.get("rooms", {})
            .get("join", {})
            .get(room_id, {})
        )

    def test_staff_with_header_sees_member_in_timeline(self) -> None:
        """A staff user (allowlisted AND header-tagged) receives
        `m.room.member` events in `timeline.events`, not relocated."""
        body = self._do_sync(
            access_token=self.staff_tok, send_staff_header=True
        )
        room = self._room_block(body, self.room_id)
        timeline_events = room.get("timeline", {}).get("events", [])
        timeline_types = _types_in(timeline_events)
        # Staff path: relocation is bypassed entirely, so member events
        # remain in the timeline as Matrix delivers them.
        self.assertIn(HIDDEN_TYPE, timeline_types)

    def test_non_staff_sees_member_relocated_to_state(self) -> None:
        """A plain (non-staff) user receives the same `m.room.member`
        events in `state.events` instead of `timeline.events` — they are
        not stripped, just moved."""
        body = self._do_sync(
            access_token=self.plain_tok, send_staff_header=False
        )
        room = self._room_block(body, self.room_id)
        timeline_types = _types_in(
            room.get("timeline", {}).get("events", [])
        )
        state_types = _types_in(room.get("state", {}).get("events", []))
        # The member event MUST NOT appear in the timeline anymore.
        self.assertNotIn(HIDDEN_TYPE, timeline_types)
        # ... but it MUST still be in state so the client's room model
        # is correct.
        self.assertIn(HIDDEN_TYPE, state_types)

    def test_header_alone_without_allowlist_is_not_staff(self) -> None:
        """A non-staff user who sends `X-STAFF-Client: 1` but is NOT in
        the `staff_users` allowlist is treated as non-staff: the F8
        relocation still applies to their sync."""
        body = self._do_sync(
            access_token=self.plain_tok, send_staff_header=True
        )
        room = self._room_block(body, self.room_id)
        timeline_types = _types_in(
            room.get("timeline", {}).get("events", [])
        )
        state_types = _types_in(room.get("state", {}).get("events", []))
        self.assertNotIn(HIDDEN_TYPE, timeline_types)
        self.assertIn(HIDDEN_TYPE, state_types)

    def test_allowlisted_without_header_is_not_staff(self) -> None:
        """A staff-allowlisted user who omits the `X-STAFF-Client` header
        is also treated as non-staff (both halves of the check must hold)."""
        body = self._do_sync(
            access_token=self.staff_tok, send_staff_header=False
        )
        room = self._room_block(body, self.room_id)
        timeline_types = _types_in(
            room.get("timeline", {}).get("events", [])
        )
        state_types = _types_in(room.get("state", {}).get("events", []))
        self.assertNotIn(HIDDEN_TYPE, timeline_types)
        self.assertIn(HIDDEN_TYPE, state_types)
