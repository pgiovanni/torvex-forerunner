"""Almost Verified must never come back through a restore path.

Regression for 2026-08-29: /altguard-release dropped Quarantine but left
Almost Verified on six members — the roster-snapshot fallback in
rejoin_roles.last_known_role_ids still listed it (they were quarantined when
the snapshot was taken), it carries no permissions so the safety filter let it
through, and _release re-added it in the same bulk edit that was meant to
remove it. sync_almost_verified revoked it up to five minutes later.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="almostrestore")
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_TMP, "security_config.db")
os.environ.setdefault("ALTGUARD_GUILD_ID", "111")
os.environ["ALTGUARD_ALMOST_ROLE_ID"] = "7777"

import quarantine_store as qstore  # noqa: E402

qstore._PATH = os.path.join(_TMP, "altguard_quarantine.db")
qstore.init()

import cogs.altguard as ag  # noqa: E402
import rejoin_roles  # noqa: E402


class AlmostVerifiedNeverRestored(unittest.TestCase):
    def test_in_deny_set(self):
        self.assertEqual(ag.ALMOST_ROLE_ID, 7777)
        self.assertIn(7777, ag.NO_RESTORE_ROLE_IDS)

    def test_filter_drops_it_even_though_permissionless(self):
        deny = ag.NO_RESTORE_ROLE_IDS
        self.assertFalse(rejoin_roles.is_restorable(0, False, False, 7777, deny, False))
        # a neighbouring permissionless self-assign role still restores
        self.assertTrue(rejoin_roles.is_restorable(0, False, False, 7778, deny, False))


if __name__ == "__main__":
    unittest.main()
