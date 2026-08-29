"""Slash-command log (2026-08-29): every app-command invocation — completed or
denied — lands in command_log as plain text, swept under the guild's text
window, purged with the guild, dropped by /msglog forget.

    PYTHONIOENCODING=utf-8 python tests/test_mod_log_commands.py
"""
import asyncio
import json
import os
import sys
import time
import shutil
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
import cogs.mod_log as ml  # noqa: E402

NOW = time.time()
H, D = 3600, 86400
OPERATOR, FREE = "111", "444"


class _Bot:
    user = type("U", (), {"id": 1})()


class _User:
    def __init__(self, uid, name):
        self.id, self.name = uid, name

    def __str__(self):
        return self.name


class _NS:
    def __init__(self, **kw):
        self._kw = kw

    def __iter__(self):
        return iter(self._kw.items())


class _Cmd:
    def __init__(self, name):
        self.qualified_name = self.name = name


class _Interaction:
    def __init__(self, guild_id, uid, name="tester", itype=discord.InteractionType.application_command,
                 command=None, data=None, **opts):
        self.type = itype
        self.guild_id = guild_id
        self.channel_id = 999
        self.user = _User(uid, name)
        self.command = command
        self.data = data or {}
        self.namespace = _NS(**opts)


class CommandLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modlog_cmd_")
        self._db, self._media = ml.DB_PATH, ml.MEDIA_DIR
        ml.DB_PATH = os.path.join(self.tmp, "messages.db")
        ml.MEDIA_DIR = os.path.join(self.tmp, "media_cache")
        ml.ARCHIVE_GUILDS = {OPERATOR}
        ml.OPERATOR_GUILDS = {OPERATOR}
        self.cog = ml.ModLog(_Bot())

    def tearDown(self):
        ml.DB_PATH, ml.MEDIA_DIR = self._db, self._media
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, **where):
        q = "SELECT * FROM command_log"
        if where:
            q += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        with self.cog._conn() as c:
            return [dict(r) for r in c.execute(q, tuple(where.values()))]

    def test_completion_records_name_options_ok(self):
        i = _Interaction(int(FREE), 7, command=_Cmd("msglog audit"), kind="role", limit=5)
        self.cog._record_command(i, _Cmd("msglog audit"))
        (r,) = self.rows()
        self.assertEqual((r["guild_id"], r["user_id"], r["command"], r["ok"], r["error"]),
                         (FREE, "7", "msglog audit", 1, None))
        self.assertEqual(json.loads(r["options"]), {"kind": "role", "limit": 5})
        self.assertEqual(r["channel_id"], "999")

    def test_denial_records_unwrapped_error_class(self):
        i = _Interaction(int(FREE), 8, command=_Cmd("antinuke status"))
        err = app_commands.MissingPermissions(["manage_guild"])
        self.cog._record_command(i, error=err)
        (r,) = self.rows()
        self.assertEqual((r["ok"], r["error"], r["command"]), (0, "MissingPermissions", "antinuke status"))

    def test_invoke_error_unwraps_original(self):
        i = _Interaction(int(FREE), 8, command=_Cmd("x"))
        err = app_commands.CommandInvokeError(_Cmd("x"), KeyError("boom"))
        self.cog._record_command(i, error=err)
        self.assertEqual(self.rows()[0]["error"], "KeyError")

    def test_unknown_command_falls_back_to_payload_name(self):
        i = _Interaction(int(FREE), 8, command=None, data={"name": "ghost"})
        self.cog._record_command(i, error=app_commands.CommandNotFound("ghost", []))
        self.assertEqual(self.rows()[0]["command"], "ghost")

    def test_non_command_interactions_ignored(self):
        i = _Interaction(int(FREE), 8, itype=discord.InteractionType.autocomplete, command=_Cmd("x"))
        self.cog._record_command(i, _Cmd("x"))
        i2 = _Interaction(int(FREE), 8, itype=discord.InteractionType.component, command=_Cmd("x"))
        self.cog._record_command(i2, error=RuntimeError("x"))
        self.assertEqual(self.rows(), [])

    def test_dm_rows_tagged_and_swept(self):
        i = _Interaction(None, 8, command=_Cmd("help"))
        self.cog._record_command(i, _Cmd("help"))
        self.assertEqual(self.rows()[0]["guild_id"], "dm")
        with self.cog._conn() as c:
            c.execute("UPDATE command_log SET ts=?", (NOW - 2 * D,))
        self.cog._sweep_rows(NOW)
        self.assertEqual(self.rows(), [])

    def test_sweep_respects_guild_window(self):
        for gid in (OPERATOR, FREE):
            self.cog._record_command(_Interaction(int(gid), 8, command=_Cmd("help")), _Cmd("help"))
        with self.cog._conn() as c:
            c.execute("UPDATE command_log SET ts=?", (NOW - 3 * D,))
        self.cog._sweep_rows(NOW)
        self.assertEqual([r["guild_id"] for r in self.rows()], [OPERATOR])

    def test_purge_guild_drops_its_commands_only(self):
        for gid in (OPERATOR, FREE):
            self.cog._record_command(_Interaction(int(gid), 8, command=_Cmd("help")), _Cmd("help"))
        counts = self.cog._purge_guild(FREE)
        self.assertEqual(counts["commands"], 1)
        self.assertEqual([r["guild_id"] for r in self.rows()], [OPERATOR])

    def test_listener_and_tree_error_chain(self):
        calls = []

        async def prior(interaction, error):
            calls.append(type(error).__name__)

        self.cog._orig_tree_error = prior
        i = _Interaction(int(FREE), 8, command=_Cmd("help"))
        asyncio.run(self.cog.on_app_command_completion(i, _Cmd("help")))
        asyncio.run(self.cog._on_tree_error(i, app_commands.CheckFailure("no")))
        self.assertEqual(calls, ["CheckFailure"])
        self.assertEqual(sorted(r["ok"] for r in self.rows()), [0, 1])

    def test_swallows_bad_interaction(self):
        self.cog._record_command(object(), None)  # no .type → must not raise
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
