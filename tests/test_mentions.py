"""utils/mentions.py — the /mentions index + read side. Pure sqlite, runs
anywhere:  py tests/test_mentions.py   (exits non-zero on failure).

Also covers cogs/mentions.py's render_line when discord.py is importable
(VPS); skipped silently otherwise.
"""
import os
import sqlite3
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from utils import mentions as mi  # noqa: E402

_fails, _total = [], 0


def check(name, got, want):
    global _total
    _total += 1
    if got != want:
        _fails.append(f"{name}: got {got!r}, want {want!r}")


MESSAGES_SQL = """CREATE TABLE messages (
    message_id TEXT PRIMARY KEY, guild_id TEXT, channel_id TEXT,
    author_id TEXT, author_name TEXT, bot INTEGER, webhook INTEGER,
    created_ts REAL, content TEXT, reply_to TEXT, attachments TEXT, stickers TEXT,
    deleted_ts REAL, deleted_by TEXT, deleted_by_name TEXT, delete_kind TEXT,
    poll TEXT, forward TEXT)"""
EDITS_SQL = """CREATE TABLE edits (message_id TEXT, guild_id TEXT, edited_ts REAL,
    old_content TEXT, new_content TEXT)"""

G, ME, ALICE, BOT, ROLE = "1", "100", "200", "300", "900"
NOW = time.time()


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(MESSAGES_SQL)
    c.execute(EDITS_SQL)
    mi.ensure_schema(c)
    return c


def msg(c, mid, author, content, ts, reply_to=None, bot=0, deleted=None, kind=None, chan="10"):
    c.execute("INSERT INTO messages(message_id,guild_id,channel_id,author_id,author_name,bot,webhook,"
              "created_ts,content,reply_to,deleted_ts,delete_kind) VALUES (?,?,?,?,?,?,0,?,?,?,?,?)",
              (mid, G, chan, author, f"u{author}", bot, ts, content, reply_to, deleted, kind))
    return {"message_id": mid, "guild_id": G, "author_id": author, "created_ts": ts,
            "content": content, "reply_to": reply_to}


# ── mention_rows ────────────────────────────────────────────────────────────
r = {"message_id": "m1", "guild_id": G, "author_id": ALICE, "created_ts": 5.0,
     "content": f"hey <@{ME}> and <@!{ME}> and <@&{ROLE}> @everyone", "reply_to": None}
check("user+role, deduped, no everyone", mi.mention_rows(r),
      [("m1", G, ME, "user", 5.0), ("m1", G, ROLE, "role", 5.0)])
r["content"] = f"<@{ALICE}> talking to myself"
check("self-ping dropped", mi.mention_rows(r), [])
r["content"] = ""; r["reply_to"] = "m0"
check("reply resolved", mi.mention_rows(r, reply_author=ME), [("m1", G, ME, "reply", 5.0)])
check("self-reply dropped", mi.mention_rows(r, reply_author=ALICE), [])
check("unresolved reply = nothing", mi.mention_rows(r, reply_author=None), [])

# ── write + fetch ───────────────────────────────────────────────────────────
c = fresh()
rows = [
    msg(c, "p1", ME, "my own post", NOW - 500),
    msg(c, "m1", ALICE, f"<@{ME}> live one", NOW - 400),
    msg(c, "m2", ALICE, f"<@{ME}> ghost", NOW - 300, deleted=NOW - 290, kind="self"),
    msg(c, "m3", ALICE, f"<@{ME}> slur here", NOW - 200, deleted=NOW - 190, kind="mod"),
    msg(c, "m4", ALICE, "reply text", NOW - 100, reply_to="p1"),
    msg(c, "m5", BOT, f"congrats <@{ME}>", NOW - 50, bot=1),
    msg(c, "m6", ALICE, f"<@&{ROLE}> role ping", NOW - 40),
    msg(c, "m7", ALICE, f"<@{ME}> <@&{ROLE}> both", NOW - 30),
    msg(c, "m8", ALICE, "edited away", NOW - 20),
    msg(c, "m9", ME, f"<@{ALICE}> pinging alice", NOW - 10),
    msg(c, "old", ALICE, f"<@{ME}> ancient", NOW - 40 * 86400),
]
c.execute("INSERT INTO edits VALUES ('m8',?,?,?,?)", (G, NOW - 15, f"<@{ME}> original", "edited away"))
n = mi.write_mentions(c, rows, recent={})
# m1 m2 m3 m4(reply) m5(bot — indexed, filtered at read) m6(role) m7×2 m9(→alice) old
check("index rows written", n, 10)
check("rewrite is a no-op", mi.write_mentions(c, rows, recent={}), 0)
# m8's CURRENT content has no ping, so a flush wouldn't index it; in real life the
# index row is written at creation, before the edit. Plant that row.
c.execute("INSERT INTO message_mentions VALUES ('m8',?,?,'user',?)", (G, ME, NOW - 20))

