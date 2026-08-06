"""Level-up announcement routing — utils/level_notify.py.

Pure module by design (no discord import), so this runs in the LOCAL venv too,
unlike the cog tests.

What's being protected here: a server that turned announcements off must stay
off through every path — a missing key, a garbage value, a deleted redirect
channel — because the failure mode this feature exists to prevent is a bot
talking in a channel it was told not to talk in.

Run:
    python tests/test_level_notify.py
Exits non-zero on any failure.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from utils import level_notify as ln  # noqa: E402

_fails = []
_total = 0

ORIGIN = 111111111111111111
TARGET = 222222222222222222


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# ── 1. the default is exactly the old behaviour ──────────────────────────────
check("empty config announces in the channel they were talking in",
      ln.destination({}, ORIGIN) == ("channel", ORIGIN))
check("None config doesn't explode",
      ln.destination(None, ORIGIN) == ("channel", ORIGIN))
check("explicit 'here' is the same thing",
      ln.destination({"levels_announce": "here"}, ORIGIN) == ("channel", ORIGIN))

# ── 2. off means off ─────────────────────────────────────────────────────────
check("off announces nowhere",
      ln.destination({"levels_announce": "off"}, ORIGIN) == ("off", None))
check("off ignores a configured channel",
      ln.destination({"levels_announce": "off",
                      "levels_announce_channel_id": TARGET}, ORIGIN) == ("off", None))

# ── 3. redirect ──────────────────────────────────────────────────────────────
check("channel mode sends to the configured channel, not the origin",
      ln.destination({"levels_announce": "channel",
                      "levels_announce_channel_id": TARGET}, ORIGIN) == ("channel", TARGET))
check("channel id stored as a string still works (json/form round-trip)",
      ln.destination({"levels_announce": "channel",
                      "levels_announce_channel_id": str(TARGET)}, ORIGIN) == ("channel", TARGET))
check("redirect with no channel set goes OFF, never back to the origin channel",
      ln.destination({"levels_announce": "channel"}, ORIGIN) == ("off", None))
check("redirect to a junk channel id goes off",
      ln.destination({"levels_announce": "channel",
                      "levels_announce_channel_id": "not-an-id"}, ORIGIN) == ("off", None))
check("redirect to 0 goes off",
      ln.destination({"levels_announce": "channel",
                      "levels_announce_channel_id": 0}, ORIGIN) == ("off", None))

# ── 4. dm ────────────────────────────────────────────────────────────────────
check("dm mode targets the member",
      ln.destination({"levels_announce": "dm"}, ORIGIN) == ("dm", None))
check("dm mode doesn't also post in a channel",
      ln.destination({"levels_announce": "dm",
                      "levels_announce_channel_id": TARGET}, ORIGIN) == ("dm", None))

# ── 5. junk input degrades to the pre-existing behaviour, not to silence ─────
check("unknown mode falls back to 'here'", ln.normalise_mode("banana") == "here")
check("None mode falls back to 'here'", ln.normalise_mode(None) == "here")
check("non-string mode falls back to 'here'", ln.normalise_mode(7) == "here")
check("case and whitespace are tolerated", ln.normalise_mode("  OFF ") == "off")
check("a garbage mode still announces where they were talking",
      ln.destination({"levels_announce": "banana"}, ORIGIN) == ("channel", ORIGIN))

# ── 6. no origin channel (a levelup from somewhere with no channel id) ───────
check("no origin channel and no redirect = nothing to send to",
      ln.destination({}, None) == ("off", None))
check("no origin channel still honours a redirect",
      ln.destination({"levels_announce": "channel",
                      "levels_announce_channel_id": TARGET}, None) == ("channel", TARGET))

# ── 7. describe() — what the admin is shown back ─────────────────────────────
check("describe names the mode in plain words",
      ln.describe({"levels_announce": "off"}) == ln.MODE_LABELS["off"])
check("describe uses the channel name when we have one",
      ln.describe({"levels_announce": "channel",
                   "levels_announce_channel_id": TARGET}, "#levels") == "In #levels")
check("describe falls back to a channel mention",
      ln.describe({"levels_announce": "channel",
                   "levels_announce_channel_id": TARGET}) == f"In <#{TARGET}>")
check("describe admits when a redirect target is gone",
      "Off" in ln.describe({"levels_announce": "channel"}))

# ── 8. the two surfaces share one vocabulary ─────────────────────────────────
check("every mode has a label (slash command + dashboard read from this)",
      set(ln.MODE_LABELS) == set(ln.MODES))
check("modes are the four the config documents",
      ln.MODES == ("here", "channel", "dm", "off"))

print(f"\n{_total - len(_fails)}/{_total} passed")
if _fails:
    print("FAILED: " + ", ".join(_fails))
    sys.exit(1)
