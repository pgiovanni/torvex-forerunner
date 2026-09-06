"""One-off: fill `message_mentions` from the existing archive so `/mentions`
has history on day one (the index is otherwise written only for messages
archived after 2026-09-06).

Run ON THE VPS as the bot user, bot can stay up (WAL, batched commits):
    sudo -u peepos venv/bin/python tools/backfill_mentions.py [--guild ID] [--batch N]
Re-runnable and resumable (UNIQUE + INSERT OR IGNORE).
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from utils import mentions as mention_index  # noqa: E402

DB = os.path.join(ROOT, "messages.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guild", help="only this guild id (default: every guild)")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    conn = sqlite3.connect(a.db, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    t0 = time.time()

    def progress(scanned, written):
        if scanned % (a.batch * 10) == 0:
            print(f"  {scanned:,} scanned, {written:,} index rows, {time.time()-t0:.0f}s", flush=True)

    scanned, written = mention_index.backfill(conn, a.guild, a.batch, progress)
    total = conn.execute("SELECT COUNT(*) FROM message_mentions").fetchone()[0]
    print(f"done: {scanned:,} messages scanned, {written:,} rows written, "
          f"{total:,} in index, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
