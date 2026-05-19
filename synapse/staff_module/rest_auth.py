#
# STAFF mod — S1 + S3 REST surface.
#
# AGENT H.  Hosts the four new endpoints that the JWT auth flow needs:
#
#   POST /_synapse/staff/v1/login_with_password
#   POST /_synapse/staff/v1/refresh
#   POST /_synapse/staff/v1/logout
#   GET  /_synapse/staff/v1/audit/list
#
# `login_with_password` is the ONLY endpoint that's not gated behind
# `_require_staff_auth` (you'd need a token to obtain a token).  Instead
# it consults Synapse's password-auth path directly:
#
#     auth_handler._check_local_password(user_id, password) -> str | None
#
# (verified at synapse/handlers/auth.py:1413-1434).  On success the
# returned canonical user_id MUST appear in `staff_users`; we never
# mint a token for a non-staff member, even if the password is correct.
#

import logging
from typing import TYPE_CHECKING, Optional, Tuple

from synapse.api.errors import AuthError, SynapseError
from synapse.http.servlet import (
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.types import JsonDict, UserID

from .audit import canonical_body_hash
from .auth import _client_ip
from .rest_base import StaffRestServlet, staff_pattern

if TYPE_CHECKING:
    from synapse.http.server import JsonResource
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


def _qualify_user_id(hs: "HomeServer", maybe_local: str) -> str:
    """Accept either a full MXID or a localpart and return a full MXID."""
    if maybe_local.startswith("@"):
        return maybe_local
    return UserID(maybe_local, hs.hostname).to_string()


class StaffLoginServlet(StaffRestServlet):
    """POST /login_with_password — verify password, mint JWT.

    Body: ``{"user_id": "...", "password": "..."}``
    Returns: ``{token, expires_at_ms, jti, user_id}``.

    Rate-limit bucket: "login", keyed on the request IP (no token yet,
    no jti to key on).
    """

    PATTERNS = staff_pattern("/login_with_password")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        # Rate-limit BEFORE we do the expensive bcrypt check, keyed on
        # the IP — protects against credential-stuffing.
        ip = _client_ip(request) or "unknown"
        await self._rate_limiter().check("login", f"ip:{ip}")

        body = parse_json_object_from_request(request)
        user_id_raw = body.get("user_id")
        password = body.get("password")
        if not isinstance(user_id_raw, str) or not user_id_raw:
            raise SynapseError(400, "user_id is required")
        if not isinstance(password, str) or not password:
            raise SynapseError(400, "password is required")
        user_id = _qualify_user_id(self.hs, user_id_raw)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "user_id is not a valid MXID")

        # Verify the password via Synapse's local-password path.
        # NB: this returns the *canonical* user_id (case-corrected) on
        # success, or None on failure.  Verified signature at
        # synapse/handlers/auth.py:1413-1434.
        auth_handler = self.hs.get_auth_handler()
        canonical = await auth_handler._check_local_password(user_id, password)
        if not canonical:
            # Don't differentiate "no such user" from "wrong password".
            raise AuthError(401, "Invalid user_id or password")

        # Must be on the staff allowlist; password alone is not enough.
        if not self.store.is_staff_user(canonical):
            logger.warning(
                "STAFF login: %s authenticated but is not in staff_users",
                canonical,
            )
            raise AuthError(403, "User is not a staff member")

        # Mint the token.
        signed = await self._jwt_keys().sign(canonical)

        # Audit (no AuthOutcome here — this is a public endpoint).
        # We synthesize an "actor_kind=login" row so audit-trail
        # readers see the login attempt.  The body has a password in
        # it; the helper hashes the body, so the password never lands
        # in the audit table.
        path = (
            request.path.decode("ascii", "replace") if request.path else ""
        )
        method = (
            request.method.decode("ascii", "replace") if request.method else ""
        )
        await self._audit_writer().record(
            actor_user_id=canonical,
            actor_kind="login",
            endpoint=path,
            method=method,
            status=200,
            target=canonical,
            body_hash=canonical_body_hash(
                # NEVER include `password` in the hashed body, even though
                # the hash itself is one-way: a shared password across
                # users would produce identical body_hash rows, leaking
                # the equality.  Strip it before hashing.
                {k: v for k, v in body.items() if k != "password"},
            ),
            ip=ip,
        )

        return 200, {
            "user_id": canonical,
            "token": signed["token"],
            "expires_at_ms": signed["expires_at_ms"],
            "jti": signed["jti"],
        }


