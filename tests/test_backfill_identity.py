"""Tests for backfill_identity.py — the parser that mines identity history out
of Carl-bot / MEE6 / Quark embeds.

These embeds are the ONLY surviving record of names and numeric ids for
accounts that have since been deleted (proven 2026-07-28), so a parsing miss is
permanent data loss. Shapes below are copied from real messages in the guild.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backfill_identity import (  # noqa: E402
    parse_before_after, rows_from_message, ts_of, snowflake_for)


def msg(mid="1377047156279480502", content="", embeds=()):
    return {"id": mid, "content": content, "embeds": list(embeds)}


def embed(title=None, desc="", footer=None, author=None, fields=()):
    e = {"title": title, "description": desc}
    if footer:
        e["footer"] = {"text": footer}
    if author:
        e["author"] = {"name": author}
    if fields:
        e["fields"] = [{"name": n, "value": v} for n, v in fields]
    return e


class BeforeAfterTests(unittest.TestCase):
    def test_carl_nickname_shape(self):
        b, a = parse_before_after("**Before:** loser\n**+After:** keep coping fatso")
        self.assertEqual(b, "loser")
        self.assertEqual(a, "keep coping fatso")

    def test_slash_separated_shape(self):
        b, a = parse_before_after("**Before:** nogie01 / **+After:** admin_nogie")
        self.assertEqual(b, "nogie01")
        self.assertEqual(a, "admin_nogie")

    def test_no_plus_prefix(self):
        b, a = parse_before_after("**Before:** x / **After:** y")
        self.assertEqual((b, a), ("x", "y"))

    def test_not_a_diff(self):
        self.assertEqual(parse_before_after("<@123> 1,474th to join"), (None, None))

    def test_empty(self):
        self.assertEqual(parse_before_after(""), (None, None))
        self.assertEqual(parse_before_after(None), (None, None))


class RowExtractionTests(unittest.TestCase):
    def test_nickname_change_keeps_uid_from_footer(self):
        """The footer id is the whole point — it outlives the account."""
        rows = rows_from_message(msg(embeds=[embed(
            title="Nickname change",
            desc="**Before:** loser / **+After:** half the members here are my alt",
            footer="ID: 1377060168243609653",
            author="eide92398dhu3heh20_26354")]))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["uid"], "1377060168243609653")
        self.assertEqual(r["kind"], "nick")
        self.assertEqual(r["before"], "loser")
        self.assertEqual(r["after"], "half the members here are my alt")

    def test_member_joined_records_name(self):
        rows = rows_from_message(msg(embeds=[embed(
            title="Member joined",
            desc="<@1311408487451988018> 1,474th to join / created 6 months ago",
            footer="ID: 1311408487451988018", author="misanthropechudjak")]))
        self.assertEqual(rows[0]["kind"], "join")
        self.assertEqual(rows[0]["uid"], "1311408487451988018")
        self.assertEqual(rows[0]["after"], "misanthropechudjak")

    def test_carl_case_ban_embed(self):
        """Case embeds carry actor + reason in the description, not fields."""
        rows = rows_from_message(msg(embeds=[embed(
            title="ban | case 9",
            desc=("**Offender:** misanthropechudjak<@1311408487451988018> / "
                  "**Reason:** Unspecified / **Responsible moderator:** MEE6#4876"),
            footer="ID: 1311408487451988018")]))
        self.assertEqual(rows[0]["kind"], "ban")
        self.assertEqual(rows[0]["uid"], "1311408487451988018")
        self.assertEqual(rows[0]["by_name"], "MEE6#4876")
        self.assertIn("Unspecified", rows[0]["reason"])

    def test_uid_falls_back_to_description_mention(self):
        rows = rows_from_message(msg(embeds=[embed(
            title="Member left", desc="<@1296443530893594707> joined 24 minutes ago",
            author="rockfinele")]))
        self.assertEqual(rows[0]["uid"], "1296443530893594707")

    def test_mee6_plaintext_leave_notice(self):
        """Pre-embed era: the name is all that survives, and it still matters."""
        rows = rows_from_message(msg(content="**misanthropechudjak** just left the server"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "leave")
        self.assertEqual(rows[0]["after"], "misanthropechudjak")
        self.assertIsNone(rows[0]["uid"])

    def test_fields_supply_moderator_and_reason(self):
        rows = rows_from_message(msg(embeds=[embed(
            title="Member banned", desc="<@1> gone", footer="ID: 1",
            fields=[("By", "mrdudebro1"), ("Reason", "dox threats")])]))
        self.assertEqual(rows[0]["by_name"], "mrdudebro1")
        self.assertIn("dox threats", rows[0]["reason"])

    def test_unrelated_embed_ignored(self):
        rows = rows_from_message(msg(embeds=[embed(
            title="Message edited", desc="**Before:** a / **+After:** b",
            footer="Message ID 999")]))
        self.assertEqual(rows, [])

    def test_timeout_titles_map(self):
        for title in ("Member timed out", "Member muted", "timeout added"):
            rows = rows_from_message(msg(embeds=[embed(
                title=title, desc="<@5> quiet", footer="ID: 5")]))
            self.assertTrue(rows and rows[0]["kind"] in ("timeout", "untimeout"),
                            f"{title!r} produced {rows}")

    def test_multiple_embeds_in_one_message(self):
        rows = rows_from_message(msg(embeds=[
            embed(title="Member joined", desc="<@7> hi", footer="ID: 7", author="a"),
            embed(title="Nickname change", desc="**Before:** x / **+After:** y",
                  footer="ID: 7", author="a")]))
        self.assertEqual([r["kind"] for r in rows], ["join", "nick"])


class SnowflakeTests(unittest.TestCase):
    def test_ts_of_known_message(self):
        """1377047156279480502 was posted 2025-05-27 22:13:46 UTC."""
        self.assertAlmostEqual(ts_of("1377047156279480502"), 1748384026.356, delta=2)

    def test_roundtrip(self):
        from datetime import datetime, timezone
        dt = datetime(2025, 5, 27, tzinfo=timezone.utc)
        self.assertAlmostEqual(ts_of(str(snowflake_for(dt))), dt.timestamp(), delta=1)


if __name__ == "__main__":
    unittest.main()
