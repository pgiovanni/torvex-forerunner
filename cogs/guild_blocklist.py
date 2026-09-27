"""Server blocklist enforcement — listener-only cog, no commands.

The list itself is managed on the dashboard (forerunner.torvex.app
/operator/blocklist; operator = the home guild's owner). Operator-level actions
never get a slash command (Paul, 9/27: "don't add commands for owner level
actions. put it in a dashboard"). Store: utils/guild_blocklist.py, shared with
the dashboard through /var/lib/torvex.

Enforcement points:
  * bot.py `on_guild_join` — a blocked server that re-adds the bot: the bot
    tries to post the one-line notice and leaves. If it has no channel it can
    speak in (a fresh add = integration role only; BlackNova, six times), it
    STAYS, pending, until someone gives it a role — Paul, 9/27: "let the bot
    rejoin and get perms then send the msg" — capped at NOTICE_MAX_WAIT.
  * bot.py `on_ready` — sweep of every current guild at startup; a re-add that
    landed while the bot was down is recognised from the ledger.
  * this cog — the minute loop leaves a blocked guild the dashboard blocked
    while the bot was inside (silently: the first exit says nothing), retries
    a pending notice, and gives up at the cap; role/channel listeners retry
    the instant the bot's permissions change so the stay is as short as it
    can be.
Clean-up: `on_guild_remove` purges the CONTENT stored for a blocked server —
messages, edits, mentions, media, and who-people-were identity rows (mod-log
purge in content_only mode) — and KEEPS the operational record: command log,
structural guild events, action identity rows (timeout/kick/ban/roles), stats
counts, and the guild's config row. Paul, 9/27: "not purge all records just
the bad ones so we can keep bug tracking alive." While a pending stay runs,
cogs/auto_rules.py refuses to run that guild's rules (blocklist check), so the
kept config cannot be used to moderate for them.
"""

import asyncio
import os
import sqlite3
import sys
import time

import discord
from discord.ext import commands, tasks

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import guild_blocklist as store  # noqa: E402

SWEEP_SECONDS = 60


def _purge_extras(gid: str) -> dict:
    """Blocking. Content-derived rows outside the mod-log purge: the mention
    index (who was mentioned in which message). Stats counts, the stats
    summary and the guild's config row are operational and stay."""
    ml = sys.modules.get("cogs.mod_log")
    if ml is None or not hasattr(ml, "DB_PATH") or not os.path.exists(ml.DB_PATH):
        return {}
    try:
        con = sqlite3.connect(ml.DB_PATH, timeout=30)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(message_mentions)")}
            if "guild_id" not in cols:
                return {}
            n = con.execute("DELETE FROM message_mentions WHERE guild_id=?", (str(gid),)).rowcount
            con.commit()
            return {"message_mentions": n} if n else {}
        finally:
            con.close()
    except Exception as e:
        print(f"[WARN] blocklist mention purge failed for {gid}: {e}")
        return {}


class GuildBlocklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not hasattr(bot, "blocked_pending"):
            bot.blocked_pending = {}
        self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()

    # ────────────────────────────────────────────────────────────── helpers
    async def _purge(self, gid: int) -> dict:
        """The content stored for the guild, off the event loop; the operational
        record stays (see the module docstring)."""
        counts = {}
        modlog = self.bot.get_cog("ModLog")
        if modlog is not None and hasattr(modlog, "_purge_guild"):
            try:
                counts.update(await asyncio.to_thread(modlog._purge_guild, str(gid), True) or {})
            except Exception as e:  # never let a purge failure hide the block itself
                print(f"[WARN] blocklist purge failed for {gid}: {e}")
        try:
            counts.update(await asyncio.to_thread(_purge_extras, str(gid)))
        except Exception as e:
            print(f"[WARN] blocklist extra purge failed for {gid}: {e}")
        return counts

    @staticmethod
    def _fmt_counts(counts: dict) -> str:
        keep = {k: v for k, v in counts.items() if v}
        return ("purged " + ", ".join(f"{v} {k}" for k, v in keep.items())) if keep \
            else "nothing was stored for it"

    async def _leave_silently(self, guild: discord.Guild, entry: dict):
        """The dashboard blocked a server the bot was inside: first exit, no notice."""
        print(f"[GUILD] BLOCKED {guild.id} ({guild.name!r}) — {entry['reason']!r}; leaving")
        # bot.py reads this to record the departure as `blocked` and to skip the
        # operator alert — the operator is the one who blocked it.
        getattr(self.bot, "blocked_leaving", set()).add(guild.id)
        try:
            await guild.leave()
        except Exception as e:
            print(f"[WARN] could not leave blocked guild {guild.id}: {e}")

    async def _finish_pending(self, guild: discord.Guild, posted: bool):
        """The pending stay is over — notice posted, or the cap ran out.
        bot.py owns the email + the `blocked` ledger row via blocklist_finish."""
        self.bot.blocked_pending.pop(guild.id, None)
        fn = getattr(self.bot, "blocklist_finish", None)
        if fn is not None:
            await fn(guild, posted)
        else:
            await self._leave_silently(guild, store.get(guild.id) or {"reason": "?"})

    async def _try_pending(self, guild: discord.Guild):
        """Retry the notice for a pending guild; leave the moment it lands."""
        if guild.id not in self.bot.blocked_pending:
            return
        if await store.post_notice(guild):
            print(f"[GUILD] BLOCKED notice posted in {guild.id} ({guild.name!r}) after "
                  f"{int(time.time() - self.bot.blocked_pending[guild.id])}s; leaving")
            await self._finish_pending(guild, True)

    # ─────────────────────────────────────────────────────────────── sweep
    @tasks.loop(seconds=SWEEP_SECONDS)
    async def sweep(self):
        try:
            blocked = {e["guild_id"]: e for e in await asyncio.to_thread(store.all_blocked)}
        except Exception as e:
            print(f"[WARN] blocklist read failed: {e}")
            return
        if not blocked:
            return
        now = time.time()
        for guild in list(self.bot.guilds):
            entry = blocked.get(guild.id)
            if entry is None:
                continue
            since = self.bot.blocked_pending.get(guild.id)
            if since is None:
                await self._leave_silently(guild, entry)
                continue
            if await store.post_notice(guild):
                print(f"[GUILD] BLOCKED notice posted in {guild.id} ({guild.name!r}) after "
                      f"{int(now - since)}s; leaving")
                await self._finish_pending(guild, True)
            elif now - since >= store.NOTICE_MAX_WAIT:
                print(f"[GUILD] BLOCKED notice NOT posted in {guild.id} ({guild.name!r}) after "
                      f"{int(now - since)}s — cap reached; leaving")
                await self._finish_pending(guild, False)

    @sweep.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()
        # Leftovers: content rows for blocked servers the bot is no longer in.
        try:
            inside = {g.id for g in self.bot.guilds}
            for e in await asyncio.to_thread(store.all_blocked):
                if e["guild_id"] not in inside:
                    counts = await self._purge(e["guild_id"])
                    if counts:
                        print(f"[GUILD] BLOCKED leftover purge {e['guild_id']}: {self._fmt_counts(counts)}")
        except Exception as e:
            print(f"[WARN] blocklist leftover purge failed: {e}")

    # ─────────────────────────────────────────────────────────── listeners
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # The server gave the bot a role: try the notice now, not next minute.
        if after.id == self.bot.user.id and after.guild.id in self.bot.blocked_pending \
                and before.roles != after.roles:
            await self._try_pending(after.guild)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if getattr(after, "guild", None) is not None and after.guild.id in self.bot.blocked_pending:
            await self._try_pending(after.guild)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if getattr(channel, "guild", None) is not None and channel.guild.id in self.bot.blocked_pending:
            await self._try_pending(channel.guild)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if after.guild.id in self.bot.blocked_pending:
            await self._try_pending(after.guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        # Every exit path from a blocked server ends here; the purge is ours.
        self.bot.blocked_pending.pop(guild.id, None)
        if store.is_blocked(guild.id):
            counts = await self._purge(guild.id)
            print(f"[GUILD] BLOCKED purge {guild.id} ({guild.name!r}): {self._fmt_counts(counts)}")


async def setup(bot):
    await bot.add_cog(GuildBlocklist(bot))
