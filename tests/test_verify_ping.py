"""Per-guild verify-channel greeting (cogs/altguard._verify_ping_wanted).

The setting decides whether a held member is @-pinged in the verify channel.
Getting it wrong in the quiet direction is the dangerous one: a server that
never chose to turn it off, or a closed-DM joiner under "dm_failed", must still
be told how to get out — otherwise they sit held with no prompt at all until the
prune clock removes them.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Point the config store at a scratch file BEFORE altguard imports it.
_TMP = tempfile.mkdtemp(prefix="verifyping")
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_TMP, "security_config.db")
os.environ.setdefault("ALTGUARD_GUILD_ID", "111")

from utils import security_config as sc  # noqa: E402
from cogs.altguard import _verify_ping_wanted  # noqa: E402


class VerifyPingTests(unittest.TestCase):
    def setUp(self):
        sc._cache.clear()

    def _mode(self, gid, mode):
        sc.set_config(gid, verify_ping=mode)
        sc._cache.clear()

    def test_default_is_always(self):
        """A guild that has never opened the page keeps the old behaviour —
        turning the greeting off must be a decision someone made."""
        self.assertEqual(sc.DEFAULTS["verify_ping"], "always")
        self.assertTrue(_verify_ping_wanted(2001, dm_delivered=True))
        self.assertTrue(_verify_ping_wanted(2001, dm_delivered=False))

    def test_never_stays_quiet_both_ways(self):
        self._mode(2002, "never")
        self.assertFalse(_verify_ping_wanted(2002, dm_delivered=True))
        self.assertFalse(_verify_ping_wanted(2002, dm_delivered=False))

    def test_dm_failed_pings_only_the_stranded(self):
        self._mode(2003, "dm_failed")
        self.assertFalse(_verify_ping_wanted(2003, dm_delivered=True))
        self.assertTrue(_verify_ping_wanted(2003, dm_delivered=False))

    def test_garbage_falls_back_to_pinging(self):
        """An unknown/blank value must fail LOUD, not silently mute the only
        prompt a held member gets."""
        for bad in ("", None, "off", 0):
            self._mode(2004, bad)
            self.assertTrue(_verify_ping_wanted(2004, dm_delivered=True), bad)


if __name__ == "__main__":
    unittest.main()
