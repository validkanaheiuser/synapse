#
# S11 — staff_audit_log smoke tests (S3 / Agent H).
#
# The `staff_audit_log` table is created by schema delta
# `synapse/storage/schema/main/delta/95/03_auth_audit.sql`.  Agent H also
# provides `StaffStore.audit_insert` / `audit_query` helpers, and a
# `StaffAuditWriter` wrapper that the staff REST base class uses via
# `_audit_record(...)`.
#
# However, at the time S11 was written, the standard staff endpoints
# (rest_settings, rest_messages, rest_schedule, rest_users, rest_widgets,
# rest_edit) still use the legacy `_require_secret` path and do NOT call
# `_audit_record`.  Only `rest_auth` is currently wired into the audit
# pipeline, and `StaffAuditWriter` is constructed against `hs._staff_audit_writer`
# which is not yet set in `StaffModule.__init__`.
#
# So this file only smoke-tests the storage layer (table exists +
# audit_insert + audit_query work), and stubs the end-to-end "one
# successful endpoint call inserts one row" test behind `skip` until
# Agent H finishes wiring it.
#

import time
import unittest

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.staff_module.conftest import StaffHomeserverTestCase


class StaffAuditStorageTest(StaffHomeserverTestCase):
    """Smoke-tests on the `staff_audit_log` table + Store helpers."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()

    def test_audit_insert_then_query_roundtrips(self) -> None:
        """`audit_insert` writes a row that `audit_query` can read back
        with the same actor/endpoint/method/status/target fields."""
        store = self.hs._staff_store
        ts = int(time.time() * 1000)

        row_id = self.get_success(
            store.audit_insert(
                ts=ts,
                actor_user_id="@staff:test",
                actor_kind="jwt",
                endpoint="/_synapse/staff/v1/settings/update/theme",
                method="POST",
                status=200,
                target="theme",
                body_hash="deadbeef" * 8,
                ip="127.0.0.1",
            )
        )
        self.assertIsInstance(row_id, str)
        self.assertTrue(row_id)

        rows = self.get_success(
            store.audit_query(actor="@staff:test", limit=10)
        )
        self.assertTrue(rows)
        match = next((r for r in rows if r["id"] == row_id), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["actor_user_id"], "@staff:test")
        self.assertEqual(match["actor_kind"], "jwt")
        self.assertEqual(
            match["endpoint"],
            "/_synapse/staff/v1/settings/update/theme",
        )
        self.assertEqual(match["method"], "POST")
        self.assertEqual(match["status"], 200)
        self.assertEqual(match["target"], "theme")

    def test_audit_query_filters_by_endpoint(self) -> None:
        """`audit_query(endpoint=...)` returns only rows for the given
        endpoint."""
        store = self.hs._staff_store
        base_ts = int(time.time() * 1000)
        self.get_success(store.audit_insert(
            ts=base_ts, actor_user_id="@a:test", actor_kind="jwt",
            endpoint="/_synapse/staff/v1/schedule", method="POST",
            status=200, target="task-1", body_hash="", ip=None,
        ))
        self.get_success(store.audit_insert(
            ts=base_ts + 1, actor_user_id="@b:test", actor_kind="jwt",
            endpoint="/_synapse/staff/v1/widgets", method="GET",
            status=200, target=None, body_hash="", ip=None,
        ))

        rows = self.get_success(
            store.audit_query(
                endpoint="/_synapse/staff/v1/schedule", limit=10,
            )
        )
        endpoints = {r["endpoint"] for r in rows}
        self.assertEqual(endpoints, {"/_synapse/staff/v1/schedule"})


class StaffAuditEndpointHookTest(StaffHomeserverTestCase):
    """Placeholder for the end-to-end audit hook integration.

    Once `StaffModule.__init__` constructs `_staff_audit_writer` and the
    standard servlets wrap their handlers with `_audit_record`, this test
    can flip from `skip` to a real assertion that "one successful
    endpoint call inserts one row".
    """

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()

    @unittest.skip(
        "End-to-end audit hook is not wired in StaffModule.__init__ yet "
        "(staff_audit_writer attribute is not constructed). The schema and "
        "Store helpers are smoke-tested in StaffAuditStorageTest above."
    )
    def test_successful_endpoint_inserts_one_audit_row(self) -> None:
        """One successful staff API call should result in exactly one new
        row in `staff_audit_log`.  Skipped until Agent H wires the
        audit writer into `StaffModule`."""
