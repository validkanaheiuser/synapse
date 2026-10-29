"""Account-based restrictions on member enumeration for power-zero users."""

from types import SimpleNamespace

from synapse.api.constants import EventTypes
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.staff_filter import MEMBER_LIST_RESTRICTED

from tests.unittest import HomeserverTestCase


class StaffMemberListTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.owner = self.register_user("owner", "pass")
        self.owner_token = self.login("owner", "pass")
        self.room_id = self.helper.create_room_as(
            self.owner, tok=self.owner_token, room_version="10"
        )
        self.users = {}
        self.tokens = {}
        for name in ("viewer", "staff", "ordinary", "power49", "power50"):
            user_id = self.register_user(name, "pass")
            token = self.login(name, "pass")
            self.users[name] = user_id
            self.tokens[name] = token
            self.helper.join(self.room_id, user_id, tok=token)

        self.power_levels = {
            "users": {
                self.owner: 100,
                self.users["power49"]: 49,
                self.users["power50"]: 50,
            },
            "users_default": 0,
        }
        self.set_power_levels()
        # The list policy needs only the authenticated allowlist lookup. Avoid
        # unrelated scheduler/autoreply hooks in these client API tests.
        hs.config.staff.staff_enabled = True
        self.staff_ids = {self.users["staff"]}
        hs._staff_store = SimpleNamespace(is_staff_user=self.staff_ids.__contains__)

    def set_power_levels(self):
        self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            self.power_levels,
            tok=self.owner_token,
        )

    def members(self, name="viewer", endpoint="members", header=True, query=""):
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{self.room_id}/{endpoint}{query}",
            access_token=self.tokens[name],
            custom_headers=[(b"X-STAFF-Client", b"1")] if header else None,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        body = channel.json_body
        if endpoint == "joined_members":
            ids = set(body["joined"])
        else:
            ids = {event["state_key"] for event in body["chunk"]}
        return ids, body[MEMBER_LIST_RESTRICTED]

    def test_zero_power_sees_only_staff_and_power_at_least_50(self):
        expected = {self.owner, self.users["staff"], self.users["power50"]}
        for endpoint in ("members", "joined_members"):
            for header in (False, True):
                ids, restricted = self.members(endpoint=endpoint, header=header)
                self.assertEqual(ids, expected)
                self.assertTrue(restricted)

    def test_staff_with_zero_power_sees_full_list(self):
        for endpoint in ("members", "joined_members"):
            ids, restricted = self.members("staff", endpoint)
            self.assertEqual(ids, {self.owner, *self.users.values()})
            self.assertFalse(restricted)

    def test_nonzero_power_requester_sees_full_list(self):
        self.power_levels["users"][self.users["viewer"]] = 1
        self.set_power_levels()
        for endpoint in ("members", "joined_members"):
            ids, restricted = self.members(endpoint=endpoint)
            self.assertEqual(ids, {self.owner, *self.users.values()})
            self.assertFalse(restricted)

    def test_users_default_is_used_for_targets_and_requester(self):
        self.power_levels["users_default"] = 50
        self.power_levels["users"][self.users["viewer"]] = 0
        self.set_power_levels()
        ids, restricted = self.members()
        self.assertTrue(restricted)
        self.assertEqual(
            ids,
            {self.owner, self.users["staff"], self.users["ordinary"], self.users["power50"]},
        )
        del self.power_levels["users"][self.users["viewer"]]
        self.set_power_levels()
        _, restricted = self.members()
        self.assertFalse(restricted)

    def test_permission_changes_are_applied_on_the_next_request(self):
        self.members()
        self.power_levels["users"][self.users["power49"]] = 50
        self.power_levels["users"][self.users["power50"]] = 49
        self.set_power_levels()
        self.staff_ids.clear()
        for endpoint in ("members", "joined_members"):
            ids, restricted = self.members(endpoint=endpoint)
            self.assertTrue(restricted)
            self.assertEqual(ids, {self.owner, self.users["power49"]})

    def test_membership_query_still_filters_eligible_members(self):
        ids, restricted = self.members(query="?membership=invite")
        self.assertTrue(restricted)
        self.assertEqual(ids, set())
        ids, _ = self.members(query="?membership=join")
        self.assertEqual(ids, {self.owner, self.users["staff"], self.users["power50"]})

    def test_disabled_staff_module_preserves_full_list(self):
        self.hs.config.staff.staff_enabled = False
        for endpoint in ("members", "joined_members"):
            ids, restricted = self.members(endpoint=endpoint)
            self.assertFalse(restricted)
            self.assertEqual(ids, {self.owner, *self.users.values()})
