#
# STAFF mod — auth helpers
#
# Two auth modes for STAFF endpoints:
#  - `X-Staff-Secret: <secret>` (matches staff.admin_secret)
#  - `assert_requester_is_admin` for endpoints that need a logged-in admin
#

import hmac
import logging
from typing import TYPE_CHECKING

from synapse.api.errors import AuthError

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from twisted.web.iweb import IRequest

logger = logging.getLogger(__name__)


def check_staff_secret(request: "IRequest", hs: "HomeServer") -> None:
    """Raise AuthError(401) if the X-Staff-Secret header doesn't match.

    Constant-time comparison to avoid timing leaks on the secret.
    """
    expected = hs.config.staff.staff_admin_secret
    if not expected:
        raise AuthError(500, "STAFF mod admin_secret is not configured")
    raw = request.getHeader(b"X-Staff-Secret")
    if raw is None:
        raise AuthError(401, "Missing X-Staff-Secret header")
    try:
        provided = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AuthError(401, "Invalid X-Staff-Secret header")
    if not hmac.compare_digest(provided, expected):
        raise AuthError(401, "Bad X-Staff-Secret")
