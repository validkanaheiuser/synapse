#
# STAFF mod — authentication for /_synapse/staff/v1/*.
#
# AGENT H — rewritten (S1).  Two auth modes are supported, in priority
# order:
#
#   1. `Authorization: Bearer <jwt>`  — preferred.  Tokens are minted by
#      POST /_synapse/staff/v1/login_with_password after the user proves
#      they hold the password for a member of the `staff_users`
#      allowlist.  See `jwt_keys.py` for the token format.
#
#   2. `X-Staff-Secret: <secret>`     — legacy / deprecated.  Still
#      accepted so unattended scripts that pre-date the JWT rollout
#      keep working.  Every legacy auth emits a `STAFF auth deprecated
#      path` warning to nudge migration.
#
# `_require_staff_auth` is the only function the REST surface needs to
# call.  It returns an `AuthOutcome` describing which mode succeeded,
# the actor identity for audit, and the rate-limit key seed.  All other
# helpers in this module are package-private to that flow.
#

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from synapse.api.errors import AuthError

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from twisted.web.iweb import IRequest

    from .jwt_keys import StaffJwtKeyManager
    from .store import StaffStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthOutcome:
    """Result of a successful staff-auth check.  `kind` is one of:

        "jwt"     - validated via Bearer token; `actor_user_id` is the
                    JWT `sub` claim and `jti` is the unique token id.
        "secret"  - validated via legacy X-Staff-Secret; `actor_user_id`
                    is None (no caller identity is conveyed by the
                    shared secret).
    """
    kind: str
    actor_user_id: Optional[str]
    jti: Optional[str]
    rate_limit_actor: str


# --------------------------------------------------------------- header helpers


def _read_header(request: "IRequest", name: str) -> Optional[str]:
    raw = request.getHeader(name.encode("ascii"))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw  # type: ignore[unreachable]


def _client_ip(request: "IRequest") -> Optional[str]:
    """Best-effort: SynapseRequest exposes getClientAddress() that
    returns either an IPv4/6 address with a `.host` or a UNIX socket
    address.  We fall back to None if anything looks wrong."""
    try:
        addr = request.getClientAddress()
        host = getattr(addr, "host", None)
        if isinstance(host, str) and host:
            return host
    except Exception:
        return None
    return None


# ------------------------------------------------------------ secret auth path


def _check_staff_secret(request: "IRequest", hs: "HomeServer") -> bool:
    """Return True if the X-Staff-Secret header matches the configured
    secret.  Returns False when the header is absent.  Raises
    AuthError(401) when the header is present but wrong (so we don't
    silently fall through to "missing auth")."""
    expected = hs.config.staff.staff_admin_secret
    raw = _read_header(request, "X-Staff-Secret")
    if raw is None:
        return False
    if not expected:
        raise AuthError(500, "STAFF mod admin_secret is not configured")
    if not hmac.compare_digest(raw, expected):
        raise AuthError(401, "Bad X-Staff-Secret")
    return True


# --------------------------------------------------------------- bearer path


def _read_bearer(request: "IRequest") -> Optional[str]:
    raw = _read_header(request, "Authorization")
    if raw is None:
        return None
    parts = raw.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    if not token:
        return None
    return token


async def _check_bearer(
    request: "IRequest",
    hs: "HomeServer",
    store: "StaffStore",
    key_manager: "StaffJwtKeyManager",
) -> Optional[AuthOutcome]:
    """Validate a Bearer JWT.  Returns an AuthOutcome on success, None
    if no Bearer header was present (so the caller can try the legacy
    secret path).  Raises AuthError(401) on any validation failure."""
    token = _read_bearer(request)
    if token is None:
        return None
    try:
        payload = await key_manager.verify(token)
    except ValueError as e:
        # Don't leak the precise reason to clients — the parameter name
        # alone is enough.  The body message stays generic; details are
        # in the server log.
        logger.info("STAFF JWT verify failed: %s", e)
        raise AuthError(401, "Invalid token")
    sub = payload["sub"]
    jti = payload["jti"]
    # Revocation list: jti must not be in staff_jwt_revocations.
    if await store.jwt_revocation_check(jti):
        raise AuthError(401, "Token revoked")
    # Staff allowlist must still contain `sub` (staff status may have
    # been revoked since the token was minted).
    if not store.is_staff_user(sub):
        raise AuthError(401, "User no longer staff")
    return AuthOutcome(
        kind="jwt",
        actor_user_id=sub,
        jti=jti,
        rate_limit_actor=f"jti:{jti}",
    )


# ----------------------------------------------------------------- entry point


async def require_staff_auth(
    request: "IRequest",
    hs: "HomeServer",
    store: "StaffStore",
    key_manager: "StaffJwtKeyManager",
) -> AuthOutcome:
    """Authenticate a staff request.  Bearer JWT first, fall back to
    legacy X-Staff-Secret.  Raises AuthError(401) on failure."""
    # Try bearer first.  A 401 raised here aborts before we fall back to
    # the secret path — a client who sends Authorization explicitly
    # expects to be authenticated by that and should not be silently
    # downgraded.
    outcome = await _check_bearer(request, hs, store, key_manager)
    if outcome is not None:
        return outcome
    # Bearer absent — try the legacy secret.
    if _check_staff_secret(request, hs):
        ip = _client_ip(request) or "unknown"
        logger.warning(
            "STAFF auth deprecated path: X-Staff-Secret used from ip=%s "
            "path=%s — migrate to JWT (POST /login_with_password)",
            ip, request.path.decode("ascii", "replace") if request.path else "?",
        )
        return AuthOutcome(
            kind="secret",
            actor_user_id=None,
            jti=None,
            rate_limit_actor=f"secret:{ip}",
        )
    raise AuthError(401, "Missing staff auth")


# ----------------------------------------------------------- backwards compat


def check_staff_secret(request: "IRequest", hs: "HomeServer") -> None:
    """Legacy synchronous secret-only check.  Kept as an alias so any
    code paths that imported the old name still work; new code should
    call `require_staff_auth` via `StaffRestServlet._require_staff_auth`
    which threads through the JWT + audit + rate-limit pipeline."""
    if not _check_staff_secret(request, hs):
        raise AuthError(401, "Missing X-Staff-Secret header")
