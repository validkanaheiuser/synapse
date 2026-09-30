#
# STAFF mod — shared filter helpers
#
# This module is imported by the core patches (visibility.py, sync.py,
# pagination.py, room.py REST servlets, user_directory.py REST servlet,
# relations.py REST servlet).  All STAFF-mode decisions go through here so
# the patches stay short and the conflict surface during git updates stays
# small.
#

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Iterable, List, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.types import Requester
    from twisted.web.iweb import IRequest


# === AGENT I (S15): memoisation slot for `get_hidden_state_types(hs)`.
# We cache the computed frozenset on the HomeServer so the override is
# read from config exactly once per process.  Stored under a dunder
# attribute name to avoid colliding with anything user-facing.
_HIDDEN_STATE_TYPES_ATTR = "_staff_hidden_state_types_cached"
# === END AGENT I ===


# Default header that the STAFF element-desktop variant sends on every
# request.  Configurable via `staff.staff_header_name` / `staff_header_value`
# in homeserver.yaml.
STAFF_HEADER_NAME = b"X-STAFF-Client"
STAFF_HEADER_VALUE = b"1"


# State event types that Element clients render as visible "system messages"
# in the timeline.  When a non-staff client reads /sync, events of these
# types are relocated from `timeline.events` into `state.events` so the
# client's room model still updates but no system-message tile appears.
# Verified safe on element-web (shouldHideEvent.ts, MessagePanel.tsx),
# element-android (RoomSyncHandler.kt), element-ios (RoomBubbleCellData.m).
HIDDEN_STATE_TYPES = frozenset(
    {
        "m.room.member",
        "m.room.power_levels",
        "m.room.join_rules",
        "m.room.history_visibility",
        "m.room.guest_access",
        "m.room.encryption",
        "m.room.canonical_alias",
        "m.room.name",
        "m.room.topic",
        "m.room.avatar",
        "m.room.create",
        "m.room.server_acl",
        # widgets — F16/F17.  Element renders "X added a widget" notices
        # otherwise (NoticeEventFormatter.kt, EventFormatter.m).
        "im.vector.modular.widgets",
        "m.widget",
    }
)


def _read_header_bytes(request: "IRequest", name: bytes) -> Optional[bytes]:
    if request is None:
        return None
    try:
        val = request.getHeader(name)
        if val is None:
            val = request.getHeader(name.lower())
        if val is None and isinstance(name, bytes):
            val = request.getHeader(name.decode("ascii", errors="ignore"))
        if val is None and isinstance(name, str):
            val = request.getHeader(name.encode("ascii", errors="ignore"))
        return val
    except Exception:
        return None


def has_staff_header(
    request: "IRequest",
    header_name: bytes = STAFF_HEADER_NAME,
    header_value: bytes = STAFF_HEADER_VALUE,
) -> bool:
    """Cheap, sync header check.  Does NOT consult the DB allowlist."""
    raw = _read_header_bytes(request, header_name)
    if raw is None:
        raw = _read_header_bytes(request, b"x-staff-client")
    if raw is None:
        raw = _read_header_bytes(request, b"X-STAFF-Client")
    if raw is None:
        return False
    if isinstance(raw, str):
        raw = raw.encode("ascii", errors="ignore")
    val = raw.strip()
    return val in (header_value, header_value.lower(), b"1", b"true", b"True")


async def is_staff_request(
    request: Optional["IRequest"],
    hs: "HomeServer",
    requester: Optional["Requester"] = None,
) -> bool:
    """Definitive STAFF check: header present AND requester user_id is in
    the staff_users DB allowlist.

    Both halves must succeed.  If staff mode is disabled in config, always
    returns False (callers behave like upstream Synapse).

    `requester` is optional — if not provided and the request has been
    authenticated, callers should still pass it; we fall back to None which
    causes the result to be False (no staff for unauthenticated requests).
    """
    config = hs.config.staff
    if not config.staff_enabled:
        return False

    header_name = config.staff_header_name.encode("ascii")
    header_value = config.staff_header_value.encode("ascii")
    if not has_staff_header(request, header_name, header_value):
        return False

    if requester is None:
        return False

    user_id = requester.user.to_string()
    # The staff allowlist lives on the StaffStore (attached as
    # `hs._staff_store` by the staff_module init), NOT on the main
    # DataStore.  is_staff_user is a synchronous in-memory set lookup,
    # so no `await` is needed here.
    staff_store = getattr(hs, "_staff_store", None)
    if staff_store is None:
        return False
    return staff_store.is_staff_user(user_id)


def is_hidden_state_event(event_type: str) -> bool:
    return event_type in HIDDEN_STATE_TYPES


