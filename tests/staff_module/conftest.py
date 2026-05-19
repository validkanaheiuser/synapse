#
# Shared test harness for the STAFF mod tests (S11).
#
# Provides `StaffHomeserverTestCase`, a HomeserverTestCase subclass that:
#
#   1. Adds a `staff:` config block (enabled + admin secret + ICT timezone)
#      so the global `hs.config.staff` is populated.
#   2. Adds a `modules:` entry that loads `synapse.staff_module.StaffModule`
#      via the standard module loader (matches the production wiring).
#   3. Merges the module-registered web resources (the staff REST mount at
#      `/_synapse/staff`) into the test site's resource tree so that
#      `self.make_request("POST", "/_synapse/staff/v1/...", ...)` actually
#      reaches the staff servlets.
#   4. Exposes helpers for: setting the X-Staff-Secret + X-STAFF-Client
#      headers, registering staff vs non-staff users, and waiting for the
#      asynchronous `prime_staff_cache` background task to complete.
#
# All staff_module tests inherit from this base instead of HomeserverTestCase
# directly so the wiring is consistent and the individual tests only assert
# behavior.
#

from typing import Any, Iterable

from twisted.web.resource import Resource

from synapse.rest import admin
from synapse.rest.client import login, profile, room, sync
from synapse.types import JsonDict

from tests.unittest import HomeserverTestCase


STAFF_SECRET = "test-staff-secret-deadbeef"
STAFF_HEADER_NAME = b"X-STAFF-Client"
STAFF_HEADER_VALUE = b"1"
STAFF_SECRET_HEADER = b"X-Staff-Secret"


class StaffHomeserverTestCase(HomeserverTestCase):
    """Base class for all staff_module tests.

    Subclasses get a HomeServer with the staff module loaded, the staff
    REST mount routed through the test site, and helpers for staff auth.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        profile.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Wire up the staff:* config block (read by synapse.config.staff).
        config["staff"] = {
            "enabled": True,
            "admin_secret": STAFF_SECRET,
            "timezone": "Asia/Ho_Chi_Minh",
            "push_account_data_for_web": False,
        }
        # Load the staff module via the modules mechanism so the test
        # path matches the production path.
        config["modules"] = [
            {
                "module": "synapse.staff_module.StaffModule",
                "config": {},
            }
        ]
        # Tests don't want the auto-accept-invites module to interfere
        # with our explicit join flows, but we leave the option open via
        # override_config in individual tests.
        return config

    def create_resource_dict(self) -> dict[str, Resource]:
        # Standard servlet tree from HomeserverTestCase, plus any
        # module-registered resources (the staff JsonResource mounted at
        # /_synapse/staff by StaffModule.__init__).
        resources = super().create_resource_dict()
        module_resources = getattr(self.hs, "_module_web_resources", {})
        for path, resource in module_resources.items():
            resources[path] = resource
        return resources

    def _staff_headers(
        self,
        *,
        with_client_header: bool = True,
    ) -> Iterable[tuple[bytes, bytes]]:
        headers: list[tuple[bytes, bytes]] = [
            (STAFF_SECRET_HEADER, STAFF_SECRET.encode("ascii")),
        ]
        if with_client_header:
            headers.append((STAFF_HEADER_NAME, STAFF_HEADER_VALUE))
        return headers

    def _client_header_only(self) -> Iterable[tuple[bytes, bytes]]:
        return [(STAFF_HEADER_NAME, STAFF_HEADER_VALUE)]

    def add_staff_user(self, mxid: str) -> None:
        """Insert a user into the staff_users allowlist and refresh the
        in-memory cache so that the `is_staff_user` hot-path sees them."""
        store = self.hs._staff_store
        self.get_success(
            store.add_staff_user(mxid, added_by="test", note="conftest")
        )

    def prime_cache(self) -> None:
        """Force a synchronous reload of the staff allowlist cache.  The
        background prime in StaffModule.__init__ is non-deterministic in
        the test reactor; calling this in `prepare` is safer."""
        store = getattr(self.hs, "_staff_store", None)
        if store is None:
            return
        self.get_success(store.prime_staff_cache())
