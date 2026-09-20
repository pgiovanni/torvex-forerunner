"""/userinfo — the pure builders behind the card.

The failure that matters here is silent and total: a Roles field over 1024
characters is a 400 on the whole embed, which reaches the member as "the
application did not respond". So `role_field` is measured against the cap with
the "+N more" tail charged for, and these are its unit tests.

Run on any box with discord.py importable:
    /opt/peepos-reclaimer/venv/bin/python tests/test_user_info.py
Exits non-zero on any failure.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.user_info as ui  # noqa: E402

_fails = []
_total = 0


def check(name, got, want):
    global _total
    _total += 1
    if got != want:
        _fails.append(f"{name}: got {got!r}, want {want!r}")


def ok(name, cond):
    global _total
    _total += 1
    if not cond:
        _fails.append(f"{name}: expected true")


class _Role:
    def __init__(self, rid, name, position, default=False):
        self.id = rid
        self.name = name
        self.position = position
        self._default = default

    def is_default(self):
        return self._default

    @property
    def mention(self):
        return f"<@&{self.id}>"


class _Colour:
    def __init__(self, value):
        self.value = value


class _Asset:
    def __init__(self, url):
        self.url = url


class _Flags:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, _):
        return False


class _Guild:
    def __init__(self, members=(), member_count=None):
        self.id = 1215140346800119868
        self.members = list(members)
        self.member_count = member_count if member_count is not None else len(members)


class _User:
    def __init__(self, uid=596446208021626938, name="mrdudebro1", bot=False,
                 created=None, flags=None, banner=None):
        self.id = uid
        self.name = name
        self.global_name = None
        self.bot = bot
        self.created_at = created or datetime(2019, 7, 4, 21, 4, tzinfo=timezone.utc)
        self.public_flags = flags or _Flags()
        self.banner = banner
        self.display_avatar = _Asset("https://cdn.discordapp.com/avatars/1/a.png")

    @property
    def mention(self):
        return f"<@{self.id}>"


class _Member(_User):
    def __init__(self, roles=(), joined=None, premium=None, colour=0,
                 timed_out=False, pending=False, guild=None, **kw):
        super().__init__(**kw)
        self.display_name = "mrdudebro1"
        self.roles = list(roles)
        self.joined_at = joined or datetime(2024, 3, 6, 22, 33, tzinfo=timezone.utc)
        self.premium_since = premium
        self.colour = _Colour(colour)
        self.timed_out_until = (datetime.now(timezone.utc) + timedelta(hours=2)) if timed_out else None
        self._timed_out = timed_out
        self.pending = pending
        self.voice = None
        self.guild = guild or _Guild()
        self.guild_avatar = None

    def is_timed_out(self):
        return self._timed_out


class _Perms:
    def __init__(self, **kw):
        self._on = kw

    def __getattr__(self, name):
        return self._on.get(name, False)

    @property
    def administrator(self):
        return self._on.get("administrator", False)


# ── ordinal ───────────────────────────────────────────────────────────────────
check("ordinal 1", ui.ordinal(1), "1st")
check("ordinal 2", ui.ordinal(2), "2nd")
check("ordinal 3", ui.ordinal(3), "3rd")
check("ordinal 4", ui.ordinal(4), "4th")
check("ordinal 11 (not 11st)", ui.ordinal(11), "11th")
check("ordinal 12 (not 12nd)", ui.ordinal(12), "12th")
check("ordinal 13 (not 13rd)", ui.ordinal(13), "13th")
check("ordinal 21", ui.ordinal(21), "21st")
check("ordinal 111", ui.ordinal(111), "111th")
check("ordinal 1042 thousands separator", ui.ordinal(1042), "1,042nd")

# ── stamp / when ──────────────────────────────────────────────────────────────
dt = datetime(2024, 3, 6, 22, 33, tzinfo=timezone.utc)
check("stamp D", ui.stamp(dt, "D"), f"<t:{int(dt.timestamp())}:D>")
check("stamp None", ui.stamp(None), None)
check("naive treated as utc",
      ui.stamp(datetime(2024, 3, 6, 22, 33)), ui.stamp(dt))
check("when has both lines", ui.when(dt).count("\n"), 1)
check("when None", ui.when(None), "—")

# ── role_field ────────────────────────────────────────────────────────────────
everyone = _Role(1, "@everyone", 0, default=True)
check("no roles", ui.role_field(_Member(roles=[everyone])), ("—", 0, 0))

three = [everyone, _Role(10, "a", 1), _Role(11, "b", 2), _Role(12, "c", 3)]
text, shown, total = ui.role_field(_Member(roles=three))
check("three roles total", total, 3)
check("three roles shown", shown, 3)
check("highest first", text, "<@&12> <@&11> <@&10>")
ok("no tail when all fit", "more" not in text)

# 26 roles, the count on Paul's reference card — comfortably under the cap
many = [everyone] + [_Role(100000000000000000 + i, f"r{i}", i) for i in range(26)]
text, shown, total = ui.role_field(_Member(roles=many))
check("26 roles all shown", (shown, total), (26, 26))
ok("26 roles fit the cap", len(text) <= ui.MAX_FIELD)

# 80 roles cannot fit: the field must still be legal and must say how many
huge = [everyone] + [_Role(100000000000000000 + i, f"r{i}", i) for i in range(80)]
text, shown, total = ui.role_field(_Member(roles=huge))
check("80 roles total", total, 80)
ok("truncated field is legal", len(text) <= ui.MAX_FIELD)
ok("truncated field admits it", text.endswith(f"+{80 - shown} more"))
ok("something was shown", 0 < shown < 80)

# The tail must be charged for BEFORE a mention is accepted: at every cap from
# 20 to 400 the rendered field has to stay legal, tail included.
worst = 0
for cap in range(20, 401, 7):
    t, s, n = ui.role_field(_Member(roles=huge), cap=cap)
    worst = max(worst, len(t) - cap)
ok("tail never pushes past the cap at any width", worst <= 0)

# A single role longer than the whole cap degrades to a count, never an empty field
giant = [everyone, _Role(123456789012345678, "x" * 50, 1)]
text, shown, total = ui.role_field(_Member(roles=giant), cap=10)
check("oversized single role", (text, shown, total), ("1 roles", 0, 1))
ok("never returns an empty value", text != "")

# ── join_rank ─────────────────────────────────────────────────────────────────
m1 = _Member(uid=1, joined=datetime(2020, 1, 1, tzinfo=timezone.utc))
m2 = _Member(uid=2, joined=datetime(2021, 1, 1, tzinfo=timezone.utc))
m3 = _Member(uid=3, joined=datetime(2022, 1, 1, tzinfo=timezone.utc))
g = _Guild([m1, m2, m3])
check("first to join", ui.join_rank(g, m1), 1)
check("last to join", ui.join_rank(g, m3), 3)
check("partial cache returns nothing",
      ui.join_rank(_Guild([m1, m2, m3], member_count=900), m1), None)
m_nojoin = _Member(uid=4, joined=None)
check("no joined_at", ui.join_rank(g, m_nojoin), None)

# ── custom_id round trip ──────────────────────────────────────────────────────
cid = ui.perms_custom_id(1215140346800119868, 596446208021626938)
check("round trip", ui.parse_perms_custom_id(cid),
      (1215140346800119868, 596446208021626938))
check("foreign cog's id ignored", ui.parse_perms_custom_id("conduct:ev:12"), None)
check("rolemenu id ignored", ui.parse_perms_custom_id("rm:4:12345"), None)
check("None id", ui.parse_perms_custom_id(None), None)
check("garbage id", ui.parse_perms_custom_id("ui:perms:abc:def"), None)
check("short id", ui.parse_perms_custom_id("ui:perms:1"), None)

# ── granted ───────────────────────────────────────────────────────────────────
check("admin stands alone",
      ui.granted(_Perms(administrator=True, ban_members=True, manage_roles=True)),
      ["Administrator"])
check("mod perms in card order",
      ui.granted(_Perms(ban_members=True, manage_roles=True, moderate_members=True)),
      ["Manage Roles", "Timeout Members", "Ban Members"])
check("plain member", ui.granted(_Perms()), [])

# ── badges ────────────────────────────────────────────────────────────────────
check("no badges", ui.badge_list(_User()), [])
check("two badges",
      ui.badge_list(_User(flags=_Flags(active_developer=True, early_supporter=True))),
      ["Early Supporter", "Active Developer"])
check("plumbing flags hidden",
      ui.badge_list(_User(flags=_Flags(team_user=True, bot_http_interactions=True))), [])
# Server Booster is the badge most people who have one actually have. It comes
# from premium_since, not from a flag, so it needs the member.
check("booster badge",
      ui.badge_list(_User(), _Member(premium=datetime(2024, 3, 7, tzinfo=timezone.utc))),
      ["💎 Server Booster"])
check("booster alongside a flag",
      ui.badge_list(_User(flags=_Flags(hypesquad_bravery=True)),
                    _Member(premium=datetime(2024, 3, 7, tzinfo=timezone.utc))),
      ["HypeSquad Bravery", "💎 Server Booster"])
check("non-booster member adds nothing", ui.badge_list(_User(), _Member()), [])
check("no member, no booster", ui.badge_list(_User(), None), [])

# ── build_card ────────────────────────────────────────────────────────────────
member = _Member(roles=three, colour=0xE74C3C,
                 premium=datetime(2024, 3, 7, 13, 41, tzinfo=timezone.utc))
e = build = ui.build_card(_User(), member, None, 42)
names = [f.name for f in e.fields]
# a booster earns the badge row as well as the date field — the profile shows
# both and one without the other reads as a bug
check("member card fields",
      names, ["Created", "Joined", "Boosting since", "Roles — 3", "Badges"])
check("footer carries the id", e.footer.text, "ID 596446208021626938")
check("role colour wins", e.colour.value, 0xE74C3C)
ok("join rank rendered", "42nd to join" in e.fields[1].value)
ok("mention in description", "<@596446208021626938>" in e.description)
ok("handle in description", "`@mrdudebro1`" in e.description)

# Someone who is not in the server: no Joined date, no roles, no crash
e = ui.build_card(_User(), None, None, None)
check("outsider fields", [f.name for f in e.fields], ["Created", "Joined"])
check("outsider joined value", e.fields[1].value, "Not in this server")

# An app is marked as one and takes the blurple bar
e = ui.build_card(_User(bot=True), None, None, None)
ok("app marked", "App" in e.description)
check("app colour", e.colour.value, ui.BOT_COLOR)

# Timed out / pending surface as Status
member = _Member(roles=three, timed_out=True, pending=True)
e = ui.build_card(_User(), member, None, None)
status = [f for f in e.fields if f.name == "Status"]
check("status field present", len(status), 1)
ok("timeout shown", "Timed out until" in status[0].value)
ok("pending shown", "rules screen" in status[0].value)

# Every field Discord will accept: nothing over its own cap
member = _Member(roles=huge)
e = ui.build_card(_User(flags=_Flags(staff=True, partner=True)), member, None, 1)
for f in e.fields:
    ok(f"field {f.name!r} within 1024", len(f.value) <= 1024)
ok("embed within 6000", len(e) <= 6000)

# ── report ────────────────────────────────────────────────────────────────────
if _fails:
    print(f"FAILED {len(_fails)} of {_total}")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print(f"OK — {_total} checks passed")