# === AGENT I (S15): config-aware hidden-state lookup ===
# Reads `hs.config.staff.staff_hidden_state_types` (set up by
# `synapse.config.staff`) the first time it is called and memoises the
# resulting frozenset on the HomeServer.  Callers that don't have an `hs`
# handy (e.g. the patch in `rest/client/sync.py`) can stay on the
# module-level `HIDDEN_STATE_TYPES` constant which carries the default;
# callers that DO have `hs` should prefer `get_hidden_state_types(hs)` to
# pick up operator overrides.  Both spellings stay in sync if no override
# is configured.
def get_hidden_state_types(hs: "HomeServer") -> FrozenSet[str]:
    cached = getattr(hs, _HIDDEN_STATE_TYPES_ATTR, None)
    if cached is not None:
        return cached
    try:
        configured = hs.config.staff.staff_hidden_state_types
    except AttributeError:
        configured = None
    if configured is None:
        result = HIDDEN_STATE_TYPES
    else:
        result = frozenset(configured)
    setattr(hs, _HIDDEN_STATE_TYPES_ATTR, result)
    return result


def is_hidden_state_event_for(hs: "HomeServer", event_type: str) -> bool:
    """Like `is_hidden_state_event` but honours the per-deployment
    override at `staff.hidden_state_types`.  Cheap after the first call."""
    return event_type in get_hidden_state_types(hs)
# === END AGENT I ===


def partition_timeline_for_relocation(
    events: Iterable,
) -> "tuple[List, List]":
    """Split a list of timeline events into (visible, relocate_to_state).

    A state event whose type is in HIDDEN_STATE_TYPES is relocated.  All
    other events (including content events like m.room.message) stay
    visible.  Caller is responsible for merging the relocated batch into
    the response's `state.events` field, de-duplicated by (type, state_key).
    """
    visible: List = []
    relocate: List = []
    for ev in events:
        # Synapse FrozenEvent has both .type and a stable .state_key only
        # when it's a state event.  is_state() is the safe check.
        try:
            is_state = ev.is_state()
        except Exception:
            is_state = bool(getattr(ev, "state_key", None) is not None)
        if is_state and is_hidden_state_event(ev.type):
            relocate.append(ev)
        else:
            visible.append(ev)
    return visible, relocate


def is_replace_relation(content: dict) -> bool:
    rel = (content or {}).get("m.relates_to") or {}
    return rel.get("rel_type") == "m.replace"


def is_redaction_event(event_type: str) -> bool:
    return event_type == "m.room.redaction"


_MASK_SALT = "elm_random_user_salt_v1"


def get_mask_suffix(localpart: str) -> str:
    """Deterministically derive 4 lowercase letters from localpart + salt."""
    h = hashlib.sha256(f"{localpart}:{_MASK_SALT}".encode("utf-8")).hexdigest()
    return "".join(chr(ord("a") + (int(h[i * 2 : i * 2 + 2], 16) % 26)) for i in range(4))


def mask_user_id(user_id: str) -> str:
    """Append 4 deterministic pseudo-random characters to the localpart of a Matrix user ID.
    e.g. @yuko2:elmchats.com -> @yuko2xkcd:elmchats.com
    """
    if not user_id or not isinstance(user_id, str):
        return user_id
    if not user_id.startswith("@") or ":" not in user_id:
        return user_id
    localpart, domain = user_id[1:].split(":", 1)
    suffix = get_mask_suffix(localpart)
    return f"@{localpart}{suffix}:{domain}"


def unmask_user_id(user_id: str) -> str:
    """Reverse a masked Matrix user ID if it ends with the expected 4-character suffix.
    e.g. @yuko2xkcd:elmchats.com -> @yuko2:elmchats.com
    """
    if not user_id or not isinstance(user_id, str):
        return user_id
    if not user_id.startswith("@") or ":" not in user_id:
        return user_id
    localpart, domain = user_id[1:].split(":", 1)
    if len(localpart) > 4:
        candidate_base = localpart[:-4]
        if get_mask_suffix(candidate_base) == localpart[-4:]:
            return f"@{candidate_base}:{domain}"
    return user_id


def should_mask_user(
    target_user: str,
    requester_user: Optional[str],
    is_staff: bool,
    staff_store: Optional[Any],
    room_pl_users: Optional[dict] = None,
) -> bool:
    """Determine whether target_user should have their username masked.

    Rules:
    1. If requester is staff (is_staff == True), NEVER mask (staff sees real usernames).
    2. If target_user is the requester themselves, do NOT mask (user sees own real username).
    3. If target_user is a staff member, do NOT mask (staff members show real username).
    4. If in a room/group and target_user has power custom = 50 (or >= 50), do NOT mask.
    5. Otherwise, mask (add 4 random characters to localpart).
    """
    if is_staff:
        return False
    if not target_user or not isinstance(target_user, str):
        return False
    if requester_user and target_user == requester_user:
        return False
    if staff_store is not None:
        try:
            if staff_store.is_staff_user(target_user):
                return False
        except Exception:
            pass
    if room_pl_users is not None and isinstance(room_pl_users, dict):
        pl = room_pl_users.get(target_user)
        if pl is not None:
            try:
                if int(pl) >= 50:
                    return False
            except (ValueError, TypeError):
                pass
    return True


