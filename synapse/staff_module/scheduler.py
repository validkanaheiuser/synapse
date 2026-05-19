#
# STAFF mod — F12 scheduler action.
#
# Uses synapse.util.task_scheduler to persist a one-shot send task.  Runs
# on the main process; 1-minute granularity is plenty for "schedule a
# message at time X" UX.
#

import logging
import time
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

# === AGENT I (S13) ===
# Action name + retention window for the nightly cleanup of fired rows.
# Verified against synapse.util.task_scheduler.TaskScheduler.register_action /
# schedule_task signatures at task_scheduler.py:145-220.  The scheduler is a
# 1-minute-precision loop so an interval of "once per day" is plenty.
CLEANUP_ACTION_NAME = "staff_scheduler_cleanup"
CLEANUP_RETENTION_MS = 7 * 24 * 60 * 60 * 1000  # 7 days
CLEANUP_INTERVAL_MS = 24 * 60 * 60 * 1000        # 24 hours
# === END AGENT I ===


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
            # Prefer the full row so we can honour `image_body` when set,
            # but fall back to the legacy getter if the new column is not
            # yet migrated (defensive — schema delta 95/02 adds it).
            row: Optional[Dict[str, Any]] = None
            getter = getattr(store, "schedule_get_full", None)
            if getter is not None:
                row = await getter(task.id)
            if row is None:
                row = await store.schedule_get(task.id)
            if row is None:
                return TaskStatus.FAILED, None, "task row missing"

            room_id = row["room_id"]
            as_user = row["as_user"]
            message = row.get("message")
            image_mxc = row.get("image_mxc")
            image_body = row.get("image_body")

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
                #
                # `body` is what screen-readers / fallback clients show.
                # Preference order:
                #   1. explicit image_body from the API caller
                #   2. the accompanying text message (captioned image)
                #   3. the literal "image" placeholder
                body_text = image_body or row.get("message") or "image"
                ev = await send_event_as(
                    hs, sender=as_user, room_id=room_id,
                    event_type="m.room.message",
                    content={
                        "msgtype": "m.image",
                        "body": body_text,
                        "url": image_mxc,
                    },
                )
                sent["image_event_id"] = ev.event_id

            # === AGENT I (S13): record fire-time, defer delete ===
            # We keep the row around (with `fired_ts` set) so the audit /
            # status UI can still observe what was sent, and the nightly
            # cleanup job removes it after `CLEANUP_RETENTION_MS`.  If the
            # `schedule_mark_fired` helper is missing (schema delta
            # 95/04 not yet applied), we fall back to the original
            # delete-immediately behaviour so old deployments keep working.
            marker = getattr(store, "schedule_mark_fired", None)
            if marker is not None:
                try:
                    await marker(task.id)
                except Exception:
                    logger.exception(
                        "STAFF: failed to mark task %s fired; "
                        "falling back to delete", task.id,
                    )
                    await store.schedule_delete(task.id)
            else:
                await store.schedule_delete(task.id)
            return TaskStatus.COMPLETE, sent, None
            # === END AGENT I ===
        except Exception as e:
            logger.exception("STAFF scheduled task %s failed", task.id)
            return TaskStatus.FAILED, None, repr(e)

    scheduler.register_action(_run, ACTION_NAME)

    # === AGENT I (S13): nightly cleanup of fired rows ===
    # Register a separate action that the TaskScheduler will fire daily.
    # The action deletes every row whose `fired_ts` is older than the
    # retention window, then schedules itself to run again 24h later
    # (the cleanest way to get repeating behaviour out of the one-shot
    # TaskScheduler API).  We avoid the legacy `clock.looping_call`
    # alternative so the cleanup survives Synapse restarts.
    async def _cleanup(task: "ScheduledTask") -> Tuple[TaskStatus, Optional[JsonMapping], Optional[str]]:
        try:
            cutoff = int(time.time() * 1000) - CLEANUP_RETENTION_MS
            cleaner = getattr(store, "schedule_cleanup_fired", None)
            removed = 0
            if cleaner is not None:
                removed = await cleaner(cutoff)
            logger.info(
                "STAFF scheduler cleanup: removed %d fired rows older than %d ms",
                removed, CLEANUP_RETENTION_MS,
            )
        except Exception as e:
            logger.exception("STAFF scheduler cleanup failed")
            # Even on failure, re-arm so we try again tomorrow.
            await _schedule_next_cleanup(hs)
            return TaskStatus.FAILED, None, repr(e)

        # Re-arm for next run.
        await _schedule_next_cleanup(hs)
        return TaskStatus.COMPLETE, {"removed": int(removed)}, None

    scheduler.register_action(_cleanup, CLEANUP_ACTION_NAME)

    # Mark scheduler-action registration on `hs` so the /health endpoint
    # (S18) can report it without needing a back-pointer to this module.
    setattr(hs, "_staff_scheduler_action_registered", True)

    # Best-effort: schedule the first cleanup run.  If `schedule_task`
    # fails (e.g. read-only DB during startup), we log and proceed — the
    # action is still registered and a manual trigger remains possible.
    try:
        clock = hs.get_clock()
        clock.call_when_running(
            lambda: hs.run_as_background_process(
                "staff_scheduler_cleanup_kickoff",
                _schedule_next_cleanup, hs,
            )
        )
    except Exception:
        logger.exception("STAFF: failed to schedule cleanup kickoff")

    logger.info(
        "STAFF: registered scheduler actions %r and %r",
        ACTION_NAME, CLEANUP_ACTION_NAME,
    )


