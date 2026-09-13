"""/deadchat helpers (cogs/deadchat.py) — the parts that decide whether a
2,000-member ping fires: config coercion, the server-wide cooldown, the
channel allow-list, and the rendered text.

Needs discord.py importable (the cog module imports it); the helpers under
test touch no gateway.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from cogs import deadchat as D  # noqa: E402


class Config(unittest.TestCase):
    def test_role_id_coerces_and_fails_closed(self):
        self.assertEqual(D.role_id({"deadchat_role_id": "1543320305051766915"}), 1543320305051766915)
        self.assertEqual(D.role_id({"deadchat_role_id": 12}), 12)
        for junk in (None, "", " ", "abc", 0, "0"):
            self.assertIsNone(D.role_id({"deadchat_role_id": junk}), junk)
        self.assertIsNone(D.role_id({}))

    def test_cooldown_minutes_clamped_with_default(self):
        self.assertEqual(D.cooldown_minutes({}), D.COOLDOWN_MIN_DEFAULT)
        self.assertEqual(D.cooldown_minutes({"deadchat_cooldown_min": "x"}), D.COOLDOWN_MIN_DEFAULT)
        self.assertEqual(D.cooldown_minutes({"deadchat_cooldown_min": 0}), D.COOLDOWN_MIN_DEFAULT)
        self.assertEqual(D.cooldown_minutes({"deadchat_cooldown_min": -5}), 1)
        self.assertEqual(D.cooldown_minutes({"deadchat_cooldown_min": 99999}), D.COOLDOWN_MIN_MAX)
        self.assertEqual(D.cooldown_minutes({"deadchat_cooldown_min": "45"}), 45)


class Cooldown(unittest.TestCase):
    def test_first_ping_is_free(self):
        self.assertEqual(D.cooldown_left(None, 30, now=1000), 0)
        self.assertEqual(D.cooldown_left(0, 30, now=1000), 0)

    def test_counts_down_then_opens(self):
        self.assertEqual(D.cooldown_left(1000, 30, now=1000), 1800)
        self.assertEqual(D.cooldown_left(1000, 30, now=1000 + 1799), 1)
        self.assertEqual(D.cooldown_left(1000, 30, now=1000 + 1800), 0)
        self.assertEqual(D.cooldown_left(1000, 30, now=1000 + 9999), 0)


class Channels(unittest.TestCase):
    def test_empty_list_means_anywhere(self):
        self.assertTrue(D.channel_allowed({}, 5))
        self.assertTrue(D.channel_allowed({"deadchat_channels": []}, 5))

    def test_list_confines_and_threads_follow_their_parent(self):
        cfg = {"deadchat_channels": ["10", "20", "junk"]}
        self.assertTrue(D.channel_allowed(cfg, 10))
        self.assertTrue(D.channel_allowed(cfg, "20"))
        self.assertFalse(D.channel_allowed(cfg, 30))
        self.assertTrue(D.channel_allowed(cfg, 999, parent_id=10))     # thread under #10
        self.assertFalse(D.channel_allowed(cfg, 999, parent_id=30))
        self.assertEqual(D.allowed_channels(cfg), {10, 20})


class Render(unittest.TestCase):
    def test_no_message(self):
        out = D.render("<@1>", 77, None)
        self.assertIn("<@&77>", out)
        self.assertIn("<@1>", out)
        self.assertTrue(out.endswith("Say something!"))
        self.assertEqual(D.render("<@1>", 77, "   "), out)

    def test_message_is_quoted_per_line_and_capped(self):
        out = D.render("<@1>", 77, "what game tonight?\n\nanyone?")
        self.assertIn("\n> what game tonight?\n> anyone?", out)
        long = "x" * 500
        out = D.render("<@1>", 77, long)
        self.assertEqual(out.count("x"), D.MAX_MESSAGE)

    def test_everyone_in_text_is_only_text(self):
        # render doesn't strip it — AllowedMentions on send is the guard — but
        # the ONLY role mention the message carries is the configured one.
        out = D.render("<@1>", 77, "@everyone <@&123> <@456> wake up")
        self.assertEqual(out.count("<@&77>"), 1)
        self.assertIn("@everyone", out)


class WhoMayPing(unittest.TestCase):
    """staff + level 20+ (Paul, 9/13). mapping = {level: role_id} as LevelRoles
    stores it; a synced member holds ONLY their current tier."""
    MAP = {1: 101, 5: 105, 10: 110, 20: 120, 30: 130, 50: 150}
    CFG = {"deadchat_min_level_role_id": "120", "deadchat_ping_roles": ["900", "901"]}

    def test_open_when_nothing_configured(self):
        self.assertTrue(D.can_ping({}, [], False, self.MAP))
        self.assertTrue(D.can_ping({"deadchat_min_level_role_id": "", "deadchat_ping_roles": []}, [], False, {}))
        self.assertFalse(D.restricted({}))
        self.assertTrue(D.restricted(self.CFG))

    def test_minimum_tier_and_every_tier_above_pass(self):
        self.assertTrue(D.can_ping(self.CFG, [120], False, self.MAP))       # exactly Level 20+
        self.assertTrue(D.can_ping(self.CFG, [150], False, self.MAP))       # Level 50+ only (synced)
        self.assertTrue(D.can_ping(self.CFG, [130, 5], False, self.MAP))
        self.assertFalse(D.can_ping(self.CFG, [110], False, self.MAP))      # Level 10+
        self.assertFalse(D.can_ping(self.CFG, [], False, self.MAP))
        self.assertEqual(D.qualifying_level_roles(120, self.MAP), {120, 130, 150})

    def test_always_roles_and_mods_bypass(self):
        self.assertTrue(D.can_ping(self.CFG, [900], False, self.MAP))
        self.assertTrue(D.can_ping(self.CFG, ["901"], False, self.MAP))
        self.assertTrue(D.can_ping(self.CFG, [], True, self.MAP))
        only_roles = {"deadchat_ping_roles": ["900"]}
        self.assertTrue(D.can_ping(only_roles, [900], False, {}))
        self.assertFalse(D.can_ping(only_roles, [120], False, self.MAP))

    def test_non_tier_role_or_no_mapping_means_must_hold_it(self):
        # not a level tier → plain "must hold this role"
        cfg = {"deadchat_min_level_role_id": 777}
        self.assertTrue(D.can_ping(cfg, [777], False, self.MAP))
        self.assertFalse(D.can_ping(cfg, [150], False, self.MAP))
        # LevelRoles down → {} mapping → never opens the door, only the role itself passes
        self.assertTrue(D.can_ping(self.CFG, [120], False, {}))
        self.assertFalse(D.can_ping(self.CFG, [150], False, {}))
        self.assertFalse(D.can_ping(self.CFG, [150], False, None))

    def test_junk_config_fails_closed_to_open(self):
        # unparsable role id / junk list entries = "not configured", same rule as deadchat_role_id
        self.assertIsNone(D.min_level_role({"deadchat_min_level_role_id": "abc"}))
        self.assertEqual(D.ping_roles({"deadchat_ping_roles": ["x", "", None, "5"]}), {5})
        self.assertTrue(D.can_ping({"deadchat_min_level_role_id": "abc", "deadchat_ping_roles": ["x"]},
                                   [], False, self.MAP))


if __name__ == "__main__":
    unittest.main()
