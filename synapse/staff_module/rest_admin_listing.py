#
# STAFF mod -- admin listing + bulk-delete + external-rooms + account create
#
# AGENT N.  Backend surface for the panel.4chats.co + acc.4chats.co
# frontends.  Four endpoints:
#
#   GET  /_synapse/staff/v1/admin/users/list
#   POST /_synapse/staff/v1/admin/users/bulk_delete
#   GET  /_synapse/staff/v1/admin/rooms/external
#   POST /_synapse/staff/v1/admin/account_create
#
# Every endpoint authenticates via `_require_staff_auth` (JWT or legacy
# secret) and emits an audit row via `_audit_record` on success.
#

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import (
    parse_boolean,
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.types import JsonDict, UserID

from .forge import fake_requester
from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.http.server import JsonResource
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers


_ALLOWED_SORT = ("creation_ts", "name", "last_seen_ts")
_ALLOWED_ORDER = ("asc", "desc")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================ 1) users list


class StaffAdminUsersListServlet(StaffRestServlet):
    """GET /admin/users/list -- paginated server-side filtered user list.

    See module docstring at the top of this file for the wire format.
    """

    PATTERNS = staff_pattern("/admin/users/list")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)

        from_ts = parse_integer(request, "from_ts", required=False)
        to_ts = parse_integer(request, "to_ts", required=False)
        search = parse_string(request, "search", required=False)
        sort = parse_string(
            request, "sort", default="creation_ts",
            allowed_values=_ALLOWED_SORT,
        )
        order = parse_string(
            request, "order", default="desc",
            allowed_values=_ALLOWED_ORDER,
        )
        limit = parse_integer(request, "limit", default=100)
        offset = parse_integer(request, "offset", default=0)
        include_deactivated = parse_boolean(
            request, "include_deactivated", default=False,
        )

        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        if offset < 0:
            offset = 0

        # `users.creation_ts` is stored in SECONDS (see schema 72 full.sql
        # and synapse/storage/databases/main/__init__.py:320 where the
        # column is multiplied by 1000 to surface as ms).  Our public API
        # speaks ms, so convert.
        from_ts_s: Optional[int] = None
        to_ts_s: Optional[int] = None
        if from_ts is not None:
            from_ts_s = int(from_ts // 1000)
        if to_ts is not None:
            to_ts_s = int(to_ts // 1000)

        sql_order = "ASC" if order == "asc" else "DESC"
        order_col_map = {
            "creation_ts": "u.creation_ts",
            "name": "u.name",
            "last_seen_ts": "last_seen_ts",
        }
        sql_order_col = order_col_map[sort]

        clauses: List[str] = []
        args: List[Any] = []
        if not include_deactivated:
            clauses.append("u.deactivated = 0")
        if from_ts_s is not None:
            clauses.append("u.creation_ts >= ?")
            args.append(from_ts_s)
        if to_ts_s is not None:
            clauses.append("u.creation_ts <= ?")
            args.append(to_ts_s)
        if search:
            # Case-insensitive substring on localpart OR displayname.
            # `u.name` is full MXID; lowercase both sides.
            term = "%" + search.lower() + "%"
            clauses.append(
                "(LOWER(u.name) LIKE ? OR LOWER(p.displayname) LIKE ?)"
            )
            args.extend([term, term])

        where_clause = ""
        if clauses:
            where_clause = "WHERE " + " AND ".join(clauses)

        # Verified join shape from synapse/storage/databases/main/__init__.py
        # `get_users_paginate_txn` (lines 300-313).  `profiles.full_user_id`
        # equals `users.name`; last_seen comes from MAX(last_seen) over
        # devices and user_ips with COALESCE.
        sql_base = (
            "FROM users AS u "
            "LEFT JOIN profiles AS p ON u.name = p.full_user_id "
            "LEFT JOIN ("
            "  SELECT user_id, MAX(last_seen) AS last_seen_ts "
            "  FROM devices GROUP BY user_id"
            ") lsd ON u.name = lsd.user_id "
            "LEFT JOIN ("
            "  SELECT user_id, MAX(last_seen) AS last_seen_ts "
            "  FROM user_ips GROUP BY user_id"
            ") lsi ON u.name = lsi.user_id "
            f"{where_clause}"
        )

        sql_count = "SELECT COUNT(*) " + sql_base
        sql_list = (
            "SELECT u.name, p.displayname, u.creation_ts, u.deactivated, "
            "u.admin, COALESCE(lsd.last_seen_ts, lsi.last_seen_ts) "
            "AS last_seen_ts "
            + sql_base
            + f" ORDER BY {sql_order_col} {sql_order}, u.name ASC "
              "LIMIT ? OFFSET ?"
        )

        list_args = list(args) + [limit, offset]

        def _txn(txn) -> Tuple[int, List[Dict[str, Any]]]:
            txn.execute(sql_count, tuple(args))
            row = txn.fetchone()
            total = int(row[0]) if row and row[0] is not None else 0

            txn.execute(sql_list, tuple(list_args))
            rows = []
            for r in txn.fetchall():
                creation_ts_s = r[2]
                rows.append({
                    "user_id": r[0],
                    "display_name": r[1],
                    "creation_ts": (
                        int(creation_ts_s) * 1000
                        if creation_ts_s is not None else None
                    ),
                    "deactivated": bool(r[3]),
                    "is_admin": bool(r[4]),
                    "last_seen_ts": int(r[5]) if r[5] is not None else None,
                })
            return total, rows

        total, rows = await self.store._db_pool.runInteraction(
            "staff_admin_users_list", _txn,
        )

        # Enrich with the in-memory `is_staff` flag.  Never trust a
        # client-supplied is_staff filter -- this is always recomputed
        # server-side from the allow-list.
        for r in rows:
            r["is_staff"] = self.store.is_staff_user(r["user_id"])

        body = {
            "users": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body=None, target=None,
        )
        return 200, body


# ====================================================== 2) bulk delete users


class StaffAdminBulkDeleteServlet(StaffRestServlet):
    """POST /admin/users/bulk_delete -- concurrent deactivation with a
    semaphore-bounded fan-out.
    """

    PATTERNS = staff_pattern("/admin/users/bulk_delete")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        body = parse_json_object_from_request(request)

        user_ids = body.get("user_ids")
        if not isinstance(user_ids, list) or not user_ids:
            raise SynapseError(
                400, "user_ids must be a non-empty list of MXIDs",
            )
        if not all(isinstance(u, str) for u in user_ids):
            raise SynapseError(400, "user_ids entries must be strings")

        # Validate every MXID up front; reject the whole request on any
        # bad input (per the spec).
        invalid = [u for u in user_ids if not UserID.is_valid(u)]
        if invalid:
            raise SynapseError(
                400, f"invalid MXID(s): {invalid[:5]}",
            )

        parallelism_raw = body.get("parallelism", 8)
        if not isinstance(parallelism_raw, int) or isinstance(
            parallelism_raw, bool,
        ):
            raise SynapseError(400, "parallelism must be an integer")
        if parallelism_raw < 1 or parallelism_raw > 32:
            raise SynapseError(400, "parallelism must be in 1..32")
        parallelism = int(parallelism_raw)

        erase = bool(body.get("erase", True))

        deactivate_handler = self.hs.get_deactivate_account_handler()
        semaphore = asyncio.Semaphore(parallelism)
        # Dedupe so a duplicate MXID in the request doesn't race itself.
        # Preserve first-occurrence order for the response.
        seen: set[str] = set()
        ordered: List[str] = []
        for u in user_ids:
            if u not in seen:
                seen.add(u)
                ordered.append(u)

        results_by_user: Dict[str, Dict[str, Any]] = {}

        async def _one(mxid: str) -> None:
            async with semaphore:
                # === AGENT N === self-guard, mirrors rest_users.py:211.
                if self.store.is_staff_user(mxid):
                    results_by_user[mxid] = {
                        "user_id": mxid,
                        "status": "skipped_staff",
                        "reason": "user is in staff allowlist",
                    }
                    return
                # === END AGENT N ===
                try:
                    requester = fake_requester(self.hs, mxid)
                    await deactivate_handler.deactivate_account(
                        mxid,
                        erase_data=erase,
                        requester=requester,
                        by_admin=True,
                    )
                    results_by_user[mxid] = {
                        "user_id": mxid,
                        "status": "deleted",
                    }
                except Exception as e:
                    logger.warning(
                        "STAFF: bulk_delete %s failed: %r", mxid, e,
                    )
                    results_by_user[mxid] = {
                        "user_id": mxid,
                        "status": "error",
                        "reason": str(e),
                    }

        started = _now_ms()
        await asyncio.gather(*(_one(u) for u in ordered))
        duration_ms = _now_ms() - started

        results = [results_by_user[u] for u in ordered]
        deleted = sum(1 for r in results if r["status"] == "deleted")
        skipped = sum(1 for r in results if r["status"] == "skipped_staff")
        errored = sum(1 for r in results if r["status"] == "error")

        body_resp: JsonDict = {
            "results": results,
            "total": len(results),
            "deleted": deleted,
            "skipped": skipped,
            "errored": errored,
            "duration_ms": duration_ms,
        }

        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body={"user_ids_count": len(ordered), "erase": erase},
            target=None,
        )
        return 200, body_resp


# ===================================================== 3) external rooms


class StaffAdminExternalRoomsServlet(StaffRestServlet):
    """GET /admin/rooms/external -- rooms containing ZERO staff members.

    SQL strategy:
      * Set B = rooms with at least one current join (any user).
      * Set A = rooms with at least one current join from a staff user.
      * External = B - A.
    Then for each external room, pull name + create_ts + member list +
    message_count.  Pagination is applied at the SQL level on the
    external-room id list (LIMIT/OFFSET on the inner query).

    is_direct heuristic:
      * Read m.room.create's content.is_direct (verified column shapes
        in events / event_json at full_schemas/72/full.sql.postgres).
      * Fall back to member_count == 2 when the flag is absent.
      * Note: Element marks DMs in `m.direct` account_data on the inviter,
        not on the room itself, so this is an APPROXIMATION.
    """

    PATTERNS = staff_pattern("/admin/rooms/external")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)

        limit = parse_integer(request, "limit", default=50)
        offset = parse_integer(request, "offset", default=0)
        search = parse_string(request, "search", required=False)

        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200
        if offset < 0:
            offset = 0

        # If for any reason the in-memory allow-list is empty, return an
        # empty page rather than enumerating every room on the server.
        staff_ids = list(self.store._staff_user_ids)
        if not staff_ids:
            await self._audit_record(
                request=request, outcome=outcome, status=200,
                body=None, target=None,
            )
            return 200, {
                "rooms": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
            }

        rows, total = await self.store.staff_list_external_rooms(
            staff_ids=staff_ids, limit=limit, offset=offset, search=search,
        )

        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body=None, target=None,
        )
        return 200, {
            "rooms": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        }


