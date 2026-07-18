import io
import os
import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# <a:name:id> (animated) or <:name:id> (static)
EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>")
# a pasted CDN link, e.g. https://cdn.discordapp.com/emojis/123456789.webp?size=96
CDN_RE = re.compile(r"(?:cdn|media)\.discordapp\.(?:com|net)/emojis/(\d{15,25})")
NAME_RE = re.compile(r"[^A-Za-z0-9_]")
MAX_EMOJI_BYTES = 256 * 1024  # Discord upload cap
MAX_PER_CALL = 10

CDN = "https://cdn.discordapp.com/emojis/{id}.{ext}"


def _clean_name(name: str) -> str:
    name = NAME_RE.sub("", name or "")[:32]
    return name if len(name) >= 2 else ""


class Emojis(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _fetch(self, session: aiohttp.ClientSession, url: str):
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()

    async def _fetch_emoji(self, session, emoji_id: str, animated: bool):
        """Fetch emoji bytes from the CDN, retrying smaller if over the upload cap.
        Returns (data, animated) or (None, animated)."""
        ext = "gif" if animated else "png"
        data = await self._fetch(session, CDN.format(id=emoji_id, ext=ext))
        if data is None and animated:
            # bare-ID guess was wrong: not animated after all
            ext, animated = "png", False
            data = await self._fetch(session, CDN.format(id=emoji_id, ext=ext))
        if data is not None and len(data) > MAX_EMOJI_BYTES:
            data = await self._fetch(session, CDN.format(id=emoji_id, ext=ext) + "?size=128")
        if data is not None and len(data) > MAX_EMOJI_BYTES:
            data = None
        return data, animated

    @app_commands.command(name="steal-emoji",
                          description="Copy custom emojis into this server — paste them (or one emoji ID) and I'll grab them.")
    @app_commands.describe(
        emoji="Paste the emoji(s) to steal (from any server), a raw emoji ID, or a CDN emoji link",
        name="Rename it (only when stealing a single emoji)")
    @app_commands.default_permissions(manage_emojis=True)
    @app_commands.checks.has_permissions(manage_emojis=True)
    async def steal_emoji(self, interaction: discord.Interaction, emoji: str, name: str = None):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        if not guild.me.guild_permissions.manage_emojis:
            return await interaction.response.send_message(
                "❌ I don't have the **Manage Emoji** permission here.", ephemeral=True)

        found = EMOJI_RE.findall(emoji)
        targets = [(anim == "a", nm, eid) for anim, nm, eid in found]
        if not targets:
            bare = emoji.strip().strip("<>")
            m = CDN_RE.search(bare)
            if m:
                bare = m.group(1)
            if bare.isdigit():
                if not name:
                    return await interaction.response.send_message(
                        "❌ An ID or CDN link doesn't carry the original name — pass `name:` too.",
                        ephemeral=True)
                # animated unknown for a bare ID/link; we try gif first and fall back
                targets = [(True, name, bare)]
            else:
                return await interaction.response.send_message(
                    "❌ No custom emoji found. Paste the emoji itself (like `<:pepe:1234…>`), "
                    "a raw emoji ID, or a cdn.discordapp.com/emojis link.",
                    ephemeral=True)
        if name and len(targets) > 1:
            return await interaction.response.send_message(
                "❌ `name:` only works when stealing a single emoji.", ephemeral=True)
        dropped = len(targets) - MAX_PER_CALL
        targets = targets[:MAX_PER_CALL]

        await interaction.response.defer()
        have = {e.id for e in guild.emojis}
        reason = f"/steal-emoji by {interaction.user} ({interaction.user.id})"
        added, failed = [], []

        async with aiohttp.ClientSession() as session:
            for animated, orig_name, eid in targets:
                final_name = _clean_name(name) if name else _clean_name(orig_name)
                if not final_name:
                    failed.append(f"`{orig_name or eid}` — bad name (2–32 letters/numbers/underscores)")
                    continue
                if int(eid) in have:
                    failed.append(f"`{final_name}` — already in this server")
                    continue
                data, animated = await self._fetch_emoji(session, eid, animated)
                if data is None:
                    failed.append(f"`{final_name}` — couldn't fetch it (deleted, or too large even resized)")
                    continue
                try:
                    new = await guild.create_custom_emoji(name=final_name, image=data, reason=reason)
                    added.append(str(new))
                except discord.HTTPException as e:
                    if e.code == 30008:
                        failed.append(f"`{final_name}` — emoji slots are FULL (free one up or boost)")
                        break  # every further attempt of this type will fail too
                    failed.append(f"`{final_name}` — Discord rejected it: {e.text}")

        lines = []
        if added:
            lines.append(f"✅ Stole {' '.join(added)}")
        if failed:
            lines.append("❌ " + "\n❌ ".join(failed))
        if dropped > 0:
            lines.append(f"⚠️ Only {MAX_PER_CALL} per command — {dropped} skipped, run it again for those.")
        embed = discord.Embed(
            description="\n".join(lines),
            color=discord.Color.green() if added else discord.Color.red())
        embed.set_footer(text=f"by {interaction.user}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="backup_emojis",
                          description="Download all server emojis and save them to the emojis/ folder on the bot host.")
    @app_commands.default_permissions(manage_emojis=True)
    @app_commands.checks.has_permissions(manage_emojis=True)
    async def backup_emojis(self, interaction: discord.Interaction):
        guild = interaction.guild
        os.makedirs("emojis", exist_ok=True)

        await interaction.response.send_message(f"Backing up {len(guild.emojis)} emojis...", ephemeral=True)

        saved = 0
        async with aiohttp.ClientSession() as session:
            for emoji in guild.emojis:
                ext = "gif" if emoji.animated else "png"
                data = await self._fetch(session, str(emoji.url))
                if data is None:
                    continue
                with open(f"emojis/{emoji.name}.{ext}", "wb") as f:
                    f.write(data)
                saved += 1

        await interaction.followup.send(f"Done! {saved} emojis saved.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions) or "required"
            msg = f"❌ You need the **{perms}** permission to use this."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Emojis(bot))
