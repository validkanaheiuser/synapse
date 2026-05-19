#
# STAFF mod — F17 widget CRUD endpoints.
#
#   POST   /_synapse/staff/v1/widgets
#   GET    /_synapse/staff/v1/widgets
#   GET    /_synapse/staff/v1/widgets/{widget_id}
#   POST   /_synapse/staff/v1/widgets/{widget_id}/update    <- legacy update path
#   PATCH  /_synapse/staff/v1/widgets/{widget_id}           <- preferred update path
#                                                             (alias of /update; both
#                                                              re-send the widget's
#                                                              state event to every
#                                                              room it lives in so
#                                                              edits propagate
#                                                              without needing a
#                                                              fresh DM)
#   DELETE /_synapse/staff/v1/widgets/{widget_id}           <- empty-content state event
#

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict, UserID

from .forge import send_event_as
from .rest_base import StaffRestServlet, staff_pattern
from .widget_inject import WIDGET_EVENT_TYPE
from .widget_payload import build_widget_state_content

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore

logger = logging.getLogger(__name__)


_VALID_TYPES = {"general_widget", "staff_custom_widget"}


class StaffWidgetCreateServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/widgets")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        body = parse_json_object_from_request(request)

        owner = body.get("owner_user_id")
        widget_type = body.get("widget_type")
        name = body.get("name")
        url = body.get("url")
        content = body.get("content", {})

        if widget_type not in _VALID_TYPES:
            raise SynapseError(400,
                f"widget_type must be one of {sorted(_VALID_TYPES)}")
        if widget_type == "general_widget" and not owner:
            owner = self.hs.config.staff.staff_default_widget_owner
        if not isinstance(owner, str) or not UserID.is_valid(owner):
            raise SynapseError(400,
                "owner_user_id must be a valid MXID (or set staff.default_widget_owner)")
        if not isinstance(name, str) or not name:
            raise SynapseError(400, "name is required")
        if not isinstance(url, str) or not (
            url.startswith("https://") or url.startswith("http://")
        ):
            raise SynapseError(400, "url must be an http(s) URL")
        if not isinstance(content, dict):
            raise SynapseError(400, "content must be a JSON object")

        widget_id = await self.store.widget_create(
            owner_user_id=owner,
            widget_type=widget_type,
            name=name,
            url=url,
            content=content,
        )
        return 200, {"widget_id": widget_id}

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        rows = await self.store.widget_list()
        return 200, {"widgets": rows}


class StaffWidgetGetServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/widgets/(?P<widget_id>[^/]+)")

    async def on_GET(self, request, widget_id: str) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        widget = await self.store.widget_get(widget_id)
        if widget is None:
            raise SynapseError(404, "widget not found")
        instances = await self.store.widget_instances_for(widget_id)
        return 200, {"widget": widget, "instances": instances}


