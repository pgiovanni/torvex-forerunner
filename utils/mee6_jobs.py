"""MEE6 migration job queue — the bot's half of level_jobs.db.

The dashboard cannot run a migration itself: the import writes Postgres
(`guild_xp` / `level_roles`) which the dashboard has no handle on, and the sync
sweep assigns roles, which only the bot process can do. So the dashboard writes
a job row and this queue hands it to the bot, exactly like `panel_store` hands
reaction-role panels to `panel_sync`.

One active job per guild is enforced by a partial unique index rather than by
checking-then-inserting, so a double-clicked "Migrate now" cannot start two
imports racing on the same guild's XP table.

The dashboard keeps its own copy of this schema (dashboard/mee6_jobs.py). That
duplication is deliberate and matches gate_terms: the dashboard imports no bot
code, so the two halves must each be able to create the table.
"""
import os
import sqlite3
import time

DB_PATH = os.environ.get("TORVEX_LEVELJOBS_DB", "/var/lib/torvex/level_jobs.db")

STATES = ("queued", "running", "done", "failed")


def _conn(path=None):
    c = sqlite3.connect(path or DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def ensure(path=None):
    """Create the table if it isn't there. Safe to call from both processes."""
    with _conn(path) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS mee6_jobs (
                job_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id       TEXT NOT NULL,
                requested_by   TEXT,
                create_missing INTEGER NOT NULL DEFAULT 0,
                run_sync       INTEGER NOT NULL DEFAULT 1,
                state          TEXT NOT NULL DEFAULT 'queued',
                created_at     REAL NOT NULL,
                started_at     REAL,
                finished_at    REAL,
                imported       INTEGER,
                tiers          INTEGER,
                roles_created  INTEGER,
                synced         INTEGER,
                detail         TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mee6jobs_state ON mee6_jobs(state)")
        # one in-flight job per guild — the DB refuses the second, so a
        # double-click can't start two imports on the same XP table
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mee6jobs_active "
                  "ON mee6_jobs(guild_id) WHERE state IN ('queued','running')")


def claim_next(path=None):
    """Atomically take one queued job. Returns a dict or None.

    BEGIN IMMEDIATE + a state flip in the same transaction means two bot
    processes (or a restart mid-run) can never both own the same job.
    """
    with _conn(path) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM mee6_jobs WHERE state='queued' "
                        "ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        c.execute("UPDATE mee6_jobs SET state='running', started_at=? WHERE job_id=?",
                  (time.time(), row["job_id"]))
        c.execute("COMMIT")
        return dict(row)


def finish(job_id, *, ok, detail, imported=None, tiers=None,
           roles_created=None, synced=None, path=None):
    with _conn(path) as c:
        c.execute("""UPDATE mee6_jobs
                        SET state=?, finished_at=?, detail=?, imported=?, tiers=?,
                            roles_created=?, synced=?
                      WHERE job_id=?""",
                  ("done" if ok else "failed", time.time(), (detail or "")[:2000],
                   imported, tiers, roles_created, synced, job_id))


def requeue_stuck(older_than=900, path=None):
    """Return jobs stranded in 'running' by a restart to the queue.

    Import is idempotent (GREATEST everywhere, ON CONFLICT upserts), so
    re-running a half-finished job is safe — leaving it stuck is not, because
    the partial unique index would block the guild from ever migrating again.
    """
    cutoff = time.time() - older_than
    with _conn(path) as c:
        cur = c.execute("UPDATE mee6_jobs SET state='queued', started_at=NULL "
                        "WHERE state='running' AND COALESCE(started_at, 0) < ?", (cutoff,))
        return cur.rowcount
