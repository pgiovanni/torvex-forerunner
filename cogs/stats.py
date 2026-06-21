"""Lightweight activity tracking for peepos-reclaimer — message counts only.

Stores NUMBERS, never message content: one row per (day, guild, channel, user)
with a running count. Counts are batched in memory and flushed to SQLite every
FLUSH_SECONDS to avoid a DB write per message. This is the forward data feed for
future activity graphs / leaderboards (Statbot parity) — start it early, because
every day not tracked is data lost forever.

Joins/leaves/kicks/bans are tracked separately by the server_backup cog's
member_events log. Guild-agnostic: counts are keyed by guild_id.
"""
import os
import time
import sqlite3
import logging
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("stats")

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stats.db"))
FLUSH_SECONDS = 60


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pending = Counter()  # (day, guild_id, channel_id, user_id) -> count
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

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self.flusher.start()

    async def cog_unload(self):
        self.flusher.cancel()
        self._flush()  # don't lose the last in-memory batch on restart

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # numbers only — we never read or store message.content
        if message.author.bot or message.guild is None:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self._pending[(day, str(message.guild.id), str(message.channel.id), str(message.author.id))] += 1

    def _flush(self):
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

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def flusher(self):
        self._flush()

    @flusher.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

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
