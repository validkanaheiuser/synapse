#
# S11 — F13 staff KV settings endpoints
# (`synapse.staff_module.rest_settings`).
#
# Endpoints:
#   POST   /_synapse/staff/v1/settings/update/{key}    stores raw JSON
#   GET    /_synapse/staff/v1/settings/get/{key}       returns the value
#   GET    /_synapse/staff/v1/settings/get/all         returns the whole map
#   DELETE /_synapse/staff/v1/settings/{key}           removes a row
#
# Key validation: `[A-Za-z0-9_.\-]{1,128}`.  Anything outside that
# character class returns 400.
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


SETTINGS_PREFIX = "/_synapse/staff/v1/settings"


def _secret_headers() -> Iterable[tuple[bytes, bytes]]:
    return [(STAFF_SECRET_HEADER, STAFF_SECRET.encode("ascii"))]


class StaffSettingsTest(StaffHomeserverTestCase):
    """KV round-trip + prefix filtering + key validation."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()

    def _upsert(self, key: str, value: object, expect_code: int = 200) -> None:
        channel = self.make_request(
            "POST",
            f"{SETTINGS_PREFIX}/update/{key}",
            content=value,
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, expect_code, channel.json_body)

    def _get(self, key: str, expect_code: int = 200) -> dict:
        channel = self.make_request(
            "GET",
            f"{SETTINGS_PREFIX}/get/{key}",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, expect_code, channel.json_body)
        return channel.json_body

    def _get_all(self, prefix: str | None = None) -> dict:
        path = f"{SETTINGS_PREFIX}/get/all"
        if prefix is not None:
            path += f"?prefix={prefix}"
        channel = self.make_request(
            "GET", path, custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def _delete(self, key: str, expect_code: int = 200) -> dict:
        channel = self.make_request(
            "DELETE",
            f"{SETTINGS_PREFIX}/{key}",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, expect_code, channel.json_body)
        return channel.json_body

    def test_kv_round_trip(self) -> None:
        """POST update/{key} stores; GET get/{key} returns; GET get/all
        contains; DELETE removes."""
        self._upsert("theme", {"mode": "dark", "accent": "#0f0"})

        got = self._get("theme")
        self.assertEqual(got["key"], "theme")
        self.assertEqual(got["value"], {"mode": "dark", "accent": "#0f0"})

        all_body = self._get_all()
        self.assertIn("theme", all_body["settings"])
        self.assertEqual(
            all_body["settings"]["theme"], {"mode": "dark", "accent": "#0f0"}
        )

        deleted = self._delete("theme")
        self.assertTrue(deleted["removed"])

        # After delete, GET returns 404.
        channel = self.make_request(
            "GET",
            f"{SETTINGS_PREFIX}/get/theme",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.json_body)

    def test_get_all_prefix_only_returns_matching_keys(self) -> None:
        """When called with `?prefix=ui.` only keys starting with `ui.`
        appear in the response (Agent I S6 functionality)."""
        self._upsert("ui.theme", "dark")
        self._upsert("ui.lang", "en")
        self._upsert("policy.retention", 90)

        body = self._get_all(prefix="ui.")
        settings = body["settings"]
        self.assertIn("ui.theme", settings)
        self.assertIn("ui.lang", settings)
        self.assertNotIn("policy.retention", settings)
        # The endpoint echoes the prefix back so callers can audit.
        self.assertEqual(body.get("prefix"), "ui.")

    def test_invalid_key_chars_rejected(self) -> None:
        """Keys outside `[A-Za-z0-9_.\\-]{1,128}` return 400."""
        # "with/slash" is excluded because the URL router would split on
        # the slash before validation runs, producing a 404 rather than
        # the 400 we want to assert on.  All keys below contain a single
        # character outside `[A-Za-z0-9_.\-]` and have no URL-significant
        # bytes.
        for bad_key in ["weird$char", "with:colon", "with!bang", "with*star"]:
            channel = self.make_request(
                "POST",
                f"{SETTINGS_PREFIX}/update/{bad_key}",
                content={"x": 1},
                custom_headers=_secret_headers(),
            )
            self.assertEqual(
                channel.code, HTTPStatus.BAD_REQUEST,
                msg=f"key {bad_key!r} should have been rejected"
            )

    def test_missing_secret_returns_unauthorised(self) -> None:
        """Without `X-Staff-Secret` every settings endpoint refuses."""
        channel = self.make_request(
            "GET", f"{SETTINGS_PREFIX}/get/all",
        )
        self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.json_body)
