"""Maintenance-window harness — pure logic, no discord import, runs anywhere.

    python3 tests/test_antinuke_window.py

What this is actually guarding. A maintenance window is the one feature here
whose entire job is to make the bot LESS protective, so every test below is
written against the direction of failure rather than the happy path:

  * it must never raise a destructive vector,
  * it must never apply to anyone but its target,
  * it must never outlive its clock,
  * and it must never make limits STRICTER than it found them.

A window that fails to open is an annoyance. A window that fails to close, or
that quietly widens channel_delete, is how a server gets taken apart with the
audit log saying it was authorised.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from utils import antinuke_window as awin  # noqa: E402

_fails, _total = [], 0
T0 = 1_000_000.0
BASE = {
    "channel_delete": (3, 12), "channel_create": (4, 12),
    "role_delete": (3, 12), "role_create": (4, 12),
    "ban": (5, 20), "kick": (5, 20), "webhook": (4, 12),
    "member_role": (5, 15), "role_remove": (5, 15),
}


def check(name, cond):
    global _total
    _total += 1
    if not cond:
        _fails.append(name)
        print(f"  FAIL  {name}")
    else:
        print(f"  ok    {name}")


def win(uid="777", minutes=30, now=T0):
    return awin.open_window(uid, "someone", minutes, "1", "opener", "bulk roles", now=now)


print("— duration clamping —")
check("default when minutes is junk", win(minutes="abc")["minutes"] == awin.DEFAULT_MINUTES)
check("clamped up to MIN", win(minutes=1)["minutes"] == awin.MIN_MINUTES)
check("clamped down to MAX", win(minutes=99999)["minutes"] == awin.MAX_MINUTES)
check("expires_at follows minutes", win(minutes=30)["expires_at"] == T0 + 1800)
_long = awin.open_window("1", "u", 30, "2", "o", "x" * 500, now=T0)
check("reason truncated", len(_long["reason"]) == 200)
check("uid stored as str", win()["uid"] == "777")

print("\n— the clock is the only truth —")
w = win(minutes=30)
check("active at open", awin.is_active(w, T0))
check("active one second before expiry", awin.is_active(w, T0 + 1799))
check("DEAD at expiry", not awin.is_active(w, T0 + 1800))
check("DEAD long after", not awin.is_active(w, T0 + 999_999))
check("remaining floors at 0", awin.remaining(w, T0 + 999_999) == 0)
check("garbage record is never active", not awin.is_active({"expires_at": "soon"}))
check("None is never active", not awin.is_active(None))
check("non-dict is never active", not awin.is_active("open"))
check("missing expires_at is never active", not awin.is_active({"uid": "777"}))

print("\n— scoped to one person —")
check("applies to its target", awin.applies_to(w, 777, T0))
check("target as str matches too", awin.applies_to(w, "777", T0))
check("does NOT apply to anyone else", not awin.applies_to(w, 778, T0))
check("does not apply once expired", not awin.applies_to(w, 777, T0 + 1800))
check("does not apply to None actor", not awin.applies_to(w, None, T0))

print("\n— what it may and may not raise —")
eff = awin.effective_limits(BASE, w, 777, T0)
check("member_role raised", eff["member_role"] == (200, 15))
check("role_remove raised", eff["role_remove"] == (200, 15))
check("role_create raised", eff["role_create"] == (25, 12))
for v in ("channel_delete", "channel_create", "role_delete", "ban", "kick", "webhook"):
    check(f"{v} UNTOUCHED", eff[v] == BASE[v])
check("ELEVATED contains no destructive vector",
      not ({"channel_delete", "channel_create", "role_delete", "ban", "kick", "webhook"}
           & set(awin.ELEVATED)))

print("\n— passthrough when nothing applies —")
check("no window -> base object itself", awin.effective_limits(BASE, None, 777, T0) is BASE)
check("wrong actor -> base object itself", awin.effective_limits(BASE, w, 778, T0) is BASE)
check("expired -> base object itself", awin.effective_limits(BASE, w, 777, T0 + 1800) is BASE)
check("base not mutated", BASE["member_role"] == (5, 15))

print("\n— a window may only ever LOOSEN —")
generous = dict(BASE, member_role=(500, 15))
eff2 = awin.effective_limits(generous, w, 777, T0)
check("already-higher count is kept", eff2["member_role"] == (500, 15))
generous_rate = dict(BASE, role_create=(100, 12))
check("higher rate is kept",
      awin.effective_limits(generous_rate, w, 777, T0)["role_create"] == (100, 12))
stingy = dict(BASE, member_role=(2, 60))
check("lower rate is raised",
      awin.effective_limits(stingy, w, 777, T0)["member_role"] == (200, 15))
check("a vector absent from base is not invented",
      "role_create" not in awin.effective_limits(
          {"member_role": (5, 15)}, w, 777, T0))

print("\n— early close —")
c = awin.close_window(w, "mrdudebro1", now=T0 + 60)
check("closed window is inactive immediately", not awin.is_active(c, T0 + 60))
check("closer recorded", c["closed_early_by"] == "mrdudebro1")
check("original left alone", awin.is_active(w, T0 + 60))
check("close of None is None", awin.close_window(None) is None)

print("\n— announcement bookkeeping —")
check("fresh window wants an open notice", awin.needs_open_notice(w, T0))
check("already-announced does not", not awin.needs_open_notice(dict(w, announced_open=1), T0))
check("active window wants no close notice", not awin.needs_close_notice(w, T0))
check("expired window wants a close notice", awin.needs_close_notice(w, T0 + 1800))
check("expired+announced wants none",
      not awin.needs_close_notice(dict(w, announced_close=1), T0 + 1800))
check("not reapable while active", not awin.is_reapable(w, T0))
check("not reapable before close notice", not awin.is_reapable(w, T0 + 999_999))
check("reapable after notice + linger",
      awin.is_reapable(dict(w, announced_close=1), T0 + 1800 + awin.LINGER_SECONDS + 1))
check("not reapable during linger",
      not awin.is_reapable(dict(w, announced_close=1), T0 + 1801))

print("\n— describe —")
check("describe handles None", awin.describe(None).startswith("No maintenance"))
check("describe says open", "Open for" in awin.describe(w, T0))
check("describe says expired", "Expired" in awin.describe(w, T0 + 1800))
check("describe names an early closer", "Closed early" in awin.describe(c, T0 + 60))

print(f"\n{_total - len(_fails)}/{_total} passed")
if _fails:
    print("FAILED: " + ", ".join(_fails))
    sys.exit(1)
