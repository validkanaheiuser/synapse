#
# STAFF mod — shared filter helpers
#
# This module is imported by the core patches (visibility.py, sync.py,
# pagination.py, room.py REST servlets, user_directory.py REST servlet,
# relations.py REST servlet).  All STAFF-mode decisions go through here so
# the patches stay short and the conflict surface during git updates stays
# small.
#

from typing import TYPE_CHECKING, FrozenSet, Iterable, List, Optional

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
        return request.getHeader(name)
    except Exception:
        return None


def has_staff_header(
    request: "IRequest",
    header_name: bytes = STAFF_HEADER_NAME,
    header_value: bytes = STAFF_HEADER_VALUE,
) -> bool:
    """Cheap, sync header check.  Does NOT consult the DB allowlist."""
    raw = _read_header_bytes(request, header_name)
    return raw == header_value


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
