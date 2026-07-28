#!/usr/bin/env python3
"""Backfill identity_events from the third-party log bots' EMBEDS.

Why this exists
---------------
Carl-bot, MEE6 and Quark have been logging this guild for years. Their records
are richer than anything we kept — nickname changes verbatim, member joins with
account age, bans with case numbers — but every one of them lives inside a
Discord *embed*. messages.db archives message CONTENT, and an embed contributes
no content, so all of it is invisible to SQL. A 2026-07-28 investigation into a
deleted account proved the cost: the only surviving record of its names and its
numeric id was in Carl-bot embeds, recovered one REST call at a time.

Embeds do survive on Discord's side, and crucially Carl-bot writes the numeric
user id into the embed footer ("ID: 123..."), which means a deleted account's
id is recoverable long after the author field has collapsed to a tombstone.

This walks the log channels via REST, parses what each bot emits, and writes it
into the same identity_events table the live cog now fills.

Usage
-----
    python backfill_identity.py                 # all known log channels
    python backfill_identity.py --channel <id>  # just one
    python backfill_identity.py --since 2025-01-01
    python backfill_identity.py --dry-run       # parse + report, write nothing

Re-runnable: rows are keyed on (uid, kind, ts, after) and skipped if present.
"""
import argparse
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "messages.db")
API = "https://discord.com/api/v10"
UA = "DiscordBot (https://torvex.app, 1.0)"

# Log channels, newest tooling last. Names are for the console only.
LOG_CHANNELS = {
    "1371628168942714934": "mod-logs-2 (Carl-bot)",
    "1216161032469352588": "mod-logs (MEE6)",
    "1361746173479747615": "leaves (MEE6 plain text)",
    "1399568893315383338": "mod-logs-3 (Quark)",
    "1377288983389802587": "modlogs (Wick, dead)",
}

DISCORD_EPOCH_MS = 1420070400000


def token():
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("DISCORD_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit("DISCORD_TOKEN not found in .env")


def snowflake_for(dt):
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def ts_of(message_id):
    return (((int(message_id) >> 22) + DISCORD_EPOCH_MS)) / 1000.0


def api_get(url, tok, tries=6):
    """GET with 429 backoff. Discord's per-route limit is the only real
    constraint here; a full channel walk is thousands of calls."""
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"Authorization": "Bot " + tok, "User-Agent": UA})
        try:
            with urllib.request.urlopen(req) as f:
                return f.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = 2.0
                try:
                    retry = float(e.headers.get("Retry-After", 2.0))
                except Exception:
                    pass
                time.sleep(min(retry + 0.4, 15))
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"gave up on {url}")


# --------------------------------------------------------------------------- parsing
FOOTER_ID = re.compile(r"ID:?\s*(\d{15,25})")
MENTION_ID = re.compile(r"<@!?(\d{15,25})>")
MEE6_LEFT = re.compile(r"\*\*(.+?)\*\*\s+just left the server")

# Carl-bot embed titles → our ledger `kind`
CARL_TITLES = {
    "member joined": "join",
    "member left": "leave",
    "member banned": "ban",
    "member unbanned": "unban",
    "member kicked": "kick",
    "nickname change": "nick",
    "nickname added": "nick",
    "nickname removed": "nick",
    "name change": "username",
    "username change": "username",
    "member timed out": "timeout",
    "member timeout removed": "untimeout",
    "avatar change": "avatar",
    # our own mod_log embeds (posted into the Quark channel since 2025-07)
    "display name changed": "global_name",
    "avatar changed": "avatar",
    "server avatar changed": "guild_avatar",
    "timeout removed": "untimeout",
    "username changed": "username",
}

# Our own goodbye line in #leaves — plain text carrying BOTH id and name, which
# is exactly the pairing that survives an account deletion.
GOODBYE = re.compile(r"<@!?(\d{15,25})>\s+goodbye\s+(\S+)")

BEFORE_AFTER = re.compile(
    r"\*\*Before:\*\*\s*(.*?)\s*(?:/|\n)\s*\*?\*?\+?\*?\*?After:\*\*\s*(.*)",
    re.S)


