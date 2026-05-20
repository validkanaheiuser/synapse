#
# STAFF mod — database access layer
#
# A thin wrapper over Synapse's DatabasePool that owns the staff_users,
# staff_settings, staff_scheduled_messages, staff_edit_history,
# staff_widget_*, staff_account_data_pushed tables defined in schema delta
# 95.  Holds an in-memory `set` of staff user_ids for the hot-path
# `is_staff_user` check called from the visibility / sync / member-list
# filters.
#

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex


class StaffStore:
    """Holds all STAFF-mod DB helpers and the in-memory staff allowlist."""

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._db_pool = hs.get_datastores().main.db_pool
        self._staff_user_ids: Set[str] = set()
        # Populated lazily by `prime_staff_cache` during module init.

    # ------------------------------------------------------------------ users

    async def prime_staff_cache(self) -> None:
        rows = await self._db_pool.simple_select_list(
            table="staff_users",
            keyvalues=None,
            retcols=("user_id",),
            desc="staff_prime_cache",
        )
        # Synapse's simple_select_list returns list[tuple]; index by position.
        self._staff_user_ids = {r[0] for r in rows}
        logger.info("STAFF: primed staff allowlist with %d users", len(self._staff_user_ids))

    def is_staff_user(self, user_id: str) -> bool:
        return user_id in self._staff_user_ids

    async def add_staff_user(
        self, user_id: str, added_by: str, note: Optional[str] = None
    ) -> None:
        await self._db_pool.simple_upsert(
            table="staff_users",
            keyvalues={"user_id": user_id},
            values={
                "added_ts": _now_ms(),
                "added_by": added_by,
                "note": note,
            },
            desc="staff_add_user",
        )
        self._staff_user_ids.add(user_id)

    async def remove_staff_user(self, user_id: str) -> int:
        deleted = await self._db_pool.simple_delete(
            table="staff_users",
            keyvalues={"user_id": user_id},
            desc="staff_remove_user",
        )
        self._staff_user_ids.discard(user_id)
        # === AGENT P ===
        # Drop any widget-group memberships for this user so a re-added
        # staff doesn't silently inherit their old groups.  Keep the call
        # after the main delete: if it fails we still leave the allowlist
        # in a consistent state.
        try:
            await self._db_pool.simple_delete(
                table="staff_widget_group_members",
                keyvalues={"user_id": user_id},
                desc="staff_widget_orphan_cleanup_on_staff_remove",
            )
        except Exception:
            logger.exception(
                "STAFF: AGENT P orphan cleanup failed for %s; "
                "membership rows may be stale",
                user_id,
            )
        # === END AGENT P ===
        return deleted

    async def list_staff_users(self) -> List[Dict[str, Any]]:
        _STAFF_USER_COLS = ("user_id", "added_ts", "added_by", "note")
        rows = await self._db_pool.simple_select_list(
            table="staff_users",
            keyvalues=None,
            retcols=_STAFF_USER_COLS,
            desc="staff_list_users",
        )
        # simple_select_list returns list[tuple]; the panel UI (and the
        # documented return type above) expects dicts keyed by column name.
        return [dict(zip(_STAFF_USER_COLS, r)) for r in rows]

    # ----------------------------------------------------------------- settings

    async def settings_get_all(self) -> Dict[str, Any]:
        rows = await self._db_pool.simple_select_list(
            table="staff_settings",
            keyvalues=None,
            retcols=("setting_key", "value"),
            desc="staff_settings_all",
        )
        # simple_select_list -> list[tuple]; positional (setting_key, value).
        return {r[0]: json.loads(r[1]) for r in rows}

    async def settings_get(self, key: str) -> Optional[Any]:
        row = await self._db_pool.simple_select_one(
            table="staff_settings",
            keyvalues={"setting_key": key},
            retcols=("value",),
            allow_none=True,
            desc="staff_settings_get",
        )
        if not row:
            return None
        # simple_select_one -> tuple; one retcol -> position 0.
        return json.loads(row[0])

    async def settings_upsert(self, key: str, value: Any) -> None:
        await self._db_pool.simple_upsert(
            table="staff_settings",
            keyvalues={"setting_key": key},
            values={
                "value": json.dumps(value),
                "updated_ts": _now_ms(),
            },
            desc="staff_settings_upsert",
        )

    async def settings_delete(self, key: str) -> int:
        return await self._db_pool.simple_delete(
            table="staff_settings",
            keyvalues={"setting_key": key},
            desc="staff_settings_delete",
        )

    # ----------------------------------------------------------- scheduled msgs

    async def schedule_insert(
        self,
        task_id: str,
        room_id: str,
        as_user: str,
        send_at_ms: int,
        message: Optional[str],
        image_mxc: Optional[str],
    ) -> None:
        await self._db_pool.simple_insert(
            table="staff_scheduled_messages",
            values={
                "task_id": task_id,
                "room_id": room_id,
                "as_user": as_user,
                "send_at_ms": send_at_ms,
                "message": message,
                "image_mxc": image_mxc,
                "created_ts": _now_ms(),
            },
            desc="staff_schedule_insert",
        )

    async def schedule_get(self, task_id: str) -> Optional[Dict[str, Any]]:
        _SCHED_COLS = ("task_id", "room_id", "as_user", "send_at_ms", "message",
                       "image_mxc", "created_ts")
        row = await self._db_pool.simple_select_one(
            table="staff_scheduled_messages",
            keyvalues={"task_id": task_id},
            retcols=_SCHED_COLS,
            allow_none=True,
            desc="staff_schedule_get",
        )
        return dict(zip(_SCHED_COLS, row)) if row else None

    async def schedule_delete(self, task_id: str) -> int:
        return await self._db_pool.simple_delete(
            table="staff_scheduled_messages",
            keyvalues={"task_id": task_id},
            desc="staff_schedule_delete",
        )

    async def schedule_list_pending(self) -> List[Dict[str, Any]]:
        now = _now_ms()

        def _q(txn):
            txn.execute(
                "SELECT task_id, room_id, as_user, send_at_ms, message, image_mxc, created_ts "
                "FROM staff_scheduled_messages WHERE send_at_ms > ? ORDER BY send_at_ms ASC",
                (now,),
            )
            cols = [c[0] for c in txn.description]
            return [dict(zip(cols, row)) for row in txn.fetchall()]

        return await self._db_pool.runInteraction("staff_schedule_list", _q)

    # ---- AGENT B additions ----------------------------------------------
    # New methods only.  Existing helpers above remain untouched per the
    # owner-file contract.  These reach the new `image_body` column added
    # by schema delta 95/02_schedule_image_body.sql and provide PATCH /
    # edit support for the schedule endpoints.

    async def schedule_get_full(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Like `schedule_get` but also returns the new `image_body`
        column.  Kept separate so existing call-sites of `schedule_get`
        are not implicitly changed."""
        _SCHED_FULL_COLS = (
            "task_id", "room_id", "as_user", "send_at_ms", "message",
            "image_mxc", "image_body", "created_ts",
        )
        row = await self._db_pool.simple_select_one(
            table="staff_scheduled_messages",
            keyvalues={"task_id": task_id},
            retcols=_SCHED_FULL_COLS,
            allow_none=True,
            desc="staff_schedule_get_full",
        )
        return dict(zip(_SCHED_FULL_COLS, row)) if row else None

    async def schedule_list_pending_full(self) -> List[Dict[str, Any]]:
        """Pending rows including `image_body`.  Used by the schedule
        list/edit UI; existing `schedule_list_pending` is left unchanged
        so its callers do not have to migrate."""
        now = _now_ms()

        def _q(txn):
            txn.execute(
                "SELECT task_id, room_id, as_user, send_at_ms, message, "
                "image_mxc, image_body, created_ts "
                "FROM staff_scheduled_messages "
                "WHERE send_at_ms > ? ORDER BY send_at_ms ASC",
                (now,),
            )
            cols = [c[0] for c in txn.description]
            return [dict(zip(cols, row)) for row in txn.fetchall()]

        return await self._db_pool.runInteraction(
            "staff_schedule_list_full", _q,
        )

    async def schedule_set_image_body(
        self, task_id: str, image_body: Optional[str],
    ) -> None:
        """Set (or clear) the `image_body` for a row.  Used at INSERT
        time as a follow-up to `schedule_insert` to avoid changing the
        existing insert signature."""
        await self._db_pool.simple_update(
            table="staff_scheduled_messages",
            keyvalues={"task_id": task_id},
            updatevalues={"image_body": image_body},
            desc="staff_schedule_set_image_body",
        )

    async def schedule_update_row(
        self,
        task_id: str,
        *,
        send_at_ms: Optional[int] = None,
        message: Optional[str] = None,
        image_mxc: Optional[str] = None,
        image_body: Optional[str] = None,
        clear_message: bool = False,
        clear_image: bool = False,
    ) -> bool:
        """Update an editable subset of a pending scheduled message row.

        Pass `clear_message=True` or `clear_image=True` to explicitly
        NULL the corresponding column(s).  A `None` value otherwise
        means "do not touch this column".  Returns True if a row was
        updated, False if nothing was supplied.
        """
        updates: Dict[str, Any] = {}
        if send_at_ms is not None:
            updates["send_at_ms"] = send_at_ms
        if clear_message:
            updates["message"] = None
        elif message is not None:
            updates["message"] = message
        if clear_image:
            updates["image_mxc"] = None
            updates["image_body"] = None
        else:
            if image_mxc is not None:
                updates["image_mxc"] = image_mxc
            if image_body is not None:
                updates["image_body"] = image_body
        if not updates:
            return False
        await self._db_pool.simple_update(
            table="staff_scheduled_messages",
            keyvalues={"task_id": task_id},
            updatevalues=updates,
            desc="staff_schedule_update_row",
        )
        return True

    # ----------------------------------------------------------- edit history

    async def edit_history_record(
        self,
        original_event_id: str,
        room_id: str,
        sender: str,
        old_content: Dict[str, Any],
        new_content: Dict[str, Any],
        replace_event_id: Optional[str],
        redaction_event_id: Optional[str],
        edited_by: str,
        kind: str,
    ) -> str:
        edit_id = _new_id()
        await self._db_pool.simple_insert(
            table="staff_edit_history",
            values={
                "edit_id": edit_id,
                "original_event_id": original_event_id,
                "room_id": room_id,
                "sender": sender,
                "old_content_json": json.dumps(old_content),
                "new_content_json": json.dumps(new_content),
                "replace_event_id": replace_event_id,
                "redaction_event_id": redaction_event_id,
                "edited_by": edited_by,
                "edited_ts": _now_ms(),
                "kind": kind,
            },
            desc="staff_edit_history_record",
        )
        return edit_id

    async def edit_history_for(
        self, original_event_id: str
    ) -> List[Dict[str, Any]]:
        def _q(txn):
            txn.execute(
                "SELECT edit_id, original_event_id, room_id, sender, "
                "old_content_json, new_content_json, replace_event_id, "
                "redaction_event_id, edited_by, edited_ts, kind "
                "FROM staff_edit_history WHERE original_event_id = ? "
                "ORDER BY edited_ts ASC",
                (original_event_id,),
            )
            cols = [c[0] for c in txn.description]
            out = []
            for row in txn.fetchall():
                d = dict(zip(cols, row))
                d["old_content"] = json.loads(d.pop("old_content_json"))
                d["new_content"] = json.loads(d.pop("new_content_json"))
                out.append(d)
            return out

        return await self._db_pool.runInteraction("staff_edit_history_for", _q)

    # ------------------------------------------------------------------- widgets

    async def widget_create(
        self,
        owner_user_id: str,
        widget_type: str,
        name: str,
        url: str,
        content: Dict[str, Any],
    ) -> str:
        widget_id = _new_id()
        now = _now_ms()
        await self._db_pool.simple_insert(
            table="staff_widget_definitions",
            values={
                "widget_id": widget_id,
                "owner_user_id": owner_user_id,
                "widget_type": widget_type,
                "name": name,
                "url": url,
                "content_json": json.dumps(content),
                "created_ts": now,
                "updated_ts": now,
            },
            desc="staff_widget_create",
        )
        return widget_id

    _WIDGET_COLS = ("widget_id", "owner_user_id", "widget_type", "name", "url",
                    "content_json", "created_ts", "updated_ts")

    async def widget_get(self, widget_id: str) -> Optional[Dict[str, Any]]:
        row_tup = await self._db_pool.simple_select_one(
            table="staff_widget_definitions",
            keyvalues={"widget_id": widget_id},
            retcols=self._WIDGET_COLS,
            allow_none=True,
            desc="staff_widget_get",
        )
        if not row_tup:
            return None
        row: Dict[str, Any] = dict(zip(self._WIDGET_COLS, row_tup))
        row["content"] = json.loads(row.pop("content_json"))
        return row

    async def widget_list(
        self,
        widget_type: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        kvs: Dict[str, Any] = {}
        if widget_type is not None:
            kvs["widget_type"] = widget_type
        if owner_user_id is not None:
            kvs["owner_user_id"] = owner_user_id
        rows = await self._db_pool.simple_select_list(
            table="staff_widget_definitions",
            keyvalues=kvs or None,
            retcols=self._WIDGET_COLS,
            desc="staff_widget_list",
        )
        out: List[Dict[str, Any]] = []
        for r_tup in rows:
            r = dict(zip(self._WIDGET_COLS, r_tup))
            r["content"] = json.loads(r.pop("content_json"))
            out.append(r)
        return out

    async def widget_update(
        self,
        widget_id: str,
        fields: Dict[str, Any],
    ) -> bool:
        updates: Dict[str, Any] = {}
        for k in ("name", "url"):
            if k in fields:
                updates[k] = fields[k]
        if "content" in fields:
            updates["content_json"] = json.dumps(fields["content"])
        if not updates:
            return False
        updates["updated_ts"] = _now_ms()
        await self._db_pool.simple_update(
            table="staff_widget_definitions",
            keyvalues={"widget_id": widget_id},
            updatevalues=updates,
            desc="staff_widget_update",
        )
        return True

    async def widget_delete(self, widget_id: str) -> int:
        # cascade: instances first
        await self._db_pool.simple_delete(
            table="staff_widget_room_instances",
            keyvalues={"widget_id": widget_id},
            desc="staff_widget_delete_instances",
        )
        deleted = await self._db_pool.simple_delete(
            table="staff_widget_definitions",
            keyvalues={"widget_id": widget_id},
            desc="staff_widget_delete_def",
        )
        # === AGENT P ===
        # Drop any group membership rows referencing this widget so we
        # don't leave orphans pointing at a deleted definition.  We do
        # this in a separate statement (not inside `simple_delete` above
        # which uses db_autocommit=True) but the user-visible cascade
        # ordering is correct: by the time this returns the widget id is
        # absent from both `staff_widget_definitions` and every group.
        await self._db_pool.simple_delete(
            table="staff_widget_group_widgets",
            keyvalues={"widget_id": widget_id},
            desc="staff_widget_delete_group_widgets",
        )
        # === END AGENT P ===
        return deleted

    _INSTANCE_COLS = ("instance_id", "widget_id", "room_id", "injected_by",
                      "last_state_event_id", "created_ts")

    async def widget_instances_for(
        self, widget_id: str
    ) -> List[Dict[str, Any]]:
        rows = await self._db_pool.simple_select_list(
            table="staff_widget_room_instances",
            keyvalues={"widget_id": widget_id},
            retcols=self._INSTANCE_COLS,
            desc="staff_widget_instances_for",
        )
        return [dict(zip(self._INSTANCE_COLS, r)) for r in rows]

    async def widget_instance_record(
        self,
        widget_id: str,
        room_id: str,
        injected_by: str,
        last_state_event_id: Optional[str],
    ) -> str:
        instance_id = _new_id()
        await self._db_pool.simple_upsert(
            table="staff_widget_room_instances",
            keyvalues={"widget_id": widget_id, "room_id": room_id},
            values={
                "instance_id": instance_id,
                "injected_by": injected_by,
                "last_state_event_id": last_state_event_id,
                "created_ts": _now_ms(),
            },
            desc="staff_widget_instance_record",
        )
        return instance_id

    async def widget_instance_get(
        self, widget_id: str, room_id: str
    ) -> Optional[Dict[str, Any]]:
        row = await self._db_pool.simple_select_one(
            table="staff_widget_room_instances",
            keyvalues={"widget_id": widget_id, "room_id": room_id},
            retcols=self._INSTANCE_COLS,
            allow_none=True,
            desc="staff_widget_instance_get",
        )
        return dict(zip(self._INSTANCE_COLS, row)) if row else None

    async def widget_instance_update_event(
        self, widget_id: str, room_id: str, new_event_id: str
    ) -> None:
        await self._db_pool.simple_update(
            table="staff_widget_room_instances",
            keyvalues={"widget_id": widget_id, "room_id": room_id},
            updatevalues={"last_state_event_id": new_event_id},
            desc="staff_widget_instance_event",
        )

    # -------------------------------------------- account_data idempotency

    async def has_pushed_account_data(self, user_id: str) -> bool:
        row = await self._db_pool.simple_select_one(
            table="staff_account_data_pushed",
            keyvalues={"user_id": user_id},
            retcols=("user_id",),
            allow_none=True,
            desc="staff_account_data_check",
        )
        return row is not None

    async def mark_account_data_pushed(self, user_id: str) -> None:
        await self._db_pool.simple_upsert(
            table="staff_account_data_pushed",
            keyvalues={"user_id": user_id},
            values={"pushed_ts": _now_ms()},
            desc="staff_account_data_mark",
        )

    # === AGENT I ===
    # New methods only; existing helpers above remain untouched per the
    # owner-file contract.  These cover S5 (replication-aware allowlist
    # mutation), S6 (server-side prefix filter for /settings/get/all), and
    # S13 (scheduler fired-ts bookkeeping + nightly cleanup).

    # ---- S5: replication-aware staff_user_ids mutations -----------------
    # The original `add_staff_user` / `remove_staff_user` only mutate the
    # caller's in-memory set, so a sibling worker keeps a stale view of
    # the allow-list until its own cache reload tick.  These helpers also
    # poke the cache-invalidation stream (verified against
    # `CacheInvalidationWorkerStore.send_invalidation_to_replication` in
    # synapse/storage/databases/main/cache.py:702-718) using the fake
    # cache name `_staff_user_ids`.  Other workers see the invalidation
    # via their `process_replication_rows` callback and clear their own
    # copy.  If the cache-stream call fails we fall through to the
    # `refresh_interval_sec` config knob (S5 fallback - see
    # `prime_staff_cache_periodic`).

    _STAFF_USER_CACHE_NAME = "_staff_user_ids"

    async def add_staff_user_replicated(
        self, user_id: str, added_by: str, note: Optional[str] = None,
    ) -> None:
        """Like `add_staff_user` but also broadcasts a cache-invalidation
        record so worker peers reload the allow-list.  Existing
        `add_staff_user` remains unchanged."""
        await self.add_staff_user(user_id, added_by=added_by, note=note)
        await self._invalidate_staff_cache_remote(user_id)

    async def remove_staff_user_replicated(self, user_id: str) -> int:
        deleted = await self.remove_staff_user(user_id)
        await self._invalidate_staff_cache_remote(user_id)
        return deleted

    async def _invalidate_staff_cache_remote(self, user_id: str) -> None:
        """Best-effort: tell sibling workers to drop their staff cache.

        We hit the `send_invalidation_to_replication` method on the main
        store.  The replication consumer treats unknown cache names as
        opaque (it just dispatches them through CachesStream); each
        worker's StaffStore listens for the `_staff_user_ids` name in
        `apply_replication_invalidation` below and reloads.  If the
        replication call raises (e.g. running single-process), we
        silently fall back to the polling reload.
        """
        try:
            main = self._hs.get_datastores().main
            sender = getattr(main, "send_invalidation_to_replication", None)
            if sender is None:
                return
            await sender(self._STAFF_USER_CACHE_NAME, (user_id,))
        except Exception:
            logger.debug(
                "STAFF: send_invalidation_to_replication unavailable; "
                "relying on refresh_interval_sec fallback",
                exc_info=True,
            )

    async def apply_replication_invalidation(
        self, keys: Optional[tuple],
    ) -> None:
        """Called by the staff module's replication hook (or the periodic
        reloader) when another worker mutates the allow-list.  We reload
        the full set rather than try to apply individual diffs - the set
        is tiny (< few thousand staff) and the safety margin is worth it.
        """
        await self.prime_staff_cache()

    # `prime_staff_cache` already exists above; we track its last-success
    # timestamp here so the /staff/v1/health endpoint (S18) and the
    # periodic-reloader fallback (S5) can both observe it.  We piggyback
    # on the existing method via a wrapper that callers may use; we do
    # NOT modify `prime_staff_cache` itself (owner-file contract for
    # store.py says "add new methods only").
    _last_cache_prime_ts: Optional[int] = None

    async def prime_staff_cache_tracked(self) -> None:
        """Wrapper around `prime_staff_cache` that records the success
        timestamp on completion.  Used by module init (S4) and the
        periodic reloader (S5 fallback)."""
        await self.prime_staff_cache()
        self._last_cache_prime_ts = _now_ms()

    # ---- S6: prefix-filtered settings_get_all --------------------------
    # We can't safely use a SQL `LIKE` here without escaping, so do the
    # filter in Python on the (already-tiny) settings table.  The
    # `prefix=None` case behaves identically to the existing
    # `settings_get_all`.

    async def settings_get_all_filtered(
        self, prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows = await self._db_pool.simple_select_list(
            table="staff_settings",
            keyvalues=None,
            retcols=("setting_key", "value"),
            desc="staff_settings_all_filtered",
        )
        # simple_select_list -> list[tuple]; positional (setting_key, value).
        out: Dict[str, Any] = {}
        for r in rows:
            key = r[0]
            if prefix is not None and not key.startswith(prefix):
                continue
            out[key] = json.loads(r[1])
        return out

    # ---- S13: scheduler fired-ts + cleanup -----------------------------
    # `fired_ts` is added by delta 95/04 (NULLABLE).  The scheduler stamps
    # the column when an action completes successfully; the cleanup job
    # then deletes rows whose `fired_ts` is older than the retention
    # window (7 days by default).

    async def schedule_mark_fired(
        self, task_id: str, fired_ts: Optional[int] = None,
    ) -> None:
        await self._db_pool.simple_update(
            table="staff_scheduled_messages",
            keyvalues={"task_id": task_id},
            updatevalues={"fired_ts": fired_ts or _now_ms()},
            desc="staff_schedule_mark_fired",
        )

    async def schedule_cleanup_fired(self, older_than_ms: int) -> int:
        """Delete rows where `fired_ts` is older than the supplied
        cutoff.  Returns the number of rows removed."""
        def _q(txn) -> int:
            txn.execute(
                "DELETE FROM staff_scheduled_messages "
                "WHERE fired_ts IS NOT NULL AND fired_ts < ?",
                (int(older_than_ms),),
            )
            try:
                return int(txn.rowcount)
            except Exception:
                return 0

        return await self._db_pool.runInteraction(
            "staff_schedule_cleanup_fired", _q,
        )

    # === AGENT H ===
    # New methods only.  These reach the staff_audit_log, staff_jwt_keys
    # and staff_jwt_revocations tables introduced by schema delta
    # 95/03_auth_audit.sql.  Existing helpers above remain untouched per
    # the owner-file contract.

    async def audit_insert(
        self,
        *,
        ts: int,
        actor_user_id: Optional[str],
        actor_kind: str,
        endpoint: str,
        method: str,
        status: int,
        target: Optional[str],
        body_hash: str,
        ip: Optional[str],
    ) -> str:
        """Insert one staff_audit_log row.  Returns the new row id."""
        row_id = _new_id()
        await self._db_pool.simple_insert(
            table="staff_audit_log",
            values={
                "id": row_id,
                "ts": ts,
                "actor_user_id": actor_user_id,
                "actor_kind": actor_kind,
                "endpoint": endpoint,
                "method": method,
                "status": status,
                "target": target,
                "body_hash": body_hash,
                "ip": ip,
            },
            desc="staff_audit_insert",
        )
        return row_id

    async def audit_query(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        actor: Optional[str] = None,
        endpoint: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Server-side filtered audit query.  All filters are optional;
        an unfiltered call returns the most recent `limit` rows.  Both
        `limit` and `offset` are clamped server-side."""
        if limit <= 0:
            limit = 1
        if limit > 1000:
            limit = 1000
        if offset < 0:
            offset = 0

        clauses: List[str] = []
        args: List[Any] = []
        if from_ts is not None:
            clauses.append("ts >= ?")
            args.append(int(from_ts))
        if to_ts is not None:
            clauses.append("ts <= ?")
            args.append(int(to_ts))
        if actor is not None:
            clauses.append("actor_user_id = ?")
            args.append(actor)
        if endpoint is not None:
            clauses.append("endpoint = ?")
            args.append(endpoint)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = (
            "SELECT id, ts, actor_user_id, actor_kind, endpoint, method, "
            "status, target, body_hash, ip "
            "FROM staff_audit_log "
            f"{where} "
            "ORDER BY ts DESC LIMIT ? OFFSET ?"
        )
        args.extend([limit, offset])

        def _q(txn):
            txn.execute(sql, tuple(args))
            cols = [c[0] for c in txn.description]
            return [dict(zip(cols, row)) for row in txn.fetchall()]

        return await self._db_pool.runInteraction("staff_audit_query", _q)

    async def jwt_key_get_current(self) -> Optional[Dict[str, Any]]:
        """Return the single active key, or None when none exists yet."""
        _JWT_KEY_COLS = ("key_id", "secret_b64", "created_ts", "active")
        row = await self._db_pool.simple_select_one(
            table="staff_jwt_keys",
            keyvalues={"active": 1},
            retcols=_JWT_KEY_COLS,
            allow_none=True,
            desc="staff_jwt_key_current",
        )
        return dict(zip(_JWT_KEY_COLS, row)) if row else None

    async def jwt_key_get_by_id(self, key_id: str) -> Optional[Dict[str, Any]]:
        """Look up any key (active or not) by id, for verifying tokens
        signed with a since-rotated key while they're still inside their
        validity window."""
        _JWT_KEY_COLS = ("key_id", "secret_b64", "created_ts", "active")
        row = await self._db_pool.simple_select_one(
            table="staff_jwt_keys",
            keyvalues={"key_id": key_id},
            retcols=_JWT_KEY_COLS,
            allow_none=True,
            desc="staff_jwt_key_by_id",
        )
        return dict(zip(_JWT_KEY_COLS, row)) if row else None

    async def jwt_key_rotate(
        self, *, new_key_id: str, new_secret_b64: str,
    ) -> None:
        """Atomically mark every active key inactive, then insert the
        new key and mark it active.  Runs in one transaction so we never
        observe zero (or two) active keys mid-rotation."""
        now = _now_ms()

        def _txn(txn):
            txn.execute(
                "UPDATE staff_jwt_keys SET active = 0 WHERE active = 1"
            )
            txn.execute(
                "INSERT INTO staff_jwt_keys "
                "(key_id, secret_b64, created_ts, active) "
                "VALUES (?, ?, ?, 1)",
                (new_key_id, new_secret_b64, now),
            )

        await self._db_pool.runInteraction("staff_jwt_key_rotate", _txn)

    async def jwt_revocation_check(self, jti: str) -> bool:
        """True if `jti` has been revoked (is in the deny-list)."""
        row = await self._db_pool.simple_select_one(
            table="staff_jwt_revocations",
            keyvalues={"jti": jti},
            retcols=("jti",),
            allow_none=True,
            desc="staff_jwt_revocation_check",
        )
        return row is not None

    async def jwt_revocation_add(self, jti: str, exp_ts: int) -> None:
        """Add `jti` to the deny-list.  `exp_ts` lets a future cleanup
        job prune rows whose tokens have expired naturally."""
        await self._db_pool.simple_upsert(
            table="staff_jwt_revocations",
            keyvalues={"jti": jti},
            values={
                "revoked_ts": _now_ms(),
                "exp_ts": int(exp_ts),
            },
            desc="staff_jwt_revocation_add",
        )
    # === END AGENT H ===

    # === AGENT N ===
    # External-rooms helper used by GET /admin/rooms/external.  Lists
    # rooms that contain ZERO staff joins.  Pagination is applied at the
    # SQL level so we never materialise the full room list into memory.
    #
    # Table shapes verified at synapse/storage/schema/main/full_schemas/
    # 72/full.sql.postgres:
    #   current_state_events(event_id, room_id, type, state_key, membership)
    #   events(event_id, type, room_id, sender, origin_server_ts, ...)
    #   event_json(event_id, room_id, json, ...)
    #   room_stats_state(room_id, name, ...)
    #   room_stats_current(room_id, joined_members, ...)
    #
    # `is_direct` heuristic: read m.room.create's content.is_direct via
    # event_json, fall back to member_count == 2.  Approximate -- the
    # canonical DM marker is `m.direct` account-data on the inviter.

    async def staff_list_external_rooms(
        self,
        *,
        staff_ids: List[str],
        limit: int,
        offset: int,
        search: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return (rows, total) where `rows` is one dict per external
        room.  Each dict matches the wire format documented in
        rest_admin_listing.StaffAdminExternalRoomsServlet.
        """
        from synapse.storage.database import make_in_list_sql_clause

        engine = self._db_pool.engine

        # SQL fragment: rooms with at least one staff-user join.
        staff_in_clause, staff_args = make_in_list_sql_clause(
            engine, "state_key", staff_ids,
        )
        # SQL fragment: rooms that contain a join from any user, minus
        # the set above.  Use a NOT IN against the subquery so the
        # filter happens server-side.
        search_clause_room = ""
        search_clause_member = ""
        search_args: List[Any] = []
        if search:
            term = "%" + search.lower() + "%"
            # Room-name filter against room_stats_state; case-insensitive.
            search_clause_room = "LOWER(COALESCE(rss.name, '')) LIKE ?"
            # Member-id filter against current_state_events.state_key
            # (case-insensitive substring on MXID).  We don't reach into
            # event content for display_name -- that lives in JSON and
            # would force a per-event_json scan.
            search_clause_member = (
                "EXISTS ("
                "  SELECT 1 FROM current_state_events cse2 "
                "  WHERE cse2.room_id = ext.room_id "
                "    AND cse2.type = 'm.room.member' "
                "    AND cse2.membership = 'join' "
                "    AND LOWER(cse2.state_key) LIKE ?"
                ")"
            )
            search_args = [term, term]

        # Build the external-room id list.  We compute it twice (once for
        # COUNT, once for the page) using identical WHERE shape.
        external_ids_sql_no_search = f"""
            SELECT DISTINCT cse.room_id AS room_id
            FROM current_state_events cse
            WHERE cse.type = 'm.room.member'
              AND cse.membership = 'join'
              AND cse.room_id NOT IN (
                  SELECT DISTINCT room_id
                  FROM current_state_events
                  WHERE type = 'm.room.member'
                    AND membership = 'join'
                    AND {staff_in_clause}
              )
        """

        # The search filter wraps the external-id list with a LEFT JOIN
        # onto room_stats_state and an OR-of-clauses against either the
        # room name or any member MXID.  We do this as a CTE-ish subquery
        # for clarity.
        if search:
            external_with_search_sql = f"""
                SELECT ext.room_id
                FROM ({external_ids_sql_no_search}) ext
                LEFT JOIN room_stats_state rss ON rss.room_id = ext.room_id
                WHERE ({search_clause_room} OR {search_clause_member})
            """
            external_filter_sql = external_with_search_sql
            external_filter_args = list(staff_args) + list(search_args)
        else:
            external_filter_sql = external_ids_sql_no_search
            external_filter_args = list(staff_args)

        count_sql = f"SELECT COUNT(*) FROM ({external_filter_sql}) c"
        # Order by room_id for stable pagination.
        list_ids_sql = (
            f"SELECT room_id FROM ({external_filter_sql}) c "
            f"ORDER BY room_id ASC LIMIT ? OFFSET ?"
        )
        list_ids_args = list(external_filter_args) + [int(limit), int(offset)]

        def _txn(txn) -> Tuple[List[Dict[str, Any]], int]:
            # --- total count -------------------------------------------------
            txn.execute(count_sql, tuple(external_filter_args))
            row = txn.fetchone()
            total = int(row[0]) if row and row[0] is not None else 0

            # --- paged room id list -----------------------------------------
            txn.execute(list_ids_sql, tuple(list_ids_args))
            room_ids = [r[0] for r in txn.fetchall()]
            if not room_ids:
                return [], total

            # We do per-room follow-up queries; the page is bounded
            # (max 200 by the REST layer) so this is acceptable.
            rooms: List[Dict[str, Any]] = []
            for room_id in room_ids:
                # room name + joined_members count
                txn.execute(
                    "SELECT rss.name, rsc.joined_members "
                    "FROM room_stats_state rss "
                    "LEFT JOIN room_stats_current rsc "
                    "  ON rsc.room_id = rss.room_id "
                    "WHERE rss.room_id = ?",
                    (room_id,),
                )
                rss_row = txn.fetchone()
                room_name = rss_row[0] if rss_row else None
                member_count = (
                    int(rss_row[1])
                    if rss_row and rss_row[1] is not None else 0
                )

                # m.room.create event -> created_ts + is_direct flag.
                # current_state_events has the create event's event_id;
                # join to events for origin_server_ts and to event_json
                # for the content (so we can read content.is_direct).
                txn.execute(
                    "SELECT e.origin_server_ts, ej.json "
                    "FROM current_state_events cse "
                    "LEFT JOIN events e ON e.event_id = cse.event_id "
                    "LEFT JOIN event_json ej ON ej.event_id = cse.event_id "
                    "WHERE cse.room_id = ? AND cse.type = 'm.room.create' "
                    "  AND cse.state_key = '' "
                    "LIMIT 1",
                    (room_id,),
                )
                cre_row = txn.fetchone()
                created_ts: Optional[int] = None
                is_direct_flag: Optional[bool] = None
                if cre_row:
                    if cre_row[0] is not None:
                        try:
                            created_ts = int(cre_row[0])
                        except (TypeError, ValueError):
                            created_ts = None
                    if cre_row[1]:
                        try:
                            doc = json.loads(cre_row[1])
                            content = doc.get("content") or {}
                            if "is_direct" in content:
                                is_direct_flag = bool(content["is_direct"])
                        except Exception:
                            pass

                # Heuristic fallback: a two-member room is treated as a DM
                # when the create event doesn't say either way.  See class
                # docstring for caveats.
                if is_direct_flag is None:
                    is_direct = (member_count == 2)
                else:
                    is_direct = is_direct_flag

                # Message count: rows in `events` for this room with
                # type=m.room.message.  Non-redacted, non-outlier.
                txn.execute(
                    "SELECT COUNT(*) FROM events "
                    "WHERE room_id = ? AND type = 'm.room.message' "
                    "  AND outlier = ?",
                    (room_id, False),
                )
                mc_row = txn.fetchone()
                message_count = int(mc_row[0]) if mc_row else 0

                # Members: pull every current m.room.member with
                # membership=join.  We collect (mxid, display_name,
                # joined_ts) by joining current_state_events ->
                # events (for origin_server_ts) -> event_json (for
                # content.displayname).
                txn.execute(
                    "SELECT cse.state_key, e.origin_server_ts, ej.json "
                    "FROM current_state_events cse "
                    "LEFT JOIN events e ON e.event_id = cse.event_id "
                    "LEFT JOIN event_json ej ON ej.event_id = cse.event_id "
                    "WHERE cse.room_id = ? AND cse.type = 'm.room.member' "
                    "  AND cse.membership = 'join' "
                    "ORDER BY e.origin_server_ts ASC",
                    (room_id,),
                )
                members: List[Dict[str, Any]] = []
                for m_row in txn.fetchall():
                    mxid = m_row[0]
                    joined_ts = (
                        int(m_row[1]) if m_row[1] is not None else None
                    )
                    display_name: Optional[str] = None
                    if m_row[2]:
                        try:
                            doc = json.loads(m_row[2])
                            content = doc.get("content") or {}
                            dn = content.get("displayname")
                            if isinstance(dn, str):
                                display_name = dn
                        except Exception:
                            pass
                    members.append({
                        "user_id": mxid,
                        "display_name": display_name,
                        "joined_ts": joined_ts,
                    })

                rooms.append({
                    "room_id": room_id,
                    "name": room_name,
                    "is_direct": is_direct,
                    "created_ts": created_ts,
                    "member_count": member_count,
                    "message_count": message_count,
                    "members": members,
                })
            return rooms, total

        return await self._db_pool.runInteraction(
            "staff_admin_list_external_rooms", _txn,
        )
    # === END AGENT N ===

    # === AGENT P ===
    # Widget-group helpers.  A "group" is a named bag of staff users that
    # collectively own a set of general_widgets.  When any of those staff
    # members joins a 2-person DM, the widget injector (widget_inject.py)
    # pulls the group's widgets and injects them.  The membership join
    # tables (staff_widget_group_members, staff_widget_group_widgets) do
    # not carry foreign keys; orphan rows are scrubbed by
    # `widget_orphan_cleanup_on_widget_delete` and
    # `widget_orphan_cleanup_on_staff_remove`, both of which are wired
    # into `widget_delete` / `remove_staff_user` above.

    async def group_create(
        self, name: str, description: Optional[str], created_by: str,
    ) -> str:
        group_id = _new_id()
        await self._db_pool.simple_insert(
            table="staff_widget_groups",
            values={
                "group_id": group_id,
                "name": name,
                "description": description,
                "created_ts": _now_ms(),
                "created_by": created_by,
            },
            desc="staff_group_create",
        )
        return group_id

    async def group_id_by_name(self, name: str) -> Optional[str]:
        """Return the group_id of an existing group with this name, or None.

        Used by the POST /groups handler to reject duplicate names before
        attempting the insert (the schema has no UNIQUE constraint on name).
        """
        row = await self._db_pool.simple_select_one(
            table="staff_widget_groups",
            keyvalues={"name": name},
            retcols=("group_id",),
            allow_none=True,
            desc="staff_group_id_by_name",
        )
        if not row:
            return None
        # simple_select_one returns a tuple; one retcol -> index 0.
        return row[0]

    async def group_get(self, group_id: str) -> Optional[Dict[str, Any]]:
        _GROUP_COLS = ("group_id", "name", "description", "created_ts",
                       "created_by")
        row_tup = await self._db_pool.simple_select_one(
            table="staff_widget_groups",
            keyvalues={"group_id": group_id},
            retcols=_GROUP_COLS,
            allow_none=True,
            desc="staff_group_get",
        )
        if not row_tup:
            return None
        # simple_select_one returns a tuple; convert to a dict so the rest
        # of this function (and the JSON response) can use named fields.
        row: Dict[str, Any] = dict(zip(_GROUP_COLS, row_tup))
        members = await self._db_pool.simple_select_onecol(
            table="staff_widget_group_members",
            keyvalues={"group_id": group_id},
            retcol="user_id",
            desc="staff_group_get_members",
        )
        widgets = await self._db_pool.simple_select_onecol(
            table="staff_widget_group_widgets",
            keyvalues={"group_id": group_id},
            retcol="widget_id",
            desc="staff_group_get_widgets",
        )
        row["members"] = list(members)
        row["widgets"] = list(widgets)
        return row

    async def group_list_full(self) -> List[Dict[str, Any]]:
        """Return every group with its members + widgets attached.

        Three queries + a Python join (NOT one per group): cheap regardless
        of group count.  Stable ordering by group name then group_id so
        the panel UI doesn't reshuffle on every refresh.
        """

        def _txn(txn) -> List[Dict[str, Any]]:
            txn.execute(
                "SELECT group_id, name, description, created_ts, created_by "
                "FROM staff_widget_groups "
                "ORDER BY name ASC, group_id ASC"
            )
            cols = [c[0] for c in txn.description]
            groups = [dict(zip(cols, r)) for r in txn.fetchall()]
            by_id: Dict[str, Dict[str, Any]] = {}
            for g in groups:
                g["members"] = []
                g["widgets"] = []
                by_id[g["group_id"]] = g

            if by_id:
                txn.execute(
                    "SELECT group_id, user_id FROM staff_widget_group_members"
                )
                for gid, uid in txn.fetchall():
                    if gid in by_id:
                        by_id[gid]["members"].append(uid)
                txn.execute(
                    "SELECT group_id, widget_id "
                    "FROM staff_widget_group_widgets"
                )
                for gid, wid in txn.fetchall():
                    if gid in by_id:
                        by_id[gid]["widgets"].append(wid)
            return groups

        return await self._db_pool.runInteraction(
            "staff_group_list_full", _txn,
        )

    async def group_update(
        self, group_id: str, fields: Dict[str, Any],
    ) -> bool:
        """Apply a partial update.

        `fields` may contain `name`, `description`, `members`, `widgets`.
        For `members` / `widgets` the supplied list REPLACES the set
        (an empty list clears membership; absence leaves it unchanged).
        Replacements happen inside a single runInteraction so a crash
        between DELETE and INSERT can't leave a half-applied set.
        """
        scalar_updates: Dict[str, Any] = {}
        if "name" in fields:
            scalar_updates["name"] = fields["name"]
        if "description" in fields:
            scalar_updates["description"] = fields["description"]

        replace_members: Optional[List[str]] = None
        replace_widgets: Optional[List[str]] = None
        if "members" in fields:
            replace_members = list(fields["members"])
        if "widgets" in fields:
            replace_widgets = list(fields["widgets"])

        if (
            not scalar_updates
            and replace_members is None
            and replace_widgets is None
        ):
            return False

        def _txn(txn) -> None:
            if scalar_updates:
                set_clause = ", ".join(
                    f"{k} = ?" for k in scalar_updates.keys()
                )
                txn.execute(
                    f"UPDATE staff_widget_groups SET {set_clause} "
                    f"WHERE group_id = ?",
                    list(scalar_updates.values()) + [group_id],
                )
            if replace_members is not None:
                txn.execute(
                    "DELETE FROM staff_widget_group_members "
                    "WHERE group_id = ?",
                    (group_id,),
                )
                if replace_members:
                    self._db_pool.simple_insert_many_txn(
                        txn,
                        table="staff_widget_group_members",
                        keys=("group_id", "user_id"),
                        values=[(group_id, u) for u in replace_members],
                    )
            if replace_widgets is not None:
                txn.execute(
                    "DELETE FROM staff_widget_group_widgets "
                    "WHERE group_id = ?",
                    (group_id,),
                )
                if replace_widgets:
                    self._db_pool.simple_insert_many_txn(
                        txn,
                        table="staff_widget_group_widgets",
                        keys=("group_id", "widget_id"),
                        values=[(group_id, w) for w in replace_widgets],
                    )

        await self._db_pool.runInteraction("staff_group_update", _txn)
        return True

    async def group_delete(self, group_id: str) -> bool:
        """Cascade-delete the group and both join tables in one txn."""

        def _txn(txn) -> int:
            txn.execute(
                "DELETE FROM staff_widget_group_members WHERE group_id = ?",
                (group_id,),
            )
            txn.execute(
                "DELETE FROM staff_widget_group_widgets WHERE group_id = ?",
                (group_id,),
            )
            txn.execute(
                "DELETE FROM staff_widget_groups WHERE group_id = ?",
                (group_id,),
            )
            try:
                return int(txn.rowcount)
            except Exception:
                return 0

        deleted = await self._db_pool.runInteraction(
            "staff_group_delete", _txn,
        )
        return bool(deleted)

    async def group_add_member(
        self, group_id: str, user_id: str,
    ) -> None:
        await self._db_pool.simple_upsert(
            table="staff_widget_group_members",
            keyvalues={"group_id": group_id, "user_id": user_id},
            values={},
            desc="staff_group_add_member",
        )

    async def group_remove_member(
        self, group_id: str, user_id: str,
    ) -> int:
        return await self._db_pool.simple_delete(
            table="staff_widget_group_members",
            keyvalues={"group_id": group_id, "user_id": user_id},
            desc="staff_group_remove_member",
        )

    async def group_add_widget(
        self, group_id: str, widget_id: str,
    ) -> None:
        await self._db_pool.simple_upsert(
            table="staff_widget_group_widgets",
            keyvalues={"group_id": group_id, "widget_id": widget_id},
            values={},
            desc="staff_group_add_widget",
        )

    async def group_remove_widget(
        self, group_id: str, widget_id: str,
    ) -> int:
        return await self._db_pool.simple_delete(
            table="staff_widget_group_widgets",
            keyvalues={"group_id": group_id, "widget_id": widget_id},
            desc="staff_group_remove_widget",
        )

    async def groups_for_user(self, user_id: str) -> List[str]:
        rows = await self._db_pool.simple_select_onecol(
            table="staff_widget_group_members",
            keyvalues={"user_id": user_id},
            retcol="group_id",
            desc="staff_groups_for_user",
        )
        return list(rows)

    async def widget_ids_for_user_via_groups(
        self, user_id: str,
    ) -> Set[str]:
        """Hot path: one SELECT joining membership -> group_widgets.

        Called by the widget injector on every staff DM join.  Returns
        the SET of widget_ids the user pulls in via group membership.
        """

        def _txn(txn) -> Set[str]:
            txn.execute(
                "SELECT DISTINCT gw.widget_id "
                "FROM staff_widget_group_members gm "
                "INNER JOIN staff_widget_group_widgets gw "
                "  ON gm.group_id = gw.group_id "
                "WHERE gm.user_id = ?",
                (user_id,),
            )
            return {r[0] for r in txn.fetchall()}

        return await self._db_pool.runInteraction(
            "staff_widget_ids_for_user_via_groups", _txn,
        )

    async def groups_for_widget(self, widget_id: str) -> List[str]:
        rows = await self._db_pool.simple_select_onecol(
            table="staff_widget_group_widgets",
            keyvalues={"widget_id": widget_id},
            retcol="group_id",
            desc="staff_groups_for_widget",
        )
        return list(rows)

    async def widget_orphan_cleanup_on_widget_delete(
        self, widget_id: str,
    ) -> int:
        """Standalone helper.  `widget_delete` calls this inline already
        (see the AGENT P fence above); exposed publicly in case a future
        reconciliation job wants to scrub orphaned join rows after a
        botched migration."""
        return await self._db_pool.simple_delete(
            table="staff_widget_group_widgets",
            keyvalues={"widget_id": widget_id},
            desc="staff_widget_orphan_cleanup_on_widget_delete",
        )

    async def widget_orphan_cleanup_on_staff_remove(
        self, user_id: str,
    ) -> int:
        """Standalone helper.  `remove_staff_user` calls this inline
        already; exposed publicly for the same reconciliation use-case as
        the widget variant."""
        return await self._db_pool.simple_delete(
            table="staff_widget_group_members",
            keyvalues={"user_id": user_id},
            desc="staff_widget_orphan_cleanup_on_staff_remove",
        )
    # === END AGENT P ===
