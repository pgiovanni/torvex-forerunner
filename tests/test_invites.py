"""Tests for invite-join attribution (cogs/invites.pick_used_invite).

Covers the classic cache-diff (use-count went up) plus the vanish fallback:
native UI invites can be max-uses (deleted the instant they're consumed) or
short-expiry, so the used code may be GONE from the post-join snapshot.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.invites import pick_used_invite, VANISH_WINDOW

NOW = 1_000_000.0


class TestPickUsedInvite(unittest.TestCase):
    def test_count_increase_wins(self):
        before = {"aaa": (3, "1"), "bbb": (0, "2")}
        after = {"aaa": (4, "1"), "bbb": (0, "2")}
        self.assertEqual(pick_used_invite(before, after, {}, NOW), ("aaa", "1"))

    def test_new_code_first_use(self):
        # invite created after our last snapshot, used immediately
        before = {}
        after = {"new": (1, "5")}
        self.assertEqual(pick_used_invite(before, after, {}, NOW), ("new", "5"))

    def test_nothing_moved(self):
        before = {"aaa": (3, "1")}
        after = {"aaa": (3, "1")}
        self.assertEqual(pick_used_invite(before, after, {}, NOW), (None, None))

    def test_count_increase_preferred_over_vanish(self):
        before = {"aaa": (3, "1"), "ggg": (0, "9")}
        after = {"aaa": (4, "1")}  # ggg vanished but aaa's count moved
        self.assertEqual(pick_used_invite(before, after, {}, NOW), ("aaa", "1"))

    def test_single_vanished_from_snapshot(self):
        # max-uses invite consumed: join processed before the INVITE_DELETE event
        before = {"aaa": (3, "1"), "max": (0, "7")}
        after = {"aaa": (3, "1")}
        self.assertEqual(pick_used_invite(before, after, {}, NOW), ("max", "7"))

    def test_gateway_delete_beat_the_join(self):
        # INVITE_DELETE processed first → code already popped from the cache,
        # so it's absent from BOTH snapshots; recent_gone remembers it
        before = {"aaa": (3, "1")}
        after = {"aaa": (3, "1")}
        gone = {"max": ("7", NOW - 2)}
        self.assertEqual(pick_used_invite(before, after, gone, NOW), ("max", "7"))

    def test_stale_gateway_delete_ignored(self):
        before = {"aaa": (3, "1")}
        after = {"aaa": (3, "1")}
        gone = {"old": ("7", NOW - VANISH_WINDOW - 1)}
        self.assertEqual(pick_used_invite(before, after, gone, NOW), (None, None))

    def test_recent_gone_preferred_over_mere_absence(self):
        # one code vanished lazily (likely expiry), another was gateway-deleted
        # right now (likely consumed) → trust the gateway-confirmed one
        before = {"lazy": (2, "3"), "aaa": (0, "1")}
        after = {"aaa": (0, "1")}
        gone = {"max": ("7", NOW - 1)}
        self.assertEqual(pick_used_invite(before, after, gone, NOW), ("max", "7"))

    def test_multiple_vanished_is_ambiguous(self):
        before = {"xxx": (2, "3"), "yyy": (0, "4")}
        after = {}
        self.assertEqual(pick_used_invite(before, after, {}, NOW), (None, None))

    def test_multiple_recent_gone_falls_back_to_single_vanished(self):
        # two gateway deletes in the window (e.g. an admin purging) but exactly
        # one code vanished from the snapshot itself → still attributable
        before = {"aaa": (0, "1"), "solo": (5, "6")}
        after = {"aaa": (0, "1")}
        gone = {"p1": ("8", NOW - 3), "p2": ("9", NOW - 4)}
        self.assertEqual(pick_used_invite(before, after, gone, NOW), ("solo", "6"))

    def test_gone_code_still_live_in_after_is_ignored(self):
        # recreated code or stale delete record: if it's in the fresh snapshot,
        # it can't be the vanished one
        before = {"aaa": (0, "1")}
        after = {"aaa": (0, "1"), "back": (0, "2")}
        gone = {"back": ("2", NOW - 1)}
        self.assertEqual(pick_used_invite(before, after, gone, NOW), (None, None))

    def test_inviter_none_is_preserved(self):
        before = {"aaa": (0, None)}
        after = {"aaa": (1, None)}
        self.assertEqual(pick_used_invite(before, after, {}, NOW), ("aaa", None))


if __name__ == "__main__":
    unittest.main()
