"""Activity-cog logic harness — pure helpers + a headless render smoke test
(Agg backend, no live Discord). Run:
    /opt/peepos-reclaimer/venv/bin/python tests/test_activity.py
Exits non-zero on any failure.
"""
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.activity as act  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc).timestamp()

# ---- fill_days ----
dates, ns = act.fill_days({"2026-07-05": 9, "2026-07-03": 4}, 5, NOW)
check("fill_days window length", len(dates) == len(ns) == 5)
check("fill_days ends today", dates[-1].isoformat() == "2026-07-05" and ns[-1] == 9)
check("fill_days zero-fills gaps", ns == [0, 0, 4, 0, 9])
check("fill_days chronological", dates[0] < dates[-1])

# ---- heatmap_matrix ----
# sqlite %w: 0=Sunday. Monday-first rows: Sunday lands on row 6, Monday row 0.
m = act.heatmap_matrix([("0", "23", 7), ("1", "0", 3)])
check("heatmap shape", len(m) == 7 and all(len(r) == 24 for r in m))
check("sunday maps to last row", m[6][23] == 7)
check("monday maps to first row", m[0][0] == 3)

# ---- short_name ----
check("discriminator stripped", act.short_name("olduser#1234") == "olduser")
check("long name ellipsized", len(act.short_name("a" * 40)) == 18)
check("none name safe", act.short_name(None) == "?")

# ---- session_seconds ----
check("plain session duration", act.session_seconds(100.0, 160.0) == 60.0)
check("session clipped to window", act.session_seconds(100.0, 160.0, cutoff=130.0) == 30.0)
check("window after session = 0", act.session_seconds(100.0, 160.0, cutoff=200.0) == 0.0)

# ---- render smoke tests (Agg — just prove a PNG comes out) ----
d, n = act.fill_days({"2026-07-05": 5}, 30, NOW)
png = act.render_daily_line(d, n, "t", "s").getvalue()
check("line renders png", png[:8] == b"\x89PNG\r\n\x1a\n")
png = act.render_leaderboard(["alpha", "beta"], [10, 3], "t", "s").getvalue()
check("bars render png", png[:8] == b"\x89PNG\r\n\x1a\n")
png = act.render_leaderboard([], [], "t", "s").getvalue()
check("empty bars still render", png[:8] == b"\x89PNG\r\n\x1a\n")
png = act.render_heatmap(act.heatmap_matrix([("3", "12", 44)]), "t", "s").getvalue()
check("heatmap renders png", png[:8] == b"\x89PNG\r\n\x1a\n")
png = act.render_growth(d, list(range(1, 31)), "t", "s").getvalue()
check("growth renders png", png[:8] == b"\x89PNG\r\n\x1a\n")

print(f"\n{_total - len(_fails)}/{_total} passed")
sys.exit(1 if _fails else 0)
