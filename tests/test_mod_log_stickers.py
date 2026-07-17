"""Tests for sticker archiving/recovery helpers in cogs/mod_log.py.

The bug these guard against: sticker-only messages have no content and no
attachments, so they archived as name-only and their delete logs came out
EMPTY with no way to recover the image.
"""
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.mod_log import (  # noqa: E402
    sticker_meta, parse_stickers, media_display_name, build_transcript)


def fake_sticker(sid=123, name="peepoHappy", fmt="png", url="https://cdn/x.png"):
    return SimpleNamespace(id=sid, name=name,
                           format=SimpleNamespace(name=fmt), url=url)


class TestStickerMeta(unittest.TestCase):
    def test_roundtrip(self):
        meta = sticker_meta([fake_sticker()])
        parsed = parse_stickers(json.dumps(meta))
        self.assertEqual(parsed, [{"id": "123", "name": "peepoHappy",
                                   "format": "png", "url": "https://cdn/x.png"}])

    def test_legacy_name_only_rows(self):
        # rows written before 2026-07-17 stored just ["name"]
        parsed = parse_stickers(json.dumps(["oldSticker"]))
        self.assertEqual(parsed[0]["name"], "oldSticker")
        self.assertIsNone(parsed[0]["url"])

    def test_null_and_garbage(self):
        self.assertEqual(parse_stickers(None), [])
        self.assertEqual(parse_stickers(""), [])
        self.assertEqual(parse_stickers("not json"), [])


class TestMediaDisplayName(unittest.TestCase):
    ATTS = [{"filename": "cat.png"}, {"filename": "dog.mp4"}]

    def test_attachment_maps_by_index_token(self):
        self.assertEqual(media_display_name("/m/111_0_cat.png", self.ATTS), "cat.png")
        self.assertEqual(media_display_name("/m/111_1_dog.mp4", self.ATTS), "dog.mp4")

    def test_skipped_oversize_attachment_does_not_shift_names(self):
        # attachment 0 was over the cache cap → only _1_ exists on disk; the
        # old positional zip would have labeled it cat.png
        self.assertEqual(media_display_name("/m/111_1_dog.mp4", self.ATTS), "dog.mp4")

    def test_sticker_file_uses_its_own_name(self):
        self.assertEqual(media_display_name("/m/111_s0_peepoHappy.png", self.ATTS),
                         "peepoHappy.png")

    def test_index_token_out_of_range_falls_back(self):
        self.assertEqual(media_display_name("/m/111_5_x.bin", self.ATTS), "x.bin")


class TestTranscriptStickers(unittest.TestCase):
    def test_sticker_message_not_empty_in_transcript(self):
        rows = [{"message_id": "1", "created_ts": 0, "author_name": "el",
                 "author_id": "9", "content": "", "attachments": None,
                 "stickers": json.dumps(sticker_meta([fake_sticker()]))}]
        out = build_transcript(rows, "g")
        self.assertIn("[stickers: peepoHappy]", out)

    def test_legacy_row_without_stickers_key(self):
        rows = [{"message_id": "1", "created_ts": 0, "author_name": "el",
                 "author_id": "9", "content": "hi", "attachments": None}]
        self.assertIn("hi", build_transcript(rows, "g"))


if __name__ == "__main__":
    unittest.main()
