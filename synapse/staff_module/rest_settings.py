#
# STAFF mod — F13 settings KV endpoints.
#
#   GET    /_synapse/staff/v1/settings/get/all
#   GET    /_synapse/staff/v1/settings/get/{key}
#   POST   /_synapse/staff/v1/settings/update/{key}    body = raw JSON, stored verbatim
#   DELETE /_synapse/staff/v1/settings/{key}
#

import json
import re
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import (
    parse_json_value_from_request,
    parse_string,
)
from synapse.types import JsonDict

from .rest_base import STAFF_API_PREFIX, StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore


# Allows the characters that legitimate KV keys actually use:
#   - alphanumerics, underscore, dot, dash       — namespace + plain id
#   - colon (`:`)                                — separator in compound keys
#                                                  e.g. `nickname:<me>:<target>`,
#                                                  `quickmsg:<id>`
#   - at-sign (`@`)                              — MXID local-part marker
#                                                  embedded in the keys above
# Length bumped to 256 so two full MXIDs comfortably fit in a
# nickname:<setter>:<target> key.
_VALID_KEY = re.compile(r"^[A-Za-z0-9_.:@\-]{1,256}$")


class StaffSettingsGetAllServlet(StaffRestServlet):
    PATTERNS = (
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/settings/get/all/?$"),
        re.compile(r"^/_synapse/staff/settings/get/all/?$"),
    )

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        # === AGENT I (S6): optional `?prefix=` server-side filter ===
        # Cheap to keep on the server: avoids round-tripping the full
        # settings table for callers that only care about a namespace
        # (e.g. STAFF UI lists keys grouped by `ui.`, `policy.`, ...).
        prefix = parse_string(request, "prefix", default=None)
        if prefix is not None:
            # Same character class as the key validator above so the
            # filter can never inject through the equality column.
            if not _VALID_KEY.match(prefix.rstrip(".") or "x"):
                # Allow trailing `.` but otherwise apply the same charset.
                # A `.`-only or empty effective prefix is rejected.
                raise SynapseError(400, "invalid prefix")
            getter = getattr(self.store, "settings_get_all_filtered", None)
            if getter is not None:
                rows = await getter(prefix=prefix)
                return 200, {"settings": rows, "prefix": prefix}
        # === END AGENT I ===
        rows = await self.store.settings_get_all()
        if "auto_reply" not in rows:
            mgr = getattr(self.hs, "_staff_auto_reply_mgr", None)
            if mgr is not None:
                rows["auto_reply"] = mgr.get_config()
            else:
                rows["auto_reply"] = {"enabled": False, "message": ""}
        return 200, {"settings": rows}


class StaffSettingsGetServlet(StaffRestServlet):
    PATTERNS = (
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/settings/get/(?P<key>[^/]+)/?$"),
        re.compile(r"^/_synapse/staff/settings/get/(?P<key>[^/]+)/?$"),
    )

    async def on_GET(self, request, key: str) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        if not _VALID_KEY.match(key):
            raise SynapseError(400, "invalid key")
        value = await self.store.settings_get(key)
        if key in ("auto_reply", "autoreply"):
            mgr = getattr(self.hs, "_staff_auto_reply_mgr", None)
            if value is None:
                if mgr is not None:
                    value = mgr.get_config()
                else:
                    value = {"enabled": False, "message": ""}
            elif isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    pass
            return 200, {"key": key, "value": value}
        if value is None:
            raise SynapseError(404, "not found")
        return 200, {"key": key, "value": value}


class StaffSettingsUpdateServlet(StaffRestServlet):
    PATTERNS = (
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/settings/update/(?P<key>[^/]+)/?$"),
        re.compile(r"^/_synapse/staff/settings/update/(?P<key>[^/]+)/?$"),
    )

    async def on_POST(self, request, key: str) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        if not _VALID_KEY.match(key):
            raise SynapseError(400, "invalid key")
        value = parse_json_value_from_request(request)
        await self.store.settings_upsert(key, value)
        if key in ("auto_reply", "autoreply"):
            mgr = getattr(self.hs, "_staff_auto_reply_mgr", None)
            if mgr is not None and isinstance(value, dict):
                mgr.update_config(
                    bool(value.get("enabled", False)),
                    str(value.get("message", "") or ""),
                )
        return 200, {"key": key, "value": value}


class StaffSettingsDeleteServlet(StaffRestServlet):
    PATTERNS = (
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/settings/(?P<key>[^/]+)/?$"),
        re.compile(r"^/_synapse/staff/settings/(?P<key>[^/]+)/?$"),
    )

    async def on_DELETE(self, request, key: str) -> Tuple[int, JsonDict]:
        await self._require_secret(request)
        if not _VALID_KEY.match(key):
            raise SynapseError(400, "invalid key")
        deleted = await self.store.settings_delete(key)
        if key in ("auto_reply", "autoreply"):
            mgr = getattr(self.hs, "_staff_auto_reply_mgr", None)
            if mgr is not None:
                mgr.update_config(False, "")
        return 200, {"key": key, "removed": bool(deleted)}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffSettingsGetAllServlet(hs, store).register(resource)
    StaffSettingsGetServlet(hs, store).register(resource)
    StaffSettingsUpdateServlet(hs, store).register(resource)
    StaffSettingsDeleteServlet(hs, store).register(resource)
