"""Cross-channel burst pure helpers — utils/crosspost.py (no discord import).

Grounding cases: the September image blasts (4 x image.jpg, 8 channels, ~2 s
apart), the 9/13 advert menu (same text, 7 channels, 77 s), and ordinary
members who must never trip it.

Run:  python tests/test_crosspost.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import crosspost as cp  # noqa: E402

SCAM = [96784, 89126, 77399, 93237]      # the 9/26 wave's four attachment sizes


def ev(ts, channel, sig, mid=None):
    return {"ts": ts, "channel_id": channel, "message_id": mid or int(ts * 1000) + channel,
            "sig": sig}


class Signature(unittest.TestCase):
    def test_images_without_text_count(self):
        self.assertIsNotNone(cp.signature("", SCAM))

    def test_same_files_same_signature_any_order(self):
        self.assertEqual(cp.signature("", SCAM), cp.signature("", list(reversed(SCAM))))

    def test_different_files_differ(self):
        self.assertNotEqual(cp.signature("", SCAM), cp.signature("", [96784, 89126, 77399, 1]))

    def test_case_and_spacing_flattened(self):
        a = cp.signature("TEEN  mega link   here join the telegram now", [])
        b = cp.signature("teen mega link here join the telegram now ", [])
        self.assertEqual(a, b)

    def test_short_text_is_nothing(self):
        for t in ("lol", "hi", "good morning", "", "   ", None, "who wants to play"):
            self.assertIsNone(cp.signature(t, []), t)

    def test_long_text_counts(self):
        self.assertIsNotNone(cp.signature("free nitro for everyone click the link in my bio", []))

    def test_short_text_with_link_counts(self):
        self.assertIsNotNone(cp.signature("https://t.me/x", []))
        self.assertIsNotNone(cp.signature("www.evil.example", []))

    def test_short_text_with_a_file_counts(self):
        self.assertIsNotNone(cp.signature("lol", [1234]))

    def test_sticker_only_is_nothing(self):
        # 8/21: a member dropped one sticker in three channels in 9 s
        self.assertIsNone(cp.signature("", [], [555]))

    def test_sticker_with_a_file_is_part_of_the_signature(self):
        self.assertNotEqual(cp.signature("", [10], [555]), cp.signature("", [10], [556]))

    def test_text_is_part_of_it(self):
        self.assertNotEqual(cp.signature("caption one for this picture", [10]),
                            cp.signature("caption two for this picture", [10]))


class Burst(unittest.TestCase):
    def setUp(self):
        self.sig = cp.signature("", SCAM)

    def test_the_image_blast_trips_on_the_third_channel(self):
        events = []
        tripped_at = None
        for i, ch in enumerate([11, 12, 13, 14, 15, 16, 17, 18]):
            now = 1000 + i * 2.2
            events.append(ev(now, ch, self.sig))
            if cp.burst(events, self.sig, 3, 15, now) and tripped_at is None:
                tripped_at = i + 1
        self.assertEqual(tripped_at, 3)

    def test_burst_returns_every_message_in_it(self):
        events = [ev(1000, 11, self.sig), ev(1002, 12, self.sig), ev(1004, 13, self.sig)]
        hit = cp.burst(events, self.sig, 3, 15, 1004)
        self.assertEqual([e["channel_id"] for e in hit], [11, 12, 13])

    def test_two_channels_is_cross_posting_not_a_burst(self):
        events = [ev(1000, 11, self.sig), ev(1002, 12, self.sig)]
        self.assertIsNone(cp.burst(events, self.sig, 3, 15, 1002))

    def test_same_channel_three_times_is_not_this_rule(self):
        events = [ev(1000 + i, 11, self.sig) for i in range(5)]
        self.assertIsNone(cp.burst(events, self.sig, 3, 15, 1004))

    def test_slow_cross_posting_does_not_trip(self):
        # a member sharing one picture in three channels over a minute
        events = [ev(1000, 11, self.sig), ev(1030, 12, self.sig), ev(1060, 13, self.sig)]
        self.assertIsNone(cp.burst(events, self.sig, 3, 15, 1060))

    def test_different_messages_in_three_channels_do_not_trip(self):
        a, b, c = (cp.signature("", [1]), cp.signature("", [2]), cp.signature("", [3]))
        events = [ev(1000, 11, a), ev(1001, 12, b), ev(1002, 13, c)]
        self.assertIsNone(cp.burst(events, c, 3, 15, 1002))

    def test_other_messages_in_between_do_not_hide_it(self):
        other = cp.signature("", [42])
        events = [ev(1000, 11, self.sig), ev(1001, 11, other), ev(1002, 12, self.sig),
                  ev(1003, 12, other), ev(1004, 13, self.sig)]
        hit = cp.burst(events, self.sig, 3, 15, 1004)
        self.assertEqual(len(hit), 3)

    def test_advert_menu_needs_a_wider_window(self):
        # 9/13: 7 channels over 77 s, two posts per channel — ~11 s per channel
        sig = cp.signature("TEEN mega link full access join the telegram today", [])
        events = [ev(1000 + i * 11, 20 + i, sig) for i in range(7)]
        self.assertIsNone(cp.burst(events[:3], sig, 3, 15, events[2]["ts"]))
        self.assertIsNotNone(cp.burst(events[:3], sig, 3, 30, events[2]["ts"]))

    def test_prune(self):
        events = [ev(1000, 1, "a"), ev(1100, 2, "a"), ev(1190, 3, "a")]
        self.assertEqual(len(cp.prune(events, 120, 1200)), 2)


class Config(unittest.TestCase):
    def test_on_by_default_as_timeout(self):
        self.assertEqual(cp.mode({}), "timeout")

    def test_modes(self):
        for m in cp.MODES:
            self.assertEqual(cp.mode({"automod_burst_mode": m}), m)
        self.assertEqual(cp.mode({"automod_burst_mode": "obliterate"}), "timeout")

    def test_master_switch_off_wins(self):
        self.assertEqual(cp.mode({"automod_enabled": 0, "automod_burst_mode": "ban"}), "off")

    def test_window_defaults_and_floors(self):
        self.assertEqual(cp.burst_window({}), (3, 15))
        self.assertEqual(cp.burst_window({"automod_burst": [5, 30]}), (5, 30))
        self.assertEqual(cp.burst_window({"automod_burst": [1, 1]}), (3, 3))
        self.assertEqual(cp.burst_window({"automod_burst": [2, 9999]}), (3, 120))
        self.assertEqual(cp.burst_window({"automod_burst": "junk"}), (3, 15))
        self.assertEqual(cp.burst_window({"automod_burst": [4]}), (3, 15))

    def test_timeout_minutes_clamped(self):
        self.assertEqual(cp.timeout_minutes({}), 1440)
        self.assertEqual(cp.timeout_minutes({"automod_burst_timeout_min": 10 ** 9}), 40320)
        self.assertEqual(cp.timeout_minutes({"automod_burst_timeout_min": "x"}), 1440)

    def test_staff_exempt_by_default(self):
        self.assertTrue(cp.staff_exempt({}))
        self.assertFalse(cp.staff_exempt({"automod_burst_exempt_staff": 0}))

    def test_exempt_channels_as_strings(self):
        self.assertEqual(cp.exempt_channels({"automod_burst_exempt_channels": [1, "2"]}),
                         {"1", "2"})
        self.assertEqual(cp.exempt_channels({}), set())


class Ledger(unittest.TestCase):
    def test_round_trip(self):
        path = os.path.join(tempfile.mkdtemp(), "hits.db")
        cp.init_db(path)
        cp.init_db(path)
        row = {"guild_id": 1, "user_id": 2, "username": "x", "sig": "abc",
               "channels": [11, 12, 13], "messages": [1, 2, 3], "span": 4.4, "content": "",
               "attachments": 4, "image_hashes": [{"dhash": "ff", "phash": "0f"}],
               "mode": "timeout", "deleted": 3, "action": "timed out", "failed": None}
        cp.record_hit(row, path)
        cp.record_hit(dict(row, user_id=9), path)
        self.assertEqual(cp.count_hits(1, 2, path), 1)
        self.assertEqual(cp.count_hits(1, 3, path), 0)

    def test_count_on_missing_table_is_zero(self):
        self.assertEqual(cp.count_hits(1, 2, os.path.join(tempfile.mkdtemp(), "e.db")), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
