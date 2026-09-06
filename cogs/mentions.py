"""/mentions — your latest pings, with jump links, and the ones that vanished.

Members asked for it (2026-09-06): Discord's own Mentions inbox is easy to
lose in a busy server, and it forgets a ping the moment the message is
deleted — which is exactly the one you want to see. The archive already
knows both: `message_mentions` (utils/mentions.py, written when mod_log
archives a message) says who was pinged, `messages` says whether it is still
there and who took it down.

What a member sees (ephemeral, only ever their own pings):
  🔗 live       — author, channel, jump link
  🗑️ deleted    — the author took it back: shown WITH the text (ghost ping)
  🛡️ removed    — staff or a purge took it down: listed, text NOT re-shown
  ✏️ edited out — still posted, ping edited away: the original wording
  ↩️ reply      — a reply to one of your messages (optional)

Privacy rules: only channels the member can currently see (deleted channels
and ones they lost access to are skipped); mod-removed text is never
re-surfaced; own messages and bot chatter are excluded unless asked. The
window is the guild's archive window — 30 days on the operator/Pro archive,
the 24 h recent window everywhere else — so nothing is shown that the
archive wouldn't keep anyway.
"""
import asyncio
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import mentions as mention_index  # noqa: E402
from utils.security_config import is_enabled  # noqa: E402

COLOR = 0x5865F2
STATE_ICON = {"live": "🔗", "deleted": "🗑️", "removed": "🛡️", "edited_out": "✏️"}
LOOKBACK_DAYS = mention_index.DEFAULT_LOOKBACK_DAYS


def jump_url(guild_id, channel_id, message_id):
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def render_line(item, guild_id):
    """One archive row (already classified) -> the embed line for it.
    item keys: state, text, kind, author_id, channel_id, message_id,
    created_ts, deleted_ts, delete_kind."""
    state, kind = item["state"], item["kind"]
    when = f"<t:{int(item['created_ts'])}:R>"
    who = f"<@{item['author_id']}>"
    where = f"<#{item['channel_id']}>"
    how = {"reply": " ↩️ replied to you", "role": " 📣 role ping"}.get(kind, "")
    head = f"{STATE_ICON[state]} {when} · {who} in {where}{how}"
    if state == "live":
        head += f" · [jump]({jump_url(guild_id, item['channel_id'], item['message_id'])})"
        return f"{head}\n> {mention_index.snippet(item['text'])}"
    if state == "deleted":
        gone = f"<t:{int(item['deleted_ts'])}:R>" if item.get("deleted_ts") else ""
        return f"{head}\n> {mention_index.snippet(item['text'])}\n> *deleted {gone}*"
    if state == "removed":
        why = "purged" if item.get("delete_kind") == "bulk" else "removed by staff"
        return f"{head}\n> *{why}*"
    # edited_out
    head += f" · [jump]({jump_url(guild_id, item['channel_id'], item['message_id'])})"
    return f"{head}\n> {mention_index.snippet(item['text'])}\n> *ping edited out*"


class Mentions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _modlog(self):
        return self.bot.get_cog("ModLog")

    def _query(self, guild, member, limit, include_replies, include_roles, include_bots):
        """Blocking: read the index, drop what the member can't see, classify."""
        ml = self._modlog()
        ml._flush()  # rows from the last few seconds are still in memory
        from cogs.mod_log import retention_tier  # loaded already; avoids an import cycle at boot
        tier = retention_tier(guild.id)
        days = LOOKBACK_DAYS if tier != "recent" else 1
        since = mention_index.lookback_ts(days)
        role_ids = [r.id for r in member.roles if not r.is_default()]
        out = []
        with ml._conn() as c:
            rows = mention_index.fetch_mentions(
                c, guild.id, member.id, role_ids, since, limit,
                include_bots=include_bots, include_replies=include_replies,
                include_roles=include_roles)
            for r in rows:
                ch = guild.get_channel_or_thread(int(r["channel_id"]))
                if ch is None or not ch.permissions_for(member).view_channel:
                    continue  # gone, or not theirs to see
                state, text = mention_index.classify(r, member.id, c)
                r["state"], r["text"] = state, text
                out.append(r)
                if len(out) >= limit:
                    break
        return out, days

    @app_commands.command(name="mentions",
                          description="Your latest pings with jump links — including the ones that were deleted.")
    @app_commands.describe(
        limit="How many to show (default 10, max 20)",
        replies="Include replies to your messages (default yes)",
        roles="Include pings of roles you hold (default no — busy servers ping roles a lot)",
        bots="Include pings from bots (default no)")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def mentions(self, interaction: discord.Interaction,
                       limit: app_commands.Range[int, 1, 20] = 10,
                       replies: bool = True, roles: bool = False, bots: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, member = interaction.guild, interaction.user
        if self._modlog() is None or not is_enabled(guild.id, "msglog"):
            await interaction.followup.send(
                "This server doesn't keep a message log, so there's nothing to look up. "
                "A server manager can turn it on with `/msglog enable`.", ephemeral=True)
            return
        items, days = await asyncio.to_thread(
            self._query, guild, member, limit, replies, roles, bots)
        window = f"last {days} days" if days > 1 else "last 24 hours"
        if not items:
            await interaction.followup.send(
                f"No pings for you in the {window}"
                + ("" if replies else " (replies excluded)") + ".", ephemeral=True)
            return
        lines = [render_line(it, guild.id) for it in items]
        gone = sum(1 for it in items if it["state"] != "live")
        embed = discord.Embed(
            title=f"📣 Your latest mentions — {len(items)}",
            description="\n".join(lines)[:4000], color=COLOR)
        foot = f"{window} · 🗑️ = deleted after pinging you"
        if gone:
            foot = f"{gone} no longer there · " + foot
        if not roles:
            foot += " · roles:True to include role pings"
        embed.set_footer(text=foot)
        await interaction.followup.send(embed=embed, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @mentions.error
    async def _err(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            try:
                await interaction.response.send_message(
                    f"Slow down. Try again in {error.retry_after:.0f}s.", ephemeral=True)
            except discord.errors.NotFound:
                pass


async def setup(bot):
    await bot.add_cog(Mentions(bot))
