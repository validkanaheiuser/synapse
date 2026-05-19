#
# STAFF mod - public health endpoint (S18, Agent I).
#
# GET /_synapse/staff/v1/health -> 200 with:
#   {
#     "staff_users_count":       int,
#     "scheduled_pending":       int,
#     "scheduler_action_registered": bool,
#     "last_cache_prime_ts":     int | None,  # ms-epoch
#     "schema_version":          int | None,
#     "version":                 "staff-mod/1.0"
#   }
#
# Intentionally public (no `_require_secret`) so monitoring systems can poll
# it without secrets sloshing through scrape configs.  Nothing exposed here
# leaks identifying information.
#

import logging
from typing import TYPE_CHECKING, Tuple

from synapse.types import JsonDict

from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


STAFF_MOD_VERSION = "staff-mod/1.0"


class StaffHealthServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/health")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        # NB: NO `_require_secret(request)` here - public on purpose (S18).

        # Number of allow-listed staff users (cheap in-memory read).
        try:
            staff_users_count = len(self.store._staff_user_ids)
        except Exception:
            staff_users_count = -1

        # Pending scheduled messages (rows whose send_at_ms is still in the
        # future).  Cheap COUNT query.
        scheduled_pending = -1
        try:
            scheduled_pending = await _count_pending(self.hs)
        except Exception:
            logger.debug("STAFF health: count_pending failed", exc_info=True)

        # Did `register_scheduler` succeed?  We tagged that on the staff
        # module object during init.
        scheduler_action_registered = bool(
            getattr(self.hs, "_staff_scheduler_action_registered", False)
        )

        # Last successful cache prime — set by store.prime_staff_cache.
        last_cache_prime_ts = getattr(
            self.store, "_last_cache_prime_ts", None,
        )

        # Schema version, if discoverable.
        schema_version = await _schema_version(self.hs)

        return 200, {
            "staff_users_count": staff_users_count,
            "scheduled_pending": scheduled_pending,
            "scheduler_action_registered": scheduler_action_registered,
            "last_cache_prime_ts": last_cache_prime_ts,
            "schema_version": schema_version,
            "version": STAFF_MOD_VERSION,
        }


async def _count_pending(hs: "HomeServer") -> int:
    db = hs.get_datastores().main.db_pool

    def _q(txn) -> int:
        txn.execute(
            "SELECT COUNT(*) FROM staff_scheduled_messages "
            "WHERE send_at_ms > ?",
            (int(hs.get_clock().time_msec()),),
        )
        row = txn.fetchone()
        return int(row[0]) if row else 0

    return await db.runInteraction("staff_health_count_pending", _q)


async def _schema_version(hs: "HomeServer") -> "int | None":
    try:
        db = hs.get_datastores().main.db_pool

        def _q(txn):
            txn.execute("SELECT MAX(version) FROM schema_version")
            row = txn.fetchone()
            return int(row[0]) if row and row[0] is not None else None

        return await db.runInteraction("staff_health_schema_version", _q)
    except Exception:
        return None


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffHealthServlet(hs, store).register(resource)
