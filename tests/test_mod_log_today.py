"""mod_log — the 2026-08-23 changes that the other suites don't reach.

`test_mod_log.py` covers the pure helpers and `test_mod_log_retention.py` covers
the sweep and per-guild media layout. Three things shipped that day with no test
of their own, all of which fail SILENTLY when broken:

1. The `edited_timestamp` gate. Discord now sends the full message object on
   every MESSAGE_UPDATE, so "content is present" stopped meaning "content
   changed" — every GIF link's embed-unfurl was logged as a real edit in any
   guild with no stored copy of the message (xottic, 8/23). A regression here
   doesn't error, it just fills a stranger's mod-log with phantom edits.

2. Evict-after-repost. Once a deleted attachment has been re-posted to the log
   channel, Discord holds the evidence and the local copy is dropped, so the
   cache holds pending files rather than history. If the eviction stops
   happening the disk fills quietly; if it evicts too much, evidence is lost.

3. The tier export the dashboard reads. If it silently stops being written the
   dashboard falls back to "free tier" copy for everyone — a paying server is
   told it has nothing.

    PYTHONIOENCODING=utf-8 python tests/test_mod_log_today.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.mod_log as ml  # noqa: E402

NOW = time.time()
GUILD = "1215140346800119868"


class _Bot:
    user = type("U", (), {"id": 1})()

    def get_guild(self, gid):
        return type("G", (), {"id": int(gid), "name": "g"})()


class _ModLogCase(unittest.TestCase):
    """Shared scaffolding: a throwaway DB + media dir, module globals restored."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modlog_today_")
        self._db, self._media = ml.DB_PATH, ml.MEDIA_DIR
        self._archive, self._operator = ml.ARCHIVE_GUILDS, ml.OPERATOR_GUILDS
        self._is_enabled, self._get_config = ml.is_enabled, ml.get_config
        self._tiers_env = os.environ.get("TORVEX_MSGLOG_TIERS_JSON")
        ml.DB_PATH = os.path.join(self.tmp, "messages.db")
        ml.MEDIA_DIR = os.path.join(self.tmp, "media_cache")
        ml.ARCHIVE_GUILDS = {GUILD}
        ml.OPERATOR_GUILDS = {GUILD}
        os.environ["TORVEX_MSGLOG_TIERS_JSON"] = os.path.join(self.tmp, "tiers.json")
        ml._CONSENT.clear()
        ml._PRO.clear()
        self.cog = ml.ModLog(_Bot())

    def tearDown(self):
        ml.DB_PATH, ml.MEDIA_DIR = self._db, self._media
        ml.ARCHIVE_GUILDS, ml.OPERATOR_GUILDS = self._archive, self._operator
        ml.is_enabled, ml.get_config = self._is_enabled, self._get_config
        ml._CONSENT.clear()
        ml._PRO.clear()
        if self._tiers_env is None:
            os.environ.pop("TORVEX_MSGLOG_TIERS_JSON", None)
        else:
            os.environ["TORVEX_MSGLOG_TIERS_JSON"] = self._tiers_env
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── 1. the phantom-edit gate ──────────────────────────────────────────────


class _Payload:
    """Minimal stand-in for discord.RawMessageUpdateEvent."""

    def __init__(self, data, message_id="9001", guild_id=int(GUILD), channel_id=5):
        self.data = data
        self.message_id = message_id
        self.guild_id = guild_id
        self.channel_id = channel_id


def _unfurl_payload(content="look https://tenor.com/x.gif"):
    """What Discord sends when it attaches a link preview: the whole message,
    including content, with edited_timestamp still null."""
    return _Payload({"content": content, "edited_timestamp": None,
                     "author": {"id": "77"}, "embeds": [{"type": "gifv"}]})


def _real_edit_payload(content="fixed typo"):
    return _Payload({"content": content, "edited_timestamp": "2026-08-23T18:00:00+00:00",
                     "author": {"id": "77"}})