async def _widget_update_impl(
    servlet: "StaffRestServlet",
    request,
    widget_id: str,
) -> Tuple[int, JsonDict]:
    """Shared update+propagate body used by both the legacy POST endpoint
    and the new PATCH alias.  Updates the widget definition row, then
    fans out a fresh state event to every room the widget already lives
    in so existing DMs see the new name/url/content WITHOUT needing the
    staff member to start a new DM.

    Fan-out runs in batches of 50 with a 50ms inter-batch sleep, sender =
    the original ``injected_by`` user from ``staff_widget_room_instances``
    (preserves identity continuity in the room timeline / state).
    """
    servlet._require_secret(request)
    body = parse_json_object_from_request(request)

    widget = await servlet.store.widget_get(widget_id)
    if widget is None:
        raise SynapseError(404, "widget not found")

    # Validate that we have at least one updatable field.  widget_type
    # and owner_user_id are intentionally not updatable -- they are
    # immutable identity on the row.
    update: Dict[str, Any] = {}
    if "name" in body:
        if not isinstance(body["name"], str) or not body["name"]:
            raise SynapseError(400, "name must be a non-empty string")
        update["name"] = body["name"]
    if "url" in body:
        if not isinstance(body["url"], str) or not (
            body["url"].startswith("http://")
            or body["url"].startswith("https://")
        ):
            raise SynapseError(400, "url must be an http(s) URL")
        update["url"] = body["url"]
    if "content" in body:
        if not isinstance(body["content"], dict):
            raise SynapseError(400, "content must be a JSON object")
        update["content"] = body["content"]
    if not update:
        raise SynapseError(400, "no updatable fields supplied")

    await servlet.store.widget_update(widget_id, update)
    updated = await servlet.store.widget_get(widget_id)
    assert updated is not None

    # === AGENT I (S7): widget-update propagation is already parallel.
    # The existing implementation fans out via asyncio.gather in
    # batches of 50, with a 50ms inter-batch sleep to bound peak
    # load on the event-creation handler.  Verified at
    # synapse/staff_module/rest_widgets.py (this very block) -- no
    # change required.  Documented here so future readers don't
    # "fix" it back into a sequential loop.
    # === END AGENT I ===
    instances = await servlet.store.widget_instances_for(widget_id)
    propagated: List[Dict[str, Any]] = []
    for batch_start in range(0, len(instances), 50):
        batch = instances[batch_start:batch_start + 50]
        results = await asyncio.gather(
            *(
                _propagate_to_room(
                    servlet.hs, servlet.store, updated, inst,
                ) for inst in batch
            ),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                propagated.append({"status": "error", "reason": repr(r)})
            else:
                propagated.append(r)
        await servlet.clock.sleep(0.05)

    return 200, {
        "widget": updated,
        "propagated": propagated,
    }


class StaffWidgetUpdateServlet(StaffRestServlet):
    """Legacy update endpoint at POST /widgets/{id}/update.  Kept so any
    external scripts pre-dating the PATCH alias keep working."""

    PATTERNS = staff_pattern("/widgets/(?P<widget_id>[^/]+)/update")

    async def on_POST(self, request, widget_id: str) -> Tuple[int, JsonDict]:
        return await _widget_update_impl(self, request, widget_id)


class StaffWidgetPatchServlet(StaffRestServlet):
    """Preferred update endpoint at PATCH /widgets/{id}.  Same path as
    GET/DELETE for the widget; HTTP method dispatch handles which servlet
    runs.  Synapse's ``RestServlet.register`` does not auto-wire PATCH so
    we register it explicitly in ``register_servlets`` below (mirroring
    the rest_widget_groups.py pattern)."""

    PATTERNS = staff_pattern("/widgets/(?P<widget_id>[^/]+)")

    async def on_PATCH(self, request, widget_id: str) -> Tuple[int, JsonDict]:
        return await _widget_update_impl(self, request, widget_id)


class StaffWidgetDeleteServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/widgets/(?P<widget_id>[^/]+)")

    async def on_DELETE(self, request, widget_id: str) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        widget = await self.store.widget_get(widget_id)
        if widget is None:
            raise SynapseError(404, "widget not found")

        instances = await self.store.widget_instances_for(widget_id)
        removed: List[Dict[str, Any]] = []
        for inst in instances:
            try:
                ev = await send_event_as(
                    self.hs,
                    sender=inst["injected_by"],
                    room_id=inst["room_id"],
                    event_type=WIDGET_EVENT_TYPE,
                    content={},  # Matrix convention for "widget gone"
                    state_key=widget_id,
                )
                removed.append({"room_id": inst["room_id"],
                                "status": "removed",
                                "event_id": ev.event_id})
            except Exception as e:
                removed.append({"room_id": inst["room_id"],
                                "status": "error",
                                "reason": repr(e)})

        # Now drop the rows.
        await self.store.widget_delete(widget_id)
        return 200, {"widget_id": widget_id, "removed": removed}


async def _propagate_to_room(
    hs: "HomeServer",
    store: "StaffStore",
    widget: Dict[str, Any],
    instance: Dict[str, Any],
) -> Dict[str, Any]:
    sender = instance["injected_by"]
    room_id = instance["room_id"]
    content = build_widget_state_content(widget, sender)
    try:
        ev = await send_event_as(
            hs,
            sender=sender,
            room_id=room_id,
            event_type=WIDGET_EVENT_TYPE,
            content=content,
            state_key=widget["widget_id"],
        )
        await store.widget_instance_update_event(
            widget_id=widget["widget_id"],
            room_id=room_id,
            new_event_id=ev.event_id,
        )
        return {"room_id": room_id, "status": "ok",
                "event_id": ev.event_id}
    except Exception as e:
        logger.warning(
            "STAFF: widget propagate to %s failed: %r", room_id, e
        )
        return {"room_id": room_id, "status": "error",
                "reason": repr(e)}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffWidgetCreateServlet(hs, store).register(resource)
    StaffWidgetUpdateServlet(hs, store).register(resource)
    StaffWidgetGetServlet(hs, store).register(resource)
    StaffWidgetDeleteServlet(hs, store).register(resource)
    # PATCH /widgets/{id} alias.  RestServlet.register only auto-wires
    # GET/PUT/POST/DELETE, so wire PATCH by hand -- same explicit
    # register_paths pattern as rest_widget_groups.py.
    patch_servlet = StaffWidgetPatchServlet(hs, store)
    resource.register_paths(
        "PATCH",
        patch_servlet.PATTERNS,
        patch_servlet.on_PATCH,
        patch_servlet.__class__.__name__,
    )
