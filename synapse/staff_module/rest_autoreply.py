#
# STAFF mod — Auto-reply configuration endpoints
#
#   GET  /_synapse/staff/v1/autoreply
#   POST /_synapse/staff/v1/autoreply
#

import json
import logging
import re
from typing import TYPE_CHECKING, Tuple

from synapse.http.servlet import parse_json_object_from_request
from synapse.types import JsonDict

from .rest_base import STAFF_API_PREFIX, StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.http.server import JsonResource
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


class StaffAutoReplyServlet(StaffRestServlet):
    """GET and POST endpoints for managing staff auto-reply status and message."""

    PATTERNS = (
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/autoreply/?$"),
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/auto_reply/?$"),
        re.compile(r"^/_synapse/staff/autoreply/?$"),
        re.compile(r"^/_synapse/staff/auto_reply/?$"),
    )

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        mgr = getattr(self.hs, "_staff_auto_reply_mgr", None)
        if mgr is not None and getattr(mgr, "is_loaded", lambda: True)():
            return 200, mgr.get_config()

        val = await self.store.settings_get("auto_reply")
        if val is None:
            val = await self.store.settings_get("autoreply")

        if isinstance(val, dict):
            config = {
                "enabled": bool(val.get("enabled", False)),
                "message": str(val.get("message", "") or ""),
            }
            if mgr is not None:
                mgr.update_config(config["enabled"], config["message"])
            return 200, config
        elif isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    config = {
                        "enabled": bool(parsed.get("enabled", False)),
                        "message": str(parsed.get("message", "") or ""),
                    }
                    if mgr is not None:
                        mgr.update_config(config["enabled"], config["message"])
                    return 200, config
            except Exception:
                pass

        if mgr is not None:
            return 200, mgr.get_config()
        return 200, {"enabled": False, "message": ""}

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        body = parse_json_object_from_request(request)

        enabled = bool(body.get("enabled", False))
        message = str(body.get("message", "") or "")

        await self.store.settings_upsert(
            "auto_reply", {"enabled": enabled, "message": message}
        )

        mgr = getattr(self.hs, "_staff_auto_reply_mgr", None)
        if mgr is not None:
            mgr.update_config(enabled, message)

        return 200, {"enabled": enabled, "message": message}


def register_servlets(
    hs: "HomeServer", store: "StaffStore", resource: "JsonResource"
) -> None:
    StaffAutoReplyServlet(hs, store).register(resource)