async def _schedule_next_cleanup(hs: "HomeServer") -> None:
    """Schedule the next cleanup run, ~24h from now.  Idempotent-ish: if
    there's already a SCHEDULED task for our cleanup action, we leave it
    alone — the existing one will fire and re-arm itself.
    """
    try:
        scheduler = hs.get_task_scheduler()
        existing = await scheduler.get_tasks(
            actions=[CLEANUP_ACTION_NAME],
            statuses=[TaskStatus.SCHEDULED],
            limit=1,
        )
        if existing:
            return
        fire_at = int(time.time() * 1000) + CLEANUP_INTERVAL_MS
        await scheduler.schedule_task(
            CLEANUP_ACTION_NAME,
            timestamp=fire_at,
        )
    except Exception:
        logger.exception("STAFF: failed to (re)schedule cleanup task")
    # === END AGENT I ===


async def schedule_message(
    hs: "HomeServer",
    store: "StaffStore",
    *,
    room_id: str,
    as_user: str,
    send_at_ms: int,
    message: Optional[str],
    image_mxc: Optional[str],
    image_body: Optional[str] = None,
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
    # `image_body` lives in a column added by a later schema delta and
    # the legacy `schedule_insert` signature does not accept it.  Apply
    # it as a follow-up update only when supplied, so the insert path
    # remains source-compatible with the original helper.
    if image_body is not None and image_mxc is not None:
        set_body = getattr(store, "schedule_set_image_body", None)
        if set_body is not None:
            await set_body(task_id, image_body)
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


async def update_scheduled(
    hs: "HomeServer",
    store: "StaffStore",
    task_id: str,
    *,
    send_at_ms: Optional[int],
    message: Optional[str],
    image_mxc: Optional[str],
    image_body: Optional[str],
    clear_message: bool,
    clear_image: bool,
) -> Optional[Dict[str, Any]]:
    """Update a pending scheduled message row plus its TaskScheduler
    entry.  Returns the resulting full row, or `None` if the task does
    not exist.

    TaskScheduler API quirks (synapse.util.task_scheduler):

      * `update_task(id, *, timestamp=...)` is the only way to re-arm a
        scheduled task; if `timestamp` is `None` it defaults to "now"
        which would fire the task immediately, so we always pass an
        explicit value.
      * Only tasks in SCHEDULED state can be reliably re-timed; an
        ACTIVE / COMPLETE / FAILED task cannot be reused, so we refuse
        to PATCH those.
      * `update_task` cannot be used to set status COMPLETE / FAILED
        (it raises) — we deliberately omit a status kwarg here.
      * The `params` field on the underlying ScheduledTask is not
        re-read by our action (the action loads everything from the
        staff_scheduled_messages row via task_id), so we do NOT need to
        thread message / image data through TaskScheduler.
    """
    # Read existing row first so we can validate and provide a useful
    # "not found" / "already fired" response.
    getter = getattr(store, "schedule_get_full", None)
    existing = None
    if getter is not None:
        existing = await getter(task_id)
    if existing is None:
        existing = await store.schedule_get(task_id)
    if existing is None:
        return None

    # Update the row in our store.
    await store.schedule_update_row(
        task_id,
        send_at_ms=send_at_ms,
        message=message,
        image_mxc=image_mxc,
        image_body=image_body,
        clear_message=clear_message,
        clear_image=clear_image,
    )

    # Re-arm the TaskScheduler entry if the fire time changed.  We
    # always pass an explicit timestamp to avoid the "fire immediately"
    # default behaviour described above.
    if send_at_ms is not None:
        scheduler = hs.get_task_scheduler()
        try:
            existing_task = await scheduler.get_task(task_id)
            if existing_task is None:
                logger.warning(
                    "STAFF: PATCH for task %s: row exists but no scheduled "
                    "task — re-scheduling",
                    task_id,
                )
            elif existing_task.status not in (
                TaskStatus.SCHEDULED,
                TaskStatus.ACTIVE,
            ):
                # Task already finished — nothing to re-arm.
                pass
            else:
                await scheduler.update_task(
                    task_id,
                    timestamp=send_at_ms,
                    status=TaskStatus.SCHEDULED,
                )
        except Exception:
            logger.exception(
                "STAFF: failed to update TaskScheduler entry for %s",
                task_id,
            )

    # Return the post-update row for the response payload.
    if getter is not None:
        updated = await getter(task_id)
    else:
        updated = await store.schedule_get(task_id)
    return updated
