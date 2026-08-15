#!/usr/bin/env python3
"""Fill stats.db history from the message archive + identity ledger.

Live counting started 2026-06-21, so the dashboard's charts would begin there.
The archive (messages.db, mod_log) goes back to the server's first day and the
identity ledger has every join/leave since 2024-03-10 — so for the guilds we
archive, history is a GROUP BY away instead of a week-long REST crawl.

What it writes (numbers only, same shape the live cog writes):
  message_counts  — per day/channel/user counts from messages.db, humans only
                    (bot=0 AND webhook=0), honouring stats_ignore_channels.
                    ONLY days with no existing rows are written, so live counts
                    and any REST backfill are never overwritten.
  member_daily    — joins/leaves per day from identity_events, de-duplicated
                    (Carl/MEE6/our own logger all recorded the same event
                    seconds apart), plus members_human reconstructed BACKWARD
                    from today's snapshot: members[d-1] = members[d] - joins[d]
                    + leaves[d]. members_total is left NULL for history — we
                    never knew the bot count on those days and won't pretend.

Idempotent: re-running changes nothing that's already there. Dry-run by default.

    venv/bin/python tools/backfill_stats_from_archive.py --guild 1215140346800119868 --write
"""
import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

MSG_DB = os.environ.get("TORVEX_MESSAGES_DB") or os.path.join(ROOT, "messages.db")
STATS_DB = os.environ.get("TORVEX_STATS_DB") or os.path.join(ROOT, "stats.db")

DEPARTURE_KINDS = ("leave", "kick", "ban")


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def prev_day(day: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def ignored_channels(gid: str) -> set:
    try:
        from utils.security_config import get_config
        return {str(x) for x in (get_config(int(gid)).get("stats_ignore_channels") or [])}
    except Exception as e:  # config unreachable → count everything, say so
        print(f"! could not read stats_ignore_channels ({e}); counting all channels")
        return set()


def message_rows(msg, gid, ignore, existing_days):
    """(day, guild, channel, user, n) for every day NOT already in stats.db."""
    q = msg.execute(
        """SELECT date(created_ts,'unixepoch') d, channel_id, author_id, COUNT(*) n
           FROM messages
           WHERE guild_id=? AND bot=0 AND webhook=0
           GROUP BY 1,2,3""", (gid,))
    out, skipped_days, skipped_chan = [], set(), 0
    for d, ch, uid, n in q:
        if d in existing_days:
            skipped_days.add(d)
            continue
        if ch in ignore:
            skipped_chan += n
            continue
        out.append((d, gid, ch, uid, n))
    return out, skipped_days, skipped_chan


def member_events(msg, gid):
    """joins[d], departures[d] with same-minute duplicates collapsed per uid."""
    joins, leaves = defaultdict(int), defaultdict(int)
    seen = set()
    q = msg.execute(
        "SELECT uid, kind, ts FROM identity_events WHERE guild_id=? AND kind IN "
        "('join','leave','kick','ban') ORDER BY ts", (gid,))
    for uid, kind, ts in q:
        bucket = "join" if kind == "join" else "leave"
        key = (uid, bucket, int(ts // 60))
        if key in seen:
            continue
        seen.add(key)
        (joins if bucket == "join" else leaves)[day_of(ts)] += 1
    return joins, leaves


def reconstruct_members(joins, leaves, today, today_human):
    """Walk backward from today's human count. Returns {day: members_human}."""
    days = sorted(set(joins) | set(leaves))
    if not days:
        return {}
    first = days[0]
    out = {today: today_human}
    d, n = today, today_human
    while d > first:
        # net change ON day d moves us to the count at end of d-1
        n = n - joins.get(d, 0) + leaves.get(d, 0)
        d = prev_day(d)
        out[d] = max(n, 0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guild", required=True)
    ap.add_argument("--write", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--members-today", type=int, default=None,
                    help="today's human member count if the bot hasn't snapshotted it yet")
    a = ap.parse_args()
    gid = str(a.guild)

    msg = sqlite3.connect(f"file:{MSG_DB}?mode=ro", uri=True, timeout=60)
    st = sqlite3.connect(STATS_DB, timeout=60)
    st.execute("""CREATE TABLE IF NOT EXISTS member_daily (
        day TEXT, guild_id TEXT, joins INTEGER NOT NULL DEFAULT 0,
        leaves INTEGER NOT NULL DEFAULT 0, members_total INTEGER, members_human INTEGER,
        PRIMARY KEY (day, guild_id))""")

    today = day_of(time.time())
    existing_days = {r[0] for r in st.execute(
        "SELECT DISTINCT day FROM message_counts WHERE guild_id=?", (gid,))}
    ignore = ignored_channels(gid)

    rows, skipped_days, skipped_chan = message_rows(msg, gid, ignore, existing_days)
    total = sum(r[4] for r in rows)
    print(f"message_counts: {len(rows):,} rows / {total:,} messages to add; "
          f"skipped {len(skipped_days)} days already tracked, {skipped_chan:,} msgs in ignored channels")

    joins, leaves = member_events(msg, gid)
    row = st.execute("SELECT members_human FROM member_daily WHERE guild_id=? AND day=?",
                     (gid, today)).fetchone()
    today_human = a.members_today if a.members_today is not None else (row[0] if row else None)
    if today_human is None:
        print("! no human count for today: run after the bot has snapshotted, or pass --members-today N")
        members = {}
    else:
        members = reconstruct_members(joins, leaves, today, today_human)
    days = sorted(set(joins) | set(leaves) | set(members))
    print(f"member_daily: {len(days)} days, {sum(joins.values()):,} joins / "
          f"{sum(leaves.values()):,} departures (deduped), members reconstructed "
          f"from {today}={today_human}: earliest {days[0] if days else '-'} = "
          f"{members.get(days[0]) if days else '-'}")

    if not a.write:
        print("dry run — nothing written (add --write)")
        return

    with st:
        st.executemany(
            "INSERT OR IGNORE INTO message_counts(day, guild_id, channel_id, user_id, count) "
            "VALUES (?,?,?,?,?)", rows)
        # history rows: set joins/leaves + human count, but never overwrite a
        # day the live cog already owns (today) — those keep their live values
        hist = [(d, gid, joins.get(d, 0), leaves.get(d, 0), members.get(d))
                for d in days if d < today]
        st.executemany(
            """INSERT INTO member_daily(day, guild_id, joins, leaves, members_human)
               VALUES (?,?,?,?,?)
               ON CONFLICT(day, guild_id) DO UPDATE SET
                 joins = CASE WHEN member_daily.joins = 0 THEN excluded.joins ELSE member_daily.joins END,
                 leaves = CASE WHEN member_daily.leaves = 0 THEN excluded.leaves ELSE member_daily.leaves END,
                 members_human = COALESCE(member_daily.members_human, excluded.members_human)""",
            hist)
    print(f"written: {len(rows):,} message rows, {len(hist):,} member_daily rows")


if __name__ == "__main__":
    main()
