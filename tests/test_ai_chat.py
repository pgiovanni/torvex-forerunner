import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cogs.ai import split_chunks, strip_bot_mention, quote_question, QUOTE_CAP  # noqa: E402

BOT_ID = 1372003518667558952


class TestSplitChunks(unittest.TestCase):
    def test_short_is_single_chunk(self):
        self.assertEqual(split_chunks("hello"), ["hello"])

    def test_empty_yields_placeholder(self):
        self.assertEqual(split_chunks(""), ["…"])

    def test_splits_at_newline(self):
        body = ("a" * 1500) + "\n" + ("b" * 1500)
        chunks = split_chunks(body)
        self.assertEqual(chunks, ["a" * 1500, "b" * 1500])

    def test_hard_split_without_newlines(self):
        body = "x" * 4000
        chunks = split_chunks(body)
        self.assertTrue(all(len(c) <= 1990 for c in chunks))
        self.assertEqual("".join(chunks), body)

    def test_no_content_lost_with_newlines(self):
        body = "\n".join(f"line {i} " + "y" * 90 for i in range(60))
        chunks = split_chunks(body)
        self.assertTrue(all(len(c) <= 1990 for c in chunks))
        self.assertEqual("\n".join(chunks), body)

    def test_early_newline_not_used_for_tiny_first_chunk(self):
        # a newline in the first half shouldn't produce a near-empty chunk
        body = "ab\n" + "z" * 3000
        chunks = split_chunks(body)
        self.assertGreater(len(chunks[0]), 100)


class TestStripBotMention(unittest.TestCase):
    def test_plain_mention(self):
        self.assertEqual(strip_bot_mention(f"<@{BOT_ID}> what is pi", BOT_ID), "what is pi")

    def test_nickname_mention_form(self):
        self.assertEqual(strip_bot_mention(f"<@!{BOT_ID}> hey", BOT_ID), "hey")

    def test_mention_at_end(self):
        self.assertEqual(strip_bot_mention(f"settle this <@{BOT_ID}>", BOT_ID), "settle this")

    def test_no_mention_returns_none(self):
        # reply-pings put the bot in message.mentions but NOT in content
        self.assertIsNone(strip_bot_mention("what is pi", BOT_ID))

    def test_other_user_mention_returns_none(self):
        self.assertIsNone(strip_bot_mention("<@1234> what is pi", BOT_ID))

    def test_bare_mention_returns_none(self):
        self.assertIsNone(strip_bot_mention(f"<@{BOT_ID}>", BOT_ID))
        self.assertIsNone(strip_bot_mention(f"  <@!{BOT_ID}>  ", BOT_ID))

    def test_other_mentions_survive_in_question(self):
        q = strip_bot_mention(f"<@{BOT_ID}> who is <@1234>?", BOT_ID)
        self.assertEqual(q, "who is <@1234>?")