# ====================================================== 4) account create


class StaffAdminAccountCreateServlet(StaffRestServlet):
    """POST /admin/account_create -- like /create_user but echoes back the
    plaintext password so acc.4chats.co can show it on screen.
    """

    PATTERNS = staff_pattern("/admin/account_create")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        body = parse_json_object_from_request(request)

        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not username:
            raise SynapseError(400, "username is required")
        if not isinstance(password, str) or not password:
            raise SynapseError(400, "password is required")
        if "@" in username or ":" in username:
            raise SynapseError(
                400,
                "username must be a localpart only (no '@' / ':' / domain)",
            )

        auth_handler = self.hs.get_auth_handler()
        registration_handler = self.hs.get_registration_handler()

        password_hash = await auth_handler.hash(password)
        try:
            user_id = await registration_handler.register_user(
                localpart=username,
                password_hash=password_hash,
                admin=False,
                by_admin=True,
                default_display_name=None,
            )
        except SynapseError:
            raise
        except Exception as e:
            raise SynapseError(500, f"register_user failed: {e!r}")

        device_id: Optional[str] = None
        access_token: Optional[str] = None
        try:
            device_id, access_token, _, _ = (
                await registration_handler.register_device(
                    user_id=user_id,
                    device_id=None,
                    initial_display_name="staff-provisioned",
                )
            )
        except Exception as e:
            logger.warning(
                "STAFF: account_create %s ok but device/token mint failed: %r",
                user_id, e,
            )

        # Audit with target=user_id; do NOT include `password` in the
        # audit body even though body_hash is one-way (collision-free
        # equality across users would leak shared passwords -- same
        # rationale as rest_auth.StaffLoginServlet:120-126).
        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body={"username": username},
            target=user_id,
        )

        return 200, {
            "user_id": user_id,
            "username": username,
            "password": password,
            "device_id": device_id,
            "access_token": access_token,
        }