class EditGate(unittest.IsolatedAsyncioTestCase, _ModLogCase):
    def setUp(self):
        _ModLogCase.setUp(self)
        ml.is_enabled = lambda gid, feature: True
        ml.get_config = lambda gid: {"msglog_edits": 1}
        # Stop before any Discord I/O: the handler writes the edits row first,
        # then bails when there is nowhere to post. That row is the observable.
        self.cog._log_channel = lambda guild, cfg: None

    def tearDown(self):
        _ModLogCase.tearDown(self)

    def _edit_rows(self):
        with self.cog._conn() as c:
            return c.execute("SELECT * FROM edits").fetchall()

    async def test_unfurl_is_ignored(self):
        # The bug: this used to insert an edit row and post "Before: not in
        # archive" for every link that got a preview.
        await self.cog.on_raw_message_edit(_unfurl_payload())
        self.assertEqual(self._edit_rows(), [])

    async def test_missing_edited_timestamp_key_is_ignored(self):
        # Pins/component updates omit the key entirely rather than nulling it.
        await self.cog.on_raw_message_edit(_Payload({"content": "hi", "author": {"id": "77"}}))
        self.assertEqual(self._edit_rows(), [])

    async def test_real_edit_proceeds(self):
        await self.cog.on_raw_message_edit(_real_edit_payload("fixed typo"))
        rows = self._edit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["new_content"], "fixed typo")
        self.assertEqual(rows[0]["guild_id"], GUILD)

    async def test_payload_without_content_is_ignored(self):
        # A partial update (reaction, flags) carries no content to compare.
        await self.cog.on_raw_message_edit(
            _Payload({"edited_timestamp": "2026-08-23T18:00:00+00:00"}))
        self.assertEqual(self._edit_rows(), [])

    async def test_empty_data_is_ignored(self):
        await self.cog.on_raw_message_edit(_Payload({}))
        self.assertEqual(self._edit_rows(), [])

    async def test_dm_edit_is_ignored(self):
        p = _real_edit_payload()
        p.guild_id = None
        await self.cog.on_raw_message_edit(p)
        self.assertEqual(self._edit_rows(), [])

    async def test_unchanged_content_is_ignored_even_with_a_timestamp(self):
        # Belt and braces: the older guard still applies once a row is stored.
        self.cog._remember({
            "message_id": "9001", "guild_id": GUILD, "channel_id": "5",
            "author_id": "77", "author_name": "a", "bot": 0, "webhook": 0,
            "created_ts": NOW, "content": "same", "reply_to": None,
            "attachments": None, "stickers": None})
        self.cog._flush()
        await self.cog.on_raw_message_edit(_real_edit_payload("same"))
        self.assertEqual(self._edit_rows(), [])

    async def test_real_edit_records_the_previous_text_as_before(self):
        self.cog._remember({
            "message_id": "9001", "guild_id": GUILD, "channel_id": "5",
            "author_id": "77", "author_name": "a", "bot": 0, "webhook": 0,
            "created_ts": NOW, "content": "original", "reply_to": None,
            "attachments": None, "stickers": None})
        self.cog._flush()
        await self.cog.on_raw_message_edit(_real_edit_payload("changed"))
        rows = self._edit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["old_content"], "original")
        self.assertEqual(rows[0]["new_content"], "changed")

    async def test_disabled_guild_records_nothing(self):
        ml.is_enabled = lambda gid, feature: False
        await self.cog.on_raw_message_edit(_real_edit_payload())
        self.assertEqual(self._edit_rows(), [])


# ── 2. evict-after-repost ─────────────────────────────────────────────────


class _FakeFP:
    def __init__(self, name):
        self.name = name


class _FakeFile:
    """Stands in for discord.File: production reads `.fp.name` and closes it."""

    def __init__(self, path):
        self.fp = _FakeFP(path)
        self.closed = False

    def close(self):
        self.closed = True


