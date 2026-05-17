#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import re
from typing import TYPE_CHECKING

from synapse.api.constants import Direction
from synapse.handlers.relations import ThreadsListInclude
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_boolean, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.storage.databases.main.relations import ThreadsNextBatch
from synapse.streams.config import PaginationConfig
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

# === STAFF-MOD BEGIN ===
from synapse.staff_filter import is_staff_request
# === STAFF-MOD END ===

logger = logging.getLogger(__name__)


class RelationPaginationServlet(RestServlet):
    """API to paginate relations on an event by topological ordering, optionally
    filtered by relation type and event type.
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/relations/(?P<parent_id>[^/]*)"
        "(/(?P<relation_type>[^/]*)(/(?P<event_type>[^/]*))?)?$",
        releases=("v1",),
    )
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._hs = hs
        self.auth = hs.get_auth()
        self._store = hs.get_datastores().main
        self._relations_handler = hs.get_relations_handler()

    async def on_GET(
        self,
        request: SynapseRequest,
        room_id: str,
        parent_id: str,
        relation_type: str | None = None,
        event_type: str | None = None,
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)

        pagination_config = await PaginationConfig.from_request(
            self._store, request, default_limit=5, default_dir=Direction.BACKWARDS
        )
        recurse = parse_boolean(request, "recurse", default=False) or parse_boolean(
            request, "org.matrix.msc3981.recurse", default=False
        )

        # The unstable version of this API returns an extra field for client
        # compatibility, see https://github.com/matrix-org/synapse/issues/12930.
        assert request.path is not None
        include_original_event = request.path.startswith(b"/_matrix/client/unstable/")

        # Return the relations
        result = await self._relations_handler.get_relations(
            requester=requester,
            event_id=parent_id,
            room_id=room_id,
            pagin_config=pagination_config,
            recurse=recurse,
            include_original_event=include_original_event,
            relation_type=relation_type,
            event_type=event_type,
        )

        # === STAFF-MOD BEGIN: hide edit history + redacted children from non-staff ===
        is_staff = await is_staff_request(request, self._hs, requester)
        if not is_staff:
            chunk = result.get("chunk", [])
            kept = []
            for ev in chunk:
                rel = (ev.get("content") or {}).get("m.relates_to") or {}
                if rel.get("rel_type") == "m.replace":
                    continue
                if (ev.get("unsigned") or {}).get("redacted_because"):
                    continue
                kept.append(ev)
            result["chunk"] = kept
            # Empty out the pagination tokens so clients don't keep paging
            # through nothing.
            result["next_batch"] = None
            result["prev_batch"] = None
        # === STAFF-MOD END ===

        return 200, result


class ThreadsServlet(RestServlet):
    PATTERNS = (re.compile("^/_matrix/client/v1/rooms/(?P<room_id>[^/]*)/threads"),)
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self._relations_handler = hs.get_relations_handler()

    async def on_GET(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        limit = parse_integer(request, "limit", default=5)
        from_token_str = parse_string(request, "from")
        include = parse_string(
            request,
            "include",
            default=ThreadsListInclude.all.value,
            allowed_values=[v.value for v in ThreadsListInclude],
        )

        # Return the relations
        from_token = None
        if from_token_str:
            from_token = ThreadsNextBatch.from_string(from_token_str)

        result = await self._relations_handler.get_threads(
            requester=requester,
            room_id=room_id,
            include=ThreadsListInclude(include),
            limit=limit,
            from_token=from_token,
        )

        return 200, result


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    RelationPaginationServlet(hs).register(http_server)
    ThreadsServlet(hs).register(http_server)