def parse_before_after(desc):
    """Carl-bot renders '**Before:** x\n**+After:** y' — tolerate the '+' and
    both newline and slash separators."""
    if not desc:
        return None, None
    d = desc.replace("\n", " / ")
    m = re.search(r"\*\*Before:\*\*\s*(.*?)\s*/\s*\*\*\+?After:\*\*\s*(.*)", d)
    if not m:
        return None, None
    clean = lambda s: (s or "").strip().strip("*").strip() or None  # noqa: E731
    return clean(m.group(1)), clean(m.group(2))


# MEE6 writes no embed title — the event is a bolded sentence in the
# description, and the footer says "User ID: …" rather than "ID: …".
MEE6_DESC = (
    ("joined the server", "join"),
    ("left the server", "leave"),
    ("has been banned", "ban"),
    ("has been unbanned", "unban"),
    ("was kicked", "kick"),
    ("has been muted", "timeout"),
    ("has been unmuted", "untimeout"),
    ("updated their profile", "profile"),
)
# MEE6 mirrors the previous avatar onto its OWN CDN, so those links can outlive
# the Discord account entirely — the single best source of historical faces.
MD_LINK = re.compile(r"\[\[?(before|after)\]?\]\((https?://[^\s)]+)\)", re.I)


def parse_mee6_profile(fields):
    """A MEE6 'updated their profile!' embed carries one field per changed
    attribute (Avatar / Nickname / Username), each holding before/after links
    or plain values. Returns [(kind, before, after, before_url, after_url)]."""
    out = []
    for fl in fields or ():
        name = (fl.get("name") or "").strip().lower()
        val = fl.get("value") or ""
        kind = {"avatar": "avatar", "nickname": "nick", "nick": "nick",
                "username": "username", "display name": "global_name",
                "global name": "global_name"}.get(name)
        if kind is None:
            continue
        links = {m.group(1).lower(): m.group(2) for m in MD_LINK.finditer(val)}
        if links:
            out.append((kind, None, None, links.get("before"), links.get("after")))
        else:
            parts = [p.strip() for p in re.split(r"\s*(?:→|->|/)\s*", val) if p.strip()]
            b = parts[0] if len(parts) > 1 else None
            a = parts[-1] if parts else None
            out.append((kind, b, a, None, None))
    return out


