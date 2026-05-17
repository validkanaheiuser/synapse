#
# STAFF mod config
#
# Provides the `staff:` block in homeserver.yaml.  All STAFF behaviour is
# gated on the presence of this block; if it is absent, no behaviour changes.
#

from typing import Any

from synapse.types import JsonDict

from ._base import Config, ConfigError


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

        if self.staff_enabled and not self.staff_admin_secret:
            raise ConfigError(
                "staff.admin_secret must be set when staff.enabled is true",
                ("staff", "admin_secret"),
            )
