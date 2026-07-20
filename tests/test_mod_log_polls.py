"""Tests for poll + forwarded-message archiving helpers in cogs/mod_log.py.

The bug these guard against: polls and forwards have no content, no
attachments and no stickers, so they archived as fully EMPTY rows and their
delete logs showed nothing at all (hit live 2026-07-20, the "pole" poll).
"""
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.mod_log import (  # noqa: E402
    poll_meta, forward_meta, parse_json_obj, format_poll, format_forward,
    build_transcript)


def fake_poll(question="pineapple on pizza?", answers=("yes", "no"),
              multiple=False, emoji=None):
    return SimpleNamespace(
        question=question,
        answers=[SimpleNamespace(text=a, emoji=emoji) for a in answers],
        multiple=multiple, expires_at=None)


def fake_forward(content="original text", atts=(), stickers=(),
                 channel_id=111, message_id=222):
    snap = SimpleNamespace(
        content=content,
        attachments=[SimpleNamespace(filename=f) for f in atts],
        stickers=[SimpleNamespace(name=n) for n in stickers])
    return SimpleNamespace(
        message_snapshots=[snap],
        reference=SimpleNamespace(channel_id=channel_id, message_id=message_id))


class TestPollMeta(unittest.TestCase):
    def test_roundtrip(self):
        meta = parse_json_obj(json.dumps(poll_meta(fake_poll())))
        self.assertEqual(meta["question"], "pineapple on pizza?")
        self.assertEqual([a["text"] for a in meta["answers"]], ["yes", "no"])
        self.assertFalse(meta["multiple"])

    def test_format_lists_answers(self):
        out = format_poll(poll_meta(fake_poll(multiple=True)))
        self.assertIn("pineapple on pizza?", out)
        self.assertIn("• yes", out)
        self.assertIn("• no", out)
        self.assertIn("multiple answers", out)

    def test_transcript_joiner_is_single_line(self):
        out = format_poll(poll_meta(fake_poll()), joiner=" | ")
        self.assertNotIn("\n", out)

    def test_emoji_rendered(self):
        out = format_poll(poll_meta(fake_poll(emoji="🍍")))
        self.assertIn("🍍 yes", out)


class TestForwardMeta(unittest.TestCase):
    def test_roundtrip(self):
        meta = parse_json_obj(json.dumps(forward_meta(
            fake_forward(atts=["cat.png"], stickers=["peepoHappy"]))))
        self.assertEqual(meta["content"], "original text")
        self.assertEqual(meta["attachments"], ["cat.png"])
        self.assertEqual(meta["stickers"], ["peepoHappy"])
        self.assertEqual(meta["origin_channel_id"], "111")
        self.assertEqual(meta["origin_message_id"], "222")

    def test_plain_message_is_none(self):
        self.assertIsNone(forward_meta(SimpleNamespace(message_snapshots=[],
                                                       reference=None)))
        self.assertIsNone(forward_meta(SimpleNamespace()))  # no snapshot attr at all

    def test_format_shows_origin_and_content(self):
        out = format_forward(forward_meta(fake_forward()))
        self.assertIn("<#111>", out)
        self.assertIn("original text", out)

    def test_empty_snapshot_still_renders(self):
        out = format_forward(forward_meta(fake_forward(content="")))
        self.assertTrue(out)


class TestParseJsonObj(unittest.TestCase):
    def test_null_garbage_and_non_dict(self):
        self.assertIsNone(parse_json_obj(None))
        self.assertIsNone(parse_json_obj(""))
        self.assertIsNone(parse_json_obj("not json"))
        self.assertIsNone(parse_json_obj(json.dumps(["list"])))


class TestTranscript(unittest.TestCase):
    def _row(self, **kw):
        base = {"message_id": "1", "created_ts": 0, "author_name": "el",
                "author_id": "9", "content": "", "attachments": None,
                "stickers": None, "poll": None, "forward": None}
        base.update(kw)
        return base

    def test_poll_message_not_empty_in_transcript(self):
        out = build_transcript([self._row(poll=json.dumps(poll_meta(fake_poll())))], "g")
        self.assertIn("[poll: pineapple on pizza?", out)

    def test_forward_message_not_empty_in_transcript(self):
        out = build_transcript(
            [self._row(forward=json.dumps(forward_meta(fake_forward())))], "g")
        self.assertIn("[forwarded: original text]", out)

    def test_pre_migration_row_without_new_keys(self):
        out = build_transcript([{k: v for k, v in self._row(content="hi").items()
                                 if k not in ("poll", "forward")}], "g")
        self.assertIn("hi", out)


if __name__ == "__main__":
    unittest.main()
