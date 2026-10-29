#
# STAFF mod — widget resync / backfill.
#
# F16 only injects widgets when a staff DM is *created* and F17 only
# re-sends a widget to rooms it already lives in.  Nothing covered the
# gap "operator added a widget / moved a staff into another group, and
# the existing DMs never pick it up".  This endpoint walks every staff
# user's 2-person DMs and reconciles them against the desired set:
#
#   POST /_synapse/staff/v1/widgets/resync
#        body: {"prune": bool = false, "staff": [mxid, ...] optional}
#        -> 200 {"job_id": ..., "status": "running", ...}
#        -> 409 if a resync job is already running
#   GET  /_synapse/staff/v1/widgets/resync            -> {"jobs": [...]}
#   GET  /_synapse/staff/v1/widgets/resync/{job_id}   -> job document
#
# Per room, for each desired widget:
#   missing (no state / empty content)     -> inject   (counted "injected")
#   present but content differs            -> re-send  (counted "refreshed")
#   present and identical                  -> nothing  (counted "noop")
# With prune=true, *tracked* instances (rows in staff_widget_room_instances)
# whose widget is no longer desired for that staff get an empty-content
# state event + their row deleted ("pruned").  User-added widgets (Jitsi,
# etc.) are never tracked, so they are never removed.
#
# The walk runs as a background process on the worker that received the
# request (HAProxy pins /_synapse/staff to main) so the HTTP call returns
# immediately; the page polls the job.  Fan-out is throttled: a short sleep
# after every ROOM_BATCH rooms, same shape as F17 propagation.
#

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .forge import send_event_as
from .rest_base import StaffRestServlet, staff_pattern
from .widget_inject import WIDGET_EVENT_TYPE, WidgetInjector
from .widget_payload import build_widget_state_content

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)

ROOM_BATCH = 50          # rooms between throttle sleeps
BATCH_SLEEP_S = 0.05
MAX_DETAIL_ROWS = 500    # per-room detail rows kept on the job document
MAX_JOBS_KEPT = 10       # finished jobs retained in memory (newest first)

_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
_STATUS_ERROR = "error"


class _JobRegistry:
    """In-memory job table.  Lives on the single process that serves the
    staff REST API, so a plain dict is enough."""

    def __init__(self) -> None:
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []   # newest first
        self.active: Optional[str] = None

    def add(self, job: Dict[str, Any]) -> None:
        self.jobs[job["job_id"]] = job
        self.order.insert(0, job["job_id"])
        while len(self.order) > MAX_JOBS_KEPT:
            dropped = self.order.pop()
            if dropped != self.active:
                self.jobs.pop(dropped, None)
            else:
                self.order.append(dropped)
                break


def _registry(hs: "HomeServer") -> _JobRegistry:
    reg = getattr(hs, "_staff_widget_resync_jobs", None)
    if reg is None:
        reg = _JobRegistry()
        setattr(hs, "_staff_widget_resync_jobs", reg)
    return reg


def _injector(hs: "HomeServer", store: "StaffStore") -> WidgetInjector:
    inj = getattr(hs, "_staff_widget_injector", None)
    if inj is None:
        inj = WidgetInjector(hs, store)
        setattr(hs, "_staff_widget_injector", inj)
    return inj


def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(job)
    out.pop("_task", None)
    return out


class StaffWidgetResyncServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/widgets/resync")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        reg = _registry(self.hs)
        return 200, {
            "active_job_id": reg.active,
            "jobs": [_public_job(reg.jobs[j]) for j in reg.order if j in reg.jobs],
        }

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)
        body = parse_json_object_from_request(request, allow_empty_body=True) or {}

        prune = bool(body.get("prune", False))
        only_staff: Optional[List[str]] = None
        if "staff" in body and body["staff"] is not None:
            raw = body["staff"]
            if not isinstance(raw, list) or not all(
                isinstance(u, str) and UserID.is_valid(u) for u in raw
            ):
                raise SynapseError(400, "staff must be a list of MXIDs")
            only_staff = sorted(set(raw))

        reg = _registry(self.hs)
        if reg.active is not None and reg.jobs.get(reg.active, {}).get("status") == _STATUS_RUNNING:
            raise SynapseError(409, "a widget resync job is already running")

        import uuid
        job_id = uuid.uuid4().hex
        now = int(self.clock.time_msec())
        job: Dict[str, Any] = {
            "job_id": job_id,
            "status": _STATUS_RUNNING,
            "started_ts": now,
            "finished_ts": None,
            "prune": prune,
            "staff_filter": only_staff,
            "requested_by": outcome.actor_user_id or outcome.kind,
            "progress": {"staff_total": 0, "staff_done": 0, "rooms_scanned": 0},
            "result": {
                "rooms_scanned": 0,
                "injected": 0,
                "refreshed": 0,
                "noop": 0,
                "pruned": 0,
                "errors": 0,
            },
            "details": [],
            "error": None,
        }
        reg.add(job)
        reg.active = job_id

        self.hs.run_as_background_process(
            "staff_widget_resync",
            _run_resync,
            self.hs,
            self.store,
            job,
        )

        await self._audit_record(
            request=request, outcome=outcome, status=200,
            body={"prune": prune, "staff": only_staff}, target=job_id,
        )
        return 200, _public_job(job)


class StaffWidgetResyncJobServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/widgets/resync/(?P<job_id>[0-9a-f]{32})")

    async def on_GET(self, request, job_id: str) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        job = _registry(self.hs).jobs.get(job_id)
        if job is None:
            raise SynapseError(404, "resync job not found")
        return 200, _public_job(job)


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------

