#
# STAFF mod — S2 per-token, per-endpoint rate limiting.
#
# AGENT H.  Wraps `synapse.api.ratelimiting.Ratelimiter` with one limiter
# per endpoint key.  Buckets are tuned for the staff use case: aggressive
# but not crippling for legitimate admin work, hostile to scripts that
# steal a token and try to abuse it.
#
# A bucket is identified by an arbitrary string ("wipe_room",
# "force_logout", ...).  Rate-limit keys are then `(endpoint_key, actor)`
# where `actor` is the JWT jti when authenticated via Bearer, the literal
# "secret" when authenticated via X-Staff-Secret, or the request IP when
# unauthenticated (only reachable for /login_with_password).
#

import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from synapse.api.errors import LimitExceededError
from synapse.api.ratelimiting import Ratelimiter
from synapse.config.ratelimiting import RatelimitSettings

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# (per_second, burst_count) — burst_count is the bucket capacity, i.e.
# how many actions can land instantly before the leak rate kicks in.
#
# Bucket math is "leaky bucket": at steady state, the per-minute rate
# the spec asked for == `burst_count` (burst happens, then bucket leaks
# at `per_second` until refilled).  We size `per_second = burst / 60`
# so refilling a fully-drained bucket takes exactly one minute, which
# matches the "per minute" spec language.
_BUCKETS: Dict[str, Tuple[float, int]] = {
    # admin actions — heavily limited
    "wipe_room":        (1 / 60.0, 1),
    "create_user":      (5 / 60.0, 5),
    # moderation actions — medium
    "force_logout":    (10 / 60.0, 10),
    # routine moderation — generous
    "delete_messages": (30 / 60.0, 30),
    "edit_message":    (30 / 60.0, 30),
    "schedule":        (30 / 60.0, 30),
    "restore_message": (60 / 60.0, 60),
    # default catch-all for everything else (settings, listing, audit,
    # widgets, staff-allowlist admin, login/refresh/logout)
    "_default":        (60 / 60.0, 60),
    # login — protect against credential stuffing; per-IP, not per-jti
    "login":            (5 / 60.0, 5),
}


class StaffRateLimiter:
    """One Ratelimiter per endpoint bucket.  Constructed lazily on first
    use so the StaffStore is reachable when buckets need to consult
    ratelimit overrides (none of ours do; the limiter still requires a
    DataStore on construction)."""

    def __init__(self, hs: "HomeServer") -> None:
        self._hs = hs
        self._clock = hs.get_clock()
        self._store = hs.get_datastores().main
        self._limiters: Dict[str, Ratelimiter] = {}

    def _limiter(self, bucket: str) -> Ratelimiter:
        existing = self._limiters.get(bucket)
        if existing is not None:
            return existing
        per_second, burst = _BUCKETS.get(bucket, _BUCKETS["_default"])
        cfg = RatelimitSettings(
            key=f"staff.{bucket}",
            per_second=per_second,
            burst_count=burst,
        )
        limiter = Ratelimiter(
            store=self._store,
            clock=self._clock,
            cfg=cfg,
        )
        self._limiters[bucket] = limiter
        return limiter

    @staticmethod
    def bucket_for_endpoint(endpoint_path: str) -> str:
        """Map a `/_synapse/staff/v1/...` path to a bucket key.

        Matches the longest leading segment we have a bucket for; falls
        back to "_default" otherwise.  Examples:

            /_synapse/staff/v1/wipe_room       -> "wipe_room"
            /_synapse/staff/v1/schedule        -> "schedule"
            /_synapse/staff/v1/schedule/abc    -> "schedule"
            /_synapse/staff/v1/login_with_password -> "login"
            /_synapse/staff/v1/refresh         -> "_default"
        """
        if "/_synapse/staff/v1/" not in endpoint_path:
            return "_default"
        tail = endpoint_path.split("/_synapse/staff/v1/", 1)[1]
        first = tail.split("/", 1)[0] if tail else ""
        # alias login_with_password -> "login" bucket
        if first == "login_with_password":
            return "login"
        if first in _BUCKETS:
            return first
        return "_default"

    async def check(self, bucket: str, actor_key: str) -> None:
        """Consume one token from `bucket` for `actor_key`.  Raises
        LimitExceededError (HTTP 429 with Retry-After) on overflow."""
        limiter = self._limiter(bucket)
        # `key` is the rate-limit dimension we want to track.  We use
        # (bucket, actor_key) so per-bucket counts don't bleed across.
        key = (bucket, actor_key)
        await limiter.ratelimit(
            requester=None,
            key=key,
            pause=0.0,  # do not async-sleep server-side; surface 429 instantly
        )
