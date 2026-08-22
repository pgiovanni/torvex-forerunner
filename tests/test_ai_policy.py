"""Per-server AI usage policy + the precheck gate.

The gate (cogs/ai.AI._precheck) had no tests before 2026-08-22, which is how
two wrong-party charges shipped: the HOME monthly cap counted every server's
usage (a paying customer could be told "back on the 1st"), and the bucks tier
fired outside the home community (members asked for bucks that don't exist
there, or charged bucks AND their server's credit for one answer). Every case
below pins which pool, which mode, and which party pays.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.ai_meter import (  # noqa: E402
    policy_from_config, remaining_energy, energy_usd, DAILY_FREE_ENERGY,
    DEFAULT_PAID_ASK_CAP, ENERGY_MIN, ENERGY_MAX, PAID_CAP_MAX, MICRO_PER_ENERGY,
)


class TestPolicyFromConfig(unittest.TestCase):
    def test_defaults(self):
        p = policy_from_config({}, is_home=False)
        self.assertEqual(p.mode, "energy")
        self.assertEqual(p.daily_energy, DAILY_FREE_ENERGY)
        self.assertFalse(p.bucks_enabled)
        self.assertEqual(p.paid_cap, 0)

    def test_home_has_bucks_and_cap(self):
        p = policy_from_config({}, is_home=True)
        self.assertTrue(p.bucks_enabled)
        self.assertEqual(p.paid_cap, DEFAULT_PAID_ASK_CAP)

    def test_unlimited_is_paid_servers_only(self):
        self.assertTrue(policy_from_config({"ai_mode": "unlimited"}, is_home=False).unlimited)
        self.assertFalse(policy_from_config({"ai_mode": "unlimited"}, is_home=True).unlimited)

    def test_garbage_falls_back_and_clamps(self):
        p = policy_from_config({"ai_mode": "yolo", "ai_daily_energy": "lots",
                                "ai_max_paid_asks": -5}, is_home=True)
        self.assertEqual(p.mode, "energy")
        self.assertEqual(p.daily_energy, DAILY_FREE_ENERGY)
        self.assertEqual(p.paid_cap, 0)
        p = policy_from_config({"ai_daily_energy": 10 ** 9, "ai_max_paid_asks": 10 ** 9},
                               is_home=True)
        self.assertEqual(p.daily_energy, ENERGY_MAX)
        self.assertEqual(p.paid_cap, PAID_CAP_MAX)
        self.assertEqual(policy_from_config({"ai_daily_energy": 1}, False).daily_energy,
                         ENERGY_MIN)

    def test_bucks_never_come_from_config(self):
        # a stray key can't switch on an economy the server doesn't have
        p = policy_from_config({"ai_max_paid_asks": 50, "bucks_enabled": 1}, is_home=False)
        self.assertFalse(p.bucks_enabled)
        self.assertEqual(p.paid_cap, 0)

    def test_remaining_energy_uses_allowance(self):
        self.assertEqual(remaining_energy(0, 150), 150)
        self.assertEqual(remaining_energy(150 * MICRO_PER_ENERGY, 150), 0)
        self.assertEqual(remaining_energy(0), DAILY_FREE_ENERGY)

    def test_energy_usd(self):
        self.assertAlmostEqual(energy_usd(100), 0.06)


# ── the gate ─────────────────────────────────────────────────────────────────

class FakeUser:
    id = 42


class FakeCog:
    """Just enough of cogs.ai.AI for _precheck: every data source is a knob."""

    def __init__(self, *, is_home, month_spent=0, credit=10_000_000,
                 spent_today=0, paid_asks=0, bucks_ok=True, cfg=None):
        from cogs import ai as ai_mod
        self._ai = ai_mod
        self.gid = ai_mod.HOME_GUILD_ID if is_home else 999
        self._month = month_spent
        self._credit = credit
        self._spent = spent_today
        self._paid = paid_asks
        self._bucks_ok = bucks_ok
        self._cfg = cfg or {}
        self.debits = []

    # data sources
    def _policy(self, gid):
        return policy_from_config(self._cfg, is_home=(int(gid) == self._ai.HOME_GUILD_ID))

    def _month_spent(self, gid=None):
        return self._month

    def _credit_balance(self, gid):
        return self._credit

    def _user_spent_today(self, uid, gid):
        return self._spent

    def _paid_asks_today(self, uid, gid):
        return self._paid

    async def _debit_bucks(self, user, amount):
        self.debits.append(amount)
        return 100 if self._bucks_ok else None

    def run(self, tier="smart"):
        return asyncio.run(self._ai.AI._precheck(self, FakeUser(), tier, self.gid))


ENERGY_GONE = (DAILY_FREE_ENERGY + 1) * MICRO_PER_ENERGY


class TestPrecheck(unittest.TestCase):
    def test_paid_server_ignores_home_monthly_cap(self):
        # the B1 regression: global month over budget, customer has credit → go
        cog = FakeCog(is_home=False, month_spent=10 ** 9, credit=5_000_000)
        self.assertEqual(cog.run(), (0, None))

    def test_home_stops_on_home_budget(self):
        cog = FakeCog(is_home=True, month_spent=10 ** 9)
        charged, err = cog.run()
        self.assertEqual(charged, 0)
        self.assertIn("home community", err)

    def test_paid_server_stops_when_credit_gone(self):
        cog = FakeCog(is_home=False, credit=0, cfg={"ai_mode": "unlimited"})
        charged, err = cog.run()
        self.assertEqual(charged, 0)
        self.assertIn("prepaid AI credit is used up", err)

    def test_paid_energy_mode_never_debits_bucks(self):
        # the B2 regression: over allowance, has bucks → stop, and NO debit
        cog = FakeCog(is_home=False, spent_today=ENERGY_GONE, bucks_ok=True)
        charged, err = cog.run()
        self.assertEqual(charged, 0)
        self.assertIn("out of AI energy in this server", err)
        self.assertEqual(cog.debits, [])

    def test_paid_unlimited_goes_past_allowance(self):
        cog = FakeCog(is_home=False, spent_today=ENERGY_GONE, cfg={"ai_mode": "unlimited"})
        self.assertEqual(cog.run(), (0, None))
        self.assertEqual(cog.debits, [])

    def test_paid_server_allowance_is_its_own(self):
        cog = FakeCog(is_home=False, spent_today=120 * MICRO_PER_ENERGY,
                      cfg={"ai_daily_energy": 150})
        self.assertEqual(cog.run(), (0, None))

    def test_home_under_allowance_is_free(self):
        cog = FakeCog(is_home=True, spent_today=0)
        self.assertEqual(cog.run(), (0, None))
        self.assertEqual(cog.debits, [])

    def test_home_over_allowance_charges_bucks(self):
        from utils.ai_meter import BUCKS_PRICE
        cog = FakeCog(is_home=True, spent_today=ENERGY_GONE, paid_asks=DEFAULT_PAID_ASK_CAP - 1)
        self.assertEqual(cog.run("quick"), (BUCKS_PRICE["quick"], None))
        self.assertEqual(cog.debits, [BUCKS_PRICE["quick"]])

    def test_home_daily_paid_cap_stops_before_debit(self):
        cog = FakeCog(is_home=True, spent_today=ENERGY_GONE, paid_asks=DEFAULT_PAID_ASK_CAP)
        charged, err = cog.run()
        self.assertEqual(charged, 0)
        self.assertIn("paid asks", err)
        self.assertEqual(cog.debits, [])

    def test_home_paid_tier_off(self):
        cog = FakeCog(is_home=True, spent_today=ENERGY_GONE, cfg={"ai_max_paid_asks": 0})
        charged, err = cog.run()
        self.assertEqual(charged, 0)
        self.assertIn("out of AI energy in this server", err)
        self.assertEqual(cog.debits, [])

    def test_home_broke_member(self):
        cog = FakeCog(is_home=True, spent_today=ENERGY_GONE, bucks_ok=False)
        charged, err = cog.run()
        self.assertEqual(charged, 0)
        self.assertIn("don't have enough bucks", err)


if __name__ == "__main__":
    unittest.main()
