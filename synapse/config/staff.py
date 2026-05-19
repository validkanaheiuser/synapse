#
# STAFF mod config
#
# Provides the `staff:` block in homeserver.yaml.  All STAFF behaviour is
# gated on the presence of this block; if it is absent, no behaviour changes.
#

from typing import Any, List, Optional

from synapse.types import JsonDict

from ._base import Config, ConfigError


# === AGENT I (S15): default set of state-event types that the visibility
# filters relocate / strip for non-staff clients.  Kept here so operators
# can override via the `staff.hidden_state_types` YAML key without code
# changes.  Matches the original `synapse.staff_filter.HIDDEN_STATE_TYPES`
# frozenset (kept in lock-step intentionally).
_DEFAULT_HIDDEN_STATE_TYPES: List[str] = [
    "m.room.member",
    "m.room.power_levels",
    "m.room.join_rules",
    "m.room.history_visibility",
    "m.room.guest_access",
    "m.room.encryption",
    "m.room.canonical_alias",
    "m.room.name",
    "m.room.topic",
    "m.room.avatar",
    "m.room.create",
    "m.room.server_acl",
    "im.vector.modular.widgets",
    "m.widget",
]
# === END AGENT I ===


# === AGENT I (S17): minimum length for staff.admin_secret.  32 hex chars =
# 128 bits of entropy, which is the floor below which a brute force is
# feasible.  We accept any printable string of >=32 chars, but the error
# message guides operators to `openssl rand -hex 32` which is unambiguously
# secure.
_MIN_ADMIN_SECRET_LEN = 32
# === END AGENT I ===


class StaffConfig(Config):
    section = "staff"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        staff_config = config.get("staff") or {}

        self.staff_enabled = bool(staff_config.get("enabled", False))
        self.staff_admin_secret = staff_config.get("admin_secret")
        self.staff_timezone = staff_config.get("timezone", "Asia/Ho_Chi_Minh")
        self.staff_header_name = staff_config.get(
            "staff_header_name", "X-STAFF-Client"
        )
        self.staff_header_value = staff_config.get("staff_header_value", "1")
        self.staff_default_widget_owner = staff_config.get(
            "default_widget_owner"
        )
        self.staff_push_account_data_for_web = bool(
            staff_config.get("push_account_data_for_web", True)
        )

        # === AGENT I (S15): hidden_state_types override.  Operators may
        # supply a custom list to add or remove types (e.g. include
        # custom widget types used by their deployment).  We validate
        # only that the value is a list of strings; semantic validity is
        # the operator's responsibility.
        hidden_raw = staff_config.get("hidden_state_types")
        if hidden_raw is None:
            self.staff_hidden_state_types: List[str] = list(
                _DEFAULT_HIDDEN_STATE_TYPES
            )
        else:
            if not isinstance(hidden_raw, list) or not all(
                isinstance(s, str) for s in hidden_raw
            ):
                raise ConfigError(
                    "staff.hidden_state_types must be a list of strings",
                    ("staff", "hidden_state_types"),
                )
            self.staff_hidden_state_types = list(hidden_raw)
        # === END AGENT I ===

        # === AGENT I (S5 fallback): if the cache-invalidation-stream
        # broadcast is unreliable in some deployments, allow operators to
        # set a periodic full-reload of the staff allow-list as a safety
        # net.  Value is in seconds; `0` disables the reloader (the
        # default, since the replication stream is the preferred path).
        try:
            self.staff_refresh_interval_sec = int(
                staff_config.get("refresh_interval_sec", 0)
            )
        except (TypeError, ValueError):
            raise ConfigError(
                "staff.refresh_interval_sec must be an integer (seconds)",
                ("staff", "refresh_interval_sec"),
            )
        if self.staff_refresh_interval_sec < 0:
            raise ConfigError(
                "staff.refresh_interval_sec must be >= 0",
                ("staff", "refresh_interval_sec"),
            )
        # === END AGENT I ===

        # === AGENT I (S19): system_user used by restore_message when the
        # original sender of an edit-history snapshot has been
        # deactivated.  Optional - if unset, restore falls back to using
        # the original sender's MXID regardless of activation status
        # (which is the legacy behaviour).
        self.staff_system_user: Optional[str] = staff_config.get(
            "system_user"
        )
        if self.staff_system_user is not None and (
            not isinstance(self.staff_system_user, str)
            or not self.staff_system_user.startswith("@")
            or ":" not in self.staff_system_user
        ):
            raise ConfigError(
                "staff.system_user must be a valid MXID (e.g. "
                "'@system:server')",
                ("staff", "system_user"),
            )
        # === END AGENT I ===

        if self.staff_enabled and not self.staff_admin_secret:
            raise ConfigError(
                "staff.admin_secret must be set when staff.enabled is true",
                ("staff", "admin_secret"),
            )

        # === AGENT I (S17): admin secret strength check.  Performed only
        # after the presence check above to keep the existing error
        # message as the primary signal for misconfiguration.
        if self.staff_enabled and isinstance(self.staff_admin_secret, str):
            if len(self.staff_admin_secret) < _MIN_ADMIN_SECRET_LEN:
                raise ConfigError(
                    "staff.admin_secret must be at least "
                    f"{_MIN_ADMIN_SECRET_LEN} random chars "
                    "(use `openssl rand -hex 32`)",
                    ("staff", "admin_secret"),
                )
        # === END AGENT I ===
