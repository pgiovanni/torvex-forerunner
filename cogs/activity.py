"""Activity graphs — Statbot-parity phase 1, powered by the mod-log archive.

Renders matplotlib PNGs posted straight into the channel (`/activity …`):
daily message lines for the server / a user / a channel, an hour×weekday
heatmap, an activity leaderboard, and a member-growth curve. All message
counts come from messages.db (the full-history archive the mod_log cog
maintains — backfilled to server start), humans only (bot=0, webhook=0).
stats.db's counters stay as the cheap forward feed; the archive is what makes
"since server start" queries possible.

Also starts VOICE tracking (Statbot's other pillar): on_voice_state_update
sessions land in stats.db `voice_sessions`, so `/activity voice` accrues data
from the day this cog deployed.

Chart style: rendered on Discord's dark chat surface (#313338) so images blend
into the channel; series colors are a CVD-validated palette (blue/aqua/red,
all ≥3:1 on that surface — computed, not eyeballed); single-hue sequential
ramp for the heatmap; text in ink tokens, never series colors; no dual axes.
Rendering uses Figure objects (no pyplot global state) in an executor behind a
lock so the event loop never blocks.
"""
import asyncio
import io
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.dates import ConciseDateFormatter, AutoDateLocator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MSG_DB = os.path.join(ROOT, "messages.db")
STATS_DB = os.path.join(ROOT, "stats.db")

# ---- chart chrome (Discord dark chat surface + ink tokens) ----
SURFACE = "#313338"
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#3f4248"
BASELINE = "#4e5058"
BLUE = "#3987e5"       # series 1
AQUA = "#199e70"       # series 2
RED = "#e66767"        # series 3 (never for "bad" semantics — just slot 3)
SEQ_RAMP = ["#0d366b", "#184f95", "#1c5cab", "#256abf", "#2a78d6",
            "#3987e5", "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4", "#cde2fb"]
EMBED_COLOR = 0x3987E5
DAY = 86400
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- pure helpers
def fill_days(counts, days, now_ts):
    """counts: {'YYYY-MM-DD': n} → ([date, ...], [n, ...]) covering the last
    `days` days ending today (UTC), zero-filled — a line with missing days
    silently skipped would lie about quiet periods."""
    end = datetime.fromtimestamp(now_ts, tz=timezone.utc).date()
    out_d, out_n = [], []
    for i in range(days - 1, -1, -1):
        d = end - timedelta(days=i)
        out_d.append(d)
        out_n.append(counts.get(d.isoformat(), 0))
    return out_d, out_n


def heatmap_matrix(triples):
    """triples of (sqlite %w weekday 0=Sun, hour 0-23, count) → 7×24 matrix
    with rows Monday-first (the way humans read week grids)."""
    m = [[0] * 24 for _ in range(7)]
    for w, h, n in triples:
        m[(int(w) + 6) % 7][int(h)] = n
    return m


def short_name(name, limit=18):
    name = (name or "?").split("#")[0]
    return name if len(name) <= limit else name[: limit - 1] + "…"


def session_seconds(joined_ts, left_ts, cutoff=0):
    """Billable seconds of a voice session clipped to a window start."""
    start = max(joined_ts, cutoff)
    return max(0.0, left_ts - start)


# --------------------------------------------------------------------------- rendering
def _fig(w, h):
    fig = Figure(figsize=(w, h), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    return fig


def _style(ax, y_grid=True):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelcolor=MUTED, length=0, labelsize=9)
    if y_grid:
        ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _title(ax, text, sub=None):
    ax.set_title(text, loc="left", color=INK, fontsize=13, fontweight="bold", pad=14)
    if sub:
        ax.text(0, 1.02, sub, transform=ax.transAxes, color=INK_2, fontsize=9)


def _png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE, bbox_inches="tight", pad_inches=0.35)
    buf.seek(0)
    return buf


def render_daily_line(dates, counts, title, sub):
    fig = _fig(8.6, 4.0)
    ax = fig.add_subplot(111)
    _style(ax)
    ax.plot(dates, counts, color=BLUE, linewidth=2, solid_capstyle="round")
    ax.fill_between(dates, counts, color=BLUE, alpha=0.12, linewidth=0)
    if counts:
        ax.annotate(f"{counts[-1]:,}", (dates[-1], counts[-1]),
                    textcoords="offset points", xytext=(4, 6),
                    color=INK_2, fontsize=9, fontweight="bold")
    loc = AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(ConciseDateFormatter(loc))
    ax.set_ylim(bottom=0)
    ax.margins(x=0.01)
    _title(ax, title, sub)
    return _png(fig)


