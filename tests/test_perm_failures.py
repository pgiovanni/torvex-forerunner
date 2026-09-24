"""perm_failures — the ledger + wording, against a throwaway sqlite file.
No live Discord. Run:
    py -X utf8 tests/test_perm_failures.py
Exits non-zero on any failure.
"""
import os
import sys
import tempfile
import time
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp()
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_tmp, "cfg.db")   # before the import

from utils import perm_failures as pf  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    if not cond:
        _fails.append(name)
        print(f"  FAIL  {name}")


class Perms:
    def __init__(self, **kw):
        self.administrator = kw.pop("administrator", False)
        for attr, _ in pf.NEEDED:
            setattr(self, attr, kw.get(attr, True))


class Chan:
    def __init__(self, cid, name, perms):
        self.id, self.name, self._p = cid, name, perms
        self.mention = f"<#{cid}>"

    def permissions_for(self, me):
        return self._p


class Guild:
    def __init__(self, gid, channels):
        self.id, self.name, self.me = gid, "G", object()
        self._c = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._c.get(cid)


# ── missing_perms: most basic first, admin short-circuits ──────────────────
c_hidden = Chan(1, "hidden", Perms(view_channel=False, send_messages=False))
c_mute = Chan(2, "mute", Perms(send_messages=False, attach_files=False))
c_ok = Chan(3, "ok", Perms())
c_admin = Chan(4, "admin", Perms(view_channel=False, administrator=True))
me = object()
check("hidden lists view first", pf.missing_perms(c_hidden, me) == ["View Channel", "Send Messages"])
check("mute lists send + attach", pf.missing_perms(c_mute, me) == ["Send Messages", "Attach Files"])
check("ok is empty", pf.missing_perms(c_ok, me) == [])
check("admin overrides everything", pf.missing_perms(c_admin, me) == [])
check("None channel is empty", pf.missing_perms(None, me) == [])

# ── explain: says what, where, and which permission ────────────────────────
s = pf.explain("post the voice log", c_mute, ["Send Messages"])
check("explain names the action", "post the voice log" in s)
check("explain names the channel", "<#2>" in s)
check("explain names the permission", "**Send Messages**" in s)
s = pf.explain("post the voice log", c_ok, [], code=50001)
check("no visible missing perm → private-channel advice", "private" in s and "View Channel" in s)
s = pf.explain("post the voice log", 12345, [], code=10003)
check("unknown channel → no longer exists", "no longer exists" in s and "`12345`" in s)
s = pf.explain("run rule “x” (react)", c_ok, [], code=50013)
check("50013 → overrides advice", "overrides" in s)
check("never the bare Discord phrase alone", "Missing Access" not in pf.explain("x", c_mute, ["Send Messages"]))

# ── note/recent/ok: one row per (guild, channel, what), count climbs ───────
g = Guild(10, [c_hidden, c_mute, c_ok])
exc = types.SimpleNamespace(code=50001)
d1 = pf.note(g, c_hidden, "post the voice log", exc)
d2 = pf.note(g, c_hidden, "post the voice log", exc)
pf.note(g, c_hidden, "post the join log", exc)
rows = pf.recent(10)
check("two whats → two rows", len(rows) == 2)
voice = [r for r in rows if r["what"] == "post the voice log"][0]
check("count climbs on repeat", voice["count"] == 2)
check("detail stored = explanation", voice["detail"] == d2 == d1)
check("missing stored as json", '"View Channel"' in voice["missing"])
check("code stored", voice["code"] == 50001)
check("other guild sees nothing", pf.recent(11) == [])
pf.ok(10, 1, "post the voice log")
check("ok() clears the row", len(pf.recent(10)) == 1)
pf.ok(10, 1, "never recorded")                          # must not raise
check("note on a bare id doesn't crash", "no longer exists" in pf.note(g, 999, "find the voice log channel"))

# ── notify throttle: once per channel per day, whatever the what ───────────
now = time.time()
check("first claim wins", pf._claim_notify("10", "1", now))
check("second claim same channel loses", not pf._claim_notify("10", "1", now + 5))
check("a day later it fires again", pf._claim_notify("10", "1", now + pf.NOTIFY_EVERY + 1))
check("other channel is independent", pf._claim_notify("10", "2", now))

# ── audit: configured keys only, problems worded ───────────────────────────
cfg = {"msglog_channel_id": "1", "msglog_voice_channel_id": 2, "welcome_channel_id": "3",
        "race_channel_id": "404", "modlog_channel_id": None, "verify_channel_id": "x"}
rows = {label: (cid, prob) for label, _k, cid, prob in pf.audit(g, cfg)}
check("unset keys skipped", "Mod log" not in rows)
check("hidden channel → both perms named", "View Channel + Send Messages" in rows["Message log"][1])
check("int id works", "Send Messages" in rows["Voice log"][1])
check("healthy channel → None", rows["Welcome messages"][1] is None)
check("missing channel → wording", "no longer exists" in rows["Last to survive"][1])
check("garbage id → re-pick", "isn't a channel id" in rows["Verify panel"][1])
check("order follows CONFIGURED", list(rows)[0] == "Message log")

print(f"{_total - len(_fails)}/{_total} checks passed")
sys.exit(1 if _fails else 0)
