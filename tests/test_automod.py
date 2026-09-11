"""AutoMod pure helpers — utils/automod.py (no discord import; runs locally).

Run:  python tests/test_automod.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import automod as am  # noqa: E402


class Links(unittest.TestCase):
    def test_extracts_plain_masked_and_www(self):
        c = "see https://evil.example/x, [click](https://sneaky.example/y) and www.foo.bar/z."
        self.assertEqual(am.extract_urls(c),
                         ["https://evil.example/x", "https://sneaky.example/y", "www.foo.bar/z"])

    def test_discord_and_gif_pickers_always_allowed(self):
        for u in ("https://tenor.com/view/abc", "https://media.tenor.com/x.gif",
                  "https://cdn.discordapp.com/attachments/1/2/a.png", "https://discord.gg/abc",
                  "https://giphy.com/gifs/x"):
            self.assertTrue(am.host_allowed(am.host_of(u), []), u)

    def test_lookalikes_are_not_allowed(self):
        self.assertFalse(am.host_allowed("notdiscord.com", []))
        self.assertFalse(am.host_allowed("tenor.com.evil.example", []))

    def test_admin_allow_list_suffix_matches(self):
        allow = ["youtube.com", "*.twitch.tv"]
        self.assertTrue(am.host_allowed("www.youtube.com", allow))
        self.assertTrue(am.host_allowed("clips.twitch.tv", allow))
        self.assertFalse(am.host_allowed("youtube.com.evil.example", allow))

    def test_blocked_urls_only_reports_policed_ones(self):
        c = "https://tenor.com/a https://random.example/b https://youtu.be/c"
        self.assertEqual(am.blocked_urls(c, ["youtu.be"]), ["https://random.example/b"])
        self.assertEqual(am.blocked_urls("no links here", []), [])

    def test_mode_parsing(self):
        self.assertEqual(am.link_mode({}), "off")
        self.assertEqual(am.link_mode({"automod_links_mode": "TIMEOUT"}), "timeout")
        self.assertEqual(am.link_mode({"automod_links_mode": "garbage"}), "off")

    def test_pass_expiry(self):
        self.assertTrue(am.pass_active({"expires_ts": 1000}, now=999))
        self.assertFalse(am.pass_active({"expires_ts": 1000}, now=1000))
        self.assertFalse(am.pass_active(None))
        self.assertFalse(am.pass_active({"expires_ts": "x"}))


class Raids(unittest.TestCase):
    def test_window_defaults_and_floors(self):
        self.assertEqual(am.raid_window({}), (10, 30))
        self.assertEqual(am.raid_window({"automod_raid": [1, 1]}), (2, 5))
        self.assertEqual(am.raid_window({"automod_raid": "bad"}), (10, 30))

    def test_trip_counts_only_inside_window(self):
        joins = [100, 101, 102, 130, 131]
        self.assertTrue(am.raid_tripped(joins, 3, 30, now=132))    # 102,130,131 (and 101? no: 132-30=102 → 102 counts)
        self.assertFalse(am.raid_tripped(joins, 4, 5, now=132))
        self.assertEqual(am.prune_joins(joins, 30, now=132), [102, 130, 131])

    def test_action_parsing(self):
        self.assertEqual(am.raid_action({}), "alert")
        self.assertEqual(am.raid_action({"automod_raid_action": "kick"}), "kick")
        self.assertEqual(am.raid_action({"automod_raid_action": "nuke"}), "alert")


if __name__ == "__main__":
    unittest.main()
