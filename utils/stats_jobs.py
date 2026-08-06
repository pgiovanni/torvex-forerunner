"""Stats-backfill job queue — the bot's half of stats_jobs.db.

Same shape as utils/mee6_jobs.py, for the same reason: the dashboard can't do
this work itself. Backfilling activity counts means crawling a guild's whole
message history over the REST API with the bot token, which only the bot has,
so the dashboard writes a job row and this hands it to the bot's loop.

WHAT A BACKFILL ACTUALLY WRITES — worth being precise, because the whole feature
rests on it: one row per (day, channel, user) holding a COUNT. No message text,
no ids, no attachments. Aggregating a server's entire history this way costs
megabytes; archiving the same history's content costs gigabytes (measured on the
home guild: 1.33M messages = 1.6 GB of archive, 1.1 MB of counts). An admin who
asks for "all time" stats is therefore asking for something cheap, and must not
be quoted the price of the expensive thing.

Progress lives on the job row rather than in memory so a restart mid-crawl
resumes instead of starting the server again from scratch.

The dashboard keeps its own copy of this schema (dashboard/stats_jobs.py) —
deliberate duplication, matching mee6_jobs/gate_terms, because the dashboard
imports no bot code and each half must be able to create the table.
"""
import os
import sqlite3
import time

DB_PATH = os.environ.get("TORVEX_STATSJOBS_DB", "/var/lib/torvex/stats_jobs.db")

STATES = ("queued", "running", "done", "failed")


def _conn(path=None):
    c = sqlite3.connect(path or DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def ensure(path=None):
    """Create the table if it isn't there. Safe to call from both processes."""
    with _conn(path) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS stats_jobs (
                job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     TEXT NOT NULL,
                requested_by TEXT,
                state        TEXT NOT NULL DEFAULT 'queued',
                created_at   REAL NOT NULL,
                started_at   REAL,
                finished_at  REAL,
                -- progress, so a restart resumes rather than recrawling
                channels_total INTEGER,
                channels_done  INTEGER NOT NULL DEFAULT 0,
                messages_seen  INTEGER NOT NULL DEFAULT 0,
                rows_written   INTEGER NOT NULL DEFAULT 0,
                cursor       TEXT,          -- json: {channel_id: last_message_id}
                detail       TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_statsjobs_state ON stats_jobs(state)")
        # One in-flight backfill per guild, enforced by the DB rather than by
        # checking-then-inserting: a double-clicked button cannot start two
        # crawls racing on the same counts table.
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_statsjobs_active "
                  "ON stats_jobs(guild_id) WHERE state IN ('queued','running')")
        # A published summary of what's tracked, so the dashboard can show real
        # numbers WITHOUT being able to read stats.db.
        #
        # /opt/peepos-reclaimer is 0750 peepos:peepos and the dashboard user is
        # deliberately not in that group — the whole point of running these as
        # two users. Widening those permissions to render one figure would trade
        # a real boundary for a nicety, so the bot pushes the figure out to the
        # queue DB (already shared, group torvexcfg) instead. Counts about
        # counts: no ids, no content, nothing per-member.
        c.execute("""
            CREATE TABLE IF NOT EXISTS stats_summary (
                guild_id   TEXT PRIMARY KEY,
                messages   INTEGER NOT NULL DEFAULT 0,
                row_count  INTEGER NOT NULL DEFAULT 0,
                first_day  TEXT,
                last_day   TEXT,
                updated_at REAL
            )
        """)


def claim_next(path=None):
    """Atomically take one queued job. Returns a dict or None."""
    with _conn(path) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM stats_jobs WHERE state='queued' "
                        "ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        c.execute("UPDATE stats_jobs SET state='running', started_at=? WHERE job_id=?",
                  (time.time(), row["job_id"]))
        c.execute("COMMIT")
        return dict(row)


def progress(job_id, *, channels_total=None, channels_done=None, messages_seen=None,
             rows_written=None, cursor=None, path=None):
    """Checkpoint. Called every flush, so 'running' on the dashboard is a live
    number and not a spinner that might mean nothing."""
    sets, args = [], []
    for col, val in (("channels_total", channels_total), ("channels_done", channels_done),
                     ("messages_seen", messages_seen), ("rows_written", rows_written),
                     ("cursor", cursor)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if not sets:
        return
    args.append(job_id)
    with _conn(path) as c:
        c.execute(f"UPDATE stats_jobs SET {', '.join(sets)} WHERE job_id=?", args)


def finish(job_id, *, ok, detail, path=None):
    with _conn(path) as c:
        c.execute("UPDATE stats_jobs SET state=?, finished_at=?, detail=? WHERE job_id=?",
                  ("done" if ok else "failed", time.time(), (detail or "")[:2000], job_id))


def requeue_stuck(older_than=1800, path=None):
    """Return jobs stranded in 'running' by a restart to the queue.

    Safe because the crawl is idempotent — counts are written per (day, channel,
    user) with REPLACE and the cursor says where to resume, so re-running only
    recomputes. Leaving one stuck is NOT safe: the partial unique index would
    block that guild from ever backfilling again.
    """
    cutoff = time.time() - older_than
    with _conn(path) as c:
        cur = c.execute("UPDATE stats_jobs SET state='queued', started_at=NULL "
                        "WHERE state='running' AND COALESCE(started_at, 0) < ?", (cutoff,))
        return cur.rowcount


def publish_summary(rows, path=None):
    """Replace the published per-guild summary. `rows` is an iterable of
    (guild_id, messages, row_count, first_day, last_day)."""
    rows = list(rows)
    if not rows:
        return 0
    now = time.time()
    with _conn(path) as c:
        c.executemany(
            """INSERT INTO stats_summary(guild_id, messages, row_count, first_day,
                                         last_day, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   messages=excluded.messages, row_count=excluded.row_count,
                   first_day=excluded.first_day, last_day=excluded.last_day,
                   updated_at=excluded.updated_at""",
            [(str(g), m, n, f, l, now) for g, m, n, f, l in rows])
    return len(rows)


def latest(guild_id, path=None):
    with _conn(path) as c:
        row = c.execute("SELECT * FROM stats_jobs WHERE guild_id=? "
                        "ORDER BY created_at DESC LIMIT 1", (str(guild_id),)).fetchone()
    return dict(row) if row else None
