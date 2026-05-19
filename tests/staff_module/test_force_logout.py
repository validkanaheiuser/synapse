#
# S11 — `/_synapse/staff/v1/force_logout` tests.
#
# Lives at `synapse.staff_module.rest_users.StaffForceLogoutServlet`.
# Three shapes of `device_ids`:
#   - absent          : wipe ALL devices for each MXID (legacy behaviour).
#   - list of strings : wipe those device ids on EVERY listed MXID.
#   - {mxid: [ids,...]}: per-user device id list.
#
# Authentication: X-Staff-Secret only (the new Bearer/JWT path is
# Agent H's work and is not exercised by the current rest_base wiring of
# this servlet).
#

import unittest
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


FORCE_LOGOUT_PATH = "/_synapse/staff/v1/force_logout"


def _secret_headers() -> Iterable[tuple[bytes, bytes]]:
    return [(STAFF_SECRET_HEADER, STAFF_SECRET.encode("ascii"))]


class StaffForceLogoutTest(StaffHomeserverTestCase):
    """Tests for the bulk-and-per-device logout endpoint."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()
        self.victim_mxid = self.register_user("victim", "pass")
        # Two separate logins => two devices for the victim.
        self.victim_tok_a = self.login("victim", "pass")
        self.victim_tok_b = self.login("victim", "pass")
        self.staff_mxid = self.register_user("logoutstaff", "pass")
        self.add_staff_user(self.staff_mxid)

    def _list_victim_devices(self) -> list[str]:
        store = self.hs.get_datastores().main
        devices_map = self.get_success(
            store.get_devices_by_user(self.victim_mxid)
        )
        return list(devices_map.keys())

    def test_force_logout_all_devices_without_device_ids(self) -> None:
        """POST with no `device_ids` deletes every device for the target."""
        # Sanity: the user has at least one device before we wipe.
        before = self._list_victim_devices()
        self.assertTrue(before)

        channel = self.make_request(
            "POST",
            FORCE_LOGOUT_PATH,
            content={"usernames": [self.victim_mxid]},
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        results = channel.json_body["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["user_id"], self.victim_mxid)
        self.assertEqual(results[0]["status"], "logged_out")
        self.assertEqual(results[0]["scope"], "all")

        # All devices removed.
        after = self._list_victim_devices()
        self.assertEqual(after, [])

    def test_force_logout_with_device_ids_list(self) -> None:
        """`device_ids: [d1, d2]` removes ONLY those devices, leaving the
        rest intact (subject to validation that the device belongs to the
        user)."""
        all_devices = self._list_victim_devices()
        self.assertGreaterEqual(len(all_devices), 2)
        # Keep at least one device alive.
        to_remove = all_devices[:1]
        to_keep = all_devices[1:]

        channel = self.make_request(
            "POST",
            FORCE_LOGOUT_PATH,
            content={
                "usernames": [self.victim_mxid],
                "device_ids": to_remove,
            },
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        results = channel.json_body["results"]
        self.assertEqual(results[0]["status"], "logged_out")
        self.assertEqual(results[0]["scope"], "devices")
        self.assertEqual(results[0]["devices_removed"], len(to_remove))

        after = set(self._list_victim_devices())
        # Targeted devices are gone, the rest stay.
        for d in to_remove:
            self.assertNotIn(d, after)
        for d in to_keep:
            self.assertIn(d, after)

    def test_force_logout_with_device_ids_map_per_user(self) -> None:
        """`device_ids: {mxid: [d1, d2]}` only removes the listed devices
        for the named MXID."""
        all_devices = self._list_victim_devices()
        self.assertGreaterEqual(len(all_devices), 2)
        target = all_devices[:1]

        channel = self.make_request(
            "POST",
            FORCE_LOGOUT_PATH,
            content={
                "usernames": [self.victim_mxid],
                "device_ids": {self.victim_mxid: target},
            },
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        results = channel.json_body["results"]
        self.assertEqual(results[0]["status"], "logged_out")
        self.assertEqual(results[0]["scope"], "devices")
        self.assertEqual(results[0]["devices_removed"], len(target))

        after = set(self._list_victim_devices())
        for d in target:
            self.assertNotIn(d, after)

    def test_missing_secret_returns_unauthorised(self) -> None:
        """Without `X-Staff-Secret` the endpoint must refuse the call."""
        channel = self.make_request(
            "POST",
            FORCE_LOGOUT_PATH,
            content={"usernames": [self.victim_mxid]},
        )
        # AuthError from check_staff_secret => 401.
        self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.json_body)

    @unittest.skip(
        "skipped_staff allowlist short-circuit is not yet implemented in "
        "rest_users.StaffForceLogoutServlet (depends on Agent H)."
    )
    def test_staff_in_allowlist_is_not_logged_out(self) -> None:
        """A staff user in the allowlist that appears in `usernames`
        should be returned with status `skipped_staff` instead of being
        logged out.  Currently the servlet has no such short-circuit, so
        this test is skipped until it lands."""
