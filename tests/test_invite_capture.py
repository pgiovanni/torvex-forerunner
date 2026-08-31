"""Invite-capture logic harness — exercises the pure helpers behind LinkGuard's
invite detection (extract_invite_codes / invite_spam_hit / format_age) plus a
round-trip through the invite_posts table on a throwaway db.

Grounding case (2026-08-31): a selfbot blasted discord.gg/SmM76hYNU at ~1.2
msg/s split across two channels — under anti-nuke's per-channel 12-in-7s flood
rule forever, zero mentions, never touched the honeypot. The invite link itself
was the only durable signature.

Run on any box with discord.py importable (the module imports discord at top):
    /opt/peepos-reclaimer/venv/bin/python tests/test_invite_capture.py
Exits non-zero on any failure.
"""
import datetime
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import cogs.link_guard as lg  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


ex = lg.extract_invite_codes

# ---------------------------------------------------------------- extraction
check("plain discord.gg link", ex("join now https://discord.gg/SmM76hYNU", []) == ["SmM76hYNU"])
check("scheme-less discord.gg", ex("discord.gg/abc123 hi", []) == ["abc123"])
check("discord.com/invite form", ex("https://discord.com/invite/xyz789", []) == ["xyz789"])
check("discordapp.com/invite form", ex("http://discordapp.com/invite/old1", []) == ["old1"])
check("ptb subdomain", ex("https://ptb.discord.com/invite/pt1", []) == ["pt1"])
check("case preserved (vanity codes)", ex("discord.gg/SoMeCaSe", []) == ["SoMeCaSe"])
check("trailing punctuation excluded", ex("go discord.gg/abc123.", []) == ["abc123"])
check("dedupe repeated code", ex("discord.gg/aa11 and discord.gg/aa11", []) == ["aa11"])
check("two codes, first-seen order", ex("discord.gg/one1 discord.gg/two2", []) == ["one1", "two2"])
check("masked link target found", ex("[click](https://discord.gg/hidden1)", []) == ["hidden1"])
check("percent-encoded url decoded", ex("https%3A%2F%2Fdiscord.gg%2Fenc0ded", []) == ["enc0ded"])
check("no invite in plain text", ex("we talked about discord.gg yesterday", []) == [])
check("channels link is NOT an invite", ex("https://discord.com/channels/123/456", []) == [])
check("plain message no links", ex("hello there", []) == [])

embed = {"type": "rich", "url": "https://discord.gg/embd1",
         "image": {"url": "https://cdn.discordapp.com/x.png"}}
check("invite in embed url field", ex("look:", [embed]) == ["embd1"])
check("empty content + no embeds", ex("", []) == [])

# ------------------------------------------------------------- spam window
hit = lg.invite_spam_hit
t = []
check("1st post no trip", hit(t, 100.0, 3, 60) is False)
check("2nd post no trip", hit(t, 110.0, 3, 60) is False)
check("3rd post inside window trips", hit(t, 120.0, 3, 60) is True)

t = []
hit(t, 100.0, 3, 60)
hit(t, 130.0, 3, 60)
check("3rd post with 1st aged out: no trip", hit(t, 165.0, 3, 60) is False)
check("window pruning keeps list bounded", len(t) == 2)

# the real 8/31 burst shape: ~1.2 msg/s → 3rd message lands ~2s in
t = []
trip_at = None
ts = 0.0
for i in range(21):
    ts = i * 0.85
    if hit(t, ts, 3, 60) and trip_at is None:
        trip_at = ts
check("8/31 burst trips on 3rd message (~2s in)", trip_at is not None and trip_at < 2.0)

# ---------------------------------------------------------------- format_age
fa = lg.format_age
check("hours for <1d", fa(datetime.timedelta(hours=7)) == "7h")
check("days for <60d", fa(datetime.timedelta(days=12)) == "12 days")
check("years beyond", fa(datetime.timedelta(days=880)) == "2.4 years")

# ------------------------------------------------- invite_posts round-trip
with tempfile.TemporaryDirectory() as td:
    real = lg._TRIPDB
    lg._TRIPDB = os.path.join(td, "t.db")
    try:
        lg._init_tripdb()
        lg._persist_invite_post({
            "ts": 1.0, "guild_id": "1", "user_id": "9", "username": "spammer",
            "channel_id": "5", "message_id": "6", "code": "SmM76hYNU",
            "target_guild_id": "777", "target_guild_name": "Free Nitro",
            "member_count": 12345, "is_foreign": 1})
        lg._persist_invite_post({
            "ts": 2.0, "guild_id": "1", "user_id": "9", "username": "spammer",
            "channel_id": "5", "message_id": "7", "code": "ownGuild1",
            "target_guild_id": "1", "target_guild_name": "Home",
            "member_count": 10, "is_foreign": 0})
        check("per-user count", lg._count_user_invite_posts("1", "9") == 2)
        check("guild stats (total, foreign)", lg._guild_invite_stats("1") == (2, 1))
        check("unknown user counts 0", lg._count_user_invite_posts("1", "404") == 0)
    finally:
        lg._TRIPDB = real

print(f"\n{_total - len(_fails)}/{_total} passed")
sys.exit(1 if _fails else 0)
