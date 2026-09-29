"""Cross-channel bursts — listener-only cog, no commands.

One member, the same message, several channels, a few seconds: the rule trips
on the channel that completes the burst (third by default), deletes every
message in it — voice-channel chats included — and carries out the action the
server picked. Anything more with the same signature from that member in the
next few minutes is deleted without another card.

Configured on the dashboard's AutoMod card, section "Same message across
channels": `automod_burst_mode` off | delete | timeout | kick | ban — ON by
default as `timeout` — and `automod_burst` [channels, seconds].

Never touched: bots and webhooks, the server owner, the whitelist, and (by
default, a switch on the card) staff. Channels can be exempted on the card.

Every trip lands in `linkguard.db.burst_hits`, with the fingerprints of any
pictures involved so a new scam image can be promoted to a template
(data/scam_image_hashes.json) — promotion is the operator's step, never
automatic: a rule that taught itself from whatever tripped it could be fed a
popular meme and would then punish everyone who posts it.
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
from utils import crosspost as cp  # noqa: E402
from utils import scam_images as si  # noqa: E402
from utils.security_config import get_config  # noqa: E402

log = logging.getLogger("crosspost")

# After a trip, the same signature from the same member keeps being deleted
# for this long, quietly.
TRIPPED_SECONDS = 300

_STAFF_PERMS = ("administrator", "manage_guild", "manage_channels", "manage_messages",
                "kick_members", "ban_members", "moderate_members", "manage_roles")


def _is_staff(member, cfg):
    picked = tuple(p for p in (str(x).strip().lower()
                               for x in (cfg or {}).get("automod_staff_perms") or [])
                   if p in _STAFF_PERMS)
    perms = member.guild_permissions
    return any(getattr(perms, name, False) for name in (picked or _STAFF_PERMS))


class CrossPost(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._events = {}     # (guild_id, user_id) -> [event]
        self._tripped = {}    # (guild_id, user_id, sig) -> until ts
        try:
            cp.init_db()
        except Exception:
            log.exception("crosspost: ledger init failed")
        print("[BURST] cross-channel burst rule loaded")

    # ------------------------------------------------------------- helpers
    def _exempt(self, guild, member, channel, cfg):
        if member.id == self.bot.user.id or member.id == guild.owner_id:
            return True
        if member.id in set(cfg.get("whitelist") or []):
            return True
        if cp.staff_exempt(cfg) and _is_staff(member, cfg):
            return True
        chan_ids = {str(channel.id), str(getattr(channel, "parent_id", "") or "")}
        return bool(chan_ids & cp.exempt_channels(cfg))

    def _sweep_memory(self, now):
        if len(self._events) > 4096:
            self._events = {k: v for k, v in self._events.items()
                            if v and v[-1]["ts"] >= now - cp.MAX_WINDOW}
        if len(self._tripped) > 1024:
            self._tripped = {k: v for k, v in self._tripped.items() if v > now}

    async def _delete(self, guild, channel_id, message_id):
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return False
        try:
            await channel.get_partial_message(message_id).delete()
            return True
        except discord.NotFound:
            return True      # already gone
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _punish(self, guild, member, mode, cfg):
        me = guild.me
        reason = "AutoMod: same message across channels (burst)"
        try:
            if mode == "ban":
                if not (me.guild_permissions.ban_members and member.top_role < me.top_role):
                    return None, "I lack **Ban Members** or my role is below theirs."
                await member.ban(reason=reason, delete_message_days=1)
                return "🔨 Banned", None
            if mode == "kick":
                if not (me.guild_permissions.kick_members and member.top_role < me.top_role):
                    return None, "I lack **Kick Members** or my role is below theirs."
                await member.kick(reason=reason)
                return "👢 Kicked", None
            if not me.guild_permissions.moderate_members:
                return None, "I lack **Timeout Members**."
            if member.guild_permissions.administrator:
                return None, "They hold Administrator — Discord ignores timeouts on admins."
            mins = cp.timeout_minutes(cfg)
            await member.timeout(datetime.timedelta(minutes=mins), reason=reason)
            return f"⏳ Timed out for {mins} min", None
        except discord.Forbidden:
            return None, "Discord refused the action (permissions / role hierarchy)."
        except discord.HTTPException:
            return None, "Discord returned an error carrying out the action."

    async def _image_hashes(self, message):
        """Fingerprints of the pictures in the burst, for the ledger. Best effort."""
        out = []
        templates, threshold = si.load_templates()
        for att in message.attachments[:10]:
            if not si.is_image(att.filename, att.content_type) or att.size > si.MAX_BYTES:
                continue
            try:
                hashes = await asyncio.to_thread(si.hash_bytes, await att.read())
            except (discord.HTTPException, discord.NotFound):
                continue
            if not hashes:
                continue
            known = si.match(hashes, templates, threshold)
            out.append({"dhash": "%016x" % hashes[0], "phash": "%016x" % hashes[1],
                        "size": att.size, "known": known["name"] if known else None})
        return out

    # ------------------------------------------------------------ listener
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        try:
            await self._check(message)
        except Exception:
            log.exception("crosspost: check failed")

    async def _check(self, message):
        sig = cp.signature(message.content, [a.size for a in message.attachments],
                           [s.id for s in message.stickers])
        if sig is None:
            return
        guild = message.guild
        cfg = get_config(guild.id)
        mode = cp.mode(cfg)
        if mode == "off":
            return
        member = guild.get_member(message.author.id)
        if member is None or self._exempt(guild, member, message.channel, cfg):
            return

        now = time.time()
        self._sweep_memory(now)
        key = (guild.id, member.id)
        if self._tripped.get(key + (sig,), 0) > now:
            await self._delete(guild, message.channel.id, message.id)
            return

        channels_needed, window = cp.burst_window(cfg)
        events = cp.prune(self._events.get(key, []), cp.MAX_WINDOW, now)
        events.append({"ts": now, "channel_id": message.channel.id,
                       "message_id": message.id, "sig": sig})
        self._events[key] = events
        hit = cp.burst(events, sig, channels_needed, window, now)
        if not hit:
            return
        # Claim the trip before the first await: the next message of the burst
        # is already on its way and must take the quiet-delete path above.
        self._tripped[key + (sig,)] = now + TRIPPED_SECONDS
        self._events[key] = [e for e in events if e["sig"] != sig]

        done, failed = (None, None)
        if mode != "delete":
            done, failed = await self._punish(guild, member, mode, cfg)
        results = await asyncio.gather(
            *[self._delete(guild, e["channel_id"], e["message_id"]) for e in hit])
        deleted = sum(1 for r in results if r)

        image_hashes = []
        try:
            image_hashes = await self._image_hashes(message)
        except Exception:
            log.exception("crosspost: image fingerprinting failed")
        prior = cp.count_hits(guild.id, member.id)
        span = hit[-1]["ts"] - hit[0]["ts"]
        try:
            await asyncio.to_thread(cp.record_hit, {
                "guild_id": guild.id, "user_id": member.id, "username": str(member), "sig": sig,
                "channels": [e["channel_id"] for e in hit],
                "messages": [e["message_id"] for e in hit], "span": span,
                "content": message.content, "attachments": len(message.attachments),
                "image_hashes": image_hashes, "mode": mode, "deleted": deleted,
                "action": done, "failed": failed})
        except Exception:
            log.exception("crosspost: ledger write failed")
        print(f"[BURST] {guild.id} {member} ({member.id}) {len(hit)} msgs / "
              f"{len({e['channel_id'] for e in hit})} channels / {span:.1f}s "
              f"deleted={deleted} action={done} failed={failed}")
        await self._card(guild, cfg, message, member, hit, span, mode, deleted, done, failed,
                         prior, image_hashes)

    async def _card(self, guild, cfg, message, member, hit, span, mode, deleted, done, failed,
                    prior, image_hashes):
        cid = am.log_channel_id(cfg)
        ch = guild.get_channel(cid) if cid else None
        if ch is None:
            return
        chans = []
        for e in hit:
            if e["channel_id"] not in chans:
                chans.append(e["channel_id"])
        embed = discord.Embed(
            title="📣 Same message across channels",
            color=0xD9534F,
            description=f"{member.mention} (`{member}` · `{member.id}`) posted the same thing in "
                        f"**{len(chans)} channels in {span:.0f}s**",
            timestamp=discord.utils.utcnow())
        embed.add_field(name="Channels", value=" ".join(f"<#{c}>" for c in chans)[:1024],
                        inline=False)
        what = []
        text = (message.content or "").strip()
        if text:
            shown = text[:300].replace("http", "hxxp").replace("`", "'")
            what.append(f"```{shown}```")
        if message.attachments:
            n = len(message.attachments)
            known = sum(1 for h in image_hashes if h.get("known"))
            what.append(f"{n} attachment{'s' if n != 1 else ''}"
                        + (f" — {known} match a known scam image" if known else ""))
        if message.stickers:
            what.append(f"{len(message.stickers)} sticker(s)")
        embed.add_field(name="What", value="\n".join(what)[:1024] or "—", inline=False)
        if mode == "delete":
            result = "No action on the member (card setting)."
        elif done:
            result = done
        else:
            result = f"⚠️ **Not actioned** — {failed}"
        result += f"\n🧹 deleted {deleted} of {len(hit)} messages"
        embed.add_field(name="Result", value=result, inline=False)
        embed.add_field(name="Prior bursts", value=str(prior), inline=True)
        joined = getattr(member, "joined_at", None)
        if joined:
            embed.add_field(name="Joined", value=f"<t:{int(joined.timestamp())}:R>", inline=True)
        embed.set_footer(text="Copies posted in the next 5 minutes are deleted without another card")
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(CrossPost(bot))