got = mi.fetch_mentions(c, G, ME, [ROLE], mi.lookback_ts(30), 10)
check("default: user pings + replies, no bots/roles/self/old",
      [r["message_id"] for r in got], ["m8", "m7", "m4", "m3", "m2", "m1"])
check("m7 counted once as a user ping", [r["kind"] for r in got if r["message_id"] == "m7"], ["user"])

got = mi.fetch_mentions(c, G, ME, [ROLE], mi.lookback_ts(30), 10, include_roles=True)
check("roles on: m6 appears, m7 still once",
      [r["message_id"] for r in got], ["m8", "m7", "m6", "m4", "m3", "m2", "m1"])
got = mi.fetch_mentions(c, G, ME, [], mi.lookback_ts(30), 10, include_bots=True, include_replies=False)
check("bots on, replies off", [r["message_id"] for r in got], ["m8", "m7", "m5", "m3", "m2", "m1"])
got = mi.fetch_mentions(c, G, ME, [], mi.lookback_ts(30), 2)
check("over-fetch bounded at 4×limit, newest first", (len(got) <= 8, got[0]["message_id"]), (True, "m8"))
check("alice's view excludes what she wrote, sees ME's ping",
      [r["message_id"] for r in mi.fetch_mentions(c, G, ALICE, [], 0, 10)], ["m9"])
check("unknown guild empty", mi.fetch_mentions(c, "2", ME, [], 0, 10), [])

# ── classify ────────────────────────────────────────────────────────────────
by = {r["message_id"]: r for r in mi.fetch_mentions(c, G, ME, [], 0, 20)}
check("live", mi.classify(by["m1"], ME, c), ("live", f"<@{ME}> live one"))
check("self-delete = ghost, text shown", mi.classify(by["m2"], ME, c), ("deleted", f"<@{ME}> ghost"))
check("mod delete = removed, text withheld", mi.classify(by["m3"], ME, c), ("removed", ""))
check("bulk = removed", mi.classify(dict(by["m3"], delete_kind="bulk"), ME, c), ("removed", ""))
check("edited out → original wording", mi.classify(by["m8"], ME, c), ("edited_out", f"<@{ME}> original"))
check("reply never 'edited out'", mi.classify(by["m4"], ME, c), ("live", "reply text"))

# ── sweep + backfill ────────────────────────────────────────────────────────
check("sweep drops old", mi.sweep(c, G, NOW - 30 * 86400), 1)
c.execute("DELETE FROM message_mentions")
scanned, written = mi.backfill(c, batch=3)
check("backfill scans pinged/reply rows", scanned, 9)   # p1 + m8 carry no ping markup
check("backfill index rows", written, 10)
check("backfill idempotent", mi.backfill(c)[1], 0)

# ── snippet ─────────────────────────────────────────────────────────────────
check("snippet collapses whitespace", mi.snippet("a\n\n b   c"), "a b c")
check("snippet empty", mi.snippet(""), "*no text*")
check("snippet truncates", len(mi.snippet("x" * 300)), 120)

# ── render (VPS only) ───────────────────────────────────────────────────────
try:
    from cogs.mentions import render_line
    base = {"kind": "user", "author_id": ALICE, "channel_id": "10", "message_id": "m1",
            "created_ts": 1000.0, "deleted_ts": None, "delete_kind": None}
    live = render_line(dict(base, state="live", text="hi"), G)
    check("live has jump link", "https://discord.com/channels/1/10/m1" in live, True)
    rem = render_line(dict(base, state="removed", text="slur", delete_kind="mod"), G)
    check("removed shows no text", "slur" in rem, False)
    check("removed labelled", "removed by staff" in rem, True)
    ghost = render_line(dict(base, state="deleted", text="boo", deleted_ts=1200.0), G)
    check("ghost shows text + when", ("boo" in ghost) and ("<t:1200:R>" in ghost), True)
    rep = render_line(dict(base, state="live", text="x", kind="reply"), G)
    check("reply labelled", "replied to you" in rep, True)
except ImportError:
    pass

print(f"{_total - len(_fails)}/{_total} passed")
for f in _fails:
    print("FAIL", f)
sys.exit(1 if _fails else 0)
