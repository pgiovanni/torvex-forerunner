"""Scam images — listener-only cog, no commands.

Every image an ordinary member posts is fingerprinted (utils/scam_images.py)
and compared with the known scam templates in data/scam_image_hashes.json. A
match is acted on at the FIRST message, in whatever channel it lands — text,
thread, or the chat inside a voice channel, which is where these bursts start
and where nobody looks. The honeypot only ever saw the eighth message.

Configured on the dashboard's AutoMod card, section "Scam images" (config
lives on the panel, not in the slash tree): `automod_scamimg_mode`
off | delete | timeout | kick | ban — ON by default as `timeout`.

Never touched: bots and webhooks, the server owner, the whitelist. Staff are
NOT exempt unless the server says so on the card — the accounts that post
this are hijacked, and a hijacked moderator posts the same pictures.

Every catch lands in `linkguard.db.scam_image_hits` and on a card in the
Moderation log channel.
"""
import asyncio
import datetime
import logging
import os
import sys
import time

import discord
from discord.ext import commands

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import automod as am  # noqa: E402
from utils import scam_images as si  # noqa: E402
from utils.security_config import get_config  # noqa: E402

log = logging.getLogger("scam_images")

# One log card per member per burst: if the punishment can't land (no
# permission, role too high) the tool keeps posting, every message is still
# deleted, and the log channel gets one card instead of eight.
CARD_QUIET_SECONDS = 300

_STAFF_PERMS = ("administrator", "manage_guild", "manage_channels", "manage_messages",
                "kick_members", "ban_members", "moderate_members", "manage_roles")


def _is_staff(member, cfg):
    picked = tuple(p for p in (str(x).strip().lower()
                               for x in (cfg or {}).get("automod_staff_perms") or [])
                   if p in _STAFF_PERMS)
    perms = member.guild_permissions
    return any(getattr(perms, name, False) for name in (picked or _STAFF_PERMS))