def mask_user_if_needed(
    user_id: str,
    requester_user: Optional[str],
    is_staff: bool,
    staff_store: Optional[Any],
    room_pl_users: Optional[dict] = None,
) -> str:
    if not should_mask_user(user_id, requester_user, is_staff, staff_store, room_pl_users):
        return user_id
    return mask_user_id(user_id)


def mask_event_dict_for_non_staff(
    ev: dict,
    requester_user: Optional[str],
    is_staff: bool,
    staff_store: Optional[Any],
    room_pl_users: Optional[dict] = None,
) -> dict:
    """Mask sender, state_key, and displayname on a serialized event dict if requester is non-staff."""
    if is_staff or not isinstance(ev, dict):
        return ev

    sender = ev.get("sender")
    if sender and isinstance(sender, str):
        ev["sender"] = mask_user_if_needed(sender, requester_user, is_staff, staff_store, room_pl_users)

    state_key = ev.get("state_key")
    if state_key is not None and isinstance(state_key, str) and state_key.startswith("@"):
        ev["state_key"] = mask_user_if_needed(state_key, requester_user, is_staff, staff_store, room_pl_users)

    content = ev.get("content")
    if isinstance(content, dict):
        ev_type = ev.get("type")
        if ev_type == "m.room.member":
            target = state_key if state_key is not None else sender
            if target and should_mask_user(target, requester_user, is_staff, staff_store, room_pl_users):
                dn = content.get("displayname")
                if target.startswith("@") and ":" in target:
                    lp = target[1:].split(":", 1)[0]
                    sfx = get_mask_suffix(lp)
                    if dn is None or dn == "" or dn == lp:
                        content["displayname"] = f"{lp}{sfx}"
                    elif dn == target:
                        content["displayname"] = f"@{lp}{sfx}:{target[1:].split(':', 1)[1]}"
        elif ev_type == "m.room.power_levels":
            users = content.get("users")
            if isinstance(users, dict):
                new_users = {}
                for u, pl in users.items():
                    new_users[mask_user_if_needed(u, requester_user, is_staff, staff_store, room_pl_users)] = pl
                content["users"] = new_users

    unsigned = ev.get("unsigned")
    if isinstance(unsigned, dict):
        redacted_because = unsigned.get("redacted_because")
        if isinstance(redacted_because, dict):
            mask_event_dict_for_non_staff(redacted_because, requester_user, is_staff, staff_store, room_pl_users)
        relations = unsigned.get("m.relations")
        if isinstance(relations, dict):
            for rel_val in relations.values():
                if isinstance(rel_val, dict) and "chunk" in rel_val:
                    for sub_ev in rel_val["chunk"]:
                        if isinstance(sub_ev, dict):
                            mask_event_dict_for_non_staff(sub_ev, requester_user, is_staff, staff_store, room_pl_users)
    return ev


def mask_ephemeral_events_for_non_staff(
    ephemeral_events: list,
    requester_user: Optional[str],
    is_staff: bool,
    staff_store: Optional[Any],
    room_pl_users: Optional[dict] = None,
) -> list:
    if is_staff or not ephemeral_events:
        return ephemeral_events
    for ev in ephemeral_events:
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get("type")
        content = ev.get("content")
        if not isinstance(content, dict):
            continue
        if ev_type == "m.typing":
            uids = content.get("user_ids")
            if isinstance(uids, list):
                content["user_ids"] = [
                    mask_user_if_needed(u, requester_user, is_staff, staff_store, room_pl_users)
                    for u in uids
                ]
        elif ev_type == "m.receipt":
            for evt_id, r_types in list(content.items()):
                if isinstance(r_types, dict):
                    for r_name, u_map in list(r_types.items()):
                        if isinstance(u_map, dict):
                            new_u_map = {}
                            for u, r_val in u_map.items():
                                masked_u = mask_user_if_needed(u, requester_user, is_staff, staff_store, room_pl_users)
                                new_u_map[masked_u] = r_val
                            r_types[r_name] = new_u_map
    return ephemeral_events


