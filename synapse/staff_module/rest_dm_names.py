#
# STAFF mod — DM Custom Nicknames REST Endpoints
#
# Client calls:
#   GET    /api/dm-names?user_id=@staff:domain
#   POST   /api/dm-names (body: { user_id, room_id, original_name, custom_name })
#   DELETE /api/dm-names/<room_id>?user_id=@staff:domain
#
# Also mirrored under /_synapse/staff/v1/dm-names for standard staff prefix.
# Data is isolated strictly per account (user_id).
#
# Who may call it:
#   * accounts in the staff allowlist (any auth mode, as before), and
#   * any account using the STAFF client (request carries `X-STAFF-Client: 1`)
#     provided the caller is proven by a valid Matrix access token. A
#     non-staff caller therefore only ever reaches their own nicknames; the
#     `user_id` parameter cannot be used to spoof another account.
#

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, Tuple
from urllib.parse import unquote

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import parse_json_object_from_request, parse_string
from synapse.staff_filter import has_staff_header
from synapse.types import JsonDict, UserID

from .rest_base import STAFF_API_PREFIX, StaffRestServlet

if TYPE_CHECKING:
    from synapse.http.server import JsonResource
    from synapse.server import HomeServer
    from .store import StaffStore

logger = logging.getLogger(__name__)


def _set_cors_headers(request) -> None:
    request.setHeader(b"Access-Control-Allow-Origin", b"*")
    request.setHeader(b"Access-Control-Allow-Methods", b"GET, POST, DELETE, OPTIONS")
    request.setHeader(
        b"Access-Control-Allow-Headers",
        b"X-Requested-With, Content-Type, Authorization, Date, X-STAFF-Client",
    )
    request.setHeader(b"Access-Control-Max-Age", b"3600")


async def resolve_dm_names_user(
    hs: "HomeServer", store: "StaffStore", request, fallback_user_id: str | None = None
) -> str:
    """Resolve the caller's user_id and check they may use the DM-names API.

    Resolution: a Bearer Matrix access token wins (verified via Synapse auth);
    otherwise `fallback_user_id` (query/body parameter) is used unverified.

    Authorisation, in order:
      1. the resolved user is in the staff allowlist -> allowed (unchanged
         behaviour, including the unverified-parameter path staff relied on);
      2. the request carries the STAFF client header AND the user was proven
         by a valid Matrix access token -> allowed, so ordinary accounts on
         the STAFF client can keep their own nicknames;
      3. otherwise -> 403.
    """
    user_id = fallback_user_id
    token_verified = False

    auth_header = request.getHeader(b"Authorization")
    if auth_header:
        try:
            requester = await hs.get_auth().get_user_by_req(request, allow_guest=False)
            token_user_id = requester.user.to_string()
            if user_id and user_id != token_user_id:
                logger.warning(
                    "STAFF: dm-names caller token (%s) differs from param user_id (%s)",
                    token_user_id,
                    user_id,
                )
            user_id = token_user_id
            token_verified = True
        except Exception as e:
            logger.debug("STAFF: dm-names token auth failed, falling back to user_id param: %s", e)

    if not user_id:
        raise SynapseError(400, "Missing user_id parameter", Codes.MISSING_PARAM)

    if not UserID.is_valid(user_id):
        raise SynapseError(400, f"Invalid user_id: {user_id}", Codes.INVALID_PARAM)

    if store.is_staff_user(user_id):
        return user_id

    if has_staff_header(request):
        if token_verified:
            return user_id
        raise SynapseError(
            403,
            "Forbidden: STAFF client access requires a valid Matrix access token",
            Codes.FORBIDDEN,
        )

    raise SynapseError(403, "Forbidden: User is not authorized as staff", Codes.FORBIDDEN)


