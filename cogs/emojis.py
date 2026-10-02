import asyncio
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
# any other Discord CDN image link (attachments etc.) — uploaded as-is
DISCORD_URL_RE = re.compile(r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/\S+", re.IGNORECASE)
NAME_RE = re.compile(r"[^A-Za-z0-9_]")
MAX_EMOJI_BYTES = 256 * 1024  # Discord upload cap
MAX_PER_CALL = 10
# Discord caps new emojis per server per hour. Past the cap it answers with a
# hold of up to an hour, which the library would silently sit out while the
# command dies as "did not respond" (33 minutes, 10/1). Give up after this long.
UPLOAD_WAIT = 25
HELD = ("Discord is holding emoji uploads for this server — it only allows so many "
        "new emojis in an hour. Try again later (it can take up to an hour to clear).")

CDN = "https://cdn.discordapp.com/emojis/{id}.{ext}"


def _clean_name(name: str) -> str:
    name = NAME_RE.sub("", name or "")[:32]
    return name if len(name) >= 2 else ""


def _shrink_if_needed(data: bytes):
    """Return emoji-uploadable bytes, downscaling stills over the 256KB cap.
    None if it can't be made to fit (e.g. oversized animation)."""
    if len(data) <= MAX_EMOJI_BYTES:
        return data
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False):
            return None
        im.thumbnail((128, 128))
        buf = io.BytesIO()
        im.convert("RGBA").save(buf, "PNG")
        out = buf.getvalue()
        return out if len(out) <= MAX_EMOJI_BYTES else None
    except Exception:
        return None


