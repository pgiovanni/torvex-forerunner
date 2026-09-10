"""/chat-levels rankings card — utils/leaderboard.py.

Pure module (no discord import) so this runs in the LOCAL venv.

Protects two rules (Paul, 2026-09-10):
  * rank by LEVEL then XP, and SHOW the XP so ties aren't a mystery
  * Peepo Bucks 💰 appear ONLY in the home community; elsewhere the money
    column is Server Bucks 💵, and the "bucks" sort follows the same rule.

Run:
    python tests/test_leaderboard.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from utils import leaderboard as lb  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    if not cond:
        _fails.append(name)
        print(f"  FAIL  {name}")


ROW = {"discord_id": "596446208021626938", "level": 13, "xp": 8729,
       "peepo_bucks": 24143, "regular_bucks": 1190}

# ── ordering ──────────────────────────────────────────────────────────────
check("local xp sort = level then xp",
      lb.order_clause(False, True, local=True) == "g.level DESC, g.xp DESC")
check("global xp sort = level then xp",
      lb.order_clause(False, False, local=False) == "level DESC, xp DESC")
check("xp sort ignores home flag",
      lb.order_clause(False, False, local=True) == lb.order_clause(False, True, local=True))
check("bucks sort at home = peepo",
      lb.order_clause(True, True, local=True).startswith("peepo_bucks DESC"))
check("bucks sort elsewhere = server bucks",
      lb.order_clause(True, False, local=True).startswith("regular_bucks DESC"))
check("global bucks sort elsewhere = server bucks",
      lb.order_clause(True, False, local=False).startswith("regular_bucks DESC"))
check("every clause tiebreaks on xp",
      all("xp DESC" in lb.order_clause(b, h, local=l)
          for b in (True, False) for h in (True, False) for l in (True, False)))

# ── row formatting ────────────────────────────────────────────────────────
home = lb.format_line(0, ROW, True)
away = lb.format_line(3, ROW, False)
check("home row shows peepo bucks", "24,143 💰" in home)
check("home row shows server bucks too", "1,190 💵" in home)
check("away row hides peepo bucks", "💰" not in away and "24,143" not in away)
check("away row shows server bucks", "1,190 💵" in away)
check("xp shown on every row", "8,729 XP" in home and "8,729 XP" in away)
check("level shown", "Lv.13" in home)
check("medal for top 3", home.startswith("🥇"))
check("number past top 3", away.startswith("4."))
check("mention by id", "<@596446208021626938>" in away)

# ── title suffix ──────────────────────────────────────────────────────────
check("home label", lb.bucks_label(True) == "Peepo Bucks 💰")
check("away label", lb.bucks_label(False) == "Server Bucks 💵")

print(f"{_total - len(_fails)}/{_total} passed")
sys.exit(1 if _fails else 0)
