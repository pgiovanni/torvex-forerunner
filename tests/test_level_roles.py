"""MEE6-parity leveling harness — pins the server-level curve in
cogs/economy.py to MEE6's real numbers and exercises the pure reward-role
picker in cogs/level_roles.py. No live Discord.

The curve fixtures come straight off MEE6's leaderboard API for the home
guild on 2026-07-05: mrdudebro1 = level 90, xp 1,404,133, detailed_xp
[58, 45100, 1404133] → cumulative XP to reach 90 is 1,404,075 and the
90→91 step costs 45,100. If these ever fail, imported levels won't match
what MEE6 showed.

Run on any box with discord.py importable:
    /opt/peepos-reclaimer/venv/bin/python tests/test_level_roles.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from cogs.economy import mee6_xp_for_level, mee6_level_from_xp  # noqa: E402
from cogs.level_roles import pick_reward, role_changes  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# ── curve vs MEE6's published formula (5L² + 50L + 100 per level) ────────────
check("level 0 costs nothing", mee6_xp_for_level(0) == 0)
check("level 1 = 100 xp", mee6_xp_for_level(1) == 100)
check("level 2 = 255 xp", mee6_xp_for_level(2) == 255)
check("90→91 step = 45,100 (live detailed_xp)", mee6_xp_for_level(91) - mee6_xp_for_level(90) == 45100)
check("cumulative to 90 = 1,404,075 (live detailed_xp)", mee6_xp_for_level(90) == 1404075)
check("per-level step matches 5L²+50L+100 for L=0..120",
      all(mee6_xp_for_level(l + 1) - mee6_xp_for_level(l) == 5 * l * l + 50 * l + 100 for l in range(121)))

# ── level_from_xp inverts the curve ──────────────────────────────────────────
check("0 xp = level 0", mee6_level_from_xp(0) == 0)
check("99 xp = level 0", mee6_level_from_xp(99) == 0)
check("100 xp = level 1", mee6_level_from_xp(100) == 1)
check("mrdudebro1's 1,404,133 xp = level 90", mee6_level_from_xp(1404133) == 90)
check("one xp under a threshold stays below",
      all(mee6_level_from_xp(mee6_xp_for_level(l) - 1) == l - 1 for l in range(1, 60)))
check("exactly at a threshold levels up",
      all(mee6_level_from_xp(mee6_xp_for_level(l)) == l for l in range(60)))

# ── reward-role picker (remove-old-give-new) ─────────────────────────────────
# The home guild's real MEE6 config: 10 tiers.
MAP = {1: 101, 3: 103, 5: 105, 10: 110, 15: 115, 20: 120, 25: 125, 30: 130, 40: 140, 50: 150}

check("level 0 gets no reward", pick_reward(0, MAP) is None)
check("level 1 gets Level 1+", pick_reward(1, MAP) == 101)
check("level 4 holds Level 3+ (not 5+)", pick_reward(4, MAP) == 103)
check("level 90 gets the top tier (50+)", pick_reward(90, MAP) == 150)

# fresh member levels to 10 with nothing: add 110, remove nothing
add, rem = role_changes(set(), 10, MAP)
check("bare member gets exactly the right tier", add == [110] and rem == [])

# MEE6 leftover mess: member has 3 old tiers, correct one missing
add, rem = role_changes({101, 103, 105}, 10, MAP)
check("old tiers stripped, new tier added", add == [110] and sorted(rem) == [101, 103, 105])

# already correct: no API calls at all (sync sweep must be cheap)
add, rem = role_changes({110}, 10, MAP)
check("already-correct member is a no-op", add == [] and rem == [])

# non-reward roles are never touched
add, rem = role_changes({110, 999, 42}, 12, MAP)
check("unrelated roles untouched", add == [] and rem == [])

# level dropped is impossible by policy, but the picker must still not crash
add, rem = role_changes({150}, 0, MAP)
check("level 0 with a stray top tier strips it", add == [] and rem == [150])

print(f"\n{_total - len(_fails)}/{_total} passed")
if _fails:
    sys.exit(1)