# =========================================== 5) single-user account details


class StaffAdminUserGetServlet(StaffRestServlet):
    """GET /admin/user/{mxid} -- account-level details for a single user.

    Path is singular (``/admin/user/...``) deliberately so it cannot collide
    with the plural ``/admin/users/list`` collection endpoint -- ``list`` is
    a valid localpart and would silently match a ``(?P<mxid>[^/]+)`` regex
    if we re-used the plural prefix.

    Used by the panel's external-rooms view: clicking a member opens a
    modal that calls this endpoint for the canonical account-level
    display_name / creation_ts / last_seen / status flags.  The room-level
    member row only carries the per-room display_name + joined_ts which is
    not the same thing.
    """

    PATTERNS = staff_pattern("/admin/user/(?P<mxid>[^/]+)")

    async def on_GET(self, request, mxid: str) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)

        # The MXID arrives URL-encoded (``@user%3Ahost`` style); the regex
        # group hands us the still-encoded form.  Decode before validating.
        from urllib.parse import unquote
        try:
            mxid = unquote(mxid)
        except Exception:
            raise SynapseError(400, "invalid MXID encoding")

        if not UserID.is_valid(mxid):
            raise SynapseError(400, "invalid MXID")

        # Same join shape as `/admin/users/list`'s _txn (verified against
        # synapse/storage/databases/main/__init__.py:300-313).  Single-row
        # variant: WHERE u.name = ?  rather than a paginated WHERE.
        sql = (
            "SELECT u.name, u.creation_ts, u.deactivated, u.admin, "
            "       p.displayname, p.avatar_url, "
            "       COALESCE(lsd.last_seen_ts, lsi.last_seen_ts) AS last_seen_ts "
            "FROM users AS u "
            "LEFT JOIN profiles AS p ON u.name = p.full_user_id "
            "LEFT JOIN ("
            "  SELECT user_id, MAX(last_seen) AS last_seen_ts "
            "  FROM devices GROUP BY user_id"
            ") lsd ON u.name = lsd.user_id "
            "LEFT JOIN ("
            "  SELECT user_id, MAX(last_seen) AS last_seen_ts "
            "  FROM user_ips GROUP BY user_id"
            ") lsi ON u.name = lsi.user_id "
            "WHERE u.name = ?"
        )

        def _txn(txn) -> Optional[Dict[str, Any]]:
            txn.execute(sql, (mxid,))
            r = txn.fetchone()
            if not r:
                return None
            # users.creation_ts is in SECONDS (schema 72); surface as ms.
            creation_ts_s = r[1]
            return {
                "user_id": r[0],
                "creation_ts": (
                    int(creation_ts_s) * 1000
                    if creation_ts_s is not None else None
                ),
                "deactivated": bool(r[2]),
                "is_admin": bool(r[3]),
                "display_name": r[4],
                "avatar_url": r[5],
                "last_seen_ts": int(r[6]) if r[6] is not None else None,
            }

        row = await self.store._db_pool.runInteraction(
            "staff_admin_user_get", _txn,
        )
        if row is None:
            raise SynapseError(404, "user not found")

        # In-memory staff allow-list lookup -- never trust a client flag.
        row["is_staff"] = self.store.is_staff_user(row["user_id"])

        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body=None, target=row["user_id"],
        )
        return 200, row


# ------------------------------------------------------------- registration


def register_servlets(
    hs: "HomeServer", store: "StaffStore", resource: "JsonResource",
) -> None:
    StaffAdminUsersListServlet(hs, store).register(resource)
    StaffAdminBulkDeleteServlet(hs, store).register(resource)
    StaffAdminExternalRoomsServlet(hs, store).register(resource)
    StaffAdminAccountCreateServlet(hs, store).register(resource)
    StaffAdminUserGetServlet(hs, store).register(resource)
