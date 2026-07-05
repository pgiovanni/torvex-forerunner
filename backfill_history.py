"""backfill_history.py — one-time import of the guild's ENTIRE message history
into the mod-log archive (messages.db), so /msglog deleted and future forensics
have the full record, not just messages since the cog deployed.

Adapted from scan_history.py (the LinkGuard retroactive audit): same REST-only
crawl (no gateway — doesn't disturb the live bot), same channel + active +
public-archived-thread enumeration, same rate-limit handling. Instead of
scanning, every message is INSERT OR IGNORE'd, so rows the live cog already
captured win and re-runs are safe/resumable. Content + metadata only — no media
download (old attachments' CDN URLs are recorded and stay fetchable while the
message exists).

Run on the VPS in /opt/peepos-reclaimer:
    venv/bin/python backfill_history.py [--per-channel N]
"""
import asyncio
import json
import os
import sys
from datetime import datetime

import aiohttp

sys.path.insert(0, ".")
from cogs.mod_log import DB_PATH  # noqa: E402  (also creates nothing — just the path)
import sqlite3  # noqa: E402

GUILD_ID = "1215140346800119868"
API = "https://discord.com/api/v10"


def _token():
    t = os.environ.get("DISCORD_TOKEN")
    if t:
        return t
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("DISCORD_TOKEN"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no DISCORD_TOKEN in env or .env")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _row(m, gid):
    a = m.get("author") or {}
    atts = [{"filename": x.get("filename"), "url": x.get("url"),
             "size": x.get("size"), "content_type": x.get("content_type")}
            for x in m.get("attachments") or []]
    stickers = [s.get("name") for s in m.get("sticker_items") or []]
    try:
        ts = datetime.fromisoformat(m["timestamp"]).timestamp()
    except (KeyError, ValueError):
        ts = None
    return (m["id"], gid, m.get("channel_id"),
            a.get("id"), a.get("username"),
            1 if a.get("bot") else 0, 1 if m.get("webhook_id") else 0,
            ts, m.get("content") or "",
            (m.get("message_reference") or {}).get("message_id"),
            json.dumps(atts) if atts else None,
            json.dumps(stickers) if stickers else None)


INSERT = """INSERT OR IGNORE INTO messages
    (message_id,guild_id,channel_id,author_id,author_name,bot,webhook,
     created_ts,content,reply_to,attachments,stickers)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""


async def req(session, url, params=None):
    for _ in range(8):
        async with session.get(url, params=params) as r:
            if r.status == 429:
                body = await r.json()
                await asyncio.sleep(float(body.get("retry_after", 1)) + 0.1)
                continue
            if r.status in (403, 404):
                return None
            if r.status >= 500:
                await asyncio.sleep(1.5)
                continue
            r.raise_for_status()
            data = await r.json()
            if r.headers.get("X-RateLimit-Remaining") == "0":
                await asyncio.sleep(float(r.headers.get("X-RateLimit-Reset-After", 0.5)) + 0.05)
            return data
    return None


async def crawl(session, db, lock, cid, label, per_channel):
    before, n = None, 0
    batch = []
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        msgs = await req(session, f"{API}/channels/{cid}/messages", params=params)
        if not msgs:
            break
        for m in msgs:
            n += 1
            m.setdefault("channel_id", cid)
            batch.append(_row(m, GUILD_ID))
        if len(batch) >= 1000:
            async with lock:
                db.executemany(INSERT, batch)
                db.commit()
            batch = []
        before = msgs[-1]["id"]
        if len(msgs) < 100 or (per_channel and n >= per_channel):
            break
    if batch:
        async with lock:
            db.executemany(INSERT, batch)
            db.commit()
    return n


async def main():
    args = sys.argv[1:]
    per_channel = int(args[args.index("--per-channel") + 1]) if "--per-channel" in args else 0
    db = _conn()
    if not db.execute("SELECT name FROM sqlite_master WHERE name='messages'").fetchone():
        raise SystemExit("messages table missing — start the bot once (mod_log cog creates the schema)")
    lock = asyncio.Lock()
    headers = {"Authorization": f"Bot {_token()}", "User-Agent": "msglog-backfill/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        chans = await req(session, f"{API}/guilds/{GUILD_ID}/channels") or []
        targets = [(c["id"], "#" + c.get("name", "?")) for c in chans if c.get("type") in (0, 5)]
        act = await req(session, f"{API}/guilds/{GUILD_ID}/threads/active") or {}
        for t in act.get("threads", []):
            targets.append((t["id"], "thread:" + t.get("name", "?")))
        for cid, lbl in list(targets):
            if lbl.startswith("#"):
                arch = await req(session, f"{API}/channels/{cid}/threads/archived/public",
                                 params={"limit": 100})
                for t in (arch or {}).get("threads", []):
                    targets.append((t["id"], "thread:" + t.get("name", "?")))

        print(f"backfilling {len(targets)} channels/threads "
              f"(per_channel={per_channel or 'ALL'})...", flush=True)
        done = [0]
        sem = asyncio.Semaphore(8)

        async def worker(cid, label):
            async with sem:
                try:
                    n = await crawl(session, db, lock, cid, label, per_channel)
                except Exception as e:
                    done[0] += 1
                    print(f"  ERR {label}: {e}  [{done[0]}/{len(targets)}]", flush=True)
                    return 0
                done[0] += 1
                print(f"  DONE {label}: {n} msgs  [{done[0]}/{len(targets)}]", flush=True)
                return n

        counts = await asyncio.gather(*(worker(cid, label) for cid, label in targets))
    total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    db.close()
    print(f"\nDONE. crawled {sum(counts)} messages; archive now holds {total} rows.", flush=True)


asyncio.run(main())
