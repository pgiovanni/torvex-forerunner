"""member_daily rollup in cogs/stats.py — joins/leaves counted in-cog, size
snapshotted on the summary loop, and the two writers never clobber each other.
Runs against a temp stats.db; no Discord, no network."""
import os
import sqlite3
import sys
import tempfile
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp()
os.environ["TORVEX_STATS_DB"] = os.path.join(_tmp, "stats.db")
os.environ.setdefault("TORVEX_SECURITY_DB", os.path.join(_tmp, "security_config.db"))

import cogs.stats as st  # noqa: E402


class _M(types.SimpleNamespace):
    pass


def _guild(gid, members, total=None):
    return types.SimpleNamespace(id=gid, members=members,
                                 member_count=total if total is not None else len(members))


def _cog(guilds=()):
    bot = types.SimpleNamespace(guilds=list(guilds))
    return st.Stats(bot)


def _rows(cog):
    with cog._conn() as c:
        return {(r["day"], r["guild_id"]): dict(r)
                for r in c.execute("SELECT * FROM member_daily")}


def test_db_path_comes_from_env():
    assert st.DB_PATH == os.environ["TORVEX_STATS_DB"]


def test_human_count_ignores_bots():
    ms = [_M(bot=False), _M(bot=True), _M(bot=False), _M()]
    assert st.human_count(ms) == 3


def test_joins_and_leaves_accumulate_and_flush():
    cog = _cog()
    day = st.utc_day()
    cog._members[(day, "1", "joins")] += 3
    cog._members[(day, "1", "leaves")] += 1
    cog._members[(day, "2", "joins")] += 1
    cog._flush()
    rows = _rows(cog)
    assert rows[(day, "1")]["joins"] == 3 and rows[(day, "1")]["leaves"] == 1
    assert rows[(day, "2")]["joins"] == 1 and rows[(day, "2")]["leaves"] == 0
    # a second flush ADDS — the row is a running total for the day
    cog._members[(day, "1", "leaves")] += 2
    cog._flush()
    assert _rows(cog)[(day, "1")]["leaves"] == 3
    assert not cog._members


def test_snapshot_writes_size_without_touching_counts():
    g = _guild(9, [_M(bot=False)] * 4 + [_M(bot=True)] * 2, total=6)
    cog = _cog([g])
    day = st.utc_day()
    cog._members[(day, "9", "joins")] += 5
    cog._flush()
    cog._snapshot_members()
    r = _rows(cog)[(day, "9")]
    assert (r["joins"], r["members_total"], r["members_human"]) == (5, 6, 4)
    # snapshot again with a different size, then more joins: both survive
    g.members.append(_M(bot=False)); g.member_count = 7
    cog._snapshot_members()
    cog._members[(day, "9", "joins")] += 1
    cog._flush()
    r = _rows(cog)[(day, "9")]
    assert (r["joins"], r["members_total"], r["members_human"]) == (6, 7, 5)


def test_snapshot_before_any_join_creates_row_with_zero_counts():
    cog = _cog([_guild(5, [_M(bot=False)])])
    cog._snapshot_members()
    r = _rows(cog)[(st.utc_day(), "5")]
    assert (r["joins"], r["leaves"], r["members_human"]) == (0, 0, 1)


def test_flush_failure_requeues(monkeypatch):
    cog = _cog()
    day = st.utc_day()
    cog._members[(day, "1", "joins")] += 2

    def boom():
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(cog, "_conn", boom)
    cog._flush_members()
    assert cog._members[(day, "1", "joins")] == 2