class StaffRefreshServlet(StaffRestServlet):
    """POST /refresh — exchange a near-expiry token for a fresh one."""

    PATTERNS = staff_pattern("/refresh")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        # Same default rate-limit bucket as the rest of the surface;
        # refresh is not particularly hot-path.
        ip = _client_ip(request) or "unknown"
        await self._rate_limiter().check("_default", f"ip:{ip}")

        body = parse_json_object_from_request(request)
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise SynapseError(400, "token is required")

        try:
            payload = await self._jwt_keys().verify(token)
        except ValueError as e:
            logger.info("STAFF refresh: verify failed: %s", e)
            raise AuthError(401, "Invalid token")

        sub = payload["sub"]
        old_jti = payload["jti"]
        old_exp = int(payload["exp"])

        if await self.store.jwt_revocation_check(old_jti):
            raise AuthError(401, "Token revoked")
        if not self.store.is_staff_user(sub):
            raise AuthError(401, "User no longer staff")
        if not self._jwt_keys().is_refreshable(payload):
            raise AuthError(401, "Token not eligible for refresh yet")

        # Issue a new token and revoke the old jti so it can't be used
        # twice — refresh is not stateless.
        signed = await self._jwt_keys().sign(sub)
        await self.store.jwt_revocation_add(old_jti, exp_ts=old_exp)

        path = (
            request.path.decode("ascii", "replace") if request.path else ""
        )
        method = (
            request.method.decode("ascii", "replace") if request.method else ""
        )
        await self._audit_writer().record(
            actor_user_id=sub,
            actor_kind="jwt",
            endpoint=path,
            method=method,
            status=200,
            target=sub,
            body_hash="",  # body had the old token; not worth hashing
            ip=ip,
        )

        return 200, {
            "user_id": sub,
            "token": signed["token"],
            "expires_at_ms": signed["expires_at_ms"],
            "jti": signed["jti"],
        }


class StaffLogoutServlet(StaffRestServlet):
    """POST /logout — add the jti to the revocation deny-list.

    The caller does not need to be authenticated by Bearer to log out
    (consider the case where the token is leaked and you want to revoke
    it).  We DO require the body to contain a valid-shape token that
    decodes correctly with one of our keys, otherwise we'd be a
    public scratch-pad for arbitrary jti strings.
    """

    PATTERNS = staff_pattern("/logout")

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        ip = _client_ip(request) or "unknown"
        await self._rate_limiter().check("_default", f"ip:{ip}")

        body = parse_json_object_from_request(request)
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise SynapseError(400, "token is required")

        try:
            payload = await self._jwt_keys().verify(token)
        except ValueError as e:
            # Even if the token is expired we still let the caller
            # "log it out" silently — but only if the signature
            # would be valid.  The verify() path covers signature
            # before expiry checks, so a bad-sig token bombs here.
            logger.info("STAFF logout: verify failed: %s", e)
            raise AuthError(401, "Invalid token")

        jti = payload["jti"]
        exp = int(payload["exp"])
        await self.store.jwt_revocation_add(jti, exp_ts=exp)

        path = (
            request.path.decode("ascii", "replace") if request.path else ""
        )
        method = (
            request.method.decode("ascii", "replace") if request.method else ""
        )
        await self._audit_writer().record(
            actor_user_id=payload.get("sub"),
            actor_kind="jwt",
            endpoint=path,
            method=method,
            status=200,
            target=payload.get("sub"),
            body_hash="",
            ip=ip,
        )
        return 200, {"revoked": True, "jti": jti}


class StaffAuditListServlet(StaffRestServlet):
    """GET /audit/list — paginated, server-side-filtered audit dump."""

    PATTERNS = staff_pattern("/audit/list")

    async def on_GET(self, request) -> Tuple[int, JsonDict]:
        outcome = await self._require_staff_auth(request)

        from_ts = parse_integer(request, "from", required=False)
        to_ts = parse_integer(request, "to", required=False)
        actor = parse_string(request, "actor", required=False)
        endpoint = parse_string(request, "endpoint", required=False)
        limit = parse_integer(request, "limit", required=False, default=200)
        offset = parse_integer(request, "offset", required=False, default=0)

        rows = await self.store.audit_query(
            from_ts=from_ts,
            to_ts=to_ts,
            actor=actor,
            endpoint=endpoint,
            limit=limit,
            offset=offset,
        )

        await self._audit_record(
            request=request,
            outcome=outcome,
            status=200,
            body=None,
            target=None,
        )
        return 200, {
            "rows": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }


def register_servlets(
    hs: "HomeServer", store: "StaffStore", resource: "JsonResource",
) -> None:
    StaffLoginServlet(hs, store).register(resource)
    StaffRefreshServlet(hs, store).register(resource)
    StaffLogoutServlet(hs, store).register(resource)
    StaffAuditListServlet(hs, store).register(resource)
