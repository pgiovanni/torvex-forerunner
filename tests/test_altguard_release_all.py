"""`/altguard-release everyone:True` — the bulk let-in for switching the gate off.

Built 2026-08-29 after the gate was turned off for the home guild and the
seven held members had no command to release them all (the alternative was
a raw REST script around the bot — exactly what the "everything through the
cogs" rule forbids). Covers the pure target selection and the store listing
it feeds on.
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="releaseall")
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_TMP, "security_config.db")
os.environ.setdefault("ALTGUARD_GUILD_ID", "111")
os.environ.setdefault("ALTGUARD_QUARANTINE_ROLE_ID", "5555")
os.environ.setdefault("ALTGUARD_ALMOST_ROLE_ID", "7777")

import quarantine_store as qstore  # noqa: E402

qstore._PATH = os.path.join(_TMP, "altguard_quarantine.db")
qstore.init()

import cogs.altguard as ag  # noqa: E402

Q, ALM = 5555, 7777


def _role(rid):
    return SimpleNamespace(id=rid)


def _member(uid, *role_ids, bot=False):
    return SimpleNamespace(id=uid, bot=bot, roles=[_role(r) for r in role_ids])


class BulkReleaseTargets(unittest.TestCase):
    def test_wearing_quarantine_or_almost_is_held(self):
        members = [_member(1, Q), _member(2, ALM), _member(3, 42)]
        held, stale = ag.bulk_release_targets(members, Q, ALM, set())
        self.assertEqual([m.id for m in held], [1, 2])
        self.assertEqual(stale, [])

    def test_store_row_without_role_still_counts_as_held(self):
        # the reconciliation listener keys on the store, so a row alone holds them
        members = [_member(1, 42)]
        held, stale = ag.bulk_release_targets(members, Q, ALM, {1})
        self.assertEqual([m.id for m in held], [1])

    def test_stored_uid_not_in_server_is_stale(self):
        members = [_member(1, Q)]
        held, stale = ag.bulk_release_targets(members, Q, ALM, {1, 900, 800})
        self.assertEqual([m.id for m in held], [1])
        self.assertEqual(stale, [800, 900])

    def test_bots_are_never_targets(self):
        members = [_member(1, Q, bot=True), _member(2, Q)]
        held, _ = ag.bulk_release_targets(members, Q, ALM, set())
        self.assertEqual([m.id for m in held], [2])

    def test_nothing_held_is_empty(self):
        held, stale = ag.bulk_release_targets([_member(1, 42)], Q, ALM, set())
        self.assertEqual((held, stale), ([], []))


class ListQuarantined(unittest.TestCase):
    def setUp(self):
        with qstore._conn() as c:
            c.execute("DELETE FROM quarantined")

    def test_lists_one_guild_or_all(self):
        qstore.save(10, 111, [1, 2], "join hold")
        qstore.save(20, 111, [], "manual")
        qstore.save(30, 222, [3], "other guild")
        self.assertEqual(sorted(qstore.list_quarantined(111)), [10, 20])
        self.assertEqual(sorted(qstore.list_quarantined()), [10, 20, 30])
        self.assertEqual(qstore.list_quarantined(333), [])

    def test_pop_removes_from_listing(self):
        qstore.save(10, 111, [1], "join hold")
        qstore.pop(10)
        self.assertEqual(qstore.list_quarantined(111), [])


if __name__ == "__main__":
    unittest.main()
