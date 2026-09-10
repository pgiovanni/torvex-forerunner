"""System messages in the archive — cogs/mod_log.archive_text.

The bug (monki780, 2026-09-10): a ban's delete-days cascade also deletes the
member's "X joined the server" system message. Its `content` is empty, so the
archive row and the bulk-delete transcript showed a BLANK line that read like
lost text. The rendered system line is kept, tagged, instead.
"""
import os
import sys
import unittest
from types import SimpleNamespace

import discord

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.mod_log import archive_text, build_transcript  # noqa: E402


def msg(content="", mtype=discord.MessageType.default, system=""):
    return SimpleNamespace(content=content, type=mtype, system_content=system)


class TestArchiveText(unittest.TestCase):
    def test_user_text_untouched(self):
        self.assertEqual(archive_text(msg("hello")), "hello")
        self.assertEqual(archive_text(msg("hi", discord.MessageType.reply)), "hi")

    def test_user_message_with_no_text_stays_empty(self):
        # sticker/attachment-only posts: their meta lives in other columns
        self.assertEqual(archive_text(msg("", system="ignored")), "")
        self.assertEqual(archive_text(msg("", discord.MessageType.reply)), "")

    def test_join_message_keeps_rendered_line(self):
        out = archive_text(msg("", discord.MessageType.new_member,
                               "White monki joined the party."))
        self.assertEqual(out, "[system: new_member] White monki joined the party.")

    def test_system_without_render_still_tagged(self):
        out = archive_text(msg("", discord.MessageType.pins_add, ""))
        self.assertEqual(out, "[system: pins_add]")

    def test_system_with_real_content_prefers_content(self):
        # e.g. boost messages can carry user text
        self.assertEqual(archive_text(msg("thanks!", discord.MessageType.premium_guild_subscription,
                                          "X boosted")), "thanks!")

    def test_missing_attrs_never_raise(self):
        self.assertEqual(archive_text(SimpleNamespace()), "[system: None]")

    def test_transcript_line_not_blank(self):
        row = {"message_id": "1", "created_ts": 0, "author_name": "monki780",
               "author_id": "42", "content": archive_text(
                   msg("", discord.MessageType.new_member, "White monki joined.")),
               "attachments": None}
        line = build_transcript([row], "Torvex").splitlines()[-1]
        self.assertTrue(line.endswith("monki780 (42): [system: new_member] White monki joined."))


if __name__ == "__main__":
    unittest.main()
