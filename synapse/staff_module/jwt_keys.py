#
# STAFF mod — S1 JWT signing key management.
#
# AGENT H.  PyJWT is only a dev-time dependency in synapse's poetry.lock
# (group = "dev"), so we cannot rely on it at runtime.  Instead, we
# implement HS256 JWS directly using the standard library (hmac +
# hashlib + base64).  That is also strictly safer: the failure mode of
# our bespoke implementation is "unable to verify our own tokens"; the
# failure mode of importing PyJWT at runtime would be `ImportError` on
# every staff request.
#
# Token format (per RFC 7519 / 7515 compact serialization):
#
#   base64url(header).base64url(payload).base64url(HMAC-SHA256(...))
#
# `header`  = {"alg":"HS256","typ":"JWT","kid":"<key_id>"}
# `payload` = {"sub":<user_id>,"iat":<sec>,"exp":<sec>,"jti":<uuid hex>}
#
# Verification accepts ANY key currently present in staff_jwt_keys
# (active or not).  Rotation marks the old key inactive but keeps it for
# verifying in-flight tokens until they expire naturally.
#

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


TOKEN_LIFETIME_SEC = 24 * 60 * 60  # 24h
REFRESH_WINDOW_SEC = 4 * 60 * 60   # only refresh in last 4h of lifetime


# ---------------------------------------------------------------- helpers


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # Restore padding stripped by `_b64url_encode`.
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _json_b64(obj: Dict[str, Any]) -> str:
    return _b64url_encode(
        json.dumps(
            obj, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    )


def _hmac_sha256(secret: bytes, msg: bytes) -> bytes:
    return hmac.new(secret, msg, hashlib.sha256).digest()


# ----------------------------------------------------------- key manager


class StaffJwtKeyManager:
    """Owns the HMAC signing keys for the staff JWT.  At least one
    active key always exists: on first use we mint one and persist it.
    """

    def __init__(self, hs: "HomeServer", store: "StaffStore") -> None:
        self._hs = hs
        self._store = store
        self._clock = hs.get_clock()
        # Cache of all known keys, keyed by key_id.  Populated on demand
        # (rotation/verification may consult historical keys).
        self._keys: Dict[str, bytes] = {}
        self._active_kid: Optional[str] = None

    async def ensure_initialised(self) -> None:
        """Load all keys from DB; mint a fresh active key if none exist."""
        if self._active_kid is not None:
            return
        row = await self._store.jwt_key_get_current()
        if row is None:
            await self._mint_new_active_key()
            return
        self._active_kid = row["key_id"]
        self._keys[row["key_id"]] = base64.b64decode(row["secret_b64"])

    async def _mint_new_active_key(self) -> str:
        kid = uuid.uuid4().hex
        secret = secrets.token_bytes(32)
        secret_b64 = base64.b64encode(secret).decode("ascii")
        await self._store.jwt_key_rotate(
            new_key_id=kid,
            new_secret_b64=secret_b64,
        )
        self._keys[kid] = secret
        self._active_kid = kid
        logger.info("STAFF JWT: minted new active key kid=%s", kid)
        return kid

    async def rotate(self) -> str:
        """Mint a new active key and deactivate the previous one.  The
        previous key remains in the table so in-flight tokens signed by
        it still verify until they expire."""
        return await self._mint_new_active_key()

    async def _get_key_by_kid(self, kid: str) -> Optional[bytes]:
        # Cache hit?
        cached = self._keys.get(kid)
        if cached is not None:
            return cached
        # Otherwise fetch from DB.  Older inactive keys are stored too.
        row = await self._store.jwt_key_get_by_id(kid)
        if row is None:
            return None
        secret = base64.b64decode(row["secret_b64"])
        self._keys[kid] = secret
        return secret

    # ----------------------------------------------------------- signing

    async def sign(self, user_id: str) -> Dict[str, Any]:
        """Return {token, expires_at_ms, jti, kid}.  Caller decides
        whether to also issue a refresh token (we don't ship one — see
        the spec: client re-logs in on expiry)."""
        await self.ensure_initialised()
        assert self._active_kid is not None
        secret = self._keys[self._active_kid]
        now = int(self._clock.time())
        exp = now + TOKEN_LIFETIME_SEC
        jti = uuid.uuid4().hex
        header = {"alg": "HS256", "typ": "JWT", "kid": self._active_kid}
        payload = {"sub": user_id, "iat": now, "exp": exp, "jti": jti}
        signing_input = f"{_json_b64(header)}.{_json_b64(payload)}".encode("ascii")
        sig = _b64url_encode(_hmac_sha256(secret, signing_input))
        token = f"{signing_input.decode('ascii')}.{sig}"
        return {
            "token": token,
            "expires_at_ms": exp * 1000,
            "jti": jti,
            "kid": self._active_kid,
            "exp_sec": exp,
        }

    # --------------------------------------------------------- verifying

    async def verify(self, token: str) -> Dict[str, Any]:
        """Decode + verify a token.  Returns the payload dict on
        success.  Raises ValueError(reason) on any failure.  Caller is
        responsible for checking revocation (the manager does not own
        the revocation table)."""
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("malformed token")
        h_b64, p_b64, s_b64 = parts
        try:
            header = json.loads(_b64url_decode(h_b64))
            payload = json.loads(_b64url_decode(p_b64))
            signature = _b64url_decode(s_b64)
        except Exception:
            raise ValueError("malformed token")
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise ValueError("malformed token")
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            raise ValueError("unsupported alg/typ")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ValueError("missing kid")
        secret = await self._get_key_by_kid(kid)
        if secret is None:
            raise ValueError("unknown kid")
        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        expected = _hmac_sha256(secret, signing_input)
        if not hmac.compare_digest(expected, signature):
            raise ValueError("bad signature")
        # Validate basic claims now that the signature is good.
        now = int(self._clock.time())
        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise ValueError("missing exp")
        if exp <= now:
            raise ValueError("token expired")
        sub = payload.get("sub")
        jti = payload.get("jti")
        if not isinstance(sub, str) or not sub:
            raise ValueError("missing sub")
        if not isinstance(jti, str) or not jti:
            raise ValueError("missing jti")
        return payload

    def is_refreshable(self, payload: Dict[str, Any]) -> bool:
        """Refresh policy: only allow refresh when the token has less
        than REFRESH_WINDOW_SEC of life remaining.  Fresh tokens just
        keep being used; near-expiry tokens get a new mint."""
        now = int(self._clock.time())
        exp = payload.get("exp", 0)
        return (exp - now) <= REFRESH_WINDOW_SEC
