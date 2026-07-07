"""Native anti-nuke + anti-raid for peepos-reclaimer — multi-guild, opt-in.

Two detectors, both attribute to an executor and trip on RATE (not single
actions — that's the Wick over-strictness we're fixing):

  1. DESTRUCTIVE actions (audit-log): mass channel/role delete+create, mass
     ban/kick, webhook spam, mass role-grants -> response: STRIP offender roles.
  2. CHAT abuse (messages): mention-bombs, sustained ping spam, @everyone spam,
     message flooding -> response: TIMEOUT offender.

Deliberately GENEROUS so normal use passes: ONE announcement (@everyone) is
fine, tagging 5-10 people in a message is fine, normal chatting is fine. Only
sustained/extreme behavior trips.

Per-guild + opt-in: runs only where `antinuke` is enabled in security_config.
Each guild has its own enforce/shadow mode, whitelist, quarantine role, modlog
channel and timeout — read per event from security_config (no module globals).
Never acts on: guild owner, the bot itself, bots/webhooks, or the guild whitelist.
"""
import os
import sys
import time
import datetime
import logging
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

import quarantine_store as qstore  # shared with AltGuard — stores stripped roles for /altguard-release

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.security_config import get_config, set_config, is_enabled

log = logging.getLogger("antinuke")

# destructive actions: key -> (count, window_s). Response = strip roles.
# Global defaults (same for every guild; per-guild tuning can come later).
ACTION_LIMITS = {
    "channel_delete": (3, 12),
    "channel_create": (4, 12),
    "role_delete":    (3, 12),
    "role_create":    (4, 12),
    "ban":            (5, 20),
    "kick":           (5, 20),
    "webhook":        (4, 12),
    "member_role":    (5, 15),   # mass ROLE-GRANTS (a member gaining roles)
    "role_remove":    (5, 15),   # mass ROLE-REMOVES (a member losing roles)
}

# friendly labels for /antinuke status + the set-limit picker.
VECTOR_LABELS = {
    "channel_delete": "channel deletes",
    "channel_create": "channel creates",
    "role_delete":    "role deletes",
    "role_create":    "role creates",
    "ban":            "bans",
    "kick":           "kicks",
    "webhook":        "webhook creates",
    "member_role":    "role grants",
    "role_remove":    "role removes",
}

# chat abuse: GENEROUS thresholds so announcements / normal pings pass clean.
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
    "role_remove":    discord.AuditLogAction.member_role_update,
    "role_update":    discord.AuditLogAction.role_update,
    "bot_add":        discord.AuditLogAction.bot_add,
}

# perms that make a role nuke-capable — granting any of these to a role is an
# instant escalation (esp. to @everyone), so it trips on ONE occurrence.
_NUKE_PERMS = discord.Permissions(
    administrator=True, manage_guild=True, manage_roles=True, manage_channels=True,
    manage_webhooks=True, ban_members=True, kick_members=True, mention_everyone=True,
).value

# the keys-to-the-kingdom — GRANTING a member a role carrying either of these is
# a takeover vector, so it's an INSTANT lockdown (only owner + this bot may do
# it; the general whitelist does NOT apply). Narrower than _NUKE_PERMS on purpose:
# handing out a "helper" role with kick perms shouldn't nuke, but admin must.
_ADMIN_LOCK_PERMS = discord.Permissions(administrator=True, manage_guild=True).value


