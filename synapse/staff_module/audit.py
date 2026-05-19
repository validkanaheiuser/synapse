#
# STAFF mod — S3 audit log.
#
# AGENT H.  Writes one row to staff_audit_log per successful staff
# request.  The request body is hashed (SHA-256 over a canonical JSON
# encoding) so we never persist a plaintext password; we still record
# the actor, endpoint, IP, and a target hint extracted by the caller.
#

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


def canonical_body_hash(body: Any) -> str:
    """SHA-256 of the canonical-JSON encoding of `body`.

    `sort_keys=True` + `separators=(",", ":")` gives a deterministic
    representation regardless of dict insertion order.  Returns an empty
    string when `body` cannot be JSON-encoded (audit table column is
    nullable; an empty string is preferable to crashing the request).
    """
    if body is None:
        return ""
    try:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def extract_target(body: Any) -> Optional[str]:
    """Best-effort target extraction for human-readable audit display.

    Looks for common identifier fields in the body, in priority order.
    Returns None when nothing useful is present.  Never raises.
    """
    if not isinstance(body, dict):
        return None
    for key in ("room_id", "user_id", "task_id", "event_id",
                "widget_id", "username", "original_event_id"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return v
    # array fields — return the count to keep the audit row short
    for key in ("event_ids", "usernames"):
        v = body.get(key)
        if isinstance(v, list) and v:
            return f"{key}:{len(v)}"
    return None


class StaffAuditWriter:
    """Thin wrapper around StaffStore.audit_insert.  Logs each row at
    INFO level too so operators can tail synapse.log instead of querying
    the database.  Failures are swallowed: an audit-write failure must
    not break the user request."""

    def __init__(self, hs: "HomeServer", store: "StaffStore") -> None:
        self._hs = hs
        self._store = store
        self._clock = hs.get_clock()

    async def record(
        self,
        *,
        actor_user_id: Optional[str],
        actor_kind: str,
        endpoint: str,
        method: str,
        status: int,
        target: Optional[str],
        body_hash: str,
        ip: Optional[str],
    ) -> None:
        ts = int(self._clock.time_msec())
        try:
            await self._store.audit_insert(
                ts=ts,
                actor_user_id=actor_user_id,
                actor_kind=actor_kind,
                endpoint=endpoint,
                method=method,
                status=status,
                target=target,
                body_hash=body_hash,
                ip=ip,
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(
                "STAFF audit write failed (endpoint=%s actor=%s): %r",
                endpoint, actor_user_id, e,
            )
            return
        logger.info(
            "STAFF audit: actor=%s kind=%s %s %s -> %d target=%s ip=%s",
            actor_user_id, actor_kind, method, endpoint, status, target, ip,
        )
