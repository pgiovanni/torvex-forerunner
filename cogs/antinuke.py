"""Native anti-nuke + anti-raid for peepos-reclaimer — replacing Wick.

Two detectors, both attribute to an executor and trip on RATE (not single
actions — that's the Wick over-strictness we're fixing):

  1. DESTRUCTIVE actions (audit-log): mass channel/role delete+create, mass
     ban/kick, webhook spam, mass role-grants -> response: STRIP offender roles.
  2. CHAT abuse (messages): mention-bombs, sustained ping spam, @everyone spam,
     message flooding -> response: TIMEOUT offender.

Deliberately GENEROUS so normal use passes: ONE announcement (@everyone) is
fine, tagging 5-10 people in a message is fine, normal chatting is fine. Only
sustained/extreme behavior trips.

Modes (env ANTINUKE_ENFORCE): 0/unset = SHADOW (alert-only, safe alongside Wick);
1 = ENFORCE (strip roles / timeout + alert).

Never acts on: guild owner, the bot itself, bots/webhooks, or ANTINUKE_WHITELIST
(space/comma IDs — add trusted admins + Wick's bot ID before enforce).
"""
import os
import time
import datetime
import logging
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

import quarantine_store as qstore  # shared with AltGuard — stores stripped roles for /altguard-release

log = logging.getLogger("antinuke")


def _env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


GUILD_ID = _env_int("ALTGUARD_GUILD_ID")
MODLOG_CHANNEL_ID = _env_int("ALTGUARD_MODLOG_CHANNEL_ID")
QUARANTINE_ROLE_ID = _env_int("ALTGUARD_QUARANTINE_ROLE_ID")
ENFORCE = os.environ.get("ANTINUKE_ENFORCE", "0") != "0"
WHITELIST = {int(x) for x in os.environ.get("ANTINUKE_WHITELIST", "").replace(",", " ").split() if x.strip().isdigit()}
TIMEOUT_MIN = _env_int("ANTINUKE_TIMEOUT_MIN", 10)
# on a mass-ban trip, auto-unban the victims the nuker just banned + post an invite
RESTORE_BANS = os.environ.get("ANTINUKE_RESTORE_BANS", "1") != "0"

# destructive actions: key -> (count, window_s). Response = strip roles.
ACTION_LIMITS = {
    "channel_delete": (3, 12),
    "channel_create": (4, 12),
    "role_delete":    (3, 12),
    "role_create":    (4, 12),
    "ban":            (5, 20),
    "kick":           (5, 20),
    "webhook":        (4, 12),
    "member_role":    (5, 15),
}

# chat abuse: GENEROUS thresholds so announcements / normal pings pass clean.
# Response = timeout.
MENTION_BOMB = 15        # >= this many mentions in ONE message = instant flag
MENTION_RATE = (25, 10)  # >= 25 mentions across messages in 10s
EVERYONE_RATE = (4, 20)  # >= 4 @everyone/@here that actually pinged in 20s
FLOOD_RATE = (12, 7)     # >= 12 messages in 7s

_AUDIT = {
    "channel_delete": discord.AuditLogAction.channel_delete,
    "channel_create": discord.AuditLogAction.channel_create,
    "role_delete":    discord.AuditLogAction.role_delete,
    "role_create":    discord.AuditLogAction.role_create,
    "ban":            discord.AuditLogAction.ban,
    "kick":           discord.AuditLogAction.kick,
    "webhook":        discord.AuditLogAction.webhook_create,
    "member_role":    discord.AuditLogAction.member_role_update,
    "role_update":    discord.AuditLogAction.role_update,
    "bot_add":        discord.AuditLogAction.bot_add,
}

# perms that make a role nuke-capable — granting any of these to a role is an
# instant escalation (esp. to @everyone), so it trips on ONE occurrence.
_NUKE_PERMS = discord.Permissions(
    administrator=True, manage_guild=True, manage_roles=True, manage_channels=True,
    manage_webhooks=True, ban_members=True, kick_members=True, mention_everyone=True,
).value