class AntiNuke(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # all per-guild state is keyed by (guild_id, user_id) so nothing bleeds
        # across the multiple servers the bot lives in.
        self.events = defaultdict(lambda: defaultdict(deque))   # (gid, uid) -> action -> ts deque
        self.msgs = defaultdict(deque)                          # (gid, uid) -> msg ts
        self.mentions = defaultdict(deque)                      # (gid, uid) -> (ts, count)
        self.everyone = defaultdict(deque)                      # (gid, uid) -> ts
        self.cooldown = {}                                      # (gid, uid, key) -> last trip ts
        self.ban_victims = defaultdict(deque)                  # (gid, executor) -> (ts, victim_uid)

    # ------------------------------------------------------------- config helpers
    @staticmethod
    def _enforce(cfg):
        return bool(cfg.get("antinuke_enforce"))

    def _exempt(self, guild, user, cfg):
        wl = set(cfg.get("whitelist") or [])
        return (user is None or user.id == self.bot.user.id
                or user.id == guild.owner_id or user.id in wl)

    def _modlog(self, guild, cfg):
        mid = cfg.get("modlog_channel_id")
        return guild.get_channel(int(mid)) if mid else None

    @staticmethod
    def _limits(cfg):
        """Effective (count, window) per vector — per-guild overrides layered on
        the code defaults."""
        lim = dict(ACTION_LIMITS)
        for k, v in (cfg.get("antinuke_limits") or {}).items():
            if k in lim and isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    lim[k] = (max(1, int(v[0])), max(1, int(v[1])))
                except (TypeError, ValueError):
                    pass
        return lim

    def _removable(self, guild, member, cfg):
        """Roles we can actually strip: not @everyone, not managed, not the
        quarantine role, and below the bot's top role."""
        me = guild.me
        qid = cfg.get("quarantine_role_id")
        out = []
        for r in member.roles:
            if r.is_default() or r.managed or (qid and r.id == int(qid)):
                continue
            if me and r >= me.top_role:
                continue
            out.append(r)
        return out

    async def _quarantine_offender(self, guild, member, reason, cfg):
        """Strip the offender's removable roles (saved for restore) AND apply the
        quarantine role so they're locked out of every channel. Reversible via
        /altguard-release. Returns True on success."""
        qid = cfg.get("quarantine_role_id")
        qrole = guild.get_role(int(qid)) if qid else None
        removable = self._removable(guild, member, cfg)
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

    def _debounced(self, gid, uid, key, window):
        now = time.time()
        if now - self.cooldown.get((gid, uid, key), 0) < window:
            return True
        self.cooldown[(gid, uid, key)] = now
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
        if not is_enabled(guild.id, "antinuke"):
            return
        cfg = get_config(guild.id)
        user = await self._executor(guild, action, target_id)
        if self._exempt(guild, user, cfg):
            return
        gid = guild.id
        count, window = self._limits(cfg)[action]
        now = time.time()
        if action == "ban" and target_id:
            bv = self.ban_victims[(gid, user.id)]
            bv.append((now, target_id))
            while bv and now - bv[0][0] > window + 30:
                bv.popleft()
        dq = self.events[(gid, user.id)][action]
        dq.append(now)
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= count and not self._debounced(gid, user.id, action, window):
            if action == "ban":
                await self._respond_ban_nuke(guild, user, len(dq), window, cfg)
            else:
                await self._respond_strip(guild, user, f"{len(dq)}× {action} in {window}s", cfg)

    async def _respond_strip(self, guild, user, why, cfg):
        member = guild.get_member(user.id)
        acted, detail = False, ""
        if self._enforce(cfg) and member is not None:
            acted = await self._quarantine_offender(guild, member, why, cfg)
            if not acted:
                detail = " (couldn't neutralize — check hierarchy/perms)"
        await self._alert(guild, user, "💥 NUKE pattern", why,
                          "stripped + quarantined" if acted else None, detail, acted, cfg)

    async def _respond_ban_nuke(self, guild, user, n, window, cfg):
        # 1) neutralize the nuker (strip roles + quarantine)
        member = guild.get_member(user.id)
        acted = False
        if self._enforce(cfg) and member is not None:
            acted = await self._quarantine_offender(guild, member, f"mass-ban ({n} in {window}s)", cfg)
        # 2) unban the victims this nuker banned in the window
        restored = []
        if self._enforce(cfg) and cfg.get("antinuke_restore_bans"):
            now = time.time()
            seen = set()
            for ts, vid in list(self.ban_victims.get((guild.id, user.id), [])):
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
        await self._alert_ban_nuke(guild, user, n, window, acted, restored, invite, cfg)

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

    async def _alert_ban_nuke(self, guild, user, n, window, acted, restored, invite, cfg):
        ch = self._modlog(guild, cfg)
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
        elif self._enforce(cfg) and cfg.get("antinuke_restore_bans"):
            embed.add_field(name="Victims", value="none captured in the window.", inline=False)
        await ch.send(content="@here", embed=embed)

    # ----------------------------------------------------- chat abuse (messages)
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        if not is_enabled(message.guild.id, "antinuke"):
            return
        cfg = get_config(message.guild.id)
        if self._exempt(message.guild, message.author, cfg):
            return
        gid = message.guild.id
        uid = message.author.id
        now = time.time()
        nmention = len(message.mentions) + len(message.role_mentions)

        # 1) single-message mention bomb
        if nmention >= MENTION_BOMB:
            if not self._debounced(gid, uid, "bomb", 30):
                await self._respond_timeout(message.guild, message.author,
                                            f"mention-bomb: {nmention} pings in one message", cfg)
            return

        # 2) sustained mention rate
        dq = self.mentions[(gid, uid)]
        if nmention:
            dq.append((now, nmention))
        while dq and now - dq[0][0] > MENTION_RATE[1]:
            dq.popleft()
        if sum(c for _, c in dq) >= MENTION_RATE[0] and not self._debounced(gid, uid, "mrate", MENTION_RATE[1]):
            await self._respond_timeout(message.guild, message.author,
                                        f"ping spam: {sum(c for _,c in dq)} mentions in {MENTION_RATE[1]}s", cfg)
            return

        # 3) @everyone / @here spam (only counts pings that actually fired)
        if message.mention_everyone:
            eq = self.everyone[(gid, uid)]
            eq.append(now)
            while eq and now - eq[0] > EVERYONE_RATE[1]:
                eq.popleft()
            if len(eq) >= EVERYONE_RATE[0] and not self._debounced(gid, uid, "everyone", EVERYONE_RATE[1]):
                await self._respond_timeout(message.guild, message.author,
                                            f"@everyone spam: {len(eq)} in {EVERYONE_RATE[1]}s", cfg)
                return

        # 4) message flood — per channel; spam channels exempt, per-channel and
        #    server-wide overrides via /antinuke messages-allowed.
        cid = getattr(message.channel, "id", None)
        if cid in set(cfg.get("antinuke_spam_channels") or []):
            return  # this channel is "allowed to be spammed"
        fcount, fwin = FLOOD_RATE
        base = cfg.get("antinuke_flood")
        if isinstance(base, (list, tuple)) and len(base) == 2:
            fcount, fwin = max(1, int(base[0])), max(1, int(base[1]))
        ov = (cfg.get("antinuke_channel_flood") or {}).get(str(cid))
        if isinstance(ov, (list, tuple)) and len(ov) == 2:
            fcount, fwin = max(1, int(ov[0])), max(1, int(ov[1]))
        mq = self.msgs[(gid, uid, cid)]
        mq.append(now)
        while mq and now - mq[0] > fwin:
            mq.popleft()
        if len(mq) >= fcount and not self._debounced(gid, uid, "flood", fwin):
            await self._respond_timeout(message.guild, message.author,
                                        f"message flood: {len(mq)} msgs in {fwin}s", cfg)

    async def _respond_timeout(self, guild, user, why, cfg):
        member = guild.get_member(user.id)
        acted, detail = False, ""
        if self._enforce(cfg) and member is not None:
            try:
                await member.timeout(datetime.timedelta(minutes=cfg.get("antinuke_timeout_min", 10)),
                                     reason=f"AntiNuke: {why}")
                acted = True
            except discord.Forbidden:
                detail = " (couldn't timeout — check hierarchy/perms)"
        await self._alert(guild, user, "📢 RAID/SPAM pattern", why,
                          f"timed out {cfg.get('antinuke_timeout_min', 10)}m" if acted else None, detail, acted, cfg)

    # ----------------------------------------------------------------- alert
    async def _alert(self, guild, user, kind, why, action_txt, detail, acted, cfg):
        ch = self._modlog(guild, cfg)
        if not ch:
            return
        enforce = self._enforce(cfg)
        if acted:
            head, color = f"🛡️ ANTI-NUKE — {kind} — neutralized", 0x2ECC71
        elif enforce:
            head, color = f"🚨 ANTI-NUKE — {kind} — action FAILED", 0xE74C3C
        else:
            head, color = f"🚨 ANTI-NUKE would trip — {kind} (shadow)", 0xE0A23B
        embed = discord.Embed(title=head, color=color,
                              description=f"{user.mention} (`{user.id}`): {why}.{detail}")
        embed.add_field(name="Mode", value="ENFORCE" if enforce else "SHADOW (alert-only)", inline=True)
        embed.add_field(name="Action", value=action_txt or ("none — SHADOW" if not enforce else "FAILED"), inline=True)
        if not enforce:
            embed.set_footer(text="Shadow mode — alert only. Enable enforce in /security to act.")
        await ch.send(content="@here" if (acted or enforce) else None, embed=embed)

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
        if not is_enabled(after.guild.id, "antinuke"):
            return
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added:
            # keys-to-the-kingdom grant = instant lockdown (owner + bot only),
            # independent of the rate limit.
            await self._check_admin_grant(after, added)
            await self._record_action(after.guild, "member_role", after.id)
        if removed:
            await self._record_action(after.guild, "role_remove", after.id)

    async def _check_admin_grant(self, member, added_roles):
        """Granting a role carrying Administrator / Manage-Server is a takeover
        vector. ONLY the guild owner and this bot may do it — NOT the general
        whitelist. Anyone else: revert the grant + strip the granter, instantly."""
        guild = member.guild
        cfg = get_config(guild.id)
        if not cfg.get("antinuke_admin_lockdown", 1):
            return
        dangerous = [r for r in added_roles
                     if (getattr(r.permissions, "value", 0) & _ADMIN_LOCK_PERMS)]
        if not dangerous:
            return
        ex = await self._executor(guild, "member_role", member.id)
        if ex is None or ex.id == self.bot.user.id or ex.id == guild.owner_id:
            return  # owner + bot are the ONLY authorized admin-granters
        reverted = False
        if self._enforce(cfg):
            try:
                await member.remove_roles(*dangerous,
                    reason="AntiNuke: unauthorized admin-role grant reverted")
                reverted = True
            except discord.Forbidden:
                pass
        names = ", ".join("@" + r.name for r in dangerous)
        tail = " — reverted" if reverted else (" (revert FAILED)" if self._enforce(cfg) else "")
        await self._respond_strip(guild, ex,
            f"granted admin role(s) {names} to {member}{tail}", cfg)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        # someone edited a role's permissions — if it GAINED nuke-capable perms
        # (e.g. handing @everyone Administrator), revert it + neutralize the actor.
        # One occurrence is enough — there's no legit slow version of this.
        if not is_enabled(after.guild.id, "antinuke"):
            return
        cfg = get_config(after.guild.id)
        gained = after.permissions.value & ~before.permissions.value
        if not (gained & _NUKE_PERMS):
            return
        user = await self._executor(after.guild, "role_update", after.id)
        if self._exempt(after.guild, user, cfg):
            return
        reverted = False
        if self._enforce(cfg):
            try:
                await after.edit(permissions=before.permissions,
                                 reason="AntiNuke: dangerous role-perm grant reverted")
                reverted = True
            except discord.Forbidden:
                pass
        tail = " — reverted" if reverted else (" (revert FAILED)" if self._enforce(cfg) else "")
        await self._respond_strip(after.guild, user, f"granted nuke perms to @{after.name}{tail}", cfg)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        # bot added to the server = classic one-click nuke vector. Only trusted
        # users (owner/whitelist) may add bots; anyone else -> kick the bot.
        if not member.bot or not is_enabled(member.guild.id, "antinuke"):
            return
        cfg = get_config(member.guild.id)
        adder = await self._executor(member.guild, "bot_add", member.id)
        had_admin = member.guild_permissions.administrator
        kicked = False
        if self._enforce(cfg) and adder is not None and not self._exempt(member.guild, adder, cfg):
            try:
                await member.kick(reason="AntiNuke: bot added by non-trusted user")
                kicked = True
            except discord.Forbidden:
                pass
        ch = self._modlog(member.guild, cfg)
        if not ch:
            return
        who = adder.mention if adder else "unknown (audit log unavailable)"
        embed = discord.Embed(
            title="🤖 Bot added" + (" — KICKED" if kicked else ""),
            color=0xE74C3C if (kicked or had_admin) else 0xE0A23B,
            description=f"**{member}** (`{member.id}`) was added by {who}."
                        + ("\n⚠️ **arrived with Administrator.**" if had_admin else ""))
        embed.add_field(name="Mode", value="ENFORCE" if self._enforce(cfg) else "SHADOW", inline=True)
        embed.add_field(name="Action", value=("bot kicked" if kicked else "alert only"), inline=True)
        if not kicked and had_admin:
            embed.set_footer(text="Bot has admin — verify it's trusted or remove it.")
        await ch.send(content="@here", embed=embed)

    # ----------------------------------------------------------- commands
    group = app_commands.Group(
        name="antinuke", description="Anti-nuke status & per-vector thresholds (admin)",
        default_permissions=discord.Permissions(administrator=True), guild_only=True)

    @group.command(name="status", description="Show anti-nuke mode & all thresholds")
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild.id)
        on = bool(cfg.get("antinuke_enabled"))
        enforce = self._enforce(cfg)
        if not on:
            mode = "⚪ DISABLED for this server — run `/security setup` to enable"
        else:
            mode = "🔴 ENFORCE" if enforce else "🟡 SHADOW (alert-only)"
        lim = self._limits(cfg)
        acts = "\n".join(f"• {VECTOR_LABELS.get(k, k)}: **{c}× / {w}s** → strip roles"
                         for k, (c, w) in lim.items())
        # message flood (server default + any per-channel overrides)
        fb = cfg.get("antinuke_flood")
        fb = (int(fb[0]), int(fb[1])) if isinstance(fb, (list, tuple)) and len(fb) == 2 else FLOOD_RATE
        flood = f"• msg flood: **{fb[0]} / {fb[1]}s** → timeout (server default)"
        for cid, v in (cfg.get("antinuke_channel_flood") or {}).items():
            if isinstance(v, (list, tuple)) and len(v) == 2:
                flood += f"\n• <#{cid}>: **{int(v[0])} / {int(v[1])}s**"
        spam = cfg.get("antinuke_spam_channels") or []
        spam_txt = ", ".join(f"<#{c}>" for c in spam) if spam else "none"
        chat = (f"• mention-bomb: {MENTION_BOMB}+ in one msg → timeout\n"
                f"• ping spam: {MENTION_RATE[0]} mentions / {MENTION_RATE[1]}s → timeout\n"
                f"• @everyone spam: {EVERYONE_RATE[0]} / {EVERYONE_RATE[1]}s → timeout\n"
                f"{flood}\n-# one announcement / tagging a few people is FINE")
        wl = ", ".join(f"`{w}`" for w in (cfg.get("whitelist") or [])) or "(owner + bot only)"
        lock = "✅ ON — only owner + this bot may grant admin" if cfg.get("antinuke_admin_lockdown", 1) else "⚠️ OFF"
        embed = discord.Embed(title="🛡️ AntiNuke", color=0x5B8CFF, description=f"**Mode:** {mode}")
        embed.add_field(name="Destructive (audit-log)", value=acts, inline=False)
        embed.add_field(name="Chat abuse (messages)", value=chat, inline=False)
        embed.add_field(name="Spam-allowed channels", value=spam_txt, inline=False)
        embed.add_field(name="Admin-grant lockdown", value=lock, inline=False)
        embed.add_field(name="Escalation guards", value=(
            "• role edited to grant nuke perms → **revert + strip** (instant)\n"
            "• admin role granted to a member by non-owner → **revert + strip** (instant)\n"
            "• bot added by non-trusted user → **kick the bot** (instant)"), inline=False)
        embed.add_field(name="Whitelist (rate-limits waived)", value=f"owner, this bot, bots, {wl}", inline=False)
        embed.set_footer(text="Tune with /antinuke messages-allowed · role-grants · role-removes · set-limit")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _set_vector(self, interaction, vector, count, window):
        cfg = get_config(interaction.guild.id)
        limits = dict(cfg.get("antinuke_limits") or {})
        limits[vector] = [int(count), int(window)]
        set_config(interaction.guild.id, antinuke_limits=limits)
        await interaction.response.send_message(
            f"✅ **{VECTOR_LABELS.get(vector, vector)}** limit set to **{count}× / {window}s** "
            f"before strip + quarantine.", ephemeral=True)

    @group.command(name="role-grants", description="Limit how many role-GRANTS an actor may do before it's a nuke")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(count="role grants allowed", window="within this many seconds")
    async def role_grants(self, interaction: discord.Interaction,
                          count: app_commands.Range[int, 1, 100], window: app_commands.Range[int, 1, 600]):
        await self._set_vector(interaction, "member_role", count, window)

    @group.command(name="role-removes", description="Limit how many role-REMOVES an actor may do before it's a nuke")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(count="role removals allowed", window="within this many seconds")
    async def role_removes(self, interaction: discord.Interaction,
                           count: app_commands.Range[int, 1, 100], window: app_commands.Range[int, 1, 600]):
        await self._set_vector(interaction, "role_remove", count, window)

    @group.command(name="set-limit", description="Set the rate limit for any destructive vector")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(vector="which action", count="allowed in the window", window="within this many seconds")
    @app_commands.choices(vector=[
        app_commands.Choice(name=lbl, value=key) for key, lbl in VECTOR_LABELS.items()])
    async def set_limit(self, interaction: discord.Interaction, vector: app_commands.Choice[str],
                        count: app_commands.Range[int, 1, 100], window: app_commands.Range[int, 1, 600]):
        await self._set_vector(interaction, vector.value, count, window)

    @group.command(name="messages-allowed", description="Set the message-flood limit (optionally per channel)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(count="messages allowed", window="within this many seconds",
                           channel="only this channel (omit = server-wide default)")
    async def messages_allowed(self, interaction: discord.Interaction,
                               count: app_commands.Range[int, 1, 500], window: app_commands.Range[int, 1, 600],
                               channel: discord.TextChannel = None):
        gid = interaction.guild.id
        cfg = get_config(gid)
        if channel:
            m = dict(cfg.get("antinuke_channel_flood") or {})
            m[str(channel.id)] = [int(count), int(window)]
            set_config(gid, antinuke_channel_flood=m)
            where = f"in {channel.mention}"
        else:
            set_config(gid, antinuke_flood=[int(count), int(window)])
            where = "server-wide"
        await interaction.response.send_message(
            f"✅ Message flood {where}: **{count} msgs / {window}s** before timeout.", ephemeral=True)

    @group.command(name="whitelist-channel", description="Exempt a channel from flood limits (allowed to be spammed)")
    @app_commands.checks.has_permissions(administrator=True)
    async def whitelist_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        gid = interaction.guild.id
        spam = list(cfg_spam := (get_config(gid).get("antinuke_spam_channels") or []))
        if channel.id in spam:
            await interaction.response.send_message(f"{channel.mention} is already spam-allowed.", ephemeral=True)
            return
        spam.append(channel.id)
        set_config(gid, antinuke_spam_channels=spam)
        await interaction.response.send_message(
            f"✅ {channel.mention} is now **spam-allowed** — message-flood limits won't fire there. "
            f"(@everyone / mention-bomb protection still applies.)", ephemeral=True)

    @group.command(name="unwhitelist-channel", description="Re-apply flood limits to a channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def unwhitelist_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        gid = interaction.guild.id
        spam = list(get_config(gid).get("antinuke_spam_channels") or [])
        if channel.id not in spam:
            await interaction.response.send_message(f"{channel.mention} wasn't spam-allowed.", ephemeral=True)
            return
        spam.remove(channel.id)
        set_config(gid, antinuke_spam_channels=spam)
        await interaction.response.send_message(
            f"✅ Flood limits re-applied to {channel.mention}.", ephemeral=True)

    @group.command(name="admin-lockdown", description="Toggle: only owner + this bot may grant Administrator")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_lockdown(self, interaction: discord.Interaction, enabled: bool):
        set_config(interaction.guild.id, antinuke_admin_lockdown=1 if enabled else 0)
        await interaction.response.send_message(
            ("🔒 Admin-grant lockdown **ON** — only you and this bot can hand out Administrator / Manage-Server; "
             "anyone else's grant is instantly reverted." if enabled
             else "⚠️ Admin-grant lockdown **OFF** — admin grants now only caught by the rate limit."),
            ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You need the **Administrator** permission to use this."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AntiNuke(bot))