class Emojis(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _fetch(self, session: aiohttp.ClientSession, url: str,
                     cap: int = 8 * 1024 * 1024, redirects: bool = True):
        """GET url, returning at most `cap` bytes (None on error/oversize).
        Streaming cap so a link to a huge attachment can't balloon RAM."""
        try:
            async with session.get(url, allow_redirects=redirects) as resp:
                if resp.status != 200:
                    return None
                data = b""
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > cap:
                        return None
                return data
        except aiohttp.ClientError:
            return None

    async def _fetch_emoji(self, session, emoji_id: str, animated: bool):
        """Fetch emoji bytes from the CDN. Returns the uploads worth trying,
        best first — empty if it's gone or nothing fits under the upload cap."""
        ext = "gif" if animated else "png"
        base = CDN.format(id=emoji_id, ext=ext)
        data = await self._fetch(session, base)
        if data is None and animated:
            # bare-ID guess was wrong: not animated after all
            ext, animated = "png", False
            base = CDN.format(id=emoji_id, ext=ext)
            data = await self._fetch(session, base)
        if data is None:
            return []
        if len(data) <= MAX_EMOJI_BYTES:
            return [data]
        # over the cap. An animated emoji is usually well under it as WebP at
        # full size; after that the CDN's downscaled GIFs. A still just shrinks.
        if animated:
            urls = [CDN.format(id=emoji_id, ext="webp") + "?animated=true",
                    base + "?size=96", base + "?size=64"]
        else:
            urls = [base + "?size=128"]
        uploads = []
        for url in urls:
            data = await self._fetch(session, url)
            if data is not None and len(data) <= MAX_EMOJI_BYTES:
                uploads.append(data)
        return uploads

    async def _create(self, guild, **kwargs):
        """create_custom_emoji, but asyncio.TimeoutError instead of waiting out
        an hourly hold. Cancelling drops the request, so nothing lands later."""
        return await asyncio.wait_for(guild.create_custom_emoji(**kwargs), timeout=UPLOAD_WAIT)

    # One or the other, enforced by Discord itself: each subcommand has its
    # one required field, so there is no way to fill in both or neither.
    steal = app_commands.Group(
        name="steal-emoji", description="Copy custom emojis into this server",
        default_permissions=discord.Permissions(manage_emojis=True))

    @steal.command(name="emoji",
                   description="Paste emoji(s) from any server, an emoji ID, or an emoji link, and I'll add them here.")
    @app_commands.describe(
        emoji="Paste the emoji(s) to steal (from any server), a raw emoji ID, or a CDN emoji link",
        name="Rename it (only when stealing a single emoji)")
    @app_commands.checks.has_permissions(manage_emojis=True)
    async def steal_emoji(self, interaction: discord.Interaction, emoji: str, name: str = None):
        await self._steal(interaction, emoji=emoji, name=name)

    @steal.command(name="file",
                   description="Upload an image or GIF and I'll add it here as an emoji.")
    @app_commands.describe(
        file="The image / GIF to turn into an emoji",
        name="Name for the emoji (defaults to the filename)")
    @app_commands.checks.has_permissions(manage_emojis=True)
    async def steal_file(self, interaction: discord.Interaction, file: discord.Attachment,
                         name: str = None):
        await self._steal(interaction, file=file, name=name)

    async def _steal(self, interaction: discord.Interaction, emoji: str = None,
                     name: str = None, file: discord.Attachment = None):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        if not guild.me.guild_permissions.manage_emojis:
            return await interaction.response.send_message(
                "❌ I don't have the **Manage Emoji** permission here.", ephemeral=True)

        targets = []
        url_target = None
        if file is not None:
            if not (file.content_type or "").startswith("image/"):
                return await interaction.response.send_message(
                    "❌ That file isn't an image (png/jpg/gif/webp).", ephemeral=True)
            name = name or os.path.splitext(file.filename)[0]
            if not _clean_name(name):
                return await interaction.response.send_message(
                    "❌ The filename doesn't make a usable emoji name — pass `name:` too.",
                    ephemeral=True)
            url_target = file.url
        else:
            found = EMOJI_RE.findall(emoji)
            targets = [(anim == "a", nm, eid) for anim, nm, eid in found]
        if not targets and not url_target:
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
            elif DISCORD_URL_RE.match(bare):
                if not name:
                    return await interaction.response.send_message(
                        "❌ An image link doesn't carry a name — pass `name:` too.", ephemeral=True)
                url_target = bare
            else:
                return await interaction.response.send_message(
                    "❌ No custom emoji found. Paste the emoji itself (like `<:pepe:1234…>`), "
                    "a raw emoji ID, or a Discord CDN link (emoji or attachment).",
                    ephemeral=True)
        if name and len(targets) > 1:
            return await interaction.response.send_message(
                "❌ `name:` only works when stealing a single emoji.", ephemeral=True)
        dropped = len(targets) - MAX_PER_CALL
        targets = targets[:MAX_PER_CALL]

        await interaction.response.defer()
        have = {e.id for e in guild.emojis}
        reason = f"/steal-emoji by {interaction.user} ({interaction.user.id})"
        added, failed, held = [], [], False

        if url_target:
            final_name = _clean_name(name)
            if not final_name:
                return await interaction.followup.send(
                    "❌ Bad name — 2–32 letters/numbers/underscores.")
            async with aiohttp.ClientSession() as session:
                # no redirects on arbitrary links — the CDN serves directly, and a
                # redirect is the one way a link could escape the allowlisted host
                data = await self._fetch(session, url_target, redirects=False)
            if data is None:
                return await interaction.followup.send(
                    "❌ Couldn't read that upload (over 8MB?)." if file is not None else
                    "❌ Couldn't fetch that link (expired, deleted, or too big — attachment links go stale, re-copy it).")
            data = _shrink_if_needed(data)
            if data is None:
                return await interaction.followup.send(
                    "❌ Image is over 256KB and I couldn't shrink it (animated images can't be resized).")
            try:
                new = await self._create(guild, name=final_name, image=data, reason=reason)
                return await interaction.followup.send(
                    f"✅ {'Added' if file is not None else 'Stole'} {new}")
            except asyncio.TimeoutError:
                return await interaction.followup.send(f"⏳ {HELD}")
            except ValueError:
                return await interaction.followup.send(
                    "❌ That isn't a valid image (png/jpg/gif/webp).")
            except discord.HTTPException as e:
                if e.code == 30008:
                    return await interaction.followup.send(
                        "❌ Emoji slots are FULL — free one up or boost.")
                return await interaction.followup.send(f"❌ Discord rejected it: {e.text}")

        async with aiohttp.ClientSession() as session:
            for animated, orig_name, eid in targets:
                final_name = _clean_name(name) if name else _clean_name(orig_name)
                if not final_name:
                    failed.append(f"`{orig_name or eid}` — bad name (2–32 letters/numbers/underscores)")
                    continue
                if int(eid) in have:
                    failed.append(f"`{final_name}` — already in this server")
                    continue
                uploads = await self._fetch_emoji(session, eid, animated)
                if not uploads:
                    failed.append(f"`{final_name}` — couldn't fetch it (deleted, or too large even resized)")
                    continue
                new, err = None, None
                for data in uploads:
                    try:
                        new = await self._create(guild, name=final_name, image=data, reason=reason)
                        break
                    except asyncio.TimeoutError:
                        held = True
                        break
                    except (discord.HTTPException, ValueError) as e:
                        err = e  # a refused format falls through to the next, smaller one
                        if getattr(e, "code", None) == 30008:
                            break
                if new is not None:
                    added.append(str(new))
                    continue
                if held:
                    break  # the hold is on the whole server; the rest would wait too
                if getattr(err, "code", None) == 30008:
                    failed.append(f"`{final_name}` — emoji slots are FULL (free one up or boost)")
                    break  # every further attempt of this type will fail too
                failed.append(f"`{final_name}` — Discord rejected it: {getattr(err, 'text', err)}")

        lines = []
        if added:
            lines.append(f"✅ Stole {' '.join(added)}")
        if failed:
            lines.append("❌ " + "\n❌ ".join(failed))
        if held:
            lines.append(f"⏳ {HELD}")
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
