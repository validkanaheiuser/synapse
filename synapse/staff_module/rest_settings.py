#
# STAFF mod — F13 settings KV endpoints.
#
#   GET    /_synapse/staff/v1/settings/get/all
#   GET    /_synapse/staff/v1/settings/get/{key}
#   POST   /_synapse/staff/v1/settings/update/{key}    body = raw JSON, stored verbatim
#   DELETE /_synapse/staff/v1/settings/{key}
#

import re
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import SynapseError
from synapse.http.servlet import (
    parse_json_value_from_request,
    parse_string,
)
from synapse.types import JsonDict

from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.http.server import JsonResource
    from .store import StaffStore


_VALID_KEY = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


class StaffSettingsGetAllServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/settings/get/all")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        self._require_secret(request)
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
        return 200, {"settings": rows}


class StaffSettingsGetServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/settings/get/(?P<key>[^/]+)")

    async def on_GET(self, request, key: str) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        if not _VALID_KEY.match(key):
            raise SynapseError(400, "invalid key")
        value = await self.store.settings_get(key)
        if value is None:
            raise SynapseError(404, "not found")
        return 200, {"key": key, "value": value}


class StaffSettingsUpdateServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/settings/update/(?P<key>[^/]+)")

    async def on_POST(self, request, key: str) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        if not _VALID_KEY.match(key):
            raise SynapseError(400, "invalid key")
        value = parse_json_value_from_request(request)
        await self.store.settings_upsert(key, value)
        return 200, {"key": key, "value": value}


class StaffSettingsDeleteServlet(StaffRestServlet):
    PATTERNS = staff_pattern("/settings/(?P<key>[^/]+)")

    async def on_DELETE(self, request, key: str) -> Tuple[int, JsonDict]:
        self._require_secret(request)
        if not _VALID_KEY.match(key):
            raise SynapseError(400, "invalid key")
        deleted = await self.store.settings_delete(key)
        return 200, {"key": key, "removed": bool(deleted)}


def register_servlets(hs: "HomeServer", store: "StaffStore",
                      resource: "JsonResource") -> None:
    StaffSettingsGetAllServlet(hs, store).register(resource)
    StaffSettingsGetServlet(hs, store).register(resource)
    StaffSettingsUpdateServlet(hs, store).register(resource)
    StaffSettingsDeleteServlet(hs, store).register(resource)
