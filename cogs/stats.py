"""Lightweight activity tracking for peepos-reclaimer — message counts only.

Stores NUMBERS, never message content: one row per (day, guild, channel, user)
with a running count. Counts are batched in memory and flushed to SQLite every
FLUSH_SECONDS to avoid a DB write per message. This is the data feed behind the
/activity graphs (Statbot parity) — start it early, because every day not
tracked is data lost forever.

WHY THIS ONE DEFAULTS TO ON. Every other opt-in in security_config starts at 0.
This starts at 1 because the thing it collects is a count, not a sentence, and
because history is the one input you cannot backfill from nothing — a server
that turns tracking on in March cannot ever be shown February. Admins can turn
it off (Stats card, or /stats-config), and per-channel exclusions exist for the
rooms nobody wants counted.

SCOPE, AND WHY IT IS CHEAP (measured on the home guild 2026-08-05):
    1,325,685 messages -> messages.db  1.6 GB   (content archive, consent-gated)
    the same traffic   -> stats.db     1.1 MB   (these counts)
About 1500x. So "count everything since the server was created" is a megabyte
decision, while "archive everything" is a gigabyte one. `stats_scope="all"`
queues a backfill (utils/stats_jobs.py) that crawls history over REST and writes
only counts — never content — which is what makes that difference real rather
than a claim.

Joins/leaves/kicks/bans are tracked separately by the server_backup cog's
member_events log. Guild-agnostic: counts are keyed by guild_id.
"""
import asyncio
import json
import os
import sys
import time
import sqlite3
import logging
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import stats_jobs  # noqa: E402
from utils.security_config import get_config, set_config  # noqa: E402

log = logging.getLogger("stats")

# The dashboard renders its stats pages straight from this file, so it lives
# where the dashboard user can read it (/var/lib/torvex, group torvexcfg) —
# the role_menus.db arrangement. Unset = the old in-repo path. cogs/activity.py
# (voice_sessions) reads the same variable; keep them in step.
DB_PATH = os.environ.get("TORVEX_STATS_DB") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stats.db"))
FLUSH_SECONDS = 60

# Backfill crawl tuning. 100 is Discord's per-request maximum for channel
# history; the sleep is politeness on top of discord.py's own rate limiter, so a
# backfill of a large server never competes with the live gateway for the bucket.
CRAWL_PAGE = 100
CRAWL_SLEEP = 0.25
CHECKPOINT_EVERY = 5000          # messages seen between progress writes
SUMMARY_MINUTES = 10             # how often per-guild totals are republished


