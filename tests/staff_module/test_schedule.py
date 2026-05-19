#
# S11 — F12 scheduled-message endpoints
# (`synapse.staff_module.rest_schedule`).
#
# Endpoints:
#   POST   /_synapse/staff/v1/schedule          create
#   GET    /_synapse/staff/v1/schedule          list pending
#   DELETE /_synapse/staff/v1/schedule/{id}     cancel
#   PATCH  /_synapse/staff/v1/schedule/{id}     edit pending row
#
# `send_at` is naive ISO 8601 interpreted in Vietnam ICT (UTC+7).  An
# attempt to schedule in the past returns a 400 with a Vietnam-named
# error message.
#

from datetime import datetime, timedelta, timezone
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


SCHEDULE_PATH = "/_synapse/staff/v1/schedule"
ICT_OFFSET = timedelta(hours=7)


def _secret_headers() -> Iterable[tuple[bytes, bytes]]:
    return [(STAFF_SECRET_HEADER, STAFF_SECRET.encode("ascii"))]


def _ict_iso_from_utc(utc_dt: datetime) -> str:
    """Format a UTC datetime as a naive ISO string in Vietnam ICT."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    ict = utc_dt.astimezone(timezone(ICT_OFFSET)).replace(tzinfo=None)
    return ict.isoformat()


class StaffScheduleTest(StaffHomeserverTestCase):
    """End-to-end tests for the scheduled-message endpoints."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()
        self.sender_mxid = self.register_user("scheduler", "pass")
        self.sender_tok = self.login("scheduler", "pass")
        # Need at least one valid room owned by `sender_mxid` to make
        # the scheduled task's `as_user` consistent.
        self.room_id = self.helper.create_room_as(
            self.sender_mxid, tok=self.sender_tok
        )

    def _now_utc(self) -> datetime:
        # Use the test reactor's clock so this is deterministic.
        return datetime.fromtimestamp(
            self.hs.get_clock().time(), tz=timezone.utc
        )

    def _post_schedule(self, body: dict, expect_code: int = 200) -> dict:
        channel = self.make_request(
            "POST",
            SCHEDULE_PATH,
            content=body,
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, expect_code, channel.json_body)
        return channel.json_body

    def test_post_with_future_ict_timestamp_succeeds(self) -> None:
        """A timestamp 1 hour in the future (expressed in Vietnam ICT)
        is accepted and returns a task_id."""
        future_utc = self._now_utc() + timedelta(hours=1)
        body = {
            "room_id": self.room_id,
            "as_user": self.sender_mxid,
            "send_at": _ict_iso_from_utc(future_utc),
            "message": "hello future",
        }
        resp = self._post_schedule(body)
        self.assertIn("task_id", resp)
        self.assertEqual(resp["room_id"], self.room_id)
        self.assertEqual(resp["as_user"], self.sender_mxid)
        self.assertGreater(resp["send_at_ms"], int(self._now_utc().timestamp() * 1000))

    def test_post_with_past_timestamp_returns_400_with_ict_message(self) -> None:
        """A timestamp comfortably in the past (more than the 60s grace
        window) returns 400 with the canonical Vietnam (ICT) error
        message."""
        past_utc = self._now_utc() - timedelta(hours=2)
        body = {
            "room_id": self.room_id,
            "as_user": self.sender_mxid,
            "send_at": _ict_iso_from_utc(past_utc),
            "message": "ghosts of the past",
        }
        channel = self.make_request(
            "POST",
            SCHEDULE_PATH,
            content=body,
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.json_body)
        # The error mentions Vietnam (ICT) so operators can recognise it.
        self.assertIn(
            "Vietnam (ICT)",
            channel.json_body.get("error", ""),
        )

    def test_list_returns_created_row(self) -> None:
        """GET /schedule lists the previously-created pending task."""
        future_utc = self._now_utc() + timedelta(hours=2)
        created = self._post_schedule({
            "room_id": self.room_id,
            "as_user": self.sender_mxid,
            "send_at": _ict_iso_from_utc(future_utc),
            "message": "queued",
        })
        task_id = created["task_id"]

        channel = self.make_request(
            "GET", SCHEDULE_PATH, custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        tasks = channel.json_body["tasks"]
        ids = [t["task_id"] for t in tasks]
        self.assertIn(task_id, ids)

    def test_patch_updates_message_and_send_at(self) -> None:
        """PATCH /schedule/{id} can update both `message` and `send_at`."""
        future_utc = self._now_utc() + timedelta(hours=2)
        created = self._post_schedule({
            "room_id": self.room_id,
            "as_user": self.sender_mxid,
            "send_at": _ict_iso_from_utc(future_utc),
            "message": "v1",
        })
        task_id = created["task_id"]

        new_future_utc = self._now_utc() + timedelta(hours=3)
        channel = self.make_request(
            "PATCH",
            f"{SCHEDULE_PATH}/{task_id}",
            content={
                "message": "v2",
                "send_at": _ict_iso_from_utc(new_future_utc),
            },
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        task = channel.json_body["task"]
        self.assertEqual(task["task_id"], task_id)
        self.assertEqual(task["message"], "v2")
        # Send-at-ms should reflect the new timestamp.
        expected_ms = int(new_future_utc.timestamp() * 1000)
        # Allow ~1s slack since the API stores millisecond-precision rounds.
        self.assertAlmostEqual(task["send_at_ms"], expected_ms, delta=2000)

    def test_delete_cancels_and_removes_row(self) -> None:
        """DELETE /schedule/{id} removes the row from the pending list."""
        future_utc = self._now_utc() + timedelta(hours=4)
        created = self._post_schedule({
            "room_id": self.room_id,
            "as_user": self.sender_mxid,
            "send_at": _ict_iso_from_utc(future_utc),
            "message": "to be cancelled",
        })
        task_id = created["task_id"]

        channel = self.make_request(
            "DELETE",
            f"{SCHEDULE_PATH}/{task_id}",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["removed"])

        # The row is gone from the pending list.
        channel = self.make_request(
            "GET", SCHEDULE_PATH, custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200)
        ids = [t["task_id"] for t in channel.json_body["tasks"]]
        self.assertNotIn(task_id, ids)

    def test_image_body_roundtrips_through_create_and_list(self) -> None:
        """The new `image_body` column (delta 95/02) is preserved on
        POST and visible in the GET listing."""
        future_utc = self._now_utc() + timedelta(hours=5)
        created = self._post_schedule({
            "room_id": self.room_id,
            "as_user": self.sender_mxid,
            "send_at": _ict_iso_from_utc(future_utc),
            "image_mxc": "mxc://test/some-image",
            "image_body": "cat.png",
        })
        task_id = created["task_id"]

        channel = self.make_request(
            "GET", SCHEDULE_PATH, custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        rows = {t["task_id"]: t for t in channel.json_body["tasks"]}
        self.assertIn(task_id, rows)
        row = rows[task_id]
        self.assertEqual(row.get("image_mxc"), "mxc://test/some-image")
        self.assertEqual(row.get("image_body"), "cat.png")
