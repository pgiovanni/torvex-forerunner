"""Scam-image pure helpers — utils/scam_images.py (no discord import; runs locally).

Grounding case (2026-09): hijacked accounts posting the same four MrBeast
crypto-casino screenshots, re-encoded and resized every wave so no file hash
ever repeated.

Run:  python tests/test_scam_images.py
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PIL import Image, ImageDraw  # noqa: E402

from utils import scam_images as si  # noqa: E402


def picture(seed=0, size=(820, 1120)):
    """A synthetic dark screenshot: blocks and bars whose layout depends on seed."""
    img = Image.new("RGB", size, (12, 14, 24))
    d = ImageDraw.Draw(img)
    w, h = size
    for i in range(9):
        x = (seed * 97 + i * 131) % (w - 200)
        y = (seed * 53 + i * 173) % (h - 120)
        shade = 60 + (seed * 31 + i * 41) % 190
        d.rectangle([x, y, x + 120 + (i * 37 + seed * 11) % 260, y + 40 + (i * 29) % 90],
                    fill=(shade, shade, 255 - shade))
    return img


def jpeg(img, quality=85):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def templates_for(img, family="fam"):
    d, p = si.hash_bytes(jpeg(img))
    return [{"family": family, "label": "Test family", "name": "t", "d": d, "p": p}]


class Hashing(unittest.TestCase):
    def test_same_bytes_same_hash(self):
        data = jpeg(picture(1))
        self.assertEqual(si.hash_bytes(data), si.hash_bytes(data))

    def test_not_an_image_is_none(self):
        self.assertIsNone(si.hash_bytes(b"this is not a picture"))
        self.assertIsNone(si.hash_bytes(b""))

    def test_hashes_are_64_bit(self):
        d, p = si.hash_bytes(jpeg(picture(2)))
        self.assertLess(d, 1 << 64)
        self.assertLess(p, 1 << 64)

    def test_hamming(self):
        self.assertEqual(si.hamming(0b1010, 0b0110), 2)
        self.assertEqual(si.hamming(5, 5), 0)


class Matching(unittest.TestCase):
    def setUp(self):
        self.base = picture(3)
        self.templates = templates_for(self.base)

    def test_reencode_still_matches(self):
        # what every wave does: same picture, different bytes
        for q in (95, 70, 50):
            m = si.match(si.hash_bytes(jpeg(self.base, q)), self.templates)
            self.assertIsNotNone(m, q)

    def test_small_resize_still_matches(self):
        for size in ((832, 1134), (806, 1101), (410, 560)):
            m = si.match(si.hash_bytes(jpeg(self.base.resize(size))), self.templates)
            self.assertIsNotNone(m, size)

    def test_png_copy_still_matches(self):
        buf = io.BytesIO()
        self.base.save(buf, "PNG")
        self.assertIsNotNone(si.match(si.hash_bytes(buf.getvalue()), self.templates))

    def test_small_text_swap_still_matches(self):
        # the casino domain changes per wave — a line of text, not the layout
        img = self.base.copy()
        ImageDraw.Draw(img).rectangle([200, 600, 330, 614], fill=(40, 90, 200))
        self.assertIsNotNone(si.match(si.hash_bytes(jpeg(img)), self.templates))

    def test_different_pictures_do_not_match(self):
        for seed in range(10, 40):
            m = si.match(si.hash_bytes(jpeg(picture(seed))), self.templates)
            self.assertIsNone(m, seed)

    def test_plain_images_do_not_match(self):
        for colour in ((0, 0, 0), (255, 255, 255), (12, 14, 24)):
            m = si.match(si.hash_bytes(jpeg(Image.new("RGB", (800, 800), colour))), self.templates)
            self.assertIsNone(m, colour)

    def test_both_hashes_must_agree(self):
        t = self.templates[0]
        close_d_only = (t["d"], t["p"] ^ ((1 << 30) - 1))
        self.assertIsNone(si.match(close_d_only, self.templates))

    def test_closest_template_wins(self):
        t = self.templates[0]
        far = {"family": "far", "label": "Far", "name": "far", "d": t["d"] ^ 0b11111, "p": t["p"]}
        m = si.match((t["d"], t["p"]), [far, dict(t, family="near")])
        self.assertEqual(m["family"], "near")
        self.assertEqual(m["distance"], 0)

    def test_no_hashes_no_templates(self):
        self.assertIsNone(si.match(None, self.templates))
        self.assertIsNone(si.match((1, 2), []))


class Templates(unittest.TestCase):
    def test_shipped_file_loads(self):
        templates, thr = si.load_templates()
        self.assertGreaterEqual(len(templates), 4)
        self.assertLessEqual(thr, si.MAX_THRESHOLD)
        self.assertTrue(all(t["family"] == "mrbeast-casino" for t in templates))

    def test_shipped_templates_cover_the_four_pictures(self):
        names = {t["name"].split(" ")[0] for t in si.load_templates()[0]}
        self.assertEqual(names, {"profile", "promo-code", "withdrawal", "payout-phone"})

    def test_the_four_pictures_are_far_apart(self):
        # a template that sat near another family's would double-match
        templates, thr = si.load_templates()
        for a in templates:
            for b in templates:
                if a["name"].split(" ")[0] != b["name"].split(" ")[0]:
                    dist = max(si.hamming(a["d"], b["d"]), si.hamming(a["p"], b["p"]))
                    self.assertGreater(dist, thr + 6, (a["name"], b["name"]))

    def test_waves_of_one_picture_sit_together(self):
        templates, thr = si.load_templates()
        for a in templates:
            for b in templates:
                if a["name"].split(" ")[0] == b["name"].split(" ")[0]:
                    dist = max(si.hamming(a["d"], b["d"]), si.hamming(a["p"], b["p"]))
                    self.assertLessEqual(dist, thr, (a["name"], b["name"]))

    def test_missing_or_broken_file_is_quiet(self):
        self.assertEqual(si.load_templates("/nope/nothing.json"), ([], si.DEFAULT_THRESHOLD))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
        try:
            self.assertEqual(si.load_templates(f.name)[0], [])
        finally:
            os.unlink(f.name)

    def test_bad_rows_skipped_and_threshold_capped(self):
        doc = {"threshold": 40, "families": [{"key": "k", "templates": [
            {"name": "ok", "dhash": "ff", "phash": "0f"},
            {"name": "no-phash", "dhash": "ff"},
            {"name": "junk", "dhash": "zz", "phash": "0f"}]}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(doc, f)
        try:
            templates, thr = si.load_templates(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual([t["name"] for t in templates], ["ok"])
        self.assertEqual(thr, si.MAX_THRESHOLD)


class Config(unittest.TestCase):
    def test_on_by_default_as_timeout(self):
        self.assertEqual(si.mode({}), "timeout")
        self.assertEqual(si.mode(None), "timeout")

    def test_each_mode_reads_back(self):
        for m in si.MODES:
            self.assertEqual(si.mode({"automod_scamimg_mode": m}), m)
        self.assertEqual(si.mode({"automod_scamimg_mode": "BAN"}), "ban")

    def test_unknown_mode_is_the_default_not_harsher(self):
        self.assertEqual(si.mode({"automod_scamimg_mode": "nuke"}), "timeout")

    def test_master_switch_off_wins(self):
        self.assertEqual(si.mode({"automod_enabled": 0, "automod_scamimg_mode": "ban"}), "off")

    def test_timeout_minutes_clamped(self):
        self.assertEqual(si.timeout_minutes({}), 1440)
        self.assertEqual(si.timeout_minutes({"automod_scamimg_timeout_min": 999999}), 40320)
        self.assertEqual(si.timeout_minutes({"automod_scamimg_timeout_min": -5}), 1)
        self.assertEqual(si.timeout_minutes({"automod_scamimg_timeout_min": "abc"}), 1440)

    def test_staff_not_exempt_by_default(self):
        self.assertFalse(si.staff_exempt({}))
        self.assertTrue(si.staff_exempt({"automod_scamimg_exempt_staff": 1}))

    def test_is_image(self):
        self.assertTrue(si.is_image("image.jpg", "image/jpeg"))
        self.assertTrue(si.is_image("x.bin", "image/png"))
        self.assertTrue(si.is_image("photo.PNG", None))
        self.assertFalse(si.is_image("pay.txt", "text/plain"))
        self.assertFalse(si.is_image("clip.mp4", "video/mp4"))
        self.assertFalse(si.is_image("image.jpg", "application/octet-stream"))


class Ledger(unittest.TestCase):
    def test_round_trip(self):
        path = os.path.join(tempfile.mkdtemp(), "hits.db")
        si.init_db(path)
        si.init_db(path)   # idempotent
        self.assertEqual(si.count_hits(1, 2, path), 0)
        row = {"guild_id": 1, "user_id": 2, "username": "x", "channel_id": 3, "message_id": 4,
               "family": "mrbeast-casino", "template": "profile", "distance": 3, "images": 4,
               "mode": "timeout", "deleted": True, "action": "timed out", "failed": None}
        si.record_hit(row, path)
        si.record_hit(dict(row, message_id=5), path)
        si.record_hit(dict(row, user_id=9), path)
        self.assertEqual(si.count_hits(1, 2, path), 2)
        self.assertEqual(si.count_hits(7, 2, path), 0)

    def test_count_on_missing_table_is_zero(self):
        path = os.path.join(tempfile.mkdtemp(), "empty.db")
        self.assertEqual(si.count_hits(1, 2, path), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
