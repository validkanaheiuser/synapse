#
# STAFF mod — F2.a element-web account_data push.
#
# On a user's first /sync after STAFF mod deploys, we write
# `im.vector.web.settings.showRedactions = false` into their account_data.
# element-web's AccountSettingsHandler listens for ClientEvent.AccountData
# and applies the new value in realtime (no re-login).  Has no effect on
# element-android / element-ios (their equivalent settings are device-local).
#
# Idempotency: we only push once per user, tracked in
# staff_account_data_pushed.  We preserve any pre-existing keys in the
# user's im.vector.web.settings event so unrelated settings are untouched.
#

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


_ACCOUNT_DATA_TYPE = "im.vector.web.settings"
_SETTING_KEY = "showRedactions"


async def maybe_push_web_settings(
    hs: "HomeServer", store: "StaffStore", user_id: str
) -> None:
    """Push the showRedactions=false setting if we haven't already done so
    for this user.  Cheap to call on every sync — backed by an idempotency
    table.
    """
    config = hs.config.staff
    if not config.staff_push_account_data_for_web:
        return
    try:
        already = await store.has_pushed_account_data(user_id)
        if already:
            return
        existing = await hs.get_datastores().main.get_global_account_data_by_type_for_user(
            user_id, _ACCOUNT_DATA_TYPE
        )
        merged: Dict[str, Any] = dict(existing) if existing else {}
        if merged.get(_SETTING_KEY) is False:
            await store.mark_account_data_pushed(user_id)
            return
        merged[_SETTING_KEY] = False
        await hs.get_account_data_handler().add_account_data_for_user(
            user_id, _ACCOUNT_DATA_TYPE, merged
        )
        await store.mark_account_data_pushed(user_id)
        logger.info(
            "STAFF: pushed showRedactions=false to %s via account_data",
            user_id,
        )
    except Exception:
        # Never fail the user's /sync because of a settings push.
        logger.exception(
            "STAFF: failed to push account_data settings for %s", user_id
        )
