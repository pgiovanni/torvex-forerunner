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


if __name__ == "__main__":
    unittest.main()
