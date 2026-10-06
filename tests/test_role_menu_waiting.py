"""role_menu — the 'waiting' state's pure helpers. No live Discord. Run:
    py -X utf8 tests/test_role_menu_waiting.py
Exits non-zero on any failure.
"""
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
_tmp = tempfile.mkdtemp()
os.environ["TORVEX_ROLEMENUS_DB"] = os.path.join(_tmp, "rm.db")     # before the import
os.environ["TORVEX_SECURITY_DB"] = os.path.join(_tmp, "cfg.db")

from cogs import role_menu as rm  # noqa: E402

_fails, _total = [], 0


def check(name, cond):
    global _total
    _total += 1
    if not cond:
        _fails.append(name)
        print(f"  FAIL  {name}")


class Perms:
    def __init__(self, **kw):
        self.administrator = kw.pop("administrator", False)
        for attr, _ in rm.PANEL_NEEDS:
            setattr(self, attr, kw.get(attr, True))
        self.attach_files = kw.get("attach_files", True)


class Chan:
    def __init__(self, name, perms):
        self.name, self._p = name, perms

    def permissions_for(self, me):
        return self._p


me = object()

# ── panel_blockers: only the three a panel needs, in fix order ─────────────
prv = Chan("⋆˚roles", Perms(send_messages=False, embed_links=False, attach_files=False))
check("prv case lists send + embed only (no attach)",
      rm.panel_blockers(prv, me) == ["Send Messages", "Embed Links"])
check("hidden channel is a View problem", rm.panel_blockers(None, me) == ["View Channel"])
check("all good is empty", rm.panel_blockers(Chan("ok", Perms()), me) == [])
check("admin short-circuits",
      rm.panel_blockers(Chan("x", Perms(administrator=True, send_messages=False)), me) == [])
check("view missing lists first",
      rm.panel_blockers(Chan("x", Perms(view_channel=False, send_messages=False)), me)
      == ["View Channel", "Send Messages"])

# ── blocked_text: what the creator reads on the dashboard ──────────────────
t = rm.blocked_text("⋆˚roles", ["Send Messages", "Embed Links"])
check("names the channel plainly", "#⋆˚roles" in t and "<#" not in t)
check("names the perms", "Send Messages + Embed Links" in t)
check("names the bot", "Torvex Forerunner" in t)
h = rm.blocked_text(None, ["View Channel"])
check("hidden channel wording", "can't see that channel" in h and "View Channel" in h)

# ── schema: the migration adds the two columns the dashboard reads ─────────
with rm._conn() as c:
    cols = {r[1] for r in c.execute("PRAGMA table_info(panels)")}
check("last_error column", "last_error" in cols)
check("error_ts column", "error_ts" in cols)

print(f"{_total - len(_fails)}/{_total} checks passed")
sys.exit(1 if _fails else 0)