class ScamImages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.templates, self.threshold = si.load_templates()
        self._carded = {}      # (guild_id, user_id) -> quiet-until ts
        self._punished = {}    # (guild_id, user_id) -> ts of the action that landed
        try:
            si.init_db()
        except Exception:
            log.exception("scam_images: ledger init failed")
        print(f"[SCAMIMG] {len(self.templates)} templates, threshold {self.threshold}")

    # ------------------------------------------------------------- helpers
    def _exempt(self, guild, member, cfg):
        if member.id == self.bot.user.id or member.id == guild.owner_id:
            return True
        if member.id in set(cfg.get("whitelist") or []):
            return True
        return si.staff_exempt(cfg) and _is_staff(member, cfg)

    async def _first_match(self, attachments):
        """→ (match, images_checked). Stops at the first image that matches."""
        checked = 0
        for att in attachments:
            if not si.is_image(att.filename, att.content_type) or att.size > si.MAX_BYTES:
                continue
            try:
                data = await att.read()
            except (discord.HTTPException, discord.NotFound):
                continue
            checked += 1
            hashes = await asyncio.to_thread(si.hash_bytes, data)
            found = si.match(hashes, self.templates, self.threshold)
            if found:
                return found, checked
        return None, checked

    async def _punish(self, guild, member, mode, cfg, label):
        """→ (done, failed): what landed, or why it didn't."""
        key = (guild.id, member.id)
        if time.time() - self._punished.get(key, 0) < CARD_QUIET_SECONDS:
            return "already actioned this burst", None
        me = guild.me
        reason = f"AutoMod: scam image ({label})"
        try:
            if mode == "ban":
                if not (me.guild_permissions.ban_members and member.top_role < me.top_role):
                    return None, "I lack **Ban Members** or my role is below theirs."
                await member.ban(reason=reason, delete_message_days=1)
                done = "🔨 Banned"
            elif mode == "kick":
                if not (me.guild_permissions.kick_members and member.top_role < me.top_role):
                    return None, "I lack **Kick Members** or my role is below theirs."
                await member.kick(reason=reason)
                done = "👢 Kicked"
            else:
                if not me.guild_permissions.moderate_members:
                    return None, "I lack **Timeout Members**."
                if member.guild_permissions.administrator:
                    return None, "They hold Administrator — Discord ignores timeouts on admins."
                mins = si.timeout_minutes(cfg)
                await member.timeout(datetime.timedelta(minutes=mins), reason=reason)
                done = f"⏳ Timed out for {mins} min"
        except discord.Forbidden:
            return None, "Discord refused the action (permissions / role hierarchy)."
        except discord.HTTPException:
            return None, "Discord returned an error carrying out the action."
        self._punished[key] = time.time()
        return done, None

    # ------------------------------------------------------------ listener
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or not message.attachments or not self.templates:
            return
        if message.author.bot or message.webhook_id:
            return
        try:
            await self._check(message)
        except Exception:
            log.exception("scam_images: check failed")

    async def _check(self, message):
        guild = message.guild
        cfg = get_config(guild.id)
        mode = si.mode(cfg)
        if mode == "off":
            return
        member = guild.get_member(message.author.id)
        if member is None or self._exempt(guild, member, cfg):
            return
        found, checked = await self._first_match(message.attachments)
        if not found:
            return

        deleted = False
        try:
            await message.delete()
            deleted = True
        except discord.NotFound:
            deleted = True     # someone else got there first; it is gone either way
        except (discord.Forbidden, discord.HTTPException):
            pass

        done, failed = (None, None)
        if mode != "delete":
            done, failed = await self._punish(guild, member, mode, cfg, found["label"])

        prior = si.count_hits(guild.id, member.id)
        try:
            await asyncio.to_thread(si.record_hit, {
                "guild_id": guild.id, "user_id": member.id, "username": str(member),
                "channel_id": message.channel.id, "message_id": message.id,
                "family": found["family"], "template": found["name"],
                "distance": found["distance"], "images": len(message.attachments),
                "mode": mode, "deleted": deleted, "action": done, "failed": failed})
        except Exception:
            log.exception("scam_images: ledger write failed")
        print(f"[SCAMIMG] {guild.id} {member} ({member.id}) #{message.channel.id} "
              f"{found['family']}/{found['name']} d={found['distance']} "
              f"deleted={deleted} action={done} failed={failed}")
        await self._card(guild, cfg, message, member, found, mode, deleted, done, failed, prior)

    async def _card(self, guild, cfg, message, member, found, mode, deleted, done, failed, prior):
        key = (guild.id, member.id)
        now = time.time()
        if len(self._carded) > 2048:
            self._carded = {k: v for k, v in self._carded.items() if v > now}
        if self._carded.get(key, 0) > now:
            return
        self._carded[key] = now + CARD_QUIET_SECONDS
        cid = am.log_channel_id(cfg)
        ch = guild.get_channel(cid) if cid else None
        if ch is None:
            return
        embed = discord.Embed(
            title="🖼️ Scam image removed" if deleted else "🖼️ Scam image detected",
            color=0xD9534F,
            description=f"{member.mention} (`{member}` · `{member.id}`) in <#{message.channel.id}>",
            timestamp=discord.utils.utcnow())
        embed.add_field(name="Matched", value=f"{found['label']}\n`{found['name']}` · "
                                              f"{found['distance']} bits off", inline=False)
        if mode == "delete":
            result = "Message deleted — no action on the member (card setting)."
        elif done:
            result = done
        else:
            result = f"⚠️ **Not actioned** — {failed}"
        embed.add_field(name="Result", value=result, inline=False)
        embed.add_field(name="Prior catches", value=str(prior), inline=True)
        joined = getattr(member, "joined_at", None)
        if joined:
            embed.add_field(name="Joined", value=f"<t:{int(joined.timestamp())}:R>", inline=True)
        if not deleted:
            embed.add_field(name="⚠️", value="delete failed — check Manage Messages", inline=True)
        embed.set_footer(text=f"Message ID {message.id} · likely a hijacked account — "
                              "further copies in this burst are deleted without another card")
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(ScamImages(bot))