def utc_day(ts=None) -> str:
    """The day bucket every table here is keyed by. UTC on purpose: a bucket
    that moved with a server's timezone setting would re-key history every time
    the setting changed. The dashboard says 'UTC' next to its charts."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def human_count(members) -> int:
    """How many of these members are people. `guild.member_count` counts apps
    as members, which inflated every headline figure this server ever quoted
    (see /server-info); the dashboard's Members chart plots both."""
    return sum(1 for m in members if not getattr(m, "bot", False))


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pending = Counter()  # (day, guild_id, channel_id, user_id) -> count
        self._members = Counter()  # (day, guild_id, 'joins'|'leaves') -> count
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS message_counts (
                       day        TEXT,
                       guild_id   TEXT,
                       channel_id TEXT,
                       user_id    TEXT,
                       count      INTEGER,
                       PRIMARY KEY (day, guild_id, channel_id, user_id)
                   )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_mc_guild_day ON message_counts(guild_id, day)")
            # One row per guild per day: how many people arrived and left, and
            # how big the server was when last looked at. Feeds the dashboard's
            # Members chart (Statbot parity). Joins/leaves exclude bots, like
            # message counts; the size columns keep both so the gap is visible.
            c.execute(
                """CREATE TABLE IF NOT EXISTS member_daily (
                       day           TEXT,
                       guild_id      TEXT,
                       joins         INTEGER NOT NULL DEFAULT 0,
                       leaves        INTEGER NOT NULL DEFAULT 0,
                       members_total INTEGER,
                       members_human INTEGER,
                       PRIMARY KEY (day, guild_id)
                   )"""
            )

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self.flusher.start()
        try:
            stats_jobs.ensure()
            stats_jobs.requeue_stuck()
        except sqlite3.Error as e:
            log.warning("stats job queue unavailable: %s", e)
        self.backfill_runner.start()
        self.summariser.start()

    async def cog_unload(self):
        self.flusher.cancel()
        self.backfill_runner.cancel()
        self.summariser.cancel()
        self._flush()  # don't lose the last in-memory batch on restart

    def _tracked(self, guild_id, channel_id) -> bool:
        """Whether this guild still wants counting, and this channel isn't
        excluded. Config is cached for 5s upstream, so this is cheap enough for
        the on_message path."""
        cfg = get_config(guild_id)
        if not cfg.get("stats_enabled", 1):
            return False
        return str(channel_id) not in {str(x) for x in (cfg.get("stats_ignore_channels") or [])}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # numbers only — we never read or store message.content
        if message.author.bot or message.guild is None:
            return
        # A thread's messages count toward the channel it lives in, so excluding
        # a channel excludes its threads too — otherwise "don't count #staff"
        # silently leaks the moment someone opens a thread there.
        parent = getattr(message.channel, "parent_id", None) or message.channel.id
        if not self._tracked(message.guild.id, parent):
            return
        self._pending[(utc_day(), str(message.guild.id), str(parent), str(message.author.id))] += 1

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        if get_config(member.guild.id).get("stats_enabled", 1):
            self._members[(utc_day(), str(member.guild.id), "joins")] += 1

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # leave, kick and ban all land here — the Members chart wants departures,
        # and the reason is mod_log's business, not a count's
        if member.bot:
            return
        if get_config(member.guild.id).get("stats_enabled", 1):
            self._members[(utc_day(), str(member.guild.id), "leaves")] += 1

    def _flush(self):
        self._flush_messages()
        self._flush_members()

    def _flush_messages(self):
        if not self._pending:
            return
        items = list(self._pending.items())
        self._pending.clear()
        try:
            with self._conn() as c:
                c.executemany(
                    """INSERT INTO message_counts(day, guild_id, channel_id, user_id, count)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(day, guild_id, channel_id, user_id)
                       DO UPDATE SET count = count + excluded.count""",
                    [(d, g, ch, u, n) for (d, g, ch, u), n in items],
                )
            log.info("flushed %d message-count rows", len(items))
        except Exception as e:
            # on failure, put the batch back so the next flush retries it
            for k, n in items:
                self._pending[k] += n
            log.warning("stats flush failed, re-queued %d rows: %s", len(items), e)

    def _flush_members(self):
        if not self._members:
            return
        items = list(self._members.items())
        self._members.clear()
        per_day = {}                                   # (day, gid) -> [joins, leaves]
        for (day, gid, kind), n in items:
            jl = per_day.setdefault((day, gid), [0, 0])
            jl[0 if kind == "joins" else 1] += n
        try:
            with self._conn() as c:
                c.executemany(
                    """INSERT INTO member_daily(day, guild_id, joins, leaves)
                       VALUES (?,?,?,?)
                       ON CONFLICT(day, guild_id)
                       DO UPDATE SET joins = joins + excluded.joins,
                                     leaves = leaves + excluded.leaves""",
                    [(d, g, j, l) for (d, g), (j, l) in per_day.items()],
                )
        except Exception as e:
            for k, n in items:
                self._members[k] += n
            log.warning("member_daily flush failed, re-queued: %s", e)

    def _snapshot_members(self):
        """Stamp today's row for every guild with how big it is right now.

        Runs on the summary loop, not on every flush — the size only needs to be
        right by end of day, and counting humans means walking the member cache
        (fine every ten minutes, wasteful every sixty seconds). Snapshotting
        UPDATEs only the size columns so it can never wipe the joins/leaves that
        _flush_members has been accumulating into the same row.
        """
        today = utc_day()
        rows = []
        for g in self.bot.guilds:
            if not get_config(g.id).get("stats_enabled", 1):
                continue
            rows.append((today, str(g.id), g.member_count or 0, human_count(g.members)))
        if not rows:
            return
        with self._conn() as c:
            c.executemany(
                """INSERT INTO member_daily(day, guild_id, members_total, members_human)
                   VALUES (?,?,?,?)
                   ON CONFLICT(day, guild_id)
                   DO UPDATE SET members_total = excluded.members_total,
                                 members_human = excluded.members_human""",
                rows)

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def flusher(self):
        self._flush()

    @flusher.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=SUMMARY_MINUTES)
    async def summariser(self):
        """Publish per-guild totals to the shared queue DB for the dashboard.

        A full GROUP BY over message_counts, so it runs on a slow loop rather
        than on every flush — the figure it feeds is a size estimate on a
        settings page, which does not need to be up to the second.
        """
        try:
            self._snapshot_members()
        except sqlite3.Error as e:
            log.warning("member snapshot failed: %s", e)
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT guild_id, COALESCE(SUM(count),0), COUNT(*), "
                    "MIN(day), MAX(day) FROM message_counts GROUP BY guild_id"
                ).fetchall()
            stats_jobs.publish_summary([tuple(r) for r in rows])
        except sqlite3.Error as e:
            log.warning("stats summary publish failed: %s", e)

    @summariser.before_loop
    async def _before_summary(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────── backfill job
    @tasks.loop(seconds=45)
    async def backfill_runner(self):
        """Consume one queued backfill at a time.

        Deliberately serial across the whole bot, not per guild: two history
        crawls at once would double our REST pressure for no gain, and this is a
        one-off per server that nobody is watching a clock on.
        """
        try:
            job = stats_jobs.claim_next()
        except sqlite3.Error:
            return
        if not job:
            return
        gid = int(job["guild_id"])
        guild = self.bot.get_guild(gid)
        if guild is None:
            stats_jobs.finish(job["job_id"], ok=False,
                              detail="I'm not in that server any more.")
            return
        try:
            seen, rows, chans = await self._backfill(guild, job)
            stats_jobs.finish(
                job["job_id"], ok=True,
                detail=(f"Counted {seen:,} messages across {chans} channels into "
                        f"{rows:,} day/channel/user rows. No message content was stored."))
            log.info("stats backfill done for %s: %d messages", gid, seen)
        except Exception as e:                       # one bad guild must not kill
            log.exception("stats backfill failed for %s", gid)      # the loop
            stats_jobs.finish(job["job_id"], ok=False, detail=f"{type(e).__name__}: {e}"[:500])

    @backfill_runner.before_loop
    async def _before_backfill(self):
        await self.bot.wait_until_ready()

    async def _backfill(self, guild, job):
        """Crawl every readable channel and write per-day counts.

        Two rules make re-running safe and make this coexist with live counting:

        * Only days STRICTLY BEFORE today are written. Today belongs to the live
          counter, which has already been adding to it; overwriting today would
          throw away everything counted since midnight.
        * Rows are REPLACEd, not incremented. The crawl reads the whole of a
          channel's history, so its number for a past day is authoritative — and
          replacing means running the job twice cannot double anyone's count.
        """
        cfg = get_config(guild.id)
        ignore = {str(x) for x in (cfg.get("stats_ignore_channels") or [])}
        today = utc_day()
        cursor = {}
        if job.get("cursor"):
            try:
                cursor = json.loads(job["cursor"]) or {}
            except (ValueError, TypeError):
                cursor = {}

        me = guild.me
        channels = [c for c in guild.text_channels
                    if str(c.id) not in ignore
                    and c.permissions_for(me).read_message_history]
        stats_jobs.progress(job["job_id"], channels_total=len(channels))

        counts = Counter()
        seen = int(job.get("messages_seen") or 0)
        rows_written = int(job.get("rows_written") or 0)
        done = 0
        last_ckpt = seen

        for ch in channels:
            # A channel already finished on an earlier run is skipped whole.
            if cursor.get(str(ch.id)) == "done":
                done += 1
                continue
            try:
                async for m in ch.history(limit=None, oldest_first=True):
                    if m.author.bot:
                        continue
                    day = m.created_at.strftime("%Y-%m-%d")
                    if day >= today:
                        continue                     # today is the live counter's
                    counts[(day, str(guild.id), str(ch.id), str(m.author.id))] += 1
                    seen += 1
                    if seen - last_ckpt >= CHECKPOINT_EVERY:
                        rows_written += self._write_counts(counts)
                        counts.clear()
                        last_ckpt = seen
                        stats_jobs.progress(job["job_id"], messages_seen=seen,
                                            rows_written=rows_written,
                                            channels_done=done,
                                            cursor=json.dumps(cursor))
                        await asyncio.sleep(CRAWL_SLEEP)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("stats backfill: skipping #%s (%s)", ch.name, e)
            cursor[str(ch.id)] = "done"
            done += 1
            stats_jobs.progress(job["job_id"], channels_done=done,
                                cursor=json.dumps(cursor))

        rows_written += self._write_counts(counts)
        stats_jobs.progress(job["job_id"], messages_seen=seen, rows_written=rows_written,
                            channels_done=done, cursor=json.dumps(cursor))
        return seen, rows_written, len(channels)

    def _write_counts(self, counts):
        """REPLACE rather than add — see _backfill's docstring for why that is
        what makes a re-run harmless."""
        if not counts:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT INTO message_counts(day, guild_id, channel_id, user_id, count)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(day, guild_id, channel_id, user_id)
                   DO UPDATE SET count = excluded.count""",
                [(d, g, ch, u, n) for (d, g, ch, u), n in counts.items()])
        return len(counts)

    @app_commands.command(name="stats-status",
                          description="Activity-tracking status — totals, date range, top channels (admin)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def stats_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._flush()  # include whatever is buffered right now
        gid = str(interaction.guild.id)
        with self._conn() as c:
            total = c.execute("SELECT COALESCE(SUM(count),0) FROM message_counts WHERE guild_id=?", (gid,)).fetchone()[0]
            span = c.execute("SELECT MIN(day), MAX(day) FROM message_counts WHERE guild_id=?", (gid,)).fetchone()
            top = c.execute(
                "SELECT channel_id, SUM(count) AS n FROM message_counts WHERE guild_id=? "
                "GROUP BY channel_id ORDER BY n DESC LIMIT 5", (gid,)).fetchall()
        if not total:
            await interaction.followup.send(
                "No messages tracked yet — counting just started. Check back in a bit.", ephemeral=True)
            return
        lines = []
        for r in top:
            ch = interaction.guild.get_channel(int(r["channel_id"]))
            lines.append(f"• {ch.mention if ch else '`' + r['channel_id'] + '`'} — {r['n']:,}")
        await interaction.followup.send(
            f"📊 Tracking **{span[0]} → {span[1]}** · **{total:,}** messages counted (numbers only, no content stored).\n"
            f"**Top channels:**\n" + "\n".join(lines),
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Stats(bot))
