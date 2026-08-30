"""The two cards /warn produces — the public one in the channel where the
behaviour happened, and the mod-log one.

Before 2026-08-29 the public notice was a plain line of text
(`⚠️ @user — **warning** (1st): reason`). Now it's an embed: the member's name
and avatar as the author line, the reason as the body, the standing count as a
field. Two things must never regress:

  * the PING lives in `content`, not the embed — embeds don't ping, so a card
    with the mention only inside it would silently stop notifying the member;
  * the public card names no moderator. That's on the mod-log card only.

    PYTHONIOENCODING=utf-8 python tests/test_conduct_cards.py
"""
import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

os.environ.setdefault("TORVEX_CONDUCT_DB", os.path.join(ROOT, "tests", "_cards_tmp.db"))

import cogs.conduct as cd  # noqa: E402


def _member(name="Zeeway Chad", uid=943583982862352424, avatar="https://cdn/x.png"):
    m = SimpleNamespace(display_name=name, id=uid, mention=f"<@{uid}>",
                        display_avatar=SimpleNamespace(url=avatar))
    m.__str__ = lambda self=m: f"{name.lower().replace(' ', '')}"
    return m


class PublicCard(unittest.TestCase):
    def test_first_warning_card(self):
        m = _member()
        e = cd.public_warning_card(m, "called all women hoes", "1st", 6, warns=1)
        self.assertEqual(e.author.name, "Zeeway Chad was warned")
        self.assertEqual(e.author.icon_url, "https://cdn/x.png")
        self.assertEqual(e.description, "called all women hoes")
        self.assertEqual(e.colour.value, cd.WARN_COLOR)
        standing = {f.name: f.value for f in e.fields}["Standing"]
        self.assertEqual(standing, "First warning")
        self.assertEqual(e.footer.text, "Warning #6")

    def test_repeat_warning_shows_ordinal_and_cleared_history(self):
        e = cd.public_warning_card(_member(), "again", "3rd", 42, warns=3, cleared=2)
        standing = {f.name: f.value for f in e.fields}["Standing"]
        self.assertEqual(standing, "3rd warning · 5 total, 2 cleared")

    def test_public_card_never_names_the_moderator(self):
        e = cd.public_warning_card(_member(), "reason", "1st", 1, warns=1)
        blob = " ".join([e.author.name or "", e.description or "", e.footer.text or ""]
                        + [f"{f.name} {f.value}" for f in e.fields])
        self.assertNotIn("Moderator", blob)
        self.assertNotIn("<@555>", blob)

    def test_mention_is_not_inside_the_embed(self):
        # The ping has to come from message content — an embed can't mention.
        m = _member()
        e = cd.public_warning_card(m, "reason", "1st", 1, warns=1)
        blob = " ".join([e.description or ""] + [f.value for f in e.fields])
        self.assertNotIn(m.mention, blob)

    def test_missing_avatar_does_not_break_the_card(self):
        m = SimpleNamespace(display_name="ghost", id=1, mention="<@1>")
        e = cd.public_warning_card(m, "r", "1st", 1, warns=1)
        self.assertEqual(e.author.name, "ghost was warned")


class LogCard(unittest.TestCase):
    COUNTS = {"warns": 2, "notes": 1, "cleared": 1}

    def test_log_card_carries_moderator_and_record(self):
        m = _member()
        mod = SimpleNamespace(mention="<@555>")
        ch = SimpleNamespace(mention="<#123>")
        e = cd.log_card(m, mod, "warn", "rude", "2nd", 7, self.COUNTS,
                        channel=ch, public=True, dmed=False)
        f = {x.name: x.value for x in e.fields}
        self.assertEqual(e.title, "⚠️ Warning #7 — 2nd for this member")
        self.assertEqual(f["Moderator"], "<@555>")
        self.assertIn("<@943583982862352424>", f["Member"])
        self.assertEqual(f["Standing record"], "2 warning(s) · 1 note(s) · 1 cleared")
        self.assertEqual(f["Where"], "<#123> · public notice posted")
        self.assertEqual(f["Notified"], "DMs closed")
        self.assertEqual(e.thumbnail.url, "https://cdn/x.png")

    def test_silent_note_has_no_notified_field_and_a_silent_footer(self):
        e = cd.log_card(_member(), SimpleNamespace(mention="<@555>"), "note", "helped",
                        None, 8, {"warns": 0, "notes": 1, "cleared": 0}, silent=True)
        f = {x.name: x.value for x in e.fields}
        self.assertEqual(e.title, "📗 Note #8")
        self.assertEqual(e.colour.value, cd.NOTE_COLOR)
        self.assertNotIn("Notified", f)
        self.assertEqual(e.footer.text, "Silent — the member was not notified")

    def test_evidence_listed(self):
        saved = [{"filename": "shot.png", "bytes": 2048, "sha256": "abcdef1234567890"}]
        e = cd.log_card(_member(), SimpleNamespace(mention="<@555>"), "warn", "r", "1st", 9,
                        {"warns": 1, "notes": 0, "cleared": 0}, saved=saved)
        f = {x.name: x.value for x in e.fields}
        self.assertIn("`shot.png` · 2.0 KB · `abcdef123456`", f["Evidence (1)"])


if __name__ == "__main__":
    unittest.main()
