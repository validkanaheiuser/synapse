"""Focused tests for the room-member visibility policy."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from synapse.api.constants import EventTypes
from synapse.api.room_versions import RoomVersions
from synapse.staff_filter import get_visible_member_ids


class MemberListVisibilityTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.owner = "@owner:test"
        self.viewer = "@viewer:test"
        self.staff = "@staff:test"
        self.low = "@low:test"
        self.mod = "@moderator:test"
        self.ordinary = "@ordinary:test"
        self.member_ids = [
            self.owner, self.viewer, self.staff, self.low, self.mod, self.ordinary
        ]
        self.staff_ids = {self.staff}
        self.power_levels = {
            "users": {self.owner: 100, self.low: 10, self.mod: 11},
            "users_default": 0,
        }
        self.auth_events = {
            (EventTypes.Create, ""): SimpleNamespace(
                content={"creator": self.owner},
                sender=self.owner,
                room_version=RoomVersions.V10,
            ),
            (EventTypes.PowerLevels, ""): SimpleNamespace(content=self.power_levels),
        }
        self.state = SimpleNamespace(
            get_current_state=AsyncMock(return_value=self.auth_events)
        )
        self.hs = SimpleNamespace(
            config=SimpleNamespace(staff=SimpleNamespace(staff_enabled=True)),
            _staff_store=SimpleNamespace(is_staff_user=self.staff_ids.__contains__),
            get_storage_controllers=lambda: SimpleNamespace(state=self.state),
        )

    async def visible(self, requester=None, request=None):
        return await get_visible_member_ids(
            self.hs, "!room:test", requester or self.viewer, iter(self.member_ids),
            request=request,
        )

    async def test_threshold_and_staff_exception(self) -> None:
        for level in (11, 12, 49, 50, 100):
            with self.subTest(level=level):
                self.power_levels["users"][self.mod] = level
                self.assertEqual(await self.visible(), {self.owner, self.staff, self.mod})

    async def test_all_zero_power_members_are_removed_not_renamed(self) -> None:
        zero_power_members = {f"@member{i}:test" for i in range(900)}
        self.member_ids.extend(zero_power_members)
        visible = await self.visible()
        self.assertEqual(visible, {self.owner, self.staff, self.mod})
        self.assertTrue(visible.isdisjoint(zero_power_members))
        self.assertNotIn(self.viewer, visible)
        self.assertNotIn(self.ordinary, visible)

    async def test_staff_viewer_with_power_zero_is_unrestricted(self) -> None:
        self.assertIsNone(await self.visible(self.staff))
        self.state.get_current_state.assert_not_awaited()

    async def test_staff_client_header_allows_zero_power_nonstaff_requester(self) -> None:
        self.assertNotIn(self.viewer, self.staff_ids)
        for value in (b"1", "1", b"true", b"True", b" 1 "):
            with self.subTest(value=value):
                headers = {b"X-STAFF-Client": value}
                request = SimpleNamespace(getHeader=headers.get)
                self.assertIsNone(await self.visible(request=request))
        self.state.get_current_state.assert_not_awaited()

    async def test_lowercase_staff_client_header_allows_full_list(self) -> None:
        headers = {b"x-staff-client": b"1"}
        request = SimpleNamespace(getHeader=headers.get)
        self.assertIsNone(await self.visible(request=request))
        self.state.get_current_state.assert_not_awaited()

    async def test_missing_or_invalid_staff_header_keeps_filter(self) -> None:
        for value in (None, b"", b"0", b"false", b"invalid"):
            with self.subTest(value=value):
                headers = {b"X-STAFF-Client": value}
                request = SimpleNamespace(getHeader=headers.get)
                self.assertEqual(
                    await self.visible(request=request), {self.owner, self.staff, self.mod}
                )

    async def test_removing_staff_client_header_restores_filter(self) -> None:
        headers = {b"X-STAFF-Client": b"1"}
        request = SimpleNamespace(getHeader=headers.get)
        self.assertIsNone(await self.visible(request=request))
        headers.clear()
        self.assertEqual(
            await self.visible(request=request), {self.owner, self.staff, self.mod}
        )

    async def test_nonzero_requester_is_unrestricted(self) -> None:
        for level in (1, 10, 11, 49, 50, 100):
            self.power_levels["users"][self.viewer] = level
            self.assertIsNone(await self.visible())

    async def test_default_power_applies_to_targets_and_requester(self) -> None:
        self.power_levels["users_default"] = 11
        self.assertIsNone(await self.visible())
        self.power_levels["users"][self.viewer] = 0
        self.assertEqual(
            await self.visible(), {self.owner, self.staff, self.mod, self.ordinary}
        )

    async def test_creator_power_without_power_event(self) -> None:
        del self.auth_events[(EventTypes.PowerLevels, "")]
        self.assertEqual(await self.visible(), {self.owner, self.staff})
        self.assertIsNone(await self.visible(self.owner))

    async def test_rechecks_power_and_staff_changes(self) -> None:
        await self.visible()
        self.power_levels["users"][self.low] = 11
        self.power_levels["users"][self.mod] = 10
        self.staff_ids.clear()
        self.assertEqual(await self.visible(), {self.owner, self.low})

    async def test_disabled_feature_is_unrestricted(self) -> None:
        self.hs.config.staff.staff_enabled = False
        self.assertIsNone(await self.visible())
        self.state.get_current_state.assert_not_awaited()

    async def test_missing_staff_store_still_filters_by_power(self) -> None:
        del self.hs._staff_store
        self.assertEqual(await self.visible(), {self.owner, self.mod})

    async def test_state_failure_does_not_return_an_unrestricted_list(self) -> None:
        self.state.get_current_state.side_effect = RuntimeError("state unavailable")
        with self.assertRaisesRegex(RuntimeError, "state unavailable"):
            await self.visible()

    async def test_empty_candidate_list_stays_restricted(self) -> None:
        self.member_ids = []
        self.assertEqual(await self.visible(), frozenset())