class TestQuoteQuestion(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(quote_question("paul", "what is pi"), "> **paul:** what is pi")

    def test_newlines_collapsed(self):
        # a newline in the question must not escape the blockquote
        q = quote_question("paul", "line one\nline two")
        self.assertNotIn("\n", q)
        self.assertIn("line one line two", q)

    def test_long_question_capped(self):
        q = quote_question("paul", "z" * 2000)
        self.assertLessEqual(len(q), QUOTE_CAP + 30)
        self.assertTrue(q.endswith("…"))


class TestNeedInfoSentinels(unittest.TestCase):
    """The on-demand reference protocol: the model's whole reply is a sentinel."""

    def _match(self, text):
        from cogs.ai import _NEED_INFO
        m = _NEED_INFO.fullmatch(text.strip())
        return m.group(1).lower() if m else None

    def test_bare_sentinels_match(self):
        self.assertEqual(self._match("NEED_COMMANDS"), "need_commands")
        self.assertEqual(self._match("NEED_ENERGY"), "need_energy")

    def test_case_and_punctuation_tolerated(self):
        self.assertEqual(self._match("need_commands."), "need_commands")
        self.assertEqual(self._match("  NEED_ENERGY!  "), "need_energy")

    def test_sentinel_inside_prose_does_not_match(self):
        self.assertIsNone(self._match("I would reply NEED_COMMANDS but here goes"))
        self.assertIsNone(self._match("NEED_ENERGY — energy resets daily"))

    def test_ordinary_answer_does_not_match(self):
        self.assertIsNone(self._match("Use the leaderboard to check levels."))


class TestEnergyInfoBlock(unittest.TestCase):
    """The energy explanation is built from the LIVE constants — pin that the
    numbers in the text are the numbers the meter actually charges."""

    def test_block_carries_live_numbers(self):
        from cogs.ai import AI, CREDIT_PACK_USD
        from utils.ai_meter import DAILY_FREE_ENERGY, BUCKS_PRICE, policy_from_config
        block = AI._energy_info_block(policy_from_config({}, is_home=True))
        self.assertIn(str(DAILY_FREE_ENERGY), block)
        self.assertIn(str(BUCKS_PRICE["smart"]), block)
        self.assertIn(str(BUCKS_PRICE["quick"]), block)
        self.assertIn(f"${CREDIT_PACK_USD:.0f}", block)
        self.assertIn("prepaid", block)
        self.assertIn("midnight UTC", block)

    def test_block_follows_the_servers_policy(self):
        """A paid server's block must describe THAT server: its allowance,
        no bucks, and unlimited when that's the mode — the block is the only
        thing allowed to answer cost questions, so it can't be home-shaped."""
        from cogs.ai import AI
        from utils.ai_meter import policy_from_config, BUCKS_PRICE
        paid = AI._energy_info_block(policy_from_config({"ai_daily_energy": 150}, is_home=False))
        self.assertIn("150 energy", paid)
        self.assertIn("not part of AI in this server", paid)
        self.assertNotIn(f"{BUCKS_PRICE['smart']} bucks normally", paid)
        unlimited = AI._energy_info_block(policy_from_config({"ai_mode": "unlimited"}, is_home=False))
        self.assertIn("UNLIMITED", unlimited)
        self.assertNotIn("energy per day", unlimited)


class TestBilledMicro(unittest.TestCase):
    def test_home_guild_pays_raw_cost(self):
        from cogs.ai import billed_micro, HOME_GUILD_ID
        self.assertEqual(billed_micro(HOME_GUILD_ID, 3000), 3000)

    def test_paid_guild_pays_marked_up_rate(self):
        import math
        from cogs.ai import billed_micro, PAID_MARKUP
        self.assertEqual(billed_micro(123, 3000), math.ceil(3000 * PAID_MARKUP))

    def test_markup_keeps_25_percent_of_revenue(self):
        from cogs.ai import PAID_MARKUP
        self.assertAlmostEqual(1 - 1 / PAID_MARKUP, 0.25, places=3)


class TestRetryAttachments(unittest.TestCase):
    """The on-demand reference protocol: a second model call happens ONLY when
    the first reply was a bare NEED_* sentinel. The inline version of this
    decision fired a duplicate, identical API call on every single answer for
    five days — `False or (None and x)` is None, and None != False — so each
    case here pins the exact pair the retry compares against."""

    def test_ordinary_answer_asks_for_nothing(self):
        from cogs.ai import retry_attachments
        self.assertEqual(retry_attachments("yo", False, False, True), (False, False))

    def test_ordinary_answer_does_not_trigger_a_retry(self):
        """The regression itself: identical pair in, identical pair out, so
        _generate's `!=` is False and no second call is made."""
        from cogs.ai import retry_attachments
        self.assertEqual(retry_attachments("Can I... what?", False, False, True),
                         (False, False))
        self.assertEqual(retry_attachments("", False, False, True), (False, False))
        self.assertEqual(retry_attachments(None, False, False, True), (False, False))

    def test_command_sentinel_attaches_the_index(self):
        from cogs.ai import retry_attachments
        self.assertEqual(retry_attachments("NEED_COMMANDS", False, False, True),
                         (True, False))
        self.assertEqual(retry_attachments(" need_commands ", False, False, True),
                         (True, False))

    def test_command_sentinel_ignored_when_the_index_is_missing(self):
        from cogs.ai import retry_attachments
        self.assertEqual(retry_attachments("NEED_COMMANDS", False, False, False),
                         (False, False))

    def test_energy_sentinel_attaches_the_reference(self):
        from cogs.ai import retry_attachments
        self.assertEqual(retry_attachments("NEED_ENERGY", False, False, True),
                         (False, True))

    def test_sentinel_inside_prose_is_not_a_request(self):
        from cogs.ai import retry_attachments
        self.assertEqual(
            retry_attachments("I would need_commands to answer that", False, False, True),
            (False, False))

    def test_pregated_attachment_survives_a_plain_answer(self):
        """Pre-gate already attached the index; a normal reply must not undo
        it — the pair has to stay equal or the answer is generated twice."""
        from cogs.ai import retry_attachments
        self.assertEqual(retry_attachments("here you go", True, False, True),
                         (True, False))
        self.assertEqual(retry_attachments("here you go", False, True, True),
                         (False, True))


class _Perms:
    def __init__(self, view):
        self.view_channel = view


class _Role:
    def __init__(self, name, view):
        self.name = name
        self.permissions = _Perms(view)

    def __repr__(self):
        return f"<{self.name}>"


class TestBaselineViewRoles(unittest.TestCase):
    """Who "the general membership" is for the context guard. An
    @everyone-only test reads as "nothing is public" in a verification-gated
    server, which silently starved the model of channel context everywhere."""

    def test_open_server_uses_everyone(self):
        from cogs.ai import baseline_view_roles
        everyone = _Role("@everyone", True)
        member = _Role("Member", True)
        self.assertEqual(baseline_view_roles(everyone, [member]), [everyone])

    def test_gated_server_falls_back_to_the_verified_role(self):
        from cogs.ai import baseline_view_roles
        everyone = _Role("@everyone", False)
        member = _Role("Peepo", True)
        ping = _Role("Announcement Ping", False)
        self.assertEqual(baseline_view_roles(everyone, [ping, member]), [member])

    def test_no_usable_role_stays_closed(self):
        """Fail closed: with nothing that can stand for the membership, the
        guard falls back to @everyone and no context is gathered."""
        from cogs.ai import baseline_view_roles
        everyone = _Role("@everyone", False)
        self.assertEqual(baseline_view_roles(everyone, []), [everyone])
        self.assertEqual(baseline_view_roles(everyone, [_Role("Ping", False)]), [everyone])


class TestIdList(unittest.TestCase):
    def test_mixed_types_coerced_and_junk_dropped(self):
        from cogs.ai import _id_list
        self.assertEqual(_id_list([1, "2", None, "x", 3.0]), [1, 2, 3])

    def test_non_list_config_is_empty_not_an_error(self):
        from cogs.ai import _id_list
        self.assertEqual(_id_list(None), [])
        self.assertEqual(_id_list("123"), [])
        self.assertEqual(_id_list(7), [])


class TestStripSubtext(unittest.TestCase):
    def test_meter_footer_removed(self):
        from cogs.ai import strip_subtext
        self.assertEqual(strip_subtext("yo\n-# smart · ⚡ 97/100 energy left today"), "yo")

    def test_footer_only_message_is_empty(self):
        from cogs.ai import strip_subtext
        self.assertEqual(strip_subtext("-# smart · ⚡ 3/100"), "")

    def test_plain_message_untouched(self):
        from cogs.ai import strip_subtext
        self.assertEqual(strip_subtext("hello there"), "hello there")
        self.assertEqual(strip_subtext(None), "")


if __name__ == "__main__":
    unittest.main()
