"""Tests for the mass-self-delete tripwire counter (cogs/mod_log.flood_update)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.mod_log import flood_update, SELFDEL_THRESHOLD, SELFDEL_WINDOW  # noqa: E402

T0 = 1_000_000.0


class TestFloodUpdate(unittest.TestCase):
    def test_crosses_exactly_at_threshold(self):
        times = []
        for i in range(SELFDEL_THRESHOLD):
            times, crossed = flood_update(times, T0 + i)
            self.assertEqual(crossed, i == SELFDEL_THRESHOLD - 1,
                             f"crossed wrong at delete #{i + 1}")

    def test_no_recross_above_threshold(self):
        # the alert must fire once — deletes 9, 10, 11... report False
        times = []
        for i in range(SELFDEL_THRESHOLD + 5):
            times, crossed = flood_update(times, T0 + i)
        self.assertFalse(crossed)

    def test_slow_deleter_never_crosses(self):
        # one delete per window-length: always pruned back to 1
        times = []
        for i in range(20):
            times, crossed = flood_update(times, T0 + i * (SELFDEL_WINDOW + 1))
            self.assertFalse(crossed)
            self.assertEqual(len(times), 1)

    def test_stale_entries_pruned(self):
        times = [T0 - SELFDEL_WINDOW - 1] * 10  # ancient history
        times, crossed = flood_update(times, T0)
        self.assertEqual(times, [T0])
        self.assertFalse(crossed)

    def test_can_cross_again_after_reset(self):
        # episode teardown clears the list; a later scrub must alert again
        times = []
        for i in range(SELFDEL_THRESHOLD):
            times, crossed = flood_update(times, T0 + i)
        self.assertTrue(crossed)
        times = []
        for i in range(SELFDEL_THRESHOLD):
            times, crossed = flood_update(times, T0 + 10_000 + i)
        self.assertTrue(crossed)


if __name__ == "__main__":
    unittest.main()
