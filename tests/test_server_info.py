"""/server-info pure helpers — no Discord, no gateway.
Run:  /opt/peepos-reclaimer/venv/bin/python tests/test_server_info.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import discord  # noqa: E402
import cogs.server_info as si  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


class M:
    def __init__(self, bot=False):
        self.bot = bot


class Ow:
    def __init__(self, view=None, connect=None):
        self.view_channel = view
        self.connect = connect


class Ch:
    """Stand-in with the one method the helper uses."""
    def __init__(self, ow):
        self._ow = ow

    def overwrites_for(self, _role):
        return self._ow


class VoiceCh(Ch, discord.VoiceChannel):
    def __init__(self, ow):
        Ch.__init__(self, ow)


# ---- count_members: apps are not people ----
check("counts humans and bots apart",
      si.count_members([M(), M(), M(bot=True)]) == (2, 1))
check("all bots -> zero humans", si.count_members([M(bot=True), M(bot=True)]) == (0, 2))
check("empty guild is (0, 0)", si.count_members([]) == (0, 0))
check("no bots -> zero bots", si.count_members([M(), M(), M()]) == (3, 0))
# the actual reason the command exists: member_count would say 2338 here
_h, _b = si.count_members([M() for _ in range(2307)] + [M(bot=True) for _ in range(31)])
check("real-shaped server splits 2338 into 2307/31", (_h, _b) == (2307, 31))

# ---- is_locked: reflects what a member actually experiences ----
check("text channel hidden from @everyone is locked",
      si.is_locked(Ch(Ow(view=False)), None) is True)
check("text channel visible is not locked",
      si.is_locked(Ch(Ow(view=True)), None) is False)
check("no overwrite set is not locked",
      si.is_locked(Ch(Ow()), None) is False)
check("voice with connect denied is locked",
      si.is_locked(VoiceCh(Ow(connect=False)), None) is True)
check("voice visible+connectable is not locked",
      si.is_locked(VoiceCh(Ow(view=True, connect=True)), None) is False)
check("voice hidden entirely is locked",
      si.is_locked(VoiceCh(Ow(view=False)), None) is True)

# ---- vocabulary tables are complete and sane ----
check("every verification level is named",
      all(lvl in si.VERIFICATION for lvl in discord.VerificationLevel))
check("every content filter is named",
      all(f in si.CONTENT_FILTER for f in discord.ContentFilter))
check("feature map has no empty labels", all(v for v in si.FEATURES.values()))
check("feature keys are raw discord flags (upper snake)",
      all(k.isupper() for k in si.FEATURES))

print(f"\n{_total - len(_fails)}/{_total} passed")
sys.exit(1 if _fails else 0)
