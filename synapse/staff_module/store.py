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
        self._staff_user_ids = {r["user_id"] for r in rows}
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
        return deleted

    async def list_staff_users(self) -> List[Dict[str, Any]]:
        return await self._db_pool.simple_select_list(
            table="staff_users",
            keyvalues=None,
            retcols=("user_id", "added_ts", "added_by", "note"),
            desc="staff_list_users",
        )

    # ----------------------------------------------------------------- settings

    async def settings_get_all(self) -> Dict[str, Any]:
        rows = await self._db_pool.simple_select_list(
            table="staff_settings",
            keyvalues=None,
            retcols=("setting_key", "value"),
            desc="staff_settings_all",
        )
        return {r["setting_key"]: json.loads(r["value"]) for r in rows}

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
        return json.loads(row["value"])

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
        return await self._db_pool.simple_select_one(
            table="staff_scheduled_messages",
            keyvalues={"task_id": task_id},
            retcols=("task_id", "room_id", "as_user", "send_at_ms", "message",
                     "image_mxc", "created_ts"),
            allow_none=True,
            desc="staff_schedule_get",
        )

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

    async def widget_get(self, widget_id: str) -> Optional[Dict[str, Any]]:
        row = await self._db_pool.simple_select_one(
            table="staff_widget_definitions",
            keyvalues={"widget_id": widget_id},
            retcols=("widget_id", "owner_user_id", "widget_type", "name", "url",
                     "content_json", "created_ts", "updated_ts"),
            allow_none=True,
            desc="staff_widget_get",
        )
        if not row:
            return None
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
            retcols=("widget_id", "owner_user_id", "widget_type", "name", "url",
                     "content_json", "created_ts", "updated_ts"),
            desc="staff_widget_list",
        )
        for r in rows:
            r["content"] = json.loads(r.pop("content_json"))
        return rows

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
        return await self._db_pool.simple_delete(
            table="staff_widget_definitions",
            keyvalues={"widget_id": widget_id},
            desc="staff_widget_delete_def",
        )

    async def widget_instances_for(
        self, widget_id: str
    ) -> List[Dict[str, Any]]:
        return await self._db_pool.simple_select_list(
            table="staff_widget_room_instances",
            keyvalues={"widget_id": widget_id},
            retcols=("instance_id", "widget_id", "room_id", "injected_by",
                     "last_state_event_id", "created_ts"),
            desc="staff_widget_instances_for",
        )

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
        return await self._db_pool.simple_select_one(
            table="staff_widget_room_instances",
            keyvalues={"widget_id": widget_id, "room_id": room_id},
            retcols=("instance_id", "widget_id", "room_id", "injected_by",
                     "last_state_event_id", "created_ts"),
            allow_none=True,
            desc="staff_widget_instance_get",
        )

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
