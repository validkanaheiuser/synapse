#
# STAFF mod — base REST servlet
#
# Provides the shared structure that every staff_module endpoint reuses:
# pattern construction, auth check, access to the StaffStore, hs.
#

import logging
import re
from typing import TYPE_CHECKING, Any, Optional, Pattern

from synapse.http.servlet import RestServlet

from .auth import check_staff_secret

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from .audit import StaffAuditWriter
    from .auth import AuthOutcome
    from .jwt_keys import StaffJwtKeyManager
    from .rate_limit import StaffRateLimiter
    from .store import StaffStore


logger = logging.getLogger(__name__)

STAFF_API_PREFIX = "/_synapse/staff/v1"


def staff_pattern(path: str) -> "tuple[Pattern[str], ...]":
    """Build a compiled-regex tuple for a staff REST path.

    `path` should start with "/" and may include named groups like
    "(?P<task_id>[^/]+)".
    """
    assert path.startswith("/")
    return (re.compile("^" + re.escape(STAFF_API_PREFIX) + path + "$"),)


class StaffRestServlet(RestServlet):
    """Base for every STAFF REST servlet.  Subclass and set PATTERNS via
    staff_pattern("/...").  Override on_GET / on_POST / etc.
    """

    def __init__(self, hs: "HomeServer", store: "StaffStore"):
        self.hs = hs
        self.store = store
        self.clock = hs.get_clock()

    # === AGENT H ===
    # Auth + audit + rate-limit pipeline.  Every staff servlet now goes
    # through `_require_staff_auth` (preferred) instead of the old
    # `_require_secret`.  `_require_secret` is retained as a thin
    # shim that delegates to the new path so legacy servlets we don't
    # own keep working without a code change at their call-sites.
    #
    # The pipeline is:
    #   1. Validate Bearer JWT (or fall back to legacy X-Staff-Secret).
    #   2. Apply the per-endpoint rate-limit bucket keyed on the actor.
    #   3. (After the handler returns) the caller is expected to invoke
    #      `_audit_record(...)` with the response status + extracted
    #      target.  We provide a `_run_with_audit` convenience that wraps
    #      a handler call but the simpler per-endpoint pattern is to
    #      call `_audit_record` directly at the end of the handler since
    #      handlers want to choose their own `target` value.
    #
    # All four collaborators (key_manager, audit_writer, rate_limiter,
    # store) live on the HomeServer via private attributes set by
    # `StaffModule` at module init.

    def _jwt_keys(self) -> "StaffJwtKeyManager":
        return getattr(self.hs, "_staff_jwt_keys")

    def _audit_writer(self) -> "StaffAuditWriter":
        return getattr(self.hs, "_staff_audit_writer")

    def _rate_limiter(self) -> "StaffRateLimiter":
        return getattr(self.hs, "_staff_rate_limiter")

    async def _require_staff_auth(self, request) -> "AuthOutcome":
        """Authenticate the request and consume one rate-limit token.

        Returns the AuthOutcome describing the validated caller.  The
        servlet should stash this somewhere (e.g. a local variable) and
        pass it to `_audit_record` after the work completes."""
        from .auth import require_staff_auth
        from .rate_limit import StaffRateLimiter

        outcome = await require_staff_auth(
            request, self.hs, self.store, self._jwt_keys(),
        )
        # Rate limit per (bucket, actor).  Bucket comes from the path,
        # actor from the AuthOutcome.
        path = request.path.decode("ascii", "replace") if request.path else ""
        bucket = StaffRateLimiter.bucket_for_endpoint(path)
        await self._rate_limiter().check(bucket, outcome.rate_limit_actor)
        return outcome

    async def _audit_record(
        self,
        *,
        request,
        outcome: "AuthOutcome",
        status: int,
        body: Any = None,
        target: Optional[str] = None,
    ) -> None:
        """Write an audit row for a request that just completed."""
        from .audit import canonical_body_hash, extract_target
        from .auth import _client_ip

        path = request.path.decode("ascii", "replace") if request.path else ""
        method = (
            request.method.decode("ascii", "replace")
            if request.method else ""
        )
        if target is None:
            target = extract_target(body)
        await self._audit_writer().record(
            actor_user_id=outcome.actor_user_id,
            actor_kind=outcome.kind,
            endpoint=path,
            method=method,
            status=status,
            target=target,
            body_hash=canonical_body_hash(body),
            ip=_client_ip(request),
        )

    async def _require_secret(self, request) -> None:
        """Lightweight authentication gate kept around for the many staff
        endpoints that don't need the full audit + rate-limit pipeline of
        `_require_staff_auth`.

        Despite the historical name, this now accepts any of:
          - `Authorization: Bearer <matrix_access_token>` whose MXID is in
            the staff_users allowlist  (preferred — element-web uses this)
          - `Authorization: Bearer <staff_jwt>`  (legacy /login_with_password)
          - `X-Staff-Secret: <admin_secret>`     (legacy / unattended scripts)

        Must be awaited.  Raises AuthError on failure."""
        from synapse.api.errors import AuthError

        from .auth import _check_bearer, _check_staff_secret, _read_bearer

        if _read_bearer(request) is not None:
            # _check_bearer dispatches Matrix-token vs staff-JWT internally
            # and raises AuthError on any failure.  Returns an AuthOutcome
            # we don't need here (audit-less legacy path).
            await _check_bearer(request, self.hs, self.store, self._jwt_keys())
            return
        if not _check_staff_secret(request, self.hs):
            raise AuthError(401, "Missing staff auth (Bearer token or X-Staff-Secret)")
    # === END AGENT H ===
