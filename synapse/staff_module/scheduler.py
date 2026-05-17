#
# STAFF mod — F12 scheduler action.
#
# Uses synapse.util.task_scheduler to persist a one-shot send task.  Runs
# on the main process; 1-minute granularity is plenty for "schedule a
# message at time X" UX.
#

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from synapse.types import JsonMapping, TaskStatus

from .forge import send_event_as

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.types import ScheduledTask
    from .store import StaffStore

logger = logging.getLogger(__name__)

ACTION_NAME = "staff_send_scheduled"


def parse_local_to_utc_ms(
    naive_iso: str, timezone_name: str = "Asia/Ho_Chi_Minh"
) -> int:
    """Convert a naive local-time ISO string (e.g. "2026-05-18T14:30:00")
    to a UTC epoch in milliseconds, interpreting the input as
    `timezone_name` local time (default Vietnam ICT, UTC+7, no DST).
    """
    naive = datetime.fromisoformat(naive_iso)
    if naive.tzinfo is not None:
        # The caller already supplied tz info; trust it.
        aware = naive
    else:
        tz = ZoneInfo(timezone_name)
        aware = naive.replace(tzinfo=tz)
    return int(aware.timestamp() * 1000)


def register_scheduler(hs: "HomeServer", store: "StaffStore") -> None:
    scheduler = hs.get_task_scheduler()

    async def _run(task: "ScheduledTask") -> Tuple[TaskStatus, Optional[JsonMapping], Optional[str]]:
        try:
            row = await store.schedule_get(task.id)
            if row is None:
                return TaskStatus.FAILED, None, "task row missing"

            room_id = row["room_id"]
            as_user = row["as_user"]
            message = row.get("message")
            image_mxc = row.get("image_mxc")

            sent: Dict[str, Any] = {}
            if message:
                ev = await send_event_as(
                    hs, sender=as_user, room_id=room_id,
                    event_type="m.room.message",
                    content={"msgtype": "m.text", "body": message},
                )
                sent["text_event_id"] = ev.event_id
            if image_mxc:
                # m.image events need at minimum body, msgtype, url.  We
                # don't know the file size or dimensions from the API
                # caller, so we use a minimal valid content shape; richer
                # clients will load it from the mxc URL.
                ev = await send_event_as(
                    hs, sender=as_user, room_id=room_id,
                    event_type="m.room.message",
                    content={
                        "msgtype": "m.image",
                        "body": row.get("message") or "image",
                        "url": image_mxc,
                    },
                )
                sent["image_event_id"] = ev.event_id

            # Done — cleanup the row.
            await store.schedule_delete(task.id)
            return TaskStatus.COMPLETE, sent, None
        except Exception as e:
            logger.exception("STAFF scheduled task %s failed", task.id)
            return TaskStatus.FAILED, None, repr(e)

    scheduler.register_action(_run, ACTION_NAME)
    logger.info("STAFF: registered scheduler action %r", ACTION_NAME)


async def schedule_message(
    hs: "HomeServer",
    store: "StaffStore",
    *,
    room_id: str,
    as_user: str,
    send_at_ms: int,
    message: Optional[str],
    image_mxc: Optional[str],
) -> str:
    scheduler = hs.get_task_scheduler()
    task_id = await scheduler.schedule_task(
        ACTION_NAME,
        resource_id=room_id,
        timestamp=send_at_ms,
        params={"room_id": room_id},
    )
    await store.schedule_insert(
        task_id=task_id,
        room_id=room_id,
        as_user=as_user,
        send_at_ms=send_at_ms,
        message=message,
        image_mxc=image_mxc,
    )
    return task_id


async def cancel_scheduled(
    hs: "HomeServer", store: "StaffStore", task_id: str
) -> bool:
    scheduler = hs.get_task_scheduler()
    try:
        await scheduler.delete_task(task_id)
    except Exception:
        pass
    deleted = await store.schedule_delete(task_id)
    return bool(deleted)