class MediaEviction(_ModLogCase):
    def _write(self, guild_id, name, size=100):
        d = self.cog._media_dir(guild_id, create=True)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def test_removal_updates_the_guild_byte_count(self):
        a = self._write(GUILD, "1_0_a.png", 100)
        self._write(GUILD, "1_1_b.png", 40)
        self.assertEqual(self.cog._guild_media_bytes(GUILD), 140)
        self.assertTrue(self.cog._remove_media_file(a))
        self.assertEqual(self.cog._guild_media_bytes(GUILD), 40)
        self.assertFalse(os.path.exists(a))

    def test_removal_is_idempotent_and_never_double_decrements(self):
        a = self._write(GUILD, "1_0_a.png", 100)
        self.cog._guild_media_bytes(GUILD)          # prime the cache
        self.assertTrue(self.cog._remove_media_file(a))
        self.assertEqual(self.cog._guild_media_bytes(GUILD), 0)
        # A second pass (retry, overlapping sweep) must be a quiet no-op.
        self.assertFalse(self.cog._remove_media_file(a))
        self.assertEqual(self.cog._guild_media_bytes(GUILD), 0)

    def test_byte_count_never_goes_negative(self):
        a = self._write(GUILD, "1_0_a.png", 100)
        self.cog._guild_bytes[GUILD] = 0            # cache understates reality
        self.cog._remove_media_file(a)
        self.assertEqual(self.cog._guild_media_bytes(GUILD), 0)

    def test_missing_file_returns_false(self):
        self.assertFalse(self.cog._remove_media_file(
            os.path.join(self.tmp, "media_cache", GUILD, "nope.png")))

    def test_legacy_flat_file_is_removed_without_touching_any_guild_count(self):
        os.makedirs(ml.MEDIA_DIR, exist_ok=True)
        flat = os.path.join(ml.MEDIA_DIR, "1_0_old.png")
        with open(flat, "wb") as f:
            f.write(b"x" * 50)
        self.cog._guild_bytes[GUILD] = 999
        self.assertTrue(self.cog._remove_media_file(flat))
        self.assertFalse(os.path.exists(flat))
        self.assertEqual(self.cog._guild_bytes[GUILD], 999,
                         "a flat-layout file belongs to no guild bucket")

    def test_reposted_files_are_evicted_and_quarantined_files_are_kept(self):
        """Mirrors the production loop at the end of on_raw_message_delete."""
        reposted = [self._write(GUILD, "1_0_a.png"), self._write(GUILD, "1_1_b.gif")]
        quarantined = self._write(GUILD, "1_2_payload.exe")
        files = [_FakeFile(p) for p in reposted]

        for f in files:                              # the production eviction loop
            name = getattr(getattr(f, "fp", None), "name", None)
            try:
                f.close()
            except Exception:
                pass
            if name:
                self.cog._remove_media_file(name)

        for p in reposted:
            self.assertFalse(os.path.exists(p), "re-posted evidence lives in Discord now")
        self.assertTrue(os.path.exists(quarantined),
                        "non-media files are never re-posted, so they stay for review")
        self.assertTrue(all(f.closed for f in files), "handles must be released")


# ── 3. the dashboard tier export ──────────────────────────────────────────