def rows_from_message(msg):
    """Yield ledger rows for one log message, whichever bot wrote it."""
    ts = ts_of(msg["id"])
    out = []

    # MEE6's leave notices are plain text — the only name↔event link that
    # survives for the pre-embed era.
    content = msg.get("content") or ""
    m = MEE6_LEFT.search(content)
    if m:
        out.append(dict(ts=ts, uid=None, username=m.group(1), kind="leave",
                        before=None, after=m.group(1), by_uid=None,
                        by_name=None, reason="backfill:mee6-leaves",
                        before_url=None, after_url=None))
    g = GOODBYE.search(content)
    if g:
        out.append(dict(ts=ts, uid=g.group(1), username=g.group(2), kind="leave",
                        before=None, after=g.group(2), by_uid=None,
                        by_name=None, reason="backfill:goodbye",
                        before_url=None, after_url=None))

    for e in msg.get("embeds", []):
        title = (e.get("title") or "").strip().lower()
        desc = e.get("description") or ""
        footer = ((e.get("footer") or {}).get("text")) or ""
        author = ((e.get("author") or {}).get("name")) or None

        uid = None
        fm = FOOTER_ID.search(footer)
        if fm:
            uid = fm.group(1)
        if not uid:
            mm = MENTION_ID.search(desc)
            if mm:
                uid = mm.group(1)

        kind = None
        # longest key first: "display name changed" must not be swallowed by
        # the shorter "name change", which would mislabel it as a username edit
        for key, k in sorted(CARL_TITLES.items(), key=lambda kv: -len(kv[0])):
            if key in title:
                kind = k
                break
        if kind is None and not title:
            # MEE6 shape: the sentence lives in the description
            low = desc.lower()
            for key, k in MEE6_DESC:
                if key in low:
                    kind = k
                    break
            if kind == "profile":
                for pk, pb, pa, burl, aurl in parse_mee6_profile(e.get("fields")):
                    out.append(dict(ts=ts, uid=uid, username=author, kind=pk,
                                    before=pb, after=pa, by_uid=None, by_name=None,
                                    reason="backfill:mee6",
                                    before_url=burl, after_url=aurl))
                continue
        if kind is None and title.startswith("ban"):
            kind = "ban"          # Carl case embeds: "ban | case 9"
        if kind is None and title.startswith("unban"):
            kind = "unban"
        if kind is None and ("mute" in title or "timeout" in title):
            kind = "timeout"
        if kind is None:
            continue

        if author is None:
            am = re.match(r"\s*\*\*(.+?)\*\*", desc)
            if am:
                author = am.group(1).strip()
        before, after = parse_before_after(desc)
        if after is None and kind in ("join", "leave", "ban", "unban", "kick"):
            after = author

        by_name = None
        reason = None
        for fl in e.get("fields", []):
            n = (fl.get("name") or "").lower()
            v = (fl.get("value") or "").strip()
            if "moderator" in n or n in ("by", "responsible moderator"):
                by_name = v
            elif "reason" in n:
                reason = v
            # our own embeds put the diff in fields rather than the description
            elif n == "before" and before is None:
                before = v.strip("`") or None
            elif n == "after" and after is None:
                after = v.strip("`") or None
        if before in ("*(none)*", "default"):
            before = None
        if after in ("*(none)*", "default"):
            after = None
        # Carl case embeds put everything in the description instead of fields
        if by_name is None:
            bm = re.search(r"\*\*Responsible moderator:\*\*\s*([^/\n]+)", desc)
            if bm:
                by_name = bm.group(1).strip()
        if reason is None:
            rm = re.search(r"\*\*Reason:\*\*\s*([^/\n]+)", desc)
            if rm:
                reason = rm.group(1).strip()

        out.append(dict(ts=ts, uid=uid, username=author, kind=kind,
                        before=before, after=after, by_uid=None,
                        by_name=by_name,
                        reason=(reason or "") + " [backfill]" if reason else "backfill",
                        before_url=None, after_url=None))
    return out


# --------------------------------------------------------------------------- db
def fetch_avatar(url, tok, conn, uid, max_kb=1024):
    """Pull a historical avatar into avatar_blobs, keyed by the last path
    segment (MEE6's CDN filename / Discord's asset hash). Returns that key."""
    if not url:
        return None
    key = url.rstrip("/").split("/")[-1].split("?")[0].split(".")[0]
    if not key:
        return None
    if conn.execute("SELECT 1 FROM avatar_blobs WHERE hash=?", (key,)).fetchone():
        return key
    headers = {"User-Agent": UA}
    if "discord" in url:
        headers["Authorization"] = "Bot " + tok
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as f:
            data = f.read(max_kb * 1024 + 1)
    except Exception:
        return key
    if not data or len(data) > max_kb * 1024:
        return key
    ctype = "image/gif" if data[:6] in (b"GIF87a", b"GIF89a") else "image/png"
    try:
        conn.execute(
            "INSERT OR IGNORE INTO avatar_blobs (hash, uid, first_seen, content_type, size, data)"
            " VALUES (?,?,?,?,?,?)", (key, uid, time.time(), ctype, len(data), data))
    except Exception:
        pass
    return key


