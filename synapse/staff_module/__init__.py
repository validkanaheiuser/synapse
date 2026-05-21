#
# STAFF mod -- module entry point
#
# Loaded from synapse/app/_base.py when `staff.enabled: true` in
# homeserver.yaml.  Owns the StaffStore singleton (attached to `hs` as
# `hs._staff_store`), registers every staff REST endpoint as a single
# JsonResource mounted at `/_synapse/staff`, registers the on_new_event
# callback that powers F16 widget auto-injection, and registers the
# scheduler action for F12.
#

import logging
from typing import TYPE_CHECKING

from twisted.internet import defer

from synapse.http.server import JsonResource
from synapse.util.duration import Duration

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

        # === AGENT H ===
        # S1/S2/S3 collaborators.  These are constructed here (not in
        # StaffStore) because they reach back into hs.get_clock() /
        # hs.get_datastores() and StaffStore is meant to be a pure
        # storage layer.  Attached to the HomeServer via private attrs
        # so the REST base class (rest_base.py) can fetch them on each
        # request -- same pattern as `_staff_store` above.
        from .audit import StaffAuditWriter
        from .jwt_keys import StaffJwtKeyManager
        from .rate_limit import StaffRateLimiter

        self._staff_jwt_keys = StaffJwtKeyManager(self._hs, self._store)
        self._staff_audit_writer = StaffAuditWriter(self._hs, self._store)
        self._staff_rate_limiter = StaffRateLimiter(self._hs)
        setattr(self._hs, "_staff_jwt_keys", self._staff_jwt_keys)
        setattr(self._hs, "_staff_audit_writer", self._staff_audit_writer)
        setattr(self._hs, "_staff_rate_limiter", self._staff_rate_limiter)

        # Prime the JWT key cache in the background so the first login
        # doesn't pay the "mint a fresh key + INSERT" latency.  We can't
        # await in __init__, so schedule it the same way the staff
        # allowlist priming below schedules itself.
        from synapse.metrics.background_process_metrics import (
            run_as_background_process as _run_bg,
        )

        _run_bg(
            "staff_jwt_key_init",
            self._hs.hostname,
            self._staff_jwt_keys.ensure_initialised,
        )
        # === END AGENT H ===

        # === AGENT I (S4): synchronous prime of the staff allow-list.
        # The original implementation kicked off `prime_staff_cache` as a
        # fire-and-forget background process.  That left an observable
        # window between module-load and first-priming during which
        # every `is_staff_user(...)` call returned False, which silently
        # demotes every staff request to a non-staff one until the
        # priming completed.  We can't `await` in `__init__` itself, but
        # we *can* schedule the priming via `clock.call_when_running`
        # (verified at synapse/util/clock.py:417-485 -- runs the
        # awaitable on the reactor in a LoggingContext) so the reactor
        # blocks on it before serving any traffic.  If it raises, we
        # propagate the error so Synapse refuses to start (cleaner than
        # silently running with a broken allow-list).  We use
        # `defer.ensureDeferred` to bridge our coroutine into the Twisted
        # callback world -- same pattern used by `synapse/app/_base.py`
        # at line 315.
        clock = self._hs.get_clock()

        async def _prime_then_record() -> None:
            try:
                tracked = getattr(
                    self._store, "prime_staff_cache_tracked", None,
                )
                if tracked is not None:
                    await tracked()
                else:
                    await self._store.prime_staff_cache()
            except Exception:
                logger.exception(
                    "STAFF: prime_staff_cache failed during module init; "
                    "homeserver will start with an empty allow-list",
                )
                raise

        clock.call_when_running(
            lambda: defer.ensureDeferred(_prime_then_record())
        )

        # === AGENT P ===
        # One-time auto-seeder for the widget-groups migration.  When an
        # operator upgrades from a pre-AGENT-P deployment the old
        # general_widgets were injected for every staff; the new injector
        # only injects widgets reachable via group membership.  To keep
        # behaviour identical on first boot we synthesise a "Default"
        # group containing every existing general_widget and every
        # existing staff.  We gate on a staff_settings entry so this only
        # runs once per database, even across restarts.
        #
        # Runs AFTER the prime_staff_cache scheduling above but BEFORE
        # REST registration in the synchronous init flow.  Actual
        # execution happens on the reactor via call_when_running, and we
        # never raise out -- the homeserver must start even if the
        # seeder hits an unexpected DB state.
        async def _maybe_seed_default_group() -> None:
            try:
                already = await self._store.settings_get(
                    "groups_default_seeded",
                )
                if already is not None:
                    return

                general_widgets = await self._store.widget_list(
                    widget_type="general_widget",
                )
                staff_users = await self._store.list_staff_users()
                if general_widgets and staff_users:
                    group_id = await self._store.group_create(
                        name="Default",
                        description=(
                            "Auto-created on upgrade - contains all "
                            "pre-existing general widgets and staff"
                        ),
                        created_by="agent_p_migration",
                    )
                    await self._store.group_update(
                        group_id,
                        {
                            "widgets": [w["widget_id"] for w in general_widgets],
                            "members": [u["user_id"] for u in staff_users],
                        },
                    )
                    logger.info(
                        "STAFF: AGENT P seeded Default widget group %s "
                        "with %d widgets / %d staff",
                        group_id, len(general_widgets), len(staff_users),
                    )
                else:
                    logger.info(
                        "STAFF: AGENT P skipped Default-group seeding "
                        "(general_widgets=%d, staff=%d)",
                        len(general_widgets), len(staff_users),
                    )
                # Always mark the flag so we don't recheck every boot,
                # even when nothing was seeded.
                await self._store.settings_upsert(
                    "groups_default_seeded", "1",
                )
            except Exception:
                logger.exception(
                    "STAFF: AGENT P widget-group auto-seeder failed; "
                    "homeserver will continue without a Default group",
                )

        clock.call_when_running(
            lambda: defer.ensureDeferred(_maybe_seed_default_group())
        )
        # === END AGENT P ===

        # === AGENT I (S5): optional periodic reloader of the staff
        # allow-list.  Acts as a safety net for deployments where the
        # cache-invalidation-stream broadcast can't reach every worker
        # (e.g. due to a misconfigured replication topology).  Disabled
        # by default; opt in by setting `staff.refresh_interval_sec`.
        try:
            interval = int(
                getattr(config, "staff_refresh_interval_sec", 0) or 0
            )
        except (TypeError, ValueError):
            interval = 0
        if interval > 0:
            clock.looping_call(
                lambda: self._hs.run_as_background_process(
                    "staff_refresh_allowlist",
                    self._store.prime_staff_cache_tracked,
                ),
                Duration(seconds=interval),
            )
            logger.info(
                "STAFF: enabled periodic allow-list refresh every %ds",
                interval,
            )
        # === END AGENT I ===

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

        # === F1 enforcement: refuse every m.room.encryption event.
        # The homeserver config disables AUTO-injection of encryption on
        # room creation, but a client (or another module) can still send
        # m.room.encryption via /createRoom's initial_state or a later
        # /state PUT.  `EncryptionBlocker` rejects every such event at
        # the third-party-rules layer so encryption can never land in any
        # room kind (DM, group DM, public, private).
        from .force_no_encryption import EncryptionBlocker

        self._encryption_blocker = EncryptionBlocker()

        # Register BOTH callbacks in a single call.  The module API merges
        # multiple registrations, but combining them keeps the wire-up
        # site contiguous and the ordering deterministic.
        self._api.register_third_party_rules_callbacks(
            on_new_event=self._widget_injector.on_new_event,
            check_event_allowed=self._encryption_blocker.check_event_allowed,
        )

        # Wire up the scheduler action (F12).
        from .scheduler import register_scheduler

        register_scheduler(self._hs, self._store)

        # === AGENT I (S20): federation guard.  This module assumes
        # federation is disabled (or strictly limited) -- forging events
        # as a deactivated user, restoring messages, stealth-redact and
        # so on are only safe when no remote server can audit them.  Read
        # `synapse/config/federation.py:38-44` to confirm the right
        # attribute name: `federation_domain_whitelist` is a dict
        # (mapping domain -> True) when set, or None when absent.  An
        # *empty* list in YAML produces an empty dict here, which is the
        # documented "block everything" sentinel; anything else is a
        # warning sign.
        try:
            fed = getattr(self._hs.config, "federation", None)
            whitelist = getattr(fed, "federation_domain_whitelist", None)
            if whitelist is None:
                logger.warning(
                    "STAFF: federation_domain_whitelist is unset -- "
                    "this deployment may federate to arbitrary servers. "
                    "STAFF features (forge, stealth-redact, restore) "
                    "assume federation is disabled.  Set "
                    "`federation_domain_whitelist: []` in homeserver.yaml "
                    "to silence this warning."
                )
            elif isinstance(whitelist, dict) and len(whitelist) > 0:
                logger.warning(
                    "STAFF: federation_domain_whitelist contains %d "
                    "domain(s) -- forging events while federated to "
                    "remote servers is undefined behaviour.  Domains: %s",
                    len(whitelist), sorted(whitelist.keys()),
                )
        except Exception:
            logger.exception("STAFF: federation guard check failed")
        # === END AGENT I ===

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
        # === AGENT H ===
        from . import rest_auth  # S1 login/refresh/logout + S3 audit-list
        # === END AGENT H ===
        # === AGENT I ===
        from . import rest_health  # S18 /staff/v1/health
        # === END AGENT I ===
        # === AGENT N ===
        from . import rest_admin_listing  # admin users/list, bulk_delete,
        # rooms/external, account_create
        # === END AGENT N ===
        # === AGENT P ===
        from . import rest_widget_groups  # /groups CRUD
        # === END AGENT P ===

        for mod in (
            rest_staff_admin,
            rest_settings,
            rest_users,
            rest_messages,
            rest_edit,
            rest_schedule,
            rest_widgets,
            # === AGENT H ===
            rest_auth,
            # === END AGENT H ===
            # === AGENT I ===
            rest_health,
            # === END AGENT I ===
            # === AGENT N ===
            rest_admin_listing,
            # === END AGENT N ===
            # === AGENT P ===
            rest_widget_groups,
            # === END AGENT P ===
        ):
            mod.register_servlets(self._hs, self._store, self._json_resource)


def get_staff_store(hs) -> "StaffStore | None":
    """Convenience accessor used by core patches.  Returns None if STAFF
    mod is not loaded.
    """
    return getattr(hs, "_staff_store", None)
