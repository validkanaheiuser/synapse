#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import logging
from typing import TYPE_CHECKING

from synapse.api.errors import SynapseError
from synapse.api.ratelimiting import Ratelimiter
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonMapping

from ._base import client_patterns

# === STAFF-MOD BEGIN ===
from synapse.staff_filter import is_staff_request
# === STAFF-MOD END ===

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class UserDirectorySearchRestServlet(RestServlet):
    PATTERNS = client_patterns("/user_directory/search$")
    CATEGORY = "User directory search requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.user_directory_handler = hs.get_user_directory_handler()

        self._per_user_limiter = Ratelimiter(
            store=hs.get_datastores().main,
            clock=hs.get_clock(),
            cfg=hs.config.ratelimiting.rc_user_directory,
        )

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonMapping]:
        """Searches for users in directory

        Returns:
            dict of the form::

                {
                    "limited": <bool>,  # whether there were more results or not
                    "results": [  # Ordered by best match first
                        {
                            "user_id": <user_id>,
                            "display_name": <display_name>,
                            "avatar_url": <avatar_url>
                        }
                    ]
                }
        """
        requester = await self.auth.get_user_by_req(request, allow_guest=False)
        user_id = requester.user.to_string()

        if not self.hs.config.userdirectory.user_directory_search_enabled:
            return 200, {"limited": False, "results": []}

        await self._per_user_limiter.ratelimit(requester)

        body = parse_json_object_from_request(request)

        limit = int(body.get("limit", 10))
        limit = max(min(limit, 50), 0)

        try:
            search_term = body["search_term"]
        except Exception:
            raise SynapseError(400, "`search_term` is required field")

        # === STAFF-MOD BEGIN: F6 exact-MXID-only for non-staff ===
        is_staff = await is_staff_request(request, self.hs, requester)
        if not is_staff:
            # Non-staff: only return a result if the search term is a
            # complete MXID (starts with @, contains a colon).  Anything
            # else returns an empty list — including partial fragments
            # like "asdb" or "@asdb".
            if (
                isinstance(search_term, str)
                and search_term.startswith("@")
                and ":" in search_term
            ):
                try:
                    info = await self.hs.get_datastores().main.get_userinfo_by_id(
                        search_term
                    )
                except Exception:
                    info = None
                if info is None or getattr(info, "is_deactivated", False):
                    return 200, {"limited": False, "results": []}
                try:
                    profile = (
                        await self.hs.get_profile_handler()
                        .get_profile(search_term)
                    )
                except Exception:
                    profile = {}
                return 200, {
                    "limited": False,
                    "results": [
                        {
                            "user_id": search_term,
                            "display_name": (profile or {}).get("displayname"),
                            "avatar_url": (profile or {}).get("avatar_url"),
                        }
                    ],
                }
            return 200, {"limited": False, "results": []}
        # === STAFF-MOD END ===

        results = await self.user_directory_handler.search_users(
            user_id, search_term, limit
        )

        return 200, results


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    UserDirectorySearchRestServlet(hs).register(http_server)
