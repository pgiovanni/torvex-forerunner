"""Conduct-record behaviour added 2026-08-23 — the notify matrix, the per-member
warning ordinal, and where warnings get logged.

`tests/test_conduct.py` predates all of it (written 8/8, storage-layer only), and
this is the part members actually see: whether they get DMed, whether they get
pinged in public, and what number the mod is told. Getting it wrong is visible to
the whole server, and one case is worse than visible — the mod-log footer
claiming "silent" while a DM already went out would make the record lie.

    PYTHONIOENCODING=utf-8 python tests/test_conduct_notify.py
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

os.environ.setdefault("TORVEX_CONDUCT_DB", os.path.join(ROOT, "tests", "_notify_tmp.db"))

import cogs.conduct as cd  # noqa: E402

BOTH_ON = {"dm": True, "public": True}
BOTH_OFF = {"dm": False, "public": False}


class NotifyMatrix(unittest.TestCase):
    """The full cross-product of server defaults × per-call choice."""

    def test_default_follows_server_settings(self):
        self.assertEqual(cd.notify_plan("warn", BOTH_ON), (True, True, False))
        self.assertEqual(cd.notify_plan("warn", BOTH_OFF), (False, False, True))
        self.assertEqual(cd.notify_plan("warn", {"dm": True, "public": False}), (True, False, False))
        self.assertEqual(cd.notify_plan("warn", {"dm": False, "public": True}), (False, True, False))

    def test_explicit_choices_override_defaults_in_both_directions(self):
        # A mod can go louder than the server default...
        self.assertEqual(cd.notify_plan("warn", BOTH_OFF, "both"), (True, True, False))
        self.assertEqual(cd.notify_plan("warn", BOTH_OFF, "ping"), (False, True, False))
        self.assertEqual(cd.notify_plan("warn", BOTH_OFF, "dm"), (True, False, False))
        # ...or quieter, to record a repeat offender without a scene.
        self.assertEqual(cd.notify_plan("warn", BOTH_ON, "none"), (False, False, True))
        self.assertEqual(cd.notify_plan("warn", BOTH_ON, "dm"), (True, False, False))
        self.assertEqual(cd.notify_plan("warn", BOTH_ON, "ping"), (False, True, False))

    def test_a_note_never_pings_publicly(self):
        # Notes are positives and quiet observations, not announcements. Even an
        # explicit "ping" request must not put one in the channel.
        for choice in ("default", "both", "ping"):
            _, do_ping, _ = cd.notify_plan("note", BOTH_ON, choice)
            self.assertFalse(do_ping, f"note must never ping (choice={choice})")

    def test_a_note_can_still_dm(self):
        self.assertEqual(cd.notify_plan("note", BOTH_ON, "dm"), (True, False, False))
        self.assertEqual(cd.notify_plan("note", BOTH_ON, "both"), (True, False, False))

    def test_note_with_ping_only_is_silent_not_public(self):
        # "ping" on a note collapses to nothing; silent must reflect reality.
        self.assertEqual(cd.notify_plan("note", BOTH_ON, "ping"), (False, False, True))

    def test_silent_is_derived_never_asserted(self):
        # The mod-log footer prints "silent" from this flag. If it could be true
        # while a DM went out, the record would lie about what the member knew.
        for kind in ("warn", "note"):
            for cfg in (BOTH_ON, BOTH_OFF, {"dm": True, "public": False}):
                for choice in ("default", "both", "dm", "ping", "none"):
                    do_dm, do_ping, silent = cd.notify_plan(kind, cfg, choice)
                    self.assertEqual(silent, not (do_dm or do_ping),
                                     f"{kind}/{choice}/{cfg} — silent disagreed with reality")

    def test_missing_config_keys_default_to_quiet(self):
        # An empty cfg dict must not raise and must not spontaneously announce.
        self.assertEqual(cd.notify_plan("warn", {}), (False, False, True))

    def test_unknown_choice_is_treated_as_no_notification(self):
        # Discord only sends our own choice values, but a stale client or a
        # future rename must not silently fall back to "announce everywhere".
        self.assertEqual(cd.notify_plan("warn", BOTH_ON, "shout"), (False, False, True))


class Ordinal(unittest.TestCase):
    def test_small_numbers(self):
        got = [cd._ordinal(n) for n in range(1, 5)]
        self.assertEqual(got, ["1st", "2nd", "3rd", "4th"])

    def test_the_teens_are_all_th(self):
        # 11/12/13 are the classic ordinal bug.
        self.assertEqual([cd._ordinal(n) for n in (11, 12, 13)], ["11th", "12th", "13th"])

    def test_twenties_resume_the_pattern(self):
        self.assertEqual([cd._ordinal(n) for n in (21, 22, 23, 24)],
                         ["21st", "22nd", "23rd", "24th"])

    def test_hundreds(self):
        self.assertEqual([cd._ordinal(n) for n in (100, 101, 111, 112)],
                         ["100th", "101st", "111th", "112th"])

    def test_zero(self):
        # Reachable only if counts() were read before the insert; it must not crash.
        self.assertEqual(cd._ordinal(0), "0th")

    def test_string_input(self):
        self.assertEqual(cd._ordinal("3"), "3rd")


class LogChannelRouting(unittest.TestCase):
    """Paul, 8/23: one channel for AltGuard, one for everything moderation.
    Warnings must land in the Mod Logs channel, never default to the security
    log — len's first warning went to the AltGuard channel because of this."""

    def setUp(self):
        self._orig = cd.get_config

    def tearDown(self):
        cd.get_config = self._orig

    def _resolve(self, cfg):
        cd.get_config = lambda gid: cfg
        return cd._cfg(1)["log_channel_id"]

    def test_explicit_conduct_channel_wins(self):
        self.assertEqual(self._resolve({
            "conduct_log_channel_id": 1, "mod_log_channel_id": 2,
            "msglog_channel_id": 3, "modlog_channel_id": 4}), 1)

    def test_then_the_moderation_channel(self):
        self.assertEqual(self._resolve({
            "mod_log_channel_id": 2, "msglog_channel_id": 3, "modlog_channel_id": 4}), 2)

    def test_then_the_message_log(self):
        self.assertEqual(self._resolve({"msglog_channel_id": 3, "modlog_channel_id": 4}), 3)

    def test_security_log_is_the_last_resort_not_the_default(self):
        self.assertEqual(self._resolve({"modlog_channel_id": 4}), 4)

    def test_nothing_configured_is_none_not_a_crash(self):
        self.assertIsNone(self._resolve({}))

    def test_defaults_are_dm_and_ping_on(self):
        cd.get_config = lambda gid: {}
        c = cd._cfg(1)
        self.assertTrue(c["dm"], "a warned member should be told by default")
        self.assertTrue(c["public"], "the room should see it was handled by default")
        self.assertTrue(c["require_reason"], "a warning with no reason isn't a record")

    def test_toggles_are_respected(self):
        cd.get_config = lambda gid: {"conduct_dm_on_warn": 0, "conduct_public_warn": 0}
        c = cd._cfg(1)
        self.assertFalse(c["dm"])
        self.assertFalse(c["public"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
