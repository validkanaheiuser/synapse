"""Focused tests for the username-masking policy (should_mask_user)."""

from types import SimpleNamespace
from unittest import TestCase

from synapse.staff_filter import (
    MASK_POWER_THRESHOLD,
    mask_user_id,
    mask_user_if_needed,
    should_mask_user,
    unmask_user_id,
)


class MaskingPolicyTestCase(TestCase):
    def setUp(self) -> None:
        self.viewer = "@viewer:test"
        self.staff = "@staff:test"
        self.member = "@member:test"
        self.store = SimpleNamespace(is_staff_user={self.staff}.__contains__)

    def mask(self, target, pl_users=None, is_staff=False):
        return should_mask_user(target, self.viewer, is_staff, self.store, pl_users)

    def test_threshold_is_shared_with_member_list(self) -> None:
        self.assertEqual(MASK_POWER_THRESHOLD, 11)

    def test_power_threshold(self) -> None:
        for level, expected in ((0, True), (1, True), (10, True), (11, False), (12, False), (50, False), (100, False)):
            with self.subTest(level=level):
                self.assertEqual(self.mask(self.member, {self.member: level}), expected)

    def test_string_power_and_garbage_values(self) -> None:
        self.assertFalse(self.mask(self.member, {self.member: "11"}))
        self.assertTrue(self.mask(self.member, {self.member: "10"}))
        self.assertTrue(self.mask(self.member, {self.member: "abc"}))

    def test_no_power_entry_means_masked(self) -> None:
        self.assertTrue(self.mask(self.member, {}))
        self.assertTrue(self.mask(self.member, None))

    def test_staff_viewer_self_and_staff_target_never_masked(self) -> None:
        self.assertFalse(self.mask(self.member, {self.member: 0}, is_staff=True))
        self.assertFalse(self.mask(self.viewer, {self.viewer: 0}))
        self.assertFalse(self.mask(self.staff, {self.staff: 0}))

    def test_mask_user_if_needed_round_trip(self) -> None:
        masked = mask_user_if_needed(self.member, self.viewer, False, self.store, {self.member: 10})
        self.assertNotEqual(masked, self.member)
        self.assertEqual(masked, mask_user_id(self.member))
        self.assertEqual(unmask_user_id(masked), self.member)
        self.assertEqual(
            mask_user_if_needed(self.member, self.viewer, False, self.store, {self.member: 11}),
            self.member,
        )
