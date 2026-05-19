#
# S11 — F16/F17 widget injection + CRUD tests.
#
# Endpoints in `synapse.staff_module.rest_widgets`:
#   POST   /_synapse/staff/v1/widgets                          create
#   GET    /_synapse/staff/v1/widgets                          list
#   GET    /_synapse/staff/v1/widgets/{widget_id}              one + instances
#   POST   /_synapse/staff/v1/widgets/{widget_id}/update       PATCH-equivalent
#   DELETE /_synapse/staff/v1/widgets/{widget_id}              remove + cascade
#
# Injection trigger lives in
# `synapse.staff_module.widget_inject.WidgetInjector.on_new_event` and
# fires on every m.room.member join where the joiner is a staff user
# and the room has exactly two members.
#

from http import HTTPStatus
from typing import Iterable

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.staff_module.widget_inject import WIDGET_EVENT_TYPE
from synapse.util.clock import Clock

from tests.staff_module.conftest import (
    STAFF_SECRET,
    STAFF_SECRET_HEADER,
    StaffHomeserverTestCase,
)


WIDGETS_PATH = "/_synapse/staff/v1/widgets"


def _secret_headers() -> Iterable[tuple[bytes, bytes]]:
    return [(STAFF_SECRET_HEADER, STAFF_SECRET.encode("ascii"))]


class StaffWidgetInjectionTest(StaffHomeserverTestCase):
    """End-to-end tests for widget CRUD + auto-injection."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, hs: HomeServer
    ) -> None:
        self.prime_cache()
        self.staff_mxid = self.register_user("widgetstaff", "pass")
        self.staff_tok = self.login("widgetstaff", "pass")
        self.peer_mxid = self.register_user("widgetpeer", "pass")
        self.peer_tok = self.login("widgetpeer", "pass")
        self.add_staff_user(self.staff_mxid)

    # ------------------------------------------------------------ helpers
    def _create_widget(
        self,
        *,
        widget_type: str = "staff_custom_widget",
        owner: str | None = None,
        name: str = "My Widget",
        url: str = "https://widgets.example/iframe",
        content: dict | None = None,
    ) -> str:
        body = {
            "widget_type": widget_type,
            "owner_user_id": owner or self.staff_mxid,
            "name": name,
            "url": url,
            "content": content or {"data": {"foo": "bar"}},
        }
        channel = self.make_request(
            "POST", WIDGETS_PATH, content=body,
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        return channel.json_body["widget_id"]

    def _two_person_room_with_staff_join(self) -> str:
        """Peer creates a room, invites staff, staff joins => 2-member
        room with staff as joiner.  This satisfies the
        `_is_two_person_dm` heuristic used by `WidgetInjector`."""
        room_id = self.helper.create_room_as(
            self.peer_mxid, tok=self.peer_tok
        )
        self.helper.invite(
            room_id,
            src=self.peer_mxid,
            targ=self.staff_mxid,
            tok=self.peer_tok,
        )
        self.helper.join(room_id, self.staff_mxid, tok=self.staff_tok)
        return room_id

    def _get_room_state(
        self, *, room_id: str, event_type: str, state_key: str, tok: str,
    ) -> tuple[int, dict]:
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{state_key}",
            access_token=tok,
        )
        return channel.code, channel.json_body

    # ------------------------------------------------------------ tests
    def test_create_widget_endpoint_returns_widget_id(self) -> None:
        """POST /widgets creates a definition and returns a widget_id."""
        widget_id = self._create_widget(name="W1")
        self.assertIsInstance(widget_id, str)
        # GET returns it.
        channel = self.make_request(
            "GET",
            f"{WIDGETS_PATH}/{widget_id}",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["widget"]["name"], "W1")

    def test_injection_writes_state_event_when_staff_joins_dm(self) -> None:
        """After a staff user joins a 2-person room with a pre-existing
        widget definition, the widget's `im.vector.modular.widgets` state
        event appears in room state with the widget_id as state_key and
        the staff as sender."""
        widget_id = self._create_widget(name="AutoInject")
        room_id = self._two_person_room_with_staff_join()

        code, body = self._get_room_state(
            room_id=room_id,
            event_type=WIDGET_EVENT_TYPE,
            state_key=widget_id,
            tok=self.staff_tok,
        )
        self.assertEqual(code, 200, body)
        # State key must equal widget_id (per widget_inject._inject_one).
        # And the URL/name match the definition.
        self.assertEqual(body.get("url"), "https://widgets.example/iframe")
        self.assertEqual(body.get("name"), "AutoInject")

    def test_injection_records_widget_room_instance(self) -> None:
        """`staff_widget_room_instances` has a row for (widget_id, room_id)
        after auto-injection.  This is what allows PATCH to propagate."""
        widget_id = self._create_widget(name="WithInstance")
        room_id = self._two_person_room_with_staff_join()

        instance = self.get_success(
            self.hs._staff_store.widget_instance_get(widget_id, room_id)
        )
        self.assertIsNotNone(instance, "expected an instance row")
        self.assertEqual(instance["widget_id"], widget_id)
        self.assertEqual(instance["room_id"], room_id)
        self.assertEqual(instance["injected_by"], self.staff_mxid)

    def test_update_propagates_to_existing_room_instances(self) -> None:
        """POST /widgets/{id}/update propagates a new state event to every
        room that has an instance — same state_key, replacing the prior
        content."""
        widget_id = self._create_widget(name="OldName")
        room_id = self._two_person_room_with_staff_join()

        # Sanity: the original is in state.
        code, before = self._get_room_state(
            room_id=room_id,
            event_type=WIDGET_EVENT_TYPE,
            state_key=widget_id,
            tok=self.staff_tok,
        )
        self.assertEqual(code, 200, before)
        self.assertEqual(before["name"], "OldName")

        # Update.
        channel = self.make_request(
            "POST",
            f"{WIDGETS_PATH}/{widget_id}/update",
            content={"name": "NewName"},
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # New state event has the new name.
        code, after = self._get_room_state(
            room_id=room_id,
            event_type=WIDGET_EVENT_TYPE,
            state_key=widget_id,
            tok=self.staff_tok,
        )
        self.assertEqual(code, 200, after)
        self.assertEqual(after["name"], "NewName")

    def test_delete_sends_empty_content_and_removes_instances(self) -> None:
        """DELETE /widgets/{id} emits a state event with empty content
        (the Matrix convention for "widget removed") into every room and
        drops the corresponding rows from `staff_widget_room_instances`."""
        widget_id = self._create_widget(name="ToDelete")
        room_id = self._two_person_room_with_staff_join()

        channel = self.make_request(
            "DELETE",
            f"{WIDGETS_PATH}/{widget_id}",
            custom_headers=_secret_headers(),
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Latest state event for this state_key has empty content.
        code, after = self._get_room_state(
            room_id=room_id,
            event_type=WIDGET_EVENT_TYPE,
            state_key=widget_id,
            tok=self.staff_tok,
        )
        # 200 + {} (empty state == widget gone) OR 404 (Synapse may
        # respond with the empty body via 200 only).  Accept both.
        self.assertIn(code, (200, 404))
        if code == 200:
            self.assertEqual(after, {})

        # Instance row should be gone.
        instance = self.get_success(
            self.hs._staff_store.widget_instance_get(widget_id, room_id)
        )
        self.assertIsNone(instance)
