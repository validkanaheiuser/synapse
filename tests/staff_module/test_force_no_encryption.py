#
# Tests for EncryptionBlocker (F1 enforcement).
#

from http import HTTPStatus

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.staff_module.conftest import (
    StaffHomeserverTestCase,
)


class StaffForceNoEncryptionTest(StaffHomeserverTestCase):
    """Test that room creation with m.room.encryption succeeds unencrypted,
    and subsequent attempts to send m.room.encryption are rejected.
    """

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()
        self.user1 = self.register_user("u1", "pass")
        self.tok1 = self.login("u1", "pass")
        self.user2 = self.register_user("u2", "pass")
        self.tok2 = self.login("u2", "pass")

    def test_create_room_with_encryption_initial_state_succeeds_unencrypted(self) -> None:
        """When Element creates a DM room with m.room.encryption in initial_state,
        the encryption event is stripped so the room creates successfully without encryption.
        """
        body = {
            "preset": "trusted_private_chat",
            "visibility": "private",
            "invite": [self.user2],
            "is_direct": True,
            "initial_state": [
                {
                    "type": "m.room.guest_access",
                    "state_key": "",
                    "content": {"guest_access": "can_join"},
                },
                {
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                },
                {
                    "type": "m.room.history_visibility",
                    "content": {"history_visibility": "invited"},
                },
            ],
        }
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/createRoom",
            content=body,
            access_token=self.tok1,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        room_id = channel.json_body["room_id"]

        # Verify that m.room.encryption state event was NOT persisted in the room
        state_channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/state/m.room.encryption/",
            access_token=self.tok1,
        )
        self.assertEqual(state_channel.code, HTTPStatus.NOT_FOUND)

    def test_put_encryption_event_rejected(self) -> None:
        """Subsequent attempt to turn on encryption in the room is rejected with 403."""
        room_id = self.helper.create_room_as(self.user1, tok=self.tok1)
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/state/m.room.encryption/",
            content={"algorithm": "m.megolm.v1.aes-sha2"},
            access_token=self.tok1,
        )
        self.assertEqual(channel.code, HTTPStatus.FORBIDDEN)
