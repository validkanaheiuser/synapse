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
            "users": {self.owner: 100, self.low: 49, self.mod: 50},
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

    async def visible(self, requester=None):
        return await get_visible_member_ids(
            self.hs, "!room:test", requester or self.viewer, iter(self.member_ids)
        )

    async def test_threshold_and_staff_exception(self) -> None:
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

    async def test_nonzero_requester_is_unrestricted(self) -> None:
        for level in (1, 49, 50, 100):
            self.power_levels["users"][self.viewer] = level
            self.assertIsNone(await self.visible())

    async def test_default_power_applies_to_targets_and_requester(self) -> None:
        self.power_levels["users_default"] = 50
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
        self.power_levels["users"][self.low] = 50
        self.power_levels["users"][self.mod] = 49
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
