"""Anti-nuke bot handling — the 2026-08-23 multi-server Phase 1 change.

Bots were fully covered by every rate rule, which false-tripped on carl-bot and
MEE6 doing ordinary reaction-role and levelling bursts (on the HOME guild, before
any stranger's server was involved). The fix exempts bots from the role-grant
rate vectors only.

That is a LOOSENING of a security control, so it gets its own suite. The tests
that matter most are the negative ones: a bot deleting channels, mass-banning,
or adding bots must still trip. A regression here is silent until a nuke.

    PYTHONIOENCODING=utf-8 python tests/test_antinuke_bots.py
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.antinuke as an  # noqa: E402


class Actor:
    def __init__(self, uid, bot=False):
        self.id = uid
        self.bot = bot


HUMAN = Actor(1001)
BOT = Actor(2002, bot=True)

# Every vector the cog tracks, split by whether a bot may burst on it.
DESTRUCTIVE = [a for a in an.ACTION_LIMITS if a not in an.BOT_BURST_ACTIONS]


class BotRateExempt(unittest.TestCase):
    def test_bot_may_burst_role_grants(self):
        for action in an.BOT_BURST_ACTIONS:
            self.assertTrue(an.bot_rate_exempt(BOT, action, {}),
                            f"{action} should be exempt for bots")

    def test_humans_are_never_rate_exempt(self):
        for action in list(an.ACTION_LIMITS):
            self.assertFalse(an.bot_rate_exempt(HUMAN, action, {}),
                             f"{action} must stay armed against humans")

    def test_destructive_vectors_stay_armed_against_bots(self):
        # The security-critical assertion. If this ever passes-as-exempt, a
        # hijacked bot can delete a server without tripping anti-nuke.
        self.assertTrue(DESTRUCTIVE, "expected destructive vectors to exist")
        for action in DESTRUCTIVE:
            self.assertFalse(an.bot_rate_exempt(BOT, action, {}),
                             f"{action} must stay armed against bots")
        for critical in ("channel_delete", "role_delete", "ban", "kick", "webhook_create"):
            if critical in an.ACTION_LIMITS:
                self.assertFalse(an.bot_rate_exempt(BOT, critical, {}),
                                 f"{critical} must stay armed against bots")

    def test_watch_bot_roles_puts_bots_back_under_the_rate_rules(self):
        cfg = {"antinuke_watch_bot_roles": 1}
        for action in an.BOT_BURST_ACTIONS:
            self.assertFalse(an.bot_rate_exempt(BOT, action, cfg))

    def test_unknown_action_is_not_exempt(self):
        self.assertFalse(an.bot_rate_exempt(BOT, "something_new", {}))

    def test_non_bot_object_without_the_attribute(self):
        self.assertFalse(an.bot_rate_exempt(object(), "member_role", {}))


class ExemptList(unittest.TestCase):
    """_exempt is the full bypass — no vector applies at all. Bots reach it only
    when the guild explicitly trusts them."""

    def setUp(self):
        self.cog = an.AntiNuke.__new__(an.AntiNuke)          # no Discord needed
        self.cog.bot = type("B", (), {"user": Actor(9)})()
        self.guild = type("G", (), {"owner_id": 7})()

    def test_untrusted_bot_is_not_fully_exempt(self):
        self.assertFalse(self.cog._exempt(self.guild, BOT, {}))

    def test_trusted_bot_is_fully_exempt(self):
        self.assertTrue(self.cog._exempt(self.guild, BOT, {"antinuke_trusted_bots": [BOT.id]}))

    def test_trusted_list_accepts_string_ids(self):
        # Dashboard ids[] fields round-trip through JSON as strings.
        self.assertTrue(self.cog._exempt(self.guild, BOT, {"antinuke_trusted_bots": [str(BOT.id)]}))

    def test_trusting_a_bot_does_not_exempt_a_human_with_that_id(self):
        human_same_id = Actor(BOT.id)
        self.assertFalse(self.cog._exempt(self.guild, human_same_id,
                                          {"antinuke_trusted_bots": [BOT.id]}))

    def test_owner_self_and_whitelist_still_exempt(self):
        self.assertTrue(self.cog._exempt(self.guild, Actor(7), {}))          # owner
        self.assertTrue(self.cog._exempt(self.guild, Actor(9), {}))          # the bot itself
        self.assertTrue(self.cog._exempt(self.guild, HUMAN, {"whitelist": [HUMAN.id]}))
        self.assertTrue(self.cog._exempt(self.guild, None, {}))

    def test_garbage_in_trusted_list_fails_closed_and_never_raises(self):
        # A raise here would abort _record_action — anti-nuke stops evaluating
        # the event instead of guarding it. Malformed config must mean "not
        # exempt", quietly.
        for bad in ([{}], ["abc"], [None], "123", 123, {"a": 1}, [[1]]):
            with self.subTest(bad=bad):
                self.assertFalse(self.cog._exempt(self.guild, BOT,
                                                  {"antinuke_trusted_bots": bad}))

    def test_string_ids_match_int_user_ids(self):
        # The dashboard stores id[] fields as digit strings; discord.py gives
        # ints. Before id_set(), every dashboard-added whitelist entry silently
        # did nothing and the member kept getting stripped.
        self.assertTrue(self.cog._exempt(self.guild, HUMAN, {"whitelist": [str(HUMAN.id)]}))
        self.assertTrue(self.cog._exempt(self.guild, HUMAN, {"whitelist": [f" {HUMAN.id} "]}))
        self.assertFalse(self.cog._exempt(self.guild, HUMAN, {"whitelist": ["999"]}))

    def test_id_set_coercion(self):
        self.assertEqual(an.id_set(["1", 2, " 3 "]), {1, 2, 3})
        self.assertEqual(an.id_set(["1", "abc", None, {}]), {1})
        for empty in (None, "", "123", 123, {}):
            self.assertEqual(an.id_set(empty), set(), f"{empty!r} should coerce to empty")


if __name__ == "__main__":
    unittest.main(verbosity=1)