def _detail(job: Dict[str, Any], **row: Any) -> None:
    if len(job["details"]) < MAX_DETAIL_ROWS:
        job["details"].append(row)


async def _run_resync(hs: "HomeServer", store: "StaffStore", job: Dict[str, Any]) -> None:
    reg = _registry(hs)
    clock = hs.get_clock()
    try:
        inj = _injector(hs, store)
        main = hs.get_datastores().main
        state = hs.get_storage_controllers().state

        staff_rows = await store.list_staff_users()
        staff_ids = [r["user_id"] for r in staff_rows]
        if job["staff_filter"]:
            wanted = set(job["staff_filter"])
            staff_ids = [u for u in staff_ids if u in wanted]
        job["progress"]["staff_total"] = len(staff_ids)

        rooms_since_sleep = 0
        res = job["result"]

        for staff_user in staff_ids:
            try:
                joined = await main.get_rooms_for_user(staff_user)
            except Exception as e:
                logger.warning("STAFF resync: get_rooms_for_user(%s) failed: %r", staff_user, e)
                res["errors"] += 1
                _detail(job, staff=staff_user, status="error", reason=repr(e))
                job["progress"]["staff_done"] += 1
                continue

            desired = await inj.desired_widgets_for_staff(staff_user)
            desired_ids: Set[str] = set(desired.keys())

            for room_id in sorted(joined):
                try:
                    if not await inj._is_two_person_dm(room_id, staff_user):
                        continue
                except Exception as e:
                    logger.warning("STAFF resync: DM check failed for %s: %r", room_id, e)
                    continue

                res["rooms_scanned"] += 1
                job["progress"]["rooms_scanned"] = res["rooms_scanned"]

                # --- ensure every desired widget is present + current ---
                for wid, widget in desired.items():
                    inst = await store.widget_instance_get(wid, room_id)
                    sender = (inst or {}).get("injected_by") or staff_user
                    expected = build_widget_state_content(widget, sender)
                    try:
                        cur = await state.get_current_state_event(room_id, WIDGET_EVENT_TYPE, wid)
                    except Exception:
                        cur = None
                    present = cur is not None and bool(cur.content)

                    if present and dict(cur.content) == expected:
                        res["noop"] += 1
                        if inst is None:
                            await store.widget_instance_record(
                                widget_id=wid, room_id=room_id,
                                injected_by=sender, last_state_event_id=cur.event_id,
                            )
                        continue

                    action = "refreshed" if present else "injected"
                    try:
                        ev = await send_event_as(
                            hs, sender=sender, room_id=room_id,
                            event_type=WIDGET_EVENT_TYPE, content=expected, state_key=wid,
                        )
                        await store.widget_instance_record(
                            widget_id=wid, room_id=room_id,
                            injected_by=sender, last_state_event_id=ev.event_id,
                        )
                        res[action] += 1
                        _detail(job, staff=staff_user, room_id=room_id, widget_id=wid,
                                name=widget.get("name"), status=action, event_id=ev.event_id)
                    except Exception as e:
                        logger.warning("STAFF resync: %s widget %s in %s failed: %r",
                                       action, wid, room_id, e)
                        res["errors"] += 1
                        _detail(job, staff=staff_user, room_id=room_id, widget_id=wid,
                                name=widget.get("name"), status="error", reason=repr(e))

                # --- prune tracked instances that are no longer desired ---
                if job["prune"]:
                    for inst in await store.widget_instances_in_room(room_id):
                        wid = inst["widget_id"]
                        if wid in desired_ids:
                            continue
                        sender = inst.get("injected_by") or staff_user
                        try:
                            cur = await state.get_current_state_event(room_id, WIDGET_EVENT_TYPE, wid)
                            if cur is not None and bool(cur.content):
                                ev = await send_event_as(
                                    hs, sender=sender, room_id=room_id,
                                    event_type=WIDGET_EVENT_TYPE, content={}, state_key=wid,
                                )
                                ev_id = ev.event_id
                            else:
                                ev_id = None
                            await store.widget_instance_delete(wid, room_id)
                            res["pruned"] += 1
                            _detail(job, staff=staff_user, room_id=room_id, widget_id=wid,
                                    status="pruned", event_id=ev_id)
                        except Exception as e:
                            logger.warning("STAFF resync: prune widget %s in %s failed: %r",
                                           wid, room_id, e)
                            res["errors"] += 1
                            _detail(job, staff=staff_user, room_id=room_id, widget_id=wid,
                                    status="error", reason=repr(e))

                rooms_since_sleep += 1
                if rooms_since_sleep >= ROOM_BATCH:
                    rooms_since_sleep = 0
                    await clock.sleep(BATCH_SLEEP_S)

            job["progress"]["staff_done"] += 1

        job["status"] = _STATUS_DONE
        logger.info("STAFF resync %s done: %r", job["job_id"], job["result"])
    except Exception as e:
        logger.exception("STAFF resync %s crashed", job["job_id"])
        job["status"] = _STATUS_ERROR
        job["error"] = repr(e)
    finally:
        job["finished_ts"] = int(clock.time_msec())
        if reg.active == job["job_id"]:
            reg.active = None


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    # NB: __init__._register_rest registers this module BEFORE rest_widgets.
    # JsonResource tries patterns in registration order, and rest_widgets'
    # greedy GET "/widgets/(?P<widget_id>[^/]+)" would otherwise swallow
    # GET /widgets/resync as widget_id="resync".
    StaffWidgetResyncServlet(hs, store).register(resource)
    StaffWidgetResyncJobServlet(hs, store).register(resource)