def mask_summary_for_non_staff(
    summary: dict,
    requester_user: Optional[str],
    is_staff: bool,
    staff_store: Optional[Any],
    room_pl_users: Optional[dict] = None,
) -> dict:
    if is_staff or not isinstance(summary, dict):
        return summary
    heroes = summary.get("m.heroes")
    if isinstance(heroes, list):
        summary["m.heroes"] = [
            mask_user_if_needed(u, requester_user, is_staff, staff_store, room_pl_users)
            for u in heroes
        ]
    return summary


async def get_room_pl_users(hs: "HomeServer", room_id: str) -> dict:
    try:
        storage_controllers = getattr(hs, "get_storage_controllers", None)
        if storage_controllers:
            pl_event = await storage_controllers().state.get_current_state_event(
                room_id, "m.room.power_levels", ""
            )
            if pl_event and pl_event.content:
                return pl_event.content.get("users") or {}
    except Exception:
        pass
    return {}


async def is_room_owner(hs: "HomeServer", room_id: str, user_id: str) -> bool:
    """Check if user_id is the room owner / trưởng nhóm.
    A user is the room owner if:
    1. They are the creator of the room (from m.room.create), OR
    2. They have power level >= 100 in m.room.power_levels.
    """
    if not room_id or not user_id:
        return False
    try:
        storage_controllers = getattr(hs, "get_storage_controllers", None)
        if not storage_controllers:
            return False
        state_handler = storage_controllers().state

        # Check m.room.power_levels:
        pl_event = await state_handler.get_current_state_event(
            room_id, "m.room.power_levels", ""
        )
        if pl_event and pl_event.content:
            users = pl_event.content.get("users") or {}
            user_pl = users.get(user_id)
            if user_pl is not None:
                try:
                    if int(user_pl) >= 100:
                        return True
                except (ValueError, TypeError):
                    pass

        # Check m.room.create:
        create_event = await state_handler.get_current_state_event(
            room_id, "m.room.create", ""
        )
        if create_event:
            creator = (create_event.content or {}).get("creator") or create_event.sender
            if creator == user_id:
                return True
    except Exception as e:
        logger.warning("Error checking is_room_owner for %s in %s: %s", user_id, room_id, e)
    return False


async def is_user_in_room(hs: "HomeServer", room_id: str, user_id: str) -> bool:
    """Check if user_id is currently a joined member in room_id."""
    if not room_id or not user_id:
        return False
    try:
        main_store = hs.get_datastores().main
        members = await main_store.get_users_in_room(room_id)
        return user_id in members
    except Exception:
        return False


async def is_group_room(hs: "HomeServer", room_id: str, user_id: Optional[str] = None) -> bool:
    """Determine whether a room is a group room (nhóm) rather than a direct message (DM).

    A room is a group room if:
    1. It has more than 2 members, OR
    2. It has an explicit m.room.name set, OR
    3. It is NOT marked as a DM via m.direct account data or is_direct flags.
    """
    if not room_id:
        return False
    try:
        main_store = hs.get_datastores().main
        members = await main_store.get_users_in_room(room_id)
        if len(members) > 2:
            return True

        # Check m.room.name
        state_handler = hs.get_storage_controllers().state
        name_ev = await state_handler.get_current_state_event(room_id, "m.room.name", "")
        if name_ev and (name_ev.content or {}).get("name"):
            return True

        # Check m.room.create for is_direct
        create_ev = await state_handler.get_current_state_event(room_id, "m.room.create", "")
        if create_ev and bool((create_ev.content or {}).get("is_direct")):
            return False

        # Check m.direct account data
        users_to_check = [user_id] if user_id else list(members)
        for uid in users_to_check:
            if not uid:
                continue
            try:
                dm_map = await main_store.get_global_account_data_by_type_for_user(uid, "m.direct")
                if isinstance(dm_map, dict):
                    for room_list in dm_map.values():
                        if isinstance(room_list, list) and room_id in room_list:
                            return False
            except Exception:
                pass

        # Check room_memberships for invite events with is_direct
        try:
            def _check_invites(txn):
                txn.execute(
                    "SELECT ej.json FROM room_memberships rm "
                    "JOIN event_json ej ON ej.event_id = rm.event_id "
                    "WHERE rm.room_id = ? AND rm.membership = 'invite' "
                    "LIMIT 10",
                    (room_id,),
                )
                for r in txn.fetchall():
                    if r and r[0]:
                        try:
                            doc = json.loads(r[0])
                            if bool((doc.get("content") or {}).get("is_direct")):
                                return True
                        except Exception:
                            pass
                return False

            has_direct_invite = await main_store.db_pool.runInteraction(
                "staff_check_is_direct_invite", _check_invites
            )
            if has_direct_invite:
                return False
        except Exception:
            pass

        # Without DM markers or is_direct flags, treat as group room
        return True
    except Exception as e:
        logger.warning("Error in is_group_room for %s: %s", room_id, e)
        return False

