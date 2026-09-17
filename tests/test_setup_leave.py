"""Guards on /setup leave-server — the operator gate and the home-guild
refusal in cogs/setup.py. Pure functions only, no live Discord.

Why this has its own harness: leaving a guild is the one bot action that
cannot be undone from the operator's side. Getting back in needs somebody
with Manage Server in the server they just left, which is usually the
person they walked away from. So the refusals matter more than the action.

Run on any box with discord.py importable:
    /opt/peepos-reclaimer/venv/bin/python tests/test_setup_leave.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from cogs.setup import can_leave, parse_guild_allowlist  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


HOME = 1215140346800119868          # Peepo's Redemption
PRV = 1540801934506594374           # somebody else's server
OPS = {HOME}

# ── the happy path ───────────────────────────────────────────────────────
ok, why = can_leave(PRV, HOME, OPS)
check("operator in their own guild may leave someone else's", ok and why == "")

# ── the operator gate ────────────────────────────────────────────────────
ok, why = can_leave(HOME, PRV, OPS)
check("run from a NON-operator guild refuses", not ok and why == "operator")
ok, why = can_leave(PRV, PRV, OPS)
check("an admin of the target can't aim it at itself from there",
      not ok and why == "operator")
ok, why = can_leave(PRV, HOME, set())
check("empty allowlist = nobody, not everybody", not ok and why == "operator")

# ── the home guard, which catches the operator too ───────────────────────
ok, why = can_leave(HOME, HOME, OPS)
check("can't evict the bot from the guild you're standing in", not ok and why == "home")
ok, why = can_leave(HOME, HOME, {HOME, PRV})
check("every operator guild is protected, not just the one in use",
      not ok and why == "home")
ok, why = can_leave(PRV, HOME, {HOME, PRV})
check("a guild listed as the operator's own is never a target",
      not ok and why == "home")

# ── junk input ───────────────────────────────────────────────────────────
ok, why = can_leave(0, HOME, OPS)
check("unparseable server id refuses instead of guessing", not ok and why == "unknown")

# ── allowlist parsing (same contract as the other operator tools) ────────
check("commas and spaces both parse",
      parse_guild_allowlist("111, 222  333") == {111, 222, 333})
check("falls back to the next value when the first is unset",
      parse_guild_allowlist(None, "", "444") == {444})
check("no env anywhere = nobody", parse_guild_allowlist(None, "") == set())
check("junk entries are dropped, not crashed on",
      parse_guild_allowlist("111, not-an-id, 222") == {111, 222})

print(f"\n{_total - len(_fails)}/{_total} passed")
if _fails:
    sys.exit(1)
