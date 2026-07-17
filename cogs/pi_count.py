"""Pi-counting channel enforcement — #count-the-digits-of-pi.

Every human message in the configured channel must be exactly the next
digit(s) of pi. Decimal points, commas and whitespace are ignored ("3.14" at
the start is the same as "314"), any amount of digits per message is fine,
and there is no alternating-poster rule. Anything else — chat, wrong digits,
emoji, attachments — is deleted with a short self-deleting notice. Correct
messages get a ✅ and are recorded in pi_count.db so that later edits or
deletes of chain messages are auto-restored by the bot (the visible sequence
always stays intact). Every MILESTONE digits the bot celebrates.

Digits are computed locally with Machin's formula in integer arithmetic — no
dependency, no lookup table. Starts at 10k digits and doubles whenever the
chain gets close, so there is no practical ceiling.
"""
import os
import sys
import asyncio
import sqlite3
import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("pi_count")

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pi_count.db"))
INITIAL_DIGITS = 10_000
HEADROOM = 500        # extend the digit cache when the chain gets this close to the end
MILESTONE = 100
NOTICE_SECONDS = 8    # how long the "wrong digit" notice stays up


def compute_pi_digits(n: int) -> str:
    """First n digits of pi as a string ("31415..."), Machin's formula."""
    def arccot(x: int, unity: int) -> int:
        xpow = unity // x
        total, k, sign = 0, 1, 1
        while xpow:
            total += sign * (xpow // k)
            xpow //= x * x
            k += 2
            sign = -sign
        return total

    # Python 3.11+ caps int->str conversion at 4300 digits by default
    if hasattr(sys, "set_int_max_str_digits"):
        sys.set_int_max_str_digits(max(sys.get_int_max_str_digits(), n + 20))
    unity = 10 ** (n + 10)  # guard digits so the tail is correct
    pi = 4 * (4 * arccot(5, unity) - arccot(239, unity))
    return str(pi)[:n]


def normalize(content: str) -> str | None:
    """Digit payload of a message, or None if it isn't a pure digit message.

    Strips decimal points, commas and whitespace; requires plain ASCII digits
    (unicode "digits" like fullwidth １ are rejected — they could never match
    the pi string anyway).
    """
    stripped = "".join(ch for ch in content if ch not in ". ,\t\n")
    if stripped and all("0" <= ch <= "9" for ch in stripped):
        return stripped
    return None


class PiCount(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pi = ""
        self._lock = asyncio.Lock()
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS config (
                       guild_id   TEXT PRIMARY KEY,
                       channel_id TEXT,
                       position   INTEGER NOT NULL DEFAULT 0
                   )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS chain_messages (
                       message_id TEXT PRIMARY KEY,
                       guild_id   TEXT,
                       channel_id TEXT,
                       user_id    TEXT,
                       digits     TEXT,
                       position   INTEGER
                   )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_pc_guild_pos ON chain_messages(guild_id, position)")

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    async def cog_load(self):
        self._pi = await asyncio.get_running_loop().run_in_executor(None, compute_pi_digits, INITIAL_DIGITS)
        log.info("pi digit cache ready (%d digits)", len(self._pi))

    # ---------- digit cache ----------

    async def _ensure_digits(self, upto: int):
        while len(self._pi) < upto + HEADROOM:
            n = max(len(self._pi) * 2, INITIAL_DIGITS)
            log.info("extending pi digit cache to %d digits", n)
            self._pi = await asyncio.get_running_loop().run_in_executor(None, compute_pi_digits, n)

    async def _matches(self, pos: int, digits: str) -> bool:
        await self._ensure_digits(pos + len(digits))
        return self._pi[pos:pos + len(digits)] == digits

    # ---------- config helpers ----------

    def _config(self, guild_id: int):
        with self._conn() as c:
            return c.execute("SELECT * FROM config WHERE guild_id=?", (str(guild_id),)).fetchone()

    def _set_position(self, guild_id: int, pos: int):
        with self._conn() as c:
            c.execute("UPDATE config SET position=? WHERE guild_id=?", (pos, str(guild_id)))

    def _record(self, message_id: int, guild_id: int, channel_id: int, user_id: int, digits: str, pos: int):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO chain_messages(message_id, guild_id, channel_id, user_id, digits, position) "
                "VALUES (?,?,?,?,?,?)",
                (str(message_id), str(guild_id), str(channel_id), str(user_id), digits, pos))

    # ---------- live enforcement ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or self.bot.user is None or message.author.id == self.bot.user.id:
            return
        cfg = self._config(message.guild.id)
        if not cfg or not cfg["channel_id"] or int(cfg["channel_id"]) != message.channel.id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return  # pins/system messages: leave alone

        async with self._lock:
            cfg = self._config(message.guild.id)  # re-read under the lock
            pos = cfg["position"]
            norm = normalize(message.content)
            if norm and not message.attachments and await self._matches(pos, norm):
                new_pos = pos + len(norm)
                self._record(message.id, message.guild.id, message.channel.id, message.author.id, norm, pos)
                self._set_position(message.guild.id, new_pos)
                try:
                    await message.add_reaction("✅")
                except discord.HTTPException:
                    pass
                if new_pos // MILESTONE > pos // MILESTONE:
                    try:
                        await message.channel.send(
                            f"🎉 **{(new_pos // MILESTONE) * MILESTONE} digits of π!** "
                            f"({new_pos} and counting — keep going!)")
                    except discord.HTTPException:
                        pass
                return

        # invalid — delete outside the lock. The chain position is untouched:
        # a wrong message never resets the count. Notify privately via DM
        # (regular messages can't get ephemeral replies); fall back to a brief
        # self-deleting channel notice only if their DMs are closed.
        try:
            await message.delete()
        except discord.HTTPException:
            return
        if norm:  # digits, but the wrong ones
            reason = f"❌ `{norm[:20]}` isn't the next number in π"
        else:
            reason = "🔢 that channel is digits of π only"
        try:
            await message.author.send(
                f"{reason} — your message in {message.channel.mention} was removed. "
                f"The count is safe at **{pos}** digits; nobody has to start over.")
        except discord.HTTPException:
            try:
                await message.channel.send(
                    f"{reason}, {message.author.mention} — count holds at **{pos}**.",
                    delete_after=NOTICE_SECONDS)
            except discord.HTTPException:
                pass

    # ---------- chain repair (edits / deletes of recorded messages) ----------

    def _pop_chain(self, message_id: int):
        with self._conn() as c:
            row = c.execute("SELECT * FROM chain_messages WHERE message_id=?", (str(message_id),)).fetchone()
            if row:
                c.execute("DELETE FROM chain_messages WHERE message_id=?", (str(message_id),))
            return row

    async def _restore(self, row, why: str):
        channel = self.bot.get_channel(int(row["channel_id"]))
        if channel is None:
            return
        try:
            msg = await channel.send(
                row["digits"],
                embed=discord.Embed(
                    description=f"🔧 restored — <@{row['user_id']}>'s digits were {why} "
                                f"(they still count for them)",
                    color=0xF1C40F))
        except discord.HTTPException:
            return
        self._record(msg.id, int(row["guild_id"]), int(row["channel_id"]), int(row["user_id"]),
                     row["digits"], row["position"])

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        with self._conn() as c:
            row = c.execute("SELECT * FROM chain_messages WHERE message_id=?",
                            (str(payload.message_id),)).fetchone()
        if not row:
            return
        content = payload.data.get("content")
        if content is None or normalize(content) == row["digits"]:
            return  # partial edit event or content still correct
        async with self._lock:
            self._pop_chain(payload.message_id)
            channel = self.bot.get_channel(int(row["channel_id"]))
            if channel is not None:
                try:
                    await (await channel.fetch_message(payload.message_id)).delete()
                except discord.HTTPException:
                    pass
            await self._restore(row, "edited away")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        async with self._lock:
            row = self._pop_chain(payload.message_id)
            if row:
                await self._restore(row, "deleted")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        async with self._lock:
            rows = [r for r in (self._pop_chain(mid) for mid in payload.message_ids) if r]
            for row in sorted(rows, key=lambda r: r["position"]):
                await self._restore(row, "bulk-deleted")

    # ---------- admin commands ----------

    group = app_commands.Group(
        name="picount", description="Pi-counting channel enforcement (admin)",
        default_permissions=discord.Permissions(administrator=True), guild_only=True)

    async def _rescan(self, channel: discord.TextChannel) -> tuple[int, int]:
        """Replay the channel's full history: rebuild position + chain records,
        delete anything that isn't the next digits of pi. Returns (position, deleted)."""
        async with self._lock:
            pos, deleted = 0, 0
            rows = []
            async for msg in channel.history(limit=None, oldest_first=True):
                own = self.bot.user is not None and msg.author.id == self.bot.user.id
                if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
                    continue
                norm = normalize(msg.content)
                if norm and not msg.attachments and await self._matches(pos, norm):
                    rows.append((str(msg.id), str(channel.guild.id), str(channel.id),
                                 str(msg.author.id), norm, pos))
                    pos += len(norm)
                elif own:
                    continue  # our milestones / restore notes stay
                else:
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.HTTPException:
                        pass
            with self._conn() as c:
                c.execute("DELETE FROM chain_messages WHERE guild_id=?", (str(channel.guild.id),))
                c.executemany(
                    "INSERT OR REPLACE INTO chain_messages(message_id, guild_id, channel_id, user_id, digits, position) "
                    "VALUES (?,?,?,?,?,?)", rows)
                c.execute(
                    "INSERT INTO config(guild_id, channel_id, position) VALUES (?,?,?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, position=excluded.position",
                    (str(channel.guild.id), str(channel.id), pos))
            return pos, deleted

    @group.command(name="set-channel", description="Enable pi-count enforcement in a channel (rescans its history)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pos, deleted = await self._rescan(channel)
        await interaction.followup.send(
            f"🥧 Enforcing in {channel.mention} — chain rebuilt at **{pos}** digits, "
            f"deleted **{deleted}** invalid message(s).", ephemeral=True)

    @group.command(name="recount", description="Rescan the pi channel from scratch and delete invalid messages")
    @app_commands.checks.has_permissions(administrator=True)
    async def recount(self, interaction: discord.Interaction):
        cfg = self._config(interaction.guild.id)
        if not cfg or not cfg["channel_id"]:
            await interaction.response.send_message("Pi-count isn't enabled here — use `/picount set-channel`.",
                                                    ephemeral=True)
            return
        channel = interaction.guild.get_channel(int(cfg["channel_id"]))
        if channel is None:
            await interaction.response.send_message("Configured channel no longer exists.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pos, deleted = await self._rescan(channel)
        await interaction.followup.send(
            f"🥧 Recount done — **{pos}** digits verified, **{deleted}** invalid message(s) deleted.",
            ephemeral=True)

    @group.command(name="status", description="Pi-count status — digits so far, top contributors")
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        cfg = self._config(interaction.guild.id)
        if not cfg or not cfg["channel_id"]:
            await interaction.response.send_message("Pi-count isn't enabled here — use `/picount set-channel`.",
                                                    ephemeral=True)
            return
        pos = cfg["position"]
        await self._ensure_digits(pos)
        with self._conn() as c:
            top = c.execute(
                "SELECT user_id, SUM(LENGTH(digits)) AS n FROM chain_messages WHERE guild_id=? "
                "GROUP BY user_id ORDER BY n DESC LIMIT 5", (str(interaction.guild.id),)).fetchall()
        tail = self._pi[max(0, pos - 10):pos]
        lines = [f"• <@{r['user_id']}> — {r['n']:,} digits" for r in top]
        await interaction.response.send_message(
            f"🥧 <#{cfg['channel_id']}> is at **{pos}** digits of π (last posted: `…{tail}`).\n"
            f"**Top contributors:**\n" + ("\n".join(lines) or "—"),
            ephemeral=True)

    @group.command(name="disable", description="Stop enforcing the pi channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def disable(self, interaction: discord.Interaction):
        with self._conn() as c:
            c.execute("UPDATE config SET channel_id=NULL WHERE guild_id=?", (str(interaction.guild.id),))
        await interaction.response.send_message("🥧 Pi-count enforcement disabled (chain records kept).",
                                                ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need **Administrator** to use pi-count commands."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot):
    await bot.add_cog(PiCount(bot))