def render_leaderboard(names, counts, title, sub):
    fig = _fig(8.0, 0.5 + 0.42 * max(len(names), 3))
    ax = fig.add_subplot(111)
    _style(ax, y_grid=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    y = range(len(names))[::-1]
    ax.barh(list(y), counts, height=0.68, color=BLUE)
    ax.set_yticks(list(y), [short_name(n) for n in names])
    ax.tick_params(axis="y", labelcolor=INK_2)
    span = max(counts) if counts else 1
    for yi, v in zip(y, counts):
        ax.text(v + span * 0.012, yi, f"{v:,}", va="center",
                color=INK_2, fontsize=9)
    ax.margins(x=0.09)
    ax.spines["bottom"].set_visible(False)
    _title(ax, title, sub)
    return _png(fig)


def render_heatmap(matrix, title, sub):
    fig = _fig(9.4, 3.4)
    ax = fig.add_subplot(111)
    _style(ax, y_grid=False)
    cmap = LinearSegmentedColormap.from_list("seq", SEQ_RAMP)
    cmap.set_under(SURFACE)  # true-zero cells recede into the surface
    mesh = ax.pcolormesh(matrix, cmap=cmap, vmin=0.5,
                         edgecolors=SURFACE, linewidth=1.5)
    ax.set_xticks([x + 0.5 for x in range(0, 24, 3)], [f"{h:02d}" for h in range(0, 24, 3)])
    ax.set_yticks([y + 0.5 for y in range(7)], WEEKDAYS)
    ax.tick_params(axis="y", labelcolor=INK_2)
    ax.invert_yaxis()
    ax.spines["bottom"].set_visible(False)
    cb = fig.colorbar(mesh, ax=ax, pad=0.015, aspect=14)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, labelsize=8, length=0)
    _title(ax, title, sub)
    return _png(fig)


def render_growth(dates, totals, title, sub):
    fig = _fig(8.6, 4.0)
    ax = fig.add_subplot(111)
    _style(ax)
    ax.plot(dates, totals, color=AQUA, linewidth=2)
    if totals:
        ax.annotate(f"{totals[-1]:,}", (dates[-1], totals[-1]),
                    textcoords="offset points", xytext=(4, 6),
                    color=INK_2, fontsize=9, fontweight="bold")
    loc = AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(ConciseDateFormatter(loc))
    ax.set_ylim(bottom=0)
    ax.margins(x=0.01)
    _title(ax, title, sub)
    return _png(fig)


HUMANS = "bot=0 AND webhook=0"


