"""The paid-AI pitch on a ping in a non-enabled server.

8/29 (Paul): "make sure if the bot is pinged it runs the ai ad response unless
it's in my server." The old gate had a 1h/guild cooldown (second ping in an
hour = silence, looked like a dead bot) and keyed on message.mentions, which
Discord also fills for reply-pings — so replying to the ad re-triggered it.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.ai import AI, AD_COOLDOWN_S, mentions_bot  # noqa: E402

BOT = 1372003518667558952


class MentionsBot(unittest.TestCase):
    def test_explicit_markup(self):
        self.assertTrue(mentions_bot(f"<@{BOT}>", BOT))
        self.assertTrue(mentions_bot(f"<@!{BOT}> hey", BOT))
        self.assertTrue(mentions_bot(f"yo <@{BOT}> u alive", BOT))

    def test_not_for_reply_pings_or_others(self):
        self.assertFalse(mentions_bot("hey", BOT))          # reply-ping: no markup
        self.assertFalse(mentions_bot("<@1234> hey", BOT))  # someone else
        self.assertFalse(mentions_bot("", BOT))
        self.assertFalse(mentions_bot(None, BOT))


class AdReady(unittest.TestCase):
    def setUp(self):
        self.cog = types.SimpleNamespace(_ad_last={})

    def ready(self, g, u, now):
        return AI._ad_ready(self.cog, g, u, now)

    def test_every_ping_fires_outside_the_guard(self):
        self.assertTrue(self.ready(1, 10, 0.0))
        self.assertTrue(self.ready(1, 10, AD_COOLDOWN_S))
        self.assertTrue(self.ready(1, 10, 2 * AD_COOLDOWN_S))

    def test_guard_is_per_member_not_per_guild(self):
        self.assertTrue(self.ready(1, 10, 0.0))
        self.assertFalse(self.ready(1, 10, 1.0))   # same member, burst
        self.assertTrue(self.ready(1, 11, 1.0))    # another member, same guild
        self.assertTrue(self.ready(2, 10, 1.0))    # same member, another guild

    def test_default_guard_is_seconds_not_an_hour(self):
        self.assertLessEqual(AD_COOLDOWN_S, 60)


if __name__ == "__main__":
    unittest.main()