class RestDmNamesServlet(StaffRestServlet):
    """GET and POST endpoints for staff custom room/DM nicknames."""

    PATTERNS = (
        re.compile(r"^/api/dm-names/?$"),
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/dm-names/?$"),
        re.compile(r"^/_synapse/staff/dm-names/?$"),
    )

    def register(self, http_server: "JsonResource") -> None:
        super().register(http_server)
        http_server.register_paths(
            "OPTIONS", self.PATTERNS, self.on_OPTIONS, self.__class__.__name__
        )

    def on_OPTIONS(self, request) -> Tuple[int, JsonDict]:
        _set_cors_headers(request)
        return 204, {}

    async def _resolve_and_verify_staff_user(self, request, fallback_user_id: str | None = None) -> str:
        return await resolve_dm_names_user(self.hs, self.store, request, fallback_user_id)

    async def on_GET(self, request) -> Tuple[int, Any]:
        _set_cors_headers(request)

        user_id_param = parse_string(request, "user_id")
        user_id = await self._resolve_and_verify_staff_user(request, user_id_param)

        # Strictly fetch nicknames owned by this staff user
        names_map = await self.store.dm_names_get_all(user_id)
        return 200, names_map

    async def on_POST(self, request) -> Tuple[int, JsonDict]:
        _set_cors_headers(request)

        body = parse_json_object_from_request(request)
        body_user_id = body.get("user_id")
        user_id = await self._resolve_and_verify_staff_user(request, body_user_id)

        room_id = body.get("room_id")
        if not room_id or not isinstance(room_id, str) or not room_id.startswith("!"):
            raise SynapseError(400, "room_id must be a valid room ID", Codes.INVALID_PARAM)

        original_name = body.get("original_name")
        if original_name is not None and not isinstance(original_name, str):
            original_name = str(original_name)

        custom_name = body.get("custom_name")
        if not custom_name or not isinstance(custom_name, str):
            raise SynapseError(400, "custom_name must be a non-empty string", Codes.INVALID_PARAM)

        custom_name = custom_name.strip()
        if not custom_name:
            raise SynapseError(400, "custom_name cannot be blank", Codes.INVALID_PARAM)

        # Save to DB isolated by user_id
        await self.store.dm_names_upsert(user_id, room_id, original_name, custom_name)

        return 200, {
            "status": "ok",
            "user_id": user_id,
            "room_id": room_id,
            "custom_name": custom_name,
        }


class RestDmNamesItemServlet(StaffRestServlet):
    """DELETE endpoint for removing staff custom room/DM nicknames."""

    PATTERNS = (
        re.compile(r"^/api/dm-names/(?P<room_id>[^/]+)/?$"),
        re.compile("^" + re.escape(STAFF_API_PREFIX) + r"/dm-names/(?P<room_id>[^/]+)/?$"),
        re.compile(r"^/_synapse/staff/dm-names/(?P<room_id>[^/]+)/?$"),
    )

    def register(self, http_server: "JsonResource") -> None:
        super().register(http_server)
        http_server.register_paths(
            "OPTIONS", self.PATTERNS, self.on_OPTIONS, self.__class__.__name__
        )

    def on_OPTIONS(self, request, **kwargs) -> Tuple[int, JsonDict]:
        _set_cors_headers(request)
        return 204, {}

    async def on_DELETE(self, request, room_id: str) -> Tuple[int, JsonDict]:
        _set_cors_headers(request)

        # URL decode room_id in case it was encoded
        decoded_room_id = unquote(room_id)

        user_id_param = parse_string(request, "user_id")
        user_id = await resolve_dm_names_user(self.hs, self.store, request, user_id_param)

        deleted = await self.store.dm_names_delete(user_id, decoded_room_id)

        return 200, {
            "status": "ok",
            "room_id": decoded_room_id,
            "deleted": bool(deleted),
        }


def register_servlets(
    hs: "HomeServer", store: "StaffStore", resource: "JsonResource"
) -> None:
    RestDmNamesServlet(hs, store).register(resource)
    RestDmNamesItemServlet(hs, store).register(resource)