class Activity(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._render_lock = asyncio.Lock()
        self._voice_open = {}  # (guild_id, user_id) -> (channel_id, joined_ts)
        with self._stats_conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS voice_sessions (
                       guild_id TEXT, channel_id TEXT, user_id TEXT,
                       user_name TEXT, joined_ts REAL, left_ts REAL, seconds REAL
                   )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_vs_guild ON voice_sessions(guild_id, left_ts)")

    def _msg_conn(self):
        c = sqlite3.connect(MSG_DB, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _stats_conn(self):
        c = sqlite3.connect(STATS_DB, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def _render(self, fn, *args):
        async with self._render_lock:  # matplotlib isn't re-entrant
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def _send_chart(self, interaction, buf, embed):
        embed.set_image(url="attachment://chart.png")
        embed.set_footer(text="torvex archive · times in UTC")
        await interaction.followup.send(embed=embed, file=discord.File(buf, "chart.png"))

    # ------------------------------------------------------------- voice tracking
    def _close_voice(self, gid, uid, name, now):
        open_ = self._voice_open.pop((gid, uid), None)
        if not open_:
            return
        cid, joined = open_
        secs = session_seconds(joined, now)
        if secs < 5:
            return  # join-blips aren't sessions
        with self._stats_conn() as c:
            c.execute("INSERT INTO voice_sessions VALUES (?,?,?,?,?,?,?)",
                      (str(gid), str(cid), str(uid), name, joined, now, secs))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or member.guild is None:
            return
        b, a = before.channel, after.channel
        if b == a:
            return  # mute/deafen/stream toggle, not a move
        now = time.time()
        if b is not None:
            self._close_voice(member.guild.id, member.id, str(member), now)
        if a is not None:
            self._voice_open[(member.guild.id, member.id)] = (a.id, now)

    async def cog_unload(self):
        now = time.time()
        for (gid, uid) in list(self._voice_open):
            self._close_voice(gid, uid, None, now)

    # ------------------------------------------------------------- data pulls
    def _daily(self, gid, since, extra="", params=()):
        with self._msg_conn() as c:
            rows = c.execute(
                f"SELECT date(created_ts,'unixepoch') d, COUNT(*) n FROM messages "
                f"WHERE guild_id=? AND created_ts>=? AND {HUMANS} {extra} GROUP BY d",
                (gid, since, *params)).fetchall()
        return {r["d"]: r["n"] for r in rows}

    # ------------------------------------------------------------- commands
    activity = app_commands.Group(name="activity",
                                  description="Server activity graphs, from the full message archive",
                                  guild_only=True)

    @activity.command(name="server", description="Messages per day, server-wide.")
    @app_commands.describe(days="Window in days (default 30)")
    async def server_cmd(self, interaction: discord.Interaction,
                         days: app_commands.Range[int, 2, 3650] = 30):
        await interaction.response.defer(thinking=True)
        gid, now = str(interaction.guild.id), time.time()
        counts = self._daily(gid, now - days * DAY)
        dates, ns = fill_days(counts, days, now)
        total = sum(ns)
        buf = await self._render(render_daily_line, dates, ns,
                                 "Messages per day",
                                 f"{interaction.guild.name} · last {days} days")
        embed = discord.Embed(title=f"📈 Server activity — last {days} days",
                              description=f"**{total:,}** messages · avg **{total // max(days,1):,}**/day",
                              color=EMBED_COLOR)
        await self._send_chart(interaction, buf, embed)

    @activity.command(name="user", description="A member's messages per day + top channels.")
    @app_commands.describe(user="Whose activity", days="Window in days (default 30)")
    async def user_cmd(self, interaction: discord.Interaction, user: discord.User,
                       days: app_commands.Range[int, 2, 3650] = 30):
        await interaction.response.defer(thinking=True)
        gid, now = str(interaction.guild.id), time.time()
        since = now - days * DAY
        counts = self._daily(gid, since, "AND author_id=?", (str(user.id),))
        dates, ns = fill_days(counts, days, now)
        with self._msg_conn() as c:
            top = c.execute(
                f"SELECT channel_id, COUNT(*) n FROM messages WHERE guild_id=? AND author_id=? "
                f"AND created_ts>=? AND {HUMANS} GROUP BY channel_id ORDER BY n DESC LIMIT 3",
                (gid, str(user.id), since)).fetchall()
        buf = await self._render(render_daily_line, dates, ns,
                                 f"Messages per day — {short_name(str(user))}",
                                 f"{interaction.guild.name} · last {days} days")
        embed = discord.Embed(title=f"📈 {user.display_name} — last {days} days",
                              description=f"**{sum(ns):,}** messages", color=EMBED_COLOR)
        if top:
            embed.add_field(name="Top channels",
                            value="\n".join(f"<#{r['channel_id']}> — {r['n']:,}" for r in top))
        await self._send_chart(interaction, buf, embed)

    @activity.command(name="channel", description="A channel's messages per day.")
    @app_commands.describe(channel="Which channel", days="Window in days (default 30)")
    async def channel_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                          days: app_commands.Range[int, 2, 3650] = 30):
        await interaction.response.defer(thinking=True)
        gid, now = str(interaction.guild.id), time.time()
        counts = self._daily(gid, now - days * DAY, "AND channel_id=?", (str(channel.id),))
        dates, ns = fill_days(counts, days, now)
        buf = await self._render(render_daily_line, dates, ns,
                                 f"Messages per day — #{channel.name}",
                                 f"{interaction.guild.name} · last {days} days")
        embed = discord.Embed(title=f"📈 #{channel.name} — last {days} days",
                              description=f"**{sum(ns):,}** messages", color=EMBED_COLOR)
        await self._send_chart(interaction, buf, embed)

    @activity.command(name="heatmap", description="When is the server active? Hour × weekday.")
    @app_commands.describe(days="Window in days (default 30)")
    async def heatmap_cmd(self, interaction: discord.Interaction,
                          days: app_commands.Range[int, 2, 3650] = 30):
        await interaction.response.defer(thinking=True)
        gid, now = str(interaction.guild.id), time.time()
        with self._msg_conn() as c:
            rows = c.execute(
                f"SELECT strftime('%w',created_ts,'unixepoch') w, "
                f"strftime('%H',created_ts,'unixepoch') h, COUNT(*) n FROM messages "
                f"WHERE guild_id=? AND created_ts>=? AND {HUMANS} GROUP BY w,h",
                (gid, now - days * DAY)).fetchall()
        m = heatmap_matrix([(r["w"], r["h"], r["n"]) for r in rows])
        buf = await self._render(render_heatmap, m, "Activity heatmap",
                                 f"{interaction.guild.name} · messages by hour (UTC) × weekday · last {days} days")
        embed = discord.Embed(title=f"🗓️ Activity heatmap — last {days} days", color=EMBED_COLOR)
        await self._send_chart(interaction, buf, embed)

    @activity.command(name="leaderboard", description="Most active members.")
    @app_commands.describe(days="Window in days (default 30)", top="How many (default 10)")
    async def leaderboard_cmd(self, interaction: discord.Interaction,
                              days: app_commands.Range[int, 2, 3650] = 30,
                              top: app_commands.Range[int, 3, 20] = 10):
        await interaction.response.defer(thinking=True)
        gid, now = str(interaction.guild.id), time.time()
        with self._msg_conn() as c:
            rows = c.execute(
                f"SELECT author_id, MAX(author_name) an, COUNT(*) n FROM messages "
                f"WHERE guild_id=? AND created_ts>=? AND {HUMANS} "
                f"GROUP BY author_id ORDER BY n DESC LIMIT ?",
                (gid, now - days * DAY, top)).fetchall()
        buf = await self._render(render_leaderboard,
                                 [r["an"] for r in rows], [r["n"] for r in rows],
                                 "Most active members",
                                 f"{interaction.guild.name} · messages · last {days} days")
        embed = discord.Embed(title=f"🏆 Leaderboard — last {days} days", color=EMBED_COLOR)
        await self._send_chart(interaction, buf, embed)

    @activity.command(name="growth", description="Member growth — current members by join date.")
    async def growth_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        joins = sorted(m.joined_at for m in interaction.guild.members if m.joined_at)
        dates = [j.date() for j in joins]
        totals = list(range(1, len(dates) + 1))
        buf = await self._render(render_growth, dates, totals,
                                 "Member growth",
                                 f"{interaction.guild.name} · current members by join date "
                                 f"(departed members not shown)")
        embed = discord.Embed(
            title="📈 Member growth",
            description=f"**{len(dates):,}** current members. Survivor curve — members who "
                        f"left aren't shown, so early history reads lower than it was.",
            color=EMBED_COLOR)
        await self._send_chart(interaction, buf, embed)

    @activity.command(name="voice", description="Voice-time leaderboard (tracked since the cog deployed).")
    @app_commands.describe(days="Window in days (default 30)", top="How many (default 10)")
    async def voice_cmd(self, interaction: discord.Interaction,
                        days: app_commands.Range[int, 2, 3650] = 30,
                        top: app_commands.Range[int, 3, 20] = 10):
        await interaction.response.defer(thinking=True)
        gid, now = str(interaction.guild.id), time.time()
        with self._stats_conn() as c:
            first = c.execute("SELECT MIN(joined_ts) FROM voice_sessions WHERE guild_id=?",
                              (gid,)).fetchone()[0]
            rows = c.execute(
                "SELECT user_id, MAX(user_name) un, SUM(seconds) s FROM voice_sessions "
                "WHERE guild_id=? AND left_ts>=? GROUP BY user_id ORDER BY s DESC LIMIT ?",
                (gid, now - days * DAY, top)).fetchall()
        if not rows:
            await interaction.followup.send(
                "🎙️ No voice sessions recorded yet — tracking started when this feature "
                "deployed, so give it a little time.")
            return
        buf = await self._render(render_leaderboard,
                                 [r["un"] for r in rows],
                                 [round((r["s"] or 0) / 3600, 1) for r in rows],
                                 "Voice time (hours)",
                                 f"{interaction.guild.name} · last {days} days · "
                                 f"tracked since {time.strftime('%Y-%m-%d', time.gmtime(first))}")
        embed = discord.Embed(title=f"🎙️ Voice leaderboard — last {days} days", color=EMBED_COLOR)
        await self._send_chart(interaction, buf, embed)


async def setup(bot):
    await bot.add_cog(Activity(bot))
