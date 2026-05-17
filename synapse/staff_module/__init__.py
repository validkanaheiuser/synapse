#
# STAFF mod — module entry point
#
# Loaded from synapse/app/_base.py when `staff.enabled: true` in
# homeserver.yaml.  Owns the StaffStore singleton (attached to `hs` as
# `hs.staff_store`), registers every staff REST endpoint as a single
# JsonResource mounted at `/_synapse/staff`, registers the on_new_event
# callback that powers F16 widget auto-injection, and registers the
# scheduler action for F12.
#

import logging
from typing import TYPE_CHECKING, Any

from synapse.http.server import JsonResource
from synapse.util.async_helpers import maybe_awaitable

from .store import StaffStore

if TYPE_CHECKING:
    from synapse.module_api import ModuleApi
    from synapse.config.staff import StaffConfig

logger = logging.getLogger(__name__)


class StaffModule:
    def __init__(self, config: "StaffConfig", api: "ModuleApi") -> None:
        self._api = api
        self._config = config
        # `api._hs` is the documented internal accessor used by
        # synapse.events.auto_accept_invites and the third-party-rules tests.
        self._hs = api._hs

        # Attach the store + module ref to the HomeServer so that core
        # patches (synapse/staff_filter.py callers) can look them up via
        # `hs.get_staff_store()`.
        self._store = StaffStore(self._hs)
        setattr(self._hs, "_staff_store", self._store)
        setattr(self._hs, "_staff_module", self)

        # Prime the in-memory staff allowlist asynchronously.
        self._api.run_db_interaction(
            "staff_prime_cache_kick",
            lambda txn: None,
        )
        # We can't await here (constructor is sync), so schedule a
        # background load.  is_staff_user returns False until the priming
        # finishes — acceptable for cold-start (a few hundred ms).
        from synapse.metrics.background_process_metrics import run_as_background_process

        run_as_background_process(
            "staff_prime_cache",
            self._store.prime_staff_cache,
        )

        # Build the mounted REST resource.
        self._json_resource = JsonResource(self._hs)
        self._register_rest()

        # Mount at the staff path prefix.  JsonResource matches the FULL
        # path (it sees the original request path), so it will correctly
        # route requests to `/_synapse/staff/v1/...`.
        self._api.register_web_resource("/_synapse/staff", self._json_resource)

        # Wire up event hooks (F16 widget DM detection).
        from .widget_inject import WidgetInjector

        self._widget_injector = WidgetInjector(self._hs, self._store)
        self._api.register_third_party_rules_callbacks(
            on_new_event=self._widget_injector.on_new_event,
        )

        # Wire up the scheduler action (F12).
        from .scheduler import register_scheduler

        register_scheduler(self._hs, self._store)

        logger.info("STAFF: module loaded; admin secret configured; "
                    "API mounted at /_synapse/staff/v1")

    def _register_rest(self) -> None:
        from . import (
            rest_staff_admin,
            rest_settings,
            rest_users,
            rest_messages,
            rest_edit,
            rest_schedule,
            rest_widgets,
        )

        for mod in (
            rest_staff_admin,
            rest_settings,
            rest_users,
            rest_messages,
            rest_edit,
            rest_schedule,
            rest_widgets,
        ):
            mod.register_servlets(self._hs, self._store, self._json_resource)


def get_staff_store(hs) -> "StaffStore | None":
    """Convenience accessor used by core patches.  Returns None if STAFF
    mod is not loaded.
    """
    return getattr(hs, "_staff_store", None)