def ensure_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS avatar_blobs (
               hash TEXT PRIMARY KEY, uid TEXT, first_seen REAL,
               content_type TEXT, size INTEGER, data BLOB)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS identity_events (
               ts REAL, guild_id TEXT, uid TEXT, username TEXT, kind TEXT,
               before TEXT, after TEXT, by_uid TEXT, by_name TEXT, reason TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ident_uid ON identity_events(uid, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ident_after ON identity_events(after)")


def already_have(conn, r, guild_id):
    return conn.execute(
        "SELECT 1 FROM identity_events WHERE guild_id=? AND kind=?"
        " AND IFNULL(uid,'')=IFNULL(?,'') AND IFNULL(after,'')=IFNULL(?,'')"
        " AND ABS(ts-?)<2",
        (guild_id, r["kind"], r["uid"], r["after"], r["ts"])).fetchone() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", action="append", help="channel id (repeatable)")
    ap.add_argument("--guild", default="1215140346800119868")
    ap.add_argument("--since", help="YYYY-MM-DD; skip older messages")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.35, help="pause between pages")
    ap.add_argument("--avatars", action="store_true",
                    help="also download historical avatar images into avatar_blobs")
    ap.add_argument("--avatar-max-kb", type=int, default=1024)
    args = ap.parse_args()

    tok = token()
    channels = args.channel or list(LOG_CHANNELS)
    stop_at = None
    if args.since:
        stop_at = snowflake_for(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc))

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    grand_seen = grand_new = 0
    for ch in channels:
        label = LOG_CHANNELS.get(ch, ch)
        before = None
        pages = seen = new = 0
        print(f"\n=== {label} ({ch}) ===", flush=True)
        while True:
            url = f"{API}/channels/{ch}/messages?limit=100" + (f"&before={before}" if before else "")
            try:
                import json
                batch = json.loads(api_get(url, tok))
            except urllib.error.HTTPError as e:
                print(f"  ! HTTP {e.code} — skipping channel", flush=True)
                break
            if not batch:
                break
            pages += 1
            for msg in batch:
                if stop_at and int(msg["id"]) < stop_at:
                    batch = []
                    break
                seen += 1
                for r in rows_from_message(msg):
                    if r["uid"] is None and r["username"] is None:
                        continue
                    if args.dry_run:
                        new += 1
                        if new <= 12:
                            print(f"   {datetime.fromtimestamp(r['ts'], timezone.utc):%Y-%m-%d %H:%M} "
                                  f"{r['kind']:9} uid={r['uid']} {r['before']!r} -> {r['after']!r}")
                        continue
                    if already_have(conn, r, args.guild):
                        continue
                    if args.avatars and r["kind"] == "avatar":
                        # historical faces: MEE6's CDN copy can outlive the account
                        r["before"] = fetch_avatar(r.get("before_url"), tok, conn,
                                                   r["uid"], args.avatar_max_kb) or r["before"]
                        r["after"] = fetch_avatar(r.get("after_url"), tok, conn,
                                                  r["uid"], args.avatar_max_kb) or r["after"]
                    conn.execute(
                        "INSERT INTO identity_events (ts,guild_id,uid,username,kind,before,after,by_uid,by_name,reason)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r["ts"], args.guild, r["uid"], r["username"], r["kind"],
                         r["before"], r["after"], r["by_uid"], r["by_name"], r["reason"]))
                    new += 1
            if not batch:
                break
            before = batch[-1]["id"]
            if not args.dry_run and pages % 5 == 0:
                conn.commit()
            if pages % 10 == 0:
                print(f"  … {pages} pages, {seen} msgs, {new} rows", flush=True)
            time.sleep(args.sleep)
        if not args.dry_run:
            conn.commit()
        print(f"  done: {pages} pages, {seen} messages, {new} rows", flush=True)
        grand_seen += seen
        grand_new += new

    print(f"\nTOTAL: {grand_seen} messages scanned, {grand_new} rows "
          f"{'(dry run — nothing written)' if args.dry_run else 'written'}")
    if not args.dry_run:
        n = conn.execute("SELECT COUNT(*) FROM identity_events").fetchone()[0]
        ids = conn.execute("SELECT COUNT(DISTINCT uid) FROM identity_events WHERE uid IS NOT NULL").fetchone()[0]
        print(f"identity_events now holds {n} rows across {ids} distinct user ids")
    conn.close()


if __name__ == "__main__":
    main()
