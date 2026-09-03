"""Join-role tandem harness — cogs/automation.py grants at join, cogs/altguard.py
grants on gate release; the two must never double-grant or fight over a member.

Covers join_default_ids (panel-vs-env source selection, coercion safety) and
Automation._gate_defers (the deferral handshake).

Run on any box with discord.py importable (both modules import discord at top):
    /opt/peepos-reclaimer/venv/bin/python tests/test_join_tandem.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.altguard as ag  # noqa: E402
import cogs.automation as auto  # noqa: E402

_fails = []
_total = 0


def check(name, got, want):
    global _total
    _total += 1
    if got != want:
        _fails.append(f"{name}: got {got!r}, want {want!r}")


# ── join_default_ids: who owns the join-role list ───────────────────────────
check("panel on: panel ids win",
      ag.join_default_ids({"auto_enabled": 1, "autorole_ids": ["11", "22"]}, [99]), [11, 22])
check("panel on, empty list: NO roles, not env",
      ag.join_default_ids({"auto_enabled": 1, "autorole_ids": []}, [99]), [])
check("panel on, junk ids skipped not trusted",
      ag.join_default_ids({"auto_enabled": 1, "autorole_ids": ["11", "x", None, "22"]}, [99]),
      [11, 22])
check("panel off: env fallback",
      ag.join_default_ids({"auto_enabled": 0}, [99]), [99])
check("no config keys at all: env fallback",
      ag.join_default_ids({}, [7, 8]), [7, 8])


# ── Automation._gate_defers: the deferral handshake ─────────────────────────
class _Guild:
    id = 123


class _Member:
    id = 42
    guild = _Guild()


class _AG:
    def __init__(self, holds, held):
        self._holds, self._held = holds, held

    def joins_held(self, guild):
        return self._holds

    def is_held(self, uid):
        return self._held


class _Boom:
    def joins_held(self, guild):
        raise RuntimeError("boom")

    def is_held(self, uid):
        raise RuntimeError("boom")


class _Bot:
    def __init__(self, cog):
        self._cog = cog

    def get_cog(self, name):
        return self._cog if name == "AltGuard" else None


m = _Member()
check("no AltGuard cog loaded: never defers",
      auto.Automation(_Bot(None))._gate_defers(m), False)
check("gate holds joins: defers to release",
      auto.Automation(_Bot(_AG(True, False)))._gate_defers(m), True)
check("member already quarantined: defers",
      auto.Automation(_Bot(_AG(False, True)))._gate_defers(m), True)
check("gate off + not held: grants normally",
      auto.Automation(_Bot(_AG(False, False)))._gate_defers(m), False)
check("helper blowing up never blocks grants",
      auto.Automation(_Bot(_Boom()))._gate_defers(m), False)

# ── sync_targets: who /welcome sync back-fills ──────────────────────────────
class _Role:
    pass


R = _Role()
OTHER = _Role()


class _M:
    def __init__(self, uid, roles=(), bot=False, pending=False):
        self.id, self.roles, self.bot, self.pending = uid, list(roles), bot, pending


members = [_M(1), _M(2, [R]), _M(3, bot=True), _M(4, [OTHER]), _M(5, pending=True), _M(6)]


def ids(ms):
    return [m.id for m in ms]


check("sync: humans missing the role, holders/bots skipped",
      ids(auto.sync_targets(members, R)), [1, 4, 5, 6])
check("sync: wait-for-onboarding drops pending members",
      ids(auto.sync_targets(members, R, skip_pending=True)), [1, 4, 6])
check("sync: gate-held members left to the release path",
      ids(auto.sync_targets(members, R, is_held=lambda uid: uid in (1, 6))), [4, 5])
check("sync: nothing to do is an empty list, not an error",
      auto.sync_targets([_M(2, [R]), _M(3, bot=True)], R), [])

if _fails:
    print(f"FAIL {len(_fails)}/{_total}")
    for f in _fails:
        print(" -", f)
    sys.exit(1)
print(f"OK {_total} checks")