class TierExport(_ModLogCase):
    @property
    def path(self):
        return os.environ["TORVEX_MSGLOG_TIERS_JSON"]

    def _read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_window_constants_are_published(self):
        self.cog._export_tiers()
        d = self._read()
        self.assertEqual(d["recent_hours"], ml.RECENT_HOURS)
        self.assertEqual(d["recent_media_mb"], ml.RECENT_MEDIA_MB)
        self.assertEqual(d["pro_text_days"], ml.PRO_TEXT_DAYS)
        self.assertEqual(d["pro_media_days"], ml.PRO_MEDIA_DAYS)
        self.assertEqual(d["pro_media_mb"], ml.PRO_MEDIA_MB)
        self.assertEqual(d["pro_url"], ml.PRO_URL)
        self.assertEqual(d["terms_version"], ml.TERMS_VERSION)
        self.assertIsInstance(d["generated_at"], int)

    def test_operator_guild_is_reported_as_operator_tier(self):
        self.cog._export_tiers()
        self.assertEqual(self._read()["guilds"][GUILD]["tier"], "operator")

    def test_pro_guild_reports_active_and_accepted(self):
        ml._PRO["222"] = {"expires_ts": NOW + 86400}
        ml._CONSENT["222"] = {"version": ml.TERMS_VERSION}
        self.cog._export_tiers()
        row = self._read()["guilds"]["222"]
        self.assertEqual(row["tier"], "pro")
        self.assertTrue(row["pro_active"])
        self.assertTrue(row["terms_accepted"])
        self.assertAlmostEqual(row["pro_expires_ts"], NOW + 86400, places=3)

    def test_paid_but_unaccepted_guild_is_not_pro(self):
        # Both halves are required; the dashboard tells them which is missing.
        ml._PRO["333"] = {"expires_ts": NOW + 86400}
        self.cog._export_tiers()
        row = self._read()["guilds"]["333"]
        self.assertEqual(row["tier"], "recent")
        self.assertTrue(row["pro_active"])
        self.assertFalse(row["terms_accepted"])

    def test_lapsed_pro_reports_inactive(self):
        ml._PRO["444"] = {"expires_ts": NOW - 86400}
        ml._CONSENT["444"] = {"version": ml.TERMS_VERSION}
        self.cog._export_tiers()
        row = self._read()["guilds"]["444"]
        self.assertEqual(row["tier"], "recent")
        self.assertFalse(row["pro_active"])

    def test_no_temp_file_is_left_behind(self):
        self.cog._export_tiers()
        self.assertFalse(os.path.exists(self.path + ".tmp"),
                         "the write is atomic: tmp then os.replace")

    def test_rewrite_replaces_cleanly(self):
        self.cog._export_tiers()
        ml._PRO["555"] = {"expires_ts": None}
        ml._CONSENT["555"] = {"version": ml.TERMS_VERSION}
        self.cog._export_tiers()
        d = self._read()
        self.assertIn("555", d["guilds"])
        self.assertTrue(d["guilds"]["555"]["pro_active"], "no expiry = active")

    def test_unwritable_path_is_swallowed(self):
        # The bot must never fail to load over a docs artifact.
        os.environ["TORVEX_MSGLOG_TIERS_JSON"] = os.path.join(
            self.tmp, "no-such-dir", "tiers.json")
        try:
            self.cog._export_tiers()
        except Exception as e:  # noqa: BLE001 - the point is that nothing escapes
            self.fail(f"export raised {type(e).__name__}: {e}")

    def test_malformed_expiry_is_swallowed(self):
        """REGRESSION (found by test 2026-08-23, fixed same day): pro_active()
        did float(expires_ts) unguarded, and _export_tiers caught only OSError.
        Since _load_pro/_load_consent call _export_tiers OUTSIDE their try and
        _load_pro runs from __init__, one bad row took the WHOLE cog down —
        deletes, edits, joins, bans all stop being logged. SQLite's dynamic
        typing accepts text in a REAL column, so a checkout webhook writing an
        ISO date instead of an epoch was enough to trigger it."""
        for bad in ("2026-09-01T00:00:00Z", "", "soon", object(), [1]):
            with self.subTest(expires_ts=bad):
                ml._PRO["666"] = {"expires_ts": bad}
                try:
                    self.cog._export_tiers()
                except Exception as e:  # noqa: BLE001
                    self.fail(f"export raised {type(e).__name__}: {e}")
                self.assertFalse(ml.pro_active("666"),
                                 "an unreadable expiry must fail CLOSED, not grant Pro")

    def test_cog_still_constructs_with_a_malformed_pro_row(self):
        """The actual blast radius: __init__ must survive it."""
        with self.cog._conn() as c:
            c.execute("INSERT OR REPLACE INTO archive_pro"
                      " (guild_id, guild_name, expires_ts, granted_ts, granted_by, note, order_ref)"
                      " VALUES ('777', 'bad row', '2026-09-01T00:00:00Z', 0, 'test', NULL, NULL)")
        try:
            ml.ModLog(_Bot())
        except Exception as e:  # noqa: BLE001
            self.fail(f"ModLog.__init__ raised {type(e).__name__}: {e} — cog would not load")
        self.assertFalse(ml.pro_active("777"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
