"""level_jobs.db schema + claim/requeue rules (utils/mee6_jobs.py).

Pure sqlite, no discord import, so it runs in the LOCAL venv:
    py tests/test_level_jobs.py

What matters here is the 2026-09-20 migration: the live table was created
before `kind`/`payload` existed, and `ensure()` has to add them to a database
full of rows rather than only creating them on a fresh one. The other half is
the rule that a stranded TRANSFER is never replayed — it empties an account, so
a restart mid-job must leave a note, not run it again.
"""
import os
import sys
import sqlite3
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from utils import mee6_jobs  # noqa: E402

_fails = []
_total = 0


def check(name, cond):
    global _total
    _total += 1
    print(f"{'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


def cols(path):
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute("PRAGMA table_info(mee6_jobs)")}


OLD_SCHEMA = """
    CREATE TABLE mee6_jobs (
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
"""

tmp = tempfile.mkdtemp(prefix="leveljobs-")

# ── a fresh database gets the new columns outright ──────────────────────────
fresh = os.path.join(tmp, "fresh.db")
mee6_jobs.ensure(fresh)
check("fresh db has kind", "kind" in cols(fresh))
check("fresh db has payload", "payload" in cols(fresh))

# ── an EXISTING pre-migration database is upgraded in place, rows intact ────
old = os.path.join(tmp, "old.db")
with sqlite3.connect(old) as c:
    c.execute(OLD_SCHEMA)
    c.execute("INSERT INTO mee6_jobs(guild_id, state, created_at, detail) "
              "VALUES('1', 'done', ?, 'ran before the migration')", (time.time(),))
mee6_jobs.ensure(old)
check("migrated db gained kind", "kind" in cols(old))
check("migrated db gained payload", "payload" in cols(old))
with sqlite3.connect(old) as c:
    row = c.execute("SELECT kind, detail FROM mee6_jobs WHERE job_id=1").fetchone()
check("existing row survives the migration", row[1] == "ran before the migration")
check("existing row counts as an import", row[0] == "import")
mee6_jobs.ensure(old)                       # idempotent — a restart re-runs it
check("ensure() is safe to run twice", "kind" in cols(old))


def queue(path, gid, kind, payload=None, state="queued"):
    """The bot half has no writer (the dashboard queues), so tests insert."""
    with sqlite3.connect(path) as c:
        cur = c.execute(
            "INSERT INTO mee6_jobs(guild_id, kind, payload, state, created_at, started_at) "
            "VALUES(?,?,?,?,?,?)",
            (str(gid), kind, payload, state, time.time(),
             time.time() - 3600 if state == "running" else None))
        return cur.lastrowid


# ── claim_next hands back the kind and payload the dashboard wrote ──────────
q = os.path.join(tmp, "claim.db")
mee6_jobs.ensure(q)
queue(q, 10, "sync")
job = mee6_jobs.claim_next(q)
check("claims the queued job", job is not None)
check("kind comes back", job["kind"] == "sync")
with sqlite3.connect(q) as c:
    claimed_state = c.execute("SELECT state FROM mee6_jobs WHERE job_id=?",
                              (job["job_id"],)).fetchone()[0]
check("claiming flips it to running", claimed_state == "running")
check("nothing left to claim", mee6_jobs.claim_next(q) is None)

# ── a stranded sync is requeued; a stranded transfer is NOT ────────────────
s = os.path.join(tmp, "stuck.db")
mee6_jobs.ensure(s)
sync_id = queue(s, 20, "sync", state="running")
xfer_id = queue(s, 21, "transfer", '{"source": 1, "target": 2}', state="running")
back = mee6_jobs.requeue_stuck(older_than=60, path=s)
with sqlite3.connect(s) as c:
    states = dict(c.execute("SELECT job_id, state FROM mee6_jobs"))
    detail = c.execute("SELECT detail FROM mee6_jobs WHERE job_id=?", (xfer_id,)).fetchone()[0]
check("one job requeued", back == 1)
check("stranded sync goes back in the queue", states[sync_id] == "queued")
check("stranded transfer is failed, not replayed", states[xfer_id] == "failed")
check("stranded transfer says what to check", "one transaction" in (detail or ""))

print(f"\n{_total - len(_fails)}/{_total} passed")
if _fails:
    print("FAILED: " + ", ".join(_fails))
    sys.exit(1)
