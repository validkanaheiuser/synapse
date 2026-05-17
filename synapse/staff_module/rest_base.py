#
# STAFF mod — base REST servlet
#
# Provides the shared structure that every staff_module endpoint reuses:
# pattern construction, auth check, access to the StaffStore, hs.
#

import re
from typing import TYPE_CHECKING, Pattern

from synapse.http.servlet import RestServlet

from .auth import check_staff_secret

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from .store import StaffStore


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

    def _require_secret(self, request) -> None:
        check_staff_secret(request, self.hs)