class AntiNuke(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.events = defaultdict(lambda: defaultdict(deque))   # destructive
        self.msgs = defaultdict(deque)                          # author -> msg ts
        self.mentions = defaultdict(deque)                      # author -> (ts, count)
        self.everyone = defaultdict(deque)                      # author -> ts
        self.cooldown = {}                                     # (id, key) -> last trip ts
        self.ban_victims = defaultdict(deque)                  # executor_id -> (ts, victim_uid)

    def _exempt(self, guild, user):
        return (user is None or user.id == self.bot.user.id
                or user.id == guild.owner_id or user.id in WHITELIST)

    def _removable(self, guild, member):
        """Roles we can actually strip: not @everyone, not managed, not the
        quarantine role, and below the bot's top role."""
        me = guild.me
        out = []
        for r in member.roles:
            if r.is_default() or r.managed or r.id == QUARANTINE_ROLE_ID:
                continue
            if me and r >= me.top_role:
                continue
            out.append(r)
        return out

    async def _quarantine_offender(self, guild, member, reason):
        """Strip the offender's removable roles (saved for restore) AND apply the
        quarantine role so they're locked out of every channel. Reversible via
        /altguard-release. Returns True on success."""
        qrole = guild.get_role(QUARANTINE_ROLE_ID)
        removable = self._removable(guild, member)
        try:
            qstore.save(member.id, guild.id, [r.id for r in removable], f"anti-nuke: {reason}")
        except Exception:
            pass  # never let a store hiccup block the neutralization
        rm = set(removable)
        target = [r for r in member.roles if r not in rm]
        if qrole and qrole not in target:
            target.append(qrole)
        try:
            await member.edit(roles=target, reason=f"AntiNuke: {reason} — quarantined")
            return True
        except discord.Forbidden:
            return False

    def _debounced(self, uid, key, window):
        now = time.time()
        if now - self.cooldown.get((uid, key), 0) < window:
            return True
        self.cooldown[(uid, key)] = now
        return False

    # ----------------------------------------------------- destructive (audit)
    async def _executor(self, guild, action, target_id=None):
        try:
            async for e in guild.audit_logs(limit=6, action=_AUDIT[action]):
                if (discord.utils.utcnow() - e.created_at).total_seconds() > 15:
                    continue
                if target_id is None or (e.target and getattr(e.target, "id", None) == target_id):
                    return e.user
        except discord.Forbidden:
            log.warning("anti-nuke can't read audit log (need View Audit Log)")
        except discord.HTTPException:
            pass
        return None

    async def _record_action(self, guild, action, target_id=None):
        if guild.id != GUILD_ID:
            return
        user = await self._executor(guild, action, target_id)
        if self._exempt(guild, user):
            return
        count, window = ACTION_LIMITS[action]
        now = time.time()
        if action == "ban" and target_id:
            bv = self.ban_victims[user.id]
            bv.append((now, target_id))
            while bv and now - bv[0][0] > window + 30:
                bv.popleft()
        dq = self.events[user.id][action]
        dq.append(now)
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= count and not self._debounced(user.id, action, window):
            if action == "ban":
                await self._respond_ban_nuke(guild, user, len(dq), window)
            else:
                await self._respond_strip(guild, user, f"{len(dq)}× {action} in {window}s")

    async def _respond_strip(self, guild, user, why):
        member = guild.get_member(user.id)
        acted, detail = False, ""
        if ENFORCE and member is not None:
            acted = await self._quarantine_offender(guild, member, why)
            if not acted:
                detail = " (couldn't neutralize — check hierarchy/perms)"
        await self._alert(guild, user, "💥 NUKE pattern", why,
                          "stripped + quarantined" if acted else None, detail, acted)

    async def _respond_ban_nuke(self, guild, user, n, window):
        # 1) neutralize the nuker (strip roles + quarantine)
        member = guild.get_member(user.id)
        acted = False
        if ENFORCE and member is not None:
            acted = await self._quarantine_offender(guild, member, f"mass-ban ({n} in {window}s)")
        # 2) unban the victims this nuker banned in the window
        restored = []
        if ENFORCE and RESTORE_BANS:
            now = time.time()
            seen = set()
            for ts, vid in list(self.ban_victims.get(user.id, [])):
                if now - ts <= window + 30 and vid not in seen:
                    seen.add(vid)
                    try:
                        await guild.unban(discord.Object(id=int(vid)),
                                          reason="AntiNuke: mass-ban victim restored")
                        restored.append(vid)
                    except discord.HTTPException:
                        pass
        # 3) recovery invite to share with the restored members
        invite = await self._recovery_invite(guild) if restored else None
        await self._alert_ban_nuke(guild, user, n, window, acted, restored, invite)

    async def _recovery_invite(self, guild):
        ch = guild.system_channel
        if ch is None or not ch.permissions_for(guild.me).create_instant_invite:
            ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite), None)
        if ch is None:
            return None
        try:
            inv = await ch.create_invite(max_age=172800, max_uses=0, unique=True,
                                         reason="AntiNuke mass-ban recovery")
            return inv.url
        except discord.HTTPException:
            return None

    async def _alert_ban_nuke(self, guild, user, n, window, acted, restored, invite):
        ch = guild.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        embed = discord.Embed(
            title="🔨 MASS-BAN NUKE — stopped + recovering", color=0x8B0000,
            description=f"{user.mention} (`{user.id}`) banned **{n}** members in {window}s.\n"
                        + ("**Neutralized** — stripped + quarantined." if acted
                           else "⚠️ couldn't neutralize — check my perms/hierarchy."))
        if restored:
            embed.add_field(name=f"♻️ Auto-unbanned {len(restored)} victim(s)",
                            value=", ".join(f"<@{v}>" for v in restored)[:1024], inline=False)
            if invite:
                embed.add_field(name="📨 Re-invite link (share with them)", value=invite, inline=False)
            embed.set_footer(text="Victims unbanned — they can rejoin via the invite. Re-ban any that were legit.")
        elif ENFORCE and RESTORE_BANS:
            embed.add_field(name="Victims", value="none captured in the window.", inline=False)
        await ch.send(content="@here", embed=embed)

    # ----------------------------------------------------- chat abuse (messages)
    @commands.Cog.listener()
    async def on_message(self, message):
        if (message.guild is None or message.guild.id != GUILD_ID
                or message.author.bot or message.webhook_id):
            return
        if self._exempt(message.guild, message.author):
            return
        uid = message.author.id
        now = time.time()
        nmention = len(message.mentions) + len(message.role_mentions)

        # 1) single-message mention bomb
        if nmention >= MENTION_BOMB:
            if not self._debounced(uid, "bomb", 30):
                await self._respond_timeout(message.guild, message.author,
                                            f"mention-bomb: {nmention} pings in one message")
            return

        # 2) sustained mention rate
        dq = self.mentions[uid]
        if nmention:
            dq.append((now, nmention))
        while dq and now - dq[0][0] > MENTION_RATE[1]:
            dq.popleft()
        if sum(c for _, c in dq) >= MENTION_RATE[0] and not self._debounced(uid, "mrate", MENTION_RATE[1]):
            await self._respond_timeout(message.guild, message.author,
                                        f"ping spam: {sum(c for _,c in dq)} mentions in {MENTION_RATE[1]}s")
            return

        # 3) @everyone / @here spam (only counts pings that actually fired)
        if message.mention_everyone:
            eq = self.everyone[uid]
            eq.append(now)
            while eq and now - eq[0] > EVERYONE_RATE[1]:
                eq.popleft()
            if len(eq) >= EVERYONE_RATE[0] and not self._debounced(uid, "everyone", EVERYONE_RATE[1]):
                await self._respond_timeout(message.guild, message.author,
                                            f"@everyone spam: {len(eq)} in {EVERYONE_RATE[1]}s")
                return

        # 4) message flood
        mq = self.msgs[uid]
        mq.append(now)
        while mq and now - mq[0] > FLOOD_RATE[1]:
            mq.popleft()
        if len(mq) >= FLOOD_RATE[0] and not self._debounced(uid, "flood", FLOOD_RATE[1]):
            await self._respond_timeout(message.guild, message.author,
                                        f"message flood: {len(mq)} msgs in {FLOOD_RATE[1]}s")

    async def _respond_timeout(self, guild, user, why):
        member = guild.get_member(user.id)
        acted, detail = False, ""
        if ENFORCE and member is not None:
            try:
                await member.timeout(datetime.timedelta(minutes=TIMEOUT_MIN),
                                     reason=f"AntiNuke: {why}")
                acted = True
            except discord.Forbidden:
                detail = " (couldn't timeout — check hierarchy/perms)"
        await self._alert(guild, user, "📢 RAID/SPAM pattern", why,
                          f"timed out {TIMEOUT_MIN}m" if acted else None, detail, acted)

    # ----------------------------------------------------------------- alert
    async def _alert(self, guild, user, kind, why, action_txt, detail, acted):
        ch = guild.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        if acted:
            head, color = f"🛡️ ANTI-NUKE — {kind} — neutralized", 0x2ECC71
        elif ENFORCE:
            head, color = f"🚨 ANTI-NUKE — {kind} — action FAILED", 0xE74C3C
        else:
            head, color = f"🚨 ANTI-NUKE would trip — {kind} (shadow)", 0xE0A23B
        embed = discord.Embed(title=head, color=color,
                              description=f"{user.mention} (`{user.id}`): {why}.{detail}")
        embed.add_field(name="Mode", value="ENFORCE" if ENFORCE else "SHADOW (alert-only)", inline=True)
        embed.add_field(name="Action", value=action_txt or ("none — SHADOW" if not ENFORCE else "FAILED"), inline=True)
        if not ENFORCE:
            embed.set_footer(text="Shadow: Wick still enforces. Flip ANTINUKE_ENFORCE=1 when tuned.")
        await ch.send(content="@here" if (acted or ENFORCE) else None, embed=embed)

    # ----------------------------------------------------------- listeners
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, c):
        await self._record_action(c.guild, "channel_delete", c.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, c):
        await self._record_action(c.guild, "channel_create", c.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, r):
        await self._record_action(r.guild, "role_delete", r.id)

    @commands.Cog.listener()
    async def on_guild_role_create(self, r):
        await self._record_action(r.guild, "role_create", r.id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await self._record_action(guild, "ban", user.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self._record_action(member.guild, "kick", member.id)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        await self._record_action(channel.guild, "webhook")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if len(after.roles) > len(before.roles):
            await self._record_action(after.guild, "member_role", after.id)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        # someone edited a role's permissions — if it GAINED nuke-capable perms
        # (e.g. handing @everyone Administrator), revert it + neutralize the actor.
        # One occurrence is enough — there's no legit slow version of this.
        if after.guild.id != GUILD_ID:
            return
        gained = after.permissions.value & ~before.permissions.value
        if not (gained & _NUKE_PERMS):
            return
        user = await self._executor(after.guild, "role_update", after.id)
        if self._exempt(after.guild, user):
            return
        reverted = False
        if ENFORCE:
            try:
                await after.edit(permissions=before.permissions,
                                 reason="AntiNuke: dangerous role-perm grant reverted")
                reverted = True
            except discord.Forbidden:
                pass
        tail = " — reverted" if reverted else (" (revert FAILED)" if ENFORCE else "")
        await self._respond_strip(after.guild, user, f"granted nuke perms to @{after.name}{tail}")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        # bot added to the server = classic one-click nuke vector. Only trusted
        # users (owner/whitelist) may add bots; anyone else -> kick the bot.
        if member.guild.id != GUILD_ID or not member.bot:
            return
        adder = await self._executor(member.guild, "bot_add", member.id)
        had_admin = member.guild_permissions.administrator
        kicked = False
        if ENFORCE and adder is not None and not self._exempt(member.guild, adder):
            try:
                await member.kick(reason="AntiNuke: bot added by non-trusted user")
                kicked = True
            except discord.Forbidden:
                pass
        ch = member.guild.get_channel(MODLOG_CHANNEL_ID)
        if not ch:
            return
        who = adder.mention if adder else "unknown (audit log unavailable)"
        embed = discord.Embed(
            title="🤖 Bot added" + (" — KICKED" if kicked else ""),
            color=0xE74C3C if (kicked or had_admin) else 0xE0A23B,
            description=f"**{member}** (`{member.id}`) was added by {who}."
                        + ("\n⚠️ **arrived with Administrator.**" if had_admin else ""))
        embed.add_field(name="Mode", value="ENFORCE" if ENFORCE else "SHADOW", inline=True)
        embed.add_field(name="Action", value=("bot kicked" if kicked else "alert only"), inline=True)
        if not kicked and had_admin:
            embed.set_footer(text="Bot has admin — verify it's trusted or remove it.")
        await ch.send(content="@here", embed=embed)

    # ----------------------------------------------------------- command
    @app_commands.command(name="antinuke", description="Anti-nuke status & thresholds (admin)")
    @app_commands.default_permissions(administrator=True)
    async def antinuke(self, interaction: discord.Interaction):
        mode = "🔴 ENFORCE" if ENFORCE else "🟡 SHADOW (alert-only, Wick still enforces)"
        wl = ", ".join(f"`{w}`" for w in WHITELIST) or "(owner + bot only)"
        acts = "\n".join(f"• {k}: {c}× / {w}s → strip roles" for k, (c, w) in ACTION_LIMITS.items())
        chat = (f"• mention-bomb: {MENTION_BOMB}+ in one msg → timeout\n"
                f"• ping spam: {MENTION_RATE[0]} mentions / {MENTION_RATE[1]}s → timeout\n"
                f"• @everyone spam: {EVERYONE_RATE[0]} / {EVERYONE_RATE[1]}s → timeout\n"
                f"• msg flood: {FLOOD_RATE[0]} / {FLOOD_RATE[1]}s → timeout\n"
                f"-# one announcement / tagging a few people is FINE")
        embed = discord.Embed(title="🛡️ AntiNuke", color=0x5B8CFF, description=f"**Mode:** {mode}")
        embed.add_field(name="Destructive (audit-log)", value=acts, inline=False)
        embed.add_field(name="Escalation guards", value=(
            "• role edited to grant nuke perms (e.g. @everyone admin) → **revert + strip** (instant)\n"
            "• bot added by non-trusted user → **kick the bot** (instant)"), inline=False)
        embed.add_field(name="Mass-ban recovery", value=(
            "✅ on — victims **auto-unbanned** + re-invite link posted" if RESTORE_BANS
            else "off"), inline=False)
        embed.add_field(name="Chat abuse (messages)", value=chat, inline=False)
        embed.add_field(name="Whitelist (never acted on)", value=f"owner, this bot, bots, {wl}", inline=False)
        embed.set_footer(text="ANTINUKE_ENFORCE=1 + ANTINUKE_WHITELIST=<ids> to go live, then retire Wick.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AntiNuke(bot))
